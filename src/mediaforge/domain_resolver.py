"""Remote domain resolver — keeps a source site's live domain up to date.

Some source sites rotate their domain on a schedule that has nothing to do
with MediaForge: MegaKino, for instance, simply increments a counter
(``megakino.to`` -> ``megakino14.com`` -> ``megakino16.com`` -> ...) whenever
the current one gets blocked or seized. Shipping that domain as a constant
means every rotation breaks the provider until a new release goes out, and the
mirror list in :mod:`mediaforge.mirrors` cannot help either — a domain nobody
has seen yet cannot be in a hardcoded fallback list.

So the domain is looked up instead of hardcoded. A small first-party endpoint
tracks the live domain and publishes it as JSON::

    GET https://mediaforge.pd-codes.net/domains.json

    {"megakino": {"final_url": "https://megakino16.com",
                  "last_check": "2026-08-23T10:33:51.699325"}}

This module fetches that document, validates it hard (see :func:`_valid_host`),
caches it in memory, and persists the result in ``app_settings`` so a cold
start without network still comes up on the last known good domain.

Wiring
------
* :func:`resolved_host` is read by ``mirrors.canonical_host()`` /
  ``mirrors._load_mirrors()``, which makes the resolved domain the *first*
  host of that site's mirror list. The shipped domains stay in the list as
  fallbacks, so a stored URL, favourite or queue entry written against the old
  domain keeps resolving and is transparently rewritten on egress.
* ``config.MEGAKINO_BASE_URL`` / ``config.megakino_base_url()`` build new URLs
  from the same value, so search results and "open in browser" links point at
  the live domain rather than a dead one.
* ``web/routes/settings.py`` exposes the state and the on/off switch under
  Settings -> Sources -> "Domain fallback (mirrors)".

Design constraints
------------------
* **Never blocking.** :func:`resolved_host` only ever reads the in-memory
  cache and, when that is stale, kicks off a background refresh. It is called
  from inside ``mirrors._load_mirrors()``, i.e. on the HTTP egress path — a
  synchronous fetch there would put a network round-trip in front of every
  scraper request and deadlock the moment the endpoint hangs.
* **Re-entrancy safe.** The refresh itself goes out through
  ``config.GLOBAL_SESSION``, which routes through ``mirrors``, which calls
  back into this module. ``_refreshing`` short-circuits that second call to a
  plain cache read, so the cycle terminates instead of recursing.
* **Only mapped sites are honoured.** The endpoint can list anything; only
  keys present in :data:`RESOLVABLE_SITES` are applied, and only when the host
  it hands back still looks like that site (see the brand-token check in
  :func:`_valid_host`). A compromised or mistyped feed therefore cannot point
  the scraper — or the image proxy's allowlist — at an arbitrary host.
"""

import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

from .logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# The published domain feed. Overridable so a fork, a self-hosted instance or a
# test run can point at its own endpoint. Only https is accepted (see
# _feed_url()): the answer decides where the scraper sends its traffic, so it
# must not be tamperable in transit.
DEFAULT_DOMAINS_URL = "https://mediaforge.pd-codes.net/domains.json"

# Sites whose domain may be taken from the feed: JSON key -> (mirror site id,
# brand token). The brand token must appear in the hostname the feed hands
# back, which is what stops a bad/compromised feed from redirecting a source to
# an unrelated host. It is deliberately the same token the URL patterns in
# config.py already match on (``[^/]*megakino[^/]*``).
#
# Only MegaKino is listed. The other sites' domains are stable and stay fully
# under the hardcoded mirror lists in mirrors.py — a site is opted IN here, it
# is never resolved implicitly.
RESOLVABLE_SITES = {
    "megakino": ("megakino", "megakino"),
}

# Setting key that turns the whole thing off (Settings -> Sources). Default on.
ENABLED_SETTING = "domain_resolver_enabled"

# app_settings keys used to survive a restart: the host and the feed's own
# last_check timestamp, per site.
_HOST_SETTING = "resolved_domain_%s"
_CHECK_SETTING = "resolved_domain_%s_checked"

# How long a successful lookup is trusted before a background refresh runs.
_TTL = 6 * 60 * 60  # 6 hours
# Backoff after a failed lookup, so an endpoint that is down is not hammered
# once per mirror-cache miss (every 30s).
_ERROR_BACKOFF = 15 * 60  # 15 minutes
# Total wall-clock budget for one lookup. Short on purpose: this runs in the
# background and a slow answer is worth nothing compared to the cached one.
_TIMEOUT = (5, 8)
# Hard cap on the response body. The document is a few hundred bytes; anything
# larger is not the feed and is refused before it is parsed.
_MAX_BODY = 64 * 1024

