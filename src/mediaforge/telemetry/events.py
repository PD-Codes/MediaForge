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
from .classify import is_cancel_exception_name, is_cancel_status, is_user_cancellation
from .registry import consent_key_for, source_flag_key
from .sanitize import (clean_url, collapse_paths_in_text, is_adult_provider,
                        mentions_adult_provider, redact_secrets, redact_urls_in_text,
                        sanitize_exception, shorten_path, strip_browser_launch_noise,
                        strip_url_paths)


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
    # A user pressing "Cancel" (on a queue item, on a captcha window, Ctrl+C in
    # CLI mode, or simply closing the browser tab an SSE stream was feeding) is
    # the app working correctly, not a defect. Those unwind through the very
    # same excepthooks/log handler a real crash does, so the ONLY place that can
    # reliably keep them out of the crash channel is right here, before anything
    # is built -- see telemetry/classify.py for what counts as a cancellation.
    if is_user_cancellation(exc_type, exc_value):
        return None
    payload = sanitize_exception(exc_type, exc_value, tb)
    # The hard 18+ rule (sanitize.is_adult_provider) applies to this channel too:
    # an exception message or a source line can carry the watch URL just as a log
    # line can, and crash_reports is stage 1 -- enabled without any consent to
    # watch data. Checked on the already-sanitized text, so it costs one scan of
    # a string that is at most MAX_TRACEBACK_BYTES long.
    if mentions_adult_provider(payload.get("traceback_text")):
        return None
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
    # Same rule as build_crash_event(), but there is no exception object here --
    # only the formatted text. Cancellations in this codebase carry a fixed
    # wording ("Download cancelled", "cancelled by the user"), which is exactly
    # what classify's message patterns match on.
    raw_message = record.getMessage()
    if is_user_cancellation(message=raw_message):
        return None
    # The crash channel is stage 1 and can be enabled entirely on its own, so a
    # log line naming the age-gated provider (the download watchdog logs the
    # episode URL verbatim) must not travel through it -- that is precisely the
    # watch data stages 4-6 exist to gate. Same guard as every provider-aware
    # builder above, just asked about free text; see sanitize.mentions_adult_provider().
    if mentions_adult_provider(raw_message):
        return None
    # strip_url_paths() rather than redact_urls_in_text(): for a log message the
    # URL path is the content-identifying part (which series/episode), and
    # collapse_paths_in_text() does the same for local filesystem paths, so a
    # "No such file: C:\Users\<name>\..." log line cannot carry the username.
    message = redact_secrets(
        collapse_paths_in_text(strip_url_paths(strip_browser_launch_noise(raw_message)))
    )[:2000]
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

    # Which theme pack this instance has applied -- the store's module id, or
    # "default" for the built-in look. An install-level preference like
    # ui_language above, and it answers the one question downloads cannot: a
    # theme downloaded once and kept beats one downloaded fifty times and
    # switched away from the same evening, and the built-in theme is never
    # downloaded at all. The store counts NULL as "not reported" rather than
    # folding it into the default, so the key is simply absent when this fails
    # -- never an empty string, which would be neither fact.
    try:
        from ..web import themes as _themes
        payload["active_theme"] = (_themes.active_theme() or {}).get(
            "id") or _themes.BUILTIN_THEME_ID
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


def build_source_usage_event(source_id):
    """Build a ``flag.source.<id>`` counter for one built-in source site.

    Answers the one question the download events could not: WHICH SITE the
    content came from. ``build_download_event``'s ``provider`` is the hoster
    (VOE, MegaPlay, ...), which says nothing about whether anybody actually
    uses filmo.to or 9anime -- i.e. nothing about which sources are worth
    maintaining.

    Three guards, in this order:

    1. Unknown/module source ids produce nothing. ``source_flag_key`` accepts
       only the closed built-in list, because this value becomes a
       ``feature_key`` on a public server and a module id is text the module
       author chose.
    2. The adult source can never pass guard 1, and is checked again here by
       name, because "it cannot get here" is not the kind of thing this
       particular rule should rely on.
    3. Consent is read from the single ``flag.sources`` toggle
       (``consent_key_for``), not from the per-source key, which is never
       shown to the user.
    """
    key = source_flag_key(source_id)
    if key is None:
        return None
    if is_adult_provider(source_id) or str(source_id).strip().lower().startswith("hanime"):
        return None
    if not settings.is_key_enabled(consent_key_for(key)):
        return None
    return _event(key, {})


