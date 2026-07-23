"""Typed telemetry event builders.

The only place in this package where an event payload is actually assembled.
Every builder here:

  1. Checks settings.is_key_enabled(data_key) (or is_adult_provider() first,
     for anything provider-related beyond the stage-2 flag) BEFORE touching
     any data -- per TELEMETRY_PLAN.md §3: "Events werden nur gebaut und
     verschickt, wenn der jeweilige data_key aktiv ist -- Prüfung passiert
     vor der Datenerhebung, nicht erst vor dem Versand."
  2. Returns None (or an empty list, for the builders that can produce more
     than one data_key at once) when disabled/guarded, so callers can just
     do ``client.submit(events.build_x(...))`` without an extra "is this
     even on" check of their own.

Callers never build the {"data_key", "occurred_at", "payload"} envelope by
hand elsewhere in the codebase -- that would risk a second, drifting copy of
the sanitizing/guard logic.
"""

from datetime import datetime, timezone

from . import settings
from .sanitize import (clean_url, is_adult_provider, redact_secrets, redact_urls_in_text,
                        sanitize_exception, shorten_path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(data_key, payload):
    return {"data_key": data_key, "occurred_at": _now_iso(), "payload": payload}


# ---------------------------------------------------------------------------
# Stage 1 — crash / system
# ---------------------------------------------------------------------------

def _attach_runtime(payload):
    """Attach a small volatile runtime snapshot (RAM/disk/load/threads/fds at
    the moment of the error) to a crash payload, so a report can show the
    machine state when it broke -- was RAM exhausted (OOM), the disk full, the
    load pegged. Best-effort and fully guarded: a problem here must never turn
    a crash report into a second failure, so it simply omits the snapshot.
    Rides on the crash_reports consent (the event it is attached to); the
    registry's crash_reports explain text documents it."""
    try:
        from . import sysinfo
        snap = sysinfo.runtime_snapshot()
        if snap:
            payload["runtime"] = snap
    except Exception:
        pass


def build_crash_event(exc_type, exc_value, tb):
    """Build a crash_reports event from a (exc_type, exc_value, tb) triple
    (sys.exc_info() shape). Returns None if the user hasn't enabled
    crash_reports."""
    if not settings.is_key_enabled("crash_reports"):
        return None
    payload = sanitize_exception(exc_type, exc_value, tb)
    _attach_runtime(payload)
    return _event("crash_reports", payload)


def build_log_error_event(record):
    """Build a crash_reports event from a logging.LogRecord that reached
    ERROR level with no exception object available at all (see
    hooks._TelemetryLogHandler -- the common case, a logger.error(f"...: {e}")
    call still inside its own except block, is handled by build_crash_event()
    via a live sys.exc_info() instead, since that gives a real traceback).

    This is the fallback for a bare logger.error("message") with no except
    block backing it -- there is no call stack to walk, so this reports only
    the log call site itself (file/line/function from the LogRecord) plus the
    formatted message, sanitized the same way a real traceback would be.
    Still far more useful for fixing a bug than nothing at all, which is the
    alternative every time code logs an error without raising."""
    if not settings.is_key_enabled("crash_reports"):
        return None
    message = redact_secrets(redact_urls_in_text(record.getMessage()))[:2000]
    filename = shorten_path(record.pathname)
    frame = {"filename": filename, "lineno": record.lineno, "name": record.funcName, "line": ""}
    traceback_text = (
        f"LoggedError (no exception object, logger.error() call site only)\n"
        f'  File "{filename}", line {record.lineno}, in {record.funcName}\n'
        f"    {message}"
    )
    payload = {
        "exception_type": "LoggedError",
        "message": message,
        "frames": [frame],
        "traceback_text": traceback_text,
    }
    _attach_runtime(payload)
    return _event("crash_reports", payload)


def build_system_info_event():
    """Build a system_info event. Returns None if the user hasn't enabled
    system_info. Usually sent once per app start rather than per-crash;
    hooks.init_telemetry() takes care of that timing.

    Beyond the original app/OS/Python/arch fields, this now carries the
    extended, error-analysis-oriented context gathered by ``sysinfo.collect()``
    (container runtime, Linux distro/libc/kernel, CPU model + core counts, GPU
    names and -- most useful for the transcoding/upscaling failure paths -- the
    hardware-acceleration methods and encoders ffmpeg actually has). All of
    those are best-effort: any field the current platform can't determine is
    simply omitted from the payload, so the event stays small and truthful
    rather than padded with nulls. ``sysinfo.collect()`` caches its result, so
    the (subprocess-touching) detection runs once per process, not per event."""
    if not settings.is_key_enabled("system_info"):
        return None
    import platform

    from .. import config
    from . import sysinfo

    payload = {
        "app_version": config.VERSION or "unknown",
        "os": platform.system(),
        "os_version": platform.version(),
        "python_version": platform.python_version(),
        "arch": platform.machine(),
    }

    # Extended context -- merged in only where a value was actually determined,
    # so the payload never carries a key whose value is None/empty. Guarded on
    # its own so a problem gathering the extras can never suppress the core
    # fields above.
    try:
        extra = sysinfo.collect()
    except Exception:
        extra = {}
    for key, value in (extra or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)) and not value:
            continue  # drop empty lists (e.g. no GPU / no hwaccel found)
        payload[key] = list(value) if isinstance(value, (list, tuple)) else value

    # UI language(s) in use -- an install-level field, so it lives here rather
    # than in the OS-level sysinfo leaf: the distinct set of per-user UI
    # languages ("de", "en", or "de,en"), which helps place locale-specific
    # rendering/formatting bugs. Read straight from the users table via the
    # existing sqlite helper (no request/session context needed); fully
    # guarded so a DB hiccup never suppresses the rest of the event.
    try:
        from ..web.db import get_db
        conn = get_db()
        try:
            rows = conn.execute("SELECT DISTINCT language FROM users").fetchall()
        finally:
            conn.close()
        langs = sorted({r["language"] for r in rows if r["language"] in ("en", "de")})
        if langs:
            payload["ui_language"] = ",".join(langs)
    except Exception:
        pass

    return _event("system_info", payload)