# A hostname: labels of [a-z0-9-] separated by dots, at least two labels.
# Deliberately strict — no port, no credentials, no path, no underscore.
_HOST_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# Hosts that must never come out of the feed, whatever it says: they would
# point the scraper (and the image proxy, which derives its allowlist from the
# same value) at the machine MediaForge itself runs on.
_FORBIDDEN_SUFFIXES = (
    ".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".test",
    ".invalid", ".example", ".onion",
)
_FORBIDDEN_HOSTS = {"localhost", "localhost.localdomain"}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_lock = threading.Lock()
# site id -> {"host": str, "last_check": str, "ts": float, "loaded": bool}
_state = {}
# Timestamp of the last completed lookup attempt (successful or not) and
# whether it failed, so a dead endpoint backs off instead of retrying per call.
_last_attempt = 0.0
_last_failed = False
# True while a refresh is in flight. Doubles as the re-entrancy guard: the
# refresh's own HTTP call re-enters resolved_host() through mirrors.py.
_refreshing = False
_enabled_cache = {"ts": 0.0, "value": None}
_ENABLED_TTL = 30.0


def _feed_url():
    """The feed URL, honouring the ``MEDIAFORGE_DOMAINS_URL`` override.

    A non-https override is rejected and the default is used instead: the
    answer steers where scraper traffic goes, so plain http (tamperable by
    anyone on the path) is not an acceptable transport for it.
    """
    raw = (os.environ.get("MEDIAFORGE_DOMAINS_URL") or "").strip()
    if not raw:
        return DEFAULT_DOMAINS_URL
    if not raw.lower().startswith("https://"):
        logger.warning(
            "[Domains] Ignoring MEDIAFORGE_DOMAINS_URL=%r — only https is accepted", raw,
        )
        return DEFAULT_DOMAINS_URL
    return raw


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _valid_host(raw, token):
    """Normalize *raw* (a hostname or full URL) and accept it only if it is a
    plausible public hostname for the site identified by *token*.

    Returns the bare lowercase hostname, or ``""`` when the value must not be
    used. Everything the feed says passes through here first.
    """
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https"):
            return ""
        # urlsplit already strips credentials/port off .hostname
        value = parts.hostname or ""
    value = value.split("/")[0].split("@")[-1].split(":")[0].strip().rstrip(".")
    if not value or len(value) > 253:
        return ""
    if value in _FORBIDDEN_HOSTS or any(value.endswith(s) for s in _FORBIDDEN_SUFFIXES):
        return ""
    if _IPV4_RE.match(value):
        # A bare IP has no certificate and no vhost of its own; bare-IP
        # fallbacks are a hand-curated mirrors.py concern, not a feed one.
        return ""
    if not _HOST_RE.match(value):
        return ""
    # The site must still be the site. "megakino17.com" and "www.megakino.to"
    # pass; "evil.example" does not.
    if token and token not in value:
        return ""
    return value


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _db():
    """(get_setting, set_setting) or (None, None) when there is no DB.

    The resolver also runs from the CLI and from unit tests, where the web
    database is never initialised — persistence is then simply skipped and the
    in-memory cache does the job for the life of the process.
    """
    try:
        from .web.db import get_setting, set_setting
    except Exception:  # pragma: no cover - DB not available
        return None, None
    return get_setting, set_setting


def is_enabled():
    """Whether the automatic lookup is switched on (Settings -> Sources).

    ``MEDIAFORGE_NO_DOMAIN_LOOKUP=1`` disables it outright, ahead of the
    setting and of the DB. That is the switch for an installation that must not
    talk to the feed at all -- and the one the test suite sets, next to
    MEDIAFORGE_NO_UPDATE_CHECK: without it the background refresh fires a real
    GLOBAL_SESSION request from a daemon thread and eats a response queued by a
    test that stubbed the session for its own purposes.
    """
    if (os.environ.get("MEDIAFORGE_NO_DOMAIN_LOOKUP") or "").strip() == "1":
        return False
    now = time.time()
    with _lock:
        if _enabled_cache["value"] is not None and now - _enabled_cache["ts"] < _ENABLED_TTL:
            return _enabled_cache["value"]
    get_setting, _ = _db()
    value = True
    if get_setting is not None:
        try:
            value = (get_setting(ENABLED_SETTING, "1") or "1") != "0"
        except Exception:
            value = True
    with _lock:
        _enabled_cache.update({"ts": now, "value": value})
    return value


