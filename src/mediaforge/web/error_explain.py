"""Turn a raw download failure into a cause and a next step.

A queue error today is whatever the provider, yt-dlp, ffmpeg or the OS said,
often several frames of a traceback. That text is exactly right for a bug
report and exactly wrong for the person looking at the queue, whose question
is always one of two: *is this my fault, and what do I do now?*

This module maps a raw error to a small, closed set of causes. Each cause has
an i18n key for its label, an i18n key for the suggested action, whether
retrying is worth it at all, and a severity. The raw text is never thrown
away -- the UI shows the explanation first and keeps the original one click
below it.

Design notes
------------
* Order matters. The first matching rule wins, so the specific patterns come
  before the general ones. "No space left on device" must not be swallowed by
  the generic OSError rule.
* Matching is on lower-cased substrings and a few regexes, deliberately not on
  exception classes: most of these arrive as strings from a subprocess, and
  the same condition surfaces under three different class names depending on
  which library noticed it.
* Both languages, always. A large part of the raised catalogue is German --
  the extractors under ``mediaforge/extractors`` speak German, yt-dlp and the
  OS speak English, and one job mixes both. Every rule below therefore needs
  its German twin, or the explanation silently degrades to ``unknown`` for
  exactly the errors users hit most.
* Unknown stays unknown. A wrong explanation is worse than none -- it sends
  somebody to fix the wrong thing -- so anything unmatched is reported as
  ``unknown`` with the raw text and no advice.
"""

from __future__ import annotations

import re

