"""Shared TMDB (The Movie Database) lookup cache and rate limiter.

Wraps the TMDB REST API with a SQLite-backed cache (see ``.db``), an
in-process token-bucket rate limiter, and in-flight request deduplication
so concurrent requests for the same title don't hammer TMDB.

Used by: web/routes/search.py, web/routes/calendar_routes.py and
web/routes/browse.py (prefetch) — kept as one shared module instead of
being duplicated across those route files.
"""
import threading
import time

import requests as _rq_tmdb

from .db import get_tmdb_cache, set_tmdb_cache
from .autosync_worker import _title_is_confident
from ..logger import get_logger

logger = get_logger(__name__)

class _TmdbRateLimiter:
    """Simple thread-safe token-bucket limiter capping calls to ``rate`` req/s.

    Tokens refill continuously based on elapsed wall-clock time; ``acquire()``
    blocks (sleeps) the calling thread just long enough for a token to become
    available. Shared process-wide via the single ``_tmdb_rl`` instance below.
    """

    def __init__(self, rate=3.0):
        self._rate = rate
        self._tokens = float(rate)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                wait = 0.0
            else:
                wait = (1.0 - self._tokens) / self._rate
                self._tokens = 0.0
        if wait > 0:
            time.sleep(wait)

# Global limiter shared by every TMDB call in this module (40 req/s cap).
_tmdb_rl = _TmdbRateLimiter(rate=40.0)

# In-flight deduplication — prevents duplicate concurrent TMDB lookups
# for the same cache_key (e.g. two cards with the same title loading at once).
_tmdb_inflight: dict = {}
_tmdb_inflight_lock = threading.Lock()

