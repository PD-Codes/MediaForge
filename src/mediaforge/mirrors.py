"""Site mirror registry and transparent domain failover.

Some of the scraper sites MediaForge talks to (s.to / serienstream.to,
aniworld.to, filmpalast.to, megakino.to, ...) regularly go dark on one of their
domains -- an ISP/CUII DNS block, a Cloudflare/DDoS-Guard hiccup, or a
domain move -- while the very same content is still reachable through a
mirror domain or, in the worst case, the bare origin IP.

This module keeps, per site, an ordered list of interchangeable hosts::

    sto: s.to  ->  serienstream.to  ->  186.2.175.5

and rewrites the *host part* of outgoing requests to whichever mirror is
currently healthy. Everything else in the app keeps using the canonical
URLs (aniworld.to/..., s.to/...) it always did: the URL patterns in
config.py, the DB rows, the queue, autosync and the frontend never see a
mirror host, so no other code has to learn about mirrors at all. Only the
actual HTTP egress is redirected.

Wiring:

* ``config._SessionProxy`` (i.e. ``GLOBAL_SESSION``) routes every ``get``/
  ``post``/``request`` call through :func:`request_with_failover`, which
  walks the mirror list until one host answers and remembers the winner
  (:func:`mark_ok` / :func:`mark_failed`).
* ``web/routes/settings.py`` exposes the lists for editing; the user's
  version is persisted in ``app_settings`` under ``site_mirrors_<site>``.
* Bare-IP mirrors are sent with an explicit ``Host:`` header (the canonical
  domain, so the origin's vhost routing still matches) and with TLS
  verification off, since the origin certificate never covers the raw IP.

The active-mirror choice is in-memory only and resets to the primary host
after ``_PRIMARY_RETRY_AFTER`` seconds, so a temporary outage never pins
the app to a fallback host forever.

Rotating domains
----------------
A hardcoded list only helps against domains that are *known*. MegaKino does
not rotate between known domains, it mints new ones (megakino.to ->
megakino14.com -> megakino16.com -> ...), so no shipped list can ever contain
the current one. For those sites :mod:`mediaforge.domain_resolver` looks the
live domain up from a first-party feed and this module pulls it to the FRONT
of that site's list -- it becomes the canonical host, the shipped domains stay
behind it as fallbacks. Everything downstream is unchanged: a favourite, queue
row or history entry stored against the old domain still maps to the site
(the old host is still in the list) and is rewritten to the live one on egress.
"""

import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from .logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
# Per site: the ordered host list. The FIRST entry is the canonical host --
# the one every URL inside MediaForge is written with, and the one sent as
# the Host header when falling back to a bare-IP mirror.
DEFAULT_SITE_MIRRORS = {
    "aniworld":   ["aniworld.to", "aniworld.cc", "186.2.175.111"],
    "sto":        ["s.to", "serienstream.to", "186.2.175.5"],
    "filmpalast": ["filmpalast.to"],
    # EMPTY on purpose -- MegaKino is the one site with no hardcoded domain
    # anywhere in this codebase. It mints a new numbered domain whenever the
    # current one is blocked (megakino.to -> megakino14.com -> megakino16.com
    # -> ...), so any name written here is wrong by the next rotation and
    # becomes a second source of truth to keep in sync with the feed. The
    # domain comes from domain_resolver.py, full stop; the only thing a user
    # can add here is a mirror of their own, which then acts as the fallback.
    "megakino":   [],
    # One entry each: no verified alternate domain is known for these three
    # yet. They are registered anyway so the failover machinery, the DNS
    # pinning fed from all_hosts() (config._chromium_map_hosts) and the
    # Settings -> Mirrors editor treat them like every other site -- a user
    # who finds a working mirror can add it in the UI without a code change.
    "filmo":      ["filmo.to"],
    "nineanime":  ["9anime.or.at"],
    "aniwaves":   ["aniwaves.ru"],
    "hanime":     ["hanime.tv"],
}

