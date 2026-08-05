"""The app_settings key-value store, incl. encrypted values and change listeners.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import json
import os
from werkzeug.security import generate_password_hash
from ...logger import get_logger

from ._core import SENSITIVE_KEYS, _ENC_PREFIX, _decrypt_value, _encrypt_existing_plaintext, _encrypt_value, get_db, is_sensitive_key
from .ui_prefs import _CREATE_USER_UI_PREFS_TABLE

logger = get_logger(__name__)


def init_media_ignored_db():
    """Table that stores missing media slots the user chose to ignore.

    A row is (folder, slot): `folder` is the lower-cased series folder name
    (matching the merge key used by the Media statistics), `slot` is either a
    specific missing slot like "S1E3" / a whole missing season like "S2", or
    the sentinel "__all__" meaning the entire series is ignored. Ignored slots
    are subtracted from a series' missing list when computing statistics, so a
    series whose remaining gaps are all ignored counts as complete."""
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS media_ignored (
                folder     TEXT NOT NULL,
                slot       TEXT NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (folder, slot)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def add_media_ignores(folder: str, slots, title: str = "") -> None:
    """Mark one or more missing slots as ignored for a series folder.

    `slots` may be a single slot string or a list. Use the sentinel "__all__"
    to ignore the whole series."""
    import time as _time
    if not folder:
        return
    folder = folder.lower()
    if isinstance(slots, str):
        slots = [slots]
    conn = get_db()
    try:
        now = _time.time()
        for slot in slots:
            if not slot:
                continue
            conn.execute(
                """INSERT INTO media_ignored (folder, slot, title, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(folder, slot) DO UPDATE SET title = excluded.title""",
                (folder, str(slot), title or "", now),
            )
        conn.commit()
    finally:
        conn.close()


def remove_media_ignore(folder: str, slot: str = None, all_slots: bool = False) -> None:
    """Remove a single ignored slot, or all ignored slots for a folder."""
    if not folder:
        return
    folder = folder.lower()
    conn = get_db()
    try:
        if all_slots or slot is None:
            conn.execute("DELETE FROM media_ignored WHERE folder = ?", (folder,))
        else:
            conn.execute(
                "DELETE FROM media_ignored WHERE folder = ? AND slot = ?",
                (folder, str(slot)),
            )
        conn.commit()
    finally:
        conn.close()


def get_media_ignores() -> dict:
    """Return {folder_lower: {"title": str, "slots": set(...)}}."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT folder, slot, title FROM media_ignored"
        ).fetchall()
        out = {}
        for r in rows:
            entry = out.setdefault(r["folder"], {"title": "", "slots": set()})
            entry["slots"].add(r["slot"])
            if r["title"]:
                entry["title"] = r["title"]
        return out
    finally:
        conn.close()


def init_app_settings_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute(_CREATE_USER_UI_PREFS_TABLE)
        conn.commit()
    finally:
        conn.close()

    _migrate_plaintext_admin_password()
    _migrate_sensitive_settings()


def _migrate_sensitive_settings():
    """Re-encrypt any core sensitive settings that are still stored as plaintext.

    Only covers SENSITIVE_KEYS: runtime-registered module keys aren't known yet
    at DB-init time and are migrated by register_sensitive_keys() instead, when
    the module that owns them registers.
    """
    _encrypt_existing_plaintext(SENSITIVE_KEYS)


def _migrate_plaintext_admin_password():
    """Remove any plaintext admin password that was previously stored in app_settings.
    If no admin account exists yet, create one from the stored credentials first."""
    conn = get_db()
    try:
        # Check whether the app_settings table exists at all (very first run)
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'"
        ).fetchone()
        if not tbl:
            return

        stored_user = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'web_admin_user'"
        ).fetchone()
        stored_pass = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'web_admin_pass'"
        ).fetchone()

        if not stored_pass:
            return  # Nothing to migrate

        plaintext_pass = stored_pass["value"]
        plaintext_user = stored_user["value"] if stored_user else ""

        # If there is no admin yet and we have credentials, create the admin properly
        admin_exists = conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
        ).fetchone()["cnt"] > 0

        if not admin_exists and plaintext_user and plaintext_pass:
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                    (plaintext_user, generate_password_hash(plaintext_pass), "admin"),
                )
                conn.commit()
                logger.info(
                    "Migrated plaintext admin credentials to hashed user account '%s'",
                    plaintext_user,
                )
            except Exception:
                logger.warning("Could not migrate plaintext admin credentials", exc_info=True)

        # Always remove the plaintext values from app_settings
        conn.execute("DELETE FROM app_settings WHERE key IN ('web_admin_pass', 'web_admin_user')")
        conn.commit()
        logger.info("Removed plaintext admin credentials from settings storage")
    except Exception:
        logger.warning("Error during admin credentials cleanup", exc_info=True)
    finally:
        conn.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return default
        val = row["value"]
        # Decrypt when the key is registered as sensitive *or* when the stored
        # value carries the encryption prefix: a module that registered a key
        # via register_sensitive_keys() and was later disabled/uninstalled
        # leaves an encrypted value behind, and reading it back must not hand
        # out the ciphertext just because nothing registered the key this run.
        if is_sensitive_key(key) or (val or "").startswith(_ENC_PREFIX):
            val = _decrypt_value(val)
        return val
    finally:
        conn.close()