def _tmdb_lookup_cached(title, imdb_id, api_key, country, ui_lang="de"):
    """
    Look up TMDB data for a title/IMDB-ID and cache in SQLite for 24 h.
    Lookup order:
      1. SQLite cache hit (checks BOTH the imdb_id key AND the title key)
      2. /find/{imdb_id}?external_source=imdb_id  (if imdb_id given)
      3. /search/multi?query={title}              (fallback)
    Results are stored under both keys so card (title) and modal (imdb_id)
    lookups always find the same cached entry.
    Returns dict with keys: found, tmdb_id, media_type, genres, providers, fsk

    Used by: web/routes/search.py, web/routes/calendar_routes.py and
    web/routes/browse.py (prefetch).
    """
    _lang_suffix = "|||" + ui_lang
    imdb_key  = (imdb_id + "|||" + country + _lang_suffix) if imdb_id else None
    title_key = (title   + "|||" + country + _lang_suffix) if title   else None

    # Check both cache keys — whichever was written first wins.
    # get_tmdb_cache() already enforces the 24h TTL (returns None once stale),
    # so any non-None row here is fresh and must be treated as a real hit —
    # no live re-fetch. Older rows cached before trailer_key/recommendations/
    # title/overview/title_confident existed just get defaults filled in
    # below instead of forcing a live lookup; they self-heal for free the
    # next time this key's TTL naturally expires and gets re-fetched.
    for ck in filter(None, [imdb_key, title_key]):
        cached = get_tmdb_cache(ck)
        if cached is not None:
            cached.setdefault("trailer_key", "")
            cached.setdefault("recommendations", [])
            cached.setdefault("title", "")
            cached.setdefault("overview", "")
            cached.setdefault("title_confident", False)
            # Warm the other key so next call is also a hit
            other = title_key if ck == imdb_key else imdb_key
            if other and get_tmdb_cache(other) is None:
                set_tmdb_cache(other, cached)
            return cached

    cache_key = imdb_key or title_key  # primary key for the fresh lookup

    # In-flight deduplication: if another thread is already fetching this
    # title, wait for it to finish then return whatever it cached.
    with _tmdb_inflight_lock:
        inflight_ev = _tmdb_inflight.get(cache_key)
        if inflight_ev is None:
            my_ev = threading.Event()
            _tmdb_inflight[cache_key] = my_ev
        else:
            my_ev = None  # we are the waiter

    if inflight_ev is not None:
        # Another thread is doing the lookup — poll the cache in short intervals
        # so we return as soon as the leader writes the result, up to 30 s total.
        _deadline = time.time() + 30
        while time.time() < _deadline:
            _remaining = max(0.0, _deadline - time.time())
            inflight_ev.wait(timeout=min(2.0, _remaining))
            for ck in filter(None, [imdb_key, title_key]):
                cached = get_tmdb_cache(ck)
                if cached is not None and "trailer_key" in cached and "recommendations" in cached:
                    return cached
            if inflight_ev.is_set():
                break  # leader finished but nothing in cache — don't keep waiting
        return {"found": False}

    def _call(path, extra=None):
        lang = "en-US" if ui_lang == "en" else "de-DE"
        params = {"api_key": api_key}
        if extra is None or "language" not in extra:
            params["language"] = lang
        if extra:
            params.update(extra)
        _tmdb_rl.acquire()  # respect the global rate limit (40 req/s)
        r = _rq_tmdb.get(
            "https://api.themoviedb.org/3" + path,
            params=params, timeout=8,
            headers={"User-Agent": "MediaForge/1.0"},
        )
        r.raise_for_status()
        return r.json()

    try:
        tid = None
        mt  = None
        # 1. Direct IMDB-ID lookup (most accurate)
        if imdb_id:
            try:
                find_data = _call("/find/" + imdb_id, {"external_source": "imdb_id"})
                for media_type, key in (("tv", "tv_results"), ("movie", "movie_results")):
                    hits = find_data.get(key, [])
                    if hits:
                        tid = hits[0]["id"]
                        mt  = media_type
                        break
            except Exception as _fe:
                logger.debug("[CineInfo] /find by imdb_id %r failed: %s", imdb_id, _fe)
        # 2. Fall back to title search
        if tid is None:
            if not title:
                out = {"found": False}
                set_tmdb_cache(cache_key, out)
                return out
            search = _call("/search/multi", {"query": title})
            results = [r for r in search.get("results", []) if r.get("media_type") in ("movie", "tv")]
            
            # Fallback 1: Try without single quotes/apostrophes (e.g. "I'll" -> "Ill" or "I ll")
            if not results:
                import re
                # Sometimes TMDB prefers the word without the apostrophe entirely, or with a space.
                # We'll try removing them first.
                clean_title = re.sub(r"['’´`]", "", title)
                if clean_title != title:
                    search = _call("/search/multi", {"query": clean_title})
                    results = [r for r in search.get("results", []) if r.get("media_type") in ("movie", "tv")]
            
            # Fallback 2: Try removing other special punctuation that might differ (like '!', '?', ':')
            if not results:
                clean_title_2 = re.sub(r"[!\?:;]", "", title)
                if clean_title_2 != title:
                    search = _call("/search/multi", {"query": clean_title_2})
                    results = [r for r in search.get("results", []) if r.get("media_type") in ("movie", "tv")]
                    
            # Fallback 3: Try removing (Year) tags like (2026)
            if not results:
                clean_title_3 = re.sub(r"\s*\(\d{4}\)", "", title).strip()
                if clean_title_3 != title:
                    search = _call("/search/multi", {"query": clean_title_3})
                    results = [r for r in search.get("results", []) if r.get("media_type") in ("movie", "tv")]
                    
            # Fallback 4: Split by hyphen and use first part (e.g. "Occupied - Die Besatzung" -> "Occupied")
            if not results and ("-" in title or "–" in title):
                clean_title_4 = re.split(r"[-–]", title)[0].strip()
                if clean_title_4 and clean_title_4 != title:
                    search = _call("/search/multi", {"query": clean_title_4})
                    results = [r for r in search.get("results", []) if r.get("media_type") in ("movie", "tv")]

            if not results:
                out = {"found": False}
                set_tmdb_cache(cache_key, out)
                return out
            best = results[0]
            tid  = best["id"]
            mt   = best["media_type"]
        # 3. Fetch details, watch-providers, FSK
        details  = _call("/" + mt + "/" + str(tid), {"append_to_response": "credits,external_ids"})
        genres   = [g["name"] for g in details.get("genres", [])]
        wp_data  = _call("/" + mt + "/" + str(tid) + "/watch/providers")
        c_data   = wp_data.get("results", {}).get(country, {})
        flatrate = [p["provider_name"] for p in c_data.get("flatrate", [])]
        buy_list = [p["provider_name"] for p in c_data.get("buy", [])]
        rent_list= [p["provider_name"] for p in c_data.get("rent", [])]
        providers = flatrate[:]
        for p in buy_list + rent_list:
            if p not in providers:
                providers.append(p)
        fsk = ""
        try:
            if mt == "tv":
                cr = _call("/tv/" + str(tid) + "/content_ratings")
                for r in cr.get("results", []):
                    if r.get("iso_3166_1") == country:
                        fsk = r.get("rating", "")
                        break
            else:
                rd = _call("/movie/" + str(tid) + "/release_dates")
                for entry in rd.get("results", []):
                    if entry.get("iso_3166_1") == country:
                        for rdate in entry.get("release_dates", []):
                            c = rdate.get("certification", "")
                            if c:
                                fsk = c
                                break
                        break
        except Exception:
            pass

        # 4. Fetch Trailers (Videos) - Fetch ALL languages first
        trailer_key = ""
        try:
            # Omit language to get all available videos
            videos = _call("/" + mt + "/" + str(tid) + "/videos", {"language": ""}) 
            results = videos.get("results", [])
            
            # Priority 1: German Trailer
            for v in results:
                if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("iso_639_1") == "de":
                    trailer_key = v.get("key")
                    break
            
            # Priority 2: English Trailer
            if not trailer_key:
                for v in results:
                    if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("iso_639_1") == "en":
                        trailer_key = v.get("key")
                        break
                        
            # Priority 3: Any Trailer
            if not trailer_key:
                for v in results:
                    if v.get("site") == "YouTube" and v.get("type") == "Trailer":
                        trailer_key = v.get("key")
                        break
        except Exception:
            pass

        if not trailer_key:
            try:
                videos = _call("/" + mt + "/" + str(tid) + "/videos", {"language": "en-US"})
                for v in videos.get("results", []):
                    if v.get("site") == "YouTube" and v.get("type") == "Trailer":
                        trailer_key = v.get("key", "")
                        break
            except Exception:
                pass

        # 5. Fetch Recommendations
        recommendations = []
        try:
            rec_data = _call("/" + mt + "/" + str(tid) + "/recommendations", {"page": 1})
            for r in rec_data.get("results", [])[:6]: # Max 6
                recommendations.append({
                    "id": r.get("id"),
                    "title": r.get("name") or r.get("title"),
                    "poster_path": r.get("poster_path"),
                    "vote_average": r.get("vote_average")
                })
        except Exception:
            pass

        _title_cands = [details.get("name"), details.get("original_name"),
                        details.get("title"), details.get("original_title")]
        out = {"found": True, "tmdb_id": tid, "media_type": mt,
               "title": details.get("name") or details.get("title") or "",
               "title_confident": _title_is_confident(title, _title_cands),
               "overview": details.get("overview") or "",
               "genres": genres, "providers": providers, "fsk": fsk,
               "vote_average": round(details.get("vote_average") or 0, 1),
               "trailer_key": trailer_key,
               "recommendations": recommendations,
               "raw_details": details}
        # Store under both keys so card (title) and modal (imdb_id) share the entry
        logger.info("[CineInfo] TMDB data for %r: trailer=%s, recs=%d", title, trailer_key, len(recommendations))
        for ck in filter(None, [imdb_key, title_key]):
            set_tmdb_cache(ck, out)
        return out
    except Exception as exc:
        logger.warning("[CineInfo] TMDB lookup failed for %r: %s", title or imdb_id, exc)
        return {"found": False}
    finally:
        # Always release the in-flight event so waiting threads wake up
        if my_ev is not None:
            with _tmdb_inflight_lock:
                _tmdb_inflight.pop(cache_key, None)
            my_ev.set()