# Actions accepted by build_network_detail_event. A closed set: `action` lands
# in an indexed column on the server, so it must not be free text.
NETWORK_ACTIONS = ("dns_fallback", "source_unavailable")


def build_network_detail_event(action, source_id=None):
    """Build a ``detail.network`` event for a network problem the app survived.

    ``action="dns_fallback"``   -- the configured resolver could not resolve a
    host and the system resolver answered instead. NO metadata at all: the
    hostname would say which site was being visited, and the resolver name is
    the user's own network configuration.

    ``action="source_unavailable"`` -- a source site failed to load. Carries
    the source id only, and only for a built-in source (same closed list as
    build_source_usage_event, for the same reason).

    Returns None for an unknown action rather than inventing one, so a typo at
    a call site cannot create a new server-side action value.
    """
    if action not in NETWORK_ACTIONS:
        return None
    if not settings.is_key_enabled("detail.network"):
        return None
    metadata = None
    if action == "source_unavailable":
        key = source_flag_key(source_id)
        if key is None:
            # An outage we cannot name without leaking a module's id is still
            # worth counting -- just anonymously.
            metadata = {"source": "other"}
        else:
            metadata = {"source": str(source_id).strip().lower()}
    return build_feature_detail_event("detail.network", action=action,
                                      status="handled", metadata=metadata)


# The library overview page itself. Every OTHER accepted section name is a
# media-kind slug straight out of web/media_kinds.py, so this is the only
# literal that has to live here.
LIBRARY_HUB_SECTION = "hub"


def _library_sections():
    """The closed set of section names build_library_view_event() accepts.

    web/media_kinds.py is imported LAZILY, inside the function: importing
    anything under ``mediaforge.web`` at module level executes
    ``web/__init__.py`` -> ``web/app.py`` and would drag the queue worker,
    every provider and their third-party dependencies into this leaf package
    (the same reason registry.py keeps the devInfo URL as its own literal, and
    the same pattern build_system_info_event() already uses for ``web.db``).

    Returns None when the registry cannot be read at all -- the caller then
    drops the event instead of sending an unvalidated string, so a future
    refactor of media_kinds.py can only ever cost a data point, never widen
    what leaves the device.
    """
    try:
        from ..web.media_kinds import ALL_SLUGS
    except Exception:
        return None
    return frozenset(ALL_SLUGS) | {LIBRARY_HUB_SECTION}


def build_library_view_event(section):
    """Build the stage-2 flag.library usage counter for one library page view.

    ``section`` is either LIBRARY_HUB_SECTION ("hub", the overview page) or a
    media-kind slug ("video", "book", "manga", ...) -- including the kinds that
    only render a "coming soon" placeholder today, because "people keep opening
    the Manga tile" is precisely the signal that page exists to produce.

    The payload is that one word and nothing else. No provider is involved
    anywhere in this event, so there is no adult-provider dimension to guard
    against: the library lists local files, and neither the section name nor
    anything else here can carry a title, a path or a hoster. (Do not add a
    provider field later -- see sanitize.is_adult_provider().)

    The value is checked against the registry's closed slug set rather than
    passed through: an unknown/typo'd/attacker-supplied segment (this is fed
    from a URL path segment) returns None instead of shipping a free-text
    string to the server.
    """
    if not settings.is_key_enabled("flag.library"):
        return None
    allowed = _library_sections()
    if not allowed:
        return None
    section = str(section or "").strip().lower()
    if section not in allowed:
        return None
    return _event("flag.library", {"section": section})


# ---------------------------------------------------------------------------
# Stage 3 — feature details & errors
# ---------------------------------------------------------------------------

