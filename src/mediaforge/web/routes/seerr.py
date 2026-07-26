"""Seerr request routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

detail.integrations (connection errors, no credentials) is wired at the
Jellyseerr/Overseerr fetch below -- see registry.py. flag.integrations.seerr
(usage counter) is intentionally NOT wired -- out of scope for now.

Security / performance notes for this module
--------------------------------------------
* Every error surfaced to the browser is a stable machine code
  (``{"error": "<code>"}``), never an upstream message. Upstream bodies and
  exception texts echo the configured Seerr URL (and, behind some reverse
  proxies, the API key) -- those stay in the server log. static/seerr.js
  translates the codes for display.
* ``approve`` / ``decline`` / the moderation half of ``batch`` are moderation
  actions on the upstream Seerr instance and are admin-gated in-route via
  ``_require_admin()``. ``hide``/``unhide`` are per-user view preferences and
  stay available to every logged-in user.
* The upstream request list is cached per Seerr instance for a few seconds
  (``_LIST_TTL``). Infinite scroll used to re-issue four 500-item upstream
  calls for every 20-item page.
* TMDB detail lookups are cached (``_DETAIL_TTL``) and only fanned out over
  the *whole* result set when a query actually needs titles (free-text search
  or sort-by-title); otherwise only the visible page is enriched.
"""

from ...config import LANG_LABELS
from ..db import get_hidden_seerr_request_ids
from ..db import get_hidden_seerr_requests
from ..db import get_setting
from ..db import hide_seerr_request
from ..db import unhide_seerr_request
from ..runtime_state import WORKING_PROVIDERS
from flask import jsonify
from flask import render_template
from flask import request
import json
import threading
import time
from .image_proxy import _poster_proxy
from ...logger import get_logger
from ...telemetry import client as telemetry_client
from ...telemetry import events as telemetry_events


logger = get_logger(__name__)

# --- Bounds -----------------------------------------------------------------
# Page size the frontend may ask for. Anything above this is clamped, not
# rejected, so a stale frontend keeps working.
MAX_TAKE = 50
# Upstream is asked for at most this many requests per filter/media-type combo.
UPSTREAM_TAKE = 500
# Free-text search / sort-by-title need a title for every candidate, i.e. one
# TMDB detail call each. Bound that fan-out so a library with thousands of open
# requests cannot turn one page view into thousands of upstream calls.
MAX_TITLE_ENRICH = 400
# Batch moderation: how many ids one call may carry.
MAX_BATCH_IDS = 50
# Free-text query length (a longer needle cannot match anything useful anyway).
MAX_QUERY_LEN = 100

_LIST_TTL = 8.0        # seconds -- covers a burst of infinite-scroll pages
_DETAIL_TTL = 600.0    # seconds -- TMDB titles/posters barely change
_DETAIL_CACHE_MAX = 2000

_list_cache = {}       # seerr_url -> (expires_at, merged_list)
_detail_cache = {}     # (seerr_url, tmdb_id, media_type) -> (expires_at, detail)
_cache_lock = threading.Lock()

_STATUS_PENDING = 1
_STATUS_APPROVED = 2
# Jellyseerr/Overseerr media status 5 == fully available -> nothing left to do.
_MEDIA_AVAILABLE = 5


def _report_seerr_error(exc):
    """Submit a detail.integrations telemetry event for a failed Seerr fetch
    (see registry.py's "detail.integrations"). Only the exception class name
    is sent -- never the raw message, which echoes the configured Seerr URL.
    Wrapped in its own try/except so a telemetry bug can never affect the
    requests page itself.
    """
    try:
        event = telemetry_events.build_feature_detail_event(
            "detail.integrations", action="connect", status="error",
            metadata={"integration": "seerr", "error_type": type(exc).__name__},
        )
        if event:
            telemetry_client.submit(event)
    except Exception:
        logger.debug("[Telemetry] failed to build/submit detail.integrations event", exc_info=True)


def _seerr_config():
    """Return (url, api_key) for the configured Seerr instance, or (None, None)."""
    url = (get_setting("seerr_url") or "").rstrip("/")
    key = get_setting("seerr_api_key") or ""
    if not url or not key:
        return None, None
    return url, key


