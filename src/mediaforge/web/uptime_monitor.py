"""Source-site monitoring — shared probe (DNS test + UpTime) and the UpTime monitor.

Telemetry: flag.uptime_monitor ("the built-in uptime monitoring is active")
is submitted from _uptime_run_round(), throttled to at most once per 24 h per
process. The monitor polls every few minutes by design, and a flag event
means "the feature is used", not "the loop turned again" -- an unthrottled
submit would drown every other data point in the batch.
"""

import threading
import time

from ..logger import get_logger
from ..telemetry import client as telemetry_client
from ..telemetry import events as telemetry_events
from .db import get_setting, prune_uptime_heartbeats, record_uptime_heartbeat, set_setting
from .dns_patch import _ip_provider
from .source_policy import setting_is_on, source_enabled_default

logger = get_logger(__name__)


# ── Source-site monitoring (shared by DNS test + UpTime) ──────────────────────
# Ordered mapping of trackable source sites. Keys match the source ids used by
# the ``source_enabled_<id>`` settings and the UpTime per-source tracking toggles.
#   id -> (label, url, expected_domain, body_markers, expected_headers)
#
# expected_headers is the reachability/identity signature: a dict of
# response-header-name -> substring that must appear in it (case-insensitive).
# Verified empirically per site (curl -I) rather than assumed — aniworld.to and
# serienstream.to sit behind DDoS-Guard ("server: ddos-guard"), while
# filmpalast.to, megakino.to and hanime.tv sit behind Cloudflare
# ("server: cloudflare", plus a cf-ray id on every response). This is checked
# instead of the resolved IP because both CDNs rotate their edge IPs
# constantly (anycast across many PoPs) — the header signature is what stays
# stable, not the address.
_MONITOR_SITES = {
    "aniworld":   ("AniWorld",     "https://aniworld.to",     "aniworld.to",     ["aniworld"],     {"server": "ddos-guard"}),
    "sto":        ("SerienStream", "https://serienstream.to", "serienstream.to", ["serienstream"], {"server": "ddos-guard"}),
    "filmpalast": ("FilmPalast",   "https://filmpalast.to",   "filmpalast.to",   ["filmpalast"],   {"server": "cloudflare"}),
    "megakino":   ("MegaKino",     "https://megakino.to",     "megakino.to",     ["megakino"],     {"server": "cloudflare"}),
    "hanime":     ("hanime",       "https://hanime.tv",       "hanime.tv",       ["hanime"],       {"server": "cloudflare"}),
}

# Signatures of ISP / CUII (Clearingstelle Urheberrecht im Internet) block pages
# and generic legal-block interstitials. If any appears in the body we must NOT
# report the site as verified even when the block page names the brand/domain.
_BLOCK_MARKERS = [
    # High-precision full phrases from real ISP / CUII block interstitials.
    # Deliberately NOT short substrings (e.g. "gvu", "cuii") — those match by
    # chance inside minified JS / base64 on a real homepage and caused false
    # "blocked" reports for aniworld.to and serienstream.to.
    "clearingstelle urheberrecht im internet",
    "cuii.info",
    "der zugang zu der von ihnen aufgerufenen",
    "der zugriff auf diese website wurde",
    "aus urheberrechtlichen gründen gesperrt",
    "aufgrund einer urheberrechtlichen",
    "diese website wurde gesperrt",
    "diese domain wurde aus rechtlichen",
    "access to this website has been blocked",
    "this website has been blocked",
    "has been blocked pursuant to",
    "blocked in accordance with",
    "site blocked by court order",
]