# ---------------------------------------------------------------------------
# Stage 2 — feature flags (usage yes/no + counter)
# ---------------------------------------------------------------------------

def build_feature_flag_event(feature_key: str, provider=None):
    """Build a flag.* usage-counter event. feature_key may be given with or
    without the "flag." prefix.

    Deliberately does NOT call is_adult_provider(): the stage-2 usage
    counter is the one data point explicitly still allowed for hanime_tv
    (see registry.DATA_REGISTRY["flag.hanime_tv"] and
    sanitize.is_adult_provider()'s docstring) -- callers pass
    feature_key="flag.hanime_tv" for that provider and this builder just
    checks whether that specific data_key is enabled, same as any other
    flag.* key. Never pass a *different* provider's activity through
    "flag.hanime_tv", and never pass provider="hanime_tv" to any OTHER
    feature_key.
    """
    data_key = feature_key if feature_key.startswith("flag.") else f"flag.{feature_key}"
    if not settings.is_key_enabled(data_key):
        return None
    payload = {}
    if provider:
        payload["provider"] = provider
    return _event(data_key, payload)


# ---------------------------------------------------------------------------
# Stage 3 — feature details & errors
# ---------------------------------------------------------------------------

def build_feature_detail_event(feature_key: str, *, action=None, status=None,
                                metadata=None, provider=None):
    """Build a detail.* event. Guarded by is_adult_provider() first (stage 3
    is beyond the hanime_tv exception -- no details are ever built for it)."""
    if is_adult_provider(provider):
        return None
    data_key = feature_key if feature_key.startswith("detail.") else f"detail.{feature_key}"
    if not settings.is_key_enabled(data_key):
        return None
    payload = {"action": action, "status": status}
    if metadata:
        payload["metadata"] = metadata
    return _event(data_key, payload)


