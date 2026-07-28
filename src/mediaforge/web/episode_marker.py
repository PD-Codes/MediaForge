"""The one SxxExx pattern MediaForge uses to read season/episode off a file name.

Every "do I already have this episode?" answer in the app ultimately comes from
a file name, so all of them have to read that name the same way. They did not:
routes/library.py had already been fixed to ``S(\\d{1,4})E(\\d{1,4})(?!\\d)``
after a truncation bug, while routes/search.py (three copies) and
autosync_worker.py still carried the old ``S(\\d{2})E(\\d{2,3})``.

That old pattern *silently truncates*. "S01E1128" matches as episode 112, with
the trailing "8" simply left over -- so the file on disk and the episode being
checked never line up, the download dialog shows "not downloaded" for something
that is right there, and auto-sync re-downloads it on every single run. Four
digits used to be exotic; with AniWorld's absolute episode numbering they are
ordinary (One Piece is past episode 1100), which is what turned a latent bug
into a routine one.

``(?!\\d)`` is the fix: the group has to consume the whole number or not match
at all, so a number that is too long falls through to the fallback instead of
being cut short. Seasons are accepted with 1-4 digits as well, so the common
"S1E1" spelling is recognised too.

Used by: routes/library.py, routes/search.py, autosync_worker.py,
queue_worker.py (Jellyfin NFO).
"""

import re

# "S01E063", "S1E1", "S02E1128" -- the full number or nothing.
EPISODE_MARKER_RE = re.compile(r"S(\d{1,4})E(\d{1,4})(?!\d)", re.IGNORECASE)

# Season-less fallback ("... E013 ..."). Deliberately still requires at least
# two digits: a bare "E1" appears inside far too many real titles.
FALLBACK_EPISODE_RE = re.compile(r"\bE(\d{2,4})(?!\d)\b", re.IGNORECASE)


def season_episode_from_name(name):
    """Return (season, episode) read off a file name, or (None, None).

    Only the full SxxExx marker counts here. The season-less fallback is
    deliberately not used: a caller that gets a season back must be able to
    trust it, and guessing season 1 from a bare "E013" would be a guess.
    """
    match = EPISODE_MARKER_RE.search(str(name or ""))
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))