def _resolve_ip(hostname, timeout):
    """Resolve *hostname* to an edge IP for display only, bounded by *timeout*.

    ``socket.getaddrinfo`` takes no timeout argument and can block on the OS
    resolver far longer than the probe's own budget — and this lookup is purely
    informational (the resolved edge IP shown in the DNS diagnostics UI), it
    plays no part in reachability verification. Running it in a short-lived
    daemon thread and giving up after *timeout* seconds means a slow or
    unresponsive system resolver can never stall a probe (which previously
    surfaced as the monitor "hanging" and then wrongly reporting a site down).

    Returns ``(ip, provider, error)`` — ip/provider are None on timeout/failure.
    """
    import socket as _sock

    result = {}

    def _worker():
        try:
            infos = _sock.getaddrinfo(hostname, 443, proto=_sock.IPPROTO_TCP)
            result["ip"] = infos[0][4][0] if infos else None
        except Exception as e:  # DNS failure -> reported as socket_error
            result["err"] = str(e)

    t = threading.Thread(target=_worker, daemon=True, name="uptime-dns")
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, None, "dns_timeout"
    ip = result.get("ip")
    if ip:
        return ip, _ip_provider(ip), None
    return None, None, result.get("err")


def _probe_site(url, expected_domain, markers, expected_headers=None, timeout=10,
                use_get=False, probe=True, resolve_ip=True):
    """Fetch a site and verify we reached the real thing via response headers.

    Verification is header-based, not IP-based. Cloudflare and DDoS-Guard (the
    two CDNs fronting these sites) both rotate their edge IPs constantly
    (anycast, load-balanced across many PoPs) — a resolved IP tells us almost
    nothing reliable about whether we actually reached the genuine site. What
    stays stable is the CDN fingerprint in the response headers (e.g.
    ``server: cloudflare`` or ``server: ddos-guard``, see ``expected_headers``
    on ``_MONITOR_SITES``) — an ISP block page or DNS hijack is very unlikely
    to reproduce that exact signature.

    A cheap HEAD request (headers only, no body download) is used first and is
    the *only* request made in the common case. The body fallback (a full GET,
    so the ISP/CUII block-page markers can be checked) runs whenever the header
    signature does *not* match **or** the status is 4xx. The 4xx half matters:
    a Cloudflare block/challenge page (403, error 1020) still carries
    ``server: cloudflare``, so a header-only verdict would have matched the
    signature and reported a blocked site as "up" -- which is precisely the
    thing this monitor exists to notice. Only a response that both matches the
    signature *and* is below 400 counts as verified outright.

    *resolve_ip* controls the informational DNS lookup. The UpTime loop passes
    ``False``: it never reads ``ip``/``ip_provider`` (only the DNS diagnostics
    UI does), and the lookup costs up to *timeout* seconds per site per round.

    Returns a dict with: hostname, http_status, http_ok, site_verified,
    headers_matched, response_ms, server_header, and optional ip/ip_provider
    (informational only — resolved for display, plays no part in verification)
    plus blocked / socket_error / http_error where applicable. Shared by the
    DNS diagnostics endpoint and the UpTime monitor so both use identical
    checks.

    Used by: web/routes/settings.py (DNS diagnostics test) and
    _uptime_run_round() below.
    """
    import time as _time
    from ..config import GLOBAL_SESSION as _GS

    expected_headers = expected_headers or {}
    hostname = url.replace("https://", "").replace("http://", "").rstrip("/")
    entry = {"hostname": hostname, "ip": None, "socket_ok": False,
             "http_ok": False, "site_verified": False, "headers_matched": False,
             "blocked": False, "response_ms": None}

    # DNS resolve — informational only (shown as the resolved edge IP in the
    # DNS diagnostics UI). NOT used to decide reachability/verification, and
    # bounded by a short timeout so a stalled system resolver can never hang the
    # probe (see _resolve_ip).
    # Skipped entirely for the monitor (resolve_ip=False) -- see the docstring.
    if resolve_ip:
        ip, provider, dns_err = _resolve_ip(hostname, timeout=min(5, timeout))
        if ip:
            entry["ip"] = ip
            entry["socket_ok"] = True
            entry["ip_provider"] = provider
        elif dns_err:
            entry["socket_error"] = dns_err

    def _headers_match(headers):
        if not expected_headers:
            return True  # no signature configured for this site — skip the check
        for key, expect_sub in expected_headers.items():
            actual = (headers.get(key) or "").lower()
            if expect_sub.lower() not in actual:
                return False
        return True

    def _check_body(text):
        # Distinguish a genuine (if differently configured) server from a known
        # ISP/CUII block interstitial. Sets blocked / site_verified on *entry*.
        body_lower = (text or "").lower()
        if any(b in body_lower for b in _BLOCK_MARKERS):
            entry["blocked"] = True
            return
        has_marker = any(m.lower() in body_lower for m in markers)
        url_on_domain = expected_domain in entry.get("final_url", "")
        # "We are still on the right domain" is not on its own evidence that
        # the real site answered when the status is 4xx -- a CDN block page is
        # served from that very domain. A body marker still counts (the real
        # page can legitimately answer 404 on "/"), the URL alone does not.
        if not entry.get("status_ok", True):
            entry["site_verified"] = bool(has_marker)
        else:
            entry["site_verified"] = bool(has_marker or url_on_domain)

    # Primary check, verified via response headers. HEAD is the cheap default;
    # ``use_get`` switches to a full GET, which is more reliable against the
    # Cloudflare / DDoS-Guard front ends (they often answer HEAD with a
    # challenge or hold the connection) at the cost of downloading the body.
    # ``budget=timeout`` bounds the whole mirror-failover walk to *timeout*
    # instead of timeout-per-mirror; ``probe=True`` keeps the monitor off the
    # shared active-mirror state (see mirrors.request_with_failover).
    try:
        _t0 = _time.monotonic()
        if use_get:
            resp = _GS.get(url, allow_redirects=True, timeout=timeout,
                           budget=timeout, probe=probe)
        else:
            resp = _GS.head(url, allow_redirects=True, timeout=timeout,
                            budget=timeout, probe=probe)
        entry["response_ms"] = int((_time.monotonic() - _t0) * 1000)
        entry["http_status"] = resp.status_code
        entry["http_ok"] = resp.status_code < 500
        entry["final_url"] = str(getattr(resp, "url", url) or url)
        entry["server_header"] = resp.headers.get("server")
        entry["headers_matched"] = _headers_match(resp.headers)
        # A 4xx is NOT proof we reached the real site, no matter how well the
        # CDN signature matches -- Cloudflare's own block/challenge page (403,
        # error 1020) answers with server: cloudflare. So the signature only
        # verifies a response that is also below 400; everything from 400 up
        # has to earn its verdict from the body check below.
        entry["status_ok"] = resp.status_code < 400
        entry["site_verified"] = bool(entry["status_ok"] and entry["headers_matched"])

        # Signature didn't match, or the status was 4xx — fall back to the
        # body markers (which is also what detects an ISP/CUII block page).
        if entry["http_ok"] and not entry["site_verified"]:
            if use_get:
                # Body already in hand from the GET — no extra request needed.
                _check_body(resp.text)
            else:
                # HEAD carries no body: do one GET so the block/marker check can
                # run (handles CDN challenge pages), still under the budget.
                _t1 = _time.monotonic()
                full = _GS.get(url, allow_redirects=True, timeout=timeout,
                               budget=timeout, probe=probe)
                entry["response_ms"] += int((_time.monotonic() - _t1) * 1000)
                entry["final_url"] = str(getattr(full, "url", url) or url)
                _check_body(full.text)
    except Exception as e:
        entry["http_error"] = str(e)

    return entry


