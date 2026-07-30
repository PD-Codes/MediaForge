"""Background worker that drains the H.264/H.265 encoding queue.

A single daemon thread claims one queued encoding job at a time from the DB
(``encoding_queue`` table) and re-encodes it via ffmpeg, using the same live
encoding_* settings download() would otherwise have applied inline (see
models/common/common.py's _get_ffmpeg_codec_opts()), then writes
progress/status back to the DB so the UI can poll it. Also exposes a helper
to enqueue newly-downloaded episodes for after-download encoding.

This module keeps its own progress dict (_encoding_progress), deliberately
separate from models/common/common.py's _ffmpeg_progress (which belongs to
the download queue's inline ffmpeg passes) — a running encode must never
show up in / interfere with the download queue modal. Mirrors how
upscale_worker.py / anime4k.py keep upscaling progress separate too.

Used by: web/app.py (starts the worker at startup) and web/queue_worker.py
(enqueues finished downloads for encoding when encoding_timing ==
"after_download").
"""

import os
import re
import subprocess
import threading
import time
import uuid

from ..config import MEDIAFORGE_TEMP_DIR
from ..logger import get_logger
from .media_publish import publish_output, sweep_stale_temp_files
from ..telemetry import client as telemetry_client
from ..telemetry import events as telemetry_events
from .db import (
    add_to_encoding_queue,
    claim_next_encoding_queued,
    is_encoding_cancelled,
    get_setting,
    reset_running_encoding_items,
    set_encoding_error,
    set_encoding_status,
    update_encoding_progress,
)
from .runtime_state import (
    _encoding_active_cancel_events,
    _encoding_cancel_lock,
)

logger = get_logger(__name__)


# Encoding worker state
_encoding_worker_started = False
# Guards both the one-time worker startup (_ensure_encoding_worker) and the
# claim call inside the loop below. claim_next_encoding_queued() is already
# atomic at the DB level (BEGIN IMMEDIATE), so this is a defensive second
# layer rather than the only thing preventing double-processing.
_encoding_lock = threading.Lock()

# Own progress dict — polled by routes/encoding.py's /api/encoding/queue/progress.
_encoding_progress_lock = threading.Lock()
_encoding_progress = {
    "active": False,
    "percent": 0.0,
    "time": "",
    "speed": "",
    "file": "",
}


def get_encoding_progress():
    """Return a snapshot of the current standalone encoding-queue progress."""
    with _encoding_progress_lock:
        return dict(_encoding_progress)


_RE_TIME     = re.compile(r"time=(\S+)")
_RE_SPEED    = re.compile(r"speed=\s*(\S+)")
_RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")


def _parse_time_str(s):
    """Parse an ffmpeg time string (HH:MM:SS.xx) to seconds."""
    try:
        h, m, sec = s.split(":")
        return float(h) * 3600 + float(m) * 60 + float(sec)
    except Exception:
        return 0.0


def _encode_one_file(input_path, output_path, label, cancel_event):
    """Run one ffmpeg encode pass, updating _encoding_progress as it goes.

    Reads the current encoding_* settings live via _get_ffmpeg_codec_opts()
    (same helper download() uses) so the queue always encodes with whatever
    mode/hw/preset/crf/audio is configured at the time the job actually runs,
    not at the time it was enqueued. Raises on failure or cancellation.
    """
    from ..models.common.common import _get_ffmpeg_codec_opts
    import ffmpeg as _ffmpeg

    vcodec, acodec, vopts, global_args = _get_ffmpeg_codec_opts()

    node = _ffmpeg.input(str(input_path)).output(
        str(output_path), vcodec=vcodec, acodec=acodec, **vopts
    )
    if global_args:
        node = node.global_args(*global_args)
    args = _ffmpeg.compile(node, overwrite_output=True)
    if "-stats_period" not in args:
        args.insert(-1, "-stats_period")
        args.insert(-1, "1")

    process = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=False
    )

    total_duration = 0.0
    buf = bytearray()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                process.kill()
                raise RuntimeError("Encoding cancelled")
            char = process.stderr.read(1)
            if not char:
                break
            if char in (b"\r", b"\n"):
                if buf:
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf.clear()
                    if total_duration == 0.0:
                        dm = _RE_DURATION.search(line)
                        if dm:
                            h, m, s, cs = dm.groups()
                            total_duration = float(h) * 3600 + float(m) * 60 + float(s) + float("0." + cs)
                    tm = _RE_TIME.search(line)
                    if tm:
                        sm = _RE_SPEED.search(line)
                        cur = _parse_time_str(tm.group(1))
                        pct = min(round(cur / total_duration * 100, 1), 99.9) if total_duration > 0 else 0.0
                        with _encoding_progress_lock:
                            _encoding_progress.update(
                                active=True, percent=pct, time=tm.group(1),
                                speed=sm.group(1) if sm else "", file=label,
                            )
            else:
                buf.extend(char)
    finally:
        process.wait()

    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Encoding cancelled")
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {process.returncode}")


