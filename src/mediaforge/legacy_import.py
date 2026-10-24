"""Import data from a previous "AniWorld Downloader" installation.

MediaForge was formerly called "AniWorld Downloader" and stored everything in
``~/.aniworld``. New installs use ``~/.mediaforge``. To make sure nobody loses
their downloads history, settings, users, watchlist or browser profile on the
rename, this module detects an old install and copies its data over — once, and
non-destructively (the old ``~/.aniworld`` directory is never modified).

The heavy lifting runs *before* the web app initialises its database (see
``entry.py``), so the app simply boots up with all the old data already in
place. A JSON marker records what happened for display in the WebUI.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .config import MEDIAFORGE_CONFIG_DIR
from .logger import get_logger

logger = get_logger(__name__)

LEGACY_DIR = Path.home() / ".aniworld"
NEW_DIR = MEDIAFORGE_CONFIG_DIR

# Old file name -> new file name. Everything else keeps its name.
_RENAME = {
    "aniworld.db": "mediaforge.db",
    # SQLite's write-ahead log belongs to the database file BY NAME. Copied
    # under its old name, every transaction not yet checkpointed when the old
    # app was stopped was silently lost.
    "aniworld.db-wal": "mediaforge.db-wal",
}
# Never copy these (runtime lock / pid files, byte-code caches).
_SKIP_NAMES = {"aniworld.pid", "mediaforge.pid", "__pycache__",
               # The WAL index is rebuilt by SQLite from the WAL itself; a
               # copied one would only describe the old file.
               "aniworld.db-shm"}
_SKIP_SUFFIXES = {".pid", ".lock", ".pyc"}

_MARKER = NEW_DIR / ".legacy_imported.json"
_NEW_DB = NEW_DIR / "mediaforge.db"
_LEGACY_DB = LEGACY_DIR / "aniworld.db"


def _target_name(name: str) -> str:
    """Map a legacy file/directory name to its new-install name (see _RENAME)."""
    return _RENAME.get(name, name)


def _should_skip(name: str) -> bool:
    """Return True for entries that must never be copied (locks, caches, pid files)."""
    if name in _SKIP_NAMES:
        return True
    return any(name.endswith(sfx) for sfx in _SKIP_SUFFIXES)


def detect_legacy() -> dict:
    """Return the current legacy-import status without changing anything.

    Used by: the settings route (``web/routes/settings.py``) to show the
    "import legacy data" option in the WebUI.
    """
    marker = None
    if _MARKER.exists():
        try:
            marker = json.loads(_MARKER.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            marker = None
    return {
        "legacy_dir": str(LEGACY_DIR),
        "legacy_exists": LEGACY_DIR.is_dir(),
        "legacy_has_db": _LEGACY_DB.is_file(),
        "new_has_db": _NEW_DB.is_file(),
        "already_imported": marker is not None,
        "marker": marker,
    }


def _copy_entry(src: Path, dst: Path, overwrite: bool) -> bool:
    """Copy a file or directory tree. Returns True if anything was copied."""
    if dst.exists() and not overwrite:
        return False
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return True


def run_import(overwrite: bool = False) -> dict:
    """Copy data from the legacy install into the new config dir.

    Non-destructive: the legacy directory is only read. Existing files in the
    new directory are kept unless ``overwrite`` is True. Returns a summary dict.
    Used by: :func:`import_legacy_if_needed` (automatic, first run) and the
    settings route (``web/routes/settings.py``, manual re-import button).
    """
    result = {"copied": [], "skipped": [], "source": str(LEGACY_DIR)}
    if not LEGACY_DIR.is_dir():
        result["error"] = "no_legacy_dir"
        return result

    NEW_DIR.mkdir(parents=True, exist_ok=True)
    for entry in sorted(LEGACY_DIR.iterdir()):
        if _should_skip(entry.name):
            continue
        target = NEW_DIR / _target_name(entry.name)
        try:
            if _copy_entry(entry, target, overwrite):
                result["copied"].append(entry.name)
            else:
                result["skipped"].append(entry.name)
        except OSError as err:
            logger.warning("Legacy import: could not copy %s: %s", entry.name, err)
            result.setdefault("errors", []).append(entry.name)

    marker = {
        "source": str(LEGACY_DIR),
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "copied": result["copied"],
    }
    try:
        _MARKER.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    except OSError:
        pass
    result["marker"] = marker
    return result


def import_legacy_if_needed() -> dict | None:
    """Auto-import on first run so existing users lose nothing.

    Runs only when the new install has no database yet and a legacy install
    with a database exists. Safe to call on every startup — it becomes a no-op
    once the new database is present.
    Used by: :func:`mediaforge.entry.mediaforge`, once at startup before the
    WebUI (and its database) initializes.
    """
    if _NEW_DB.is_file():
        return None
    if not _LEGACY_DB.is_file():
        return None
    logger.info("Detected previous AniWorld installation at %s — importing data...", LEGACY_DIR)
    summary = run_import(overwrite=False)
    logger.info(
        "Legacy import done: %d item(s) copied from %s",
        len(summary.get("copied", [])), LEGACY_DIR,
    )
    return summary