# (cause_id, matcher) -- matcher is a lowercase substring, a tuple of
# substrings (all must appear), or a compiled regex.
_RULES: list[tuple[str, object]] = [
    # --- disk and filesystem -------------------------------------------------
    ("disk_full",        "no space left on device"),
    ("disk_full",        "errno 28"),
    ("disk_full",        "not enough space"),
    ("path_missing",     "custom path"),
    ("path_missing",     "no such file or directory"),
    ("path_missing",     "datei nicht gefunden"),
    ("permission",       "permission denied"),
    ("permission",       "errno 13"),
    ("permission",       "nicht überschreibbar"),
    ("file_in_use",      "used by another process"),
    ("file_in_use",      "errno 26"),

    # --- captcha and bot protection -----------------------------------------
    ("captcha",          "captcha"),
    ("captcha",          "cf_clearance"),
    ("captcha",          "just a moment"),
    ("captcha",          "checking your browser"),
    ("rate_limited",     "429"),
    ("rate_limited",     "too many requests"),

    # Our own SSRF/allow-list guard, not the remote site. Must win over the
    # generic "forbidden" rule below, which would send the user hunting for a
    # VPN when the block came from MediaForge itself.
    ("security_blocked", "aus sicherheitsgründen nicht erlaubt"),
    ("security_blocked", "forbidden address"),
    ("security_blocked", "ist aus sicherheitsgr"),

    ("blocked",          "403"),
    ("blocked",          "forbidden"),

    # --- provider / hoster ---------------------------------------------------
    # The hoster is up but parked. Retrying later is the fix, switching hoster
    # is not -- so this cannot fall into hoster_dead.
    ("hoster_maintenance", "wartungsmodus"),
    ("hoster_maintenance", "maintenance mode"),

    # Language first: "Nicht verfügbar in: Deutsch" would otherwise be eaten
    # by the German hoster_dead rule further down.
    ("language_missing", "nicht verfügbar in"),
    ("language_missing", "no provider data found for language"),
    ("language_missing", "language"),

    ("hoster_dead",      "no video url"),
    ("hoster_dead",      "unable to extract"),
    ("hoster_dead",      "file was deleted"),
    ("hoster_dead",      "video not found"),
    ("hoster_dead",      "410"),
    # German extractor catalogue (voe/veev/vidoza/vidmoly/filemoon/doodstream).
    # The hoster name sits *inside* the noun ("Keine VOE-Videoquelle"), so a
    # plain substring misses -- match the tail of the compound instead.
    ("hoster_dead",      "videoquelle"),
    ("hoster_dead",      "keine cdn-url"),
    ("hoster_dead",      "video nicht verfügbar"),
    ("hoster_dead",      "wurde entfernt"),
    # English half of the extractors: "No <Hoster> video source found in page."
    ("hoster_dead",      re.compile(r"no (?:\S+ )?video source")),
    ("hoster_dead",      "no usable stream url"),
    ("hoster_dead",      "no redirect url"),
    ("hoster_dead",      "empty video base url"),
    ("hoster_dead",      "cannot extract filecode"),

    ("episode_missing",  "episode not found"),

    # A hoster we know of but have not written an extractor for yet. Retrying
    # will never help; the queue falls back to the next hoster.
    ("not_implemented",  "is not implemented yet"),
    ("not_implemented",  "not yet implemented"),
    ("not_implemented",  "is not yet implemented"),

    ("provider_layout",  "layout"),
    ("provider_layout",  "unexpected response"),
    ("provider_layout",  "no html content"),
    ("provider_layout",  "failed to decode voe string"),

    # --- network -------------------------------------------------------------
    ("dns",              "name or service not known"),
    ("dns",              "nodename nor servname"),
    ("dns",              "getaddrinfo"),
    ("dns",              "nicht auflösbar"),
    ("tls",              "certificate verify failed"),
    ("tls",              "ssl"),
    ("timeout",          "timed out"),
    ("timeout",          "timeout"),
    ("timeout",          "zeitüberschreitung"),
    ("connection",       "connection reset"),
    ("connection",       "connection refused"),
    ("connection",       "connection aborted"),
    ("connection",       "network is unreachable"),
    ("connection",       "verbindung"),
    # "…konnte nach N Versuchen nicht geladen werden: <err>". Deliberately last
    # in this block: the appended <err> usually names the real cause (timeout,
    # DNS, TLS) and should win over this catch-all.
    ("connection",       "nicht geladen werden"),

    # --- our own watchdogs ---------------------------------------------------
    ("watchdog_hang",    "watchdog"),
    ("stalled",          "no progress"),
    ("cancelled",        "cancelled"),
    ("cancelled",        "abgebrochen"),

    # --- ffmpeg --------------------------------------------------------------
    # A non-zero exit is a broken input or a bad parameter, never a missing
    # binary -- the old single "ffmpeg" rule sent people to re-download a
    # perfectly fine ffmpeg. Order matters: this must precede ffmpeg_missing.
    ("ffmpeg_failed",    "ffmpeg exited with code"),
    ("ffmpeg_failed",    "ffprobe exited with code"),
    ("ffmpeg_missing",   "ffmpeg"),
    ("ffmpeg_missing",   "ffprobe"),

    # --- generic HTTP, last so a specific status above wins ------------------
    ("server_error",     re.compile(r"\b5\d\d\b")),
    ("not_found",        re.compile(r"\b404\b")),
]