# Human-readable labels for the settings UI.
SITE_LABELS = {
    "aniworld":   "AniWorld",
    "sto":        "S.TO / SerienStream",
    "filmpalast": "FilmPalast",
    "megakino":   "MegaKino",
    "filmo":      "filmo.to",
    "nineanime":  "9anime",
    "aniwaves":   "Aniwaves",
    "hanime":     "hanime",
}

# How long a non-primary mirror stays active before the primary host is
# retried again (seconds).
_PRIMARY_RETRY_AFTER = 600

# Lower bound for a single mirror attempt when a total probe budget is split
# across the mirror list (see request_with_failover(budget=...)). Ensures every
# mirror still gets a usable timeout even when a small budget is shared by many
# mirrors, instead of shrinking to a fraction of a second.
_MIN_ATTEMPT_TIMEOUT = 3.0

# Cap for the connect phase of a budget-split attempt; the (usually slower)
# read phase may use the full per-attempt slice.
_CONNECT_TIMEOUT_CAP = 8.0

# HTTP statuses that mean "this host is not serving the site right now" and
# are therefore worth retrying on the next mirror rather than handing back
# to the caller.
_FAILOVER_STATUSES = frozenset({403, 421, 451, 500, 502, 503, 504, 520, 521, 522, 523, 525, 530})

# Statuses that mean "the origin hiccuped", as opposed to "this host is not
# serving the site". Moving to the next mirror is the right answer for both --
# but half the sites have exactly ONE known host (filmo.to, 9anime.or.at,
# aniwaves.ru, filmpalast.to, hanime.tv), and for those "next mirror" does not
# exist, so a single transient 502 failed the whole request outright. One
# retry against the same host covers that without pretending a mirror exists.
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504, 520, 521, 522, 523, 525})
# Only ever retried for methods that can be repeated without side effects. A
# POST is not: filmo.to's /n mints a one-shot token, and sending it twice would
# burn the payload.
_RETRYABLE_METHODS = frozenset({"GET", "HEAD"})
_LAST_HOST_RETRY_DELAY = 1.5

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

_state_lock = threading.Lock()
# site -> {"idx": <active mirror index>, "ts": <when idx was last changed>}
_active = {}

# Cache of the merged (DB-override or default) mirror lists, so a request
# doesn't hit the settings table on every single call.
_cache_lock = threading.Lock()
_cache = {"ts": 0.0, "mirrors": None, "host_map": None}
_CACHE_TTL = 30.0


def _is_ip_host(host):
    return bool(_IPV4_RE.match((host or "").split(":")[0]))


def _clean_host(raw):
    """Normalize a user-entered mirror entry to a bare ``host[:port]``."""
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if "://" in value:
        value = urlsplit(value).netloc or value.split("://", 1)[1]
    value = value.split("/")[0].strip()
    return value


def _resolved_host(site):
    """The live domain the remote domain feed reports for *site*, or ``""``.

    Non-blocking by contract (see domain_resolver.resolved_host) — this runs
    inside the mirror-table build, i.e. on the HTTP egress path, so it must
    never make a network call of its own. Wrapped defensively because a
    failure here has to degrade to "use the shipped domains", never to an
    exception on every outgoing request.
    """
    try:
        from .domain_resolver import resolved_host
        return resolved_host(site)
    except Exception:  # pragma: no cover - defensive
        return ""


