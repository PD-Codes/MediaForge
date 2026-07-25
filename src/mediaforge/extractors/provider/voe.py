"""VOE (voe.sx and related mirrors) video hoster extractor.

VOE embeds its player config as a JSON-encoded string inside a
``<script type="application/json">`` tag, but the string itself is put
through a multi-step, reversible obfuscation chain before being JSON-parsed
(see decode_voe_string() for the full ROT13 + junk-removal + double
base64 + byte-shift + reversal sequence). If that inline JSON is missing or
the page shows a maintenance notice / captcha, we fall back to following a
plain HTTP(S) redirect URL found in the page and retry there. A shared
captcha-solving helper (playwright.captcha) is used whenever either the
initial page or the redirect target challenges us with a captcha.

Used by: dispatched generically via extractors.provider_functions (key
"get_direct_link_from_voe"); see the provider alias table in
models/megakino_to/scraper.py (("voe", "VOE")) and the generic provider
dispatch in models/s_to/episode.py, models/filmpalast_to/episode.py,
models/aniworld_to/episode.py, and models/megakino_to/{episode,movie}.py --
VOE is the default provider ("MEDIAFORGE_PROVIDER" fallback) for most of
these model classes.
"""
import base64
import binascii
import json
import logging
import re
import time

import niquests

logger = logging.getLogger(__name__)

try:
    from ...config import DEFAULT_USER_AGENT, GLOBAL_SESSION, PROVIDER_HEADERS_D, is_source_unavailable
    from ...playwright.captcha import is_captcha_page, solve_captcha
    from ..subtitle_parse import absolutize, tracks_from_config, tracks_from_text
except ImportError:
    from mediaforge.config import DEFAULT_USER_AGENT, GLOBAL_SESSION, PROVIDER_HEADERS_D, is_source_unavailable
    from mediaforge.playwright.captcha import is_captcha_page, solve_captcha
    from mediaforge.extractors.subtitle_parse import absolutize, tracks_from_config, tracks_from_text

# -----------------------------
# Precompiled regex patterns
# -----------------------------
REDIRECT_PATTERN = re.compile(r"https?://[^'\"<>]+")
B64_PATTERN = re.compile(r"var a168c='([^']+)'")
HLS_PATTERN = re.compile(r"'hls': '(?P<hls>[^']+)'")
VOE_SCRIPT_PATTERN = re.compile(
    r'<script type="application/json">\s*"(?:\\.|[^"\\])*"\s*</script>', re.DOTALL
)
JUNK_PARTS = ["@$", "^^", "~@", "%?", "*~", "!!", "#&"]


# -----------------------------
# Helper functions
# -----------------------------
def shift_letters(input_str):
    """Apply the ROT13 cipher to alphabetic characters (step 1 of decode_voe_string).

    Non-alphabetic characters (digits, punctuation, junk markers) pass
    through unchanged so later steps can still find and strip them.
    """
    result = []
    for c in input_str:
        code = ord(c)
        if 65 <= code <= 90:  # Uppercase A-Z
            code = (code - 65 + 13) % 26 + 65
        elif 97 <= code <= 122:  # Lowercase a-z
            code = (code - 97 + 13) % 26 + 97
        result.append(chr(code))
    return "".join(result)


def replace_junk(input_str):
    """Replace VOE's junk filler substrings with underscores (step 2 of decode_voe_string).

    VOE sprinkles fixed decoy tokens (see JUNK_PARTS) throughout the
    ROT13'd payload purely to break naive base64 decoding; swapping each one
    for "_" (later stripped) restores a valid base64 string.
    """
    for part in JUNK_PARTS:
        input_str = input_str.replace(part, "_")
    return input_str


def shift_back(s, n):
    """Shift every character's code point down by n (step 4 of decode_voe_string).

    This undoes a simple Caesar-style shift VOE applies to the first-stage
    base64-decoded text before it can be base64-decoded a second time.
    """
    return "".join(chr(ord(c) - n) for c in s)


def decode_voe_string(encoded):
    """Reverse VOE's multi-step string obfuscation and parse the result as JSON.

    The encoded string travels through 5 reversible transforms, applied here
    in reverse of how VOE's JS produced it:
      1. ROT13 the letters back to their original case/position.
      2. Strip VOE's junk filler tokens (see JUNK_PARTS) that were inserted
         to make the string look non-base64 at a glance.
      3. Base64-decode the now-clean string (first encoding layer).
      4. Shift every byte back by 3 (undoes a Caesar shift applied before
         the second base64 pass).
      5. Reverse the string and base64-decode again (second encoding layer)
         to recover the final JSON text, which is then parsed.
    Each step exists purely to defeat naive scraping; none of it is
    cryptographically secure, it's just enough obfuscation to slow down
    simple regex-based extractors.
    """
    try:
        step1 = shift_letters(encoded)
        step2 = replace_junk(step1).replace("_", "")
        step3 = base64.b64decode(step2).decode()
        step4 = shift_back(step3, 3)
        step5 = base64.b64decode(step4[::-1]).decode()
        return json.loads(step5)
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ValueError(f"Failed to decode VOE string: {err}") from err


