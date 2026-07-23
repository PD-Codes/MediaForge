"""Download queue worker and its URL/window helper functions."""

import json
import re
import threading
import time

from pathlib import Path

from ..logger import get_logger
from ..providers import resolve_provider
from .db import (
    claim_next_queued,
    get_custom_path_by_id,
    get_setting,
    is_queue_cancelled,
    set_queue_status,
    update_queue_errors,
    update_queue_progress,
    update_queue_stats,
)
from .download_history import _record_download_history
from .language_groups import (
    is_group_ref,
    labels_from_provider_data,
    pick_language,
    resolve_chain,
)
from .mediascan import _schedule_mediascan_delayed, _trigger_mediaplayer_refresh
from ..telemetry import client as telemetry_client
from ..telemetry import events as telemetry_events
from .runtime_state import (
    _active_cancel_events,
    _active_cancel_events_lock,
    consume_episode_skip,
    get_provider_fallback_chain,
    is_queue_paused,
)
from .upscale_worker import _trigger_batch_after_download_upscale
from .encoding_worker import _trigger_after_download_encode

logger = get_logger(__name__)


# Errors that mean "this hoster has nothing for this episode" (as opposed to a
# transient failure worth retrying on the same hoster): the site didn't offer
# the provider for this episode/language at all, or MediaForge has no extractor
# for it. Both are answered by moving on to the next provider in the chain.
_PROVIDER_UNAVAILABLE_MARKERS = (
    "is not available for",          # AniworldEpisode.redirect_url
    "not found for language",        # SerienstreamEpisode.provider_link (s.to)
    "is not yet implemented",        # extractor missing for this provider
    "no extractor available",
    "provider source not found",     # HTTP 404 on the /redirect/<id> link
    "did not return a stream url",
)


def _is_provider_unavailable_error(exc) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _PROVIDER_UNAVAILABLE_MARKERS)


def _resolve_episode_language(ep_url, chain):
    """First language of `chain` this episode is actually offered in.

    Returns the chain's first language when the episode page can't be read at
    all: guessing beats dropping the episode, and the download surfaces the
    real problem in the normal way. Costs one extra page fetch per episode,
    which is why it only runs for items that use a fallback group.
    """
    try:
        prov = resolve_provider(ep_url)
        probe = prov.episode_cls(url=ep_url)
        pd = probe.provider_data
        available = labels_from_provider_data(pd._data if hasattr(pd, "_data") else pd)
    except Exception as exc:
        logger.warning(
            "[LangGroup] Could not read available languages for %s: %s — "
            "trying '%s'", ep_url, exc, chain[0],
        )
        return chain[0]
    chosen = pick_language(chain, available)
    if chosen is None:
        # Nothing in the chain exists for this episode. Still hand back the
        # first language so the attempt runs and fails with the usual
        # "not available" message instead of a silent skip.
        return chain[0]
    if chosen != chain[0]:
        logger.info(
            "[LangGroup] %s not available in '%s' — falling back to '%s'",
            ep_url, chain[0], chosen,
        )
    return chosen


def _delete_replaced_files(old_paths, new_path, ep_url):
    """Delete the previous, worse-language copies of an episode.

    Only ever called with paths auto-sync recorded on the queue item when it
    planned a language upgrade (see autosync_worker), and only after the new
    file exists — losing the old copy to a download that then failed is the one
    outcome this must never produce. `new_path` is excluded by resolved path so
    a mis-planned upgrade cannot delete what was just written.
    """
    if not old_paths:
        return
    try:
        new_resolved = Path(new_path).resolve()
    except (OSError, TypeError):
        logger.warning(
            "[LangGroup] Not deleting replaced files for %s: new file %r unusable",
            ep_url, new_path,
        )
        return
    if not new_resolved.is_file():
        logger.warning(
            "[LangGroup] Not deleting replaced files for %s: %s does not exist",
            ep_url, new_resolved,
        )
        return
    for raw in old_paths:
        try:
            old = Path(raw).resolve()
            if old == new_resolved or not old.is_file():
                continue
            old.unlink()
            logger.info("[LangGroup] Replaced by better language, deleted: %s", old)
        except OSError as exc:
            # A locked or already-removed file must not fail the download that
            # has otherwise succeeded.
            logger.warning("[LangGroup] Could not delete replaced file %s: %s", raw, exc)


def _build_attempt_plan(primary_provider, max_retries):
    """Ordered [(provider, attempt_no, attempts_for_this_provider), ...].

    The provider the user picked gets the full *max_retries* budget; every
    other working provider (in the order configured in the settings) then gets
    one shot each, so a dead hoster costs one extra try rather than failing the
    whole episode. Direct-link jobs ("Direct") have no hoster concept and just
    keep the plain retry loop.
    """
    if primary_provider == "Direct":
        return [("Direct", i, max_retries) for i in range(1, max_retries + 1)]

    chain = get_provider_fallback_chain(primary_provider)
    plan = [(chain[0], i, max_retries) for i in range(1, max_retries + 1)]
    for fallback in chain[1:]:
        plan.append((fallback, 1, 1))
    return plan


# Queue worker state
_queue_worker_started = False
_queue_lock = threading.Lock()
# Guards duplicate-check + add_to_queue so two near-simultaneous requests can't
# both pass the "already queued?" check and double-queue the same episodes.
# Used by: routes/queue.py's add endpoint and autosync_worker.py's queuing step.
_dl_lock = threading.Lock()


_EP_URL_SEASON_EPISODE_RE = re.compile(r"staffel-(\d+)/episode-(\d+)", re.IGNORECASE)
_EP_URL_EPISODE_QS_RE = re.compile(r"[?&]episode=(\d+)", re.IGNORECASE)


