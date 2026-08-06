"""Background worker that drains the Anime4K upscale queue.

A single daemon thread claims one queued upscale job at a time from the DB
(``upscale_queue`` table), runs it through ``mediaforge.anime4k``, and writes
progress/status back to the DB so the UI can poll it. Also exposes a helper
to enqueue newly-downloaded episodes for upscaling.

Used by: web/app.py (starts the worker at startup) and web/queue_worker.py
(enqueues episodes after a download finishes).

Telemetry: flag.upscale fires once per actually started job (right after a
job is claimed and its settings are read), detail.upscale once per finished
job with the preset/engine used and the duration -- never a title or path.
A job the user cancelled produces no detail event at all (see
telemetry/classify.py: a cancel is the app working correctly, not an error).
"""

import os
import threading
import time
import uuid

from ..config import MEDIAFORGE_TEMP_DIR
from ..logger import get_logger
from ..telemetry import classify as telemetry_classify
from ..telemetry import client as telemetry_client
from ..telemetry import events as telemetry_events
from . import worker_registry as _wr
from . import worker_watchdog as _watchdog
from .media_publish import publish_output, sweep_stale_temp_files
from .db import (
    add_to_upscale_queue,
    append_download_upscale_file,
    claim_next_upscale_queued,
    finalize_upscale_item,
    is_upscale_cancelled,
    get_setting,
    get_upscale_files,
    reset_running_upscale_items,
    set_upscale_error,
    set_upscale_status,
    update_upscale_progress,
)
from .runtime_state import (
    _upscale_active_cancel_events,
    _upscale_cancel_lock,
)

logger = get_logger(__name__)


# Upscale worker state
_upscale_worker_started = False
# Guards both the one-time worker startup (_ensure_upscale_worker) and the
# claim call inside the loop below. claim_next_upscale_queued() is already
# atomic at the DB level (BEGIN IMMEDIATE), so this is a defensive second
# layer rather than the only thing preventing double-processing.
_upscale_lock = threading.Lock()


def _report_upscale_started():
    """Submit the flag.upscale stage-2 usage counter for one started job.

    A pure counter -- build_feature_flag_event() takes no metadata at all.
    Wrapped in its own try/except so a telemetry bug can never affect the
    upscale worker itself (same defensive pattern as encoding_worker.py).
    """
    try:
        telemetry_client.submit(telemetry_events.build_feature_flag_event("flag.upscale"))
    except Exception:
        logger.debug("[Telemetry] failed to build/submit flag.upscale event", exc_info=True)


def _report_upscale_detail(settings, *, status, duration=None, failed_files=None,
                           total_files=None, error_type=None):
    """Submit a detail.upscale event for one finished job (see registry.py's
    "detail.upscale" -- "Welches Upscaling-Preset verwendet wurde und ob der
    Vorgang erfolgreich war").

    Metadata is limited to the shader/engine settings, the duration and the
    file counts -- never a title or a file path, per the stage-3 "kein
    Titel/Inhalt" rule. Callers must not pass a cancel status here; a job the
    user stopped is not reported at all (checked again below as a safety net).
    """
    try:
        if telemetry_classify.is_cancel_status(status):
            return
        metadata = {}
        for key in ("preset", "quality", "resolution", "engine", "out_vcodec"):
            value = (settings or {}).get(key)
            if value:
                metadata[key] = value
        if duration is not None:
            metadata["duration_seconds"] = round(duration, 1)
        if total_files is not None:
            metadata["total_files"] = total_files
        if failed_files is not None:
            metadata["failed_files"] = failed_files
        if error_type:
            metadata["error_type"] = error_type
        event = telemetry_events.build_feature_detail_event(
            "detail.upscale", action="run", status=status, metadata=metadata,
        )
        if event:
            telemetry_client.submit(event)
    except Exception:
        logger.debug("[Telemetry] failed to build/submit detail.upscale event", exc_info=True)