def set_enabled(enabled):
    """Persist the on/off switch and drop the caches that depend on it."""
    _, set_setting = _db()
    if set_setting is not None:
        set_setting(ENABLED_SETTING, "1" if enabled else "0")
    with _lock:
        _enabled_cache.update({"ts": 0.0, "value": None})
    _invalidate_mirrors()


def _load_persisted(site):
    """Seed a site's in-memory entry from ``app_settings`` (once per process)."""
    get_setting, _ = _db()
    host = ""
    last_check = ""
    if get_setting is not None:
        try:
            _, token = RESOLVABLE_SITES.get(site, (site, ""))
            host = _valid_host(get_setting(_HOST_SETTING % site, ""), token)
            last_check = str(get_setting(_CHECK_SETTING % site, "") or "")
        except Exception:
            host, last_check = "", ""
    with _lock:
        entry = _state.setdefault(site, {})
        entry.setdefault("host", host)
        entry.setdefault("last_check", last_check)
        entry.setdefault("ts", 0.0)
        entry["loaded"] = True


def _persist(site, host, last_check):
    _, set_setting = _db()
    if set_setting is None:
        return
    try:
        set_setting(_HOST_SETTING % site, host)
        set_setting(_CHECK_SETTING % site, last_check or "")
    except Exception:
        logger.debug("[Domains] Could not persist the resolved domain for %s", site, exc_info=True)


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------
def resolved_host(site, wait=0.0):
    """The live domain for *site*, or ``""`` when there is none.

    With the default ``wait=0`` this never blocks and never raises: it reads
    the cache, schedules a background refresh when the cache is stale, and
    hands back whatever is known right now. That is the contract
    ``mirrors._load_mirrors()`` depends on -- it runs on the HTTP egress path,
    where a network round-trip would sit in front of every scraper request.

    ``wait`` > 0 blocks for up to that many seconds, but ONLY when nothing is
    known yet (no cached value, no persisted one). MegaKino ships no domain at
    all, so on a fresh install the very first page request would otherwise have
    nowhere to go while the background lookup is still in flight. Once any
    value exists this returns immediately again and the refresh stays in the
    background, so the wait is paid at most once per installation.
    """
    if site not in RESOLVABLE_SITES:
        return ""
    if not is_enabled():
        return ""
    with _lock:
        entry = _state.get(site)
        loaded = bool(entry and entry.get("loaded"))
    if not loaded:
        _load_persisted(site)
    _maybe_refresh()
    with _lock:
        host = (_state.get(site) or {}).get("host", "") or ""
    if host or not wait:
        return host

    deadline = time.monotonic() + float(wait)
    while time.monotonic() < deadline:
        with _lock:
            host = (_state.get(site) or {}).get("host", "") or ""
            running = _refreshing
        if host:
            return host
        if not running:
            break  # the lookup finished without producing anything -- don't spin
        time.sleep(0.1)
    with _lock:
        return (_state.get(site) or {}).get("host", "") or ""


def resolved_info(site):
    """Diagnostic snapshot for one site (used by the settings API)."""
    if site not in RESOLVABLE_SITES:
        return {}
    host = resolved_host(site)
    with _lock:
        entry = dict(_state.get(site) or {})
        stale = _last_failed
    return {
        "host": host,
        "last_check": entry.get("last_check", "") or "",
        "enabled": is_enabled(),
        "degraded": bool(stale and host),
        "source": _feed_url(),
    }


def resolvable_sites():
    """The mirror site ids this module may override."""
    return {site for site, _ in RESOLVABLE_SITES.values()}


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------
def _invalidate_mirrors():
    try:
        from .mirrors import invalidate_cache
        invalidate_cache()
    except Exception:
        pass


def _maybe_refresh():
    """Start a background refresh when the cache is stale. Non-blocking."""
    global _refreshing

    now = time.time()
    with _lock:
        if _refreshing:
            return
        ttl = _ERROR_BACKOFF if _last_failed else _TTL
        if _last_attempt and now - _last_attempt < ttl:
            return
        _refreshing = True
    try:
        threading.Thread(target=_refresh_worker, name="domain-resolver", daemon=True).start()
    except Exception:
        # Thread creation can fail on an exhausted interpreter (shutdown, or a
        # hard thread limit). Release the flag, otherwise the guard stays set
        # forever and no refresh ever runs again.
        with _lock:
            _refreshing = False
        logger.debug("[Domains] Could not start the refresh thread", exc_info=True)


