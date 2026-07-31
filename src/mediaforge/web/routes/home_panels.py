"""The home page's button bar: /api/home-panels and /api/home-panel/<id>.

The registry itself is core (mediaforge/home_panels.py); this is where the
built-in panels live, because every one of them reads the database, the queue
or the session -- none of which core is allowed to import.

Two routes on purpose:

  * ``/api/home-panels`` is the bar. It answers once per page load with the
    buttons and their badge numbers, and nothing else -- a badge is a COUNT,
    so the bar stays cheap even with a dozen registered panels.
  * ``/api/home-panel/<id>`` is the body of the *one* panel that is open. It
    is the only thing that polls, and only while its tab is visible (see
    static/mf_poll.js). That is the whole reason the design is "one panel at a
    time" instead of a widget grid: six widgets means six pollers.

Access is decided here, in both routes. A panel marked ``admin_only`` is not
listed for a normal account *and* returns 403 when asked for directly --
leaving the button out of the template would put the data one fetch away for
anyone who opened the console once.

NO TRANSLATABLE STRING LIVES IN THIS FILE. babel.cfg deliberately extracts
Jinja templates only (a bare _() in Python source is never picked up -- read
the comment at the top of babel.cfg for why), so every built-in label is sent
as a KEY and resolved by static/home_panels.js against window.__HOME_I18N,
which index.html renders through Flask-Babel like the rest of the home page.

Module panels are the exception and send ready-made text: a module owns its
own catalogue, and this file cannot translate a string it has never seen. The
payload therefore carries both -- ``*_key`` for the built-ins, plain text for
modules -- and the client prefers the key when one is present.
"""

from ..db import get_custom_paths
from ..db import get_download_history
from ..db import get_encoding_badge_count
from ..db import get_queue
from ..db import get_queue_stats
from ..db import get_upscale_badge_count
from ..db import register_ui_pref_key
from ..request_context import get_current_user_info
from ..runtime_state import is_queue_paused
from ...home_panels import PANEL_ACTIONS
from ...home_panels import PANEL_MAX_ITEMS
from ...home_panels import iter_home_panels
from ...logger import get_logger
from flask import jsonify
import os
import shutil
import threading
import time

logger = get_logger(__name__)

# The panel a user last had open, so the home page comes back the way they
# left it. Registered through the module API rather than hardcoded in
# USER_UI_PREF_KEYS because that is exactly what the API is for -- and it
# keeps this feature out of db.py entirely.
_PANEL_PREF_KEY = "home_panel"

# Badges are recomputed at most this often per process. The bar is fetched on
# every home page visit, and with several accounts on one instance that is a
# handful of COUNT queries per second for numbers that change every few
# seconds at best.
_BADGE_TTL = 10.0
# How many rows the two "here is what happened" panels list. PANEL_MAX_ITEMS
# is the registry's hard cap (what a module may not exceed); this is the
# editorial limit for the built-in history/library lists, which are a glance
# at the newest entries and have an "open the full page" link right below.
_RECENT_MAX_ITEMS = 10
# Disk usage is the one built-in panel that touches the filesystem, and a
# spinning disk under load can make statvfs take real milliseconds.
_DISK_TTL = 30.0

_cache_lock = threading.Lock()
_badge_cache = {"at": 0.0, "values": {}}
_disk_cache = {"at": 0.0, "value": []}


# ── helpers ──────────────────────────────────────────────────────────────

def _is_admin() -> bool:
    """True when the current session is an admin (or auth is off entirely).

    Wrapped because a request that cannot tell is treated as "not admin",
    which is the safe direction.
    """
    try:
        _username, is_admin = get_current_user_info()
        return bool(is_admin)
    except Exception:
        return False


def _clean_text(value, limit=160) -> str:
    """One line of plain text, length-capped.

    Panels may come from modules, and the client renders these into the DOM
    (escaped, via mfEscape). The cap is not about safety, it is about a module
    being able to push the poster rows off the screen with one long string.
    """
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ")
    return text.strip()[:limit]


def _clean_key(value) -> str:
    """An i18n key the client looks up in window.__HOME_I18N."""
    key = _clean_text(value, 40)
    return key if all(c.isalnum() or c == "_" for c in key) else ""


def _clean_args(value) -> list:
    """Placeholder values for a keyed string. Text only, three at most --
    the client substitutes them into '{}' one by one."""
    if not isinstance(value, (list, tuple)):
        return []
    return [_clean_text(v, 60) for v in value[:3]]