def _upscale_worker():
    """Single global worker loop: claim one queued job, process it fully, repeat.

    Runs forever on its own daemon thread (started once via
    _ensure_upscale_worker). Any exception inside the loop is caught so the
    worker keeps running instead of dying; on error it tries to mark the
    current item "failed" and sleeps 5s before retrying the loop.
    """
    while True:
        # Bound outside the try, so the finally can always stop it -- inside,
        # an exception on the very first statement would leave _hb undefined
        # and turn a worker error into a NameError.
        _hb = None
        try:
            item = None
            _final_status_set = False
            # Telemetry bookkeeping for this job: wall-clock start and the
            # settings actually used. Bound out here because the outer except
            # handler reads them, and a job can die before they are filled.
            _job_started = time.monotonic()
            settings = None

            # The watchdog asked this worker to come back. Honoured here,
            # between jobs, which is the only point at which unwinding is
            # safe: mid-job there is a half-written file and a claimed queue
            # row. See worker_watchdog.py.
            if _watchdog.restart_requested("upscale"):
                logger.warning("[Upscale] Restart requested by the watchdog — unwinding")
                _wr.idle("upscale", detail="restarted by watchdog")
                # Also clears _upscale_worker_started; see worker_exiting().
                _watchdog.worker_exiting("upscale")
                return

            # An active maintenance window can forbid upscaling outright.
            # Checked BEFORE claiming, so a held job stays "queued" instead of
            # being marked running for the length of the window.
            try:
                from .maintenance import is_allowed as _mw_allows
                if not _mw_allows("upscale"):
                    _wr.idle("upscale", detail="held by a maintenance window")
                    time.sleep(30)
                    continue
            except Exception:
                pass  # a restriction that cannot be read is not a restriction

            with _upscale_lock:
                item = claim_next_upscale_queued()

            if not item:
                _wr.idle("upscale")
                time.sleep(4)
                continue

            # Heartbeat for the whole job, not just its start. Upscaling one
            # episode routinely runs for hours; beating only at the edges is
            # what made a perfectly healthy job show up as overdue in the
            # Operations view, and what would now trip the stall watchdog.
            _hb = _watchdog.heartbeat_ticker(
                "upscale", detail=item.get("title") or item.get("file_path", ""))
            _hb.start()

            try:
                from .audit_hooks import record_job

                record_job("upscale", "upscale_started",
                           item.get("title") or item.get("file_path", ""),
                           job_id=item.get("id"))
            except Exception:
                logger.debug("[Upscale] Audit hook failed", exc_info=True)

            # An item whose file an encode still has to touch is never
            # handed out in the first place -- claim_next_upscale_queued()
            # skips those and takes the next candidate, so one blocked job no
            # longer stalls the whole queue.

            cancel_ev = threading.Event()
            with _upscale_cancel_lock:
                _upscale_active_cancel_events[item["id"]] = cancel_ev

            try:
                from ..anime4k.anime4k import upscale_file, get_upscale_progress
                from ..anime4k.anime4k import _upscale_progress, _upscale_progress_lock
            except ImportError:
                set_upscale_status(item["id"], "failed")
                set_upscale_error(item["id"], "anime4k Modul nicht gefunden")
                continue

            settings = {
                "preset":     get_setting("upscaling_shader_preset", "B"),
                "quality":    get_setting("upscaling_shader_quality", "high"),
                "resolution": get_setting("upscaling_resolution", "1080p"),
                "engine":     get_setting("upscaling_engine", "auto"),
                "out_vcodec": get_setting("upscaling_out_vcodec", "libx264"),
                "out_crf":    int(get_setting("upscaling_out_crf", "18") or "18"),
                "out_preset": get_setting("upscaling_out_preset", "medium"),
            }

            # Telemetry: stage-2 usage counter -- one event per job that
            # actually starts processing (not for a job that never got past
            # the missing-module check above).
            _job_started = time.monotonic()
            _report_upscale_started()

            # Progress-poll thread: mirrors anime4k live progress -> DB every 2s
            import threading as _th
            _poll_stop = _th.Event()
            def _progress_poller():
                while not _poll_stop.wait(2):
                    prog = get_upscale_progress()
                    if prog.get("active") and not is_upscale_cancelled(item["id"]):
                        _cur_idx = item.get("_runtime_file_idx", 0)
                        _tot = max(item.get("total_files", 1), 1)
                        _base = _cur_idx / _tot * 100
                        _file_pct = prog.get("percent", 0.0) / _tot
                        update_upscale_progress(item["id"],
                            min(round(_base + _file_pct, 1), 99.9))
            _pt = _th.Thread(target=_progress_poller, daemon=True)
            _pt.start()

            import json as _wjson
            from pathlib import Path as _WPath

            # Build file list: multi-file entries store JSON in .files column.
            # It is re-read before every file because an "after download" job
            # for a season grows while it runs — the download worker appends
            # each episode as soon as that episode is on disk.
            def _current_files():
                try:
                    files = get_upscale_files(item["id"])
                except Exception as _le:
                    logger.warning(f"[Upscale] Dateiliste konnte nicht neu gelesen werden: {_le}")
                    files = []
                if files:
                    return files
                _raw_files = item.get("files")
                if _raw_files:
                    try:
                        return _wjson.loads(_raw_files)
                    except Exception:
                        pass
                return [{"file_path": item["file_path"],
                         "output_path": item.get("output_path") or item["file_path"]}]

            _file_list = _current_files()
            _total_files = max(len(_file_list), 1)
            _overall_failed = 0
            _fi = 0

            while True:
                if is_upscale_cancelled(item["id"]):
                    break

                _file_list = _current_files()
                _total_files = max(len(_file_list), 1)
                if _fi >= len(_file_list):
                    # Nothing left in the list. finalize_upscale_item() takes
                    # the same DB lock as the append, so a file added in this
                    # very moment is not lost: it makes this return False and
                    # the loop picks the new entry up on the next round.
                    try:
                        if finalize_upscale_item(item["id"], _fi):
                            break
                    except Exception as _fex:
                        logger.warning(f"[Upscale] Abschluss-Check fehlgeschlagen: {_fex}")
                        break
                    continue

                _fentry = _file_list[_fi]
                file_path   = _fentry["file_path"]
                output_path = _fentry.get("output_path") or file_path

                temp_output = str(MEDIAFORGE_TEMP_DIR / f"{_WPath(file_path).stem}_{uuid.uuid4().hex[:8]}_upscale_tmp.mkv")
                actual_output = output_path

                # Track current file index / list length (poller reads both)
                item["_runtime_file_idx"] = _fi
                item["total_files"] = _total_files
                update_upscale_progress(item["id"],
                    round(_fi / _total_files * 100, 1),
                    current_file_idx=_fi)

                _published = False
                try:
                    # See encoding_worker.py: the scratch dir sits on the OS
                    # temp volume and may have been wiped between runs. Inside
                    # the try so a permission problem fails this one file
                    # rather than the whole queue item.
                    os.makedirs(MEDIAFORGE_TEMP_DIR, exist_ok=True)
                    # The source can be gone by now (deleted in the library,
                    # replaced by an after-download encode). Say so plainly
                    # instead of letting ffmpeg fail with an opaque error.
                    if not _WPath(file_path).exists():
                        raise FileNotFoundError(f"Datei nicht gefunden: {file_path}")
                    upscale_file(
                        input_path=file_path,
                        output_path=temp_output,
                        settings=settings,
                        cancel_event=cancel_ev,
                        label=item.get("title", ""),
                    )
                    if not is_upscale_cancelled(item["id"]):
                        # Never unlink the original first: publish_output()
                        # stages the result next to its destination and swaps
                        # it in atomically, so a failed copy cannot leave the
                        # user without a file. See web/media_publish.py.
                        publish_output(temp_output, actual_output)
                        _published = True
                except Exception as _fe:
                    _overall_failed += 1
                    logger.error(f"[Upscale] Fehler bei {file_path}: {_fe}")
                    # Continue with next file unless cancelled
                    if is_upscale_cancelled(item["id"]):
                        break
                finally:
                    # Covers the cancel path too: a job stopped between the
                    # finished upscale and the publish step used to leave a
                    # full-size scratch file behind forever.
                    if not _published:
                        try:
                            _WPath(temp_output).unlink(missing_ok=True)
                        except Exception:
                            pass
                    _fi += 1

            # Final status. Counts refer to what was actually processed (_fi),
            # not to the list length at claim time — the list can have grown.
            _done_files = max(_fi, 1)
            if not is_upscale_cancelled(item["id"]):
                update_upscale_progress(item["id"], 100.0, current_file_idx=_done_files)
                if _overall_failed == 0:
                    set_upscale_status(item["id"], "completed")
                elif _overall_failed < _done_files:
                    set_upscale_status(item["id"], "completed")
                    set_upscale_error(item["id"], f"{_overall_failed}/{_done_files} Datei(en) fehlgeschlagen")
                else:
                    set_upscale_status(item["id"], "failed")
                    set_upscale_error(item["id"], f"Alle {_done_files} Datei(en) fehlgeschlagen")
                _final_status_set = True

            _poll_stop.set()
            _pt.join(timeout=3)

            _was_cancelled = is_upscale_cancelled(item["id"])
            if _was_cancelled:
                set_upscale_status(item["id"], "cancelled")
                _final_status_set = True

            # Telemetry: stage-3 detail for the finished job. A cancelled job
            # is the user stopping something, so nothing is reported for it --
            # neither a success nor an error.
            if not _was_cancelled:
                _report_upscale_detail(
                    settings,
                    # Any failed file makes the run an "error" -- the exact
                    # ratio is in failed_files/total_files, so a partial
                    # failure stays distinguishable from a total one.
                    status="error" if _overall_failed else "success",
                    duration=time.monotonic() - _job_started,
                    failed_files=_overall_failed,
                    total_files=_done_files,
                )

            with _upscale_cancel_lock:
                _upscale_active_cancel_events.pop(item["id"], None)

            # Job over: stamp last_run and go back to idle, so the Operations
            # view stops showing a stall countdown for a worker with nothing
            # to do.
            _wr.done("upscale", detail="")

            try:
                from .audit_hooks import record_job

                if _was_cancelled:
                    _action = "upscale_cancelled"
                elif _overall_failed:
                    _action = "upscale_failed"
                else:
                    _action = "upscale_completed"
                record_job("upscale", _action,
                           item.get("title") or item.get("file_path", ""),
                           job_id=item.get("id"), failed=_overall_failed)
            except Exception:
                logger.debug("[Upscale] Audit hook failed", exc_info=True)

        except Exception as e:
            logger.error(f"[Upscale] Worker-Fehler: {e}", exc_info=True)
            _wr.fail("upscale", str(e), detail=(item or {}).get("title", ""))
            # Telemetry: a crashed job counts as a failed run -- but only when
            # the exception is a real defect. A user-initiated cancel raised as
            # an exception must never show up as an error (telemetry/classify.py),
            # and neither must a crash on a job the user had already stopped.
            _cancel_seen = False
            if item is not None:
                try:
                    _cancel_seen = bool(is_upscale_cancelled(item["id"]))
                except Exception:
                    _cancel_seen = False
            if (item is not None and settings is not None and not _cancel_seen
                    and not telemetry_classify.is_user_cancellation(type(e), e)):
                _report_upscale_detail(
                    settings, status="error",
                    duration=time.monotonic() - _job_started,
                    error_type=type(e).__name__,
                )
            if item is not None and not _final_status_set:
                try:
                    if not is_upscale_cancelled(item["id"]):
                        set_upscale_status(item["id"], "failed")
                        set_upscale_error(item["id"], f"Upscale worker error: {str(e)}")
                except Exception as db_err:
                    logger.error(f"[Upscale] Failed to set status to failed for item {item['id']}: {db_err}", exc_info=True)
            time.sleep(5)
        finally:
            # Every exit from an iteration goes through here -- normal end,
            # `continue` out of a guard clause, or an exception. A ticker that
            # outlived its job would report a finished worker as working, and
            # the stall watchdog would eventually "restart" a worker that is
            # sitting idle.
            if _hb is not None:
                _hb.stop()