# ── UpTime monitor ────────────────────────────────────────────────────────────
_uptime_monitor_started = False
_uptime_monitor_lock = threading.Lock()
_uptime_wake = threading.Event()  # set to wake the monitor early (config change)


def _tracked_default(site_id) -> str:
    """Default value of ``uptime_track_<id>`` for a site that never had one set.

    A built-in site follows the app-wide source rule and nothing else (see
    source_policy): opt-out, except an adult source, which is opt-in. There is
    deliberately no ``if site_id == "hanime"`` here any more -- that hardcoded
    copy of the rule is the reason the tracking toggle and the source toggle
    could disagree about the same site.

    A third-party site uses whatever its module asked for at registration
    (``tracked_by_default``), which register_monitor_site() also seeds into the
    DB, so this is only the fallback for a site registered while the DB write
    failed.
    """
    if site_id in _MONITOR_TRACKED_DEFAULTS:
        return _MONITOR_TRACKED_DEFAULTS[site_id]
    return source_enabled_default(site_id)


def _uptime_config():
    """Read the current UpTime configuration from app_settings (clamped).

    Used by: web/routes/uptime.py, _start_uptime_monitor() below.
    """
    def _clamp_int(key, default, lo, hi):
        try:
            v = int(float(get_setting(key, str(default))))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    tracked = {}
    for _sid in list(_MONITOR_SITES):
        tracked[_sid] = setting_is_on(
            get_setting("uptime_track_" + _sid, _tracked_default(_sid)))

    return {
        "enabled":           get_setting("uptime_enabled", "0") == "1",
        "interval":          _clamp_int("uptime_interval", 300, 60, 86400),
        "retention_days":    _clamp_int("uptime_retention_days", 7, 1, 7),
        "timeout":           _clamp_int("uptime_timeout", 15, 5, 120),
        # Consecutive failed checks required before a site flips to "down" — a
        # debounce so a single transient timeout/DNS hiccup can't report an
        # online site as offline (1 = old immediate behaviour).
        "failure_threshold": _clamp_int("uptime_failure_threshold", 2, 1, 10),
        # Verify with a full GET instead of a HEAD (see _probe_site).
        "use_get":           get_setting("uptime_use_get", "0") == "1",
        "tracked":           tracked,
    }