def _load_mirrors():
    """Merged mirror lists: DB override per site, else the shipped default,
    with the remotely resolved domain (if any) pulled to the front."""
    try:
        from .web.db import get_setting
    except Exception:  # pragma: no cover - DB not available (e.g. CLI import)
        get_setting = None

    mirrors = {}
    for site, default in DEFAULT_SITE_MIRRORS.items():
        hosts = None
        if get_setting is not None:
            try:
                raw = get_setting("site_mirrors_" + site, "")
            except Exception:
                raw = ""
            if raw:
                hosts = [h for h in (_clean_host(p) for p in raw.split(",")) if h]
        if not hosts:
            hosts = list(default)
        # The canonical host must always stay first and present -- it is what
        # every URL in the app is written with. For a site whose domain rotates
        # (MegaKino), that canonical host is whatever the domain feed currently
        # reports, and the shipped list is empty, so the feed is the only
        # source; anything the user added follows behind it as a fallback.
        #
        # default may legitimately be empty (see DEFAULT_SITE_MIRRORS), in which
        # case the list stays empty until the feed answers -- callers treat an
        # empty list as "no domain known yet" rather than crashing on it.
        canonical = _resolved_host(site) or (default[0] if default else "")
        ordered = [canonical]
        if default:
            ordered.append(default[0])
        ordered.extend(hosts)
        mirrors[site] = list(dict.fromkeys(h for h in ordered if h))
    return mirrors


def _get_tables():
    """(mirrors, host_map) with a short TTL cache. host_map: host -> site."""
    now = time.time()
    with _cache_lock:
        if _cache["mirrors"] is not None and now - _cache["ts"] < _CACHE_TTL:
            return _cache["mirrors"], _cache["host_map"]

    mirrors = _load_mirrors()
    host_map = {}
    for site, hosts in mirrors.items():
        for host in hosts:
            host_map[host] = site
            if not _is_ip_host(host):
                host_map["www." + host] = site

    with _cache_lock:
        _cache.update({"ts": now, "mirrors": mirrors, "host_map": host_map})
    return mirrors, host_map


def invalidate_cache():
    """Drop the cached mirror lists — called after the settings are saved."""
    with _cache_lock:
        _cache.update({"ts": 0.0, "mirrors": None, "host_map": None})
    with _state_lock:
        _active.clear()


# ---------------------------------------------------------------------------
# Third-party site mirrors
# ---------------------------------------------------------------------------
_EXTRA_SITES = {}  # item_id -> site_id


def register_site_mirrors(item_id, site_id, hosts, label=None) -> None:
    """Register a mirror/fallback domain list for a third-party content
    source, from the module's own ``register(app)``::

        register_provider("kinox_mod", Provider(name="Kinox", ...))
        register_site_mirrors("kinox_mod", "kinox",
                               ["kinox.to", "kinox.cx", "kinox.am"], label="Kinox")

    *site_id* becomes a key in :data:`DEFAULT_SITE_MIRRORS` / :data:`SITE_LABELS`
    -- the exact same dicts the five built-in sites (aniworld/sto/filmpalast/
    megakino/hanime) live in. Everything downstream of those two dicts is
    already generic (see the module docstring above), so this one call gets
    you, for free, no template/route/JS changes:

    - A mirror-editing card under Settings -> Sources -> "Domain fallback
      (mirrors)", right alongside the built-in sites -- ``web/routes/
      settings.py``'s settings API and ``web/static/settings.js``'s
      ``_loadMirrorSettings()`` both already iterate
      ``mirrors.DEFAULT_SITE_MIRRORS`` generically, they don't special-case
      the five built-in ids.
    - Persistence of the user's edits under
      ``app_settings["site_mirrors_<site_id>"]`` (same route as above).
    - Transparent host failover for outgoing requests -- **but only for
      requests your module makes through ``mediaforge.config.GLOBAL_SESSION``**
      (`config._SessionProxy` is what's wired to
      :func:`request_with_failover`); a bare `requests`/`niquests` session of
      your own never sees a mirror, same as it never saw DoH DNS either.

    ``hosts[0]`` becomes the canonical host -- the one your ``Provider``'s URL
    patterns (see ``providers.register_provider``) should be written against.
    The rest are fallback hosts, in priority order; a bare IP is allowed for
    the same last-resort reason the built-in sites use one (see the module
    docstring). This is exactly the seed list a user can later edit under
    Settings, same as any built-in site's.

    Raises ``ValueError`` if *site_id* is already registered (built-in or
    another module) or *hosts* is empty after cleaning. Removed automatically
    on disable/uninstall via :func:`unregister_site_mirrors` (see
    ``web/thirdparties/registry.py``'s ``unregister_module()``).
    """
    if site_id in DEFAULT_SITE_MIRRORS:
        raise ValueError(f"register_site_mirrors: site id already registered: {site_id!r}")
    cleaned = [h for h in (_clean_host(h) for h in hosts) if h]
    if not cleaned:
        raise ValueError("register_site_mirrors: hosts must contain at least one host")
    DEFAULT_SITE_MIRRORS[site_id] = cleaned
    SITE_LABELS[site_id] = label or site_id
    _EXTRA_SITES[item_id] = site_id
    invalidate_cache()
    logger.info("[Mirrors] Registered third-party site mirrors: %s (%s)", site_id, item_id)


