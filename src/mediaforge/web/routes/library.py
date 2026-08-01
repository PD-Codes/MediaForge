"""Library page/API + scan helpers.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ..db import get_all_library_cache
from ..db import get_library_cache_status
from ..db import get_custom_path_by_id
from ..db import get_custom_paths
from ..db import get_setting
from ..lang_folders import LANG_FOLDERS
from ..books.scanner import BOOKS_FORMAT_VERSION
from ..books.scanner import scan_books
from ..comics.scanner import COMICS_FORMAT_VERSION
from ..comics.scanner import scan_comics
from ..comics import convert as comic_convert
from ..comics import covers as comic_covers
from ..media_types import BOOK_ALL_EXTS
from ..media_types import BOOK_CONVERTIBLE_EXTS
from ..media_types import BOOK_COVER_EXTS
from ..media_types import BOOK_EXTS
from ..media_types import VIDEO_EXTS
from ..media_kinds import ALL_SLUGS
from ..media_kinds import DEFAULT_KINDS_CSV
from ..media_kinds import KIND_BOOK
from ..media_kinds import KIND_COMIC
from ..media_kinds import KIND_VIDEO
from ..media_kinds import parse_kinds
from ..media_kinds import MEDIA_KINDS
from ..media_kinds import get_kind_by_url
from ..media_kinds import kinds_for_api
from ..db import invalidate_library_cache
from ..db import prune_library_cache
from ..db import set_library_cache
from ..db import set_library_scanning
from ..runtime_state import _move_jobs
from ..runtime_state import _move_jobs_lock
from flask import abort
from flask import jsonify
from flask import render_template
from flask import request
import os
import re
import threading
import time as _time
from ...logger import get_logger
from ...telemetry import client as telemetry_client
from ...telemetry import events as telemetry_events
from ...telemetry import settings as telemetry_settings


logger = get_logger(__name__)


_LIB_LANG_FOLDERS = LANG_FOLDERS
# Kept as a module-level name because a dozen call sites read it; the values
# now live in web/media_types.py, which is the single source of truth shared
# with the watcher and the duplicate checker (they used to disagree).
_LIB_VIDEO_EXTS = VIDEO_EXTS
# SxxExx episode marker.
#
# The digit counts matter more than they look. The previous pattern was
# S(\d{2})E(\d{2,3}), which silently *truncated* longer numbers: "S02E0013"
# matched as season 2 episode 001 because the episode group stopped after
# three digits and the trailing "3" was simply left over. Episode 13 was
# therefore indexed as episode 1 -- identical to the real S02E001, which made
# the two files look like the same episode (a false duplicate) and corrupted
# the missing-episode detection at the same time.
#
# (?!\d) is what fixes it: the group has to consume the whole number or not
# match at all, so a number that is too long is left to the fallback rather
# than being cut short. The season is accepted with 1-4 digits too, so the
# common "S1E1" spelling is finally recognised.
_LIB_EP_RE = re.compile(r"S(\d{1,4})E(\d{1,4})(?!\d)", re.IGNORECASE)
# Season-less fallback ("... E013 ..."). Deliberately still requires at least
# two digits: a bare "E1" appears inside far too many real titles.
_LIB_FALLBACK_EP_RE = re.compile(r"\bE(\d{2,4})(?!\d)\b", re.IGNORECASE)

# ── episode parsing ──────────────────────────────────────────────────────
#
# Everything below exists because "does the name contain SxxExx" was the
# WHOLE of the series/movie decision, and scene names have never spelled it
# only that way. A file that matched nothing was filed as a movie, which is
# how half-German, half-anime libraries ended up with series listed as films.
#
# Two rules keep this from over-matching in the other direction:
#   * release tags are removed BEFORE the loose patterns run -- "x264",
#     "H.265", "1080p", "DDP5.1" and a bare year are the four things that
#     otherwise look exactly like an episode number,
#   * the loose patterns that carry no "E"/"Episode" marker at all (anime's
#     "Title - 062 - ...") only run for a file that already sits inside a
#     season/specials folder. Outside one, "Rocky - 2 - ...' is a film.

# Release metadata, stripped to a space before any loose pattern is tried.
_LIB_TAG_RE = re.compile(
    r"\b(?:[xh]\.?26[45]|hevc|avc|xvid|divx"
    r"|\d{3,4}[pi]|4k|uhd|hdr10\+?|hdr|dv|sdr|(?:8|10|12)bits?"
    r"|aac|ac3|eac3|ddp?|dts(?:-hd)?|truehd|flac|opus|mp3|atmos|\d\.\d(?=\b)"
    r"|web-?dl|web-?rip|bd-?rip|br-?rip|blu-?ray|hdtv|dvdrip|remux|repack|proper"
    r"|multi|dual|dl|ger|eng|jap|sub(?:bed|s)?|dub(?:bed)?|omu"
    r")\b",
    re.IGNORECASE,
)
# A four-digit year, with or without brackets. Removed for the same reason.
_LIB_YEAR_RE = re.compile(r"[(\[]?\b(?:19|20)\d{2}\b[)\]]?")

# season + episode, in descending order of how explicit they are
_LIB_SE_PATTERNS = (
    # S01E05, S1E5, S01.E05, S01_E05, S01 E05, S01EP05
    re.compile(r"S(\d{1,4})\s*[._\- ]?\s*E(?:P|PISODE)?\.?\s*(\d{1,4})(?!\d)", re.IGNORECASE),
    # 1x05, 01x05
    re.compile(r"(?<!\d)(\d{1,4})\s*x\s*(\d{1,4})(?!\d)", re.IGNORECASE),
    # Season 1 ... Episode 5 / Staffel 1 ... Folge 5
    re.compile(r"(?:SEASON|STAFFEL)\s*[._\- ]?\s*(\d{1,4})\D{0,16}?"
               r"(?:EPISODE|FOLGE|EP)\s*[._\- ]?\s*(\d{1,4})(?!\d)", re.IGNORECASE),
)
# episode only -- the season comes from the folder (or 1)
_LIB_E_PATTERNS = (
    re.compile(r"(?:EPISODE|FOLGE)\s*[._\- ]?\s*(\d{1,4})(?!\d)", re.IGNORECASE),
    re.compile(r"\bEP\.?\s*[._\- ]?\s*(\d{1,4})(?!\d)", re.IGNORECASE),
    _LIB_FALLBACK_EP_RE,
)
# episode only and WITHOUT any marker: anime's "[Group] Show - 062 [1080p]".
# Only consulted inside a season/specials folder, see above.
_LIB_BARE_EP_RE = re.compile(r"(?:^|[\s._\]\)-])-\s*(\d{1,4})(?!\d)(?:$|[\s._\[\(-])")

# "Season 2" / "Staffel 02" / "S2" / "Series 2" as a FOLDER name.
_LIB_SEASON_DIR_RE = re.compile(
    r"^(?:S|SEASON|STAFFEL|SERIES)\s*[._\- ]?\s*(\d{1,4})(?!\d)", re.IGNORECASE)
# Season 0 by another name. Both spellings are what the scrapers write.
_LIB_SPECIALS_DIR_RE = re.compile(r"^(?:SPECIALS?|EXTRAS?|OVAS?|SPECIALS?_\w+)$", re.IGNORECASE)


def _lib_strip_tags(name):
    """The file name with release metadata and the year blanked out."""
    return _LIB_YEAR_RE.sub(" ", _LIB_TAG_RE.sub(" ", name))


def _lib_looks_like_year(value):
    """True for a 4-digit number that is far more likely a year.

    Only consulted for the season-less patterns: "Show.Title.E2019.mkv" has
    no season to disagree with, so nothing else could catch it -- and there is
    no series on earth with a 2019th episode of season 1. An explicit
    ``S01E2019`` is left alone; if somebody spells it out, they mean it.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    return 1900 <= number <= 2099


def _lib_dir_season(dir_name):
    """Season number a folder name declares, or ``None``.

    ``Specials``/``Extras``/``OVA`` answer 0 -- that is the season number
    every scraper and every player uses for them, and filing them as season 1
    (which the old season-less fallback did unconditionally) put them in the
    middle of the real episode list.
    """
    text = str(dir_name or "").strip()
    if not text:
        return None
    if _LIB_SPECIALS_DIR_RE.match(text):
        return 0
    m = _LIB_SEASON_DIR_RE.match(text)
    return int(m.group(1)) if m else None


def _lib_parse_episode(file_name, dir_name=""):
    """``(season, episode)`` for one file name, or ``None`` when it is not an
    episode at all -- which is what makes it a movie.

    ``dir_name`` is the name of the folder the file sits in (not the title
    folder): it supplies the season for the many naming schemes that put the
    season in the path and only the episode in the file, and it is the gate
    for the marker-less anime pattern.

    Deliberately returns the FIRST match of a multi-episode file
    ("S01E01-E02"): the rest of the library model is one row per episode, so
    inventing a second row for a file that does not exist would break the
    missing-episode detection it is meant to help.
    """
    name = str(file_name or "")
    dir_season = _lib_dir_season(dir_name)

    # Explicit S..E.. first, on the RAW name: it is unambiguous enough that
    # tag stripping could only ever hurt it.
    m = _LIB_EP_RE.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))

    cleaned = _lib_strip_tags(name)
    for pattern in _LIB_SE_PATTERNS:
        m = pattern.search(cleaned)
        if m:
            return int(m.group(1)), int(m.group(2))
    for pattern in _LIB_E_PATTERNS:
        m = pattern.search(cleaned)
        if m and not _lib_looks_like_year(m.group(1)):
            return (dir_season if dir_season is not None else 1), int(m.group(1))
    if dir_season is not None:
        m = _LIB_BARE_EP_RE.search(cleaned)
        if m and not _lib_looks_like_year(m.group(1)):
            return dir_season, int(m.group(1))
    return None
# Serialises full scans -- see _lib_do_scan(). Declared here since the
# original code, where it was defined and then never used.
_lib_scan_lock = threading.Lock()

# Probing bounds (see _lib_scan_base step 3).
#
# The *time* budget is the guarantee that a scan always returns; it holds
# whether a probe takes 20 ms on an NVMe or runs into the timeout on a stalled
# network share. A second cap on the number of files was tried and removed: it
# bound long before the budget on fast storage and turned the first scan of a
# large library into a many-day drip (45k files at 3000 per pass = 15 passes),
# while adding nothing the budget does not already cover.
_LIB_PROBE_TIMEOUT = 8            # seconds per file (30 is for remote streams)
_LIB_PROBE_TIME_BUDGET = 120      # seconds spent probing per scan pass
# Sanity ceiling only, so a pathological library cannot make the executor
# iterate millions of no-op tasks after the budget is spent.
_LIB_PROBE_MAX_PER_SCAN = 100000

# ffprobe processes in flight. 0 = derive from the CPU count. ffprobe is short
# and I/O bound, so more workers genuinely help on fast local storage; on a
# network share they mostly multiply the timeouts, which is why this is
# configurable rather than a fixed number.
_LIB_PROBE_WORKERS_AUTO = 0
_LIB_PROBE_WORKER_CHOICES = (0, 2, 4, 8, 12, 16, 24, 32)


def _lib_probe_workers():
    """Number of parallel ffprobe workers, from the setting or derived."""
    raw = get_setting("library_probe_workers", str(_LIB_PROBE_WORKERS_AUTO))
    try:
        workers = int(raw)
    except (TypeError, ValueError):
        workers = _LIB_PROBE_WORKERS_AUTO
    if workers not in _LIB_PROBE_WORKER_CHOICES:
        workers = _LIB_PROBE_WORKERS_AUTO
    if workers:
        return workers
    # Auto: ffprobe spends most of its life waiting on I/O, so oversubscribing
    # the cores pays off — but not without limit, since each worker is a real
    # process.
    cores = os.cpu_count() or 4
    return max(4, min(16, cores * 2))


# path_keys whose last scan ran out of probe budget. The auto-rescan loop
# treats them as due, so a large first scan converges in 15-minute steps
# instead of waiting a full rescan interval between passes.
_LIB_PROBE_PENDING = set()