# Per-source count of consecutive reachability failures, for the failure-
# threshold debounce in _uptime_run_round(). In-memory only (resets on
# restart, which at worst grants one extra round of tolerance).
_consec_fail = {}
_consec_fail_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Third-party monitor sites
# ---------------------------------------------------------------------------
_EXTRA_MONITOR_SITES = {}  # item_id -> site_id
# site_id -> the app_settings key that reflects "is this source itself
# enabled" (shown as the enabled_source badge on the site's Uptime card).
# Only set for third-party sites -- a built-in one keeps using the
# "source_enabled_<id>" convention from source_policy.
_MONITOR_ENABLED_KEYS = {}
# site_id -> what an *unset* enabled key means for that third-party site.
# The core cannot know: the key belongs to the module (e.g.
# "kinox_search_enabled"), and a module that only writes it on first save
# would otherwise be reported as "source disabled" forever. Modules say so
# themselves via register_monitor_site(enabled_setting_default=...).
_MONITOR_ENABLED_DEFAULTS = {}
# site_id -> the seeded default of the "track this site" toggle, as the
# registering module asked for it (see _tracked_default).
_MONITOR_TRACKED_DEFAULTS = {}


def register_monitor_site(
    item_id,
    site_id,
    label,
    url,
    expected_domain,
    body_markers,
    expected_headers=None,
    enabled_setting_key=None,
    enabled_setting_default=True,
    tracked_by_default=True,
) -> None:
    """Register a third-party content source for UpTime tracking *and* the
    DNS test (Settings -> Network & Access), from the module's own
    ``register(app)``::

        register_monitor_site(
            "kinox_mod", "kinox", "Kinox", "https://kinox.to", "kinox.to",
            body_markers=["kinox"], expected_headers={"server": "cloudflare"},
            enabled_setting_key="kinox_search_enabled",
        )

    *site_id* becomes a key in :data:`_MONITOR_SITES` -- the exact dict the
    five built-in sites (aniworld/sto/filmpalast/megakino/hanime) live in, and
    the one every part of the UpTime feature (the probe loop, the DNS test,
    ``web/routes/uptime.py``'s API, ``web/static/uptime.js``'s rendering) is
    already generic over. Registering here is therefore the whole
    integration: your site gets a card on the UpTime dashboard, its own
    heartbeat history, the same failure-threshold debounce and blocked-page
    detection as a built-in site, with no template/route/JS change needed.

    - *label*, *url*, *expected_domain*: same meaning as a built-in entry --
      *url* is what gets probed, *expected_domain* is checked against the
      final response to catch a DNS hijack/wrong-site redirect.
    - *body_markers*: substrings that must appear in the response body to
      count the site as actually itself (not just "some server answered").
    - *expected_headers*: optional dict of response-header-name -> substring
      (e.g. ``{"server": "cloudflare"}``) -- the CDN/edge signature check, see
      the module docstring's explanation of why this is checked instead of
      the resolved IP.
    - *enabled_setting_key*: optional -- the ``app_settings`` key that
      reflects whether this source is actually enabled (typically the same
      ``enabled_setting_key`` you already passed to ``register_thirdparty()``).
      Purely cosmetic (feeds the "enabled_source" badge on the UpTime card);
      omit it and the card just won't show that badge as accurately.
    - *enabled_setting_default*: what an *unset* ``enabled_setting_key``
      means. Defaults to True, because a module source was installed on
      purpose. Pass False if your key is opt-in -- without this the core would
      have to guess, and guessing "off" made a module that writes its key only
      on first save show a permanent "source disabled" badge.
    - *tracked_by_default*: whether the "track this site" toggle starts on.
      Seeded once, the same "only if never explicitly set" semantics as
      ``MODULE_ENABLED_DEFAULT`` (see the module README) -- a later call never
      re-flips a value a user (or a previous run) already changed.

    Raises ``ValueError`` if *site_id* is already registered. Removed
    automatically on disable/uninstall via :func:`unregister_monitor_site`.
    """
    if site_id in _MONITOR_SITES:
        raise ValueError(f"register_monitor_site: site id already registered: {site_id!r}")
    _MONITOR_SITES[site_id] = (label, url, expected_domain, list(body_markers), dict(expected_headers or {}))
    _EXTRA_MONITOR_SITES[item_id] = site_id
    _MONITOR_TRACKED_DEFAULTS[site_id] = "1" if tracked_by_default else "0"
    if enabled_setting_key:
        _MONITOR_ENABLED_KEYS[site_id] = enabled_setting_key
        _MONITOR_ENABLED_DEFAULTS[site_id] = "1" if enabled_setting_default else "0"
    if get_setting("uptime_track_" + site_id, None) is None:
        # Seed the toggle so the stored state matches what the module asked
        # for from the first round on. _tracked_default() would answer the
        # same either way now, but the seeded row is what the settings UI
        # reads back and what survives an uninstall/reinstall.
        set_setting("uptime_track_" + site_id, _MONITOR_TRACKED_DEFAULTS[site_id])
    logger.info("[Uptime] Registered third-party monitor site: %s (%s)", site_id, item_id)