def _report_transcode_started():
    """Submit the flag.transcoding stage-2 usage counter for one started job.

    A pure counter -- build_feature_flag_event() takes no metadata at all, the
    codec/preset context belongs to detail.transcoding below. Fires once per
    job that actually starts encoding, never per file. Wrapped in its own
    try/except so a telemetry bug can never affect the encoding worker.
    """
    try:
        telemetry_client.submit(telemetry_events.build_feature_flag_event("flag.transcoding"))
    except Exception:
        logger.debug("[Telemetry] failed to build/submit flag.transcoding event", exc_info=True)


def _report_transcode_failure(*, status, failed, total):
    """Submit a detail.transcoding telemetry event for a finished job that
    had at least one failed file (see registry.py's "detail.transcoding" --
    "Fehlermeldungen, wenn ein Transcoding-Vorgang fehlschlägt"). Deliberately
    NOT called on a clean success -- stage 3 is for feature errors/context,
    not a usage counter (that would be flag.transcoding, not wired here).

    Metadata is limited to the codec/hw settings and failure counts -- no
    title, no file path, per TELEMETRY_PLAN.md's "kein Titel/Inhalt" rule
    for stage 3. Wrapped in its own try/except so a telemetry bug can never
    affect the encoding worker itself (same defensive pattern as
    telemetry/hooks.py's _report_exception).
    """
    try:
        mode = get_setting("encoding_mode", "copy")
        hw = get_setting(f"encoding_hw_{mode}", "cpu") if mode in ("h264", "h265") else None
        preset = get_setting(f"encoding_preset_{mode}", "") if mode in ("h264", "h265") else None
        event = telemetry_events.build_feature_detail_event(
            "detail.transcoding", action="encode", status=status,
            metadata={"mode": mode, "hw": hw, "preset": preset,
                      "failed_files": failed, "total_files": total},
        )
        if event:
            telemetry_client.submit(event)
    except Exception:
        logger.debug("[Telemetry] failed to build/submit detail.transcoding event", exc_info=True)