def _refresh_worker():
    global _refreshing

    try:
        refresh(force=True)
    except Exception:
        logger.debug("[Domains] Background refresh failed", exc_info=True)
    finally:
        with _lock:
            _refreshing = False


def refresh(force=False):
    """Fetch the feed and apply it. Blocking — call it from a worker thread
    (or from the settings route, where the user asked for it explicitly).

    Returns the number of sites whose domain changed.
    """
    global _last_attempt, _last_failed

    # force= skips the user's setting (the settings route uses it right after
    # the switch was flipped on, before the new value is readable), but never
    # the hard environment kill-switch.
    if (os.environ.get("MEDIAFORGE_NO_DOMAIN_LOOKUP") or "").strip() == "1":
        return 0
    if not force and not is_enabled():
        return 0

    url = _feed_url()
    payload = None
    try:
        # GLOBAL_SESSION, not a bare session: it carries the project's DoH
        # resolver and its system-resolver fallback, which is exactly what a
        # user with a filtering ISP needs here too. The feed host is itself in
        # the mirror table (mirrors.INFRA_MIRRORS), so a blocked/dead primary
        # falls back to the previous domain -- the feed cannot be the one thing
        # that has no fallback of its own. Reading that table re-enters this
        # module; _refreshing makes that a plain cache read, and the infra site
        # is not in RESOLVABLE_SITES, so resolved_host() answers "" for it
        # without any lookup at all.
        from .config import GLOBAL_SESSION
        # stream=True so an endpoint answering with a multi-gigabyte body cannot
        # make this thread allocate it: the size is checked while reading, and
        # the read stops at the cap. A Content-Length that already exceeds the
        # cap is rejected before a single chunk is pulled.
        response = GLOBAL_SESSION.get(
            url, timeout=_TIMEOUT, allow_redirects=True, stream=True,
        )
        try:
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > _MAX_BODY:
                raise ValueError(f"response too large ({declared} bytes)")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > _MAX_BODY:
                    raise ValueError(f"response too large (> {_MAX_BODY} bytes)")
        finally:
            try:
                response.close()
            except Exception:
                pass
        payload = json.loads(bytes(body).decode("utf-8", "replace"))
    except Exception as exc:
        with _lock:
            _last_attempt = time.time()
            _last_failed = True
        logger.warning(
            "[Domains] Could not refresh the domain list from %s (%s) — keeping "
            "the last known domains", url, exc,
        )
        return 0

    if not isinstance(payload, dict):
        with _lock:
            _last_attempt = time.time()
            _last_failed = True
        logger.warning("[Domains] %s did not return a JSON object — ignored", url)
        return 0

    changed = 0
    for key, (site, token) in RESOLVABLE_SITES.items():
        record = payload.get(key)
        if not isinstance(record, dict):
            continue
        host = _valid_host(record.get("final_url"), token)
        if not host:
            logger.warning(
                "[Domains] %s reported an unusable domain for %r (%r) — ignored",
                url, key, record.get("final_url"),
            )
            continue
        last_check = str(record.get("last_check") or "")[:64]
        with _lock:
            entry = _state.setdefault(site, {"host": "", "last_check": "", "ts": 0.0, "loaded": True})
            previous = entry.get("host", "")
            entry.update({"host": host, "last_check": last_check, "ts": time.time(), "loaded": True})
        if previous != host:
            changed += 1
            logger.info(
                "[Domains] %s domain is now %s (was %s)",
                site, host, previous or "the shipped default",
            )
        _persist(site, host, last_check)

    with _lock:
        _last_attempt = time.time()
        _last_failed = False

    if changed:
        # The mirror lists are built from these values; drop their 30s cache so
        # the new domain is used on the very next request instead of up to 30s
        # later, and reset the sticky active-mirror index with it.
        _invalidate_mirrors()
    return changed


def prime():
    """Kick off the first lookup at startup, without blocking the boot.

    Called from ``web/app.py``. Safe to call more than once — the TTL/backoff
    check in :func:`_maybe_refresh` collapses repeat calls.
    """
    if not is_enabled():
        return
    for site in resolvable_sites():
        _load_persisted(site)
    _maybe_refresh()