def _ensure_upscale_worker():
    """Start the upscale worker thread once (idempotent).

    Used by: web/app.py (called during app startup).
    """
    global _upscale_worker_started
    with _upscale_lock:
        if _upscale_worker_started:
            return
        _upscale_worker_started = True
    reset_running_upscale_items()
    sweep_stale_temp_files("_upscale_tmp.mkv")
    thread = threading.Thread(target=_upscale_worker, daemon=True, name="upscale-worker")
    thread.start()


def _after_download_upscale_target(episode_path):
    """(file_path, output_path) for one finished episode, or None.

    None means "don't upscale this": the mode is off, or the file isn't on
    disk (yet). Keeping that check here is what stops a job from ever being
    queued for a file that doesn't exist.
    """
    from pathlib import Path as _Path

    if get_setting("upscaling_mode", "disabled") != "after_download":
        return None
    ep = _Path(episode_path)
    try:
        if not ep.exists():
            return None
    except OSError:
        return None
    replace = get_setting("upscaling_replace_original", "1") == "1"
    out = str(ep) if replace else str(ep.with_name(ep.stem + " (upscale).mkv"))
    return str(ep), out


def _trigger_episode_after_download_upscale(episode_path, title, queue_item_id, upscale=False):
    """Add ONE finished episode to the download's upscale job.

    Called right after each episode is on disk (mirrors
    encoding_worker._trigger_after_download_encode). The first episode of a
    download creates the queue entry, every later one is appended to that same
    entry, so a season stays one job in the UI while upscaling can already
    start on episode 1 instead of waiting for the whole season.

    Only enqueues when the "upscale after download" mode is active and the
    caller requested upscaling for this download; silently no-ops otherwise.

    Used by: web/queue_worker.py (per episode).
    """
    try:
        if not upscale:
            return
        target = _after_download_upscale_target(episode_path)
        if target is None:
            return
        file_path, output_path = target
        _uid, _created = append_download_upscale_file(
            queue_item_id=queue_item_id,
            title=title,
            file_path=file_path,
            output_path=output_path,
        )
        if _uid is None:
            logger.info(f"[Upscale] Auftrag abgebrochen — Folge übersprungen: {file_path}")
            return
        logger.info(
            "[Upscale] %s: %s (Job #%s)",
            "Neuer Auftrag" if _created else "Folge angehängt",
            file_path, _uid,
        )
    except Exception as exc:
        logger.warning(f"[Upscale] Episoden-Trigger Fehler: {exc}")