def unregister_site_mirrors(item_id) -> None:
    """Drop a mirror list previously added via :func:`register_site_mirrors`.

    Leaves any saved ``app_settings["site_mirrors_<site_id>"]`` row in place
    -- it is a flat, un-namespaced key (see the module README's "Settings
    namespacing" section for why that matters for a module's *own* settings);
    here it is simply inert once the site id is gone, the same way any
    setting for a since-removed feature is.
    """
    site_id = _EXTRA_SITES.pop(item_id, None)
    if site_id is None:
        return
    DEFAULT_SITE_MIRRORS.pop(site_id, None)
    SITE_LABELS.pop(site_id, None)
    invalidate_cache()
    logger.info("[Mirrors] Unregistered third-party site mirrors: %s", site_id)


def thirdparty_mirror_ids() -> set:
    """item_ids that currently own a third-party mirror list.

    Read-only counterpart of :func:`unregister_site_mirrors` -- the
    Modulmanager uses it to report what a module registered without reaching
    into this module's private dict.
    """
    return set(_EXTRA_SITES)


def get_mirrors(site=None):
    """All mirror lists, or the list for one site."""
    mirrors, _ = _get_tables()
    if site is None:
        return {k: list(v) for k, v in mirrors.items()}
    return list(mirrors.get(site, []))


def all_hosts():
    """Every host of every site — used to pin the captcha browser's DNS."""
    mirrors, _ = _get_tables()
    hosts = []
    for site_hosts in mirrors.values():
        for host in site_hosts:
            if _is_ip_host(host):
                continue
            hosts.append(host)
            hosts.append("www." + host)
    return tuple(dict.fromkeys(hosts))


def site_for_host(host):
    """The site key a hostname belongs to, or None if it isn't one of ours."""
    if not host:
        return None
    _, host_map = _get_tables()
    return host_map.get(host.split(":")[0].lower())


def site_for_url(url):
    try:
        return site_for_host(urlsplit(url).hostname or "")
    except Exception:
        return None


def canonical_host(site):
    """The host every URL for *site* is currently written with.

    That is the domain the remote feed resolved for the site when there is one
    (MegaKino rotates its domain, see domain_resolver.py), else the shipped
    canonical from :data:`DEFAULT_SITE_MIRRORS`. Also the ``Host:`` header sent
    when falling back to a bare-IP mirror.
    """
    return _resolved_host(site) or shipped_canonical_host(site)


def shipped_canonical_host(site):
    """The hardcoded canonical host, ignoring the remote domain feed.

    Used by the Settings -> Mirrors editor: the user's saved fallback list must
    be anchored to the stable shipped domain, not to whatever the feed happens
    to report at save time (which would freeze a rotating domain into the
    saved list and go stale on the next rotation).

    ``""`` for a site that ships no domain at all (MegaKino) -- there the feed
    is the only source and there is nothing to anchor to.
    """
    hosts = DEFAULT_SITE_MIRRORS.get(site) or []
    return hosts[0] if hosts else ""


