"""Sanitizing helpers applied to every piece of data before it is allowed
into a telemetry event payload (TELEMETRY_PLAN.md §5 / IMPLEMENTATION_PLAN §3.3).

Order matters and every step here is deliberately conservative -- when in
doubt, redact rather than risk a credential/URL-token leaking into a report
that leaves the user's device:

  1. extract_traceback_frames() -- filename/lineno/name/line ONLY, via
     traceback.extract_tb(). Never touches frame.f_locals or any other
     runtime value.
  2. shorten_path() -- absolute paths collapsed to the part from
     "mediaforge/" onward, so a Windows username / install path never
     leaves the device.
  3. clean_url() / redact_urls_in_text() -- query string and fragment
     stripped from any URL (that's typically where session tokens for
     streaming hosters live).
  4. redact_secrets() -- a regex safety net over the fully-assembled text,
     independent of steps 1-3, catching Authorization headers, Bearer
     tokens, api_key=/password=/token= patterns.
  5. A ~8 KB size cap on the final traceback text, so a pathological
     recursion error with thousands of frames can't blow up the ingest
     payload.
"""

import re
import traceback
from urllib.parse import urlsplit

MAX_TRACEBACK_BYTES = 8 * 1024  # ~8 KB cap, see module docstring point 5

# Absolute path -> "mediaforge/..." onward. Matches both forward and
# backward slashes so it works the same on Windows and POSIX.
_MEDIAFORGE_PATH_RE = re.compile(r".*?([\\/]mediaforge[\\/].*)", re.IGNORECASE)

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)

# Regex safety net (point 4 above) -- deliberately broad and case-insensitive.
# Each pattern keeps its own "key=" / "Bearer " prefix and replaces only the
# value, so the redacted text still shows *what kind* of secret was removed.
_SECRET_PATTERNS = [
    re.compile(r"(authorization\s*:\s*)\S+", re.IGNORECASE),
    re.compile(r"(bearer\s+)\S+", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(password\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(token\s*[=:]\s*)\S+", re.IGNORECASE),
]


def is_adult_provider(provider) -> bool:
    """HARD RULE -- not a setting, not a toggle, not something a future
    editor of this file should ever make configurable.

    MediaForge has one age-gated 18+ provider, ``hanime_tv``. Per
    TELEMETRY_PLAN.md §2/§7: the ONLY telemetry data point ever allowed for
    this provider is the stage-2 usage counter ``flag.hanime_tv`` (see
    ``events.build_feature_flag_event``, which does NOT call this guard --
    that is the one intentional exception). Every other event builder in
    ``events.py`` (feature detail, download, play, watch) calls this
    function first and returns ``None`` immediately if it is True, so no
    title, error message, play event, progress value or watch time for this
    provider is ever built -- let alone sent -- regardless of which stages
    or data_keys the user has enabled in Settings.

    If you are adding a new event builder to events.py: call this function
    first, before touching any provider-specific data. If you are adding a
    new adult-gated provider to the app: add it to the check below, don't
    create a second guard function.
    """
    return (provider or "").strip().lower() == "hanime_tv"


def shorten_path(path) -> str:
    """Collapse an absolute path down to the part starting at 'mediaforge/'
    (case-insensitive, either slash style), discarding everything before it
    -- in particular the OS username and install directory. Paths that don't
    contain a 'mediaforge' segment (e.g. stdlib/site-packages frames) are
    returned with only backslashes normalized to forward slashes, since
    those aren't very informative for MediaForge's own crash reports anyway
    but still shouldn't leak a full local path unnecessarily."""
    if not path:
        return path
    match = _MEDIAFORGE_PATH_RE.match(path)
    if match:
        return match.group(1).replace("\\", "/")
    return path.replace("\\", "/")


def clean_url(url) -> str:
    """Return scheme://host/path only -- query string and fragment (where
    streaming hosters typically embed session tokens) are dropped."""
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return url
        return f"{parts.scheme}://{parts.netloc}{parts.path}"
    except Exception:
        return url


def redact_urls_in_text(text) -> str:
    """Find every http(s):// URL embedded in *text* and clean it via
    clean_url(), leaving the surrounding text untouched."""
    if not text:
        return text
    return _URL_RE.sub(lambda m: clean_url(m.group(0)), text)


def redact_secrets(text) -> str:
    """Regex safety net (point 4 in the module docstring) -- applied to the
    fully-assembled string, independent of whatever URL/path cleanup already
    happened."""
    if not text:
        return text
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


def _clean_text(text) -> str:
    """URL-clean + secret-redact, in that order (redact_secrets is a
    superset safety net so it always runs last)."""
    return redact_secrets(redact_urls_in_text(text or ""))


def extract_traceback_frames(tb):
    """Return a list of {filename, lineno, name, line} dicts for *tb* (a
    traceback object), via traceback.extract_tb() -- NEVER frame.f_locals or
    any other runtime value. filename is shortened via shorten_path(); line
    (the source line text) is URL/secret-cleaned since it can legitimately
    contain literal strings (e.g. a URL a request was made with)."""
    frames = []
    for frame in traceback.extract_tb(tb):
        frames.append({
            "filename": shorten_path(frame.filename),
            "lineno": frame.lineno,
            "name": frame.name,
            "line": _clean_text(frame.line or ""),
        })
    return frames


def sanitize_exception(exc_type, exc_value, tb) -> dict:
    """Build the sanitized payload for a crash_reports event out of a raw
    (exc_type, exc_value, tb) triple, as returned by sys.exc_info() /
    passed to sys.excepthook.

    Returns a dict with exception_type/message/frames/traceback_text, all
    already sanitized and size-capped -- safe to drop straight into an event
    payload.
    """
    frames = extract_traceback_frames(tb)
    message = _clean_text(str(exc_value))[:2000]
    exception_type = getattr(exc_type, "__name__", str(exc_type))

    lines = [f"{exception_type}: {message}"]
    for f in frames:
        lines.append(f'  File "{f["filename"]}", line {f["lineno"]}, in {f["name"]}')
        if f["line"]:
            lines.append(f'    {f["line"]}')
    traceback_text = "\n".join(lines)

    encoded = traceback_text.encode("utf-8", errors="ignore")
    if len(encoded) > MAX_TRACEBACK_BYTES:
        traceback_text = encoded[:MAX_TRACEBACK_BYTES].decode("utf-8", errors="ignore") + "\n...[truncated]"

    return {
        "exception_type": exception_type,
        "message": message,
        "frames": frames,
        "traceback_text": traceback_text,
    }
