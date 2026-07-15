"""Search + TMDB discovery routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ...config import LANG_KEY_MAP
from ...config import LANG_LABELS
from ...config import MEDIAFORGE_CONFIG_DIR
from ...config import check_redirect_available
from ...config import probe_redirect
from ...providers import resolve_provider
from ...search import hanime_search
from ...search import megakino_search
from ...search import query as aniworld_query
from ...search import query_s_to
from ..db import clear_tmdb_cache
from .browse import _browse_cache
from .browse import _prefetch_cycle
from ..db import get_custom_paths
from ..db import get_setting
from ..lang_folders import LANG_FOLDERS
from ..queue_worker import _hanime_enabled
from ..queue_worker import _is_filmpalast_url
from ..queue_worker import _is_hanime_url
from ..queue_worker import _is_megakino_url
from ..queue_worker import _megakino_is_series
from ..queue_worker import _megakino_watch
from ..runtime_state import WORKING_PROVIDERS
from ..runtime_state import _SERIES_LINK_PATTERN
from ..runtime_state import _STO_SERIES_LINK_PATTERN
from flask import jsonify
from flask import render_template
from flask import request
from flask import session
from html import unescape as _html_unescape
import json
import os
import re
import threading
from ..tmdb_cache import _tmdb_lookup_cached
from .image_proxy import _poster_proxy
from ...logger import get_logger


logger = get_logger(__name__)


def _filter_and_dedup_providers(providers_map):
    """Turn a {provider_label: redirect_url} map into an ordered list of live,
    de-duplicated provider names for the provider dropdown.

    For every listed hoster with a working extractor it does one live probe
    (probe_redirect): dead/removed embeds are dropped, and entries whose
    redirect resolves to the *same* real host are collapsed to a single provider
    (labelled by the resolved host when known). This is what makes the dropdown
    reflect what is actually playable rather than every label the site lists.
    Runs for series as well as movies (previously movies-only), which is the
    whole point of the check — a few extra GETs per modal open in exchange for
    an accurate list.
    """
    wp_by_lower = {w.lower(): w for w in WORKING_PROVIDERS}
    seen = set()
    out = []
    for name, redirect in providers_map.items():
        if name not in WORKING_PROVIDERS:
            continue
        available, host_provider = probe_redirect(redirect, name)
        if not available:
            continue
        # Collapse mirror labels that resolve to the same host; key by the
        # resolved host when known, otherwise by the label itself.
        key = host_provider or name.lower()
        if key in seen:
            continue
        seen.add(key)
        # Prefer the canonical name of the resolved host (so a label that really
        # lands on a known hoster shows that hoster); fall back to the original
        # label when the host is unknown or has no working extractor.
        out.append(wp_by_lower.get(host_provider) or name)
    return out


def _filmpalast_search(keyword):
    """Search filmpalast.to via autocomplete and return list of {title, url} dicts."""
    import urllib.parse as _up
    import requests as _req
    try:
        url = f"https://filmpalast.to/autocomplete.php?term={_up.quote(keyword)}"
        resp = _req.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/javascript, */*",
                "Accept-Encoding": "gzip, deflate",  # requests decompresses automatically
            },
            timeout=8,
        )
        resp.raise_for_status()
        if not resp.text.strip():
            return []  # empty response = no results
        titles = resp.json()
    except Exception as e:
        logger.warning("FilmPalast autocomplete failed: %s", e)
        return []

    # The API returns either a list ["Title1", "Title2", ...]
    # or a dict {"0": "Title1", "1": "Title2", ...} depending on the query.
    # Normalise to a flat list of title strings.
    if isinstance(titles, dict):
        title_list = list(titles.values())
    elif isinstance(titles, list):
        title_list = titles
    else:
        return []

    candidates = []
    seen_urls = set()
    for title in title_list:
        if not isinstance(title, str) or not title.strip():
            continue
        slugs = _filmpalast_title_to_slugs(title)
        for slug in slugs:
            fp_url = f"https://filmpalast.to/stream/{slug}"
            if fp_url not in seen_urls:
                seen_urls.add(fp_url)
                candidates.append({"title": title, "url": fp_url})

    if not candidates:
        return []

    # Validate each candidate URL exists (our slug may differ from the real slug).
    # Run HEAD requests in parallel so the total wait is ~1 request RTT, not N×RTT.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import requests as _req2

    _val_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    def _head_ok(candidate):
        try:
            r = _req2.head(candidate["url"], headers=_val_headers, timeout=5, allow_redirects=True)
            return candidate if r.status_code == 200 else None
        except Exception:
            return None

    raw_ok = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_head_ok, c): c for c in candidates}
        for fut in as_completed(futures):
            ok = fut.result()
            if ok:
                raw_ok.append(ok)

    # Restore original autocomplete order & keep only the first working URL per title
    order = {c["url"]: i for i, c in enumerate(candidates)}
    raw_ok.sort(key=lambda c: order.get(c["url"], 9999))

    results = []
    seen_titles = set()
    for item in raw_ok:
        if item["title"] not in seen_titles:
            seen_titles.add(item["title"])
            results.append(item)

    # Re-sort results to preserve the original autocomplete order of titles
    title_order = {t: i for i, t in enumerate(title_list)}
    results.sort(key=lambda c: title_order.get(c["title"], 9999))
    return results


def _filmpalast_title_to_slugs(title):
    """Convert a movie title to a list of potential filmpalast URL slugs.

    Strategy: lowercase, replace umlauts with both simple and phonetic forms,
    replace non-alphanumeric with hyphens, collapse consecutive hyphens,
    strip leading/trailing hyphens.
    """
    import unicodedata
    import itertools
    import re as _r

    replacements = {
        "ä": ["a", "ae"],
        "ö": ["o", "oe"],
        "ü": ["u", "ue"],
        "Ä": ["a", "ae"],
        "Ö": ["o", "oe"],
        "Ü": ["u", "ue"],
        "ß": ["s", "ss"]
    }

    chars = list(title)
    options = []
    for c in chars:
        if c in replacements:
            options.append(replacements[c])
        else:
            options.append([c])

    special_count = sum(1 for c in chars if c in replacements)
    if special_count > 4:
        simple_list = []
        complex_list = []
        for c in chars:
            if c in replacements:
                simple_list.append(replacements[c][0])
                complex_list.append(replacements[c][1])
            else:
                simple_list.append(c)
                complex_list.append(c)
        combinations = ["".join(simple_list), "".join(complex_list)]
    else:
        combinations = ["".join(p) for p in itertools.product(*options)]

    slugs = set()
    for s in combinations:
        s = unicodedata.normalize("NFKD", s)
        s = s.encode("ascii", "ignore").decode("ascii")
        s = s.lower()
        s = _r.sub(r"[^a-z0-9]+", "-", s)
        s = s.strip("-")
        if s:
            slugs.add(s)

    return list(slugs)


def register_search_routes(app):
    """Register search, series/season/episode/provider lookup, and TMDB discovery endpoints."""
    @app.route("/api/search", methods=["POST"])
    def api_search():
        """Search a given site (aniworld/sto/filmpalast/megakino/hanime) for a keyword.

        POST /api/search. Called from app.js's doSearch() and
        runAniSearch(), and from seerr.js's search flow.
        """
        data = request.get_json(silent=True) or {}
        keyword = (data.get("keyword") or "").strip()
        keyword = re.sub(r"!+$", "", keyword).strip()
        site = (data.get("site") or "aniworld").strip()
        if not keyword:
            return jsonify({"error": "keyword is required"}), 400

        results = []

        def _get_site_results(kw, site_name):
            site_res = []
            if site_name == "sto":
                items = query_s_to(kw) or []
                if isinstance(items, dict): items = [items]
                for item in items:
                    link = item.get("link") or item.get("url", "")
                    if _STO_SERIES_LINK_PATTERN.match(link):
                        title = _html_unescape(item.get("title") or item.get("name", "Unknown")).replace("<em>", "").replace("</em>", "")
                        site_res.append({"title": title, "url": f"https://serienstream.to{link}"})
            else:
                items = aniworld_query(kw) or []
                if isinstance(items, dict): items = [items]
                for item in items:
                    link = item.get("link") or item.get("url", "")
                    if _SERIES_LINK_PATTERN.match(link):
                        title = _html_unescape(item.get("title") or item.get("name", "Unknown")).replace("<em>", "").replace("</em>", "")
                        site_res.append({"title": title, "url": f"https://aniworld.to{link}"})
            return site_res

        if site == "filmpalast":
            results = _filmpalast_search(keyword)
        elif site == "megakino":
            results = megakino_search(keyword) or []
        elif site == "hanime":
            # Adult source: only search when explicitly enabled.
            results = (hanime_search(keyword) or []) if _hanime_enabled() else []
        else:
            results = _get_site_results(keyword, site)
            
            # Fallback for apostrophes (AniWorld's search is broken for titles with apostrophes)
            if not results and ("'" in keyword or "’" in keyword):
                # Try the opposite apostrophe first
                alt_keyword = keyword.replace("'", "’") if "'" in keyword else keyword.replace("’", "'")
                if alt_keyword != keyword:
                    logger.debug("[CineInfo] Fallback Alt Apostrophe: Searching for %r", alt_keyword)
                    results = _get_site_results(alt_keyword, site)

                # Strategy: search for the part before the apostrophe
                if not results:
                    clean = keyword.replace("’", "'").split("'")[0].strip()
                    if clean and clean != keyword and clean != alt_keyword:
                        logger.debug("[CineInfo] Fallback 1: Searching for %r", clean)
                        results = _get_site_results(clean, site)
                
                # Secondary fallback: just remove the apostrophe
                if not results:
                    clean2 = keyword.replace("'", "").replace("’", "")
                    if clean2 and clean2 != keyword and clean2 != alt_keyword:
                        logger.debug("[CineInfo] Fallback 2: Searching for %r", clean2)
                        results = _get_site_results(clean2, site)

            # Fallback for hyphens / dashes
            if not results and ("-" in keyword or "–" in keyword):
                # Strategy: search for the part before the hyphen
                clean_hyphen = re.split(r"[-–]", keyword)[0].strip()
                if clean_hyphen and clean_hyphen != keyword:
                    logger.debug("[CineInfo] Fallback Hyphen: Searching for %r", clean_hyphen)
                    results = _get_site_results(clean_hyphen, site)

        return jsonify({"results": results})
    @app.route("/api/tmdb/genres")
    def api_tmdb_genres():
        """Fetch TV and Movie genres (German + English labels) from TMDB.

        GET /api/tmdb/genres. Called from app.js's loadGenres().
        """
        import requests as _req
        from ..db import get_setting
        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "No TMDB API Key"}), 400
        
        try:
            headers = {"accept": "application/json"}
            tv_de   = _req.get(f"https://api.themoviedb.org/3/genre/tv/list?language=de&api_key={api_key}",    headers=headers, timeout=10)
            tv_en   = _req.get(f"https://api.themoviedb.org/3/genre/tv/list?language=en&api_key={api_key}",    headers=headers, timeout=10)
            mov_de  = _req.get(f"https://api.themoviedb.org/3/genre/movie/list?language=de&api_key={api_key}", headers=headers, timeout=10)
            mov_en  = _req.get(f"https://api.themoviedb.org/3/genre/movie/list?language=en&api_key={api_key}", headers=headers, timeout=10)
            for r in (tv_de, tv_en, mov_de, mov_en):
                r.raise_for_status()

            return jsonify({
                "tv":    {"de": tv_de.json().get("genres", []),  "en": tv_en.json().get("genres", [])},
                "movie": {"de": mov_de.json().get("genres", []), "en": mov_en.json().get("genres", [])},
            })
        except Exception as e:
            logger.error(f"Error fetching TMDB genres: {e}")
            return jsonify({"error": str(e)}), 500
    @app.route("/api/tmdb/keywords")
    def api_tmdb_keywords():
        """Autocomplete search over the downloaded keyword_ids.json file."""
        query = request.args.get("q", "").strip().lower()
        if not query or len(query) < 2:
            return jsonify({"results": []})
        
        dest_file = MEDIAFORGE_CONFIG_DIR / "keyword_ids.json"
        if not dest_file.exists():
            return jsonify({"error": "Keyword data not downloaded yet. Please wait."}), 404
            
        results = []
        try:
            with open(dest_file, "r", encoding="utf-8") as f:
                for line in f:
                    if query in line.lower():
                        data = json.loads(line)
                        results.append(data)
                        if len(results) >= 20:
                            break
            return jsonify({"results": results})
        except Exception as e:
            logger.error(f"Error searching TMDB keywords: {e}")
            return jsonify({"error": str(e)}), 500
    @app.route("/api/tmdb/watch_regions")
    def api_tmdb_watch_regions():
        """Fetch the list of available watch-provider regions from TMDB."""
        import requests as _req
        from ..db import get_setting
        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "No TMDB API Key"}), 400

        _ui_lang = session.get("ui_language", "de")
        _tmdb_lang = "en-US" if _ui_lang == "en" else "de-DE"
        url = f"https://api.themoviedb.org/3/watch/providers/regions?language={_tmdb_lang}&api_key={api_key}"
        try:
            resp = _req.get(url, headers={"accept": "application/json"}, timeout=10)
            resp.raise_for_status()
            return jsonify({"results": resp.json().get("results", [])})
        except Exception as e:
            logger.error(f"Error fetching TMDB watch regions: {e}")
            return jsonify({"error": str(e)}), 500
    @app.route("/api/tmdb/watch_providers")
    def api_tmdb_watch_providers():
        """Fetch the list of watch providers for tv/movie from TMDB."""
        import requests as _req
        from ..db import get_setting
        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "No TMDB API Key"}), 400

        media_type = request.args.get("type", "tv")
        if media_type not in ("tv", "movie"):
            media_type = "tv"
        watch_region = request.args.get("watch_region", "").strip()

        _ui_lang = session.get("ui_language", "de")
        _tmdb_lang = "en-US" if _ui_lang == "en" else "de-DE"
        url = f"https://api.themoviedb.org/3/watch/providers/{media_type}?language={_tmdb_lang}&api_key={api_key}"
        if watch_region:
            url += f"&watch_region={watch_region}"
        try:
            resp = _req.get(url, headers={"accept": "application/json"}, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            results.sort(key=lambda p: p.get("display_priority", 9999))
            return jsonify({"results": results})
        except Exception as e:
            logger.error(f"Error fetching TMDB watch providers: {e}")
            return jsonify({"error": str(e)}), 500
    @app.route("/api/tmdb/discover")
    def api_tmdb_discover():
        """Search TMDB discover API with given params."""
        import requests as _req
        from ..db import get_setting
        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "No TMDB API Key"}), 400
            
        params = request.args.to_dict()
        media_type = params.pop("type", "tv")
        if media_type not in ["tv", "movie"]:
            media_type = "tv"
            
        import urllib.parse
        args = dict(request.args)
        args["api_key"] = api_key
        args.pop("type", None)
        
        # Map sorting key for TV shows / movies since TMDB uses different release date keys
        if "sort_by" in args:
            sort_val = args["sort_by"]
            if isinstance(sort_val, list):
                sort_val = sort_val[0] if sort_val else ""
            
            if isinstance(sort_val, str):
                if media_type == "tv" and sort_val.startswith("primary_release_date"):
                    args["sort_by"] = sort_val.replace("primary_release_date", "first_air_date")
                elif media_type == "movie" and sort_val.startswith("first_air_date"):
                    args["sort_by"] = sort_val.replace("first_air_date", "primary_release_date")
                    
        qs = urllib.parse.urlencode(args, doseq=True)
        url = f"https://api.themoviedb.org/3/discover/{media_type}?{qs}"
        logger.info(f"Discovering on TMDB: /discover/{media_type} (params redacted)")
        try:
            resp = _req.get(url, headers={"accept": "application/json"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return jsonify(data)
        except Exception as e:
            logger.error(f"Error discovering on TMDB: {e}")
            return jsonify({"error": str(e)}), 500
    @app.route("/api/tmdb/details")
    def api_tmdb_details():
        """Fetch details for a specific TMDB item (e.g., to get number of seasons)."""
        import requests as _req
        from ..db import get_setting
        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "No TMDB API Key"}), 400
            
        tmdb_id = request.args.get("id")
        media_type = request.args.get("type", "tv")
        
        if not tmdb_id or media_type not in ["tv", "movie"]:
            return jsonify({"error": "Invalid params"}), 400
            
        _ui_lang = session.get("ui_language", "de")
        _tmdb_lang = "en" if _ui_lang == "en" else "de"
        url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?language={_tmdb_lang}&api_key={api_key}&append_to_response=translations"
        try:
            resp = _req.get(url, headers={"accept": "application/json"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return jsonify(data)
        except Exception as e:
            logger.error(f"Error fetching TMDB details: {e}")
            return jsonify({"error": str(e)}), 500
    @app.route("/api/series")
    def api_series():
        """Fetch series/movie metadata (title, poster, description, genres) for a URL.

        GET /api/series. Called from app.js's loadPoster() and
        openSeries(), and from seerr.js, when a search result is opened.
        Localizes title/description/genres via TMDB when an API key is
        configured.
        """
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400

        # FilmPalast: use episode object directly as metadata source (no series class)
        if _is_filmpalast_url(url):
            try:
                from ...models.filmpalast_to.episode import FilmPalastEpisode
                ep = FilmPalastEpisode(url=url)
                poster = ep.image_url
                if poster and poster.startswith("/"):
                    poster = f"https://filmpalast.to{poster}"
                
                title = ep.title_de or ""
                description = ep.description or ""
                genres = ep.genres or []
                
                from ..db import get_setting
                api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
                if api_key:
                    try:
                        country = get_setting("cineinfo_country", "DE")
                        ui_lang = session.get("ui_language", "de")
                        tmdb_data = _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
                        if tmdb_data.get("found"):
                            if tmdb_data.get("title_confident"):
                                title = tmdb_data.get("title") or title
                            description = tmdb_data.get("overview") or description
                            if tmdb_data.get("genres"):
                                genres = tmdb_data.get("genres")
                    except Exception as _tmdb_exc:
                        logger.debug("[api_series] TMDB localization failed for FilmPalast: %s", _tmdb_exc)

                return jsonify({
                    "title": title,
                    "poster_url": _poster_proxy(poster),
                    "description": description,
                    "genres": genres,
                    "release_year": str(ep.release_year) if ep.release_year else "",
                    "is_movie": True,
                    "available_providers": ep.available_providers,
                })
            except Exception as e:
                logger.error(f"FilmPalast series fetch failed: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        # MegaKino (movie or series) — the /watch URL is shared; the JSON API's
        # "tv" field decides the type.
        if _is_megakino_url(url):
            try:
                _mk_data = _megakino_watch(url)
                from ..db import get_setting
                api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
                _country = get_setting("cineinfo_country", "DE")
                _ui_lang = session.get("ui_language", "de")

                if _megakino_is_series(_mk_data):
                    from ...models.megakino_to.series import MegakinoSeries
                    series = MegakinoSeries(url=url, _data=_mk_data)
                    title = _html_unescape(series.title)
                    description = series.description or ""
                    genres = series.genres or []
                    poster = series.poster_url
                    imdb_id = series.imdb or None
                    if api_key:
                        try:
                            tmdb_data = _tmdb_lookup_cached(title, imdb_id, api_key, _country, _ui_lang)
                            if tmdb_data.get("found"):
                                if tmdb_data.get("title_confident"):
                                    title = tmdb_data.get("title") or title
                                description = tmdb_data.get("overview") or description
                                if tmdb_data.get("genres"):
                                    genres = tmdb_data.get("genres")
                        except Exception as _tmdb_exc:
                            logger.debug("[api_series] TMDB localization failed for MegaKino series: %s", _tmdb_exc)
                    return jsonify({
                        "title": title,
                        "poster_url": _poster_proxy(poster),
                        "description": description,
                        "genres": genres,
                        "release_year": series.release_year or "",
                        "imdb_id": imdb_id,
                    })

                from ...models.megakino_to.movie import MegakinoMovie
                mv = MegakinoMovie(url=url, _data=_mk_data)
                title = mv.title_de or ""
                description = mv.description or ""
                genres = mv.genres or []
                poster = mv.image_url
                imdb_id = mv.imdb or None
                if api_key:
                    try:
                        tmdb_data = _tmdb_lookup_cached(title, imdb_id, api_key, _country, _ui_lang)
                        if tmdb_data.get("found"):
                            if tmdb_data.get("title_confident"):
                                title = tmdb_data.get("title") or title
                            description = tmdb_data.get("overview") or description
                            if tmdb_data.get("genres"):
                                genres = tmdb_data.get("genres")
                    except Exception as _tmdb_exc:
                        logger.debug("[api_series] TMDB localization failed for MegaKino movie: %s", _tmdb_exc)
                return jsonify({
                    "title": title,
                    "poster_url": _poster_proxy(poster),
                    "description": description,
                    "genres": genres,
                    "release_year": str(mv.release_year) if mv.release_year else "",
                    "is_movie": True,
                    "available_providers": mv.available_providers,
                })
            except Exception as e:
                _cls = e.__class__.__name__.lower()
                if any(k in _cls for k in ("connection", "timeout", "protocol", "ssl")):
                    logger.warning("MegaKino nicht erreichbar (Netzwerk): %s", e)
                    return jsonify({"error": "MegaKino ist gerade nicht erreichbar"}), 502
                logger.error(f"MegaKino series/movie fetch failed: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500
        try:
            prov = resolve_provider(url)
            series = prov.series_cls(url=url)
            poster = getattr(series, "poster_url", None)
            # s.to returns relative poster paths - make them absolute
            if poster and poster.startswith("/"):
                from urllib.parse import urlparse

                parsed = urlparse(url)
                poster = f"{parsed.scheme}://{parsed.netloc}{poster}"
                
            title = _html_unescape(series.title)
            description = getattr(series, "description", "")
            genres = getattr(series, "genres", [])
            imdb_id = getattr(series, "imdb", None) or None
            
            from ..db import get_setting
            api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
            if api_key:
                try:
                    country = get_setting("cineinfo_country", "DE")
                    ui_lang = session.get("ui_language", "de")
                    tmdb_data = _tmdb_lookup_cached(title, imdb_id, api_key, country, ui_lang)
                    if tmdb_data.get("found"):
                        # Only adopt the TMDB title when it is a confident match,
                        # otherwise keep the original site title (avoids wrong
                        # matches like a spin-off overriding the real name).
                        if tmdb_data.get("title_confident"):
                            title = tmdb_data.get("title") or title
                        description = tmdb_data.get("overview") or description
                        if tmdb_data.get("genres"):
                            genres = tmdb_data.get("genres")
                except Exception as _tmdb_exc:
                    logger.debug("[api_series] TMDB localization failed: %s", _tmdb_exc)

            return jsonify(
                {
                    "title": title,
                    "poster_url": _poster_proxy(poster),
                    "description": description,
                    "genres": genres,
                    "release_year": getattr(series, "release_year", ""),
                    "imdb_id": imdb_id,
                }
            )
        except Exception as e:
            logger.error(f"Series fetch failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500
    @app.route("/api/seasons")
    def api_seasons():
        """List the seasons available for a series/movie URL.

        GET /api/seasons. Called from app.js's openSeries() and
        autosync_filter.js, and from seerr.js, to populate the season
        picker. Movies are represented as a single fake season.
        """
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400

        # FilmPalast: return a single fake "season 1 / episode 1 = the movie itself"
        if _is_filmpalast_url(url):
            return jsonify({"seasons": [{"url": url, "season_number": 1, "episode_count": 1, "are_movies": True, "is_single_movie": True}]})

        # MegaKino: movie -> single fake season; series -> the one season post
        if _is_megakino_url(url):
            try:
                _mk_data = _megakino_watch(url)
                if not _megakino_is_series(_mk_data):
                    return jsonify({"seasons": [{"url": url, "season_number": 1, "episode_count": 1, "are_movies": True, "is_single_movie": True}]})
                from ...models.megakino_to.series import MegakinoSeries
                series = MegakinoSeries(url=url, _data=_mk_data)
                seasons_data = []
                for season in series.seasons:
                    seasons_data.append({
                        "url": season.url,
                        "season_number": season.season_number,
                        "episode_count": season.episode_count,
                        "are_movies": False,
                    })
                return jsonify({"seasons": seasons_data})
            except Exception as e:
                logger.error(f"MegaKino seasons fetch failed: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        try:
            prov = resolve_provider(url)
            series = prov.series_cls(url=url)
            seasons_data = []
            for season in series.seasons:
                seasons_data.append(
                    {
                        "url": season.url,
                        "season_number": season.season_number,
                        "episode_count": season.episode_count,
                        "are_movies": getattr(season, "are_movies", False),
                    }
                )
            return jsonify({"seasons": seasons_data})
        except Exception as e:
            logger.error(f"Seasons fetch failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500
    @app.route("/api/episodes")
    def api_episodes():
        """List episodes for a season/movie URL, with downloaded/language-flag detection.

        GET /api/episodes. Called from app.js's buildAccordion(),
        autosync_filter.js, and seerr.js, to populate the episode list
        once a season is selected.
        """
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400

        # FilmPalast: return the movie itself as a single episode entry
        if _is_filmpalast_url(url):
            try:
                from ...models.filmpalast_to.episode import FilmPalastEpisode
                ep = FilmPalastEpisode(url=url)
                return jsonify({"episodes": [{
                    "url": url,
                    "episode_number": 1,
                    "season_number": 1,
                    "title_de": ep.title_de or "",
                    "title_en": ep.title_de or "",
                    "downloaded": False,
                    "languages": ["German Dub"],
                }]})
            except Exception as e:
                logger.error(f"FilmPalast episodes fetch failed: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        # MegaKino: movie -> single episode; series -> all episodes of the season
        if _is_megakino_url(url):
            try:
                from pathlib import Path as _P
                _mk_data = _megakino_watch(url)
                if not _megakino_is_series(_mk_data):
                    from ...models.megakino_to.movie import MegakinoMovie
                    mv = MegakinoMovie(url=url, _data=_mk_data)
                    return jsonify({"episodes": [{
                        "url": url,
                        "episode_number": 1,
                        "season_number": 1,
                        "title_de": mv.title_de or "",
                        "title_en": mv.title_de or "",
                        "downloaded": False,
                        "languages": ["German Dub"],
                    }]})

                from ...models.megakino_to.series import MegakinoSeries
                series = MegakinoSeries(url=url, _data=_mk_data)
                season = series.seasons[0]
                sn = season.season_number

                # Downloaded detection via SxxExx filename scan
                downloaded_eps = set()
                try:
                    title_clean = (series.title_cleaned or "").lower()
                    if title_clean:
                        raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
                        dl_base = _P(raw).expanduser() if raw else (_P.home() / "Downloads")
                        if not dl_base.is_absolute():
                            dl_base = _P.home() / dl_base
                        roots = [dl_base]
                        for cp in get_custom_paths():
                            cpp = _P(cp["path"]).expanduser()
                            if not cpp.is_absolute():
                                cpp = _P.home() / cpp
                            roots.append(cpp)
                        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
                        lang_folders = LANG_FOLDERS
                        ep_re = re.compile(r"S(\d{2})E(\d{2,3})", re.IGNORECASE)
                        bases = []
                        for root in roots:
                            bases.extend([root / lf for lf in lang_folders] if lang_sep else [root])
                        for base in bases:
                            if not base.is_dir():
                                continue
                            for folder in base.iterdir():
                                if folder.is_dir() and folder.name.lower().startswith(title_clean):
                                    for f in folder.rglob("*"):
                                        if f.is_file():
                                            mm = ep_re.search(f.name)
                                            if mm:
                                                downloaded_eps.add((int(mm.group(1)), int(mm.group(2))))
                except Exception:
                    pass

                episodes_data = []
                for ep in season.episodes:
                    episodes_data.append({
                        "url": ep.url,
                        "episode_number": ep.episode_number,
                        "title_de": ep.title_de or "",
                        "title_en": ep.title_en or "",
                        "downloaded": (sn, ep.episode_number) in downloaded_eps,
                        "languages": ["German Dub"],
                    })
                return jsonify({"episodes": episodes_data})
            except Exception as e:
                logger.error(f"MegaKino episodes fetch failed: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        # hanime: list the franchise's episodes (single "Japanese Dub" language)
        if _is_hanime_url(url):
            try:
                from pathlib import Path as _P
                from ...models.hanime_tv.series import HanimeSeries
                series = HanimeSeries(url=url)
                season = series.seasons[0]
                sn = season.season_number
                downloaded_eps = set()
                try:
                    title_clean = (series.title_cleaned or "").lower()
                    if title_clean:
                        raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
                        dl_base = _P(raw).expanduser() if raw else (_P.home() / "Downloads")
                        if not dl_base.is_absolute():
                            dl_base = _P.home() / dl_base
                        roots = [dl_base]
                        for cp in get_custom_paths():
                            cpp = _P(cp["path"]).expanduser()
                            if not cpp.is_absolute():
                                cpp = _P.home() / cpp
                            roots.append(cpp)
                        ep_re = re.compile(r"S(\d{2})E(\d{2,3})", re.IGNORECASE)
                        # With language separation on, hanime lands in the
                        # "japanese-dub" subfolder, not in the root itself.
                        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
                        bases = []
                        for root in roots:
                            bases.extend([root / lf for lf in LANG_FOLDERS] if lang_sep else [root])
                        for base in bases:
                            if not base.is_dir():
                                continue
                            for folder in base.iterdir():
                                if folder.is_dir() and folder.name.lower().startswith(title_clean):
                                    for f in folder.rglob("*"):
                                        if f.is_file():
                                            mm = ep_re.search(f.name)
                                            if mm:
                                                downloaded_eps.add((int(mm.group(1)), int(mm.group(2))))
                except Exception:
                    pass
                episodes_data = []
                for ep in season.episodes:
                    episodes_data.append({
                        "url": ep.url,
                        "episode_number": ep.episode_number,
                        "title_de": ep.title_de or "",
                        "title_en": ep.title_en or "",
                        "downloaded": (sn, ep.episode_number) in downloaded_eps,
                        "languages": ["Japanese Dub"],
                    })
                return jsonify({"episodes": episodes_data})
            except Exception as e:
                logger.error(f"hanime episodes fetch failed: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        try:
            prov = resolve_provider(url)
            # Pass series to avoid broken series URL reconstruction in s.to
            # season model (its fallback splits on "-" which fails)
            series_url = re.sub(r"/staffel-\d+/?$", "", url)
            series_url = re.sub(r"/filme/?$", "", series_url)
            try:
                series = prov.series_cls(url=series_url)
            except Exception:
                series = None
            season = prov.season_cls(url=url, series=series)

            # Scan download directory for downloaded episodes.
            # Uses S##E### filename matching so it works regardless of
            # which NAMING_TEMPLATE was active when files were downloaded.
            from pathlib import Path

            lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
            lang_folders = LANG_FOLDERS

            raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
            if raw:
                dl_base = Path(raw).expanduser()
                if not dl_base.is_absolute():
                    dl_base = Path.home() / dl_base
            else:
                dl_base = Path.home() / "Downloads"

            # Collect all scan roots: default + custom paths
            scan_roots = [dl_base]
            for cp in get_custom_paths():
                cp_path = Path(cp["path"]).expanduser()
                if not cp_path.is_absolute():
                    cp_path = Path.home() / cp_path
                scan_roots.append(cp_path)

            # Build set of (season_num, episode_num) found on disk
            downloaded_eps = set()
            try:
                title_clean = ""
                if series:
                    title_clean = (
                        getattr(series, "title_cleaned", None)
                        or getattr(series, "title", "")
                    ).lower()
                if title_clean:
                    ep_re = re.compile(r"S(\d{2})E(\d{2,3})", re.IGNORECASE)
                    all_bases = []
                    for root in scan_roots:
                        if lang_sep:
                            all_bases.extend([root / lf for lf in lang_folders])
                        else:
                            all_bases.append(root)
                    for base in all_bases:
                        if not base.is_dir():
                            continue
                        for folder in base.iterdir():
                            if (
                                not folder.is_dir()
                                or not folder.name.lower().startswith(title_clean)
                            ):
                                continue
                            for f in folder.rglob("*"):
                                if f.is_file():
                                    m = ep_re.search(f.name)
                                    if m:
                                        downloaded_eps.add(
                                            (int(m.group(1)), int(m.group(2)))
                                        )
            except Exception:
                pass

            # Parse language flags per episode from the already-fetched season HTML
            # (no extra network requests — flags are embedded in the season page)
            ep_languages: dict[str, list[str]] = {}
            try:
                s_html = getattr(season, "_html", None) or ""
                _is_sto = "serienstream" in url or "/serie/" in url

                if _is_sto:
                    # Determine actual base domain from the season URL
                    _sto_base = "https://serienstream.to" if "serienstream" in url else "https://s.to"
                    # s.to: <tr class="episode-row" onclick="window.location='/serie/.../episode-X'">
                    #         <td class="episode-language-cell"> <svg class="svg-flag-german"> ...
                    # Trailing quote prevents svg-flag-german matching svg-flag-english-german
                    _sto_flag_map = {
                        'svg-flag-german':          "German Dub",
                        'svg-flag-english':         "English Dub",
                        'svg-flag-english-german':  "English Dub (German Sub)",
                        'svg-flag-english-english': "English Sub",
                    }
                    for _tr_m in re.finditer(r'<tr[^>]+class="episode-row[^"]*"[^>]*onclick="[^"]*\'(/serie/[^\']+)\'"', s_html):
                        _ep_path = _tr_m.group(1)
                        _ep_url = _sto_base + _ep_path
                        _tr_end = s_html.find("</tr>", _tr_m.start())
                        _tr_chunk = s_html[_tr_m.start():_tr_end]
                        _flag_classes = re.findall(r'svg-flag-[\w-]+', _tr_chunk)
                        _langs = []
                        for _cls in _flag_classes:
                            lbl = _sto_flag_map.get(_cls)
                            if lbl and lbl not in _langs:
                                _langs.append(lbl)
                        ep_languages[_ep_url] = _langs
                else:
                    # AniWorld: <td class="editFunctions"> with <img src=".../german.svg">
                    _flag_map = {
                        "/german.svg":           "German Dub",
                        "/japanese-german.svg":  "German Sub",
                        "/japanese-english.svg": "English Sub",
                        "/english.svg":          "English Dub",
                        "/english-german.svg":   "English Dub (German Sub)",
                    }
                    _marker = 'itemtype="http://schema.org/Episode"'
                    _pos = 0
                    while True:
                        _pos = s_html.find(_marker, _pos)
                        if _pos == -1:
                            break
                        _tr_s = s_html.rfind("<tr", 0, _pos)
                        _tr_e = s_html.find("</tr>", _pos)
                        if _tr_s == -1 or _tr_e == -1:
                            break
                        _tr = s_html[_tr_s:_tr_e]
                        _ep_url = None
                        _up = _tr.find('itemprop="url"')
                        if _up != -1:
                            _hs = _tr.find('href="', _up) + 6
                            _he = _tr.find('"', _hs)
                            _href = _tr[_hs:_he]
                            _ep_url = ("https://aniworld.to" + _href) if _href.startswith("/") else _href
                        if not _ep_url:
                            _hp = _tr.find("film-")
                            if _hp != -1:
                                _hs = _tr.rfind('href="', 0, _hp) + 6
                                _he = _tr.find('"', _hs)
                                _href = _tr[_hs:_he]
                                _ep_url = ("https://aniworld.to" + _href) if _href.startswith("/") else _href
                        if _ep_url:
                            _ed = _tr.find('class="editFunctions"')
                            if _ed != -1:
                                _ee = _tr.find("</td>", _ed)
                                _edit = _tr[_ed:_ee]
                                _langs = [lbl for src, lbl in _flag_map.items() if src in _edit]
                                ep_languages[_ep_url] = _langs
                        _pos = _tr_e
            except Exception as _lang_exc:
                logger.debug("[api_episodes] language flag parsing failed: %s", _lang_exc)

            episodes_data = []
            for ep in season.episodes:
                downloaded = (
                    ep.season.season_number,
                    ep.episode_number,
                ) in downloaded_eps

                episodes_data.append(
                    {
                        "url": ep.url,
                        "episode_number": ep.episode_number,
                        "title_de": getattr(ep, "title_de", ""),
                        "title_en": getattr(ep, "title_en", ""),
                        "downloaded": downloaded,
                        "languages": ep_languages.get(ep.url, []),
                    }
                )
            return jsonify({"episodes": episodes_data})
        except Exception as e:
            logger.error(f"Episodes fetch failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500
    @app.route("/api/providers")
    def api_providers():
        """List available hoster providers per language for an episode URL.

        GET /api/providers. Called from app.js's fetchProviders() and
        seerr.js, to populate the provider dropdown once an episode is
        selected. Runs a live-availability check for movies only.
        """
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400

        # hanime: no third-party hoster and one stream -> a single pseudo provider.
        if _is_hanime_url(url):
            return jsonify({"providers": {"Japanese Dub": ["hanime"]}})

        # MegaKino: single German language, direct hoster embeds (VOE etc.)
        if _is_megakino_url(url):
            try:
                from ...models.megakino_to.movie import MegakinoMovie
                prov = resolve_provider(url)
                ep = prov.episode_cls(url=url)
                pd = ep.provider_data  # {"German Dub": {hoster: embed_url}}
                # Live-availability check + mirror de-dup for every label, movies
                # and series alike (MegaKino posts are user-edited and often keep
                # dead/removed hoster embeds listed).
                provider_info = {}
                for label, hosters in pd.items():
                    working = _filter_and_dedup_providers(hosters)
                    if working:
                        provider_info[label] = working
                return jsonify({"providers": provider_info})
            except Exception as e:
                logger.error(f"MegaKino providers fetch failed: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        try:
            prov = resolve_provider(url)
            episode = prov.episode_cls(url=url)
            pd = episode.provider_data

            disable_eng_sub = os.environ.get("MEDIAFORGE_DISABLE_ENGLISH_SUB", "0") == "1"
            provider_info = {}

            # Normalize any provider_data format into {label: {name: url}}
            # so the availability check runs once, uniformly for all sites.
            raw_by_label = {}

            if isinstance(pd, list):
                # FilmPalast: [{"name": "VOE HD", "url": "..."}, ...]
                def _norm_fp(n):
                    n_clean = n.replace(" HD", "").replace(" HQ", "").strip()
                    for wp in WORKING_PROVIDERS:
                        if wp.lower() == n_clean.lower():
                            return wp
                    return n_clean
                raw_by_label["German Dub"] = {
                    _norm_fp(p["name"]): p["url"]
                    for p in pd
                    if p.get("name") and p.get("url")
                }
            elif hasattr(pd, "_data"):
                # AniWorld: ProviderData object
                lang_tuple_to_label = {
                    (audio.value, subtitles.value): LANG_LABELS.get(key)
                    for key, (audio, subtitles) in LANG_KEY_MAP.items()
                    if LANG_LABELS.get(key)
                }
                for (audio, subtitles), providers in pd._data.items():
                    label = lang_tuple_to_label.get((audio.value, subtitles.value))
                    if not label or (disable_eng_sub and label == "English Sub"):
                        continue
                    raw_by_label[label] = dict(providers)
            else:
                # s.to: plain dict with (Audio, Subtitles) enum tuple keys
                sto_label_map = {
                    ("German", "None"): "German Dub",
                    ("English", "None"): "English Dub",
                }
                for (audio, subtitles), providers in pd.items():
                    label = sto_label_map.get((audio.value, subtitles.value))
                    if label:
                        raw_by_label[label] = dict(providers)

            # Single unified live-availability check + mirror de-dup for every
            # label / site, movies and series alike (previously movies-only).
            # This is what keeps dead hosters and duplicate mirror labels out of
            # the provider dropdown for AniWorld/s.to series too.
            for label, providers in raw_by_label.items():
                working = _filter_and_dedup_providers(providers)
                if working:
                    provider_info[label] = working

            return jsonify({"providers": provider_info})
        except Exception as e:
            logger.error(f"Providers fetch failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500
    @app.route("/api/veev/check", methods=["POST"])
    def api_veev_check():
        """Check whether a VeeV-hosted episode is actually available for streaming.

        The VeeV CDN sometimes serves a placeholder / offline page instead of
        real video.  We detect this by launching the same headless-browser session
        that the downloader uses and checking whether a 206-response CDN URL is
        captured.  If yes → available, if not → unavailable.
        """
        data = request.get_json(silent=True) or {}
        episode_url = (data.get("episode_url") or "").strip()
        if not episode_url:
            return jsonify({"available": False, "error": "episode_url fehlt"}), 400

        try:
            from ...models.filmpalast_to.episode import FilmPalastEpisode
            from ...extractors.provider.veev import _extract_veev_details
        except ImportError:
            try:
                from mediaforge.models.filmpalast_to.episode import FilmPalastEpisode
                from mediaforge.extractors.provider.veev import _extract_veev_details
            except ImportError as ie:
                return jsonify({"available": False, "error": f"Import-Fehler: {ie}"}), 500

        try:
            ep = FilmPalastEpisode(episode_url, selected_provider="VeeV")
            embed_url = ep.provider_url
        except Exception as e:
            return jsonify({"available": False, "error": f"Episode konnte nicht aufgelöst werden: {e}"})

        try:
            cdn_url, _, _ = _extract_veev_details(embed_url, timeout_ms=30_000)
        except Exception as e:
            return jsonify({"available": False, "error": f"Veev-Prüfung fehlgeschlagen: {e}"})

        if cdn_url:
            return jsonify({"available": True})
        return jsonify({
            "available": False,
            "error": "Dieser Film ist auf Veev momentan nicht verfügbar (kein Stream gefunden).",
        })
    @app.route("/advanced-search")
    def advanced_search_page():
        """Render the advanced (TMDB discover) search page, if the feature is enabled.

        GET /advanced-search. Linked from templates/base.html's nav.
        Redirects to the index page when the "cineinfo_advanced_search"
        setting is off.
        """
        from ..db import get_setting
        if get_setting("cineinfo_advanced_search", "0") != "1":
            from flask import redirect, url_for
            return redirect(url_for("index"))
        sto_lang_labels = {"1": "German Dub", "2": "English Dub", "3": "English Dub (German Sub)"}
        return render_template(
            "advanced_search.html",
            lang_labels=LANG_LABELS,
            sto_lang_labels=sto_lang_labels,
            supported_providers=WORKING_PROVIDERS,
        )
    @app.route("/api/tmdb/info")
    def api_tmdb_info():
        """Look up a single title/imdb_id against TMDB (cached).

        GET /api/tmdb/info. Called from app.js and seerr.js when TMDB
        localization data is needed for a single title (batch requests use
        /api/tmdb/batch instead).
        """
        title   = (request.args.get("title")   or "").strip()
        imdb_id = (request.args.get("imdb_id") or "").strip() or None
        if not title and not imdb_id:
            return jsonify({"error": "title or imdb_id required"}), 400
        api_key = get_setting("cineinfo_tmdb_api_key", "")
        if not api_key:
            return jsonify({"found": False, "reason": "no_key"})
        country  = get_setting("cineinfo_country", "DE")
        ui_lang  = session.get("ui_language", "de")
        return jsonify(_tmdb_lookup_cached(title, imdb_id, api_key, country, ui_lang))
    @app.route("/api/tmdb/batch", methods=["POST"])
    def api_tmdb_batch():
        """Fetch TMDB data for multiple titles in one request.

        Accepts: {"titles": ["Title A", "Title B", ...]}  (max 25)
        Returns: {"Title A": {found, ...}, "Title B": {found, ...}}

        Internally uses a thread pool (max 3 workers) so lookups for
        already-cached titles complete instantly while cold ones are
        fetched concurrently. All HTTP calls to TMDB still go through
        the shared rate-limiter, so we never exceed 40 req/s globally.
        """
        import concurrent.futures as _cf
        data = request.get_json(silent=True) or {}
        raw_titles = data.get("titles") or []
        titles = [str(t).strip() for t in raw_titles if t][:25]  # cap at 25
        if not titles:
            return jsonify({})
        api_key = get_setting("cineinfo_tmdb_api_key", "")
        if not api_key:
            return jsonify({t: {"found": False, "reason": "no_key"} for t in titles})
        country  = get_setting("cineinfo_country", "DE")
        ui_lang  = session.get("ui_language", "de")

        results = {}
        # max_workers=3 limits concurrent lookups; the rate limiter inside
        # _tmdb_lookup_cached serialises the actual TMDB HTTP calls globally.
        with _cf.ThreadPoolExecutor(max_workers=3, thread_name_prefix="tmdb-batch") as pool:
            future_to_title = {
                pool.submit(_tmdb_lookup_cached, t, None, api_key, country, ui_lang): t
                for t in titles
            }
            for fut in _cf.as_completed(future_to_title, timeout=35):
                t = future_to_title[fut]
                try:
                    results[t] = fut.result()
                except Exception as exc:
                    logger.debug("[CineInfo] batch lookup failed for %r: %s", t, exc)
                    results[t] = {"found": False}
        return jsonify(results)
    @app.route("/api/tmdb/cache/clear", methods=["POST"])
    def api_tmdb_cache_clear():
        """Clear all CineInfo/TMDB cached data and trigger a fresh prefetch cycle.

        Steps:
          1. Wipe the SQLite tmdb_cache table (24h persistent data)
          2. Clear the in-memory browse cache so the next browse request
             re-attaches TMDB data from scratch
          3. Kick off a new prefetch cycle in the background so data is
             warmed up without making the caller wait
        """
        clear_tmdb_cache()
        _browse_cache.clear()   # force re-evaluation of inline TMDB data
        # Also drop the Fernsehserien and Crunchyroll provider caches — they're
        # part of the same "Cache Options" section in the UI and now use the
        # same persistent (SQLite) caching mechanism as TMDB.
        try:
            from .. import fernsehserien_service
            fernsehserien_service.invalidate_cache()
        except Exception:
            logger.debug("[Fernsehserien] could not invalidate cache", exc_info=True)
        try:
            from .. import crunchyroll_service
            crunchyroll_service.invalidate_availability_cache()
        except Exception:
            logger.debug("[Crunchyroll] could not invalidate cache", exc_info=True)
        # Start a fresh prefetch in background — returns immediately to caller
        threading.Thread(
            target=_prefetch_cycle,
            daemon=True,
            name="cineinfo-manual-refresh",
        ).start()
        logger.info("[CineInfo] Cache manually cleared — prefetch triggered")
        return jsonify({"ok": True, "message": "Cache geleert, Neuladen gestartet"})
