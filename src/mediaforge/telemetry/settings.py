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

import threading
import time
import uuid
from datetime import datetime, timezone

from ..web.db import get_json_setting, get_setting, set_json_setting, set_setting

# Stage 1 default the first-run consent dialog grants on "Ja, Absturzberichte
# senden" (TELEMETRY_PLAN.md §4.0) -- install_id is always-on/no-toggle (see
# registry.DATA_REGISTRY), included here so a single enabled_keys set fully
# describes what may be sent.
CONSENT_DEFAULT_KEYS = ["install_id", "crash_reports", "system_info"]


# ---------------------------------------------------------------------------
# Consent/enabled_keys read cache
# ---------------------------------------------------------------------------
# is_key_enabled() is the gate EVERY event builder calls, and each call used to
# be two uncached SELECTs on app_settings (one for consent, one for the key
# set) -- plus two more from telemetry_active() inside client.submit(). With the
# feature flags now wired across the whole app that is up to four blocking
# SQLite reads per event, on paths that hold locks (SyncPlay room bookkeeping) or
# sit inside a request. The queue worker holds write transactions and
# busy_timeout is 30s, so those reads are not free.
#
# A short TTL is the right trade-off here rather than a permanent cache with
# explicit invalidation only: writes DO invalidate immediately (see
# set_consent()/set_enabled_keys() below), so the only staleness left is a
# settings change made in a DIFFERENT process -- multiple gunicorn workers, or
# the CLI running alongside the web UI. Those converge within _CACHE_TTL, and
# the direction of the error is bounded: a user who just switched something off
# may have at most _CACHE_TTL seconds of events still built by another worker.
_CACHE_TTL = 5.0  # seconds
_cache_lock = threading.Lock()
_cache_stamp = 0.0        # time.monotonic() of the last DB read; 0.0 == invalid
_cache_consent = None     # True / False / None, as is_consent_given() defines it
_cache_keys = frozenset()


def _invalidate_cache():
    """Drop the cached consent/enabled_keys snapshot. Called after every write
    so a settings change is visible to this process on the very next event."""
    global _cache_stamp
    with _cache_lock:
        _cache_stamp = 0.0


def _read_state():
    """Return (consent, enabled_keys) -- from cache when fresh, otherwise from
    the DB. The DB read deliberately happens OUTSIDE the lock: holding it across
    a potentially blocking SQLite read would serialize every event builder in the
    process behind one slow read, which is the problem this cache exists to
    avoid. Two threads racing simply both read and both store the same value."""
    global _cache_stamp, _cache_consent, _cache_keys
    now = time.monotonic()
    with _cache_lock:
        if _cache_stamp and (now - _cache_stamp) < _CACHE_TTL:
            return _cache_consent, _cache_keys

    raw_consent = get_setting("telemetry_consent_given")
    if raw_consent is None or raw_consent == "":
        consent = None
    else:
        consent = raw_consent == "1"

    # get_json_setting() already turns a missing/corrupt/wrong-shaped value into
    # the [] default, so a broken row means "nothing enabled" -- which is the
    # safe direction for telemetry -- instead of raising in here.
    keys = frozenset(
        k for k in get_json_setting("telemetry_enabled_keys", []) if isinstance(k, str)
    )

    with _cache_lock:
        _cache_consent = consent
        _cache_keys = keys
        _cache_stamp = time.monotonic()
    return consent, keys


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
    # The device secret belongs to the OLD install_id -- the server derives it
    # from that id and will refuse any signature the new one makes with it.
    # Keeping it would not merely be useless, it would break enrollment
    # silently: ensure_enrolled() sees a stored secret, never registers the new
    # identity, and every request is then refused as a bad signature. Imported
    # here rather than at module scope because device_auth imports this module.
    from . import device_auth
    device_auth.clear()
    return new_id


def is_consent_given():
    """Return True/False once the first-run consent dialog has been
    answered, or None if it hasn't been shown/answered yet -- the frontend
    uses that None case to decide whether to render the dialog at all.

    Served from the short-lived read cache (see _read_state())."""
    consent, _keys = _read_state()
    return consent


def get_consent_at():
    return get_setting("telemetry_consent_at") or None


def set_consent(granted: bool):
    """Record the first-run consent decision (or a later change of mind via
    the Settings page -- withdrawing consent must be exactly as easy as
    granting it, TELEMETRY_PLAN.md §7a). Granting sets the stage-1 defaults;
    withdrawing clears every enabled key, not just the stage-1 ones."""
    set_setting("telemetry_consent_given", "1" if granted else "0")
    set_setting("telemetry_consent_at", datetime.now(timezone.utc).isoformat())
    _invalidate_cache()
    if granted:
        set_enabled_keys(CONSENT_DEFAULT_KEYS)
    else:
        set_enabled_keys([])


def get_enabled_keys() -> set:
    """The set of currently active data_keys. Returns a NEW set each call (not
    the cached frozenset) so a caller mutating the result cannot corrupt the
    cache for everyone else."""
    _consent, keys = _read_state()
    return set(keys)


def set_enabled_keys(keys):
    """Overwrite the full set of enabled data_keys (not additive -- the
    Settings page always sends the complete desired end state after the
    confirmation dialog, TELEMETRY_IMPLEMENTATION_PLAN.md §3.7)."""
    set_json_setting("telemetry_enabled_keys", sorted(set(keys)))
    _invalidate_cache()


def is_key_enabled(data_key: str) -> bool:
    """Whether a specific data_key may currently be collected/sent. False
    whenever consent hasn't been actively granted, even if enabled_keys
    somehow contains the key (defense in depth -- enabled_keys is only ever
    written together with consent via set_consent()/set_enabled_keys(), but
    this function is the actual gate every event builder calls)."""
    consent, keys = _read_state()
    if consent is not True:
        return False
    return data_key in keys


def telemetry_active() -> bool:
    """Overall kill switch checked by TelemetryClient before it will queue
    anything at all: consent must be granted AND at least one key enabled."""
    consent, keys = _read_state()
    return consent is True and bool(keys)