def unregister_monitor_site(item_id) -> None:
    """Drop a monitor site previously added via :func:`register_monitor_site`.
    Leaves stored heartbeats/settings in place (same "inert, not deleted"
    treatment as :func:`mirrors.unregister_site_mirrors`)."""
    site_id = _EXTRA_MONITOR_SITES.pop(item_id, None)
    if site_id is None:
        return
    _MONITOR_SITES.pop(site_id, None)
    _MONITOR_ENABLED_KEYS.pop(site_id, None)
    _MONITOR_ENABLED_DEFAULTS.pop(site_id, None)
    _MONITOR_TRACKED_DEFAULTS.pop(site_id, None)
    with _consec_fail_lock:
        _consec_fail.pop(site_id, None)
    logger.info("[Uptime] Unregistered third-party monitor site: %s", site_id)


def thirdparty_monitor_ids() -> set:
    """item_ids that currently own a third-party monitor site.

    Read-only counterpart of :func:`unregister_monitor_site` -- the
    Modulmanager uses it to report what a module registered without reaching
    into this module's private dict.
    """
    return set(_EXTRA_MONITOR_SITES)


# Throttle state for flag.uptime_monitor: the monotonic timestamp of the last
# submitted flag event (None = never submitted in this process). Guarded by its
# own lock because a manual "run now" from the UI and the scheduled loop can
# reach _uptime_run_round() at the same time.
_TELEMETRY_FLAG_INTERVAL = 86400.0  # seconds -- one event per day per process
_telemetry_flag_last = None
_telemetry_flag_lock = threading.Lock()