def _clean_action(value) -> str:
    """A named client-side action, from a fixed list.

    The queue is a modal that lives in base.html, not a page: linking to
    /queue produced a 404, because no such route exists. Rather than let a
    payload name a JS function (a module could then name any of them), the
    client maps these ids itself -- see PANEL_ACTIONS.
    """
    action = _clean_text(value, 40)
    return action if action in PANEL_ACTIONS else ""


def _clean_href(value) -> str:
    """Site-relative paths only.

    ``//evil.example`` is a protocol-relative URL a browser follows off-site,
    so "starts with /" is not enough on its own.
    """
    href = _clean_text(value, 300)
    if not href.startswith("/") or href.startswith("//"):
        return ""
    return href


def _clean_tone(value) -> str:
    return value if value in ("ok", "warn", "err") else ""


def _clean_view(raw) -> dict:
    """Normalise whatever a panel returned into the shape the client expects.

    Everything is rebuilt field by field rather than passed through: a module
    panel is the one place in this feature where third-party data reaches the
    page, and "only the keys we know" is the cheapest way to keep it that way.
    """
    empty = {"stats": [], "items": [], "link": None, "empty": "", "empty_key": ""}
    if not isinstance(raw, dict):
        return empty
    stats = []
    for entry in (raw.get("stats") or [])[:6]:
        if not isinstance(entry, dict):
            continue
        stats.append({
            "label": _clean_text(entry.get("label"), 40),
            "label_key": _clean_key(entry.get("label_key")),
            "value": _clean_text(entry.get("value"), 24),
            "value_key": _clean_key(entry.get("value_key")),
            "tone": _clean_tone(entry.get("tone")),
        })
    items = []
    for entry in (raw.get("items") or [])[:PANEL_MAX_ITEMS]:
        if not isinstance(entry, dict):
            continue
        percent = entry.get("percent")
        try:
            percent = max(0, min(100, int(percent))) if percent is not None else None
        except (TypeError, ValueError):
            percent = None
        items.append({
            "title": _clean_text(entry.get("title")),
            "sub": _clean_text(entry.get("sub")),
            "sub_key": _clean_key(entry.get("sub_key")),
            "sub_args": _clean_args(entry.get("sub_args")),
            "percent": percent,
            "href": _clean_href(entry.get("href")),
            "action": _clean_action(entry.get("action")),
            "tone": _clean_tone(entry.get("tone")),
        })
    link = raw.get("link")
    if isinstance(link, dict) and (_clean_href(link.get("href"))
                                   or _clean_action(link.get("action"))):
        link = {"href": _clean_href(link.get("href")),
                "action": _clean_action(link.get("action")),
                "label": _clean_text(link.get("label"), 40),
                "label_key": _clean_key(link.get("label_key"))}
    else:
        link = None
    return {"stats": stats, "items": items, "link": link,
            "empty": _clean_text(raw.get("empty"), 120),
            "empty_key": _clean_key(raw.get("empty_key"))}


def _download_roots() -> list:
    """Every path downloads can land in: the configured root plus custom paths.

    Same resolution order as /api/downloaded-folders in browse.py -- if the
    two disagreed, the storage panel would report free space for a disk
    nothing is written to.
    """
    from pathlib import Path
    roots = []
    raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.home() / path
        roots.append(("Downloads", path))
    else:
        roots.append(("Downloads", Path.home() / "Downloads"))
    for entry in get_custom_paths() or []:
        path = Path(entry.get("path") or "").expanduser()
        if not str(entry.get("path") or ""):
            continue
        if not path.is_absolute():
            path = Path.home() / path
        roots.append((entry.get("name") or path.name, path))
    return roots


def _device_id(path):
    """``st_dev`` of a path, or ``None`` when it cannot be read.

    ``st_dev`` identifies the SUPERBLOCK, not the mount point, which is
    exactly the question this panel asks. In Docker that is what makes six
    bind mounts of one NAS export

        /mnt/nas/X/Anime:/app/downloads-main
        /mnt/nas/X/Filme:/app/downloads-movies
        ...

    answer with one id: a bind mount does not create a filesystem, it grafts
    the existing one into a second place, so every one of those paths reports
    the device of the filesystem holding /mnt/nas. Mount the shares
    SEPARATELY on the host (one NFS/CIFS mount per share) and they are
    genuinely different superblocks with their own free space -- different
    ids, and rightly so, even though one NAS is behind all of them.

    On Windows Python fills st_dev from the volume serial number, so the same
    reasoning holds there.
    """
    try:
        return os.stat(str(path)).st_dev
    except OSError:
        return None