def get_setting_int(key: str, default: int, env_key: str | None = None) -> int:
    """Integer setting with a fallback chain: DB -> environment -> *default*.

    Every value in that chain is user-editable, and a non-numeric one used to
    take the caller down with a ValueError. The DB side is validated when the
    settings page writes it, the environment side is not at all: a typo in
    e.g. MEDIAFORGE_SYNC_ERROR_RETRIES threw the auto-sync worker into its
    error branch on every cycle -- it slept 30s, retried, and never synced,
    without anything visible in the UI -- and made the settings page answer
    with a 500. A bad value is logged once and the default is used.
    """
    raw = get_setting(key)
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get(env_key, "") if env_key else ""
    if str(raw).strip() == "":
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning(
            "Setting %s has the non-numeric value %r — falling back to %s",
            env_key or key, raw, default,
        )
        return default


def set_setting(key: str, value: str) -> None:
    conn = get_db()
    try:
        stored = _encrypt_value(value) if is_sensitive_key(key) else value
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, stored),
        )
        conn.commit()
    finally:
        conn.close()
    _notify_setting_listeners(key, value)


# ---------------------------------------------------------------------------
# JSON-valued settings
# ---------------------------------------------------------------------------
#
# app_settings stores plain strings, so anything list- or dict-shaped has so far
# been hand-encoded at the call site: json.dumps() on save, json.loads() on read,
# each with its own (or no) try/except. A single corrupted value -- a half-written
# row, a hand-edited DB, a value written by an older version with a different
# shape -- therefore took down whatever read it, which is exactly the kind of
# failure a settings read must not have. These two helpers are the supported way
# for core *and* modules to keep a list/dict in a setting.


def get_json_setting(key: str, default=None):
    """Read a JSON-encoded setting, returning *default* on any problem.

    "Any problem" means: key missing, value empty, value not valid JSON, or the
    decoded value having a different container type than *default* (a caller
    that passes ``[]`` and gets a dict back would break one line later). A copy
    of *default* is returned, never the object itself, so a caller that mutates
    the result cannot poison the next caller's default.

    Never raises -- a broken value is logged once per read and treated as unset.
    """
    raw = get_setting(key)
    if raw is None or str(raw).strip() == "":
        return json.loads(json.dumps(default)) if default is not None else default
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Setting %s does not contain valid JSON — falling back to the default", key
        )
        return json.loads(json.dumps(default)) if default is not None else default
    if default is not None and not isinstance(parsed, type(default)):
        logger.warning(
            "Setting %s holds %s, expected %s — falling back to the default",
            key, type(parsed).__name__, type(default).__name__,
        )
        return json.loads(json.dumps(default))
    return parsed


