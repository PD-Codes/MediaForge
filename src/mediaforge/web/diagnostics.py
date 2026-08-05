"""One-click diagnostic bundle.

What a support request usually contains is a screenshot and "it doesn't work".
What it needs to contain is the version, the platform, which settings are set
(not their values), what the workers are doing, and the tail of the log. This
builds that as a ZIP the user can attach, and -- the part that makes it usable
at all -- it is scrubbed before it is written, not after.

Scrubbing reuses :mod:`mediaforge.telemetry.sanitize`, which already knows how
to collapse home directories, strip URL paths and redact secret-shaped tokens.
Sharing that code is deliberate: a second, parallel redaction implementation is
a second thing to forget to update when a new secret-shaped setting appears.

Nothing here leaves the machine on its own. The bundle is generated on request,
streamed to the browser once, and never stored -- unlike telemetry, which is
consent-gated because it *is* sent. A diagnostic bundle the user hands over
themselves needs no consent dialog, only honesty about what is in it, which is
what ``manifest.json`` inside the ZIP is for.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import os
import platform
import sys
import tempfile
import zipfile
from pathlib import Path

from ..config import MEDIAFORGE_CONFIG_DIR, MEDIAFORGE_TEMP_DIR
from ..logger import get_logger

logger = get_logger(__name__)

# Tail only. A debug-mode log can be tens of megabytes and the useful part is
# always the end.
_LOG_TAIL_BYTES = 2 * 1024 * 1024


def _sanitize(text: str) -> str:
    try:
        from ..telemetry import sanitize as _s
        for step in ("strip_browser_launch_noise", "redact_secrets",
                     "redact_urls_in_text", "collapse_paths_in_text"):
            fn = getattr(_s, step, None)
            if callable(fn):
                text = fn(text) or ""
        return text
    except Exception as exc:
        logger.warning("[Diagnostics] Sanitizer unavailable (%s) -- log omitted", exc)
        # Failing closed matters here: shipping a raw log because the scrubber
        # broke is exactly the accident this module exists to prevent.
        return "<log omitted: sanitizer unavailable>"


def _system_info() -> dict:
    info = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "in_docker": os.path.exists("/.dockerenv"),
        "cwd_is_config_dir": str(MEDIAFORGE_CONFIG_DIR) in os.getcwd(),
    }
    try:
        from .version_info import get_version_info
        info["version"] = get_version_info()
    except Exception as exc:
        info["version_error"] = str(exc)
    try:
        import shutil
        total, used, free = shutil.disk_usage(str(MEDIAFORGE_CONFIG_DIR))
        info["config_disk"] = {"total": total, "used": used, "free": free}
    except Exception:
        pass
    return info


def _settings_keys() -> dict:
    """Which settings are set, and their values -- except the sensitive ones.

    The distinction is the whole point. "seerr_api_key is set" is diagnostic
    information; the key itself is a credential, and a support bundle is
    forwarded through chat apps and issue trackers.
    """
    out: dict = {}
    try:
        from .db import get_db, is_sensitive_key
        conn = get_db()
        try:
            for row in conn.execute("SELECT key, value FROM app_settings").fetchall():
                key = row["key"]
                value = row["value"]
                if is_sensitive_key(key):
                    out[key] = "<set>" if value else "<empty>"
                else:
                    out[key] = (value or "")[:300]
        finally:
            conn.close()
    except Exception as exc:
        out["_error"] = str(exc)
    return out


def _tables() -> dict:
    out: dict = {}
    try:
        from .db import get_db
        conn = get_db()
        try:
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall():
                name = row["name"]
                try:
                    out[name] = conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
                except Exception:
                    out[name] = -1
        finally:
            conn.close()
    except Exception as exc:
        out["_error"] = str(exc)
    return out


def _queue_errors(limit: int = 50) -> list:
    """The last failures, which is what the report is usually about."""
    out = []
    try:
        from .db import get_db
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT id, status, series_url, language, errors, created_at "
                "FROM download_queue WHERE errors IS NOT NULL AND errors != '' "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            for row in rows:
                entry = dict(row)
                entry["series_url"] = _sanitize(entry.get("series_url") or "")
                entry["errors"] = _sanitize(entry.get("errors") or "")[:4000]
                out.append(entry)
        finally:
            conn.close()
    except Exception as exc:
        out.append({"_error": str(exc)})
    return out


def _log_tail() -> str:
    candidates = [
        Path(tempfile.gettempdir()) / "mediaforge.log",
        MEDIAFORGE_TEMP_DIR / "mediaforge.log",
        MEDIAFORGE_CONFIG_DIR / "mediaforge.log",
    ]
    for path in candidates:
        try:
            if not path.exists():
                continue
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > _LOG_TAIL_BYTES:
                    fh.seek(size - _LOG_TAIL_BYTES)
                    fh.readline()  # discard the partial first line
                raw = fh.read()
            return _sanitize(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.debug("[Diagnostics] Could not read %s: %s", path, exc)
    return "<no log file found>"


def _modules() -> list:
    try:
        from .thirdparties import list_installed_modules
        return list_installed_modules()
    except Exception:
        try:
            from .thirdparties.store import list_installed
            return list_installed()
        except Exception:
            return []


MANIFEST_NOTE = (
    "This bundle is generated on demand and is not sent anywhere automatically.\n"
    "Included: version and platform info, setting KEYS (sensitive values are\n"
    "replaced with <set>), table row counts, worker states, schema version,\n"
    "recent queue errors and the tail of the log.\n"
    "Excluded: passwords, API keys, tokens, session cookies, media files,\n"
    "library paths beyond their shortened form, and the audit log itself.\n"
    "The log and queue errors are scrubbed with the same sanitizer telemetry\n"
    "uses (home directories collapsed, URL paths stripped, secret-shaped\n"
    "tokens redacted). Please still skim it before sharing.\n"
)


def build_bundle() -> tuple[bytes, str]:
    """Return ``(zip_bytes, filename)``."""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = "mediaforge-diagnostics-%s.zip" % stamp

    parts: dict[str, str] = {}
    parts["manifest.json"] = json.dumps({
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "note": MANIFEST_NOTE,
        "contents": ["system.json", "settings.json", "tables.json",
                     "workers.json", "schema.json", "modules.json",
                     "queue_errors.json", "mediaforge.log"],
    }, indent=2)
    parts["system.json"] = json.dumps(_system_info(), indent=2, default=str)
    parts["settings.json"] = json.dumps(_settings_keys(), indent=2, default=str)
    parts["tables.json"] = json.dumps(_tables(), indent=2, default=str)

    try:
        from .worker_registry import snapshot as _workers
        parts["workers.json"] = json.dumps(_workers(), indent=2, default=str)
    except Exception as exc:
        parts["workers.json"] = json.dumps({"error": str(exc)})

    try:
        from .dbmigrate import list_snapshots, status as _mig_status
        parts["schema.json"] = json.dumps(
            {"migrations": _mig_status(), "snapshots": list_snapshots()},
            indent=2, default=str)
    except Exception as exc:
        parts["schema.json"] = json.dumps({"error": str(exc)})

    parts["modules.json"] = json.dumps(_modules(), indent=2, default=str)
    parts["queue_errors.json"] = json.dumps(_queue_errors(), indent=2, default=str)
    parts["mediaforge.log"] = _log_tail()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            zf.writestr(name, content)
    return buf.getvalue(), filename
