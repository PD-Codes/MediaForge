"""Portable settings profiles — the small sibling of Backup.

[Backup](web/backup.py) exports *everything*, encrypted, as a restore point for
one installation. That is the wrong tool for the two cases this module covers:

* **Support.** "Send me your settings so I can reproduce this" currently means
  either a full encrypted backup containing the person's users and history, or
  a dozen screenshots. Neither is reasonable.
* **Sharing a setup.** Naming template, provider order, quality defaults and
  path layout are the interesting part of somebody's configuration, and they
  are the same on every machine.

So a profile is deliberately **small, plain JSON, and free of secrets**: no
users, no history, no queues, no API keys. It is meant to be pasted into an
issue, and anything that could not survive that is excluded rather than
encrypted.

Keys are chosen by an explicit allowlist. A denylist would mean every new
sensitive setting is exposed until somebody remembers to add it — the failure
mode that turns "share your settings" into an incident.
"""

from __future__ import annotations

import datetime as _dt
import json

from ..logger import get_logger

logger = get_logger(__name__)

FORMAT_VERSION = 1

# Prefixes and exact keys that make up a profile. Grouped by what they are, so
# adding one lands next to its relatives instead of at the end of a flat list.
_ALLOWED_PREFIXES: tuple[str, ...] = (
    "naming_",           # naming template and its switches
    "download_",         # window, retries, parallelism, subtitle behaviour
    "encoding_",         # codec, preset, hardware acceleration
    "upscale_",          # Anime4K shader selection and scale
    "library_",          # scan interval, nfo writing, language folders
    "source_",           # source order and per-source enable flags
    "sync_",             # Auto-Sync cadence and retry behaviour
    "home_",             # start page layout (the kids PIN is excluded below)
    "calendar_",
    "browse_",
    "player_",
    "reading_",
    "dupe_",
    "subtitle_",
)

_ALLOWED_KEYS: frozenset = frozenset({
    "default_language", "preferred_language", "language_separation",
    "movie_subfolder", "quality", "provider_order", "theme_pack",
    "theme_mode", "accent", "default_ui_language", "date_format",
    "history_retention_days", "audit_retention_days",
    "aniworld_absolute_episodes", "dns_mode", "dns_server",
})

# Explicitly excluded even though a prefix above would match. Every entry here
# is a secret, a machine-specific path, or something that identifies the
# installation rather than describing its configuration.
_DENY_KEYS: frozenset = frozenset({
    "home_kids_pin",
    "download_path",
    "source_api_key",
})


def _is_exportable(key: str) -> bool:
    if key in _DENY_KEYS:
        return False
    # Belt and braces: even inside an allowed prefix, anything the database
    # considers sensitive is refused. That check is the one that stays correct
    # when a module registers a new secret at runtime
    # (db.register_sensitive_keys), which no static list here can anticipate.
    try:
        from .db import is_sensitive_key
        if is_sensitive_key(key):
            return False
    except Exception:
        return False
    if key in _ALLOWED_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


def export_profile(name: str = "") -> dict:
    """Build a shareable profile document."""
    from .db import get_db

    settings: dict = {}
    conn = get_db()
    try:
        for row in conn.execute("SELECT key, value FROM app_settings").fetchall():
            key = row["key"]
            if _is_exportable(key):
                settings[key] = row["value"]
    finally:
        conn.close()

    version = ""
    try:
        from .version_info import get_version_info
        version = str(get_version_info().get("version") or "")
    except Exception:
        pass

    return {
        "format": "mediaforge-settings-profile",
        "format_version": FORMAT_VERSION,
        "name": (name or "profile")[:80],
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "app_version": version,
        "settings": settings,
    }


def preview_profile(document) -> dict:
    """Validate an uploaded profile and report what it would change.

    Nothing is written. The point is that "import settings" from a stranger's
    file is a decision, and a decision needs the diff in front of it.
    """
    from .db import get_setting

    if isinstance(document, str):
        try:
            document = json.loads(document)
        except Exception:
            return {"ok": False, "error": "invalid_json"}
    if not isinstance(document, dict):
        return {"ok": False, "error": "invalid_document"}
    if document.get("format") != "mediaforge-settings-profile":
        return {"ok": False, "error": "not_a_profile"}
    if int(document.get("format_version") or 0) > FORMAT_VERSION:
        return {"ok": False, "error": "newer_format"}

    incoming = document.get("settings")
    if not isinstance(incoming, dict):
        return {"ok": False, "error": "invalid_settings"}

    changes, unchanged, refused = [], 0, []
    for key, value in incoming.items():
        if not _is_exportable(key):
            # A profile that names a key outside the allowlist was either
            # hand-edited or built by a newer version. Either way it is not
            # applied, and saying so is better than silently dropping it.
            refused.append(key)
            continue
        current = get_setting(key, None)
        if current == value:
            unchanged += 1
        else:
            changes.append({"key": key, "from": current, "to": value})

    changes.sort(key=lambda c: c["key"])
    return {
        "ok": True,
        "name": document.get("name", ""),
        "app_version": document.get("app_version", ""),
        "created_at": document.get("created_at", ""),
        "changes": changes,
        "unchanged": unchanged,
        "refused": sorted(refused),
    }


def import_profile(document, keys=None) -> dict:
    """Apply a profile. ``keys`` limits it to a subset of the previewed changes."""
    from .db import set_setting

    preview = preview_profile(document)
    if not preview.get("ok"):
        return preview

    wanted = set(keys) if keys else None
    applied = []
    for change in preview["changes"]:
        if wanted is not None and change["key"] not in wanted:
            continue
        try:
            set_setting(change["key"], change["to"])
            applied.append(change["key"])
        except Exception as exc:
            logger.warning("[Profile] Could not apply %s: %s", change["key"], exc)

    if applied:
        try:
            from . import audit as _audit
            _audit.audit("settings", "profile_imported",
                         target=preview.get("name", ""),
                         detail={"keys": applied}, severity="notice")
        except Exception:
            pass

    return {"ok": True, "applied": applied, "count": len(applied),
            "refused": preview["refused"]}