def _parse_season_episode_from_url(ep_url: str):
    """Best-effort (season, episode) extraction from a provider episode URL,
    for the downloads.titles/downloads.errors telemetry events below --
    there's no already-parsed season/episode int available at this point in
    the queue worker (unlike autosync_worker, which has the season/episode
    objects handy). Covers the aniworld.to/serienstream.to
    ".../staffel-N/episode-M" shape and megakino.to's "...?episode=N"
    synthetic URL; returns (None, None) for anything else (e.g. FilmPalast/
    Direct Link movie URLs, which have no season/episode concept)."""
    m = _EP_URL_SEASON_EPISODE_RE.search(ep_url or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _EP_URL_EPISODE_QS_RE.search(ep_url or "")
    if m:
        return None, int(m.group(1))
    return None, None


def _is_job_adaptive_paused(job) -> bool:
    """Return True if the job is currently in Adaptive Auto-Sync pause mode:
    enabled, not currently retrying after an error, and no new episode found
    for longer than the configured threshold. Mirrors the worker logic in
    `_autosync_worker` so the UI can show a matching status pill.

    Used by: routes/autosync.py to annotate each job in the job list with an
    "adaptive_paused" flag for the frontend.
    """
    import os
    from datetime import datetime, timedelta

    from .runtime_state import SYNC_ADAPTIVE_PAUSE_MAP

    if not job.get("enabled"):
        return False
    if (get_setting("sync_adaptive_enabled")
            or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_ENABLED", "0")) != "1":
        return False
    # Adaptive pause only applies in interval mode (weekly runs on fixed slots).
    if (os.environ.get("MEDIAFORGE_SYNC_MODE", "interval") or "interval").lower() == "weekly":
        return False
    # Jobs in the error-retry window are handled by that logic, not adaptive pause.
    try:
        max_retries = int(get_setting("sync_error_retries")
                          or os.environ.get("MEDIAFORGE_SYNC_ERROR_RETRIES", "0"))
    except (ValueError, TypeError):
        max_retries = 0
    retry_count = job.get("retry_count", 0) or 0
    if 0 < retry_count <= max_retries:
        return False
    last_new = job.get("last_new_found")
    if not last_new:
        return False
    try:
        ref_dt = datetime.strptime(last_new, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    pause_key = (get_setting("sync_adaptive_pause_after")
                 or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_PAUSE_AFTER", "4w"))
    pause_seconds = SYNC_ADAPTIVE_PAUSE_MAP.get(pause_key, 4 * 7 * 86400)
    return datetime.utcnow() >= ref_dt + timedelta(seconds=pause_seconds)


_last_disk_notif_time: "float" = 0.0  # debounce: max once per hour per run


def _check_disk_space_and_notify(username: str | None = None, check_path: str | None = None) -> bool:
    """Check free disk space against the configured minimum.

    Returns True when space is OK, False when below threshold.
    Sends a notification (at most once per hour per process lifetime).
    check_path: path to check (custom download path or default).

    Used by: `_queue_worker`, right before starting each queued item.
    """
    import shutil
    import time as _time
    import os

    global _last_disk_notif_time

    raw_gb = None
    try:
        from .db import get_setting as _gs
        raw_gb = _gs("notif_disk_space_min_gb")
    except Exception as e:
        logger.debug("[DiskCheck] Could not read disk space setting: %s", e)
    try:
        min_gb = float(raw_gb) if raw_gb else 5.0
    except (TypeError, ValueError):
        min_gb = 5.0

    if min_gb <= 0:
        return True  # feature disabled

    try:
        dl_path = check_path or os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "") or str(Path.home() / "Downloads")
        usage = shutil.disk_usage(dl_path)
        free_gb = usage.free / (1024 ** 3)
    except Exception:
        return True  # can't determine → don't block

    if free_gb >= min_gb:
        return True

    # Only notify once per hour
    now = _time.time()
    if now - _last_disk_notif_time < 3600:
        return False
    _last_disk_notif_time = now

    try:
        from .notifications import notify_all
        notify_all(
            title="⚠️ Speicherplatz niedrig",
            body=f"Nur noch {free_gb:.1f} GB frei (Limit: {min_gb:.0f} GB). Download wird trotzdem gestartet.",
            event="on_disk_space_low",
            username=username,
        )
    except Exception as e:
        logger.warning("[DiskCheck] Notification failed: %s", e)
    return False


def _normalize_media_url(raw: str) -> str:
    """Ensure a media-server URL has an http(s):// scheme.

    Used by: routes/settings.py and mediascan.py, when reading the configured
    Jellyfin/Plex URLs before validating or calling them.
    """
    raw = (raw or "").strip().rstrip("/")
    if raw and not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw


def _validate_server_url(url: str) -> None:
    """Validate a user-supplied server URL against SSRF risks.
    Raises ValueError with a user-friendly message on violation.
    Private/local IPs are allowed (legitimate for home-server setups);
    cloud metadata endpoints and unroutable addresses are blocked.

    Used by: routes/settings.py (Overseerr URL) and mediascan.py (Jellyfin/Plex
    URLs), before persisting or calling a user-supplied server address.
    """
    import ipaddress
    import socket as _socket
    from urllib.parse import urlparse

    url = (url or "").strip()
    if not url:
        return

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL muss mit http:// oder https:// beginnen.")

    hostname = (parsed.hostname or "").strip()
    if not hostname:
        raise ValueError("URL muss einen Hostnamen enthalten.")

    # Explicitly blocked hostnames (cloud metadata endpoints)
    _BLOCKED_HOSTS = {
        "169.254.169.254",           # AWS / Azure / GCP instance metadata
        "metadata.google.internal",  # GCP metadata
        "metadata.internal",
    }
    if hostname.lower() in _BLOCKED_HOSTS:
        raise ValueError(f"Host '{hostname}' ist aus Sicherheitsgründen nicht erlaubt.")

    # Blocked IP networks — cloud metadata and unroutable only
    _BLOCKED_NETS = [
        ipaddress.ip_network("169.254.0.0/16"),  # link-local (cloud metadata)
        ipaddress.ip_network("0.0.0.0/8"),        # "this" network
        ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
    ]

    def _check_addr(addr_str):
        try:
            addr = ipaddress.ip_address(addr_str)
            for net in _BLOCKED_NETS:
                if addr in net:
                    raise ValueError(
                        f"Adresse '{addr_str}' ist aus Sicherheitsgründen nicht erlaubt."
                    )
        except ValueError as exc:
            if "nicht erlaubt" in str(exc):
                raise
            # Not a valid IP literal — ignore

    # Check if hostname is a bare IP
    _check_addr(hostname)

    # Also resolve hostname and check the resolved IP
    try:
        resolved = _socket.gethostbyname(hostname)
        _check_addr(resolved)
    except (_socket.gaierror, OSError):
        pass  # Cannot resolve at config time — allow; validated at connection time


def _is_filmpalast_url(url: str) -> bool:
    """True for a Filmpalast stream URL (single-file movie, no episode list).

    Used by: routes/search.py and routes/browse.py to route movie-shaped URLs
    differently from series URLs, and by `_queue_worker` to pick the
    movie-vs-episode notification wording.
    """
    return "filmpalast.to/stream/" in url


def _is_megakino_url(url: str) -> bool:
    """True for a MegaKino /watch/ URL (movie or series, disambiguated via `_megakino_watch`)."""
    return "megakino" in (url or "") and "/watch/" in (url or "")


def _megakino_watch(url: str):
    """Fetch the /data/watch payload for a MegaKino /watch URL (movie or series).

    Used by: routes/search.py and routes/browse.py, to fetch metadata needed
    before deciding whether a MegaKino URL is a movie or a series
    (see `_megakino_is_series`).
    """
    from ..models.megakino_to import scraper as _mk
    return _mk.fetch_watch(url)


def _megakino_is_series(watch_data) -> bool:
    """Whether a `_megakino_watch` payload describes a series (vs. a movie)."""
    return str((watch_data or {}).get("tv")) == "1"


def _is_hanime_url(url: str) -> bool:
    """True for a hanime.tv video URL."""
    return "hanime.tv/videos/hentai/" in (url or "")


def _hanime_enabled() -> bool:
    """Whether the (opt-in, age-gated) hanime.tv source is enabled in settings.

    Used by: routes/search.py, routes/browse.py, and routes/integrations.py to
    gate hanime results/search behind the setting.
    """
    return get_setting("source_enabled_hanime", "0") == "1"


def _parse_season_episode(url):
    """Extract (season, episode) ints from an aniworld/s.to episode URL, else (None, None).

    Used by: download_history.py when recording a completed download.
    """
    if not url:
        return None, None
    s = re.search(r"staffel-(\d+)", url)
    e = re.search(r"episode-(\d+)", url)
    return (int(s.group(1)) if s else None, int(e.group(1)) if e else None)


def _is_within_download_window() -> bool:
    """Return True if downloads are allowed to start right now.

    When the download time window is enabled, new downloads are only started
    between the configured start and end time (local wall-clock). Supports
    overnight windows (e.g. 22:00 → 06:00). Already-running downloads are not
    affected — this only gates *starting* new ones.
    """
    import os
    enabled = (get_setting("download_window_enabled")
               or os.environ.get("MEDIAFORGE_DOWNLOAD_WINDOW_ENABLED", "0")) == "1"
    if not enabled:
        return True
    start = (get_setting("download_window_start")
             or os.environ.get("MEDIAFORGE_DOWNLOAD_WINDOW_START", "22:00"))
    end = (get_setting("download_window_end")
           or os.environ.get("MEDIAFORGE_DOWNLOAD_WINDOW_END", "06:00"))
    try:
        sh, sm = (int(x) for x in str(start).split(":"))
        eh, em = (int(x) for x in str(end).split(":"))
    except (ValueError, AttributeError):
        return True
    from datetime import datetime
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    s = sh * 60 + sm
    e = eh * 60 + em
    if s == e:
        return True  # zero-length window → treat as no restriction
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e  # overnight window wrapping past midnight


def _queue_worker():
    """Single global worker loop that processes one queued download at a time.

    Runs forever in a daemon thread. Each iteration: claims the next queued
    item (`claim_next_queued`, serialized by `_queue_lock`), resolves its
    download path (custom path / language-separated subfolder / default),
    then downloads each episode with up to `MAX_EP_RETRIES` attempts. Each
    attempt runs in its own thread so a watchdog can enforce a hard timeout
    (`_HANG_TIMEOUT`) and a no-progress stall timeout (`_STALL_TIMEOUT`)
    without blocking the whole worker on a hung yt-dlp process. Registers a
    `threading.Event` per item in `_active_cancel_events` so
    routes/queue.py's cancel endpoint can interrupt an in-flight download.
    Records per-episode results to the download history, updates queue
    progress/status, and fires notifications + media-server refresh on
    completion.

    Used by: started as a daemon thread by `_ensure_queue_worker()`.
    """
    while True:
        item = None
        try:
            # Hold new downloads outside the configured time window. Checked
            # before claiming so items aren't marked running while held.
            if not _is_within_download_window():
                time.sleep(30)
                continue

            _final_status_set = False
            with _queue_lock:
                item = claim_next_queued()

            if not item:
                time.sleep(3)
                continue

            # Don't start a new item while paused
            while is_queue_paused():
                time.sleep(2)

            # Warn if disk space is below configured threshold (non-blocking)
            # Resolve the actual download path for this item (custom path or default)
            _disk_check_path = None
            try:
                _cp_id = item.get("custom_path_id")
                if _cp_id:
                    _cp = get_custom_path_by_id(_cp_id)
                    if not _cp:
                        raise RuntimeError(
                            f"Der ausgewählte Custom Path (ID #{_cp_id}) existiert nicht mehr in der Datenbank oder wurde gelöscht."
                        )
                    _disk_check_path = str(Path(_cp["path"]).expanduser())
            except RuntimeError:
                raise
            except Exception as e:
                logger.debug("[Queue] Could not resolve custom path for disk check: %s", e)
            _check_disk_space_and_notify(username=item.get("username"), check_path=_disk_check_path)

            episodes = json.loads(item["episodes"])
            # Carry over any pre-existing errors (e.g. other failed episodes that
            # were kept when the user retried just one specific episode).
            # For brand-new jobs the DB default is '[]', so this is always safe.
            errors = json.loads(item.get("errors") or "[]")

            # Language separation: compute subfolder path if enabled
            import os

            lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
            if item.get("source") == "sync:all_langs":
                lang_sep = True
            selected_path = None

            from pathlib import Path

            # Determine base path: custom path or default
            custom_path_id = item.get("custom_path_id")
            if custom_path_id:
                cp = get_custom_path_by_id(custom_path_id)
                if not cp:
                    raise RuntimeError(
                        f"Der ausgewählte Custom Path (ID #{custom_path_id}) existiert nicht mehr in der Datenbank oder wurde gelöscht."
                    )
                base = Path(cp["path"]).expanduser()
                if not base.is_absolute():
                    base = Path.home() / base
                if not base.is_dir():
                    raise RuntimeError(
                        f"Der ausgewählte Custom Path '{base}' ist nicht erreichbar oder kein Verzeichnis."
                    )
            else:
                base = None

            if base is None:
                raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
                if raw:
                    base = Path(raw).expanduser()
                    if not base.is_absolute():
                        base = Path.home() / base
                else:
                    base = Path.home() / "Downloads"

            # Language fallback group (see web/language_groups.py): the item
            # carries a "group:<id>" reference instead of a language label, and
            # the actual language is picked per episode below — a chain only
            # means anything if each episode may resolve differently. Nothing
            # past this point may see the reference itself: it is neither a
            # legal folder name nor a language any episode model knows.
            lang_chain = []
            if is_group_ref(item["language"]):
                if not lang_sep:
                    raise RuntimeError(
                        "Sprachgruppen benötigen die Einstellung 'Sprachen in Ordner "
                        "trennen'. Bitte wieder aktivieren oder den Download mit einer "
                        "einzelnen Sprache neu anlegen."
                    )
                lang_chain = resolve_chain(item["language"])
                if not lang_chain:
                    raise RuntimeError(
                        f"Die Sprachgruppe '{item['language']}' existiert nicht mehr. "
                        "Bitte den Download mit einer Sprache oder Gruppe neu anlegen."
                    )

            def _path_for_language(language):
                """Target folder for one episode's resolved language."""
                if lang_sep:
                    from .lang_folders import lang_folder_for

                    return str(base / lang_folder_for(language))
                if custom_path_id:
                    return str(base)
                return None

            if not lang_chain:
                selected_path = _path_for_language(item["language"])

            # {episode_url: [old files]} for language upgrades planned by
            # auto-sync — deleted per episode once the new file is on disk.
            try:
                _replace_paths = json.loads(item.get("replace_paths") or "{}")
            except (TypeError, ValueError):
                _replace_paths = {}

            MAX_EP_RETRIES = 3

            # Create a cancel event for this item so api_queue_cancel can
            # kill the active subprocess immediately.
            _item_cancel = threading.Event()
            with _active_cancel_events_lock:
                _active_cancel_events[item["id"]] = _item_cancel

            # Friendly labels for language-not-available errors
            _LANG_LABELS = {
                ("GERMAN",   "NONE"):    "Deutsch",
                ("ENGLISH",  "NONE"):    "Englisch",
                ("JAPANESE", "GERMAN"):  "Japanisch mit deutschen Untertiteln",
                ("JAPANESE", "ENGLISH"): "Japanisch mit englischen Untertiteln",
                ("GERMAN",   "ENGLISH"): "Deutsch mit englischen Untertiteln",
                ("ENGLISH",  "GERMAN"):  "Englisch mit deutschen Untertiteln",
            }

            def _lang_unavailable_msg(exc):
                """Return a human-readable message if exc is a language-not-found error, else None."""
                msg = str(exc)
                if "No provider data found for language" not in msg:
                    return None
                if lang_chain:
                    # The episode was already probed against the whole chain, so
                    # reaching this point means none of its languages exists —
                    # naming only the one that was attempted would mislead.
                    return "Nicht verfügbar in: " + ", ".join(lang_chain)
                audio_m = re.search(r"Audio\.(\w+)", msg)
                subs_m  = re.search(r"Subtitles\.(\w+)", msg)
                audio = audio_m.group(1).upper() if audio_m else ""
                subs  = subs_m.group(1).upper()  if subs_m  else "NONE"
                label = _LANG_LABELS.get((audio, subs), f"{audio}" + (f" / UT: {subs}" if subs != "NONE" else ""))
                return f"Nicht verfügbar in: {label}"

            total_bytes_before = 0
            _upscale_after_paths = []  # collect for batch after_download upscaling
            download_start_time = time.time()

            downloaded_count = 0
            for i, ep_url in enumerate(episodes):
                update_queue_progress(item["id"], i, ep_url)

                last_error = None
                _episode_skipped = False
                _episode_cancelled = False
                # Language for THIS episode: the item's own language, or — for a
                # fallback group — the first language of the chain the episode is
                # offered in. The target folder follows the resolved language, so
                # a series that falls back mid-season still sorts correctly.
                _ep_language = item["language"]
                if lang_chain:
                    _ep_language = _resolve_episode_language(ep_url, lang_chain)
                    selected_path = _path_for_language(_ep_language)
                # Per-episode tracking for the download history
                _ep_start_time = time.time()
                _ep_path = None
                _ep_size_bytes = 0

                # ── Provider fallback chain ──────────────────────────────────
                # The hoster the user picked is tried MAX_EP_RETRIES times; if
                # it still fails (dead embed, extractor error, hoster simply not
                # offered for this episode), every other working hoster is tried
                # once, in the order configured in the settings. See
                # runtime_state.get_provider_fallback_chain(). Direct-link jobs
                # have no hoster concept and keep the plain retry loop.
                _attempt_plan = _build_attempt_plan(item.get("provider"), MAX_EP_RETRIES)
                _current_provider = None
                _dead_providers = set()
                # {hoster: last error string} across the whole fallback chain,
                # used to enrich the failure telemetry so a debug report shows
                # WHY each provider failed, not just the surfaced one.
                _provider_errors = {}
                # {resolved provider_url: first hoster that tried it}. Lets the
                # fallback skip a different hoster that resolves to the SAME
                # embed we already tried (mirrored VOE/Vidara/Vidavaca labels
                # all pointing at one voe.sx URL) instead of re-downloading the
                # identical stream. Same-hoster retries are unaffected.
                _seen_stream_urls = {}

                for _plan_idx, (_hoster, attempt, _attempts_for_hoster) in enumerate(_attempt_plan):
                    _last_attempt = _plan_idx == len(_attempt_plan) - 1
                    if _hoster in _dead_providers:
                        continue
                    if _hoster != _current_provider and _current_provider is not None:
                        logger.debug(
                            f"[Provider-Fallback] {ep_url}: '{_current_provider}' failed — "
                            f"switching to '{_hoster}'"
                        )
                    _current_provider = _hoster
                    try:
                        if item.get("provider") == "Direct":
                            # Direct Link job (see routes/direct_link.py): a raw
                            # yt-dlp URL with no series/season/provider/language
                            # structure, so resolve_provider() is bypassed entirely.
                            from ..models.direct_link.episode import DirectLinkEpisode
                            ep_kwargs = {
                                "url": ep_url,
                                "title": item["title"],
                                "format_id": item.get("format_id"),
                                "source_provider": item.get("source_provider"),
                            }
                            if selected_path:
                                ep_kwargs["selected_path"] = selected_path
                            episode = DirectLinkEpisode(**ep_kwargs)
                        else:
                            prov = resolve_provider(ep_url)
                            ep_kwargs = {
                                "url": ep_url,
                                "selected_language": _ep_language,
                                "selected_provider": _hoster,
                            }
                            if selected_path:
                                ep_kwargs["selected_path"] = selected_path
                            episode = prov.episode_cls(**ep_kwargs)
                            # Deduplicate mirrored labels: if this hoster
                            # resolves to an embed a *different* hoster already
                            # tried, skip straight to the next genuinely
                            # different stream instead of re-downloading the same
                            # URL. provider_url is cached, so the download below
                            # reuses this result (no second resolve). queue_id is
                            # set so an interactive resolve (e.g. the s.to modal)
                            # still streams to the web UI as during a download.
                            from ..playwright import captcha as _dedup_captcha
                            _dedup_captcha._local.queue_id = item["id"]
                            try:
                                _resolved_url = episode.provider_url
                            except Exception:
                                _resolved_url = None  # let the download surface the real error
                            finally:
                                _dedup_captcha._local.queue_id = None
                            if _resolved_url:
                                _owner = _seen_stream_urls.get(_resolved_url)
                                if _owner is not None and _owner != _hoster:
                                    logger.info(
                                        f"Episode {ep_url}: provider '{_hoster}' resolves to an "
                                        f"already-tried stream ({_resolved_url}) — skipping to the "
                                        f"next hoster."
                                    )
                                    _dead_providers.add(_hoster)
                                    continue
                                _seen_stream_urls.setdefault(_resolved_url, _hoster)
                        from ..playwright import captcha as _captcha_mod
                        from ..models.common.common import get_ffmpeg_progress
                        _queue_id = item["id"]
                        _upscale = bool(item.get("upscale", 0))

                        # ── Watchdog: run download in a thread so a hung yt-dlp
                        # cannot freeze the entire queue worker forever. ──────
                        _HANG_TIMEOUT   = int(get_setting("watchdog_hang_timeout") or os.environ.get("MEDIAFORGE_HANG_TIMEOUT", "1800"))   # 30 min default
                        _STALL_TIMEOUT  = int(get_setting("watchdog_stall_timeout") or os.environ.get("MEDIAFORGE_STALL_TIMEOUT", "3600"))  # 60 min no progress (must comfortably exceed yt-dlp's reconnect_delay_max=60s so a normal reconnect isn't mistaken for a stall)
                        _dl_exc = [None]
                        _dl_res = [None]
                        _dl_done = threading.Event()
                        _attempt_cancel = threading.Event()

                        def _dl_thread():
                            # threading.local() is per-thread — set queue_id here, not in the parent thread
                            _captcha_mod._local.queue_id = _queue_id
                            _captcha_mod._local.upscale = _upscale
                            try:
                                _dl_res[0] = episode.download(cancel_event=_attempt_cancel)
                            except Exception as _e:
                                _dl_exc[0] = _e
                            finally:
                                _captcha_mod._local.queue_id = None
                                _captcha_mod._local.upscale = False
                                _dl_done.set()

                        _t = threading.Thread(target=_dl_thread, daemon=True)
                        _t.start()

                        # Poll for stall / hard timeout
                        _last_pct    = -1.0
                        _last_change = time.monotonic()
                        _start_watch = time.monotonic()
                        _timed_out   = False
                        while not _dl_done.wait(timeout=5):
                            if _item_cancel.is_set():
                                _attempt_cancel.set()
                                break
                            _now = time.monotonic()
                            if _now - _start_watch > _HANG_TIMEOUT:
                                logger.error(
                                    f"[watchdog] Download hard-timeout ({_HANG_TIMEOUT}s) for {ep_url} — aborting"
                                )
                                _attempt_cancel.set()
                                _timed_out = True
                                break
                            _prog = get_ffmpeg_progress()
                            _pct  = _prog.get("percent", 0.0)
                            if _pct != _last_pct:
                                _last_pct    = _pct
                                _last_change = _now
                            elif _now - _last_change > _STALL_TIMEOUT:
                                logger.error(
                                    f"[watchdog] No progress for {_STALL_TIMEOUT}s on {ep_url} — aborting"
                                )
                                _attempt_cancel.set()
                                _timed_out = True
                                break

                        if _timed_out:
                            _dl_done.wait(timeout=10)  # give thread a moment to notice cancel
                            raise RuntimeError(
                                f"Download aborted by watchdog (stalled/hung after {_STALL_TIMEOUT}s without progress)"
                            )

                        if _dl_exc[0] is not None:
                            raise _dl_exc[0]
                        # ── end watchdog ─────────────────────────────────────
                        last_error = None

                        # Track size
                        try:
                            if hasattr(episode, "_episode_path") and episode._episode_path.exists():
                                _ep_size_bytes = os.path.getsize(episode._episode_path)
                                total_bytes_before += _ep_size_bytes
                                _ep_path = str(episode._episode_path)
                        except Exception:
                            pass

                        # Collect path for batch after_download upscaling
                        try:
                            if hasattr(episode, "_episode_path") and episode._episode_path.exists():
                                _upscale_after_paths.append(str(episode._episode_path))
                        except Exception:
                            pass

                        # Trigger after_download encoding for THIS episode right away
                        # (not batched at the end of the whole queue item) so encoding
                        # starts as soon as this episode finishes downloading while the
                        # next episode keeps downloading in parallel. The encoding
                        # worker only processes one item at a time, so if it's already
                        # busy this episode's entry simply waits its turn in the queue.
                        try:
                            if hasattr(episode, "_episode_path") and episode._episode_path.exists():
                                _trigger_after_download_encode([str(episode._episode_path)], item.get("title", ""))
                        except Exception as _ee:
                            logger.warning(f"[Encoding] Trigger Fehler: {_ee}")

                        break  # success — stop retrying
                    except Exception as e:
                        from ..playwright import captcha as _captcha_mod
                        _captcha_mod._local.queue_id = None
                        if is_queue_cancelled(item["id"]) or "Download cancelled" in str(e):
                            _episode_cancelled = True
                            last_error = None
                            break
                        friendly = _lang_unavailable_msg(e)
                        if friendly is not None:
                            # Language simply doesn't exist — no point retrying
                            last_error = Exception(friendly)
                            logger.warning(f"Language unavailable for {ep_url}: {e}")
                            break
                        # Record this hoster's error so the per-provider failure
                        # reasons survive for telemetry / debugging, even when the
                        # episode error ultimately surfaced is a later hoster's.
                        _provider_errors[_hoster] = str(e)

                        # Pick the most useful error to surface: a real failure
                        # from an earlier provider (typically the hoster the user
                        # picked, which is tried first) must not be overwritten by
                        # a later hoster that simply doesn't offer this episode at
                        # all ("not available" skip). A real error always wins over
                        # such a skip; otherwise keep the newest error.
                        _this_unavailable = _is_provider_unavailable_error(e)
                        _have_real_error = (
                            last_error is not None
                            and not _is_provider_unavailable_error(last_error)
                        )
                        if not (_this_unavailable and _have_real_error):
                            last_error = e

                        if _this_unavailable:
                            # This hoster isn't offered for this episode at all —
                            # retrying it is pointless, so drop it and let the
                            # chain move straight on to the next hoster. Debug:
                            # a site not offering every hoster for every episode
                            # is normal, and at warning level this alone would
                            # print several lines per episode.
                            _dead_providers.add(_hoster)
                            logger.debug(
                                f"Episode {ep_url}: provider '{_hoster}' not available "
                                f"for this episode: {e}"
                            )
                        elif attempt < _attempts_for_hoster:
                            delay = 2
                            logger.warning(
                                f"Episode {ep_url} failed with provider '{_hoster}' "
                                f"(attempt {attempt}/{_attempts_for_hoster}), retrying in {delay}s: {e}"
                            )
                            time.sleep(delay)
                        elif not _last_attempt:
                            # Real failure on this hoster with more hosters left.
                            # Logged at INFO (not debug) on purpose: THIS is the
                            # actual reason a download fell through to a fallback
                            # hoster. The episode error surfaced later may read as
                            # a trivial "not available" from the last hoster in the
                            # chain, so this line is where the real cause stays
                            # visible in the log.
                            logger.info(
                                f"Episode {ep_url} failed with provider '{_hoster}' after "
                                f"{_attempts_for_hoster} attempt(s), trying the next provider: {e}"
                            )
                        else:
                            _breakdown = "; ".join(
                                f"{p}: {msg}" for p, msg in _provider_errors.items()
                            )
                            logger.error(
                                f"Episode {ep_url} failed with every provider "
                                f"({', '.join(dict.fromkeys(p for p, _a, _b in _attempt_plan))}) "
                                f"— per-provider errors: {_breakdown}"
                            )
                    # Check skip flag after each attempt (success or fail)
                    if consume_episode_skip(item["id"]):
                        logger.info(f"Episode {ep_url} skipped by user request")
                        last_error = None  # treat as skipped, not failed
                        _episode_skipped = True
                        break

                from ..models.common.common import print_episode_summary
                _tel_season, _tel_episode = _parse_season_episode_from_url(ep_url)
                # Best-effort movie/series classification for telemetry: Direct
                # Link jobs are always a single file, and a URL with neither a
                # parsed season/episode nor a "staffel-" segment is treated as
                # a movie (matches FilmPalast/megakino movie URLs); everything
                # else is a series episode.
                if item.get("provider") == "Direct":
                    _tel_media_type = "movie"
                elif _tel_season is None and _tel_episode is None and "staffel" not in (ep_url or ""):
                    _tel_media_type = "movie"
                else:
                    _tel_media_type = "series"
                if last_error is not None:
                    errors.append({"url": ep_url, "error": str(last_error)})
                    update_queue_errors(item["id"], json.dumps(errors))
                    print_episode_summary(item["title"], ep_url, success=False)
                    _record_download_history(item, ep_url, _ep_start_time, None, 0, "failed", error=last_error, language=_ep_language)
                    telemetry_client.submit_all(telemetry_events.build_download_event(
                        provider=item.get("provider"), media_type=_tel_media_type, title=item.get("title"),
                        season=_tel_season, episode=_tel_episode, status="failed",
                        error_message=str(last_error), provider_errors=_provider_errors,
                    ))
                elif _episode_cancelled:
                    print_episode_summary(item["title"], ep_url, success="Abgebrochen")
                    _record_download_history(item, ep_url, _ep_start_time, None, 0, "cancelled", language=_ep_language)
                elif not _episode_skipped:
                    if _dl_res[0] is not False:
                        downloaded_count += 1
                        print_episode_summary(item["title"], ep_url, success=True)
                        _record_download_history(item, ep_url, _ep_start_time, _ep_path, _ep_size_bytes, "completed", language=_ep_language)

                        # Language upgrade: the better-language file is on disk
                        # now, so the copies it supersedes can go.
                        _delete_replaced_files(_replace_paths.get(ep_url), _ep_path, ep_url)

                        # ------ START JELLYFIN NFO HOOK ------
                        try:
                            from .nfo_provider import generate_nfo_for_download
                            from .tmdb_cache import _tmdb_lookup_cached, _tmdb_fetch_season_and_episode
                            
                            cineinfo_api_key = get_setting("cineinfo_tmdb_api_key", "")
                            cineinfo_country = get_setting("cineinfo_country", "DE")
                            if cineinfo_api_key and _ep_path:
                                tmdb_data = _tmdb_lookup_cached(item["title"], None, cineinfo_api_key, cineinfo_country)
                                if tmdb_data and tmdb_data.get("found"):
                                    season_data = None
                                    episode_data = None
                                    if _tel_media_type == "series" and _tel_season is not None and _tel_episode is not None:
                                        se_data = _tmdb_fetch_season_and_episode(tmdb_data["tmdb_id"], _tel_season, _tel_episode, cineinfo_api_key)
                                        season_data = se_data.get("season")
                                        episode_data = se_data.get("episode")
                                    generate_nfo_for_download(_ep_path, tmdb_data, _tel_media_type, season_data, episode_data)
                        except Exception as nfo_exc:
                            logger.error(f"[JellyfinNFO] NFO Generation failed: {nfo_exc}", exc_info=True)
                        # ------ END JELLYFIN NFO HOOK ------

                        telemetry_client.submit_all(telemetry_events.build_download_event(
                            provider=item.get("provider"), media_type=_tel_media_type, title=item.get("title"),
                            season=_tel_season, episode=_tel_episode, status="completed",
                        ))
                    else:
                        print_episode_summary(item["title"], ep_url, success="Bereits vorhanden")
                        _record_download_history(item, ep_url, _ep_start_time, _ep_path, 0, "skipped", error="Bereits vorhanden", language=_ep_language)
                else:
                    print_episode_summary(item["title"], ep_url, success=True)
                    _record_download_history(item, ep_url, _ep_start_time, None, 0, "skipped", error="Übersprungen", language=_ep_language)

                # Mark episode as done: increment counter and clear current URL
                update_queue_progress(item["id"], i + 1, "")

                # Check for cancellation after each episode
                if is_queue_cancelled(item["id"]):
                    logger.info(f"Download cancelled for queue item {item['id']}")
                    # Record the remaining, not-yet-started episodes as cancelled so
                    # the whole aborted job is reflected in the download history.
                    for _rem_url in episodes[i + 1:]:
                        _record_download_history(item, _rem_url, time.time(), None, 0, "cancelled")
                    break

                # Pause: hold here until resumed (checks every 2s)
                while is_queue_paused():
                    time.sleep(2)

            # Batch-trigger after_download upscaling for all collected episode paths
            if _upscale_after_paths and not is_queue_cancelled(item["id"]):
                try:
                    _trigger_batch_after_download_upscale(_upscale_after_paths, item.get("title", ""), upscale=bool(item.get("upscale", 0)))
                except Exception as _ue:
                    logger.warning(f"[Upscale] Batch-Trigger Fehler: {_ue}")

            # Only set final status if not already cancelled
            if not is_queue_cancelled(item["id"]):
                update_queue_progress(item["id"], len(episodes), "")

                # Calculate speed and update stats
                download_end_time = time.time()
                duration = download_end_time - download_start_time
                if duration > 0 and total_bytes_before > 0:
                    total_size_mb = total_bytes_before / (1024 * 1024)
                    avg_speed = total_size_mb / duration
                    update_queue_stats(item["id"], round(avg_speed, 2), round(total_size_mb, 1))

                successful = len(episodes) - len(errors)
                if not errors:
                    status = "completed"
                elif successful > 0:
                    status = "partial"
                else:
                    status = "failed"
                set_queue_status(item["id"], status)
                _final_status_set = True

                # Send notifications (all services)
                from .notifications import notify_all
                # Direct Link jobs are always a single file, so they get the
                # same "Film" notification wording as a FilmPalast movie.
                _is_movie = _is_filmpalast_url(item.get("url", "")) or item.get("provider") == "Direct"
                if status == "completed":
                    _body = "✅ Film heruntergeladen" if _is_movie else f"✅ {len(episodes)} Episode(n) heruntergeladen"
                    _event = "on_completed"
                elif status == "partial":
                    _body = f"❌ Film-Download fehlgeschlagen ({len(errors)} Fehler)" if _is_movie else f"⚠️ {successful} von {len(episodes)} Episode(n) heruntergeladen, {len(errors)} Fehler"
                    _event = "on_partial"
                else:
                    _body = f"❌ Download fehlgeschlagen ({len(errors)} Fehler)"
                    _event = "on_errors"
                notify_all(
                    title=item.get("title", "Unbekannt"),
                    body=_body,
                    event=_event,
                    username=item.get("username"),
                    status=status,
                    episode_count=len(episodes),
                    errors=errors,
                    is_movie=_is_movie,
                )
                # Trigger Jellyfin/Plex library refresh on completed or partial downloads
                if status in ("completed", "partial"):
                    _trigger_mediaplayer_refresh(title=item.get("title"), selected_path=selected_path)
                    # MediaScan: schedule a delayed library re-fetch (2 min) so
                    # Plex/Jellyfin has time to ingest the new file first
                    _schedule_mediascan_delayed(delay=120.0)
            else:
                _final_status_set = True
                from .notifications import notify_all
                notify_all(
                    title=item.get("title", "Unbekannt"),
                    body="⏹️ Download abgebrochen",
                    event="on_cancelled",
                    username=item.get("username"),
                    status="cancelled",
                    episode_count=len(episodes),
                    errors=[],
                )

        except Exception as e:
            logger.error(f"Queue worker error: {e}", exc_info=True)
            if item is not None and not _final_status_set:
                try:
                    if not is_queue_cancelled(item["id"]):
                        try:
                            errors = json.loads(item.get("errors") or "[]")
                        except Exception:
                            errors = []
                        errors.append({"url": item.get("series_url", ""), "error": f"Internal worker error: {str(e)}"})
                        update_queue_errors(item["id"], json.dumps(errors))
                        set_queue_status(item["id"], "failed")

                        try:
                            from .notifications import notify_all
                            # Direct Link jobs are always a single file, so they get
                            # the same "Film" notification wording as a FilmPalast movie.
                            _is_movie = _is_filmpalast_url(item.get("url", "")) or item.get("provider") == "Direct"
                            notify_all(
                                title=item.get("title", "Unbekannt"),
                                body=f"❌ Download durch internen Fehler abgebrochen: {e}",
                                event="on_errors",
                                username=item.get("username"),
                                status="failed",
                                episode_count=0,
                                errors=[{"url": item.get("series_url", ""), "error": str(e)}],
                                is_movie=_is_movie,
                            )
                        except Exception as ne:
                            logger.error(f"Failed to send crash notification for item {item['id']}: {ne}", exc_info=True)
                except Exception as db_err:
                    logger.error(f"Failed to set status to failed for item {item['id']}: {db_err}", exc_info=True)
            time.sleep(3)
        finally:
            # Always deregister the cancel event for this item.
            if item is not None:
                with _active_cancel_events_lock:
                    _active_cancel_events.pop(item["id"], None)


def _ensure_queue_worker():
    """Start the queue worker thread once.

    Used by: app.py's create_app() (initial start) and routes/history.py
    (lazy-starts the worker on first history access if it isn't running yet).
    """
    global _queue_worker_started
    with _queue_lock:
        if _queue_worker_started:
            return
        _queue_worker_started = True

    # Crash recovery: reset any 'running' items back to 'queued'
    from .db import get_db

    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET status = 'queued' WHERE status = 'running'"
        )
        conn.execute("UPDATE download_queue SET captcha_url = NULL")
        conn.commit()
    finally:
        conn.close()

    thread = threading.Thread(target=_queue_worker, daemon=True)
    thread.start()
