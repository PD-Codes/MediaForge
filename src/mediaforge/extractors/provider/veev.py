"""VeeV (veev.to) video hoster extractor.

VeeV serves video from a CDN URL that is only reachable with the exact
session cookies (and matching User-Agent) issued to the browser that
requested the embed page -- a plain HTTP client or yt-dlp given just the
CDN URL will be rejected. There is no JS-decoding trick to reverse here;
instead a headless Playwright (patchright) browser loads the real embed
page, plays the video to trigger the CDN request, and we intercept that
request's URL plus the session's cookies/User-Agent from the network layer.

Two entry points build on that:
- get_direct_link_from_veev(): returns the raw CDN URL for display/logging
  only (it is not independently downloadable without the session cookies).
- download_from_veev(): the real download path. It runs the Playwright
  session once to obtain the CDN URL + cookies + User-Agent, then closes
  the browser and performs the actual transfer via plain HTTP range
  requests (using those captured cookies) in parallel chunks for speed.

Used by: models/filmpalast_to/episode.py and models/megakino_to/{episode,movie}.py
call download_from_veev directly (VeeV needs the two-step
browser-then-HTTP flow, so it bypasses the generic
extractors.provider_functions dispatch used for other hosters).
"""
import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from ...config import GLOBAL_SESSION, PROVIDER_HEADERS_D
except ImportError:
    from mediaforge.config import GLOBAL_SESSION, PROVIDER_HEADERS_D

_PREVIEW_PATTERN = re.compile(
    r'''["']image["']\s*:\s*["'](https?://[^"']+)["']'''
)
_CDN_PATTERN = re.compile(r"https?://[a-z0-9\-]+\.(veev\.to|veevcdn\.co)/")

# No get_subtitles_from_veev(): unlike the other hosters, VeeV's player_api
# response is only observable from inside the Playwright session below, so
# harvesting subtitle tracks would mean launching a headless browser purely
# for captions. Its one .vtt (vtt_timeslide_url) is the scrubber-thumbnail
# strip anyway, not a caption track.

# Cache: embed_url -> cdn_url (so get_direct_link_from_veev is cheap on repeat calls)
_cdn_cache_lock = threading.Lock()
_cdn_cache: dict[str, str] = {}


def _get_headers() -> dict:
    """Return the request headers used for plain (non-Playwright) VeeV requests."""
    return PROVIDER_HEADERS_D.get("VeeV", {"Referer": "https://veev.to/"})