def _active_index(site, count):
    """Current mirror index for *site*, auto-resetting to the primary once
    _PRIMARY_RETRY_AFTER has passed since the last failover."""
    now = time.time()
    with _state_lock:
        st = _active.get(site)
        if not st:
            return 0
        if st["idx"] and now - st["ts"] > _PRIMARY_RETRY_AFTER:
            _active.pop(site, None)
            logger.debug("[Mirrors] %s: retrying primary host again", site)
            return 0
        return min(st["idx"], max(count - 1, 0))


def mark_ok(site, index):
    """Remember the mirror that just answered successfully."""
    with _state_lock:
        st = _active.get(site)
        if index == 0:
            if st:
                _active.pop(site, None)
            return
        if not st or st["idx"] != index:
            _active[site] = {"idx": index, "ts": time.time()}


def mark_failed(site, index):
    """Record that mirror *index* of *site* just failed."""
    with _state_lock:
        _active[site] = {"idx": index + 1, "ts": time.time()}


def _pins_to_primary(site):
    """True for a site whose primary host is resolved remotely.

    Such a site must NOT stick to a fallback after one bad answer. Its
    fallbacks are the domains it already abandoned (megakino.to, .tv, .org --
    dead, parked or seized), so a single Cloudflare challenge on the live
    domain would otherwise pin every request for the next ten minutes to a host
    that can only ever answer with a parking page. The live domain is the
    authoritative one here, and the feed -- not a failed request -- is what
    decides when it changes.
    """
    return bool(_resolved_host(site))


def active_host(site):
    """The host currently used for *site* (for status/debug output)."""
    hosts = get_mirrors(site)
    if not hosts:
        return ""
    if _pins_to_primary(site):
        return hosts[0]
    return hosts[_active_index(site, len(hosts))]


def map_url(url, host):
    """Rewrite *url*'s host part to *host* (keeping scheme/path/query)."""
    parts = urlsplit(url)
    netloc = host
    if parts.port and ":" not in host:
        netloc = f"{host}:{parts.port}"
    if parts.username:
        cred = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{cred}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def iter_candidates(url, from_primary=False):
    """Yield ``(candidate_url, extra_headers, verify, site, index)`` for *url*,
    starting at the site's currently active mirror and walking the rest of the
    list.

    When *from_primary* is True the walk always starts at the canonical host
    (index 0) instead of the sticky active mirror -- used by the UpTime monitor
    so a check measures the real primary host and never stays pinned to a
    fallback that a transient failure selected earlier. A site whose primary
    host comes from the domain feed behaves that way unconditionally, see
    :func:`_pins_to_primary`.

    For a URL that doesn't belong to any known site, yields exactly one
    candidate: the URL unchanged, with site=None.
    """
    site = site_for_url(url)
    if not site:
        yield url, {}, None, None, 0
        return

    hosts = get_mirrors(site)
    if not hosts:
        yield url, {}, None, None, 0
        return

    if from_primary or _pins_to_primary(site):
        start = 0
    else:
        start = _active_index(site, len(hosts))
    order = list(range(start, len(hosts))) + list(range(0, start))
    canonical = canonical_host(site)

    for idx in order:
        host = hosts[idx]
        headers = {}
        verify = None
        if _is_ip_host(host):
            # A bare origin IP: the vhost still needs the real domain, and the
            # certificate never covers the IP.
            headers["Host"] = canonical
            verify = False
        yield map_url(url, host), headers, verify, site, idx