# ---------------------------------------------------------------------------
# Stage 4 — download content
# ---------------------------------------------------------------------------

def build_download_event(*, provider, media_type, title, season=None, episode=None,
                          status="completed", error_message=None, provider_errors=None):
    """Build up to two events (downloads.titles / downloads.errors -- each
    individually toggled by the user) for one finished/failed download.
    Returns a list (possibly empty), never None, so callers can always do
    ``client.submit_all(events.build_download_event(...))``.

    ``provider_errors`` is an optional {hoster: error_message} map covering
    every provider tried in the fallback chain. It rides along in the
    downloads.errors payload so a debug report shows WHY each provider failed
    -- not just the single error that happened to be surfaced to the user (see
    queue_worker.py: a later hoster's "not available" skip can otherwise mask
    the real failure of the picked provider). Each entry is sanitized exactly
    like error_message.

    Guarded by is_adult_provider() first -- no download event of any kind is
    ever built for hanime_tv."""
    if is_adult_provider(provider):
        return []
    out = []
    if settings.is_key_enabled("downloads.titles"):
        out.append(_event("downloads.titles", {
            "provider": provider, "media_type": media_type, "title": title,
            "season": season, "episode": episode, "status": status,
        }))
    if error_message and settings.is_key_enabled("downloads.errors"):
        payload = {
            "provider": provider, "media_type": media_type, "title": title,
            "season": season, "episode": episode, "status": status,
            "error_message": redact_secrets(str(error_message))[:2000],
        }
        if provider_errors:
            payload["provider_errors"] = {
                str(hoster): redact_secrets(redact_urls_in_text(str(msg)))[:500]
                for hoster, msg in provider_errors.items()
            }
        out.append(_event("downloads.errors", payload))
    return out


def build_direct_link_event(url: str):
    """Build a direct_link.urls event. Not currently wired to a live call
    site (registry-only for now, see routes/direct_link.py TODO) but
    provided here so the builder exists alongside its data_key."""
    if not settings.is_key_enabled("direct_link.urls"):
        return None
    return _event("direct_link.urls", {"url": clean_url(url)})


# ---------------------------------------------------------------------------
# Stage 5 — playback context
# ---------------------------------------------------------------------------

def build_play_event(*, provider, media_type, title, season=None, episode=None, context="direct"):
    """Build a stream.play_events event -- "this title was started", no
    watch time. Guarded by is_adult_provider() first."""
    if is_adult_provider(provider):
        return None
    if not settings.is_key_enabled("stream.play_events"):
        return None
    return _event("stream.play_events", {
        "provider": provider, "media_type": media_type, "title": title,
        "season": season, "episode": episode, "context": context,
    })


# ---------------------------------------------------------------------------
# Stage 6 — watch behaviour
# ---------------------------------------------------------------------------

def build_watch_event(*, provider, media_type, title, season=None, episode=None,
                       watch_seconds=None, progress_percent=None, completed=None):
    """Build up to three events (watch.progress / watch.duration /
    watch.completion -- each individually toggled) for one playback-progress
    update. Returns a list (possibly empty), never None.

    Guarded by is_adult_provider() first -- no watch behaviour of any kind
    is ever built for hanime_tv, regardless of which stage-6 keys the user
    enabled."""
    if is_adult_provider(provider):
        return []
    base = {
        "provider": provider, "media_type": media_type, "title": title,
        "season": season, "episode": episode,
    }
    out = []
    if progress_percent is not None and settings.is_key_enabled("watch.progress"):
        out.append(_event("watch.progress", {**base, "progress_percent": progress_percent}))
    if watch_seconds is not None and settings.is_key_enabled("watch.duration"):
        out.append(_event("watch.duration", {**base, "watch_seconds": watch_seconds}))
    if completed is not None and settings.is_key_enabled("watch.completion"):
        out.append(_event("watch.completion", {**base, "completed": bool(completed)}))
    return out
