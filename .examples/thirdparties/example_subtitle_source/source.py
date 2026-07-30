"""Example subtitle source -- demonstrates the fetch() contract, offline-safe.

``example_subtitle_source/__init__.py`` registers :func:`fetch` as the module's
external subtitle source. It is asked in the LAST step of the download path's
subtitle chain -- after yt-dlp's own renditions and after the hoster's
out-of-band player config, next to the built-in OpenSubtitles lookup -- and only
ever for the languages the finished file does not already have.

This demo makes no network call and always returns an empty list: it only shows
what the arguments carry and what a real source would do with them. A real
source replaces the body with a couple of short-timeout HTTP requests (via
``requests``, already a project dependency), writes the sidecar files it got and
returns their paths. Everything else -- the signature, the "never fetch a
language twice" rule, the "never raise" rule -- stays exactly the same.
"""
from __future__ import annotations

from pathlib import Path

from ...db import get_setting
from ....logger import get_logger

logger = get_logger(__name__)

# The module's single master toggle (see __init__.py's register_thirdparty).
ENABLED_KEY = "example_subtitle_source_enabled"

# Languages this demo would look for, as ISO 639-2/B tags -- the same form
# have_langs uses, so the two can be compared directly.
DEMO_LANGUAGES = ("ger", "eng")


def fetch(video_path, have_langs, meta) -> list:
    """``fetch(video_path, have_langs, meta) -> [Path, ...]``.

    - ``video_path``: the finished (still temporary) video file. Its size and
      content are readable, which is what a hash-based match needs.
    - ``have_langs``: a set of ISO 639-2/B tags the file ALREADY has. Never
      fetch these again -- those tracks are timed to this exact stream, an
      external one very likely is not.
    - ``meta``: ``{"query", "season", "episode", "imdb_id", "tmdb_id"}``, each
      value possibly ``None`` (a direct-link download knows none of them).

    Returns the sidecar paths written, named ``<video stem>.<lang>.<ext>`` so
    the existing collect/mux path picks them up and muxes them into the .mkv as
    tagged soft-sub tracks. Anything else is ignored.

    This runs in the queue worker BETWEEN the download and the ffmpeg mux, so it
    holds up that one episode: keep it to a couple of HTTP requests with short
    timeouts. It must not raise -- exceptions are caught and logged, but a
    source that throws on every episode is just dead weight.
    """
    # Follow the module's master toggle, so a switched-off module stops
    # contributing immediately. (Uninstall is handled by the item_id passed to
    # register_subtitle_source(); see __init__.py.)
    if get_setting(ENABLED_KEY, "0") != "1":
        return []

    video_path = Path(video_path)

    # Ask only for what is still missing. Skipping the request entirely when
    # nothing is missing is the whole point of have_langs: no wasted call, no
    # wasted API quota.
    wanted = [lang for lang in DEMO_LANGUAGES if lang not in set(have_langs or ())]
    if not wanted:
        logger.debug("[ExampleSubs] Nothing missing for %s", video_path.name)
        return []

    # A real source would now do its lookup, e.g.:
    #   1. compute a hash over the file (size + first/last 64 KiB) and try an
    #      exact-release match -- those subtitles are actually in sync;
    #   2. fall back to a title search built from meta["query"] plus
    #      meta["season"] / meta["episode"], or meta["imdb_id"] /
    #      meta["tmdb_id"] when they are known;
    #   3. write each result next to the video as
    #      video_path.with_suffix(f".{lang}.srt") and collect that path.
    logger.info(
        "[ExampleSubs] Would look up %s for %r (S%sE%s) -- demo source, nothing fetched",
        ", ".join(wanted), meta.get("query"), meta.get("season"), meta.get("episode"),
    )

    # Demo: no file was written, so nothing is reported back.
    return []