def _err(code, status=400, **extra):
    """Build a stable, non-leaking error response.

    The frontend maps `code` to a translated message; the raw upstream text
    never leaves the server (it contains the Seerr URL, and occasionally the
    API key when a reverse proxy echoes the query string back).
    """
    payload = {"error": code}
    payload.update(extra)
    return jsonify(payload), status


def _current_uid():
    from flask import session as _fs
    return _fs.get("user_id", 0)


def _require_admin():
    """Return an error response when the caller is not an admin, else None.

    Gating happens in-route rather than through app.py's `_admin_only` set
    because a single endpoint (`/batch`) mixes a per-user action (hide) with
    moderation actions (approve/decline) and must decide per action.
    """
    from ..request_context import get_current_user_info
    try:
        _username, is_admin = get_current_user_info()
    except Exception:
        logger.debug("[Seerr] could not resolve current user, denying", exc_info=True)
        return _err("forbidden", 403)
    if not is_admin:
        return _err("forbidden", 403)
    return None


def _int_arg(name, default, lo, hi):
    """Parse a bounded integer query arg. Garbage falls back to `default`
    instead of raising a 500 -- int("abc") used to escape as an unhandled
    ValueError straight out of the view."""
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Upstream access
# ---------------------------------------------------------------------------

def _seerr_get(seerr_url, seerr_key, path, params=None):
    """GET a Seerr API path and return the decoded JSON body."""
    import urllib.request as _urllib
    import urllib.parse as _urlparse

    url = seerr_url + path
    if params:
        url += "?" + _urlparse.urlencode(params)
    req = _urllib.Request(url, headers={"X-Api-Key": seerr_key})
    with _urllib.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _seerr_session(seerr_url, seerr_key):
    """Build a requests.Session carrying the API key and, when the instance
    hands one out, a CSRF token.

    Seerr (Jellyseerr/Overseerr) requires a CSRF token even for API-key
    requests. A session is used so cookies (including the CSRF cookie) persist
    across the token fetch and the actual POST. Previously approve and decline
    each carried their own verbatim copy of this block.
    """
    import requests as _req

    session = _req.Session()
    session.headers.update({"X-Api-Key": seerr_key})

    csrf_token = ""
    for csrf_path in ("/api/auth/csrf", "/api/v1/settings/public"):
        try:
            pre = session.get(f"{seerr_url}{csrf_path}", timeout=10)
            # The Next.js csrf endpoint returns {"csrfToken": "..."}.
            if csrf_path == "/api/auth/csrf" and pre.ok:
                csrf_token = pre.json().get("csrfToken", "")
            if not csrf_token:
                # Double-submit cookie pattern: XSRF-TOKEN or CSRF-TOKEN cookie
                csrf_token = (
                    session.cookies.get("XSRF-TOKEN")
                    or session.cookies.get("CSRF-TOKEN")
                    or session.cookies.get("csrf_token")
                    or ""
                )
            if csrf_token:
                break
        except Exception:
            pass

    logger.debug("Seerr CSRF token obtained: %s", "yes" if csrf_token else "no")
    if csrf_token:
        session.headers.update({
            "X-CSRF-Token": csrf_token,
            "X-XSRF-TOKEN": csrf_token,
        })
    return session


def _moderate(seerr_url, seerr_key, req_id, action):
    """POST approve/decline for one request id.

    Returns (ok, error_code). The upstream status code and body are logged,
    never returned -- see the module docstring.
    """
    try:
        session = _seerr_session(seerr_url, seerr_key)
        resp = session.post(
            f"{seerr_url}/api/v1/request/{req_id}/{action}",
            json={},
            timeout=10,
        )
        logger.info("Seerr %s req %s -> %s", action, req_id, resp.status_code)
        if not resp.ok:
            logger.warning(
                "Seerr %s req %s failed: %s %s",
                action, req_id, resp.status_code, resp.text[:300],
            )
            return False, "upstream_error"
        return True, None
    except Exception as e:
        logger.warning("Seerr %s req %s error: %s", action, req_id, e)
        _report_seerr_error(e)
        return False, "unreachable"


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

