"""Statistics routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ..db import add_media_ignores
from ..db import get_all_library_cache
from ..db import get_general_stats
from ..db import get_media_ignores
from ..db import get_queue_stats
from ..db import get_setting
from ..db import get_stats_trends
from ..db import get_sync_stats
from ...languages import ENGLISH_DUB
from ...languages import GERMAN_DUB
from ...languages import JAPANESE_DUB
from ...languages import LANG_FOLDER_MAP
from ...languages import LANG_PAIR_TO_LABEL
from ..db import remove_media_ignore
from ..runtime_state import SYNC_SCHEDULE_MAP
from .library import _lib_active_path_keys
from .library import _lib_build_scan_targets
from .library import _lib_trigger_scan_async
from flask import jsonify
from flask import render_template
from flask import request
import os
import os.path
import re
import threading
import time as _time


# Memoised media results.
#
# Walking the library cache costs ~400 ms on a 90k-file library, and both
# helpers were re-run on every /api/stats call -- including the background
# re-polls that fire every few seconds while a scan is running, and once per
# page of the duplicates modal. The walk only produces something new when a
# scan has finished, so the result is cached against a fingerprint of the
# cache rows (path_key + scanned_at + is_scanning) and recomputed only when
# that changes.
_MEDIA_MEMO = {"fp": None, "stats": None, "duplicates": None, "at": 0.0}
_MEDIA_MEMO_LOCK = threading.Lock()

# Minimum distance between two full recomputations. Statistics over a large
# library are expensive, and a library that is actively downloading changes its
# fingerprint constantly -- without this, every page load (and every modal
# page) would re-walk everything. Slightly stale numbers are fine here; the
# next request past the interval picks the change up.
_MEDIA_MEMO_MIN_INTERVAL = 60.0


def _cache_fingerprint(cache):
    """Cheap identity of the library cache: changes exactly when a scan does."""
    return tuple(sorted(
        (k, v.get("scanned_at"), bool(v.get("is_scanning")))
        for k, v in (cache or {}).items()
    ))


def _media_results(cache=None):
    """Return (media_stats, duplicates) for the current library cache.

    Both are computed together and memoised, so a page of the duplicates
    modal costs a dict lookup instead of a full re-walk.
    """
    if cache is None:
        cache = get_all_library_cache()
    cache = _active_library_cache(cache)
    fp = _cache_fingerprint(cache)
    now = _time.monotonic()
    with _MEDIA_MEMO_LOCK:
        if _MEDIA_MEMO["stats"] is not None:
            # Unchanged cache -> serve the memo. Changed cache -> still serve
            # it while the throttle window is open (see above).
            if _MEDIA_MEMO["fp"] == fp or (now - _MEDIA_MEMO["at"]) < _MEDIA_MEMO_MIN_INTERVAL:
                return _MEDIA_MEMO["stats"], _MEDIA_MEMO["duplicates"]
    # Computed outside the lock: it is pure and side-effect free, so a rare
    # duplicate computation on a cold start is cheaper than serialising every
    # request behind one mutex.
    stats = _compute_media_stats(cache)
    dups = _compute_media_duplicates(cache)
    with _MEDIA_MEMO_LOCK:
        _MEDIA_MEMO.update({"fp": fp, "stats": stats, "duplicates": dups,
                            "at": _time.monotonic()})
    return stats, dups


def _paginate(items, args):
    """Slice `items` for ?page=/?per_page= and return the envelope.

    per_page is clamped to 1..100 and page into range, so a crafted query can
    neither request the whole list in one response nor land out of bounds.
    """
    try:
        per_page = int(args.get("per_page", 20))
    except (TypeError, ValueError):
        per_page = 20
    per_page = max(1, min(100, per_page))
    try:
        page = int(args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    total_pages = max(1, -(-len(items) // per_page))     # ceil
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return {
        "items": items[start:start + per_page],
        "page": page,
        "per_page": per_page,
        "total": len(items),
        "total_pages": total_pages,
    }


def _dup_summary(dups):
    """Aggregate figures for the duplicates modal header.

    Computed server-side because they need the whole result set, which is
    exactly what is no longer sent to the browser.
    """
    files = 0
    movies = 0
    waste_bytes = 0
    res_mix = {}
    for g in dups:
        gf = g.get("files") or []
        files += len(gf)
        if g.get("kind") == "movie":
            movies += 1
        sizes = [int(f.get("size") or 0) for f in gf]
        if len(sizes) > 1:
            waste_bytes += sum(sizes) - max(sizes)
        for f in gf:
            r = (f.get("resolution") or "").strip() or "?"
            res_mix[r] = res_mix.get(r, 0) + 1
    return {
        "groups": len(dups),
        "files": files,
        "movies": movies,
        "series": len(dups) - movies,
        "reclaimable_mb": round(waste_bytes / (1024.0 * 1024.0), 2),
        "resolutions": [
            {"name": k, "value": v}
            for k, v in sorted(res_mix.items(), key=lambda kv: kv[1], reverse=True)
        ],
    }


def _active_library_cache(cache):
    """Drop library_cache rows that are not a configured scan target.

    Rows are keyed by path_key and were never deleted before this guard
    existed, so a deleted custom path (or a pre-rename install carried over by
    legacy_import) leaves a full, frozen copy of its scan behind. Since the
    duplicate key is deliberately location-independent, that copy made every
    single episode look like it existed twice.

    The scan itself prunes those rows now; this is the second line of defence
    for the window before the next scan runs, and for a cache written by an
    older version.
    """
    active = _lib_active_path_keys()
    return {k: v for k, v in (cache or {}).items() if k in active}


# ---------------------------------------------------------------------------
# Language detection for duplicate identity
#
# With MEDIAFORGE_LANG_SEPARATION off, the language is not a folder the scanner
# knows about -- it ends up inside the file name (and often a season folder)
# via the {language} placeholder of the naming template. The library cache
# therefore reports language=None for those files, so "… (German Dub) …" and
# "… (English Dub) …" of the same episode collapsed into one identity and were
# reported as duplicates of each other.
#
# Labels come from mediaforge/languages.py so this never drifts from the rest
# of the app.
# ---------------------------------------------------------------------------

def _lang_norm(text):
    """Lower-case, with separators flattened to single spaces.

    Makes "German.Dub", "german-dub" and "(German Dub)" all compare equal.
    """
    return re.sub(r"[\s._\-\[\]()]+", " ", str(text or "")).strip().lower()


def _build_lang_patterns():
    """[(normalised needle, canonical label)], longest needle first.

    Longest-first matters: "english dub german sub" contains both
    "english dub" and "german sub", and must win over either.
    """
    pairs = {}
    for label in LANG_PAIR_TO_LABEL.values():
        pairs[_lang_norm(label)] = label
    for label, folder in LANG_FOLDER_MAP.items():
        pairs[_lang_norm(folder)] = label
    return sorted(pairs.items(), key=lambda kv: len(kv[0]), reverse=True)


_LANG_PATTERNS = _build_lang_patterns()

# Single words that only carry a language when they stand alone in a *folder*
# name -- "Staffel 2 Deutsch" next to "Staffel 2 Englisch". Deliberately not
# applied to file names: a series called "The English Patient" would otherwise
# be tagged as English Dub.
_LANG_WORDS = {
    "deutsch": GERMAN_DUB, "german": GERMAN_DUB,
    "englisch": ENGLISH_DUB, "english": ENGLISH_DUB,
    "japanisch": JAPANESE_DUB, "japanese": JAPANESE_DUB,
}


def _has_token(haystack, needle):
    """Whole-token match: "german dub" hits, "germany" does not."""
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack) is not None


def _match_lang(haystack, allow_bare_words=False):
    """Find a language label in an already-normalised string, or None.

    `allow_bare_words` additionally accepts a lone "Deutsch"/"English"/… and
    is only ever enabled for directory names. On a file name it produced
    false positives on ordinary titles -- "The English Patient" was read as
    English Dub.
    """
    if not haystack:
        return None
    for needle, label in _LANG_PATTERNS:
        if _has_token(haystack, needle):
            return label
    if allow_bare_words:
        for word, label in _LANG_WORDS.items():
            if _has_token(haystack, word):
                return label
    return None


def _detect_language(entry, explicit=None):
    """Language of one library entry, or "" when it cannot be determined.

    `explicit` is the language-separation folder the scanner already knows
    about and always wins. Otherwise the file name is checked (that is where
    {language} lands), then the directory names from the file upwards.

    Returning "" for "unknown" is deliberate: two files that both carry no
    language marker still belong to the same identity, so a plain
    1080p-vs-720p duplicate keeps being detected.
    """
    if explicit:
        return explicit
    hit = _match_lang(_lang_norm(entry.get("file") or ""))
    if hit:
        return hit
    path = entry.get("path") or ""
    if path:
        # Nearest folder first: "Staffel 2 Deutsch" is more specific than the
        # library root, which might sit under a "German" share.
        for part in reversed(re.split(r"[\\/]+", str(path))[:-1]):
            hit = _match_lang(_lang_norm(part), allow_bare_words=True)
            if hit:
                return hit
    return ""


def _norm_path(p):
    """Normalised absolute path for identity comparison, or None.

    normcase() matters on Windows, where the same file legitimately appears as
    both "G:\\Anime" and "g:\\anime" depending on how the path was configured.
    """
    if not p:
        return None
    try:
        return os.path.normcase(os.path.normpath(str(p)))
    except (TypeError, ValueError):
        return None


# Quality / resolution / codec / source tokens that describe *how* a file was
# encoded rather than *which* movie it is. Stripped when deriving a movie's
# identity key so the same film in different resolutions collapses to one group.
_DUP_QUALITY_RE = re.compile(
    r"\b(4k|2k|2160p|1440p|1080p|720p|480p|360p|uhd|hdr10?|hdr|sdr|"
    r"hevc|h ?264|h ?265|x264|x265|av1|10 ?bit|8 ?bit|"
    r"bluray|blu ray|brrip|bdrip|remux|web ?dl|web ?rip|webhd|hdtv|dvdrip|hdrip|cam|ts|"
    r"aac|ac3|eac3|dts(?: ?hd)?|truehd|ddp? ?5 ?1|5 ?1|atmos)\b",
    re.IGNORECASE,
)


def _dup_norm_movie_key(filename: str) -> str:
    """Normalize a movie filename into an identity key.

    Strips the extension and common quality/resolution/codec/source tokens so
    the same film stored in different resolutions (e.g. "Movie.720p.mkv" and
    "Movie.480p.mkv") maps to the same key. Falls back to the lower-cased
    filename if normalization would leave the key empty."""
    name = filename.rsplit(".", 1)[0] if "." in filename else filename
    name = re.sub(r"[._\-\[\]()]+", " ", name)
    name = _DUP_QUALITY_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().lower()
    return name or filename.strip().lower()


def _compute_media_duplicates(cache=None):
    """Find media present more than once under the same identity.

    Two files are duplicates when their series/film title, season and episode
    match (and, in language-separation mode, their language) but the actual
    files differ — most commonly the same episode kept in two resolutions
    (e.g. 480p and 720p). Resolution/codec are deliberately NOT part of the
    identity key, so differing quality is exactly what surfaces here.

    Series episodes are keyed by (folder, season, episode); movies — which all
    share episode 1 in the "movies" bucket — are keyed by a normalized filename
    so distinct films in one folder are not falsely grouped. Returns a list of
    duplicate groups (each with its individual files) sorted by title."""
    if cache is None:
        cache = get_all_library_cache()
    cache = _active_library_cache(cache)
    groups = {}  # identity key tuple -> group dict
    seen_paths = {}  # identity key -> set of normalised paths already counted

    for path_key, entry in cache.items():
        data = entry.get("data") or {}
        location = data.get("label", path_key)
        lang_folders = data.get("lang_folders") or []
        if lang_folders:
            labelled = [(lf.get("name"), lf.get("titles") or []) for lf in lang_folders]
        else:
            labelled = [(None, data.get("titles") or [])]
        for language, titles in labelled:
            for t in titles:
                folder = t.get("folder")
                if not folder:
                    continue
                is_movie = bool(t.get("is_movie"))
                for skey, eps in (t.get("seasons") or {}).items():
                    for e in eps:
                        if not e.get("is_video", True):
                            continue
                        # Language is part of the identity. Without language
                        # separation it is not a folder the scanner reports,
                        # but sits in the file name via {language} -- so it has
                        # to be read back off the entry, or the German and the
                        # English copy of an episode look like duplicates of
                        # each other.
                        ep_lang = _detect_language(e, language)
                        if is_movie or skey == "movies":
                            norm = _dup_norm_movie_key(e.get("file") or "")
                            key = (folder.lower(), "movie", norm, ep_lang)
                            kind = "movie"
                            display_slot = "movie"  # frontend localizes the label
                        else:
                            ep = e.get("episode")
                            if ep is None:
                                continue
                            key = (folder.lower(), skey, ep, ep_lang)
                            kind = "series"
                            display_slot = f"S{skey}E{ep}"
                        # A copy is only a copy if it is a *different file*.
                        # Without this, one physical file reached through two
                        # overlapping scan targets (or listed twice in the
                        # cache) counted as a duplicate of itself.
                        npath = _norm_path(e.get("path"))
                        if npath is None:
                            # No path recorded — written by a version older
                            # than the current scanner. It cannot be told
                            # apart from any other copy, so counting it would
                            # only ever produce false positives.
                            continue
                        already = seen_paths.setdefault(key, set())
                        if npath in already:
                            continue
                        already.add(npath)

                        g = groups.setdefault(key, {
                            "title": folder,
                            "location": location,
                            "kind": kind,
                            "slot": display_slot,
                            "language": ep_lang or None,
                            "files": [],
                        })
                        g["files"].append({
                            "resolution": e.get("resolution"),
                            "video_codec": e.get("video_codec"),
                            "file": e.get("file"),
                            "path": e.get("path"),
                            "size": e.get("size"),
                        })

    dups = []
    for g in groups.values():
        if len(g["files"]) < 2:
            continue
        g["files"].sort(key=lambda f: (str(f.get("resolution") or ""), f.get("file") or ""))
        g["count"] = len(g["files"])
        dups.append(g)
    dups.sort(key=lambda x: (x["title"].lower(), str(x["slot"])))
    return dups


def _media_missing_episodes(seasons: dict) -> list:
    """Detect gaps in a series' episode numbering from library data alone.

    Returns a list of human-readable missing slots (e.g. "S1E3", "S2").
    A whole season counts as missing when it is absent within the
    1..max-season range; within a present season, any episode missing
    between 1 and the highest present episode is reported. An empty list
    means the series is considered complete."""
    notes = []
    season_nums = sorted(
        int(k) for k in seasons.keys()
        if k != "movies" and str(k).isdigit()
    )
    if not season_nums:
        return notes  # only loose/movie files — not treated as a gappy series
    for s in range(1, max(season_nums) + 1):
        skey = str(s)
        if s not in season_nums:
            notes.append(f"S{s}")  # whole season missing
            continue
        eps = sorted({
            e.get("episode") for e in seasons.get(skey, [])
            if e.get("episode") is not None and e.get("is_video", True)
        })
        if not eps:
            continue
        present = set(eps)
        for ep in range(1, max(eps) + 1):
            if ep not in present:
                notes.append(f"S{s}E{ep}")
    return notes


def _compute_media_stats(cache=None):
    """Build the Media statistics category from the library cache.

    The library cache is kept current by the library watcher, so these
    numbers track on-disk media automatically. Series that appear in
    multiple language folders (lang-separation mode) are merged by folder
    name so each logical series is counted once; their seasons are unioned
    so an episode present in any language counts as present."""
    if cache is None:
        cache = get_all_library_cache()
    cache = _active_library_cache(cache)
    any_scanning = any(e.get("is_scanning") for e in cache.values())
    ignores = get_media_ignores()
    # Physical files already counted, so a file reachable through two
    # overlapping scan targets is not counted (or sized) twice.
    counted_paths = set()

    # Merge titles across all locations / language folders by folder name.
    series = {}  # folder -> {"seasons": {skey: set(eps)}, "episodes": int, "location": str}
    movie_folders = set()
    # Technical distribution of the files on disk, for the Statistics charts.
    # Counted per physical file (not per merged logical episode) because that
    # is what actually occupies the disk.
    resolutions = {}
    codecs = {}
    by_location = {}
    total_size_mb = 0.0
    files_total = 0

    for path_key, entry in cache.items():
        data = entry.get("data") or {}
        location = data.get("label", path_key)
        lang_folders = data.get("lang_folders") or []
        if lang_folders:
            title_lists = [lf.get("titles") or [] for lf in lang_folders]
        else:
            title_lists = [data.get("titles") or []]
        for titles in title_lists:
            for t in titles:
                folder = t.get("folder")
                if not folder:
                    continue
                is_movie = bool(t.get("is_movie"))
                agg = None
                if not is_movie:
                    agg = series.setdefault(
                        folder.lower(),
                        {"title": folder, "seasons": {}, "location": location, "size_mb": 0.0},
                    )
                else:
                    movie_folders.add(folder.lower())
                for skey, eps in (t.get("seasons") or {}).items():
                    bucket = agg["seasons"].setdefault(skey, set()) if agg is not None else None
                    for e in eps:
                        if not e.get("is_video", True):
                            continue
                        npath = _norm_path(e.get("path"))
                        if npath is not None:
                            if npath in counted_paths:
                                # Same file seen through another location —
                                # count it once, or the disk-usage total ends
                                # up as a multiple of the real figure.
                                if agg is not None and e.get("episode") is not None:
                                    bucket.add(e.get("episode"))
                                continue
                            counted_paths.add(npath)
                        # --- technical distribution (per file on disk) ---
                        files_total += 1
                        res = (e.get("resolution") or "").strip() or "?"
                        resolutions[res] = resolutions.get(res, 0) + 1
                        codec = (e.get("video_codec") or "").strip() or "?"
                        codecs[codec] = codecs.get(codec, 0) + 1
                        try:
                            # Library entries store bytes; fall back to 0 on
                            # anything non-numeric rather than failing the page.
                            size_mb = float(e.get("size") or 0) / (1024.0 * 1024.0)
                        except (TypeError, ValueError):
                            size_mb = 0.0
                        total_size_mb += size_mb
                        by_location[location] = by_location.get(location, 0.0) + size_mb
                        if agg is not None:
                            agg["size_mb"] += size_mb
                            if e.get("episode") is not None:
                                bucket.add(e.get("episode"))

    movies_total = len(movie_folders)
    series_total = len(series)
    episodes_total = 0
    complete = 0
    incomplete_list = []

    for folder_key, agg in series.items():
        # episode count = distinct episodes across all (numeric) seasons
        for skey, eps in agg["seasons"].items():
            if skey != "movies":
                episodes_total += len(eps)
        seasons_for_gap = {
            skey: [{"episode": ep} for ep in eps]
            for skey, eps in agg["seasons"].items()
        }
        missing = _media_missing_episodes(seasons_for_gap)
        # Subtract user-ignored slots so a series whose remaining gaps are
        # all ignored counts as complete.
        ig = ignores.get(folder_key)
        if ig:
            if "__all__" in ig["slots"]:
                missing = []
            else:
                missing = [m for m in missing if m not in ig["slots"]]
        if missing:
            incomplete_list.append({
                "folder": folder_key,
                "title": agg["title"],
                "location": agg["location"],
                "missing": missing,
            })
        else:
            complete += 1

    incomplete_list.sort(key=lambda x: x["title"].lower())

    # Management view: everything the user has ignored, so it can be restored.
    ignored_list = [
        {
            "folder": folder_key,
            "title": ig.get("title") or folder_key,
            "slots": sorted(ig["slots"]),
        }
        for folder_key, ig in ignores.items()
    ]
    ignored_list.sort(key=lambda x: x["title"].lower())

    def _top(d, limit=10):
        """Sort a {name: value} map into a descending [{name, value}] list."""
        return [
            {"name": k, "value": round(v, 2) if isinstance(v, float) else v}
            for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        ]

    largest_series = sorted(
        (
            {"title": a["title"], "size_mb": round(a["size_mb"], 2),
             "episodes": sum(len(e) for k, e in a["seasons"].items() if k != "movies")}
            for a in series.values()
        ),
        key=lambda x: x["size_mb"],
        reverse=True,
    )[:10]

    return {
        "movies_total": movies_total,
        "series_total": series_total,
        "series_complete": complete,
        "series_incomplete": len(incomplete_list),
        "episodes_total": episodes_total,
        "files_total": files_total,
        "total_size_mb": round(total_size_mb, 2),
        "resolutions": _top(resolutions, 12),
        "codecs": _top(codecs, 12),
        "by_location": _top(by_location, 12),
        "largest_series": largest_series,
        "incomplete": incomplete_list,
        "ignored": ignored_list,
        "scanning": any_scanning,
        "scanned": bool(cache),
    }


def register_stats_routes(app):
    """Register the Statistics page and its supporting API routes
    (general/queue/sync/media stats, ignore-list management) on the
    Flask app."""
    @app.route("/monitoring")
    def monitoring_page():
        """Render the Monitoring overview. GET /monitoring.

        Menu rework: Monitoring is the Management-category landing that bundles
        Statistics, Download History and Uptime -- built like the Settings /
        Integrations pages (an overview grid of cards plus the shared in-page
        floating side menu). The three sub-pages keep their own routes (they
        each load their own JS/data); this overview links to them.
        """
        return render_template("monitoring.html")

    @app.route("/stats")
    def stats_page():
        """Render the Statistics page. GET /stats."""
        return render_template("stats.html")
    @app.route("/api/stats")
    def api_stats():
        """Return the combined stats payload (general, queue, sync, and —
        if enabled — media library stats). Triggers an initial library scan
        on first call if media stats are enabled but nothing has been
        scanned yet. GET /api/stats.

        Called from static/stats.js's `loadStats()`."""
        payload = {
            "general": get_general_stats(),
            "queue": get_queue_stats(),
            "sync": get_sync_stats(),
            # Chart series for the reworked Statistics page. The window is
            # user-selectable; get_stats_trends() clamps it to 1..365 days so a
            # crafted ?days= value can neither scan an unbounded range nor blow
            # up the response size.
            "trends": get_stats_trends(request.args.get("days")),
        }
        media_enabled = (get_setting("media_stats_enabled")
                         or os.environ.get("MEDIAFORGE_MEDIA_STATS_ENABLED", "0")) == "1"
        if media_enabled:
            # Load the library cache once and reuse it for both the media stats
            # and the duplicate scan, so the cache JSON is only decoded once per
            # request instead of on every helper call.
            cache = get_all_library_cache()
            # Kick off an initial library scan if nothing has been scanned yet,
            # so the Media category isn't permanently empty for fresh installs.
            if not cache:
                lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
                _lib_trigger_scan_async(_lib_build_scan_targets(), lang_sep)
            media, dups = _media_results(cache)
            # Only the count travels with the page payload. The rows themselves
            # are fetched per page from /api/media/duplicates: a 90k-file
            # library produces ~43k groups, which serialised to ~28 MB of JSON
            # on *every* stats load -- by far the largest cost on the page,
            # and the reason it took forever to appear.
            media = dict(media)
            media["duplicates_count"] = len(dups)
            # Same for the gappy-series list: it grows with the library and was
            # shipped on every load even though it is only ever read by the
            # modal. Counts stay, rows move to /api/media/incomplete.
            media["incomplete_count"] = len(media.get("incomplete") or [])
            media["ignored_count"] = len(media.get("ignored") or [])
            media.pop("incomplete", None)
            media.pop("ignored", None)
            payload["media"] = media
        return jsonify(payload)
    @app.route("/api/media/duplicates")
    def api_media_duplicates():
        """Return one page of duplicate groups plus the aggregate summary.
        GET /api/media/duplicates?page=1&per_page=20&q=<title filter>.

        Called from static/stats.js's `_loadDuplicatesPage()`. Exists so the
        stats page never has to carry the full group list (see api_stats)."""
        _stats, dups = _media_results()

        q = (request.args.get("q") or "").strip().lower()
        items = [d for d in dups if q in (d.get("title") or "").lower()] if q else dups

        out = _paginate(items, request.args)
        out["filtered"] = bool(q)
        # The summary always describes the *unfiltered* set, so the header
        # figures do not jump around while the user types in the search box.
        out["summary"] = _dup_summary(dups)
        return jsonify(out)

    @app.route("/api/media/incomplete")
    def api_media_incomplete():
        """Return one page of incomplete (or ignored) series.
        GET /api/media/incomplete?view=incomplete|ignored&page=&per_page=&q=.

        Called from static/stats.js's `_loadIncompletePage()`. Same reasoning
        as api_media_duplicates: a library with hundreds of gappy series
        produced a payload that grew without bound, and it was carried on
        every stats page load whether the modal was opened or not."""
        media, _dups = _media_results()
        view = "ignored" if request.args.get("view") == "ignored" else "incomplete"
        source = media.get("ignored" if view == "ignored" else "incomplete") or []

        q = (request.args.get("q") or "").strip().lower()
        items = [x for x in source if q in (x.get("title") or "").lower()] if q else source

        out = _paginate(items, request.args)
        out["view"] = view
        out["filtered"] = bool(q)
        out["summary"] = {
            "incomplete": len(media.get("incomplete") or []),
            "ignored": len(media.get("ignored") or []),
            "complete": media.get("series_complete", 0),
            "missing_slots": sum(len(x.get("missing") or []) for x in (media.get("incomplete") or [])),
        }
        return jsonify(out)
    @app.route("/api/media/ignore", methods=["POST"])
    def api_media_ignore():
        """Ignore missing slots (or whole series) in the Incomplete-series view.
        POST /api/media/ignore.

        Called from static/stats.js's `mediaIgnoreSelected()`."""
        data = request.get_json(silent=True) or {}
        items = data.get("items", [])
        if not isinstance(items, list) or not items:
            return jsonify({"error": "items required"}), 400
        for it in items:
            folder = str(it.get("folder", "")).strip()
            title = str(it.get("title", "")).strip()
            if it.get("all"):
                slots = ["__all__"]
            else:
                slots = [str(s).strip() for s in (it.get("slots") or []) if str(s).strip()]
            if folder and slots:
                add_media_ignores(folder, slots, title)
        return jsonify({"ok": True})
    @app.route("/api/media/unignore", methods=["POST"])
    def api_media_unignore():
        """Restore a previously ignored slot (or the whole series).
        POST /api/media/unignore.

        Called from static/stats.js's `mediaUnignore()`."""
        data = request.get_json(silent=True) or {}
        folder = str(data.get("folder", "")).strip()
        if not folder:
            return jsonify({"error": "folder required"}), 400
        if data.get("all"):
            remove_media_ignore(folder, all_slots=True)
        else:
            slot = str(data.get("slot", "")).strip()
            if not slot:
                return jsonify({"error": "slot required"}), 400
            remove_media_ignore(folder, slot=slot)
        return jsonify({"ok": True})
    @app.route("/api/stats/sync")
    def api_stats_sync():
        """Return sync stats plus the computed next scheduled run time.
        GET /api/stats/sync. No confirmed frontend caller was found in
        static/templates."""
        stats = get_sync_stats()
        # Compute next_run_at from last check + schedule interval
        schedule_key = os.environ.get("MEDIAFORGE_SYNC_SCHEDULE", "0")
        interval = SYNC_SCHEDULE_MAP.get(schedule_key, 0)
        stats["schedule"] = schedule_key
        stats["next_run_at"] = None
        if interval and stats.get("last_check"):
            from datetime import datetime, timedelta

            try:
                last = datetime.strptime(stats["last_check"], "%Y-%m-%d %H:%M:%S")
                nxt = last + timedelta(seconds=interval)
                stats["next_run_at"] = nxt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        return jsonify(stats)
    @app.route("/api/stats/trends")
    def api_stats_trends():
        """Return only the chart series for a given window.
        GET /api/stats/trends?days=30.

        Called from static/stats.js's `setStatsRange()` so switching the
        7/30/90/365-day selector does not re-run the (much more expensive)
        library-cache scan that the full /api/stats payload triggers."""
        return jsonify(get_stats_trends(request.args.get("days")))
    @app.route("/api/stats/queue")
    def api_stats_queue():
        """Return queue stats only. GET /api/stats/queue. No confirmed
        frontend caller was found in static/templates."""
        return jsonify(get_queue_stats())
    @app.route("/api/stats/general")
    def api_stats_general():
        """Return general stats only. GET /api/stats/general. No confirmed
        frontend caller was found in static/templates."""
        return jsonify(get_general_stats())