def _lib_get_resolution(file_path):
    """Best-effort resolution label for a single file: try filename keyword/
    regex hints first, then fall back to an ffprobe height lookup."""
    fname = file_path.name.lower()
    if "4k" in fname or "2160p" in fname or "3840x2160" in fname:
        return "4K"
    if "2k" in fname or "1440p" in fname or "2560x1440" in fname:
        return "2K"
    if "1080p" in fname or "1080i" in fname or "1920x1080" in fname:
        return "1080p"
    if "720p" in fname or "1280x720" in fname:
        return "720p"
    if "480p" in fname or "854x480" in fname or "640x480" in fname:
        return "480p"
    if "360p" in fname or "640x360" in fname:
        return "360p"
        
    m = re.search(r"\b(2160|1440|1080|720|480|360|240)p?\b", fname)
    if m:
        val = m.group(1)
        if val == "2160": return "4K"
        if val == "1440": return "2K"
        return val + "p"
        
    try:
        from ..transcoder import probe_file
        info = probe_file(file_path)
        if info and info.get("height"):
            h = info["height"]
            if h >= 2160: return "4K"
            if h >= 1440: return "2K"
            if h >= 1080: return "1080p"
            if h >= 720: return "720p"
            if h >= 480: return "480p"
            if h >= 360: return "360p"
            return f"{h}p"
    except Exception:
        pass
    return None


def _lib_resolve_base():
    """Resolve the default download-root Path from MEDIAFORGE_DOWNLOAD_PATH,
    falling back to ~/Downloads."""
    from pathlib import Path
    raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
    if raw:
        dl_base = Path(raw).expanduser()
        if not dl_base.is_absolute():
            dl_base = Path.home() / dl_base
    else:
        dl_base = Path.home() / "Downloads"
    return dl_base


def _lib_scan_base(base, old_cache_lookup=None, progress=None, only_folder=None, only_loose=False):
    """Walk one library root and build its title/season/episode structure.

    Collects video files (top-level "loose movie" files plus per-title
    folders, with SxxExx episodes found recursively), resolves each file's
    resolution/codec info (reusing `old_cache_lookup` or a fast filename
    match where possible, otherwise probing in parallel via ffprobe), and
    returns a sorted list of title dicts ready to be cached/served by
    /api/library.

    Partial modes (used by the file-system watcher so a single new download
    does not re-walk a 90k-file library):
      * only_folder -- read just that one title folder below `base`.
      * only_loose  -- read just the top-level "loose movie" files.
    Both skip everything else, so the caller must merge the result into the
    existing cache instead of replacing it."""
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor
    lang_folder_set = set(_LIB_LANG_FOLDERS)
    titles = {}
    if not base.is_dir():
        return []

    # Helper to check if file is video
    def is_video_file(f):
        if not f.is_file(): return False
        fname = f.name
        if fname.startswith(".temp_") or fname.startswith("."): return False
        if ".part" in fname or fname.endswith(".part"): return False
        fname_lower = fname.lower()
        return any(fname_lower.endswith(ext) for ext in _LIB_VIDEO_EXTS)

    # 1. Collect all video files
    all_videos = []
    
    # Zero-th pass candidates (skipped when only one title folder is wanted)
    if not only_folder:
        for f in base.iterdir():
            if is_video_file(f):
                # Same question, same answer as the build pass below -- when
                # these two disagree a file is built into the tree without
                # ever having been probed, and shows up with no resolution.
                if _lib_parse_episode(f.name, base.name) is not None:
                    continue
                all_videos.append(f)

    # First and Second pass candidates
    def title_folders():
        """Top-level folders to look at -- narrowed down in partial mode."""
        if only_loose:
            return []
        if only_folder:
            one = base / only_folder
            return [one] if one.is_dir() else []
        return base.iterdir()

    for folder in title_folders():
        if not folder.is_dir():
            continue
        name = folder.name
        if name in lang_folder_set:
            continue
        for f in folder.iterdir():
            if is_video_file(f):
                if _lib_parse_episode(f.name, folder.name) is not None:
                    continue
                all_videos.append(f)
        for f in folder.rglob("*"):
            if is_video_file(f):
                if _lib_parse_episode(f.name, f.parent.name) is not None:
                    all_videos.append(f)

    # Remove duplicates while preserving order
    seen = set()
    unique_videos = []
    for f in all_videos:
        if f not in seen:
            seen.add(f)
            unique_videos.append(f)

    # 2. Determine which videos need probing
    probe_candidates = []
    resolved_media_data = {} # Path -> {"resolution": ..., "video_codec": ..., "audio_codec": ...}
    
    for f in unique_videos:
        try:
            fsize = f.stat().st_size
        except OSError:
            fsize = 0
        
        # Reuse what the previous scan already worked out for this exact file.
        #
        # The condition used to require a video_codec, which meant every file
        # whose probe had come back empty was probed again on *every* scan --
        # on a large library that is thousands of ffprobe calls per scan, for
        # a result that is already known to be nothing. "probed" marks a file
        # as done regardless of whether anything useful came out.
        cached = old_cache_lookup.get((str(f), fsize)) if old_cache_lookup else None
        if cached and (cached.get("video_codec") or cached.get("resolution") or cached.get("probed")):
            resolved_media_data[f] = cached
            continue


        # Check filename keywords/regex
        fname = f.name.lower()
        res_fast = None
        if "4k" in fname or "2160p" in fname or "3840x2160" in fname: res_fast = "4K"
        elif "2k" in fname or "1440p" in fname or "2560x1440" in fname: res_fast = "2K"
        elif "1080p" in fname or "1080i" in fname or "1920x1080" in fname: res_fast = "1080p"
        elif "720p" in fname or "1280x720" in fname: res_fast = "720p"
        elif "480p" in fname or "854x480" in fname or "640x480" in fname: res_fast = "480p"
        elif "360p" in fname or "640x360" in fname: res_fast = "360p"
        else:
            m = re.search(r"\b(2160|1440|1080|720|480|360|240)p?\b", fname)
            if m:
                val = m.group(1)
                if val == "2160": res_fast = "4K"
                elif val == "1440": res_fast = "2K"
                else: res_fast = val + "p"
                
        vc_fast = None
        if "hevc" in fname or "x265" in fname or "h.265" in fname: vc_fast = "HEVC"
        elif "h264" in fname or "x264" in fname or "h.264" in fname or "avc" in fname: vc_fast = "H.264"
        elif "av1" in fname: vc_fast = "AV1"
        
        if res_fast:
            resolved_media_data[f] = {"resolution": res_fast, "video_codec": vc_fast, "audio_codec": None}
        else:
            probe_candidates.append(f)

    # 3. Probe candidates in parallel — bounded in both count and wall-clock.
    #
    # Probing is the only unbounded part of a scan: a file whose name carries
    # no resolution has to be opened with ffprobe. On a large library on a slow
    # or flaky network share that is tens of thousands of calls, each of which
    # could sit at the ffprobe timeout, which is what made a scan look like it
    # had hung. It is now capped per scan and given a total time budget; what
    # does not fit is simply resolved by the next scan, and because results are
    # sticky (see "probed" above) every scan makes real progress.
    if probe_candidates:
        if len(probe_candidates) > _LIB_PROBE_MAX_PER_SCAN:
            probe_candidates = probe_candidates[:_LIB_PROBE_MAX_PER_SCAN]
        workers = _lib_probe_workers()
        logger.info("[LibraryScan] Probing %d file(s) with %d worker(s)...",
                    len(probe_candidates), workers)
        deadline = _time.monotonic() + _LIB_PROBE_TIME_BUDGET
        started = _time.monotonic()

        def probe_one(file_path):
            if _time.monotonic() > deadline:
                return None          # budget spent — leave it for the next scan
            try:
                from ..transcoder import probe_file
                info = probe_file(file_path, timeout=_LIB_PROBE_TIMEOUT)
                if info:
                    res = None
                    if info.get("height"):
                        h = info["height"]
                        if h >= 2160: res = "4K"
                        elif h >= 1440: res = "2K"
                        elif h >= 1080: res = "1080p"
                        elif h >= 720: res = "720p"
                        elif h >= 480: res = "480p"
                        elif h >= 360: res = "360p"
                        else: res = f"{h}p"
                        
                    vc = info.get("video_codec")
                    if vc:
                        vc = vc.lower()
                        if vc in ["hevc", "x265", "h265"]: vc = "HEVC"
                        elif vc in ["h264", "x264", "avc"]: vc = "H.264"
                        elif vc == "av1": vc = "AV1"
                        else: vc = vc.upper()
                        
                    ac = info.get("audio_codec")
                    if ac:
                        ac = ac.upper()
                        
                    return {"resolution": res, "video_codec": vc, "audio_codec": ac}
            except Exception:
                pass
            return None

        probed_ok = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = executor.map(probe_one, probe_candidates)
            for f, res_dict in zip(probe_candidates, results):
                if res_dict:
                    resolved_media_data[f] = dict(res_dict, probed=True)
                    probed_ok += 1
                elif _time.monotonic() <= deadline:
                    # Genuinely nothing to learn from this file (corrupt, or
                    # ffprobe cannot read it). Remember that, so it is not
                    # retried on every future scan. Files skipped because the
                    # budget ran out are deliberately *not* marked.
                    resolved_media_data[f] = {
                        "resolution": None, "video_codec": None,
                        "audio_codec": None, "probed": True,
                    }
        if _time.monotonic() > deadline:
            if progress is not None:
                progress["probe_incomplete"] = True
            logger.warning(
                "[LibraryScan] Probe time budget of %ds exhausted after %d file(s); "
                "the remainder is picked up by the follow-up scan",
                _LIB_PROBE_TIME_BUDGET, probed_ok)
        else:
            logger.info("[LibraryScan] Probed %d file(s) in %.1fs",
                        probed_ok, _time.monotonic() - started)

    # 4. Perform the actual build of titles/seasons structure using pre-resolved resolutions
    # Zero-th pass: video files sitting DIRECTLY in base (no title subfolder).
    for f in (base.iterdir() if not only_folder else []):
        if not is_video_file(f):
            continue
        if _lib_parse_episode(f.name, base.name) is not None:
            continue
        title_name = f.stem
        try:
            _st = f.stat()
            fsize = _st.st_size
            fmtime = _st.st_mtime
        except OSError:
            fsize = 0
            fmtime = 0
        if title_name not in titles:
            titles[title_name] = {"folder": title_name, "seasons": {}, "total_size": 0, "is_movie": False, "_added_at": 0}
        entry = titles[title_name]
        if "movies" not in entry["seasons"]:
            entry["seasons"]["movies"] = []
        if not any(e["file"] == f.name for e in entry["seasons"]["movies"]):
            mdata = resolved_media_data.get(f) or {}
            entry["seasons"]["movies"].append({
                "episode": 1, "file": f.name, "size": fsize, "is_video": True,
                "is_movie_file": True, "path": str(f),
                "resolution": mdata.get("resolution"),
                "video_codec": mdata.get("video_codec"),
                "audio_codec": mdata.get("audio_codec")
            })
            entry["total_size"] += fsize
            entry["is_movie"] = True
            entry["_added_at"] = max(entry.get("_added_at", 0), fmtime)

    for folder in title_folders():
        if not folder.is_dir():
            continue
        name = folder.name
        if name in lang_folder_set:
            continue
        if name not in titles:
            titles[name] = {"folder": name, "seasons": {}, "total_size": 0, "is_movie": False, "_added_at": 0}
        entry = titles[name]

        # First pass: direct video files in the title folder (no season subfolder)
        for f in folder.iterdir():
            if not is_video_file(f):
                continue
            if _lib_parse_episode(f.name, folder.name) is not None:
                continue
            try:
                _st = f.stat()
                fsize = _st.st_size
                fmtime = _st.st_mtime
            except OSError:
                fsize = 0
                fmtime = 0
            skey = "movies"
            if skey not in entry["seasons"]:
                entry["seasons"][skey] = []
            if not any(e["file"] == f.name for e in entry["seasons"][skey]):
                mdata = resolved_media_data.get(f) or {}
                entry["seasons"][skey].append({
                    "episode": 1, "file": f.name, "size": fsize, "is_video": True,
                    "is_movie_file": True, "path": str(f),
                    "resolution": mdata.get("resolution"),
                    "video_codec": mdata.get("video_codec"),
                    "audio_codec": mdata.get("audio_codec")
                })
                entry["total_size"] += fsize
                entry["is_movie"] = True
                entry["_added_at"] = max(entry.get("_added_at", 0), fmtime)

        # Second pass: recurse into subfolders for SxxExx episodes
        for f in folder.rglob("*"):
            if not is_video_file(f):
                continue
            parsed = _lib_parse_episode(f.name, f.parent.name)
            if parsed is None:
                continue
            snum, enum = parsed
            try:
                _st = f.stat()
                fsize = _st.st_size
                fmtime = _st.st_mtime
            except OSError:
                fsize = 0
                fmtime = 0
            skey = str(snum)
            if skey not in entry["seasons"]:
                entry["seasons"][skey] = []
            if not any(e["episode"] == enum and e["file"] == f.name for e in entry["seasons"][skey]):
                mdata = resolved_media_data.get(f) or {}
                entry["seasons"][skey].append({
                    "episode": enum, "file": f.name, "size": fsize, "is_video": True,
                    "path": str(f),
                    "resolution": mdata.get("resolution"),
                    "video_codec": mdata.get("video_codec"),
                    "audio_codec": mdata.get("audio_codec")
                })
                entry["total_size"] += fsize
                entry["_added_at"] = max(entry.get("_added_at", 0), fmtime)

    result = []
    for entry in sorted(titles.values(), key=lambda x: x["folder"].lower()):
        if not any(entry["seasons"].values()):
            continue
        total_eps = sum(sum(1 for e in eps if e.get("is_video", True)) for eps in entry["seasons"].values())
        for skey in entry["seasons"]:
            if skey != "movies":
                entry["seasons"][skey].sort(key=lambda e: e["episode"])
        # is_movie was set by the two movie passes and never cleared again, so
        # ONE unparseable extra in a series folder (a trailer, a "Making
        # Of", a folder.mp4) turned the whole series into a film for good --
        # and the Library page's filter, Auto-Sync's "series only" check and
        # the missing-episode detection all read this flag. A title that has
        # real numbered episodes is a series, whatever else sits next to them.
        is_movie = entry["is_movie"] and not any(
            key != "movies" and eps for key, eps in entry["seasons"].items())
        result.append({"folder": entry["folder"], "seasons": entry["seasons"],
                       "total_episodes": total_eps, "total_size": entry["total_size"],
                       "is_movie": is_movie, "added_at": entry.get("_added_at", 0)})
    return result


