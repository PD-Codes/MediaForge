"""Registry for external subtitle sources (the last step of the subtitle chain).

The download path collects subtitles in three passes, cheapest first: the
renditions yt-dlp finds in the stream, the tracks the hoster's player config
carries out of band, and -- only for the languages still missing after those
two -- an *external* lookup. OpenSubtitles.com is the built-in implementation
of that third pass (``models/common/opensubtitles.py``).

This module is what makes that third pass extensible. A third-party module can
register its own subtitle service (a private Plex-style server, a fansub index,
a paid API) and it is asked in exactly the same place, under the same rule:
never for a language the file already has, always allowed to fail.

Same ``item_id`` convention as every other secondary registry, so
``web/thirdparties/registry.py``'s ``unregister_module()`` cleans registrations
up when a module is disabled or uninstalled.

Deliberately free of any ``mediaforge.web`` import: this is core, and the web
layer imports it, never the other way round.
"""

from __future__ import annotations

import threading

from .logger import get_logger

logger = get_logger(__name__)

# Source ids the core already owns, so a module cannot shadow the built-in.
RESERVED_SOURCE_IDS = frozenset({"opensubtitles"})

_lock = threading.Lock()

# item_id -> {"source_id", "label", "fetch"}
_EXTRA_SUBTITLE_SOURCES: dict = {}


def register_subtitle_source(item_id, source_id, label, fetch) -> None:
    """Add a subtitle source to the download path's external lookup step.

    - ``item_id``: the id the module already passed to ``register_thirdparty()``.
      Registrations are keyed by it so ``unregister_module()`` drops them
      automatically -- a source registered under any other id keeps running
      after the module is gone.
    - ``source_id``: stable id, used in log lines. Must not collide with a
      built-in (:data:`RESERVED_SOURCE_IDS`) or another registered source.
    - ``label``: human-readable name for logs and the module manager.
    - ``fetch``: ``fetch(video_path, have_langs, meta) -> [Path, ...]``.

      * ``video_path``: the finished (still temporary) video file. Its
        ``.stat().st_size`` and content are available, which is what a
        hash-based match needs.
      * ``have_langs``: ISO 639-2/B tags already present. A source **must not**
        fetch these again -- they are already timed to this exact stream.
      * ``meta``: ``{"query", "season", "episode", "imdb_id", "tmdb_id"}``,
        each possibly ``None`` (a direct-link download knows none of them).
      * Return the sidecar paths written, named ``<video stem>.<lang>.<ext>``
        so the existing collect/mux path picks them up. Anything else is
        ignored.

    ``fetch`` runs inside the queue worker, between the download and the
    ffmpeg mux, so it holds up that one episode: keep it to a couple of HTTP
    requests with short timeouts. It must not raise -- exceptions are caught
    and logged, but a source that throws on every episode is just dead weight.
    """
    if not callable(fetch):
        raise ValueError("register_subtitle_source: fetch must be callable")
    source_id = str(source_id or "").strip().lower()
    if not source_id:
        raise ValueError("register_subtitle_source: source_id is required")
    if source_id in RESERVED_SOURCE_IDS:
        raise ValueError(f"register_subtitle_source: {source_id!r} is a built-in source id")
    with _lock:
        for owner, entry in _EXTRA_SUBTITLE_SOURCES.items():
            if owner != item_id and entry["source_id"] == source_id:
                raise ValueError(
                    f"register_subtitle_source: {source_id!r} is already registered by {owner!r}"
                )
        _EXTRA_SUBTITLE_SOURCES[item_id] = {
            "source_id": source_id,
            "label": str(label or source_id),
            "fetch": fetch,
        }
    logger.info("[Subtitles] Registered third-party subtitle source: %s (%s)", source_id, item_id)


def unregister_subtitle_source(item_id) -> None:
    """Drop a source previously added via :func:`register_subtitle_source`."""
    with _lock:
        removed = _EXTRA_SUBTITLE_SOURCES.pop(item_id, None)
    if removed:
        logger.info("[Subtitles] Unregistered third-party subtitle source: %s (%s)",
                    removed["source_id"], item_id)


def thirdparty_subtitle_source_ids() -> set:
    """item_ids that currently own a subtitle source.

    Read-only counterpart of :func:`unregister_subtitle_source`, used by the
    Modulmanager's capability list so it can report what a module added
    without reaching into this module's private dict.
    """
    with _lock:
        return set(_EXTRA_SUBTITLE_SOURCES)


def iter_subtitle_sources() -> list:
    """Every *active* registered source as a list of copies, in registration
    order. A source whose module is switched off is left out -- see
    module_gate.py for why the enabled check lives at the point of use."""
    from .module_gate import filter_enabled

    with _lock:
        snapshot = dict(_EXTRA_SUBTITLE_SOURCES)
    return [dict(entry) for entry in filter_enabled(snapshot).values()]
