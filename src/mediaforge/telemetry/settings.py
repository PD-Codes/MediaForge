"""Telemetry settings persistence.

Thin wrapper around the project's existing DB-first settings store
(``web/db.py``'s ``get_setting``/``set_setting`` -- a generic key/value table,
``app_settings``, already used for every other persistent setting in this
project). No new table, no ``ALTER TABLE`` migration needed: unlike the
typed-column tables in ``web/db.py`` (``users``, ``download_queue``, ...),
``app_settings`` is schemaless key/value storage, so a brand-new setting key
is just a new row -- this module only defines *which* keys telemetry uses and
what they mean.

Keys used (all under the app_settings table):
    telemetry_install_id      TEXT   UUID4, generated once, stable until the
                                     user explicitly regenerates it.
    telemetry_consent_given   "1"/"0" -- None (row absent) means "the
                                     first-run consent dialog has not been
                                     answered yet", which is deliberately
                                     different from "0" (answered "No").
    telemetry_consent_at      TEXT   ISO-8601 UTC timestamp of the consent
                                     decision -- proof of when/whether
                                     consent was given (TELEMETRY_PLAN.md §7a).
    telemetry_enabled_keys    TEXT   JSON array of active data_keys (see
                                     registry.DATA_REGISTRY). Empty/absent
                                     means nothing is enabled.
"""

import json
import uuid
from datetime import datetime, timezone

from ..web.db import get_setting, set_setting

# Stage 1 default the first-run consent dialog grants on "Ja, Absturzberichte
# senden" (TELEMETRY_PLAN.md §4.0) -- install_id is always-on/no-toggle (see
# registry.DATA_REGISTRY), included here so a single enabled_keys set fully
# describes what may be sent.
CONSENT_DEFAULT_KEYS = ["install_id", "crash_reports", "system_info"]


def get_install_id() -> str:
    """Return this installation's UUID4, generating and persisting one on
    first access. Always available, independent of consent/enabled_keys --
    a local identifier existing is harmless; whether it's ever sent
    anywhere is gated entirely by telemetry_active()/is_key_enabled()."""
    value = get_setting("telemetry_install_id")
    if not value:
        value = str(uuid.uuid4())
        set_setting("telemetry_install_id", value)
    return value


def regenerate_install_id() -> str:
    """"Identität zurücksetzen" -- generate a brand-new install_id with no
    link kept to the old one (a deliberate break in history, not a
    migration; TELEMETRY_IMPLEMENTATION_PLAN.md §3.1). Returns the new id."""
    new_id = str(uuid.uuid4())
    set_setting("telemetry_install_id", new_id)
    return new_id


def is_consent_given():
    """Return True/False once the first-run consent dialog has been
    answered, or None if it hasn't been shown/answered yet -- the frontend
    uses that None case to decide whether to render the dialog at all."""
    raw = get_setting("telemetry_consent_given")
    if raw is None or raw == "":
        return None
    return raw == "1"


def get_consent_at():
    return get_setting("telemetry_consent_at") or None


def set_consent(granted: bool):
    """Record the first-run consent decision (or a later change of mind via
    the Settings page -- withdrawing consent must be exactly as easy as
    granting it, TELEMETRY_PLAN.md §7a). Granting sets the stage-1 defaults;
    withdrawing clears every enabled key, not just the stage-1 ones."""
    set_setting("telemetry_consent_given", "1" if granted else "0")
    set_setting("telemetry_consent_at", datetime.now(timezone.utc).isoformat())
    if granted:
        set_enabled_keys(CONSENT_DEFAULT_KEYS)
    else:
        set_enabled_keys([])


def get_enabled_keys() -> set:
    raw = get_setting("telemetry_enabled_keys")
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
        return {k for k in parsed if isinstance(k, str)}
    except (ValueError, TypeError):
        return set()


def set_enabled_keys(keys):
    """Overwrite the full set of enabled data_keys (not additive -- the
    Settings page always sends the complete desired end state after the
    confirmation dialog, TELEMETRY_IMPLEMENTATION_PLAN.md §3.7)."""
    set_setting("telemetry_enabled_keys", json.dumps(sorted(set(keys))))


def is_key_enabled(data_key: str) -> bool:
    """Whether a specific data_key may currently be collected/sent. False
    whenever consent hasn't been actively granted, even if enabled_keys
    somehow contains the key (defense in depth -- enabled_keys is only ever
    written together with consent via set_consent()/set_enabled_keys(), but
    this function is the actual gate every event builder calls)."""
    if is_consent_given() is not True:
        return False
    return data_key in get_enabled_keys()


def telemetry_active() -> bool:
    """Overall kill switch checked by TelemetryClient before it will queue
    anything at all: consent must be granted AND at least one key enabled."""
    return is_consent_given() is True and bool(get_enabled_keys())