def _report_uptime_monitor_active():
    """Submit the flag.uptime_monitor stage-2 usage counter, at most once per
    24 h per process (see _TELEMETRY_FLAG_INTERVAL).

    A pure counter -- build_feature_flag_event() takes no metadata at all, so
    nothing about which sites are tracked is ever sent. Wrapped in its own
    try/except so a telemetry bug can never affect a monitor round.
    """
    global _telemetry_flag_last
    try:
        now = time.monotonic()
        with _telemetry_flag_lock:
            if (_telemetry_flag_last is not None
                    and now - _telemetry_flag_last < _TELEMETRY_FLAG_INTERVAL):
                return
            _telemetry_flag_last = now
        telemetry_client.submit(telemetry_events.build_feature_flag_event("flag.uptime_monitor"))
    except Exception:
        logger.debug("[Telemetry] failed to build/submit flag.uptime_monitor event", exc_info=True)


# Serialises monitor rounds. The scheduled loop and any number of manual
# "check now" clicks all funnel through here: two rounds running at once wrote
# two heartbeats per site into the same second (skewing uptime_pct and avg_ms)
# and, worse, incremented _consec_fail twice per real failure, so the
# failure-threshold debounce flipped a site to "down" in half the rounds the
# user configured. Callers that cannot wait use uptime_round_in_progress().
_uptime_round_lock = threading.Lock()


def uptime_round_in_progress() -> bool:
    """True while a monitor round is running (scheduled or manual).

    Used by: web/routes/uptime.py, to answer a redundant "check now" with 409
    instead of stacking another thread onto a round already in flight.
    """
    return _uptime_round_lock.locked()


def _uptime_run_round(cfg=None):
    """Probe every tracked source once and store a heartbeat each; then prune.

    Serialised: if a round is already running this call returns immediately
    rather than probing everything a second time (see _uptime_round_lock).

    Used by: web/routes/uptime.py (manual "run now"), _start_uptime_monitor()
    below (scheduled loop).
    """
    if not _uptime_round_lock.acquire(blocking=False):
        logger.debug("[UpTime] round already in progress — skipping this one")
        return
    try:
        _uptime_run_round_locked(cfg)
    finally:
        _uptime_round_lock.release()