def build_feature_detail_event(feature_key: str, *, action=None, status=None,
                                metadata=None, provider=None):
    """Build a detail.* event. Guarded by is_adult_provider() first (stage 3
    is beyond the hanime_tv exception -- no details are ever built for it).

    A cancelled operation produces NOTHING at all: neither a ``status="cancelled"``
    row nor an ``error_type`` describing the cancellation exception. Stage 3 is
    "feature details & errors" -- the user aborting their own job is neither. This
    guard is deliberately here rather than only at the ~20 call sites, so a future
    call site cannot reintroduce the problem by forgetting it (see
    telemetry/classify.py).
    """
    if is_adult_provider(provider):
        return None
    if is_cancel_status(status):
        return None
    if metadata:
        # Call sites commonly pass metadata={"error_type": type(e).__name__, ...}
        # -- a cancellation that reached an except block that broad must not slip
        # through just because the caller stamped it status="error".
        if is_cancel_exception_name(metadata.get("error_type")) or \
           is_user_cancellation(message=str(metadata.get("error", ""))) or \
           is_user_cancellation(message=str(metadata.get("reason", ""))):
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
                          status="completed", error_message=None, provider_errors=None,
                          source=None):
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
    ever built for hanime_tv.

    A cancelled download produces NO event at all -- not even a downloads.titles
    row with status="cancelled". queue_worker.py already books a cancelled episode
    on its own branch without calling this builder, but a cancel can also surface
    as a plain failure deeper down (an ffmpeg process killed by the cancel event,
    a hoster chain aborted mid-way), and that path DOES arrive here with
    status="failed" plus a "Download cancelled" message. Both shapes are filtered
    here so the check lives in exactly one place."""
    if is_adult_provider(provider):
        return []
    if is_cancel_status(status) or is_user_cancellation(message=error_message):
        return []
    # The SITE the download came from ("filmo", "nineanime", ...) alongside the
    # hoster. Only ever a built-in id -- source_flag_key() rejects a module's
    # own id, which is text its author chose and has no place in a field the
    # server groups by. None (unknown/module/adult) simply omits the field
    # rather than sending a placeholder.
    site = source_flag_key(source)
    site = source.strip().lower() if site else None

    out = []
    if settings.is_key_enabled("downloads.titles"):
        payload = {
            "provider": provider, "media_type": media_type, "title": title,
            "season": season, "episode": episode, "status": status,
        }
        if site:
            payload["source"] = site
        out.append(_event("downloads.titles", payload))
    if error_message and settings.is_key_enabled("downloads.errors"):
        payload = {
            "provider": provider, "media_type": media_type, "title": title,
            "season": season, "episode": episode, "status": status,
            "error_message": redact_secrets(str(error_message))[:2000],
        }
        if site:
            payload["source"] = site
        if provider_errors:
            payload["provider_errors"] = {
                str(hoster): redact_secrets(redact_urls_in_text(str(msg)))[:500]
                for hoster, msg in provider_errors.items()
            }
        out.append(_event("downloads.errors", payload))
    return out


def build_direct_link_event(url: str):
    """Build a direct_link.urls event -- the URL a Direct Link download was
    started from, with query string, fragment and any userinfo stripped by
    clean_url().

    Note for callers: this builder takes no provider and therefore CANNOT run
    the is_adult_provider() guard itself. A caller that cannot establish with
    certainty that the URL is not 18+ content must not call it at all (see
    routes/direct_link.py, which then sends only flag.direct_link)."""
    if not settings.is_key_enabled("direct_link.urls"):
        return None
    return _event("direct_link.urls", {"url": clean_url(url)})


# ---------------------------------------------------------------------------
# Stage 5 — playback context
# ---------------------------------------------------------------------------

# The two stage-5 data_keys this builder can produce. They are SEPARATE consent
# toggles (see registry.DATA_REGISTRY), so a caller must name the one that
# matches what it is reporting -- emitting SyncPlay room content under
# "stream.play_events" would hand the title to a user who enabled ordinary play
# events but deliberately left the SyncPlay one off.
_PLAY_EVENT_KEYS = ("stream.play_events", "syncplay.room_content")


def build_play_event(*, provider, media_type, title, season=None, episode=None,
                      context="direct", data_key="stream.play_events"):
    """Build a stage-5 playback event -- "this title was started", no watch time.

    ``data_key`` picks which of the two stage-5 keys is used:

      * ``stream.play_events``   -- an ordinary playback start (default).
      * ``syncplay.room_content`` -- the title currently playing in a SyncPlay
        room. Same payload shape (the server routes both into PlayEvent and
        derives context="syncplay" from the key), but its own consent toggle.

    An unknown data_key returns None rather than silently falling back to
    stream.play_events, so a typo can never widen what gets sent. Guarded by
    is_adult_provider() first."""
    if is_adult_provider(provider):
        return None
    if data_key not in _PLAY_EVENT_KEYS:
        return None
    if not settings.is_key_enabled(data_key):
        return None
    return _event(data_key, {
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
