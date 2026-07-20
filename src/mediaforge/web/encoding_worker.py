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

import re
import subprocess
import threading
import time

from ..logger import get_logger
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

            with _encoding_progress_lock:
                _encoding_progress.update(active=True, percent=0.0, time="", speed="", file="")

            for _fi, _fentry in enumerate(_file_list):
                if is_encoding_cancelled(item["id"]):
                    break

                file_path   = _fentry["file_path"]
                output_path = _fentry.get("output_path") or file_path

                _replace_original = (file_path == output_path)
                if _replace_original:
                    actual_output = str(_WPath(file_path).with_suffix(".encode_tmp.mkv"))
                else:
                    actual_output = output_path

                update_encoding_progress(item["id"],
                    round(_fi / _total_files * 100, 1),
                    current_file_idx=_fi)

                try:
                    _encode_one_file(
                        input_path=file_path,
                        output_path=actual_output,
                        label=item.get("title", ""),
                        cancel_event=cancel_ev,
                    )
                    if not is_encoding_cancelled(item["id"]):
                        if _replace_original:
                            _WPath(file_path).unlink(missing_ok=True)
                            _WPath(actual_output).rename(file_path)
                except Exception as _fe:
                    _overall_failed += 1
                    logger.error(f"[Encoding] Fehler bei {file_path}: {_fe}")
                    try:
                        _WPath(actual_output).unlink(missing_ok=True)
                    except Exception:
                        pass
                    if is_encoding_cancelled(item["id"]):
                        break

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
    thread = threading.Thread(target=_encoding_worker, daemon=True, name="encoding-worker")
    thread.start()


def _trigger_after_download_encode(episode_paths, title):
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
            return
        if mode not in ("h264", "h265"):
            return
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
            return
        add_to_encoding_queue(
            title=title,
            file_path=valid[0]["file_path"],
            output_path=valid[0]["output_path"],
            source="download",
            files=valid if len(valid) > 1 else None,
        )
        logger.info(f"[Encoding] {len(valid)} Datei(en) als ein Eintrag in Queue: {title}")
    except Exception as exc:
        logger.warning(f"[Encoding] Batch-Trigger Fehler: {exc}")