def _tmdb_calendar_episodes(tmdb_id, api_key, ui_lang="de"):
    """Return {poster, title, episodes} of dated episodes around the currently
    airing season for a TV show. Results are cached for 6 h in tmdb_cache.

    Used by: web/routes/calendar_routes.py.
    """
    cache_key = f"calendar|||{tmdb_id}|||{ui_lang}"
    cached = get_tmdb_cache(cache_key, ttl=21600.0)  # 6 h
    if cached is not None:
        return cached

    lang = "en-US" if ui_lang == "en" else "de-DE"

    def _call(path):
        _tmdb_rl.acquire()  # respect the global rate limit (40 req/s)
        r = _rq_tmdb.get(
            "https://api.themoviedb.org/3" + path,
            params={"api_key": api_key, "language": lang}, timeout=8,
            headers={"User-Agent": "MediaForge/1.0"},
        )
        r.raise_for_status()
        return r.json()

    poster = None
    title = ""
    episodes = []
    try:
        details = _call("/tv/" + str(tmdb_id))
        poster = details.get("poster_path")
        title = details.get("name") or details.get("original_name") or ""
        # Collect the seasons referenced by the next/last aired episodes — these
        # are the ones holding the relevant past/future air dates.
        season_numbers = set()
        for ep in (details.get("next_episode_to_air"), details.get("last_episode_to_air")):
            if ep and ep.get("season_number") is not None:
                season_numbers.add(ep["season_number"])
        for sn in sorted(season_numbers):
            try:
                sdata = _call("/tv/" + str(tmdb_id) + "/season/" + str(sn))
            except Exception:
                continue
            for e in sdata.get("episodes", []):
                ad = e.get("air_date")
                if not ad:
                    continue
                episodes.append({
                    "season":   e.get("season_number"),
                    "episode":  e.get("episode_number"),
                    "name":     e.get("name") or "",
                    "air_date": ad,
                    "still":    e.get("still_path"),
                })
    except Exception as exc:
        logger.debug("[Calendar] episode lookup failed for tmdb %s: %s", tmdb_id, exc)

    out = {"poster": poster, "title": title, "episodes": episodes}
    set_tmdb_cache(cache_key, out)
    return out