# -----------------------------------------------------------------------
# Core Playwright function — intercepts CDN URL AND downloads within session
# -----------------------------------------------------------------------
def _extract_veev_details(
    embed_url: str,
    timeout_ms: int = 25_000,
) -> tuple[str | None, list | None, str | None]:
    """Load the VeeV embed page in a headless browser and capture the real CDN request.

    Opens embed_url with Playwright, waits for the page's own API call to
    resolve (to distinguish the actual video segment CDN response from the
    unrelated VTT/timeslide-thumbnail request), then nudges the <video>
    element to play (both via JS and a synthetic click, since autoplay is
    often blocked) so the browser issues the real 206 partial-content
    request to the CDN. Returns (cdn_url, cookies, user_agent) so the
    caller can replay that request outside the browser, or (None, None,
    None) if no CDN request was observed within timeout_ms.
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "patchright nicht installiert. "
            "Installieren mit: pip install patchright && patchright install chromium"
        )

    cdn_url: str | None = None
    vtt_url: str | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()

            def _on_response(response):
                nonlocal cdn_url, vtt_url
                url = response.url
                if "/dl?op=player_api&cmd=gi" in url and response.status == 200:
                    try:
                        data = response.json()
                        if data.get("status") == "success":
                            vtt_url = (data.get("file") or {}).get(
                                "vtt_timeslide_url", ""
                            )
                    except Exception:
                        pass
                    return
                if (
                    _CDN_PATTERN.match(url)
                    and response.status == 206
                    and not cdn_url
                    and url != vtt_url
                ):
                    cdn_url = url

            page.on("response", _on_response)
            try:
                page.goto(embed_url, wait_until="networkidle", timeout=timeout_ms)
                
                # Wait for video element and play it programmatically to trigger the 206 stream request
                try:
                    page.wait_for_selector("video", timeout=5000)
                    page.evaluate("""() => {
                        const v = document.querySelector('video');
                        if (v) {
                            v.muted = true;
                            v.play().catch(() => {});
                        }
                    }""")
                except Exception:
                    pass
                
                # Click the center of the page as a fallback to trigger playback
                try:
                    page.mouse.click(640, 360)
                except Exception:
                    pass
                
                # Wait a bit to capture the network response
                page.wait_for_timeout(3000)
            except Exception:
                pass

            if not cdn_url:
                return None, None, None

            cookies = context.cookies()
            user_agent = page.evaluate("navigator.userAgent")
            return cdn_url, cookies, user_agent
        finally:
            browser.close()


# -----------------------------------------------------------------------
# Public standalone download function (called from FilmPalast episode)
# -----------------------------------------------------------------------
def download_from_veev(
    embed_url: str,
    output_path: str | Path,
    cancel_event=None,
    label: str = "",
) -> None:
    """Download a VeeV video to output_path using high-performance parallel chunk requests.

    Launches Playwright to extract the CDN URL and cookies, then closes it and
    performs parallel range requests using python requests to bypass individual thread limits.
    """
    if not embed_url:
        raise ValueError("embed_url darf nicht leer sein")
    logger.debug("VeeV: extracting CDN URL and cookies for %s", embed_url)
    
    cdn_url, cookies, user_agent = _extract_veev_details(embed_url)
    if not cdn_url:
        raise ValueError(f"VeeV: Keine CDN-URL gefunden ({embed_url})")

    with _cdn_cache_lock:
        _cdn_cache[embed_url] = cdn_url

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        import requests
        import time
        import threading
        from concurrent.futures import ThreadPoolExecutor
        
        cookie_dict = {c["name"]: c["value"] for c in cookies} if cookies else {}
        
        # Get total length from Content-Range header via Range: bytes=0-0
        probe_resp = requests.get(
            cdn_url,
            headers={
                "User-Agent": user_agent,
                "Referer": "https://veev.to/",
                "Range": "bytes=0-0"
            },
            cookies=cookie_dict,
            timeout=30
        )
        
        total_size = None
        if probe_resp.status_code in (200, 206):
            cr_hdr = probe_resp.headers.get("content-range", "")
            if cr_hdr:
                m = re.search(r"bytes \d+-\d+/(\d+)", cr_hdr)
                if m:
                    total_size = int(m.group(1))
            if not total_size and probe_resp.status_code == 200:
                cl_hdr = probe_resp.headers.get("content-length")
                if cl_hdr:
                    total_size = int(cl_hdr)

        if not total_size:
            logger.warning("VeeV: Size not determined. Downloading whole file at once.")
            resp = requests.get(
                cdn_url,
                headers={
                    "User-Agent": user_agent,
                    "Referer": "https://veev.to/"
                },
                cookies=cookie_dict,
                timeout=600
            )
            resp.raise_for_status()
            out.write_bytes(resp.content)
            return

        logger.debug(
            "VeeV: total size %d bytes (%.1f MB)",
            total_size,
            total_size / (1024**2)
        )
        
        # Pre-allocate file
        with open(out, "wb") as f:
            f.truncate(total_size)
            
        common_mod = None
        try:
            from ...models.common import common as common_mod
        except ImportError:
            try:
                from mediaforge.models.common import common as common_mod
            except ImportError:
                pass
                
        if common_mod:
            with common_mod._ffmpeg_progress_lock:
                common_mod._ffmpeg_active_count += 1
                common_mod._ffmpeg_progress.update(
                    percent=0.0,
                    time="",
                    speed="",
                    bandwidth="",
                    downloaded_mb=0.0,
                    total_mb=round(total_size / 1_048_576, 1),
                    active=True,
                    phase="download",
                )
                
        # Split file into 10 MB chunks
        chunk_size = 10 * 1024 * 1024
        chunks = []
        offset = 0
        while offset < total_size:
            end = min(offset + chunk_size - 1, total_size - 1)
            chunks.append((offset, end))
            offset += chunk_size

        f_out = open(out, "r+b")
        write_lock = threading.Lock()
        downloaded_bytes = 0
        stats_lock = threading.Lock()
        start_time = time.monotonic()
        
        errors = []
        
        def download_chunk(range_tuple):
            nonlocal downloaded_bytes
            if cancel_event and cancel_event.is_set():
                return
                
            start_byte, end_byte = range_tuple
            headers = {
                "User-Agent": user_agent,
                "Referer": "https://veev.to/",
                "Range": f"bytes={start_byte}-{end_byte}"
            }
            
            for attempt in range(5):
                if cancel_event and cancel_event.is_set():
                    return
                try:
                    r = requests.get(cdn_url, headers=headers, cookies=cookie_dict, stream=True, timeout=30)
                    if r.status_code not in (200, 206):
                        raise ValueError(f"HTTP {r.status_code}")
                        
                    current_offset = start_byte
                    for block in r.iter_content(chunk_size=256 * 1024):  # 256 KB blocks
                        if cancel_event and cancel_event.is_set():
                            return
                        if not block:
                            continue
                        with write_lock:
                            f_out.seek(current_offset)
                            f_out.write(block)
                        current_offset += len(block)
                        
                        with stats_lock:
                            downloaded_bytes += len(block)
                            
                    break  # Success
                except Exception as chunk_err:
                    if attempt == 4:
                        logger.error("VeeV: Failed to download chunk %d-%d after 5 attempts", start_byte, end_byte)
                        errors.append(chunk_err)
                    else:
                        logger.warning("VeeV: Chunk download %d-%d attempt %d failed: %s. Retrying...", start_byte, end_byte, attempt + 1, chunk_err)
                        time.sleep(2)
                        
        try:
            # Use 16 parallel threads for high download speed (up to 20-30 MB/s)
            with ThreadPoolExecutor(max_workers=16) as executor:
                futures = [executor.submit(download_chunk, c) for c in chunks]
                
                while not all(fut.done() for fut in futures):
                    if cancel_event and cancel_event.is_set():
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise RuntimeError("Download cancelled")
                        
                    now = time.monotonic()
                    elapsed = now - start_time
                    with stats_lock:
                        cur_downloaded = downloaded_bytes
                        
                    percent = (cur_downloaded / total_size) * 100.0
                    speed = cur_downloaded / elapsed if elapsed > 0 else 0.0
                    speed_str = f"{speed / 1_048_576:.1f} MB/s" if speed else ""
                    downloaded_mb = round(cur_downloaded / 1_048_576, 1)
                    total_mb = round(total_size / 1_048_576, 1)
                    eta_sec = max(0, int((total_size - cur_downloaded) / speed)) if speed > 0 else 0
                    elapsed_str = f"{int(elapsed // 3600):02d}:{int((elapsed % 3600) // 60):02d}:{int(elapsed % 60):02d}"
                    
                    if common_mod:
                        with common_mod._ffmpeg_progress_lock:
                            common_mod._ffmpeg_progress.update(
                                percent=round(percent, 1),
                                time=elapsed_str,
                                speed=speed_str,
                                bandwidth=speed_str,
                                downloaded_mb=downloaded_mb,
                                total_mb=total_mb,
                                eta_sec=eta_sec,
                                active=True,
                            )
                        if hasattr(common_mod, "_print_cli_progress"):
                            common_mod._print_cli_progress(percent, elapsed_str, speed_str, label)
                            
                    time.sleep(0.5)
                    
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Download cancelled")
                
            if errors:
                raise errors[0]
                
            # Clear progress line
            import sys
            if sys.stderr.isatty():
                sys.stderr.write("\r" + " " * 120 + "\r")
                sys.stderr.flush()
                
        finally:
            f_out.close()
            if common_mod:
                with common_mod._ffmpeg_progress_lock:
                    common_mod._ffmpeg_active_count -= 1
                    common_mod._ffmpeg_progress.update(
                        percent=0.0,
                        time="",
                        speed="",
                        bandwidth="",
                        downloaded_mb=0.0,
                        total_mb=0.0,
                        active=common_mod._ffmpeg_active_count > 0,
                        phase="" if common_mod._ffmpeg_active_count == 0 else "download"
                    )
                    
    except Exception as e:
        if out.exists():
            try:
                out.unlink()
                logger.info("VeeV: Deleted partial file %s due to error/cancellation", out.name)
            except Exception as unlink_err:
                logger.warning("VeeV: Failed to delete partial file %s: %s", out.name, unlink_err)
        raise e


# -----------------------------------------------------------------------
# Extractor entry point (generic provider dispatch via stream_url)
# -----------------------------------------------------------------------
def get_direct_link_from_veev(embed_url: str) -> str:
    """Return the VeeV CDN URL (for display/logging only).

    The CDN URL alone is not downloadable by yt-dlp or a plain HTTP client:
    the CDN requires the session cookies and User-Agent that were issued
    together with it, which this function does not return. The actual
    download MUST go through download_from_veev(), which captures and reuses
    those cookies/User-Agent alongside the CDN URL.
    """
    if not embed_url:
        raise ValueError("Embed URL darf nicht leer sein")

    with _cdn_cache_lock:
        if embed_url in _cdn_cache:
            return _cdn_cache[embed_url]

    logger.debug("VeeV: launching headless browser (stream_url path)")
    cdn_url, _, _ = _extract_veev_details(embed_url)

    if not cdn_url:
        raise ValueError(f"VeeV: Keine Videoquelle gefunden ({embed_url})")

    with _cdn_cache_lock:
        _cdn_cache[embed_url] = cdn_url

    logger.debug("VeeV: CDN URL: %s…", cdn_url[:80])
    return cdn_url


def get_preview_image_link_from_veev(embed_url: str) -> str | None:
    """Get VeeV preview image URL."""
    if not embed_url:
        return None
    try:
        resp = GLOBAL_SESSION.get(embed_url, headers=_get_headers(), timeout=10)
        resp.raise_for_status()
        match = _PREVIEW_PATTERN.search(resp.text)
        return match.group(1) if match else None
    except Exception:
        return None


if __name__ == "__main__":
    link = input("Enter VeeV embed URL: ").strip()
    if not link:
        print("Error: No link provided")
        exit(1)
    out = input("Output path (.mp4): ").strip() or "veev_out.mp4"
    print("Downloading…")
    download_from_veev(link, out)
    print("Done:", out)