def _lib_path_key(cp_id):
    """The library_cache key for a scan target: "default" or the custom path id."""
    return "default" if cp_id is None else str(cp_id)


def lib_iter_cached_titles(data):
    """Every title in one library_cache entry, language folders included.

    A cache entry is a dict -- {"label", "custom_path_id", "titles",
    "lang_folders", "books", ...} -- and with language separation switched on
    the titles live under ``lang_folders[i]["titles"]`` while ``titles`` is
    None. Every consumer outside this module got that wrong at least once
    (routes/browse.py iterated the dict itself and quietly got key strings),
    so the reader lives here, next to the writer, and is exported on purpose.

    Public (no leading underscore): routes/browse.py and routes/home_panels.py
    both call it.
    """
    data = data or {}
    if not isinstance(data, dict):
        return []
    out = list(data.get("titles") or [])
    for folder in data.get("lang_folders") or []:
        out.extend((folder or {}).get("titles") or [])
    return out


# Setting holding the media kinds of the DEFAULT download root. Custom paths
# carry theirs in a column of their own; the default root has no custom_paths
# row to hang one on, so it lives here.
_LIB_DEFAULT_KINDS_SETTING = "default_path_media_kinds"


def _lib_kinds_map():
    """{path_key: [media kind, ...]} for every configured scan target.

    Built in one pass on purpose. The obvious shape -- ask the DB per target --
    turns the scan loop and every /api/library request into N+1 queries, and
    the scan loop already holds a lock while it runs.

    Read live rather than taken from the cached scan result: changing a path
    from "Movies & Series" to "eBooks" in Settings has to take effect on the
    next page load, not only after the next rescan.
    """
    out = {"default": parse_kinds(get_setting(_LIB_DEFAULT_KINDS_SETTING,
                                              DEFAULT_KINDS_CSV))}
    for cp in get_custom_paths():
        out[str(cp["id"])] = parse_kinds(cp.get("media_kinds"))
    return out


def _lib_kinds_for(cp_id):
    """The media kinds of a single scan target."""
    if cp_id is None:
        return parse_kinds(get_setting(_LIB_DEFAULT_KINDS_SETTING, DEFAULT_KINDS_CSV))
    cp = get_custom_path_by_id(cp_id)
    return parse_kinds((cp or {}).get("media_kinds"))


def lib_path_keys_for_kind(kind):
    """path_keys of the configured targets that feed one library.

    Public because it is the hook every other consumer of library_cache needs:
    routes/stats.py, routes/browse.py, routes/home_panels.py and
    routes/calendar_routes.py all iterate the raw cache and all of them mean
    "the video library" when they say "the library". Without this they would
    keep counting a path the user has assigned to eBooks only.

    It replaces the former _lib_active_path_keys() and keeps that function's
    second duty: the map is built from the CONFIGURED targets, so a leftover
    cache row -- a deleted custom path, or a pre-rename install carried over
    by legacy_import -- is not in it and is therefore filtered out. Losing
    that guard is what once made every episode look like a duplicate.
    """
    return {pk for pk, kinds in _lib_kinds_map().items() if kind in kinds}


# Automatic rescan interval, in hours. 0 disables it entirely; anything else
# means "rescan a location whose cache is older than this". Read live from the
# DB on every check, so changing it in Settings takes effect without a restart.
_LIB_RESCAN_DEFAULT_HOURS = 24
_LIB_RESCAN_CHOICES = (0, 6, 12, 24, 48, 168)
# How often the background loop asks "is anything stale?". Cheap (one DB read),
# and well below the smallest selectable interval.
_LIB_RESCAN_CHECK_SECONDS = 15 * 60


def _lib_rescan_hours():
    """The configured rescan interval in hours, clamped to a known choice."""
    raw = get_setting("library_rescan_hours", str(_LIB_RESCAN_DEFAULT_HOURS))
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        return _LIB_RESCAN_DEFAULT_HOURS
    return hours if hours in _LIB_RESCAN_CHOICES else _LIB_RESCAN_DEFAULT_HOURS


def _lib_stale_targets(targets, hours=None):
    """Targets that need scanning: never scanned, or their cache is too old.

    A location with no cache at all is *always* returned, whatever the
    interval — otherwise a fresh install or a newly added custom path would
    show an empty Library page until the user pressed refresh.
    """
    if hours is None:
        hours = _lib_rescan_hours()
    try:
        # Status only: this runs on a timer and used to parse every target's
        # full listing just to check a timestamp.
        cached = get_library_cache_status()
    except Exception:
        logger.exception("[LibraryScan] Could not read the library cache")
        return list(targets)

    cutoff = _time.time() - hours * 3600 if hours else None
    stale = []
    for (label, cp_id, base_path) in targets:
        path_key = _lib_path_key(cp_id)
        entry = cached.get(path_key) or {}
        if not entry.get("has_data"):
            stale.append((label, cp_id, base_path))
            continue
        if path_key in _LIB_PROBE_PENDING:
            # Last pass ran out of probe budget — finish the job now instead of
            # dripping through a huge library one rescan interval at a time.
            stale.append((label, cp_id, base_path))
            continue
        if cutoff is None:
            continue                      # interval disabled: only fill gaps
        try:
            scanned_at = float(entry.get("scanned_at") or 0)
        except (TypeError, ValueError):
            scanned_at = 0
        if scanned_at < cutoff:
            stale.append((label, cp_id, base_path))
    return stale


def _lib_start_auto_rescan(build_targets, lang_sep_getter):
    """Start the background loop that rescans locations as their cache ages.

    Replaces the unconditional full scan that used to run at every startup:
    that cost minutes on a large library for a result that is almost always
    identical, while the file watcher already catches changes live. What this
    loop covers is what the watcher cannot -- files that appeared while
    MediaForge was not running, or on a network share whose events never reach
    the watcher at all.

    Returns the thread (daemon, so it never keeps the process alive).
    """
    def _loop():
        while True:
            try:
                targets = build_targets()
                stale = _lib_stale_targets(targets)
                if stale:
                    logger.info("[LibraryScan] Auto-rescan: %d location(s) due", len(stale))
                    _lib_do_scan(stale, lang_sep_getter())
            except Exception:
                # Never let a bad scan kill the loop -- the next tick retries.
                logger.exception("[LibraryScan] Auto-rescan tick failed")
            _time.sleep(_LIB_RESCAN_CHECK_SECONDS)

    t = threading.Thread(target=_loop, daemon=True, name="library-auto-rescan")
    t.start()
    return t


def _lib_title_folders_for(base, changed_paths):
    """Map changed file paths to the title folders they belong to.

    A library root holds one folder per title, plus loose files directly in the
    root. For a change inside "…/Serie X/Staffel 1/ep.mkv" the affected unit is
    "Serie X"; for a loose file it is the file itself. Returns
    ``(folder_names, loose_files, outside)`` where `outside` is True when a path
    could not be attributed and the caller should fall back to a full scan.
    """
    from pathlib import Path
    base = Path(base).resolve()
    folders, loose = set(), set()
    outside = False
    for raw in changed_paths or ():
        try:
            rel = Path(raw).resolve().relative_to(base)
        except (ValueError, OSError):
            outside = True
            continue
        parts = rel.parts
        if not parts:
            continue
        if len(parts) == 1:
            loose.add(parts[0])          # video file sitting directly in the root
        else:
            folders.add(parts[0])
    return folders, loose, outside


def _lib_scan_title_folder(base, folder_name, old_cache_lookup=None, progress=None):
    """Scan exactly one title folder and return its title dict, or None.

    Reuses `_lib_scan_base` against a throwaway root containing just that one
    folder -- rather than duplicating the season/episode logic, which is the
    part most likely to drift out of sync.
    """
    from pathlib import Path
    target = Path(base) / folder_name
    if not target.is_dir():
        return None
    titles = _lib_scan_base(Path(base), old_cache_lookup, progress, only_folder=folder_name)
    for t in titles:
        if t.get("folder") == folder_name:
            return t
    return None


def _lib_apply_partial(path_key, label, cp_id, base, changed_paths, lang_sep):
    """Update only the parts of a cached scan that actually changed.

    This is what the file watcher calls. Before it existed, a single finished
    download re-walked the whole library root -- `iterdir` + `rglob` + `stat`
    over every file, repeated after every download, which on a large library
    kept the disk busy for no reason at all.

    Returns True when the cache was updated, False when the caller should fall
    back to a full scan (nothing attributable, language separation, or no
    usable cache yet).
    """
    from pathlib import Path
    if lang_sep:
        # With language separation a file's title folder sits under a language
        # folder, and a title can exist in several of them. Not worth the extra
        # bookkeeping for the rarer setup -- fall back to a full scan.
        return False

    # This whole function is the *video* fast path -- it rebuilds `titles` and
    # carries `books` over untouched. On a path the user has not assigned to
    # the video library there is nothing here to update, and merging a stale
    # title list back in would resurrect entries the last full scan cleared.
    if KIND_VIDEO not in _lib_kinds_for(cp_id):
        return False

    # A changed book cannot be merged in incrementally: which files form one
    # book is decided across the WHOLE location at once (a new file may join a
    # group three directories away, or split one), so there is no such thing as
    # rescanning a single book folder. Falling back to the full scan is also no
    # more expensive here, because that scan does exactly one book pass either
    # way -- it just also refreshes the video side while it is at it.
    if any(Path(p).suffix.lower() in BOOK_ALL_EXTS for p in (changed_paths or ())):
        return False

    cache = get_all_library_cache().get(path_key) or {}
    data = cache.get("data") or {}
    if data.get("lang_folders") or not isinstance(data.get("titles"), list):
        return False

    folders, loose, outside = _lib_title_folders_for(base, changed_paths)
    if outside or (not folders and not loose):
        return False

    # Loose files in the root are keyed by file stem, so the whole root's
    # top level has to be re-read for those; that is cheap (one iterdir).
    if loose:
        folders = folders | {None}

    old_cache_lookup = {}
    for t in data["titles"]:
        for eps in (t.get("seasons") or {}).values():
            for ep in eps:
                if ep.get("path"):
                    old_cache_lookup[(ep["path"], ep.get("size"))] = {
                        "resolution": ep.get("resolution"),
                        "video_codec": ep.get("video_codec"),
                        "audio_codec": ep.get("audio_codec"),
                        "probed": True,
                    }

    by_folder = {t.get("folder"): t for t in data["titles"]}
    progress = {}
    touched = 0

    for folder_name in sorted(f for f in folders if f is not None):
        fresh = _lib_scan_title_folder(base, folder_name, old_cache_lookup, progress)
        if fresh is None:
            by_folder.pop(folder_name, None)      # folder gone -> drop the title
        else:
            by_folder[folder_name] = fresh
        touched += 1

    if None in folders:
        # Re-read the loose top-level files and replace exactly those titles.
        loose_titles = _lib_scan_base(Path(base), old_cache_lookup, progress, only_loose=True)
        loose_names = {t["folder"] for t in loose_titles}
        for name, t in list(by_folder.items()):
            # A loose title is a single movie file; drop the stale ones whose
            # file has disappeared.
            if t.get("is_movie") and name not in loose_names and name in loose:
                by_folder.pop(name, None)
        for t in loose_titles:
            by_folder[t["folder"]] = t
        touched += 1

    titles = sorted(by_folder.values(), key=lambda x: (x.get("folder") or "").lower())
    set_library_cache(path_key, {
        "label": label, "custom_path_id": cp_id,
        "media_kinds": data.get("media_kinds"),
        "lang_folders": None, "titles": titles,
        # set_library_cache replaces the whole row, so the book list has to be
        # carried over explicitly. Forgetting this would make every video-only
        # partial update silently empty the eBook shelf until the next full
        # scan -- a bug that would look like flaky scanning, not like a lost
        # key.
        "books": data.get("books") or [],
        "books_version": data.get("books_version"),
        # Carried over for the same reason as the book list above:
        # set_library_cache replaces the whole row, so a video-only partial
        # update would otherwise empty the comic shelf until the next full scan.
        "comics": data.get("comics") or [],
        "comics_version": data.get("comics_version"),
    })
    if progress.get("probe_incomplete"):
        _LIB_PROBE_PENDING.add(path_key)
    logger.info("[LibraryScan] Partial update of %s: %d folder(s), %d title(s) cached",
                label, touched, len(titles))
    return True