def _uptime_run_round_locked(cfg=None):
    """The body of a monitor round. Only called with _uptime_round_lock held."""
    cfg = cfg or _uptime_config()

    # Telemetry: stage-2 usage counter for "monitoring is actively running".
    # Throttled inside the helper -- see _report_uptime_monitor_active().
    _report_uptime_monitor_active()

    # Snapshot the site table: register_monitor_site()/unregister_monitor_site()
    # mutate _MONITOR_SITES from the request thread (a module being enabled or
    # uninstalled), and a round takes long enough — every tracked site times
    # out at up to cfg["timeout"] seconds — that iterating the live dict would
    # eventually raise "dictionary changed size during iteration" and kill the
    # round. A site that disappears mid-round simply gets one last heartbeat.
    _sites = list(_MONITOR_SITES.items())

    # Sites that are no longer tracked must not keep a stale failure streak:
    # untracking a site mid-outage and re-tracking it later would otherwise
    # resume at the old count and flip it to "down" on the first check.
    with _consec_fail_lock:
        for _stale in [s for s in _consec_fail if not cfg["tracked"].get(s)]:
            _consec_fail.pop(_stale, None)

    for _sid, (_label, _url, _domain, _markers, _headers) in _sites:
        if not cfg["tracked"].get(_sid):
            continue
        try:
            r = _probe_site(_url, _domain, _markers, expected_headers=_headers,
                            timeout=cfg["timeout"], use_get=cfg.get("use_get", False),
                            probe=True, resolve_ip=False)
            if r.get("http_ok") and r.get("site_verified"):
                status, msg = "up", None
            elif r.get("blocked"):
                status, msg = "down", "blocked_page"
            elif r.get("http_ok"):
                status, msg = "degraded", "reachable, content unverified"
            else:
                status = "down"
                msg = r.get("http_error") or r.get("socket_error") or "unreachable"

            # Debounce transient reachability failures: only a *confirmed* block
            # page flips a site to "down" immediately. A timeout/unreachable
            # "down" is held as "degraded" until it has failed
            # `failure_threshold` consecutive rounds — this is what stops a brief
            # DNS/CDN hiccup from reporting an online site as offline. Any
            # non-reachability-down result resets the counter.
            threshold = cfg.get("failure_threshold", 1)
            is_reach_down = (status == "down" and msg != "blocked_page")
            with _consec_fail_lock:
                if is_reach_down:
                    n = _consec_fail.get(_sid, 0) + 1
                    _consec_fail[_sid] = n
                    if n < threshold:
                        status = "degraded"
                        msg = "transient failure %d/%d (%s)" % (n, threshold, msg)
                else:
                    _consec_fail.pop(_sid, None)

            record_uptime_heartbeat(
                _sid, status,
                response_ms=r.get("response_ms"),
                http_status=r.get("http_status"),
                message=msg,
            )
        except Exception as exc:
            try:
                record_uptime_heartbeat(_sid, "down", message=str(exc))
            except Exception:
                pass
    try:
        prune_uptime_heartbeats(cfg["retention_days"])
    except Exception:
        pass


def _start_uptime_monitor():
    """Start the background monitor loop once. Idle-waits while disabled.

    Used by: web/app.py (called during app startup).
    """
    global _uptime_monitor_started
    with _uptime_monitor_lock:
        if _uptime_monitor_started:
            return
        _uptime_monitor_started = True

    def _loop():
        while True:
            # Clear BEFORE reading the config, never after waiting. The old
            # order (wait -> clear) dropped any set() that landed while a round
            # was running — and a round can run for minutes — so a settings
            # change was silently swallowed and only took effect after the full
            # interval, which clamps as high as 24 h. Clearing first means a
            # set() from this point on is always still pending at the next
            # wait() and wakes it immediately.
            _uptime_wake.clear()
            try:
                cfg = _uptime_config()
            except Exception:
                cfg = None
            if not cfg or not cfg["enabled"]:
                _uptime_wake.wait(timeout=10)
                continue
            _t0 = time.monotonic()
            try:
                _uptime_run_round(cfg)
            except Exception:
                logger.warning("[UpTime] monitor round failed", exc_info=True)
            # Sleep until the next round, waking early if the config changes.
            # The round's own duration counts towards the interval: probing
            # five sites sequentially at timeout=120 can outlast interval=60
            # outright, and sleeping the full interval on top of that drifted
            # the heartbeat spacing further with every round. Never busy-loop
            # though — a round that overran keeps a 5 s floor.
            _elapsed = time.monotonic() - _t0
            _uptime_wake.wait(timeout=max(5.0, cfg["interval"] - _elapsed))

    threading.Thread(target=_loop, daemon=True, name="uptime-monitor").start()