def _tmdb_movie_release(tmdb_id, api_key, ui_lang="de"):
    """Return {poster, title, release_date} for a movie. Cached 6 h.

    Used by: web/routes/calendar_routes.py.
    """
    cache_key = f"calmovie|||{tmdb_id}|||{ui_lang}"
    cached = get_tmdb_cache(cache_key, ttl=21600.0)
    if cached is not None:
        return cached
    lang = "en-US" if ui_lang == "en" else "de-DE"
    out = {"poster": None, "title": "", "release_date": None}
    try:
        _tmdb_rl.acquire()
        r = _rq_tmdb.get(
            "https://api.themoviedb.org/3/movie/" + str(tmdb_id),
            params={"api_key": api_key, "language": lang}, timeout=8,
            headers={"User-Agent": "MediaForge/1.0"},
        )
        r.raise_for_status()
        d = r.json()
        out = {
            "poster": d.get("poster_path"),
            "title": d.get("title") or d.get("original_title") or "",
            "release_date": d.get("release_date") or None,
        }
    except Exception as exc:
        logger.debug("[Calendar] movie lookup failed for tmdb %s: %s", tmdb_id, exc)
    set_tmdb_cache(cache_key, out)
    return out
def _tmdb_fetch_season_and_episode(tmdb_id, season_num, episode_num, api_key, ui_lang='de'):
    '''Fetch detailed season and episode info from TMDB for NFO generation.'''
    lang = 'en-US' if ui_lang == 'en' else 'de-DE'
    out = {'season': None, 'episode': None}
    
    def _call(path):
        _tmdb_rl.acquire()
        r = _rq_tmdb.get(
            'https://api.themoviedb.org/3' + path,
            params={'api_key': api_key, 'language': lang, 'append_to_response': 'credits,external_ids,videos'}, timeout=8,
            headers={'User-Agent': 'MediaForge/1.0'},
        )
        if r.status_code == 200:
            return r.json()
        return None

    try:
        if season_num is not None:
            out['season'] = _call('/tv/' + str(tmdb_id) + '/season/' + str(season_num))
        if season_num is not None and episode_num is not None:
            out['episode'] = _call('/tv/' + str(tmdb_id) + '/season/' + str(season_num) + '/episode/' + str(episode_num))
    except Exception as exc:
        logger.debug('[TMDB] Fetch season/episode failed for %s (S%sE%s): %s', tmdb_id, season_num, episode_num, exc)
    return out