def set_json_setting(key: str, value) -> None:
    """Store *value* (list/dict/scalar) as JSON under *key*.

    ``ensure_ascii=False`` keeps umlauts and non-Latin titles readable in the DB
    instead of turning them into \\uXXXX escapes. Anything not JSON-serialisable
    raises TypeError here, at the write, rather than silently landing in the DB
    as a repr that the matching read cannot parse.
    """
    set_setting(key, json.dumps(value, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Setting-change listeners
# ---------------------------------------------------------------------------
#
# So that "a setting changed" can be an event instead of something every
# interested party polls for. web/thirdparties/ subscribes exactly once and
# turns a write to "module:<id>:<key>" into that module's on_settings_changed()
# hook plus a restart of its background worker -- which is what every module
# with a bot was hand-rolling as a config poll on a 20-second timer.
#
# Listeners are called AFTER the value is committed (so a listener that reads
# the setting back sees the new one) and never inside the DB transaction.

_SETTING_LISTENERS = []


def add_setting_listener(fn) -> None:
    """Call ``fn(key, value)`` after every successful set_setting().

    `value` is the plaintext that was passed in, not what is stored (a sensitive
    key is encrypted at rest, and a listener has no business decrypting it just
    to be told what it already got).

    A listener must not raise and must be quick -- it runs on the thread that
    saved the setting, i.e. usually inside an HTTP request. Anything slow
    (restarting a bot) belongs on a thread of the listener's own; see
    web/thirdparties/__init__.py's _on_setting_changed(), which does exactly
    that.
    """
    if callable(fn) and fn not in _SETTING_LISTENERS:
        _SETTING_LISTENERS.append(fn)


def _notify_setting_listeners(key: str, value: str) -> None:
    """Fire every listener, swallowing (but logging) whatever they raise: a
    module with a broken handler must not be able to make saving a setting
    fail."""
    for fn in list(_SETTING_LISTENERS):
        try:
            fn(key, value)
        except Exception:
            logger.warning("Setting listener %r failed for key %r", fn, key, exc_info=True)


def get_encoding_ffmpeg_opts():
    """Read the encoding_* app_settings and build a dict with vcodec, acodec,
    vopts ready for ffmpeg.output() kwargs.

    Structure:
        {
            "vcodec": str | None,   # None means expert flags override via vopts
            "acodec": str | None,
            "vopts":  dict,         # extra encoder kwargs (preset, crf, etc.)
        }

    Note: as of this audit, no other module in the repo calls this function
    (grepped the whole tree) — routes/encoding.py reads/writes the same
    encoding_* settings directly via get_setting()/set_setting() instead.
    Kept here for whichever download/transcode step is meant to consume it.
    """
    import shlex

    def _parse_expert_flags(flags_str):
        """Parse '-c:v libx265 -preset slow -crf 18' -> dict for ffmpeg-python."""
        if not flags_str:
            return {}
        try:
            tokens = shlex.split(flags_str.strip())
        except Exception:
            return {}
        result = {}
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t.startswith("-"):
                key = t.lstrip("-")
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    val = tokens[i + 1]
                    try:
                        val = int(val)
                    except ValueError:
                        try:
                            val = float(val)
                        except ValueError:
                            pass
                    result[key] = val
                    i += 2
                else:
                    i += 1
            else:
                i += 1
        return result

    mode = get_setting("encoding_mode", "copy") or "copy"

    if mode == "copy":
        audio = get_setting("encoding_audio_copy", "copy") or "copy"
        audio_map = {"copy": "copy", "aac": "aac", "ac3": "ac3"}
        acodec = audio_map.get(audio, "copy")
        return {"vcodec": "copy", "acodec": acodec, "vopts": {}}

    if mode in ("h264", "h265"):
        hw      = get_setting(f"encoding_hw_{mode}", "cpu") or "cpu"
        preset  = get_setting(f"encoding_preset_{mode}", "medium") or "medium"
        crf_def = "23" if mode == "h264" else "28"
        crf     = int(get_setting(f"encoding_crf_{mode}", crf_def) or crf_def)
        audio   = get_setting(f"encoding_audio_{mode}", "copy") or "copy"

        # Map hw + mode -> encoder name
        codec_map = {
            "h264": {
                "cpu":          "libx264",
                "nvenc":        "h264_nvenc",
                "vaapi":        "h264_vaapi",
                "videotoolbox": "h264_videotoolbox",
            },
            "h265": {
                "cpu":          "libx265",
                "nvenc":        "hevc_nvenc",
                "vaapi":        "hevc_vaapi",
                "videotoolbox": "hevc_videotoolbox",
            },
        }
        vcodec = codec_map[mode].get(hw, "libx264" if mode == "h264" else "libx265")

        # Encoder-specific quality options
        vopts = {}
        if hw == "nvenc":
            vopts["preset"] = preset
            vopts["rc"]     = "vbr"
            vopts["cq"]     = crf      # NVENC quality knob
        elif hw == "vaapi":
            vopts["vf"]             = "format=nv12,hwupload"
            vopts["global_quality"] = crf
        elif hw == "videotoolbox":
            pass  # VideoToolbox quality is controlled differently per stream
        else:
            # CPU (libx264 / libx265)
            vopts["preset"] = preset
            vopts["crf"]    = crf

        audio_map = {"copy": "copy", "aac": "aac", "ac3": "ac3"}
        acodec = audio_map.get(audio, "copy")
        return {"vcodec": vcodec, "acodec": acodec, "vopts": vopts}

    if mode == "expert":
        video_flags = get_setting("encoding_expert_video", "") or ""
        audio_flags = get_setting("encoding_expert_audio", "") or ""
        vparsed = _parse_expert_flags(video_flags)
        aparsed = _parse_expert_flags(audio_flags)
        # Extract vcodec/acodec from parsed flags if present
        vcodec = vparsed.pop("c:v", vparsed.pop("vcodec", "copy"))
        acodec = aparsed.pop("c:a", aparsed.pop("acodec", "copy"))
        # Merge remaining audio opts into vopts (prefix a: to scope them to audio stream)
        vopts = dict(vparsed)
        for k, v in aparsed.items():
            vopts[f"a:{k}"] = v
        return {"vcodec": vcodec, "acodec": acodec, "vopts": vopts}

    # Fallback
    return {"vcodec": "copy", "acodec": "copy", "vopts": {}}


def delete_setting(key: str) -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def delete_settings_by_prefix(prefix: str) -> int:
    """Delete every app_settings row whose key starts with `prefix`, returning
    how many were removed.

    Exists for uninstalling a thirdparty module: everything a module stores
    lives under the "module:<module_id>:" prefix (see
    web/thirdparties/registry.py's module_setting_key()), so removing the
    module's folder can also remove its settings instead of leaving orphaned
    rows behind forever. `prefix` is escaped for LIKE (a module id containing
    % or _ would otherwise match far more than its own keys) -- note "_" is a
    LIKE wildcard and folder names use it constantly, which is exactly the
    trap this avoids.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM app_settings WHERE key LIKE ? ESCAPE '\\'", (escaped + "%",)
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()