# cause -> (label key, advice key, retry worth it, severity)
CAUSES: dict[str, tuple[str, str, bool, str]] = {
    "disk_full":        ("err_disk_full", "fix_disk_full", False, "critical"),
    "path_missing":     ("err_path_missing", "fix_path_missing", False, "warning"),
    "permission":       ("err_permission", "fix_permission", False, "warning"),
    "file_in_use":      ("err_file_in_use", "fix_file_in_use", True, "warning"),
    "captcha":          ("err_captcha", "fix_captcha", True, "info"),
    "rate_limited":     ("err_rate_limited", "fix_rate_limited", True, "info"),
    "blocked":          ("err_blocked", "fix_blocked", True, "warning"),
    "security_blocked": ("err_security_blocked", "fix_security_blocked", False, "warning"),
    "hoster_dead":      ("err_hoster_dead", "fix_hoster_dead", True, "info"),
    "hoster_maintenance": ("err_hoster_maintenance", "fix_hoster_maintenance", True, "info"),
    "not_implemented":  ("err_not_implemented", "fix_not_implemented", False, "info"),
    "episode_missing":  ("err_episode_missing", "fix_episode_missing", False, "info"),
    "language_missing": ("err_language_missing", "fix_language_missing", False, "info"),
    "provider_layout":  ("err_provider_layout", "fix_provider_layout", False, "warning"),
    "dns":              ("err_dns", "fix_dns", True, "warning"),
    "tls":              ("err_tls", "fix_tls", True, "warning"),
    "timeout":          ("err_timeout", "fix_timeout", True, "info"),
    "connection":       ("err_connection", "fix_connection", True, "info"),
    "watchdog_hang":    ("err_watchdog", "fix_watchdog", True, "warning"),
    "stalled":          ("err_stalled", "fix_stalled", True, "info"),
    "cancelled":        ("err_cancelled", "fix_cancelled", False, "info"),
    "ffmpeg_missing":   ("err_ffmpeg", "fix_ffmpeg", True, "warning"),
    "ffmpeg_failed":    ("err_ffmpeg_failed", "fix_ffmpeg_failed", True, "warning"),
    "server_error":     ("err_server", "fix_server", True, "info"),
    "not_found":        ("err_not_found", "fix_not_found", False, "info"),
    "unknown":          ("err_unknown", "fix_unknown", True, "info"),
}


def classify(raw: str) -> str:
    """Return the cause id for one raw error string."""
    if not raw:
        return "unknown"
    text = str(raw).lower()
    for cause, matcher in _RULES:
        if isinstance(matcher, str):
            if matcher in text:
                return cause
        elif isinstance(matcher, tuple):
            if all(part in text for part in matcher):
                return cause
        elif matcher.search(text):
            return cause
    return "unknown"


def explain(raw: str) -> dict:
    """Return ``{cause, label, advice, retryable, severity, raw}``.

    ``label`` and ``advice`` are **i18n keys**, not text. The server does not
    know the viewer's language here (this is called from a JSON endpoint that
    several pages share), and translating in the template is what keeps these
    strings in the Babel catalogue -- a string written in Python never reaches
    it, since babel.cfg extracts Jinja only.
    """
    cause = classify(raw)
    label, advice, retryable, severity = CAUSES.get(cause, CAUSES["unknown"])
    return {
        "cause": cause,
        "label": label,
        "advice": advice,
        "retryable": retryable,
        "severity": severity,
        "raw": str(raw or "")[:4000],
    }


def summarize(errors) -> dict:
    """Group a job's error list by cause.

    A twelve-episode job that failed twelve times almost always failed twelve
    times for one reason. Showing that reason once, with a count, is the
    difference between a queue entry somebody reads and one they scroll past.
    """
    buckets: dict[str, dict] = {}
    for entry in errors or []:
        raw = entry.get("error") if isinstance(entry, dict) else str(entry)
        info = explain(raw)
        bucket = buckets.setdefault(info["cause"], {
            "cause": info["cause"],
            "label": info["label"],
            "advice": info["advice"],
            "retryable": info["retryable"],
            "severity": info["severity"],
            "count": 0,
            "examples": [],
        })
        bucket["count"] += 1
        if len(bucket["examples"]) < 3:
            bucket["examples"].append(info["raw"][:300])

    order = {"critical": 0, "warning": 1, "info": 2}
    return {
        "total": sum(b["count"] for b in buckets.values()),
        "causes": sorted(buckets.values(),
                         key=lambda b: (order.get(b["severity"], 3), -b["count"])),
        # True only when every cause is worth retrying. A job that is half
        # "disk full" is not fixed by pressing retry, and offering it as the
        # obvious next step wastes the user's time twice.
        "retryable": bool(buckets) and all(b["retryable"] for b in buckets.values()),
    }
