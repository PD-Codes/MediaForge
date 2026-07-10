"""ICS (iCalendar) export for MediaCalendar.

Plain stdlib string building -- no calendar library dependency needed for
a handful of VEVENT blocks, and it keeps this module free of any new
third-party package the rest of MediaForge would have to install. One
all-day VEVENT per resolved release (movie release date, or the next
airing episode for a TV show), so subscribing calendar apps show "Movie X"
/ "Show Y S02E05" on the right day. UIDs are stable (derived from
tmdb_id/media_type/season/episode) so re-exporting/re-subscribing doesn't
create duplicate events in the subscribing app.
"""

from datetime import datetime, timezone


def _escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _fold(line: str) -> str:
    # RFC 5545 line folding: split at 75 octets, continuation lines start
    # with a space. Simple char-count approximation is fine for our
    # (mostly ASCII-ish, always-short) SUMMARY/DESCRIPTION lines.
    if len(line) <= 75:
        return line
    parts = [line[:75]]
    rest = line[75:]
    while rest:
        parts.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(parts)


def _event(release: dict, calendar_name: str) -> str:
    date_str = (release.get("release_date") or "").replace("-", "")
    if not date_str:
        return ""
    title = release["title"]
    if release.get("media_type") == "tv" and release.get("episode_number", -1) >= 0:
        season = release.get("season_number", -1)
        episode = release.get("episode_number", -1)
        title = f"{title} S{season:02d}E{episode:02d}"
        if release.get("episode_title"):
            title += f" - {release['episode_title']}"
    uid = (f"mc-{release['tmdb_id']}-{release.get('media_type')}-"
           f"{release.get('season_number', -1)}-{release.get('episode_number', -1)}@mediaforge")
    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_stamp}",
        f"DTSTART;VALUE=DATE:{date_str}",
        f"SUMMARY:{_escape(title)}",
        f"DESCRIPTION:{_escape(release.get('overview', ''))}",
        f"CATEGORIES:{_escape(calendar_name)}",
        "END:VEVENT",
    ]
    return "\r\n".join(_fold(line) for line in lines)


def build_ics(calendar_name: str, releases: list) -> str:
    """Full .ics document text for one calendar's resolved releases."""
    events = [_event(r, calendar_name) for r in releases if r.get("release_date")]
    events = [e for e in events if e]
    body = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//MediaForge//MediaCalendar//DE",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
        *events,
        "END:VCALENDAR",
    ]
    return "\r\n".join(body) + "\r\n"
