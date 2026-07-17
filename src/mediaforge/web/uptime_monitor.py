"""Source-site monitoring — shared probe (DNS test + UpTime) and the UpTime monitor.

# TODO(telemetry): wire up flag.uptime_monitor (usage counter) -- see
# telemetry/registry.py. Registry-only for now.
"""

import threading

from ..logger import get_logger
from .db import get_setting, prune_uptime_heartbeats, record_uptime_heartbeat
from .dns_patch import _ip_provider

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
                use_get=False, probe=True):
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
    the *only* request made in the common case. Only when the header signature
    does not match do we fall back to a full GET so the body can still be
    checked against known ISP/CUII block-page markers, purely for diagnostics.

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
        entry["site_verified"] = bool(entry["http_ok"] and entry["headers_matched"])

        # Header signature didn't match — fall back to the body markers.
        if entry["http_ok"] and not entry["headers_matched"]:
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
    for _sid in _MONITOR_SITES:
        _def = "0" if _sid == "hanime" else "1"
        tracked[_sid] = get_setting("uptime_track_" + _sid, _def) == "1"

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


def _uptime_run_round(cfg=None):
    """Probe every tracked source once and store a heartbeat each; then prune.

    Used by: web/routes/uptime.py (manual "run now"), _start_uptime_monitor()
    below (scheduled loop).
    """
    cfg = cfg or _uptime_config()
    for _sid, (_label, _url, _domain, _markers, _headers) in _MONITOR_SITES.items():
        if not cfg["tracked"].get(_sid):
            continue
        try:
            r = _probe_site(_url, _domain, _markers, expected_headers=_headers,
                            timeout=cfg["timeout"], use_get=cfg.get("use_get", False),
                            probe=True)
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
            try:
                cfg = _uptime_config()
            except Exception:
                cfg = None
            if not cfg or not cfg["enabled"]:
                _uptime_wake.wait(timeout=10)
                _uptime_wake.clear()
                continue
            try:
                _uptime_run_round(cfg)
            except Exception:
                logger.warning("[UpTime] monitor round failed", exc_info=True)
            # Sleep until the next round, waking early if the config changes.
            _uptime_wake.wait(timeout=cfg["interval"])
            _uptime_wake.clear()

    threading.Thread(target=_loop, daemon=True, name="uptime-monitor").start()