def _trigger_batch_after_download_upscale(episode_paths, title, upscale=False):
    """Add several finished episodes as ONE upscale queue entry.

    Legacy entry point, kept for third-party modules: the download queue no
    longer waits for a whole season, it appends each episode through
    _trigger_episode_after_download_upscale() instead. Only enqueues when the
    "upscale after download" mode is active and the caller requested
    upscaling; silently no-ops otherwise, and files that aren't on disk are
    dropped rather than queued.
    """
    try:
        mode = get_setting("upscaling_mode", "disabled")
        if mode != "after_download":
            return
        if not upscale:
            return
        replace = get_setting("upscaling_replace_original", "1") == "1"
        from pathlib import Path as _Path
        valid = []
        for episode_path in episode_paths:
            ep = _Path(episode_path)
            if not ep.exists():
                continue
            out = str(ep) if replace else str(ep.with_name(ep.stem + " (upscale).mkv"))
            valid.append({"file_path": str(ep), "output_path": out})
        if not valid:
            return
        add_to_upscale_queue(
            title=title,
            file_path=valid[0]["file_path"],
            output_path=valid[0]["output_path"],
            source="download",
            files=valid if len(valid) > 1 else None,
        )
        logger.info(f"[Upscale] {len(valid)} Datei(en) als ein Eintrag in Queue: {title}")
    except Exception as exc:
        logger.warning(f"[Upscale] Batch-Trigger Fehler: {exc}")