def _cached_request_list(seerr_url, seerr_key):
    """Return the merged pending+approved request list for this instance.

    Cached for `_LIST_TTL` seconds and keyed by the Seerr URL only: the list
    itself is instance-wide, per-user hiding is applied by the caller after the
    fact. Infinite scroll previously re-issued four 500-item upstream calls for
    every 20-item page.
    """
    from concurrent.futures import ThreadPoolExecutor

    now = time.time()
    with _cache_lock:
        hit = _list_cache.get(seerr_url)
        if hit and hit[0] > now:
            return hit[1]

    def fetch_filter(args):
        f, media_type = args
        return media_type, _seerr_get(seerr_url, seerr_key, "/api/v1/request", {
            "filter": f, "mediaType": media_type,
            "take": UPSTREAM_TAKE, "skip": 0,
            "sort": "added", "sortDirection": "desc",
        })

    combos = [("pending", "tv"), ("approved", "tv"),
              ("pending", "movie"), ("approved", "movie")]
    with ThreadPoolExecutor(max_workers=4) as ex:
        batches = list(ex.map(fetch_filter, combos))

    # Merge + de-duplicate by request id, tagging each item with its media type
    # so we know which detail endpoint to call later.
    seen = set()
    merged = []
    for media_type, payload in batches:
        for r in (payload or {}).get("results", []):
            rid = r.get("id")
            if rid is None or rid in seen:
                continue
            seen.add(rid)
            r.setdefault("_media_type", media_type)
            merged.append(r)

    # Keep only truly pending (1) or approved-but-not-yet-available (2), and
    # drop anything whose media is already fully available upstream.
    merged = [
        r for r in merged
        if r.get("status") in (_STATUS_PENDING, _STATUS_APPROVED)
        and (r.get("media") or {}).get("status") != _MEDIA_AVAILABLE
    ]
    merged.sort(key=lambda r: r.get("createdAt") or "", reverse=True)

    with _cache_lock:
        _list_cache[seerr_url] = (time.time() + _LIST_TTL, merged)
        # Normally a single entry. Drop anything stale so a changed URL cannot
        # pin the old instance's list in memory forever.
        for k in [k for k, (exp, _v) in _list_cache.items() if exp <= now]:
            _list_cache.pop(k, None)
    return merged


def invalidate_seerr_list_cache():
    """Drop the cached upstream request list (after approve/decline/batch)."""
    with _cache_lock:
        _list_cache.clear()


def _fetch_details(seerr_url, seerr_key, targets):
    """Fetch TMDB detail payloads for [(tmdb_id, media_type), ...] in parallel.

    Results are memoised for `_DETAIL_TTL`; only cache misses hit the network.
    """
    from concurrent.futures import ThreadPoolExecutor

    now = time.time()
    out = {}
    misses = []
    with _cache_lock:
        for target in targets:
            hit = _detail_cache.get((seerr_url,) + target)
            if hit and hit[0] > now:
                out[target] = hit[1]
            elif target not in out:
                misses.append(target)
    misses = list(dict.fromkeys(misses))

    if misses:
        def fetch_one(target):
            tid, mt = target
            try:
                endpoint = f"/api/v1/{'movie' if mt == 'movie' else 'tv'}/{tid}"
                return target, _seerr_get(seerr_url, seerr_key, endpoint)
            except Exception:
                # A single unresolvable id must not fail the whole page.
                return target, {}

        with ThreadPoolExecutor(max_workers=8) as ex:
            fetched = list(ex.map(fetch_one, misses))

        expires = time.time() + _DETAIL_TTL
        with _cache_lock:
            for target, det in fetched:
                out[target] = det
                # Only cache successful lookups; an empty dict would pin a
                # transient upstream hiccup for ten minutes.
                if det:
                    _detail_cache[(seerr_url,) + target] = (expires, det)
            if len(_detail_cache) > _DETAIL_CACHE_MAX:
                # Oldest-expiring first, back down to 80% of the cap.
                items = sorted(_detail_cache.items(), key=lambda kv: kv[1][0])
                excess = len(_detail_cache) - int(_DETAIL_CACHE_MAX * 0.8)
                for k, _v in items[:excess]:
                    _detail_cache.pop(k, None)
    return out


def _title_of(det, tmdb_id, is_movie):
    """Display title for a detail payload.

    TV uses "name"/"firstAirDate"; movies use "title"/"releaseDate".
    """
    return (
        (det.get("title") if is_movie else det.get("name"))
        or det.get("originalTitle")
        or det.get("originalName")
        or f"TMDB #{tmdb_id}"
    )


