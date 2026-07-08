"""A generic yt-dlp-backed direct-link download job (GitHub issue #8).

Unlike the scraper-site *Episode classes (AniworldEpisode, FilmPalastEpisode,
...), a direct link has no series/season/provider/dub-sub structure to
reconcile: the user pastes a raw stream URL (typically an .m3u8 HLS master
playlist, but anything yt-dlp supports works), picks one of the quality
variants returned by models/direct_link/probe.py, and gives the job a
filename. This class exists purely to satisfy the small interface
web/queue_worker.py's dispatch loop expects from an "episode" object
(``__init__(url, selected_path=...)``, ``.download(cancel_event=...)``,
``._episode_path``) so that loop's retry/watchdog/history machinery can be
reused unchanged -- see queue_worker.py's branch on
``item.get("provider") == "Direct"``, which constructs this class directly
instead of going through mediaforge.providers.resolve_provider().
"""

import os
import re
from pathlib import Path

from ...logger import get_logger
from ..common.common import _run_ytdlp_download
from .probe import DIRECT_LINK_USER_AGENT, discover_and_resolve

logger = get_logger(__name__)


class DirectLinkEpisode:
    """Represents a single Direct Link download job.

    Parameters:
        url:              Required. The raw/embed-page URL the user pasted
                          (e.g. an .m3u8 link, or a VOE/Vidoza/... embed page).
        title:            Required. User-provided filename (job title), used
                          as-is (minus filesystem-unsafe characters) for the
                          output file name.
        selected_path:    Optional. Resolved absolute output directory (custom
                          path or language-separation subfolder), as computed
                          by queue_worker.py. Falls back to
                          MEDIAFORGE_DOWNLOAD_PATH / ~/Downloads when unset.
        format_id:        Optional. The exact yt-dlp format selector chosen in
                          the format-picker modal (see probe.py). Falls back
                          to "bestvideo+bestaudio/best" when unset.
        source_provider:  Optional. The embed host detected at probe time
                          (e.g. "VOE"), if any -- informational (shown in the
                          queue) and used as a hint, but download() always
                          re-runs the full discovery (direct link, or a fresh
                          page-scan) rather than reusing anything resolved at
                          probe time, since many embed hosts hand out
                          short-lived, signed CDN URLs that can expire while
                          the job is still waiting in the queue.
    """

    def __init__(self, url, title, selected_path=None, format_id=None, source_provider=None):
        self.url = url
        self.title = title
        self.format_id = format_id or "bestvideo+bestaudio/best"
        self.source_provider = source_provider or None
        self._selected_path_param = selected_path

    @property
    def title_cleaned(self):
        """Title with filesystem-unsafe characters removed, for use in file names."""
        t = re.sub(r'[<>:"/\\|?*]', "", self.title or "")
        t = t.strip()
        return t or "Direct Download"

    @property
    def _folder_path(self):
        if self._selected_path_param:
            base = Path(self._selected_path_param).expanduser()
        else:
            raw = os.getenv("MEDIAFORGE_DOWNLOAD_PATH", str(Path.home() / "Downloads"))
            base = Path(raw).expanduser()
        if not base.is_absolute():
            base = Path.home() / base
        return base

    @property
    def _episode_path(self):
        return self._folder_path / f"{self.title_cleaned}.mkv"

    def download(self, cancel_event=None, **kwargs):
        """Download the selected format via yt-dlp. Returns False if the
        output file already exists (skip), True on a fresh download."""
        if self._episode_path.exists():
            logger.debug(f"[SKIPPED] {self._episode_path.name} already exists")
            return False

        os.makedirs(self._folder_path, exist_ok=True)
        ep_label = self.title_cleaned

        stream_url = self.url
        headers = {"User-Agent": DIRECT_LINK_USER_AGENT}
        if self.source_provider:
            # Re-run discovery fresh rather than reusing anything resolved
            # at probe time (covers both a direct embed link and a page
            # whose embedded hoster link needs a fresh page-scan), since
            # signed CDN tokens from probe time may have expired by now.
            # discover_and_resolve() already falls back to (self.url, a
            # default User-Agent) on any failure, so this never raises.
            _, stream_url, headers = discover_and_resolve(self.url, timeout=20)

        _run_ytdlp_download(
            stream_url,
            self._episode_path,
            headers=headers,
            label=ep_label,
            cancel_event=cancel_event,
            format_override=self.format_id,
        )
        return True