def _lib_build_scan_targets():
    """Build the list of (label, custom_path_id, base_path) scan targets:
    the default download root plus every configured custom path."""
    from pathlib import Path
    dl_base = _lib_resolve_base()
    targets = [("Default", None, dl_base)]
    for cp in get_custom_paths():
        cp_base = Path(cp["path"]).expanduser()
        if not cp_base.is_absolute():
            cp_base = Path.home() / cp_base
        targets.append((cp["name"], cp["id"], cp_base))
    return targets


def _lib_scan_books_safe(base_path, label):
    """Run the book pass for one location, swallowing its failures.

    Deliberately isolated from the video scan: eBook indexing is the newer and
    more speculative half, and a library that has no books at all must not lose
    its films because a malformed OPF or an unreadable Calibre database raised
    somewhere inside it.
    """
    try:
        return scan_books(base_path)
    except Exception:
        logger.exception("[LibraryScan] Book scan of %s (%s) failed", base_path, label)
        return []


def _lib_scan_comics_safe(base_path, label):
    """Run the comic pass for one location, swallowing its failures.

    Same isolation as the book pass and for the same reason: a single
    malformed archive must not cost a location its films. Comics add one
    failure mode books do not have -- five container formats, two of which
    need an external tool -- so this is if anything more likely to trip.
    """
    try:
        return scan_comics(base_path)
    except Exception:
        logger.exception("[LibraryScan] Comic scan of %s (%s) failed", base_path, label)
        return []


def _lib_queue_book_covers(books):
    """Ask the background worker for the covers this book shelf needs.

    Same contract as _lib_queue_comic_covers below, and here for the same
    reason: before this the book shelf only ever showed a cover when a
    `cover.jpg` happened to lie next to the file, so most libraries were a
    wall of grey placeholders even though every EPUB carries its cover inside
    it.

    Queued per BOOK, not per format: a title held as both EPUB and MOBI is one
    card, and the EPUB is the cheap one to read. The list is therefore ordered
    readable-first, so the worker gets its answer from a zip member rather
    than by waiting for a conversion whenever it has the choice.

    Called from the scan AND from every /api/library?kind=book request -- a
    library that was indexed before this existed would otherwise never get a
    single cover, because the work was queued once at a moment that had
    already passed. The worker de-duplicates, so a shelf load costs a set
    lookup per book.
    """
    try:
        from ..books import covers as book_covers
        sources = []
        for book in books or ():
            if book.get("cover_path"):
                continue        # a sidecar image; the cover route caches that
            formats = book.get("formats") or []
            # .epub first, then anything else readable, then the rest.
            ranked = sorted(
                (f for f in formats if f.get("path")),
                key=lambda f: (0 if (f.get("ext") or "").lower() == "epub"
                               else (1 if f.get("readable") else 2)),
            )
            if ranked:
                sources.append(ranked[0]["path"])
        if sources:
            book_covers.prepare_async(sources)
    except Exception:
        # A cover is decoration. Never let it cost the scan or the request.
        logger.debug("[Books] Could not queue cover preparation", exc_info=True)


def _lib_refresh_comic_conversion_state(series_list):
    """Clear the "has to be prepared" flag on issues that meanwhile were.

    The scan decides ``needs_conversion`` and the answer is cached for as long
    as nothing on disk changes -- but preparing an issue changes nothing about
    the file the scan looked at. It writes a repacked copy into a separate
    cache. So the shelf went on showing the "!" badge, and the series card went
    on counting the issue as pending, for a comic the user had just prepared
    and could already read. That is this function.

    Applied to the rows on their way out rather than written back into the
    cache: the library cache is a JSON blob in SQLite, and rewriting a
    multi-megabyte row on a shelf load to correct a boolean would cost far more
    than recomputing it. The next scan persists the corrected value anyway (see
    comics/scanner.py).

    Cheap by construction:
      * one directory listing of the conversion cache for the whole shelf, and
      * if that listing is empty -- nothing has ever been prepared, the common
        case -- it returns before touching a single file, and
      * only issues still flagged are looked at, a set that shrinks with every
        conversion.
    """
    try:
        from ..comics import convert as comic_convert
        flagged = [issue
                   for entry in series_list or ()
                   for issue in (entry.get("issues") or ())
                   if issue.get("needs_conversion") and issue.get("path")]
        if not flagged:
            return
        keys = comic_convert.converted_keys()
        if not keys:
            return
        changed = False
        for issue in flagged:
            try:
                key = comic_convert.cache_key(issue["path"])
            except OSError:
                # The file is gone. Not this function's problem -- the next
                # scan drops the row.
                continue
            if key in keys:
                issue["needs_conversion"] = False
                issue["readable"] = True
                changed = True
        if not changed:
            return
        # The series card shows its own count, so it has to be recomputed from
        # the issues that were just corrected -- otherwise the badge on the
        # card and the badges on its issues disagree.
        for entry in series_list or ():
            issues = entry.get("issues") or ()
            entry["needs_conversion_count"] = sum(
                1 for i in issues if i.get("needs_conversion"))
            entry["readable_count"] = sum(1 for i in issues if i.get("readable"))
    except Exception:
        # A shelf that shows a stale badge beats a shelf that does not load.
        logger.debug("[Comics] Could not refresh conversion state", exc_info=True)


def _lib_queue_comic_covers(series_list):
    """Ask the background worker for the covers this shelf needs.

    Covers are NOT behind a setting. A shelf of blank tiles reads as a broken
    scan, so getting a picture onto each card is part of showing the shelf at
    all -- the same way the video shelf probes resolutions without asking.
    What IS optional is repacking every issue in the library; that is
    `comic_auto_prepare_all` below, and it is off by default because it costs
    real time and disk.

    Called from the scan AND from every /api/library?kind=comic request. Only
    the scan used to call it, so a library that had already been indexed never
    got a single cover: the work was queued exactly once, at a moment that had
    already passed. The worker de-duplicates, so calling it on every shelf
    load costs a set lookup per series.
    """
    try:
        # EVERY issue, not just the one each series card shows. The shelf has a
        # single-issue view where every one of them is a card of its own, and a
        # grid of blank tiles there is exactly the state this whole mechanism
        # exists to avoid.
        #
        # Affordable because a cover is only the FIRST PAGE: comics/convert.py
        # pulls that one member out of the archive instead of unpacking it, so
        # this is one cheap read per issue rather than a full repack. The
        # covers are ordered series-first so the grouped view -- the default,
        # and what the user is looking at while this runs -- fills in first.
        sources = [s.get("cover_source") for s in series_list if s.get("cover_source")]
        seen = set(sources)
        for entry in series_list:
            for issue in entry.get("issues") or []:
                path = issue.get("path")
                if path and path not in seen:
                    seen.add(path)
                    sources.append(path)
        if sources:
            comic_covers.prepare_async(sources)

        # Opt-in and separate: repacking a whole archive is what costs real
        # time and a second copy on disk. Covers above do not need it.
        if get_setting("comic_auto_prepare_all", "0") == "1":
            issues = [i.get("path")
                      for entry in series_list
                      for i in (entry.get("issues") or [])
                      if i.get("path") and i.get("needs_conversion")]
            if issues:
                # request_conversion(), not a covers.* call: this setting is
                # about repacking the archives, which is convert.py's job --
                # covers.py only ever wanted one page out of them.
                #
                # It used to call comic_covers.prepare_full_async(), which does
                # not exist and never did. The AttributeError landed in the
                # `except Exception` below and was logged at debug, so the
                # setting silently did nothing at all while reporting success.
                # convert.py caps how many run at once and refuses duplicates,
                # so handing it the whole list is safe.
                from pathlib import Path as _Path
                queued = 0
                for path in issues:
                    try:
                        state = comic_convert.request_conversion(_Path(path))
                        queued += 1 if state.get("pending") else 0
                    except Exception:
                        logger.debug("[Comics] Could not queue %s for conversion",
                                     path, exc_info=True)
                logger.info("[Comics] Queued %s of %s issue(s) for full preparation",
                            queued, len(issues))
    except Exception:
        # A cover is decoration. Never let it cost the scan or the request.
        logger.debug("[Comics] Could not queue cover preparation", exc_info=True)


def _lib_do_scan(targets, lang_sep):
    """Perform a full scan and store results in the cache. Runs in background thread.

    Serialised by `_lib_scan_lock`. That lock existed but was never acquired,
    so a startup scan, the file watcher, POST /api/library/refresh and the
    Statistics page's initial trigger could all walk the same tree at the same
    time, each spawning its own pool of ffprobe processes. On a small library
    that is merely wasteful; on a large one it is a thread and I/O storm that
    looks exactly like a hang.

    A scan that arrives while another is running is *skipped*, not queued: it
    would have produced the same result as the one already in flight, and
    queueing them is how a burst of watcher events turns into a scan backlog
    that never drains.
    """
    from pathlib import Path

    if not _lib_scan_lock.acquire(blocking=False):
        logger.info("[LibraryScan] A scan is already running — skipping this request")
        return
    try:
        _lib_do_scan_locked(targets, lang_sep)
    finally:
        _lib_scan_lock.release()