def request_with_failover(session, method, url, budget=None, probe=False, **kwargs):
    """Perform ``session.request(method, url)`` against the site's mirrors,
    moving on to the next host whenever one is unreachable or answers with a
    "site is not here" status (see ``_FAILOVER_STATUSES``).

    Non-site URLs (TMDB, hosters, DoH endpoints, ...) are passed straight
    through, untouched — this is a no-op for them.

    ``budget`` (seconds, optional): a total wall-clock budget for the *whole*
    failover walk. When set, the budget is divided across the candidate mirrors
    so each still gets a usable per-attempt ``(connect, read)`` timeout (floored
    at ``_MIN_ATTEMPT_TIMEOUT``) while the total time stays bounded. This keeps
    the failover walk fully intact but stops N mirrors from multiplying one
    timeout into N×timeout — a single hung host can no longer stall the caller
    for the full timeout per mirror. A hard deadline ends the walk once the
    budget is spent.

    ``probe`` (optional): when True, always start at the canonical host and do
    NOT mutate the shared active-mirror state (``mark_ok`` / ``mark_failed``).
    Used by the UpTime monitor so its checks measure the real primary host,
    never get pinned to a fallback, and never flip the mirror that real user
    traffic is using.
    """
    site = site_for_url(url)
    if not site:
        return session.request(method, url, **kwargs)

    base_headers = kwargs.pop("headers", None) or {}
    last_exc = None
    last_resp = None
    retried_last = False

    candidates = list(iter_candidates(url, from_primary=probe))

    # Split a total budget into a bounded per-mirror timeout instead of giving
    # every mirror the full timeout. deadline is the hard stop for the walk.
    per_attempt = None
    deadline = None
    if budget:
        per_attempt = max(_MIN_ATTEMPT_TIMEOUT, float(budget) / max(1, len(candidates)))
        deadline = time.monotonic() + float(budget)

    for pos, (cand_url, extra_headers, verify, cand_site, idx) in enumerate(candidates):
        is_last = pos == len(candidates) - 1
        # Budget spent — don't start another attempt (the first one always runs).
        if deadline is not None and pos > 0 and time.monotonic() >= deadline:
            break
        call_kwargs = dict(kwargs)
        if extra_headers:
            call_kwargs["headers"] = {**base_headers, **extra_headers}
        elif base_headers:
            call_kwargs["headers"] = dict(base_headers)
        if verify is not None and "verify" not in call_kwargs:
            call_kwargs["verify"] = verify
        if per_attempt is not None:
            call_kwargs["timeout"] = (min(_CONNECT_TIMEOUT_CAP, per_attempt), per_attempt)

        try:
            resp = session.request(method, cand_url, **call_kwargs)
        except Exception as exc:  # network-level failure -> next mirror
            last_exc = exc
            if is_last:
                break
            logger.debug(
                "[Mirrors] %s: host %s unreachable (%s) — trying next mirror",
                cand_site, urlsplit(cand_url).hostname, exc,
            )
            if not probe:
                mark_failed(cand_site, idx)
            continue

        if resp.status_code in _FAILOVER_STATUSES and not is_last:
            logger.debug(
                "[Mirrors] %s: host %s answered HTTP %s — trying next mirror",
                cand_site, urlsplit(cand_url).hostname, resp.status_code,
            )
            last_resp = resp
            if not probe:
                mark_failed(cand_site, idx)
            continue

        # Last (often only) host, and the failure looks transient: give it one
        # more go rather than failing the caller. Skipped for probes -- the
        # UpTime monitor has to report what the site actually answered, not a
        # retried best-of.
        if (is_last and not probe and not retried_last
                and resp.status_code in _TRANSIENT_STATUSES
                and str(method).upper() in _RETRYABLE_METHODS):
            retried_last = True
            logger.debug(
                "[Mirrors] %s: host %s answered HTTP %s and has no further mirror "
                "— retrying once in %.1fs",
                cand_site, urlsplit(cand_url).hostname, resp.status_code,
                _LAST_HOST_RETRY_DELAY,
            )
            time.sleep(_LAST_HOST_RETRY_DELAY)
            try:
                resp = session.request(method, cand_url, **call_kwargs)
            except Exception as exc:
                last_exc = exc
                last_resp = None
                continue
            if resp.status_code in _FAILOVER_STATUSES:
                last_resp = resp
                continue

        if not probe:
            mark_ok(cand_site, idx)
        return resp

    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    # Unreachable in practice (candidates is never empty), but keep it safe.
    return session.request(method, url, headers=base_headers or None, **kwargs)
