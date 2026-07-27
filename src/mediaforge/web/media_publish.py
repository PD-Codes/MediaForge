"""Crash-safe publishing of a freshly produced media file.

Both the encoding and the upscale worker write their ffmpeg output to a
scratch file in MEDIAFORGE_TEMP_DIR and then have to put it in its final
place -- often ON TOP of the source file, when "replace original" is on.

Doing that as ``original.unlink()`` followed by ``shutil.move(temp, final)``
destroys data: the scratch dir usually sits on the OS temp volume, so the
move is a copy+delete, and a copy can fail half way through (target volume
full, read-only mount, network share timeout, ACL). At that point the
original is already gone and the scratch file is discarded by the error
handler -- the media file is lost with nothing but a "1/1 file(s) failed"
line to show for it.

publish_output() removes that window: the payload is first staged NEXT TO
its final destination (same volume, so the expensive copy happens while the
original is still fully intact) and only then swapped in with os.replace(),
which is atomic on both POSIX and Windows. Any failure leaves the original
exactly as it was.

Used by: web/encoding_worker.py, web/upscale_worker.py.
"""

import os
import shutil
from pathlib import Path

from ..config import MEDIAFORGE_TEMP_DIR
from ..logger import get_logger

logger = get_logger(__name__)

# Suffix of the staging file that lives next to the final destination for the
# duration of the copy. Deliberately not ".part"/".tmp": those are common
# enough that a media scanner or another tool might pick them up.
_STAGING_SUFFIX = ".mfnew"


def publish_output(temp_output, final_path):
    """Move *temp_output* to *final_path*, overwriting it atomically.

    *final_path* may be the source file itself ("replace original"); it does
    NOT have to be removed by the caller first -- os.replace() overwrites it
    in one step. Raises on failure, having removed the staging file; the
    file at *final_path* is untouched in that case.
    """
    final = Path(final_path)
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = final.with_name(final.name + _STAGING_SUFFIX)

    # A staging file can survive a hard kill mid-copy. It belongs to a run
    # that is long gone, so it is safe -- and necessary -- to drop it.
    try:
        staging.unlink(missing_ok=True)
    except OSError as exc:
        raise OSError(f"Staging-Datei {staging} nicht überschreibbar: {exc}") from exc

    try:
        # The cross-volume copy happens here, while the original is intact.
        shutil.move(str(temp_output), str(staging))
        # Same directory, therefore a true rename: atomic, and it replaces
        # the destination in one operation.
        os.replace(str(staging), str(final))
    except Exception:
        try:
            staging.unlink(missing_ok=True)
        except Exception:
            logger.warning("[Publish] Staging-Datei %s konnte nicht entfernt werden", staging)
        raise


def sweep_stale_temp_files(*suffixes):
    """Delete leftover scratch files matching ``*<suffix>`` in the temp dir.

    Called once per worker start. A scratch file only outlives its run if the
    process was killed between the ffmpeg pass and the publish step; there is
    no live job that could still be holding one, so removing them at startup
    is safe and keeps /tmp from filling up with full-size video files.
    """
    try:
        base = Path(MEDIAFORGE_TEMP_DIR)
        if not base.is_dir():
            return
        for suffix in suffixes:
            for leftover in base.glob(f"*{suffix}"):
                try:
                    leftover.unlink()
                    logger.info("[Publish] Verwaiste Temp-Datei entfernt: %s", leftover)
                except Exception as exc:
                    logger.debug("[Publish] %s nicht entfernbar: %s", leftover, exc)
    except Exception as exc:
        logger.debug("[Publish] Temp-Aufräumen fehlgeschlagen: %s", exc)