def _lib_do_scan_locked(targets, lang_sep):
    """The actual scan. Only ever called with `_lib_scan_lock` held."""
    from pathlib import Path
    
    # Build lookup from old cache to optimize scans
    old_cache_lookup = {}
    try:
        cache = get_all_library_cache()
        for pk, entry in cache.items():
            if entry and entry.get("data"):
                data = entry["data"]
                t_list = []
                if data.get("titles"):
                    t_list.extend(data["titles"])
                if data.get("lang_folders"):
                    for lf in data["lang_folders"]:
                        if lf.get("titles"):
                            t_list.extend(lf["titles"])
                for t in t_list:
                    for skey, eps in t.get("seasons", {}).items():
                        for ep in eps:
                            if ep.get("path"):
                                old_cache_lookup[(ep["path"], ep.get("size"))] = {
                                    "resolution": ep.get("resolution"),
                                    "video_codec": ep.get("video_codec"),
                                    "audio_codec": ep.get("audio_codec")
                                }
    except Exception as e:
        logger.warning("[LibraryScan] Failed to build resolution cache lookup: %s", e)

    # One lookup for the whole run: which library each target feeds. A path
    # assigned to "Movies & Series" only must not pay for a book pass, and a
    # book-only path must not pay for the ffprobe pass -- which is the whole
    # point of the setting, since those two passes are the expensive half of a
    # scan on a large library.
    kinds_map = _lib_kinds_map()

    for (label, cp_id, base_path) in targets:
        path_key = "default" if cp_id is None else str(cp_id)
        kinds = kinds_map.get(path_key) or parse_kinds(None)
        want_video = KIND_VIDEO in kinds
        want_books = KIND_BOOK in kinds
        want_comics = KIND_COMIC in kinds

        # An unreachable location must not overwrite its cache with "empty".
        # _lib_scan_base() returns [] for a path it cannot read, and caching
        # that would (a) blank the Library page for a NAS that happened to be
        # offline at startup and (b) refresh scanned_at, so the auto-rescan
        # would not look at it again for a whole interval. Leaving both alone
        # keeps the last good scan visible and has the next tick retry.
        try:
            reachable = Path(base_path).is_dir()
        except OSError:
            reachable = False
        if not reachable:
            logger.warning("[LibraryScan] %s (%s) is not reachable — keeping the previous scan",
                           base_path, label)
            set_library_scanning(path_key, False)
            continue

        set_library_scanning(path_key, True)
        progress = {}
        try:
            # Books are collected by a pass of their own and land under their
            # own key. They are never folded into `titles`: everything reading
            # that list -- stats, calendar, auto-sync, the v1 API, the upscale
            # and encoding workers -- assumes a video, and a book reaching any
            # of them is at best a wrong number and at worst a destroyed file.
            # Language separation does not apply: a book has no dub track, so
            # the same list is stored for both shapes.
            loc_books = _lib_scan_books_safe(base_path, label) if want_books else []
            loc_comics = _lib_scan_comics_safe(base_path, label) if want_comics else []
            if loc_books:
                _lib_queue_book_covers(loc_books)
            if loc_comics:
                _lib_queue_comic_covers(loc_comics)
            if lang_sep and want_video:
                loc_lang_folders = []
                for lf in _LIB_LANG_FOLDERS:
                    lf_titles = _lib_scan_base(base_path / lf, old_cache_lookup, progress)
                    if lf_titles:
                        loc_lang_folders.append({"name": lf, "titles": lf_titles})
                set_library_cache(path_key, {
                    "label": label, "custom_path_id": cp_id, "media_kinds": kinds,
                    "lang_folders": loc_lang_folders, "titles": None,
                    "books": loc_books, "books_version": BOOKS_FORMAT_VERSION,
                    "comics": loc_comics, "comics_version": COMICS_FORMAT_VERSION,
                })
            else:
                loc_titles = (_lib_scan_base(base_path, old_cache_lookup, progress)
                              if want_video else [])
                set_library_cache(path_key, {
                    "label": label, "custom_path_id": cp_id, "media_kinds": kinds,
                    # `lang_folders: None` on a book-only path is not a
                    # degraded shape, it is the correct one: lib_iter_cached_titles
                    # reads `titles` when lang_folders is None, and an empty
                    # list there is exactly "this path holds no videos".
                    "lang_folders": None, "titles": loc_titles,
                    "books": loc_books, "books_version": BOOKS_FORMAT_VERSION,
                    "comics": loc_comics, "comics_version": COMICS_FORMAT_VERSION,
                })
        except Exception:
            logger.exception("[LibraryScan] Scan of %s (%s) failed", base_path, label)
            set_library_scanning(path_key, False)
        else:
            # is_scanning is already set to 0 by set_library_cache.
            # Remember whether this location still has files left to probe, so
            # the auto-rescan loop comes back in minutes rather than waiting a
            # whole rescan interval for the next pass.
            if progress.get("probe_incomplete"):
                _LIB_PROBE_PENDING.add(path_key)
            else:
                _LIB_PROBE_PENDING.discard(path_key)

    # Drop cache rows for targets that no longer exist. Without this a deleted
    # custom path keeps its last scan forever, and every consumer that reads
    # the whole cache (Statistics, duplicate check) counts those files a
    # second time -- which shows up as "everything is a duplicate".
    try:
        # Keyed off the *configured* targets, not the ones just scanned: an
        # unreachable location is still configured and must keep its cache.
        removed = prune_library_cache({_lib_path_key(cp_id)
                                       for (_l, cp_id, _b) in _lib_build_scan_targets()})
        if removed:
            logger.info("[LibraryScan] Removed %d stale library cache entr%s",
                        removed, "y" if removed == 1 else "ies")
    except Exception:
        logger.exception("[LibraryScan] Could not prune stale library cache entries")


def _lib_trigger_scan_async(targets, lang_sep):
    """Kick off `_lib_do_scan` on a background daemon thread and return immediately."""
    import threading
    t = threading.Thread(target=_lib_do_scan, args=(targets, lang_sep), daemon=True)
    t.start()


def _get_lib_watcher():
    """Return the (singleton) library file watcher."""
    from ..library_watcher import get_watcher
    return get_watcher()


def _lib_watcher_scan_callback(path_key: str, changed_paths=None):
    """Called by watchdog when files change in a watched folder.

    Tries a partial update first -- only the title folders the changed paths
    belong to are re-read. A full re-walk of the root happens only when the
    change cannot be attributed (see `_lib_apply_partial`), which keeps a
    single finished download from touching every file in the library.
    """
    targets = _lib_build_scan_targets()
    lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
    for (label, cp_id, base_path) in targets:
        pk = "default" if cp_id is None else str(cp_id)
        if pk != path_key:
            continue
        if changed_paths:
            try:
                if _lib_apply_partial(path_key, label, cp_id, base_path,
                                      changed_paths, lang_sep):
                    return
            except Exception:
                logger.exception("[LibraryScan] Partial update failed, falling back")
        _lib_do_scan([(label, cp_id, base_path)], lang_sep)
        return


def lib_resolve_library_file(path, exts=None):
    """Resolve *path* and return it only if it is a real media file inside one
    of the configured scan targets; otherwise None.

    The single place that answers "may the caller touch this file?". Any
    endpoint that reads, probes, replaces or deletes a file the client named
    has to go through here -- /api/upscale/add-library did not, which let any
    logged-in user hand the upscale worker an arbitrary path and have it
    overwritten (upscaling_replace_original is on by default).

    resolve() is applied to both sides, so symlinks pointing out of the
    library are rejected too.

    *exts* is the extension set the CALLER accepts and defaults to video. It
    is a parameter rather than one module-wide constant on purpose, because
    the callers behind this guard are not interchangeable: the media-info
    route hands whatever it gets to ffprobe, and the upscale worker re-encodes
    it and writes the result back over the original. Widening a single shared
    set to also cover eBooks would mean an .epub could be probed for eight
    seconds and then destroyed by an upscale job. So each caller names the
    kind of file it actually wants.
    """
    from pathlib import Path as _P
    if not path:
        return None
    allowed = _LIB_VIDEO_EXTS if exts is None else exts
    try:
        resolved = _P(path).resolve()
    except (OSError, ValueError):
        return None
    if resolved.suffix.lower() not in allowed:
        return None
    if not resolved.is_file():
        return None
    for (_, _, base_path) in _lib_build_scan_targets():
        try:
            resolved.relative_to(_P(base_path).resolve())
            return resolved
        except (ValueError, OSError):
            continue
    return None


def _lib_assert_within_root(path, root):
    """Resolve path and verify it stays within root — blocks symlink escapes.
    Returns the resolved Path on success, raises ValueError on violation."""
    from pathlib import Path as _P
    resolved = _P(path).resolve()
    resolved_root = _P(root).resolve()
    resolved.relative_to(resolved_root)  # raises ValueError if outside
    return resolved


def _lib_move_resolve_base(cp_id):
    """Resolve a custom_path_id (or None for default) to an absolute, symlink-free Path."""
    from pathlib import Path
    if cp_id:
        cp = get_custom_path_by_id(cp_id)
        if not cp:
            return None
        p = Path(cp["path"]).expanduser()
    else:
        raw = get_setting("download_path") or os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
        p = Path(raw).expanduser() if raw else Path.home() / "Downloads"
    p = p if p.is_absolute() else Path.home() / p
    return p.resolve()


def _lib_move_worker(job_id, src, dst):
    """Background thread: copy src→dst with progress tracking, then delete src."""
    import shutil
    from pathlib import Path
    job = _move_jobs[job_id]
    try:
        # Calculate total bytes
        all_files = [f for f in Path(src).rglob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in all_files)
        with _move_jobs_lock:
            job["total_bytes"] = total
            job["status"] = "running"

        copied = 0
        dst_path = Path(dst)
        src_path = Path(src)
        dst_path.mkdir(parents=True, exist_ok=True)

        for src_file in all_files:
            rel = src_file.relative_to(src_path)
            dst_file = dst_path / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            with _move_jobs_lock:
                job["current_file"] = str(rel)
            # buffered copy for progress
            with open(src_file, "rb") as fin, open(dst_file, "wb") as fout:
                while True:
                    buf = fin.read(256 * 1024)  # 256 KB chunks
                    if not buf:
                        break
                    fout.write(buf)
                    copied += len(buf)
                    with _move_jobs_lock:
                        job["copied_bytes"] = copied
            try:
                shutil.copystat(str(src_file), str(dst_file))
            except Exception:
                pass

        # Also copy empty directories
        for src_dir in sorted(Path(src).rglob("*")):
            if src_dir.is_dir():
                rel = src_dir.relative_to(src_path)
                (dst_path / rel).mkdir(parents=True, exist_ok=True)

        # Delete source
        shutil.rmtree(str(src))
        invalidate_library_cache()
        with _move_jobs_lock:
            job["status"] = "done"
            job["current_file"] = ""
    except Exception as exc:
        logger.error("[LibMove] Move job %s failed: %s", job_id, exc, exc_info=True)
        # Clean up partial destination
        try:
            import shutil as _sh
            _sh.rmtree(str(dst), ignore_errors=True)
        except Exception:
            pass
        with _move_jobs_lock:
            job["status"] = "error"
            job["error"] = str(exc)


def _lib_move_loose_files_worker(job_id, file_paths, dst_dir):
    """Background thread: move individual loose files (movies in root) to dst_dir."""
    import shutil
    from pathlib import Path
    job = _move_jobs[job_id]
    try:
        files = [Path(p) for p in file_paths]
        total = sum(f.stat().st_size for f in files if f.exists())
        with _move_jobs_lock:
            job["total_bytes"] = total
            job["status"] = "running"

        dst_path = Path(dst_dir)
        dst_path.mkdir(parents=True, exist_ok=True)
        copied = 0
        for src_file in files:
            if not src_file.exists():
                continue
            dst_file = dst_path / src_file.name
            with _move_jobs_lock:
                job["current_file"] = src_file.name
            with open(src_file, "rb") as fin, open(dst_file, "wb") as fout:
                while True:
                    buf = fin.read(256 * 1024)
                    if not buf:
                        break
                    fout.write(buf)
                    copied += len(buf)
                    with _move_jobs_lock:
                        job["copied_bytes"] = copied
            try:
                shutil.copystat(str(src_file), str(dst_file))
            except Exception:
                pass
            src_file.unlink()

        invalidate_library_cache()
        with _move_jobs_lock:
            job["status"] = "done"
            job["current_file"] = ""
    except Exception as exc:
        logger.error("[LibMove] Loose file move job %s failed: %s", job_id, exc, exc_info=True)
        with _move_jobs_lock:
            job["status"] = "error"
            job["error"] = str(exc)

def _lib_overview_counts():
    """Per-kind headline numbers for the library hub tiles.

    Reads the cache that is already there rather than touching the disk: the
    hub is the first thing shown when the user clicks "Library", and making
    that click wait on a filesystem walk is exactly the sort of thing that
    makes an overview page feel worse than the list it replaced. A location
    that has never been scanned simply contributes nothing.
    """
    kinds_map = _lib_kinds_map()
    cache = get_all_library_cache() or {}

    titles = episodes = books = series = issues = 0
    video_size = book_size = comic_size = 0

    for path_key, entry in cache.items():
        kinds = kinds_map.get(path_key)
        if not kinds:
            continue                      # leftover row of a deleted path
        data = (entry or {}).get("data") or {}
        if KIND_VIDEO in kinds:
            for title in lib_iter_cached_titles(data):
                titles += 1
                episodes += int(title.get("total_episodes") or 0)
                video_size += int(title.get("total_size") or 0)
        if KIND_BOOK in kinds:
            for book in data.get("books") or []:
                books += 1
                book_size += int(book.get("total_size") or 0)
        if KIND_COMIC in kinds:
            # A comic tile counts series first and issues second, the way the
            # shelf is grouped -- "12 series" is what a reader recognises,
            # "1,480 issues" is the detail under it.
            for entry in data.get("comics") or []:
                series += 1
                issues += int(entry.get("issue_count") or 0)
                comic_size += int(entry.get("total_size") or 0)

    return {
        KIND_VIDEO: {"primary": titles, "secondary": episodes, "size": video_size},
        KIND_BOOK:  {"primary": books,  "secondary": 0,        "size": book_size},
        KIND_COMIC: {"primary": series, "secondary": issues,   "size": comic_size},
    }