def _encoding_worker():
    """Single global worker loop: claim one queued job, process it fully, repeat.

    Runs forever on its own daemon thread (started once via
    _ensure_encoding_worker). Any exception inside the loop is caught so the
    worker keeps running instead of dying; on error it tries to mark the
    current item "failed" and sleeps 5s before retrying the loop.
    """
    while True:
        item = None
        _final_status_set = False
        # Files already passed to the upscale queue. Bound out here because the
        # except handler reads it, and the item can die before the file list
        # even exists.
        _handed_paths = set()
        try:
            with _encoding_lock:
                item = claim_next_encoding_queued()

            if not item:
                time.sleep(4)
                continue

            cancel_ev = threading.Event()
            with _encoding_cancel_lock:
                _encoding_active_cancel_events[item["id"]] = cancel_ev

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

            # Telemetry: stage-2 usage counter -- one event per job that
            # actually starts encoding, not per file and not per loop turn.
            _report_transcode_started()

            with _encoding_progress_lock:
                _encoding_progress.update(active=True, percent=0.0, time="", speed="", file="")

            for _fi, _fentry in enumerate(_file_list):
                if is_encoding_cancelled(item["id"]):
                    break

                file_path   = _fentry["file_path"]
                output_path = _fentry.get("output_path") or file_path

                temp_output = str(MEDIAFORGE_TEMP_DIR / f"{_WPath(file_path).stem}_{uuid.uuid4().hex[:8]}_encode_tmp.mkv")
                actual_output = output_path

                update_encoding_progress(item["id"],
                    round(_fi / _total_files * 100, 1),
                    current_file_idx=_fi)

                _published = False
                try:
                    # The scratch dir lives on the OS temp volume, which a
                    # reboot or a tmp cleaner may have wiped since the last
                    # run. Inside the try on purpose: a permission problem
                    # here must fail this one file, not the whole queue item.
                    os.makedirs(MEDIAFORGE_TEMP_DIR, exist_ok=True)
                    _encode_one_file(
                        input_path=file_path,
                        output_path=temp_output,
                        label=item.get("title", ""),
                        cancel_event=cancel_ev,
                    )
                    if not is_encoding_cancelled(item["id"]):
                        # Never unlink the original first: publish_output()
                        # stages the result next to its destination and swaps
                        # it in atomically, so a failed copy cannot leave the
                        # user without a file. See web/media_publish.py.
                        publish_output(temp_output, actual_output)
                        _published = True
                        # Hand this file to the upscale queue now that the
                        # encode is done with it. With both steps set to
                        # "after download" the download worker deliberately
                        # does NOT queue the upscale itself -- otherwise both
                        # workers would run ffmpeg on the same path at the
                        # same time and the slower one would overwrite the
                        # other's result. Note actual_output, not file_path:
                        # with "replace original" off the encode wrote a new
                        # file and that is what should be upscaled.
                        if item.get("upscale_after"):
                            _handed_paths.add(file_path)
                            _handover_to_upscale(item, actual_output)
                except Exception as _fe:
                    _overall_failed += 1
                    logger.error(f"[Encoding] Fehler bei {file_path}: {_fe}")
                    if is_encoding_cancelled(item["id"]):
                        break
                    # A failed encode is no reason to silently drop the
                    # upscale the user also asked for -- upscale the original
                    # instead. Since publish_output() never removes the
                    # original before the new file is completely written, it
                    # is still there in every failure path; the existence
                    # check stays as a cheap guard against an outside change
                    # (user deleted it in the library meanwhile). Not done on
                    # cancel: there the user stopped the whole chain.
                    if item.get("upscale_after"):
                        _handed_paths.add(file_path)
                        if _WPath(file_path).exists():
                            _handover_to_upscale(item, file_path)
                        else:
                            logger.warning(
                                "[Encoding] %s ist nach dem Fehler nicht mehr da — "
                                "Upscaling entfällt", file_path,
                            )
                finally:
                    # Covers the cancel path too: a job stopped between the
                    # finished encode and the publish step used to leave a
                    # full-size scratch file behind forever.
                    if not _published:
                        try:
                            _WPath(temp_output).unlink(missing_ok=True)
                        except Exception:
                            pass

                # Overall progress after this file completes
                update_encoding_progress(item["id"],
                    round((_fi + 1) / _total_files * 100, 1),
                    current_file_idx=_fi + 1)

            with _encoding_progress_lock:
                _encoding_progress.update(active=False, percent=0.0, time="", speed="", file="")

            # Final status
            if not is_encoding_cancelled(item["id"]):
                if _overall_failed == 0:
                    set_encoding_status(item["id"], "completed")
                elif _overall_failed < _total_files:
                    set_encoding_status(item["id"], "completed")
                    set_encoding_error(item["id"], f"{_overall_failed}/{_total_files} Datei(en) fehlgeschlagen")
                    _report_transcode_failure(status="partial_failure",
                                               failed=_overall_failed, total=_total_files)
                else:
                    set_encoding_status(item["id"], "failed")
                    set_encoding_error(item["id"], f"Alle {_total_files} Datei(en) fehlgeschlagen")
                    _report_transcode_failure(status="failed",
                                               failed=_overall_failed, total=_total_files)
                _final_status_set = True
            else:
                set_encoding_status(item["id"], "cancelled")
                _final_status_set = True

            with _encoding_cancel_lock:
                _encoding_active_cancel_events.pop(item["id"], None)

        except Exception as e:
            logger.error(f"[Encoding] Worker-Fehler: {e}", exc_info=True)
            with _encoding_progress_lock:
                _encoding_progress.update(active=False, percent=0.0, time="", speed="", file="")
            # The item died outside the per-file loop, so nothing handed its
            # files to the upscale queue -- and queue_worker.py no longer
            # queues them as a fallback. Do it here instead of losing them.
            if item is not None and not is_encoding_cancelled(item["id"]):
                _handover_remaining(item, _handed_paths)
            if item is not None and not _final_status_set:
                try:
                    if not is_encoding_cancelled(item["id"]):
                        set_encoding_status(item["id"], "failed")
                        set_encoding_error(item["id"], f"Encoding worker error: {str(e)}")
                except Exception as db_err:
                    logger.error(f"[Encoding] Failed to set status to failed for item {item['id']}: {db_err}", exc_info=True)
            time.sleep(5)