def iter_voe_configs(html):
    """Yield every decoded VOE player-config dict found in the page.

    Split out of extract_voe_source_from_html() because that config carries
    more than the stream URL: the subtitle tracks live there too, and nowhere
    else (VOE does not list them in the HLS master playlist). Decoding is the
    expensive, fragile part, so both readers share it.
    """
    script_blocks = re.findall(
        r'<script\s+type=["\']application/json["\']>(.*?)</script>', html, re.DOTALL
    )
    for script_block in script_blocks:
        encoded_text = script_block.strip()
        if encoded_text.startswith('"') and encoded_text.endswith('"'):
            encoded_text = encoded_text[1:-1]

        encoded_text = encoded_text.encode().decode("unicode_escape")

        try:
            yield decode_voe_string(encoded_text)
        except ValueError:
            continue


def extract_voe_source_from_html(html):
    """Find each ``<script type="application/json">`` block in the page and
    try to decode it via decode_voe_string() until one yields a "source" URL.
    """
    try:
        for decoded in iter_voe_configs(html):
            source = decoded.get("source")
            if source:
                return source

        return None
    except Exception:
        return None


def is_maintenance_page(html):
    """Check if the VOE CDN page is in maintenance mode."""
    return bool(re.search(r'<title>\s*Maintenance Mode\s*</title>', html, re.IGNORECASE))