# --- Telemetry: library usage ------------------------------------------------
# Two independent, deliberately content-free signals (see telemetry/registry.py
# for the wording the user is shown for each):
#
#   * flag.library   (stage 2) -- WHICH library section was opened. A pure
#     usage counter; the payload is one word out of the fixed list in
#     web/media_kinds.py plus "hub".
#   * detail.library (stage 3) -- HOW the configured download paths are spread
#     over the media kinds. This is what answers the question the split was
#     built for: are the separate libraries actually used, or is every path
#     still assigned to "Movies & Series" only? Counts, nothing else.
#
# Neither carries a provider, so the hard hanime_tv limit in
# telemetry/sanitize.py has nothing to bite on here -- and must not get
# anything to bite on: do not add a provider/title/path field to either event.
#
# Both are throttled per process. The stage-2 counter is throttled PER SECTION
# rather than globally, because a single global throttle would make the very
# signal this exists for ("which sections does this install use?") depend on
# the order the user happened to click things in. Hourly resolution is all a
# yes/no-plus-counter needs; a page-reload streak is not extra information.
#
# NOTHING in here logs above DEBUG, on purpose: telemetry/hooks.py turns every
# ERROR-level log record anywhere in this app into a crash report, so a
# telemetry helper that logged its own failure at ERROR would file itself as an
# application crash.
_TEL_VIEW_MIN_INTERVAL = 3600.0
_TEL_PATHS_MIN_INTERVAL = 3600.0
_tel_view_last = {}
_tel_paths_last = None
_tel_lock = threading.Lock()


def _report_library_view(section):
    """Submit the flag.library stage-2 usage counter for one library page view.

    `section` is "hub" or a media-kind slug. The builder validates it against
    web/media_kinds.py and drops anything else, so a mistyped or probed URL
    segment can never turn into a free-text field on the server.

    Opening a section that only shows a "coming soon" placeholder is an
    ordinary page view and is reported exactly like any other -- it is not an
    error and never produces an error/crash event of any kind. Wrapped in its
    own try/except (DEBUG only, see the note above) so a telemetry bug can
    never affect the library pages.
    """
    try:
        section = str(section or "").strip().lower()
        now = _time.monotonic()
        with _tel_lock:
            # The keys come from the media-kind registry (the route 404s before
            # calling this for anything else), so this dict is bounded by that
            # list; the length check only keeps a future call site from turning
            # it into an unbounded map of URL segments.
            if section not in _tel_view_last and len(_tel_view_last) >= 16:
                return
            last = _tel_view_last.get(section)
            if last is not None and now - last < _TEL_VIEW_MIN_INTERVAL:
                return
            _tel_view_last[section] = now
        telemetry_client.submit(telemetry_events.build_library_view_event(section))
    except Exception:
        logger.debug("[Telemetry] failed to build/submit flag.library event", exc_info=True)


def _report_library_paths():
    """Submit the detail.library stage-3 event: how the configured download
    paths are spread over the media kinds.

    Counts only -- one number per kind, the total, and how many paths carry
    more than one kind. No path, label, drive or share name is read at all
    (only the `media_kinds` values are looked at), and no file/title counts are
    involved. An empty, never-scanned or currently unreachable location
    therefore contributes exactly the same as a fully indexed one, and nothing
    in here can fail because a NAS happens to be offline.

    The consent check comes FIRST, before the two DB reads _lib_kinds_map()
    costs -- per TELEMETRY_PLAN.md §3 the gate sits in front of the data
    collection, not in front of the send. build_feature_detail_event() checks
    the same key again; that is the real gate, this one only avoids the work.

    Throttled to once per _TEL_PATHS_MIN_INTERVAL per process: this is a
    configuration snapshot, not an activity signal. Wrapped in its own
    try/except (DEBUG only) so neither a telemetry bug nor a DB hiccup while
    reading the paths can affect the library pages.
    """
    global _tel_paths_last
    try:
        if not telemetry_settings.is_key_enabled("detail.library"):
            return
        now = _time.monotonic()
        with _tel_lock:
            if _tel_paths_last is not None and now - _tel_paths_last < _TEL_PATHS_MIN_INTERVAL:
                return
            _tel_paths_last = now
        kinds_map = _lib_kinds_map()
        # Every known kind is reported, including the ones with a count of 0.
        # "no eBook path configured" and "this client is too old to know about
        # eBook paths" are different answers, and a fixed key set is the only
        # way the server can tell them apart.
        per_kind = {slug: 0 for slug in ALL_SLUGS}
        multi_kind = 0
        for kinds in kinds_map.values():
            for slug in kinds:
                if slug in per_kind:
                    per_kind[slug] += 1
            if len(kinds) > 1:
                multi_kind += 1
        event = telemetry_events.build_feature_detail_event(
            "detail.library", action="paths", status="success",
            metadata={
                "paths_total": len(kinds_map),
                "paths_per_kind": per_kind,
                "paths_multi_kind": multi_kind,
            },
        )
        if event:
            telemetry_client.submit(event)
    except Exception:
        logger.debug("[Telemetry] failed to build/submit detail.library event", exc_info=True)


