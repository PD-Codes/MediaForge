"""Background worker that drains the Anime4K upscale queue.

A single daemon thread claims one queued upscale job at a time from the DB
(``upscale_queue`` table), runs it through ``mediaforge.anime4k``, and writes
progress/status back to the DB so the UI can poll it. Also exposes a helper
to enqueue newly-downloaded episodes for upscaling.

Used by: web/app.py (starts the worker at startup) and web/queue_worker.py
(enqueues episodes after a download finishes).

# TODO(telemetry): wire up flag.upscale / detail.upscale (preset used,
# success/failure) at the point a queued job finishes below -- see
# telemetry/registry.py. Registry-only for now.
"""

import threading
import time

from ..logger import get_logger
from .db import (
    add_to_upscale_queue,
    claim_next_upscale_queued,
    is_upscale_cancelled,
    get_setting,
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


def _upscale_worker():
    """Single global worker loop: claim one queued job, process it fully, repeat.

    Runs forever on its own daemon thread (started once via
    _ensure_upscale_worker). Any exception inside the loop is caught so the
    worker keeps running instead of dying; on error it tries to mark the
    current item "failed" and sleeps 5s before retrying the loop.
    """
    while True:
        try:
            item = None
            _final_status_set = False
            with _upscale_lock:
                item = claim_next_upscale_queued()

            if not item:
                time.sleep(4)
                continue

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

            # Build file list: multi-file entries store JSON in .files column
            _raw_files = item.get("files")
            if _raw_files:
                try:
                    _file_list = _wjson.loads(_raw_files)
                except Exception:
                    _file_list = [{"file_path": item["file_path"],
                                   "output_path": item.get("output_path") or item["file_path"]}]
            else:
                _file_list = [{"file_path": item["file_path"],
                               "output_path": item.get("output_path") or item["file_path"]}]

            _total_files = max(len(_file_list), 1)
            _overall_failed = 0

            for _fi, _fentry in enumerate(_file_list):
                if is_upscale_cancelled(item["id"]):
                    break

                file_path   = _fentry["file_path"]
                output_path = _fentry.get("output_path") or file_path

                _replace_original = (file_path == output_path)
                if _replace_original:
                    actual_output = str(_WPath(file_path).with_suffix(".upscale_tmp.mkv"))
                else:
                    actual_output = output_path

                # Track current file index (poller reads this)
                item["_runtime_file_idx"] = _fi
                update_upscale_progress(item["id"],
                    round(_fi / _total_files * 100, 1),
                    current_file_idx=_fi)

                try:
                    upscale_file(
                        input_path=file_path,
                        output_path=actual_output,
                        settings=settings,
                        cancel_event=cancel_ev,
                        label=item.get("title", ""),
                    )
                    if not is_upscale_cancelled(item["id"]):
                        if _replace_original:
                            _WPath(file_path).unlink(missing_ok=True)
                            _WPath(actual_output).rename(file_path)
                except Exception as _fe:
                    _overall_failed += 1
                    logger.error(f"[Upscale] Fehler bei {file_path}: {_fe}")
                    try:
                        _WPath(actual_output).unlink(missing_ok=True)
                    except Exception:
                        pass
                    # Continue with next file unless cancelled
                    if is_upscale_cancelled(item["id"]):
                        break

            # Final status
            if not is_upscale_cancelled(item["id"]):
                update_upscale_progress(item["id"], 100.0, current_file_idx=_total_files)
                if _overall_failed == 0:
                    set_upscale_status(item["id"], "completed")
                elif _overall_failed < _total_files:
                    set_upscale_status(item["id"], "completed")
                    set_upscale_error(item["id"], f"{_overall_failed}/{_total_files} Datei(en) fehlgeschlagen")
                else:
                    set_upscale_status(item["id"], "failed")
                    set_upscale_error(item["id"], f"Alle {_total_files} Datei(en) fehlgeschlagen")
                _final_status_set = True

            _poll_stop.set()
            _pt.join(timeout=3)

            if is_upscale_cancelled(item["id"]):
                set_upscale_status(item["id"], "cancelled")
                _final_status_set = True

            with _upscale_cancel_lock:
                _upscale_active_cancel_events.pop(item["id"], None)

        except Exception as e:
            logger.error(f"[Upscale] Worker-Fehler: {e}", exc_info=True)
            if item is not None and not _final_status_set:
                try:
                    if not is_upscale_cancelled(item["id"]):
                        set_upscale_status(item["id"], "failed")
                        set_upscale_error(item["id"], f"Upscale worker error: {str(e)}")
                except Exception as db_err:
                    logger.error(f"[Upscale] Failed to set status to failed for item {item['id']}: {db_err}", exc_info=True)
            time.sleep(5)


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
    thread = threading.Thread(target=_upscale_worker, daemon=True, name="upscale-worker")
    thread.start()


def _trigger_batch_after_download_upscale(episode_paths, title, upscale=False):
    """Add ALL downloaded episodes as ONE upscale queue entry.

    Only enqueues when the "upscale after download" mode is active and the
    caller requested upscaling for this download; silently no-ops otherwise.

    Used by: web/queue_worker.py (called after a download batch completes).
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