def _disk_rows() -> list:
    """(label, used, total) per download root, cached -- see _DISK_TTL.

    Every configured root is accounted for. Roots that turn out to sit on the
    same storage are merged into ONE row that names all of them, rather than
    all but the first being dropped -- that silent drop was the bug: someone
    with six configured paths saw three bars and no way to tell which three.

    Two roots count as the same storage when EITHER holds:

      * they report the same ``st_dev`` -- one filesystem, however many mount
        points (the Docker bind-mount case above), or
      * they report byte-identical total AND free space. This second test is
        not redundant: ZFS datasets and btrfs subvolumes each get their own
        st_dev while sharing one pool's free space, so a NAS would otherwise
        draw six identical bars claiming to be six independent disks.

    Merging two genuinely separate disks needs them to agree to the byte on
    both numbers, and even then it costs nothing but a combined label -- no
    path can go missing, which is the property that actually matters here.
    """
    now = time.monotonic()
    with _cache_lock:
        if now - _disk_cache["at"] < _DISK_TTL:
            return list(_disk_cache["value"])
    rows = []
    by_dev = {}
    by_size = {}
    for label, path in _download_roots():
        try:
            usage = shutil.disk_usage(str(path))
        except OSError:
            continue          # path not mounted right now -- not an error here
        dev = _device_id(path)
        size_key = (usage.total, usage.free)
        existing = (by_dev.get(dev) if dev is not None else None) or by_size.get(size_key)
        if existing is not None:
            if label and label not in existing["labels"]:
                existing["labels"].append(label)
        else:
            existing = {"labels": [label] if label else [],
                        "used": usage.total - usage.free, "total": usage.total}
            rows.append(existing)
        if dev is not None:
            by_dev.setdefault(dev, existing)
        by_size.setdefault(size_key, existing)
    rows = [(" · ".join(r["labels"]) or "/", r["used"], r["total"]) for r in rows]
    with _cache_lock:
        _disk_cache["at"] = now
        _disk_cache["value"] = list(rows)
    return rows


def _human_size(num_bytes) -> str:
    """Locale-neutral on purpose: the unit is the same word everywhere and the
    number carries no translatable text, so this may be built here."""
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return ("%d %s" % (value, unit)) if unit in ("B", "KB") else ("%.1f %s" % (value, unit))
        value /= 1024
    return "0 B"