def register_library_routes(app):
    """Register the Library page and its supporting API routes (listing,
    refresh/status/watcher polling, delete, media info, rename, move) on
    the Flask app."""
    @app.route("/library")
    def library_page():
        """Render the library hub -- one tile per media kind. GET /library.

        Kept under the endpoint name `library_page` although the page it
        renders changed completely: `url_for('library_page')` appears in the
        sidebar, in saved third-party module navigation and in the PWA start
        URL, and renaming the endpoint would 500 every one of them.
        """
        _report_library_view(telemetry_events.LIBRARY_HUB_SECTION)
        _report_library_paths()
        return render_template("library_hub.html", media_kinds=MEDIA_KINDS)

    @app.route("/library/<kind_url>")
    def library_kind_page(kind_url):
        """Render one media kind's library. GET /library/<video|books|...>.

        Unknown segments 404 instead of falling back to the hub: silently
        serving a different page for a mistyped URL hides broken links in
        modules and bookmarks rather than surfacing them.
        """
        entry = get_kind_by_url(kind_url)
        if entry is None:
            abort(404)
        # Reported AFTER the 404 above, so a mistyped or probed URL never
        # becomes a data point, and BEFORE the render, so the signal is "the
        # user asked for this section" rather than "the template happened to
        # render". A "coming soon" section counts as a view like any other.
        _report_library_view(entry["slug"])
        _report_library_paths()
        if not entry["available"]:
            # Reachable on purpose -- the sidebar entry is disabled, but the
            # hub tile and a direct link still have to land somewhere that
            # explains itself instead of 404ing.
            return render_template("library_soon.html", kind=entry)
        # Keyed off the URL segment, not the slug: the template is named after
        # the page it renders (library_books.html), while the slug names the
        # data ("book"). Using the slug here silently looked for
        # library_book.html and 500'd.
        return render_template(f"library_{entry['url']}.html", kind=entry)

    @app.route("/api/library/kinds")
    def api_library_kinds():
        """The media-kind registry. GET /api/library/kinds.

        Exposed so external modules (and the Settings page) do not have to
        hard-code the list; see web/media_kinds.py.
        """
        return jsonify({"kinds": kinds_for_api()})

    @app.route("/api/library/overview")
    def api_library_overview():
        """Counters for the hub tiles. GET /api/library/overview.

        Called from static/library_hub.js.
        """
        cache = get_library_cache_status()
        return jsonify({
            "counts": _lib_overview_counts(),
            "is_scanning": any(e["is_scanning"] for e in cache.values()),
            "last_updated": max((e["scanned_at"] for e in cache.values()), default=0),
        })
    @app.route("/api/library")
    def api_library():
        """Return one library's listing across the scan targets that feed it,
        triggering an initial background scan for any of them that has never
        been scanned yet. GET /api/library?kind=video|book.

        `kind` defaults to "video" so the callers that predate the split --
        static/syncplay_page.js's inline library loader, and any third-party
        module hitting this endpoint -- keep getting exactly what they used to
        get. Paths the user has not assigned to the requested kind are left
        out entirely, and so is the other kind's payload: a video-only request
        no longer ships the eBook shelf it never renders, which on a large
        library is the difference between a few hundred KB and a few MB per
        page load.

        Called from static/library_core.js's `libFetch()`."""
        kind = (request.args.get("kind") or KIND_VIDEO).strip().lower()
        if kind not in (KIND_VIDEO, KIND_BOOK, KIND_COMIC):
            return jsonify({"error": "unknown media kind"}), 400

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        kinds_map = _lib_kinds_map()
        targets = [t for t in _lib_build_scan_targets()
                   if kind in (kinds_map.get(_lib_path_key(t[1])) or [])]
        cache = get_all_library_cache()

        locations = []
        any_scanning = False
        needs_initial_scan = []

        def _slim(data):
            """Keep only the shelf this request is about.

            Three lists live in one cache row now, and a request for one of
            them has no use for the other two. On a location that holds films
            *and* a comic run this is most of the payload.
            """
            if kind == KIND_VIDEO:
                return dict(data, books=[], comics=[])
            if kind == KIND_BOOK:
                return dict(data, titles=None, lang_folders=None, comics=[])
            return dict(data, titles=None, lang_folders=None, books=[])

        for (label, cp_id, base_path) in targets:
            path_key = "default" if cp_id is None else str(cp_id)
            entry = cache.get(path_key)
            if entry:
                if entry["is_scanning"]:
                    any_scanning = True
                if entry["data"]:
                    data = entry["data"]
                    # Books cached by an older scanner are re-read rather than
                    # served. The scanner's OUTPUT changes with it -- a release
                    # that starts flattening Calibre's HTML descriptions, or
                    # normalising language codes, leaves every previously
                    # scanned book carrying the old shape, and nothing about
                    # the files on disk has changed to trigger a rescan. The
                    # user then sees the old bug and reasonably reports it as
                    # not fixed. Same lesson as the conversion cache version.
                    if data.get("books") and data.get("books_version") != BOOKS_FORMAT_VERSION:
                        data = dict(data, books=[])
                        needs_initial_scan.append((label, cp_id, base_path))
                    if data.get("comics") and data.get("comics_version") != COMICS_FORMAT_VERSION:
                        data = dict(data, comics=[])
                        needs_initial_scan.append((label, cp_id, base_path))
                    if kind == KIND_BOOK:
                        _lib_queue_book_covers(data.get("books") or [])
                    if kind == KIND_COMIC:
                        # Before the covers: this corrects what the shelf shows
                        # about rows it is about to render.
                        _lib_refresh_comic_conversion_state(data.get("comics") or [])
                        _lib_queue_comic_covers(data.get("comics") or [])
                    locations.append(_slim(data))
            else:
                # Never scanned yet — trigger once
                needs_initial_scan.append((label, cp_id, base_path))

        # `not any_scanning` is a guard against starting a second scan while
        # one runs, and _lib_do_scan holds a lock anyway. But any_scanning is
        # true for a location that is CURRENTLY scanning OR whose is_scanning
        # flag was left set by a crashed run -- and in the second case this
        # skips the rescan on every request, forever. A location whose books
        # were emptied above (a scanner-version bump) then shows "no books
        # found" permanently, with nothing on disk changed to fix it.
        #
        # Retried on the next request rather than forced through here: this
        # runs on a page load, and a stuck flag is cleared by the scan that
        # eventually starts, not by a second one queued on top of it.
        if needs_initial_scan and not any_scanning:
            _lib_trigger_scan_async(needs_initial_scan, lang_sep)
            any_scanning = True
        elif needs_initial_scan:
            logger.info("[Library] %s location(s) need a rescan but a scan is "
                        "already flagged as running -- retrying on the next request",
                        len(needs_initial_scan))

        # Watcher status
        watcher = _get_lib_watcher()
        last_updated = max((e["scanned_at"] for e in cache.values()), default=0)

        return jsonify({
            "kind": kind,
            "lang_sep": lang_sep,
            "locations": locations,
            "is_scanning": any_scanning,
            "last_updated": last_updated,
            "watcher": {
                "available": watcher.available,
                "active": watcher.active,
                "watched": watcher.watched,
            },
        })
    @app.route("/api/library/refresh", methods=["POST"])
    def api_library_refresh():
        """Invalidate the library cache and trigger a full rescan of all
        targets, restarting the file watcher against the current target
        list. POST /api/library/refresh.

        Called from static/library_core.js's `libLoad()`."""
        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        targets = _lib_build_scan_targets()
        invalidate_library_cache()
        _lib_trigger_scan_async(targets, lang_sep)
        # Restart watcher so it picks up any newly configured paths
        _get_lib_watcher().restart(targets, _lib_watcher_scan_callback)
        return jsonify({"ok": True, "scanning": True})
    @app.route("/api/library/status")
    def api_library_status():
        """Lightweight endpoint: returns only scanning state + last_updated timestamp.
        Used by the UI to detect watcher-triggered rescans without transferring location data.
        GET /api/library/status.

        Called from static/library_core.js's `libIdlePoll()`/`libPollScan()`."""
        # Status only -- get_all_library_cache() would parse the whole cached
        # listing (several MB on a large library) for two numbers.
        cache = get_library_cache_status()
        any_scanning = any(e["is_scanning"] for e in cache.values())
        last_updated = max((e["scanned_at"] for e in cache.values()), default=0)
        return jsonify({"is_scanning": any_scanning, "last_updated": last_updated})
    @app.route("/api/library/watcher")
    def api_library_watcher():
        """Return the library file watcher's current availability/active state
        and watched paths. GET /api/library/watcher. No confirmed frontend
        caller was found in static/templates (the same data is also embedded
        in the /api/library response, which the UI reads via
        `libUpdateWatcherStatus()`)."""
        watcher = _get_lib_watcher()
        return jsonify({
            "available": watcher.available,
            "active": watcher.active,
            "watched": watcher.watched,
        })
    @app.route("/api/library/book/cover")
    def api_library_book_cover():
        """Serve a cover image that sits next to a book in the library.

        GET /api/library/book/cover?path=<absolute path to the image>

        Two independent restrictions, because this reads a file the client
        named: the path has to resolve inside a configured scan target (the
        same guard every other library route uses) and the extension has to be
        one of a short list of image types. Without the second check this would
        be "read any file inside the library and hand it to the browser".

        The image is served THROUGH the cover cache rather than straight off
        the disk, so a sidecar cover is downscaled once instead of being sent
        at full size on every card -- a Calibre cover.jpg is routinely three
        megabytes for a tile 160 pixels wide. If the cache cannot take it, the
        original is still served: a large cover beats no cover.

        Not cached forever on purpose: `private` keeps it out of shared proxies
        because a library path is not public, and a day is long enough that
        scrolling the shelf costs nothing.
        """
        from flask import send_file
        from ..books import covers as book_covers
        resolved = lib_resolve_library_file(request.args.get("path", ""), exts=BOOK_COVER_EXTS)
        if resolved is None:
            return jsonify({"error": "not found"}), 404
        served, mimetype = resolved, None
        try:
            key = book_covers.cache_key(resolved)
            cached = book_covers._CACHE.cached(key)
            if cached is None:
                cached = book_covers._CACHE.store_image(
                    key, resolved, resolved.name, resolved.read_bytes())
            if cached is not None:
                served, mimetype = cached, book_covers.cover_mimetype(cached)
        except (OSError, MemoryError):
            logger.debug("[Books] Serving %s uncached", resolved.name, exc_info=True)
        response = send_file(str(served), mimetype=mimetype, conditional=True)
        # Same bound as the two cover routes below: this one goes through the
        # cover cache too, so "Clear cover cache" can delete what the browser
        # is still painting. Revalidation is a 304 off the ETag.
        response.headers["Cache-Control"] = "private, max-age=300, must-revalidate"
        return response

    @app.route("/api/library/book/embedded-cover")
    def api_library_book_embedded_cover():
        """Serve the cover from INSIDE a book. GET …?path=<path to the book>

        The counterpart of the route above: that one takes a picture the
        library already has as a file, this one takes the one an EPUB carries
        inside itself. Both answer with a cached, downscaled image; the shelf
        asks for this one whenever a book has no sidecar cover, which is most
        of them.

        start_conversion=False, always: a shelf render must never kick off two
        hundred MOBI conversions because it drew two hundred cards. The
        background worker (books/covers.py) is what fills those in, and the
        shelf polls its progress.
        """
        from flask import send_file
        from ..books import covers as book_covers
        resolved = lib_resolve_library_file(request.args.get("path", ""), exts=BOOK_ALL_EXTS)
        if resolved is None:
            return jsonify({"error": "not found"}), 404
        cached = book_covers.cover_path(resolved, start_conversion=False)
        if cached is None:
            # A cover that does not exist YET is not a fact worth remembering.
            # Browsers heuristically cache a 404 that carries no caching
            # headers, so the miss a card collected before the background
            # worker got to it was served from the browser's own cache
            # afterwards -- the picture existed on disk and the shelf still
            # showed a blank tile until a hard reload. no-store is what makes
            # the next request actually ask.
            missing = jsonify({"error": "no cover"})
            missing.headers["Cache-Control"] = "no-store"
            return missing, 404
        response = send_file(str(cached), mimetype=book_covers.cover_mimetype(cached),
                             conditional=True)
        # A cover is not immutable the way a page inside an archive is: the
        # "Clear cover cache" button in Settings deletes it, and the URL
        # carries no content hash that would change when it does. A full day of
        # max-age meant the browser kept painting covers that no longer existed
        # on disk -- so the shelf looked fine while the settings page correctly
        # reported an empty cache, and nothing ever asked for them again, so
        # nothing was regenerated either.
        #
        # Five minutes plus must-revalidate keeps scrolling a shelf of hundreds
        # of cards free while bounding how long a deleted cover can survive.
        # send_file(conditional=True) above answers the revalidation with a 304
        # from the ETag, so this costs headers, not pictures.
        response.headers["Cache-Control"] = "private, max-age=300, must-revalidate"
        return response

    @app.route("/api/library/book/covers/status")
    def api_library_book_covers_status():
        """How far the background cover preparation has got.

        GET /api/library/book/covers/status
        -> {running, total, done, failed, pending, current, finished_at}

        Read-only and cheap: the shelf polls it while covers are still being
        made, so it must not start anything or touch the disk.
        """
        from ..books import covers as book_covers
        return jsonify(book_covers.preparation_status())

    @app.route("/api/library/book/cache")
    def api_library_book_cache():
        """What the book caches cost. GET /api/library/book/cache

        One request for the whole eBooks block on the Library settings tab, so
        "clear" is pressed with a number in view rather than blind. Admin only
        -- it reports server-wide state and its sibling deletes files.
        """
        from ..books import convert as book_convert
        from ..books import covers as book_covers
        return jsonify({
            "ok": True,
            "covers": book_covers.cache_stats(),
            "converted": book_convert.cache_stats(),
        })

    @app.route("/api/library/book/cache/clear", methods=["POST"])
    def api_library_book_cache_clear():
        """Empty one of the two book caches.

        POST /api/library/book/cache/clear  {"cache": "covers"|"converted"}

        Both hold nothing but derived data: a cover is re-extracted the next
        time the shelf is drawn, a conversion redone the next time the book is
        opened. The book files themselves are never touched -- the cleanup
        functions only ever look inside the config directory.

        A whitelist of two names rather than a path: nothing the client sends
        may decide *what* gets deleted. Admin only.

        Deliberately separate from the comic caches, which have their own
        endpoint and their own directory. They share an implementation
        (web/covercache.py), never a cache: clearing one must not throw away
        the other's work, and the two libraries can be very different sizes.
        """
        from ..books import convert as book_convert
        from ..books import covers as book_covers
        data = request.get_json(silent=True) or {}
        which = str(data.get("cache", "") or "").strip().lower()
        if which == "covers":
            removed = book_covers.cleanup_covers(max_age_days=0)
            book_covers.reset_preparation()
            stats = book_covers.cache_stats()
        elif which == "converted":
            removed = book_convert.cleanup_converted(max_age_days=0)
            stats = book_convert.cache_stats()
        else:
            return jsonify({"ok": False, "error": "unknown cache"}), 400
        logger.info("[Books] Cleared the %s cache (%s entries removed)", which, removed)
        return jsonify({"ok": True, "cache": which, "removed": removed, "stats": stats})

    @app.route("/api/library/book/file")
    def api_library_book_file():
        """Serve a book file itself, for the reader.

        GET /api/library/book/file?path=<absolute path to the book>

        `conditional=True` gives byte ranges, 304s and 206 partial responses
        for free, which is what makes a 50 MB PDF usable in a browser viewer
        instead of a 50 MB download before the first page appears.

        The extension set is BOOK_EXTS, not BOOK_ALL_EXTS: a DRM-protected
        .kfx is listed in the shelf so the user can see it exists, but there is
        nothing a reader could do with the bytes, so it is not served either.
        """
        from flask import send_file
        resolved = lib_resolve_library_file(request.args.get("path", ""), exts=BOOK_EXTS)
        if resolved is None:
            return jsonify({"error": "not found"}), 404
        response = send_file(str(resolved), conditional=True)
        response.headers["Cache-Control"] = "private, max-age=3600"
        # A book is a document the browser must never try to render inline in
        # a top-level context; the reader fetches it with JavaScript.
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/library/book/convert")
    def api_library_book_convert():
        """Ask for the EPUB of a MOBI/AZW3/AZW, converting it if needed.

        GET /api/library/book/convert?path=<absolute path>
            -> {"ready": true, "key": ...} | {"pending": true} | {"failed": ...}

        Polled by the reader, exactly like the player polls the seek-preview
        sprites. The conversion runs on a worker thread so the request returns
        immediately even for a large book.
        """
        from ..books.convert import conversion_status
        resolved = lib_resolve_library_file(request.args.get("path", ""), exts=BOOK_CONVERTIBLE_EXTS)
        if resolved is None:
            return jsonify({"failed": True, "reason": "not_found"}), 404
        return jsonify(conversion_status(resolved))

    @app.route("/api/library/book/converted/<key>.epub")
    def api_library_book_converted(key):
        """Serve a finished conversion by its cache key.

        The key is a hash, not a path: it is validated against a strict hex
        pattern and then resolved inside the conversion cache, so this route
        cannot be talked into reading anything else.
        """
        from flask import send_file
        from ..books.convert import converted_path
        try:
            target = converted_path(key)
        except (ValueError, OSError):
            return jsonify({"error": "not found"}), 404
        if not target.is_file():
            return jsonify({"error": "not found"}), 404
        response = send_file(str(target), conditional=True, mimetype="application/epub+zip")
        response.headers["Cache-Control"] = "private, max-age=3600"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/library/delete", methods=["POST"])
    def api_library_delete():
        """Delete an entire title, a season, or a single episode from disk
        (path-traversal-safe), then invalidate the library cache.
        POST /api/library/delete.

        Called from static/library_video.js's `libDeleteTitle()`,
        `libDeleteSeason()`, and `libDeleteEpisode()` (via `libApiPost()`)."""
        import shutil
        from pathlib import Path

        data = request.get_json(silent=True) or {}
        folder = data.get("folder", "")
        season = data.get("season")  # int or null
        episode = data.get("episode")  # int or null
        custom_path_id = data.get("custom_path_id")  # int or null

        # Security: reject dangerous folder names
        if (
            not folder
            or ".." in folder
            or "/" in folder
            or "\\" in folder
            or "\x00" in folder
        ):
            return jsonify({"error": "Invalid folder name"}), 400

        # Resolve base path from custom_path_id or default
        if custom_path_id:
            cp = get_custom_path_by_id(custom_path_id)
            if not cp:
                return jsonify({"error": "Custom path not found"}), 404
            dl_base = Path(cp["path"]).expanduser()
            if not dl_base.is_absolute():
                dl_base = Path.home() / dl_base
        else:
            raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
            if raw:
                dl_base = Path(raw).expanduser()
                if not dl_base.is_absolute():
                    dl_base = Path.home() / dl_base
            else:
                dl_base = Path.home() / "Downloads"

        # Resolve the base itself to eliminate symlinks in the configured path
        dl_base = dl_base.resolve()

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        lang_folders = LANG_FOLDERS
        lang_folder = data.get("lang_folder")  # str or null

        if lang_sep and lang_folder:
            if lang_folder not in lang_folders:
                return jsonify({"error": "Invalid language folder"}), 400
            bases = [dl_base / lang_folder]
        elif lang_sep:
            bases = [dl_base / lf for lf in lang_folders]
        else:
            bases = [dl_base]

        deleted = 0
        for base in bases:
            title_path = base / folder
            # Verify resolved path stays within the allowed base (blocks symlink escapes)
            try:
                title_path = _lib_assert_within_root(title_path, base)
            except ValueError:
                continue
            if not title_path.is_dir():
                continue

            if season is None and episode is None:
                # Delete entire title
                shutil.rmtree(title_path, ignore_errors=True)
                deleted += 1
            else:
                # Which files belong to this season/episode is decided by the
                # SAME parser the scan used. It used to be a second, narrower
                # regex built here with fixed zero-padding (S01E001), so every
                # spelling the scanner accepts but that one does not -- "S1E1",
                # "1x05", "Staffel 2/Folge 7" -- was listed on the page and
                # then silently not deleted. One parser, one answer.
                season_num = int(season)
                episode_num = int(episode) if episode is not None else None

                def _matches(path):
                    parsed = _lib_parse_episode(path.name, path.parent.name)
                    if parsed is None:
                        return False
                    return parsed[0] == season_num and (
                        episode_num is None or parsed[1] == episode_num)

                for f in list(title_path.rglob("*")):
                    if f.is_file() and _matches(f):
                        try:
                            f.unlink()
                            deleted += 1
                        except OSError:
                            pass

                # Cleanup empty directories bottom-up
                for dirpath in sorted(
                    title_path.rglob("*"), key=lambda p: len(p.parts), reverse=True
                ):
                    if dirpath.is_dir():
                        try:
                            dirpath.rmdir()  # only succeeds if empty
                        except OSError:
                            pass
                # Remove title folder itself if empty
                try:
                    title_path.rmdir()
                except OSError:
                    pass

        if deleted == 0:
            return jsonify({"error": "Nothing found to delete"}), 404
        invalidate_library_cache()
        return jsonify({"ok": True, "deleted": deleted})
    @app.route("/api/library/media_info", methods=["POST"])
    def api_library_media_info():
        """Run ffprobe on a library file and return parsed video/audio stream
        details (codec, resolution, bitrate, HDR range, etc.). Path must
        resolve inside one of the known scan targets. POST /api/library/media_info.

        Called from static/library_video.js's `libOpenMediaInfo()`."""
        import subprocess
        import json

        data = request.get_json(silent=True) or {}
        path = data.get("path")
        if not path:
            return jsonify({"error": "Path required"}), 400

        # Security check: the path must resolve to a media file inside one of
        # the scanned library bases (shared helper, see lib_resolve_library_file)
        path_obj = lib_resolve_library_file(path)
        if path_obj is None:
            return jsonify({"error": "Access denied"}), 403

        # Run ffprobe
        try:
            from ..transcoder import _ffprobe_bin
            ffprobe = _ffprobe_bin()
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path_obj)],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode != 0:
                return jsonify({"error": "ffprobe failed"}), 500
            probe_data = json.loads(r.stdout)
        except Exception as e:
            return jsonify({"error": f"Failed to run ffprobe: {e}"}), 500

        fmt = probe_data.get("format", {})
        streams = probe_data.get("streams", [])

        # 1. Basic properties
        info = {
            "filename": path_obj.name,
            "container": path_obj.suffix.lstrip(".").lower(),
            "path": str(path_obj),
            "size_bytes": path_obj.stat().st_size,
        }

        # 2. Extract Video & Audio Streams
        video = None
        audio = None

        for s in streams:
            ct = s.get("codec_type")
            if ct == "video" and not video:
                v_codec = s.get("codec_name", "").upper()
                v_profile = s.get("profile", "Unknown")
                v_level = s.get("level")
                v_level_str = str(v_level) if v_level is not None else ""

                # Width x Height
                w = s.get("width", 0)
                h = s.get("height", 0)
                res_str = f"{w}x{h}" if w and h else ""

                # Aspect ratio
                dar = s.get("display_aspect_ratio", "")

                # Framerate
                r_fr = s.get("r_frame_rate", "")
                framerate = ""
                if r_fr and "/" in r_fr:
                    try:
                        num, den = map(int, r_fr.split("/"))
                        if den > 0:
                            framerate = f"{round(num / den)}"
                    except ValueError:
                        pass

                # Bit depth
                pix_fmt = s.get("pix_fmt", "")
                bit_depth = 8
                if "10" in pix_fmt:
                    bit_depth = 10
                elif "12" in pix_fmt:
                    bit_depth = 12

                # Video range
                color_tr = s.get("color_transfer", "")
                v_range = "SDR"
                if color_tr in ["smpte2084", "arib-std-b67"]:
                    v_range = "HDR"

                # Bitrate
                v_br = s.get("bit_rate") or fmt.get("bit_rate")
                v_bitrate_kbps = ""
                if v_br:
                    try:
                        v_bitrate_kbps = f"{int(v_br) // 1000} kbps"
                    except ValueError:
                        pass

                # AVC
                is_avc = "Yes" if s.get("is_avc") in [True, "true", "1", 1] else "No"

                # Refs & NAL
                refs = s.get("refs", "")
                nal = s.get("nal_length_size", "")

                video = {
                    "codec": v_codec,
                    "profile": v_profile,
                    "level": v_level_str,
                    "resolution": res_str,
                    "aspect_ratio": dar,
                    "framerate": framerate,
                    "bit_depth": f"{bit_depth} bit",
                    "video_range": v_range,
                    "pixel_format": pix_fmt,
                    "bitrate": v_bitrate_kbps,
                    "avc": is_avc,
                    "refs": str(refs) if refs != "" else "",
                    "nal": str(nal) if nal != "" else "",
                }

            elif ct == "audio" and not audio:
                a_codec = s.get("codec_name", "").upper()
                a_profile = s.get("profile", "Unknown")

                # Channels & Layout
                channels = s.get("channels", "")
                layout = s.get("channel_layout", "")

                # Language
                lang = s.get("tags", {}).get("language", "und")

                # Bitrate
                a_br = s.get("bit_rate")
                a_bitrate_kbps = ""
                if a_br:
                    try:
                        a_bitrate_kbps = f"{int(a_br) // 1000} kbps"
                    except ValueError:
                        pass

                # Sample rate
                sr = s.get("sample_rate", "")
                sr_str = f"{sr} Hz" if sr else ""

                # Default / Forced
                disp = s.get("disposition", {})
                is_default = "Yes" if disp.get("default") == 1 else "No"
                is_forced = "Yes" if disp.get("forced") == 1 else "No"

                audio = {
                    "codec": a_codec,
                    "profile": a_profile,
                    "channels": f"{channels} ch" if channels else "",
                    "layout": layout,
                    "language": lang,
                    "bitrate": a_bitrate_kbps,
                    "sample_rate": sr_str,
                    "default": is_default,
                    "forced": is_forced,
                }

        info["video"] = video
        info["audio"] = audio
        return jsonify(info)
    @app.route("/api/library/rename", methods=["POST"])
    def api_library_rename():
        """Rename a title folder, a season folder, or a single episode file
        (path-traversal-safe), then invalidate the library cache.
        POST /api/library/rename.

        Called from static/library_video.js's `libStartRename()` and
        `libStartEpRename()`."""
        from pathlib import Path
        data = request.get_json(silent=True) or {}
        folder    = data.get("folder", "")
        new_name  = data.get("new_name", "").strip()
        season    = data.get("season")      # int → rename season folder; None → rename title folder
        episode   = data.get("episode")     # int → rename specific episode file; None → season/title level
        old_file  = data.get("old_file")    # original filename for episode rename
        custom_path_id = data.get("custom_path_id")
        lang_folder    = data.get("lang_folder")

        # Validate inputs
        def _safe(name):
            return name and ".." not in name and "/" not in name and "\\" not in name and "\x00" not in name

        if not _safe(folder) or not new_name:
            return jsonify({"error": "Invalid folder or new name"}), 400
        if not _safe(new_name):
            return jsonify({"error": "New name contains invalid characters"}), 400

        # Resolve base path
        if custom_path_id:
            cp = get_custom_path_by_id(custom_path_id)
            if not cp:
                return jsonify({"error": "Custom path not found"}), 404
            dl_base = Path(cp["path"]).expanduser()
            if not dl_base.is_absolute():
                dl_base = Path.home() / dl_base
        else:
            raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
            dl_base = Path(raw).expanduser() if raw else Path.home() / "Downloads"
            if not dl_base.is_absolute():
                dl_base = Path.home() / dl_base

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        if lang_sep and lang_folder:
            if lang_folder not in _LIB_LANG_FOLDERS:
                return jsonify({"error": "Invalid language folder"}), 400
            base = dl_base / lang_folder
        else:
            base = dl_base

        # Resolve base to eliminate symlinks in the configured path
        base = base.resolve()

        try:
            title_path = _lib_assert_within_root(base / folder, base)
        except ValueError:
            return jsonify({"error": "Path traversal detected"}), 400

        if episode is not None and old_file:
            # Rename a specific episode file
            if season is None:
                return jsonify({"error": "season required for episode rename"}), 400
            season_path = title_path / ("Staffel " + str(int(season)))
            if not season_path.is_dir():
                # Try without Staffel prefix — flat layout
                season_path = title_path
            try:
                src = _lib_assert_within_root(season_path / old_file, base)
            except ValueError:
                return jsonify({"error": "Path traversal detected"}), 400
            if not src.is_file():
                return jsonify({"error": "File not found"}), 404
            dst = src.parent / new_name
            if dst.exists():
                return jsonify({"error": "Target name already exists"}), 409
            src.rename(dst)
        else:
            # Rename title folder
            if not title_path.is_dir():
                return jsonify({"error": "Folder not found"}), 404
            dst = title_path.parent / new_name
            if dst.exists():
                return jsonify({"error": "Target name already exists"}), 409
            title_path.rename(dst)

        invalidate_library_cache()
        return jsonify({"ok": True})
    @app.route("/api/library/move", methods=["POST"])
    def api_library_move():
        """Start an async move job. Returns {job_id} immediately.

        POST /api/library/move. Handles both a title folder (series) and
        loose movie files sitting directly in the base folder, validating
        source/destination paths against traversal before spawning the
        background worker thread. Called from static/library_video.js's
        `libConfirmMove()`."""
        import uuid
        from pathlib import Path
        data = request.get_json(silent=True) or {}
        folder      = data.get("folder", "")
        from_cp_id  = data.get("from_custom_path_id")
        to_cp_id    = data.get("to_custom_path_id")
        lang_folder = data.get("lang_folder")

        def _safe(name):
            return name and ".." not in name and "/" not in name and "\\" not in name and "\x00" not in name

        if not _safe(folder):
            return jsonify({"error": "Invalid folder name"}), 400

        from_base = _lib_move_resolve_base(from_cp_id)
        to_base   = _lib_move_resolve_base(to_cp_id)
        if from_base is None or to_base is None:
            return jsonify({"error": "Invalid path configuration"}), 400

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        if lang_sep and lang_folder:
            if lang_folder not in _LIB_LANG_FOLDERS:
                return jsonify({"error": "Invalid language folder"}), 400
            from_base = from_base / lang_folder
            to_base   = to_base   / lang_folder

        src = (from_base / folder).resolve()
        try:
            src.relative_to(from_base.resolve())
        except ValueError:
            return jsonify({"error": "Path traversal detected"}), 400

        # Check if source is a directory (series) or loose files directly in base (movie)
        loose_files = []
        if not src.is_dir():
            # Movie files sitting directly in the base folder (e.g. Film.mkv, Film.srt)
            loose_files = [f for f in from_base.iterdir()
                           if f.is_file() and f.stem == folder]
            if not loose_files:
                return jsonify({"error": "Source folder not found"}), 404

        if loose_files:
            # Loose files → move each file to to_base (no subfolder)
            dst = to_base
            for lf in loose_files:
                if (dst / lf.name).exists():
                    return jsonify({"error": "Ziel existiert bereits am Speicherort"}), 409
        else:
            dst = to_base / folder
            if dst.resolve() == src.resolve():
                return jsonify({"error": "Quelle und Ziel sind identisch"}), 400
            if dst.exists():
                return jsonify({"error": "Ziel existiert bereits am Speicherort"}), 409

        try:
            to_base.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return jsonify({"error": f"Zielordner konnte nicht erstellt werden: {exc}"}), 500

        job_id = uuid.uuid4().hex[:12]
        with _move_jobs_lock:
            _move_jobs[job_id] = {
                "status": "starting",
                "copied_bytes": 0,
                "total_bytes": 0,
                "current_file": "",
                "error": None,
                "folder": folder,
            }

        if loose_files:
            t = threading.Thread(
                target=_lib_move_loose_files_worker,
                args=(job_id, [str(f) for f in loose_files], str(dst)),
                daemon=True,
            )
        else:
            t = threading.Thread(target=_lib_move_worker, args=(job_id, str(src), str(dst)), daemon=True)
        t.start()
        return jsonify({"job_id": job_id})
    @app.route("/api/library/move_status/<job_id>")
    def api_library_move_status(job_id):
        """Poll move job progress.

        GET /api/library/move_status/<job_id>. Removes the job entry once
        its final (done/error) state has been polled once. Called from
        static/library_video.js's `libConfirmMove()`."""
        with _move_jobs_lock:
            job = _move_jobs.get(job_id)
            if job is None:
                return jsonify({"error": "Job nicht gefunden"}), 404
            result = dict(job)
            # Clean up finished jobs after first poll of final state
            if job["status"] in ("done", "error"):
                _move_jobs.pop(job_id, None)
        return jsonify(result)