# -----------------------------
# Main VOE functions
# -----------------------------
def get_direct_link_from_voe(embeded_voe_link, headers=None, max_retries=3, timeout=30):
    """Get direct VOE video URL with improved retry logic."""
    if headers is None:
        headers = PROVIDER_HEADERS_D.get("VOE", {"User-Agent": DEFAULT_USER_AGENT})
    
    # Enhanced headers for better compatibility
    enhanced_headers = {
        **headers,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    
    for attempt in range(max_retries):
        try:
            # Add delay between retries
            if attempt > 0:
                wait_time = 2 ** attempt  # Exponential backoff: 2, 4, 8 seconds
                logger.warning(f"Retry attempt {attempt + 1}/{max_retries}, waiting {wait_time}s...")
                time.sleep(wait_time)
            
            # First request to VOE
            resp = GLOBAL_SESSION.get(embeded_voe_link, headers=enhanced_headers, timeout=timeout)
            resp.raise_for_status()
            html = resp.text

            if is_source_unavailable(html, resp.status_code):
                raise ValueError("VOE: Video nicht verfügbar oder wurde entfernt.")

            # Captcha on VOE page → solve and retry this request
            if is_captcha_page(html, resp.status_code):
                solve_captcha(embeded_voe_link)
                resp = GLOBAL_SESSION.get(
                    embeded_voe_link, headers=enhanced_headers, timeout=timeout
                )
                resp.raise_for_status()
                html = resp.text

            # Try extracting source directly from the VOE embed page first
            source = extract_voe_source_from_html(html)
            if source:
                logger.debug(f"VOE source extracted on attempt {attempt + 1}")
                return source

            # Fallback: follow the redirect URL embedded in the page
            redirect_match = REDIRECT_PATTERN.search(html)
            if redirect_match:
                redirect_url = redirect_match.group(0)
                
                # Second request with retry
                for redirect_attempt in range(max_retries):
                    try:
                        if redirect_attempt > 0:
                            wait_time = 2 ** redirect_attempt
                            logger.warning(f"Redirect retry {redirect_attempt + 1}/{max_retries}, waiting {wait_time}s...")
                            time.sleep(wait_time)
                        
                        resp = GLOBAL_SESSION.get(redirect_url, headers=enhanced_headers, timeout=timeout)
                        resp.raise_for_status()
                        html = resp.text

                        # Captcha on redirect target → solve and retry
                        if is_captcha_page(html, resp.status_code):
                            solve_captcha(redirect_url)
                            resp = GLOBAL_SESSION.get(
                                redirect_url, headers=enhanced_headers, timeout=timeout
                            )
                            resp.raise_for_status()
                            html = resp.text
                        break
                    except (niquests.RequestException, Exception) as err:
                        if redirect_attempt == max_retries - 1:
                            raise ValueError(f"Weiterleitung konnte nach {max_retries} Versuchen nicht geladen werden: {err}") from err
                        continue
            
            source = extract_voe_source_from_html(html)
            if not source:
                if is_maintenance_page(html):
                    raise ValueError("Dieser VOE-Server ist derzeit im Wartungsmodus. Bitte versuche es später erneut.")
                raise ValueError("Keine VOE-Videoquelle auf der Seite gefunden.")
            
            logger.debug(f"VOE source extracted on attempt {attempt + 1}")
            return source
            
        except (niquests.RequestException, Exception) as err:
            if any(k in str(err) for k in ("Wartungsmodus", "Keine VOE-Videoquelle", "nicht verfügbar")):
                raise
            if attempt == max_retries - 1:
                raise ValueError(f"VOE-Seite konnte nach {max_retries} Versuchen nicht geladen werden: {err}") from err
            logger.warning(f"Attempt {attempt + 1} failed: {str(err)[:100]}...")
            continue
    
    raise ValueError("Unexpected error in get_direct_link_from_voe")


def get_subtitles_from_voe(embeded_voe_link, headers=None, timeout=30):
    """Return the subtitle tracks VOE's player offers, as ``[{"url","lang","label"}]``.

    VOE keeps captions out of the HLS master playlist and only ships them in
    the obfuscated player config, so we re-fetch the embed page and read the
    part get_direct_link_from_voe() discards. The raw HTML is parsed as a
    fallback for pages whose config decodes but carries no track list.

    Returns [] on any failure -- subtitles must never break a download.
    """
    try:
        if headers is None:
            headers = PROVIDER_HEADERS_D.get("VOE", {"User-Agent": DEFAULT_USER_AGENT})

        resp = GLOBAL_SESSION.get(embeded_voe_link, headers=headers, timeout=timeout)
        resp.raise_for_status()
        html = resp.text

        if is_captcha_page(html, resp.status_code):
            solve_captcha(embeded_voe_link)
            resp = GLOBAL_SESSION.get(embeded_voe_link, headers=headers, timeout=timeout)
            resp.raise_for_status()
            html = resp.text

        page_url = embeded_voe_link
        tracks = _voe_tracks_from_html(html)

        # The embed page often only redirects to the CDN host that holds the
        # real config -- same fallback the direct-link path takes.
        if not tracks:
            redirect_match = REDIRECT_PATTERN.search(html)
            if redirect_match:
                redirect_url = redirect_match.group(0)
                resp = GLOBAL_SESSION.get(redirect_url, headers=headers, timeout=timeout)
                resp.raise_for_status()
                html = resp.text
                if is_captcha_page(html, resp.status_code):
                    solve_captcha(redirect_url)
                    resp = GLOBAL_SESSION.get(redirect_url, headers=headers, timeout=timeout)
                    resp.raise_for_status()
                    html = resp.text
                page_url = redirect_url
                tracks = _voe_tracks_from_html(html)

        return absolutize(tracks, page_url)
    except Exception as err:
        logger.debug("VOE subtitle extraction failed: %s", err)
        return []


def _voe_tracks_from_html(html):
    """Subtitle tracks from a VOE page: decoded configs first, raw HTML second."""
    tracks, seen = [], set()
    try:
        for decoded in iter_voe_configs(html):
            for track in tracks_from_config(decoded):
                if track["url"] not in seen:
                    seen.add(track["url"])
                    tracks.append(track)
    except Exception:
        pass
    if not tracks:
        tracks = tracks_from_text(html)
    return tracks


def get_preview_image_link_from_voe(embeded_voe_link, headers=None):
    """Get VOE preview image URL."""
    try:
        if headers is None:
            headers = PROVIDER_HEADERS_D.get("VOE", {"User-Agent": DEFAULT_USER_AGENT})

        resp = GLOBAL_SESSION.get(embeded_voe_link, headers=headers)
        resp.raise_for_status()
        html = resp.text

        redirect_match = REDIRECT_PATTERN.search(html)
        if not redirect_match:
            raise ValueError("No redirect URL found in VOE response.")

        redirect_url = redirect_match.group(0)
        image_url = f"{redirect_url.replace('/e/', '/cache/')}_storyboard_L2.jpg"

        head_resp = GLOBAL_SESSION.head(
            image_url, headers=headers, allow_redirects=True
        )
        head_resp.raise_for_status()
        if "image" not in head_resp.headers.get("Content-Type", ""):
            raise ValueError("Preview image not reachable.")
        return image_url

    except niquests.RequestException as err:
        raise ValueError(f"Failed to fetch VOE preview image: {err}") from err


if __name__ == "__main__":
    # Tested on 2026/01/27 -> WORKING
    # Example: https://voe.sx/e/oa16zsjaqohr

    # logging.basicConfig(level=logging.DEBUG)

    link = input("Enter VOE Link: ").strip()
    if not link:
        print("Error: No link provided")
        exit(1)

    try:
        print("=" * 25)

        direct_link = get_direct_link_from_voe(link)
        print("Direct link:", direct_link)
        print("=" * 25)

        print("Preview image:", get_preview_image_link_from_voe(link))
        print("=" * 25)

        print(
            f'mpv "{direct_link}" --http-header-fields=User-Agent: "{DEFAULT_USER_AGENT}"'
        )

        print("=" * 25)
    except ValueError as e:
        print("Error:", e)