def _target_of(req):
    """(tmdb_id, media_type) for a raw upstream request, or None."""
    tmdb_id = (req.get("media") or {}).get("tmdbId")
    if not tmdb_id:
        return None
    return tmdb_id, req.get("_media_type", "tv")


def register_seerr_routes(app):
    """Register all Jellyseerr/Overseerr request-browsing and moderation routes
    (list/approve/decline/hide/batch) on the given Flask app."""

    @app.route("/api/seerr/requests")
    def api_seerr_requests():
        """Return a filtered, sorted, paginated list of pending/approved Seerr requests.

        Route: GET /api/seerr/requests. Called from static/seerr.js.

        Query parameters (all optional):
          take / skip   page window (take is clamped to MAX_TAKE)
          q             free-text title filter
          status        "pending" | "approved" | "all" (default "all")
          type          "tv" | "movie" | "all" (default "all")
          sort          "added" | "title" | "status" (default "added")
          dir           "asc" | "desc"

        The upstream list is cached briefly; TMDB detail lookups are cached for
        ten minutes and only fanned out over the whole candidate set when a
        title is actually needed (free-text search or sort-by-title).
        """
        seerr_url, seerr_key = _seerr_config()
        if not seerr_url:
            return _err("not_configured", 400)

        take = _int_arg("take", 20, 1, MAX_TAKE)
        skip = _int_arg("skip", 0, 0, 100000)
        query = (request.args.get("q") or "").strip()[:MAX_QUERY_LEN].lower()
        status_f = (request.args.get("status") or "all").lower()
        type_f = (request.args.get("type") or "all").lower()
        sort_f = (request.args.get("sort") or "added").lower()
        dir_f = (request.args.get("dir") or "").lower()
        if status_f not in ("pending", "approved", "all"):
            status_f = "all"
        if type_f not in ("tv", "movie", "all"):
            type_f = "all"
        if sort_f not in ("added", "title", "status"):
            sort_f = "added"
        if dir_f not in ("asc", "desc"):
            dir_f = "desc" if sort_f == "added" else "asc"

        try:
            merged = _cached_request_list(seerr_url, seerr_key)
        except Exception as e:
            _report_seerr_error(e)
            logger.warning("[Seerr] request list unreachable: %s", e)
            return _err("unreachable", 502)

        uid = _current_uid()
        hidden_ids = get_hidden_seerr_request_ids(uid)
        visible = [r for r in merged if r["id"] not in hidden_ids]

        # Facet counts describe the split *before* the status/type filters
        # narrow it, so the chips can show what switching to them would yield.
        facets = {
            "all": len(visible),
            "pending": sum(1 for r in visible if r.get("status") == _STATUS_PENDING),
            "approved": sum(1 for r in visible if r.get("status") == _STATUS_APPROVED),
            "tv": sum(1 for r in visible if r.get("_media_type", "tv") == "tv"),
            "movie": sum(1 for r in visible if r.get("_media_type") == "movie"),
        }

        # Cheap filters first: both live on the raw upstream payload, so they
        # shrink the set before any TMDB detail call is made.
        candidates = visible
        if status_f == "pending":
            candidates = [r for r in candidates if r.get("status") == _STATUS_PENDING]
        elif status_f == "approved":
            candidates = [r for r in candidates if r.get("status") == _STATUS_APPROVED]
        if type_f in ("tv", "movie"):
            candidates = [r for r in candidates if r.get("_media_type", "tv") == type_f]

        needs_titles = bool(query) or sort_f == "title"
        title_truncated = False
        details = {}

        if needs_titles:
            targets = [t for t in (_target_of(r) for r in candidates) if t]
            if len(targets) > MAX_TITLE_ENRICH:
                # Newest-first is already the order; keep the freshest slice and
                # tell the frontend the search was not exhaustive rather than
                # silently pretending it was.
                targets = targets[:MAX_TITLE_ENRICH]
                title_truncated = True
            details = _fetch_details(seerr_url, seerr_key, targets)

            resolved = []
            for r in candidates:
                target = _target_of(r)
                if not target or target not in details:
                    continue
                title = _title_of(details[target], target[0], target[1] == "movie")
                if query and query not in title.lower():
                    continue
                r = dict(r)
                r["_title"] = title
                resolved.append(r)
            candidates = resolved

        # Sorting
        reverse = dir_f == "desc"
        if sort_f == "title":
            candidates = sorted(candidates, key=lambda r: (r.get("_title") or "").lower(), reverse=reverse)
        elif sort_f == "status":
            candidates = sorted(
                candidates,
                key=lambda r: (r.get("status") or 0, r.get("createdAt") or ""),
                reverse=reverse,
            )
        else:
            candidates = sorted(candidates, key=lambda r: r.get("createdAt") or "", reverse=reverse)

        total_all = len(candidates)
        items = candidates[skip: skip + take]

        # Enrich exactly the visible page (cache hits for anything already
        # resolved by the search path above).
        page_targets = [t for t in (_target_of(r) for r in items) if t]
        page_details = dict(details)
        page_details.update(_fetch_details(seerr_url, seerr_key, page_targets))

        result = []
        for req in items:
            target = _target_of(req)
            tmdb_id = target[0] if target else None
            media_type = target[1] if target else req.get("_media_type", "tv")
            det = page_details.get(target, {}) if target else {}
            is_movie = media_type == "movie"

            # Both dates come straight out of the detail payload we already
            # hold. This used to be a second, *serial* upstream call per item
            # (getReleaseDate()) -- up to 50 sequential round-trips per page.
            release_date = (det.get("releaseDate") if is_movie else det.get("firstAirDate")) or ""

            poster_path = det.get("posterPath") or ""
            backdrop_path = det.get("backdropPath") or ""
            result.append({
                "id": req["id"],
                "status": req.get("status"),
                "downloadStatus": (req.get("media") or {}).get("status"),
                "createdAt": req.get("createdAt"),
                "requestedBy": (req.get("requestedBy") or {}).get("displayName", ""),
                "tmdbId": tmdb_id,
                "mediaType": media_type,
                "isMovie": is_movie,
                "title": req.get("_title") or _title_of(det, tmdb_id, is_movie),
                "posterPath": poster_path,
                "posterUrl": _poster_proxy("https://image.tmdb.org/t/p/w342" + poster_path) if poster_path else "",
                "backdropUrl": _poster_proxy("https://image.tmdb.org/t/p/w780" + backdrop_path) if backdrop_path else "",
                "overview": det.get("overview") or "",
                "firstAirDate": release_date[:4],
                "releaseDate": release_date,
                "voteAverage": det.get("voteAverage") or 0,
                "numberOfSeasons": det.get("numberOfSeasons") or 0,
                "requestedSeasons": sorted(
                    s["seasonNumber"] for s in (req.get("seasons") or [])
                    if isinstance(s, dict) and s.get("seasonNumber") is not None
                ),
            })

        return jsonify({
            "requests": result,
            "total": total_all,
            "skip": skip,
            "take": take,
            "facets": facets,
            "truncated": title_truncated,
        })

    @app.route("/seerr")
    def seerr_page():
        """Render the Seerr requests page. Route: GET /seerr."""
        sto_lang_labels = {"1": "German Dub", "2": "English Dub", "3": "English Dub (German Sub)"}
        return render_template(
            "seerr.html",
            lang_labels=LANG_LABELS,
            sto_lang_labels=sto_lang_labels,
            supported_providers=WORKING_PROVIDERS,
        )

    @app.route("/api/seerr/requests/<int:req_id>/approve", methods=["POST"])
    def api_seerr_approve(req_id):
        """Approve a pending Seerr request so it starts being fulfilled upstream.

        Route: POST /api/seerr/requests/<req_id>/approve. Called from app.js's
        `_approveSeerrRequestIfPending()` right before a download is queued for
        a request that was still pending, and from the batch endpoint below.

        Admin-only: approving is moderation on the *upstream* Seerr instance,
        not a local preference. Non-admins can still download a pending request
        -- app.js treats the approve step as best-effort, so a 403 there simply
        leaves the request pending for an admin to act on.
        """
        denied = _require_admin()
        if denied:
            return denied
        seerr_url, seerr_key = _seerr_config()
        if not seerr_url:
            return _err("not_configured", 400)
        ok, code = _moderate(seerr_url, seerr_key, req_id, "approve")
        if not ok:
            return _err(code, 502)
        invalidate_seerr_list_cache()
        return jsonify({"ok": True})

    @app.route("/api/seerr/requests/<int:req_id>/decline", methods=["POST"])
    def api_seerr_decline(req_id):
        """Decline a pending Seerr request.

        Route: POST /api/seerr/requests/<req_id>/decline. Called from
        static/seerr.js. Admin-only -- the action is irreversible upstream and
        the button was already admin-only in the UI (`seerrCanDecline`); the
        route itself was not, so any logged-in user could decline any request
        by calling it directly.
        """
        denied = _require_admin()
        if denied:
            return denied
        seerr_url, seerr_key = _seerr_config()
        if not seerr_url:
            return _err("not_configured", 400)
        ok, code = _moderate(seerr_url, seerr_key, req_id, "decline")
        if not ok:
            return _err(code, 502)
        invalidate_seerr_list_cache()
        return jsonify({"ok": True})

    @app.route("/api/seerr/requests/batch", methods=["POST"])
    def api_seerr_batch():
        """Apply one action to several requests at once.

        Route: POST /api/seerr/requests/batch. Body:
            {"action": "approve"|"decline"|"hide", "ids": [1, 2, ...],
             "items": {"<id>": {"title": "...", "posterUrl": "..."}}}

        `items` is optional metadata used only by `hide`, so the hidden-requests
        modal can show a poster and title without another upstream round-trip.

        approve/decline are admin-only (see the single-id routes); hide is a
        per-user view preference and therefore open to every logged-in user.
        Ids are capped at MAX_BATCH_IDS so one call cannot fan out unbounded.
        """
        data = request.get_json(silent=True) or {}
        action = str(data.get("action", "")).lower()
        if action not in ("approve", "decline", "hide"):
            return _err("bad_action", 400)

        raw_ids = data.get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return _err("bad_ids", 400)
        ids = []
        for value in raw_ids[:MAX_BATCH_IDS]:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        ids = list(dict.fromkeys(ids))
        if not ids:
            return _err("bad_ids", 400)

        if action == "hide":
            uid = _current_uid()
            items = data.get("items")
            meta = items if isinstance(items, dict) else {}
            for req_id in ids:
                entry = meta.get(str(req_id))
                entry = entry if isinstance(entry, dict) else {}
                title = str(entry.get("title", ""))[:300].strip()
                poster = str(entry.get("posterUrl", ""))[:500].strip()
                hide_seerr_request(uid, req_id, title, poster)
            return jsonify({"ok": True, "done": ids, "failed": []})

        denied = _require_admin()
        if denied:
            return denied
        seerr_url, seerr_key = _seerr_config()
        if not seerr_url:
            return _err("not_configured", 400)

        done, failed = [], []
        for req_id in ids:
            ok, _code = _moderate(seerr_url, seerr_key, req_id, action)
            (done if ok else failed).append(req_id)
        invalidate_seerr_list_cache()
        return jsonify({"ok": not failed, "done": done, "failed": failed})

    @app.route("/api/seerr/requests/<int:req_id>/hide", methods=["POST"])
    def api_seerr_hide(req_id):
        """Hide a Seerr request from the current user's request list (per-user).

        Route: POST /api/seerr/requests/<req_id>/hide. Called from
        static/seerr.js. `title`/`posterUrl` are stored purely so the
        hidden-requests modal can render a row without another upstream call --
        both are length-capped here and HTML-escaped at render time.
        """
        uid = _current_uid()
        data = request.get_json(silent=True) or {}
        title = str(data.get("title", ""))[:300].strip()
        poster_url = str(data.get("posterUrl", ""))[:500].strip()
        hide_seerr_request(uid, req_id, title, poster_url)
        return jsonify({"ok": True})

    @app.route("/api/seerr/requests/<int:req_id>/unhide", methods=["POST"])
    def api_seerr_unhide(req_id):
        """Un-hide a previously hidden Seerr request for the current user.

        Route: POST /api/seerr/requests/<req_id>/unhide. Called from
        static/seerr.js.
        """
        unhide_seerr_request(_current_uid(), req_id)
        return jsonify({"ok": True})

    @app.route("/api/seerr/hidden")
    def api_seerr_hidden():
        """List the Seerr requests the current user has hidden.

        Route: GET /api/seerr/hidden. Called from static/seerr.js.
        """
        return jsonify({"hidden": get_hidden_seerr_requests(_current_uid())})
