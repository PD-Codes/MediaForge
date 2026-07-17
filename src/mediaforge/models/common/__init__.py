"""Re-exports the site-agnostic helpers from common.py: the ProviderData
container, the check_downloaded() ffmpeg-probe helper, the clean_title()
filename sanitizer, and the shared download()/watch()/syncplay() episode
actions used (directly or via a `download = episode_download`-style alias)
by every site's episode/movie model.

Also exposes movie_folder_enabled(), which the movie-capable models (kinox,
cineby — and, inline, megakino/filmpalast) use to decide whether a downloaded
movie gets its own subfolder.
"""
import os

from .common import (
    ProviderData,
    check_downloaded,
    clean_title,
    download,
    syncplay,
    watch,
)


def movie_folder_enabled():
    """Whether a downloaded movie is placed in its own ``<Title (Year)>``
    subfolder (True) or written straight into the download root (False).

    Off by default; enabled by setting ``MEDIAFORGE_MOVIE_SUBFOLDER`` (or the
    per-site ``MEGAKINO_MOVIE_SUBFOLDER`` / ``FILMPALAST_MOVIE_SUBFOLDER``
    aliases) to ``"1"``. This mirrors the inline check the megakino and
    filmpalast movie models already use, so every movie source lays files out
    the same way.
    """
    return (
        os.getenv("MEDIAFORGE_MOVIE_SUBFOLDER", "0") == "1"
        or os.getenv("MEGAKINO_MOVIE_SUBFOLDER", "0") == "1"
        or os.getenv("FILMPALAST_MOVIE_SUBFOLDER", "0") == "1"
    )


__all__ = [
    "ProviderData",
    "check_downloaded",
    "clean_title",
    "download",
    "movie_folder_enabled",
    "syncplay",
    "watch",
]