def _ensure_encoding_worker():
    """Start the encoding worker thread once (idempotent).

    Used by: web/app.py (called during app startup).
    """
    global _encoding_worker_started
    with _encoding_lock:
        if _encoding_worker_started:
            return
        _encoding_worker_started = True
    reset_running_encoding_items()
    sweep_stale_temp_files("_encode_tmp.mkv")
    thread = threading.Thread(target=_encoding_worker, daemon=True, name="encoding-worker")
    thread.start()


def _handover_remaining(item, done_paths):
    """Hand every not-yet-processed file of a dying item to the upscale queue.

    Reached when the item fails outside the per-file loop (a DB error, a bad
    files column). Without this the upscale would be lost silently: since the
    chain was introduced, queue_worker.py no longer queues it as a fallback.
    """
    import json as _wjson
    from pathlib import Path as _WPath

    if not item.get("upscale_after"):
        return
    try:
        raw = item.get("files")
        entries = _wjson.loads(raw) if raw else None
        if not entries:
            entries = [{"file_path": item.get("file_path")}]
        for entry in entries:
            path = (entry or {}).get("file_path")
            if not path or path in done_paths:
                continue
            if _WPath(path).exists():
                _handover_to_upscale(item, path)
    except Exception as exc:
        logger.warning(f"[Encoding] Upscale-Nachreichung fehlgeschlagen: {exc}")


def _handover_to_upscale(item, encoded_path):
    """Queue one finished encode for upscaling.

    Only reached when the download that produced this job asked for upscaling
    AND upscaling runs after the download -- see queue_worker.py, which skips
    its own upscale trigger in exactly that case so the two never overlap.
    Failing here must not fail the encode: the file is already in place, a
    missing upscale is a degradation, not a loss.
    """
    try:
        from .upscale_worker import _trigger_episode_after_download_upscale
        _trigger_episode_after_download_upscale(
            str(encoded_path),
            item.get("title", ""),
            item.get("queue_item_id"),
            upscale=True,
        )
    except Exception as exc:
        logger.warning(f"[Encoding] Upscale-Übergabe fehlgeschlagen: {exc}")


def _trigger_after_download_encode(episode_paths, title, queue_item_id=None, upscale_after=False):
    """Add one or more just-downloaded episodes as ONE encoding queue entry.

    Called PER EPISODE, right after that single episode finishes downloading
    — not once at the end of the whole download-queue item — so encoding of
    episode 1 starts immediately while episode 2 is still downloading. If an
    encode is already running (the worker only processes one item at a
    time), this episode's entry simply waits its turn in the queue instead
    of blocking the download.

    Only enqueues when the "encode after download" timing is active AND the
    configured encoding_mode is an actual transcode (h264/h265) — "copy" and
    "expert" modes stay applied inline during download as before (copy is
    already cheap; expert is left inline since it may just be a remux),
    silently no-ops otherwise.

    episode_paths is usually a single-element list (one call per episode),
    but still accepts multiple paths so a caller can batch if it ever needs
    to.

    Used by: web/queue_worker.py (called right after each episode's download
    completes).
    """
    try:
        timing = get_setting("encoding_timing", "during_download")
        mode = get_setting("encoding_mode", "copy")
        if timing != "after_download":
            return False
        if mode not in ("h264", "h265"):
            return False
        replace = get_setting("encoding_replace_original", "1") == "1"
        from pathlib import Path as _Path
        valid = []
        for episode_path in episode_paths:
            ep = _Path(episode_path)
            if not ep.exists():
                continue
            out = str(ep) if replace else str(ep.with_name(ep.stem + f" ({mode.upper()}).mkv"))
            valid.append({"file_path": str(ep), "output_path": out})
        if not valid:
            return False
        add_to_encoding_queue(
            title=title,
            file_path=valid[0]["file_path"],
            output_path=valid[0]["output_path"],
            source="download",
            files=valid if len(valid) > 1 else None,
            queue_item_id=queue_item_id,
            upscale_after=upscale_after,
        )
        logger.info(f"[Encoding] {len(valid)} Datei(en) als ein Eintrag in Queue: {title}"
                    + (" (Upscaling folgt)" if upscale_after else ""))
        return True
    except Exception as exc:
        logger.warning(f"[Encoding] Batch-Trigger Fehler: {exc}")
        return False