def _ago(stamp):
    """(key, args) for "3 min ago", or ("", []) when the stamp is unusable.

    A naive ISO string is read as UTC, not as local time: every timestamp
    sqlite writes here comes from ``datetime('now')``, which is UTC without a
    suffix. Reading those as local time is how "downloaded 2 hours ago" turns
    into "in 1 hour" east of Greenwich.
    """
    if not stamp:
        return "", []
    try:
        seconds = time.time() - float(stamp)
    except (TypeError, ValueError):
        try:
            from datetime import datetime, timezone
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = time.time() - parsed.timestamp()
        except Exception:
            return "", []
    if seconds < 0:
        return "", []
    minutes = int(seconds // 60)
    if minutes < 1:
        return "hp_just_now", []
    if minutes < 60:
        return "hp_min_ago", [str(minutes)]
    hours = minutes // 60
    if hours < 24:
        return "hp_h_ago", [str(hours)]
    return "hp_d_ago", [str(hours // 24)]


def _safe_count(fn) -> int:
    try:
        return int(fn() or 0)
    except Exception:
        return 0


# ── built-in panels ──────────────────────────────────────────────────────

def _panel_queue() -> dict:
    # visible_only, because the item list below comes from get_queue(), which
    # skips hidden rows. Counting differently from the list you are printing
    # next to it is how this panel said "3 failed" over an empty list.
    stats = get_queue_stats(visible_only=True)
    by_status = stats.get("by_status") or {}
    paused = is_queue_paused()
    items = []
    for entry in get_queue() or []:
        if entry.get("status") not in ("running", "queued"):
            continue
        total = entry.get("total_episodes") or 0
        current = entry.get("current_episode") or 0
        running = entry.get("status") == "running"
        items.append({
            "title": entry.get("title") or "",
            "sub_key": "hp_ep_of" if total else "",
            "sub_args": [str(current), str(total)] if total else [],
            "sub": entry.get("provider") or entry.get("source") or "" if not total else "",
            "percent": (round(current / total * 100) if total else None) if running else None,
            "action": "queue",
            "tone": "ok" if running else "",
        })
        if len(items) >= PANEL_MAX_ITEMS:
            break
    return {
        "stats": [
            {"label_key": "hp_running", "value": str(by_status.get("running", 0)),
             "tone": "ok" if by_status.get("running") else ""},
            {"label_key": "hp_waiting", "value": str(by_status.get("queued", 0))},
            {"label_key": "hp_failed", "value": str(by_status.get("failed", 0)),
             "tone": "err" if by_status.get("failed") else ""},
            {"label_key": "hp_paused", "value_key": "hp_yes" if paused else "hp_no",
             "tone": "warn" if paused else ""},
        ],
        "items": items,
        "link": {"action": "queue", "label_key": "hp_open_queue"},
        "empty_key": "hp_empty_queue",
    }


def _panel_activity() -> dict:
    entries, _total = get_download_history(username=None, limit=_RECENT_MAX_ITEMS)
    items = []
    for entry in entries or []:
        status = (entry.get("status") or "").lower()
        key, args = _ago(entry.get("finished_at") or entry.get("created_at"))
        items.append({
            "title": entry.get("title") or "",
            "sub_key": key,
            "sub_args": args,
            "href": "/history",
            "tone": "ok" if status == "completed" else ("err" if status == "failed" else ""),
        })
    return {
        "items": items,
        "link": {"href": "/history", "label_key": "hp_open_history"},
        "empty_key": "hp_empty_history",
    }


def _panel_library() -> dict:
    """Series, episodes, movies and total size -- deliberately counted the same
    way the library page's own summary pills count them (static/library.js,
    libUpdateTotalSize): per title, `total_size` summed, movies and series told
    apart by `is_movie`. Two places showing two different totals for the same
    library is worse than either number being slightly off, and stats.py's
    _compute_media_stats() counts per FILE with de-duplication, so it does not
    agree with the page.
    """
    from ..db import get_all_library_cache
    from .library import _lib_active_path_keys, lib_iter_cached_titles
    series = movies = episodes = 0
    total_size = 0
    newest = []
    try:
        active = _lib_active_path_keys()
        for path_key, entry in (get_all_library_cache() or {}).items():
            if path_key not in active:
                continue          # leftover of a removed scan target
            # A cache entry is a dict, and with language separation on the
            # titles hide under lang_folders -- iterating entry["data"] gave
            # the key strings, which is why this panel reported "1 title".
            for title in lib_iter_cached_titles(entry.get("data")):
                total_size += int(title.get("total_size") or 0)
                if title.get("is_movie"):
                    movies += 1
                else:
                    series += 1
                    episodes += int(title.get("total_episodes") or 0)
                if title.get("added_at"):
                    newest.append(title)
    except Exception:
        logger.debug("[HomePanels] library panel failed", exc_info=True)
    newest.sort(key=lambda t: t.get("added_at") or 0, reverse=True)
    items = []
    for title in newest[:_RECENT_MAX_ITEMS]:
        key, args = _ago(title.get("added_at"))
        items.append({"title": title.get("folder") or "", "sub_key": key,
                      "sub_args": args, "href": "/library"})
    return {
        "stats": [
            {"label_key": "hp_series", "value": str(series)},
            {"label_key": "hp_episodes", "value": str(episodes)},
            {"label_key": "hp_movies", "value": str(movies)},
            {"label_key": "hp_size", "value": _human_size(total_size)},
        ],
        "items": items,
        "link": {"href": "/library", "label_key": "hp_open_library"},
        "empty_key": "hp_empty_library",
    }


def _panel_storage() -> dict:
    items = []
    worst = 0
    for label, used, total in _disk_rows():
        percent = round(used / total * 100) if total else 0
        worst = max(worst, percent)
        items.append({
            "title": label,
            "sub_key": "hp_used",
            "sub_args": [_human_size(used), _human_size(total)],
            "percent": percent,
            "tone": "err" if percent >= 95 else ("warn" if percent >= 90 else ""),
        })
    return {
        "stats": [{"label_key": "hp_fullest", "value": "%d%%" % worst,
                   "tone": "err" if worst >= 95 else ("warn" if worst >= 90 else "ok")}],
        "items": items,
        "empty_key": "hp_empty_storage",
    }


def _panel_system() -> dict:
    paused = is_queue_paused()
    stats = []
    try:
        from ..version_info import _get_display_version
        stats.append({"label_key": "hp_version",
                      "value": _clean_text(_get_display_version(), 24)})
    except Exception:
        logger.debug("[HomePanels] version lookup failed", exc_info=True)
    encoding = _safe_count(get_encoding_badge_count)
    upscale = _safe_count(get_upscale_badge_count)
    stats.append({"label_key": "hp_encoding", "value": str(encoding),
                  "tone": "warn" if encoding else ""})
    stats.append({"label_key": "hp_upscaling", "value": str(upscale),
                  "tone": "warn" if upscale else ""})
    stats.append({"label_key": "hp_queue",
                  "value_key": "hp_state_paused" if paused else "hp_state_running",
                  "tone": "warn" if paused else "ok"})
    items = []
    for entry in get_queue() or []:
        if entry.get("status") != "failed":
            continue
        error = _clean_text(entry.get("error"))
        items.append({
            "title": entry.get("title") or "",
            "sub": error,
            "sub_key": "" if error else "hp_download_failed",
            "action": "queue",
            "tone": "err",
        })
        if len(items) >= PANEL_MAX_ITEMS:
            break
    return {
        "stats": stats,
        "items": items,
        "link": {"href": "/settings", "label_key": "hp_open_settings"},
        "empty_key": "hp_empty_system",
    }


def _queue_badge() -> int:
    by_status = get_queue_stats(visible_only=True).get("by_status") or {}
    return int(by_status.get("running", 0)) + int(by_status.get("queued", 0))


def _failed_count() -> int:
    """Queue items that ended in failure AND are still in the queue.

    ``visible_only`` is the whole point of this counter. Clearing a failed
    entry sets ``hidden = 1`` instead of deleting the row, so the download
    keeps counting towards the statistics -- but a badge is not a statistic,
    it is a to-do list. Without the flag the number only ever grew: every
    failure that was ever cleared away stayed in it for the lifetime of the
    installation, which is why it sat at 58 with nothing to act on.

    Deliberately NOT "monitored sites that are down": the live status of a
    monitor is not held in memory (_MONITOR_SITES is the *configuration*), so
    reading it means a heartbeat query per site -- far too much for something
    that runs on every home page visit. UpTime has its own page for that.
    """
    try:
        stats = get_queue_stats(visible_only=True)
        return int((stats.get("by_status") or {}).get("failed", 0))
    except Exception:
        return 0


def _system_badge() -> int:
    """Things that want a human: failed downloads plus a paused queue."""
    return _failed_count() + (1 if is_queue_paused() else 0)


# (id, label key, view, badge, admin_only, icon path)
# The icon is SVG path data for a 24x24 stroked path -- the same shape the
# registry accepts from a module, so a built-in button and a module button are
# built by exactly one code path in the client.
_BUILTIN_PANELS = (
    ("queue", "hp_p_queue", _panel_queue, _queue_badge, False,
     "M3 6h18M3 12h18M3 18h12"),
    ("activity", "hp_p_activity", _panel_activity, None, False,
     "M12 8v4l3 3M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z"),
    ("library", "hp_p_library", _panel_library, None, False,
     "M4 19.5A2.5 2.5 0 0 1 6.5 17H20M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"),
    ("storage", "hp_p_storage", _panel_storage, None, True,
     "M22 12H2M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89"
     "A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"),
    ("system", "hp_p_system", _panel_system, _system_badge, True,
     "M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4"),
)

# What a badge number MEANS, as an i18n key the client resolves and hangs on
# the button as a tooltip. A bare "58" next to "System" is unreadable -- it
# was read as a version, an uptime and an error code before anyone guessed
# "failed downloads". Module panels get no entry: their badge is theirs to
# explain, and this file cannot translate a string it has never seen.
_BADGE_HINTS = {
    "queue": "hp_badge_queue",
    "system": "hp_badge_system",
}


# ── routes ───────────────────────────────────────────────────────────────

def register_home_panel_routes(app):
    """Register the two button-bar routes. Called from register_browse_routes()."""

    register_ui_pref_key(_PANEL_PREF_KEY, _valid_panel_pref)

    @app.route("/api/home-panels")
    def api_home_panels():
        """The button bar: one entry per panel the current account may see,
        with its badge number. GET /api/home-panels.

        Called once per home page load from static/home_panels.js.
        """
        is_admin = _is_admin()
        badges = _badges(is_admin)
        out = []
        for pid, label_key, _view, _badge, admin_only, icon in _BUILTIN_PANELS:
            if admin_only and not is_admin:
                continue
            out.append({"id": pid, "label_key": label_key, "label": "",
                        "badge": badges.get(pid) or 0,
                        "badge_key": _BADGE_HINTS.get(pid, ""),
                        "badge_label": "",
                        "icon": icon, "builtin": True})
        for panel in iter_home_panels():
            if panel["admin_only"] and not is_admin:
                continue
            out.append({"id": panel["panel_id"], "label_key": "",
                        "label": _clean_text(panel["label"], 40),
                        "badge": badges.get(panel["panel_id"]) or 0,
                        # A module owns its own catalogue, so it sends ready-
                        # made text where a built-in sends a key -- same split
                        # as label/label_key everywhere else in this payload.
                        "badge_key": "",
                        "badge_label": _clean_text(panel.get("badge_label"), 120),
                        "icon": panel["icon"], "builtin": False})
        return jsonify({"panels": out, "active": _stored_panel(out)})

    @app.route("/api/home-panel/<panel_id>")
    def api_home_panel(panel_id):
        """The body of one panel. GET /api/home-panel/<id>.

        The only endpoint of this feature that polls, and only while its tab
        is visible. A panel that raises answers 200 with an error flag rather
        than 500: one broken module panel must not look like the home page is
        down.
        """
        panel_id = str(panel_id or "").strip().lower()
        is_admin = _is_admin()
        for pid, label_key, view, _badge, admin_only, _icon in _BUILTIN_PANELS:
            if pid != panel_id:
                continue
            if admin_only and not is_admin:
                return jsonify({"error": "forbidden"}), 403
            return jsonify(_render_panel(pid, "", label_key, view))
        for panel in iter_home_panels():
            if panel["panel_id"] != panel_id:
                continue
            if panel["admin_only"] and not is_admin:
                return jsonify({"error": "forbidden"}), 403
            return jsonify(_render_panel(panel["panel_id"],
                                         _clean_text(panel["label"], 40), "",
                                         panel["view"]))
        return jsonify({"error": "unknown panel"}), 404


def _render_panel(panel_id, label, label_key, view) -> dict:
    try:
        body = _clean_view(view())
    except Exception:
        logger.warning("[HomePanels] panel %r failed to render", panel_id, exc_info=True)
        return {"id": panel_id, "label": label, "label_key": label_key,
                "stats": [], "items": [], "link": None, "empty": "",
                "empty_key": "", "error": True}
    body["id"] = panel_id
    body["label"] = label
    body["label_key"] = label_key
    return body


def _badges(is_admin) -> dict:
    """Every panel's badge number, cached per process for _BADGE_TTL seconds.

    Cached across accounts on purpose: every badge here counts something
    instance-wide (queue length, failed downloads), never anything personal --
    which is also what the registry asks of a module's badge.
    """
    now = time.monotonic()
    with _cache_lock:
        if now - _badge_cache["at"] < _BADGE_TTL:
            return dict(_badge_cache["values"])
    values = {}
    for pid, _key, _view, badge, admin_only, _icon in _BUILTIN_PANELS:
        if badge is None or (admin_only and not is_admin):
            continue
        values[pid] = _safe_count(badge)
    for panel in iter_home_panels():
        if panel["badge"] is None or (panel["admin_only"] and not is_admin):
            continue
        values[panel["panel_id"]] = _safe_count(panel["badge"])
    with _cache_lock:
        # Only an admin's pass computes the admin-only badges, so a non-admin
        # request must not replace a full cache with a partial one -- merge.
        _badge_cache["values"].update(values)
        _badge_cache["at"] = now
        return dict(_badge_cache["values"])


def _valid_panel_pref(value) -> bool:
    text = str(value or "")
    if len(text) > 40:
        return False
    return text == "" or all(c.isalnum() or c in "_-" for c in text)


def _stored_panel(available) -> str:
    """The panel this account last had open, if they may still see it.

    Falls back to "" (bar closed) rather than to the first button: someone who
    closed the panel wants it closed, and someone whose admin rights were
    taken away should not land on a 403 on every visit.
    """
    try:
        from flask import session
        from ..db import get_user_ui_prefs
        uid = session.get("user_id")
        if uid is None:
            return ""
        stored = (get_user_ui_prefs(uid) or {}).get(_PANEL_PREF_KEY) or ""
    except Exception:
        return ""
    return stored if any(entry["id"] == stored for entry in available) else ""
