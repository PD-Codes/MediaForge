"""Sanitizing helpers applied to every piece of data before it is allowed
into a telemetry event payload (TELEMETRY_PLAN.md §5 / IMPLEMENTATION_PLAN §3.3).

Order matters and every step here is deliberately conservative -- when in
doubt, redact rather than risk a credential/URL-token leaking into a report
that leaves the user's device:

  1. extract_traceback_frames() -- filename/lineno/name/line ONLY, via
     traceback.extract_tb(). Never touches frame.f_locals or any other
     runtime value.
  2. shorten_path() -- absolute paths collapsed to the part from
     "mediaforge/" onward (or the home directory replaced by "~" when there
     is no such segment), so a Windows username / install path never leaves
     the device. collapse_paths_in_text() applies the same treatment to
     paths embedded in free text (exception messages, source lines, log
     messages), which _clean_text() runs for every one of them.
  3. clean_url() / redact_urls_in_text() -- query string and fragment
     stripped from any URL (that's typically where session tokens for
     streaming hosters live). strip_url_paths() goes one step further for
     log-derived crash messages and drops the path as well, since that is
     the content-identifying part.
  4. redact_secrets() -- a regex safety net over the fully-assembled text,
     independent of steps 1-3, catching Authorization headers, Bearer
     tokens, api_key=/password=/token= patterns.
  5. A ~8 KB size cap on the final traceback text, so a pathological
     recursion error with thousands of frames can't blow up the ingest
     payload.
"""

import hashlib
import re
import traceback
from urllib.parse import urlsplit

MAX_TRACEBACK_BYTES = 8 * 1024  # ~8 KB cap, see module docstring point 5

# Absolute path -> "mediaforge/..." onward. Matches both forward and
# backward slashes so it works the same on Windows and POSIX.
_MEDIAFORGE_PATH_RE = re.compile(r".*?([\\/]mediaforge[\\/].*)", re.IGNORECASE)

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)

# Home directory -> "~". Runs on already slash-normalized text, so only forward
# slashes need to be handled. This is what actually keeps a Windows/Linux
# username off the wire for paths that have no "mediaforge" segment at all
# (a media root on a NAS, an ffmpeg temp file, a FileNotFoundError on a file
# under C:\Users\<name>\Downloads, ...).
_HOME_DIR_RE = re.compile(r"(?:[a-z]:)?/(?:users|home)/[^/]+", re.IGNORECASE)
_ROOT_HOME_RE = re.compile(r"(?:[a-z]:)?/root(?![^\W_])", re.IGNORECASE)

# Absolute-path-looking token inside free text (an exception message, a source
# line, a log message). Deliberately anchored on either a drive letter or one of
# the usual absolute roots rather than "any slash", so it cannot swallow ordinary
# prose or an already-cleaned URL's host. Each match is handed to shorten_path().
_ABS_PATH_IN_TEXT_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]"
    r"|/(?:home|users|root|mnt|media|srv|opt|var|tmp|data|app|config|downloads)\b)"
    r"[^\s'\"<>,;)\]}]*",
    re.IGNORECASE,
)

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
    normalized = path.replace("\\", "/")
    # No "mediaforge" segment (stdlib/site-packages frame, a media file on a
    # NAS, a temp file under the user's profile): at minimum collapse the home
    # directory, otherwise the OS username would still leave the device -- which
    # is exactly what this function exists to prevent.
    normalized = _HOME_DIR_RE.sub("~", normalized)
    return _ROOT_HOME_RE.sub("~", normalized)


def collapse_paths_in_text(text) -> str:
    """Run shorten_path() over every absolute-path-looking token inside *text*.

    shorten_path() only ever saw frame filenames, so an exception message
    ("[Errno 2] No such file or directory: 'C:\\\\Users\\\\bob\\\\Videos\\\\x.mkv'")
    or an ffmpeg error line carried the full local path -- including the
    username -- straight into the payload. This closes that gap for free text;
    the per-token work is still shorten_path(), not a second implementation.

    URLs are stepped over rather than rewritten: a URL path can legitimately
    contain "/home/<something>", and collapsing it produced garbage like
    "https://host~/file.mkv". URLs are already handled by clean_url() /
    strip_url_paths(), which run before this in _clean_text().
    """
    if not text:
        return text
    out = []
    pos = 0
    for m in _URL_RE.finditer(text):
        out.append(_ABS_PATH_IN_TEXT_RE.sub(
            lambda x: shorten_path(x.group(0)), text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_ABS_PATH_IN_TEXT_RE.sub(
        lambda x: shorten_path(x.group(0)), text[pos:]))
    return "".join(out)


def clean_url(url) -> str:
    """Return scheme://host[:port]/path only -- query string, fragment AND any
    embedded credentials are dropped.

    The query string is where streaming hosters put session tokens, which is
    what this originally existed for. The userinfo part matters just as much:
    ``urlsplit().netloc`` keeps ``user:password@`` intact, so building the
    result from netloc handed a NAS/Jellyfin link like
    ``https://user:secret@nas.local/media/x.m3u8?token=…`` back with username
    and password still in it -- and nothing downstream redacts that field.
    Rebuilding from ``hostname`` (+ ``port``) drops the credentials by
    construction rather than by a pattern that has to guess.
    """
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return url
        host = parts.hostname or ""
        if not host:
            return f"{parts.scheme}://"
        if ":" in host:  # IPv6 literal -- urlsplit strips the brackets
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            port = None  # malformed port: drop it rather than echo it back
        netloc = f"{host}:{port}" if port else host
        return f"{parts.scheme}://{netloc}{parts.path}"
    except Exception:
        # Never hand back the raw URL on a parse failure -- that is the one case
        # where it may still carry whatever this function was meant to remove.
        return "[unparsable-url]"


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


# A failed patchright/Playwright launch raises with the ENTIRE Chromium command
# line attached, followed by a "Browser logs:" block. That text carries, in one
# string: every provider domain via --host-resolver-rules (hanime.tv among them,
# i.e. an adult source this project never reports without an explicit consent
# check -- see is_adult_provider() above), the resolved IP of each, the absolute
# browser path and the profile directory. None of it survives redact_urls_in_text(),
# because none of it is a scheme-qualified URL.
#
# It also has no diagnostic value here: playwright/captcha.py's
# _classify_browser_error() already reduces such a failure to a coarse reason code
# ("no_display", "missing_lib:libX.so", "target_closed", ...) and reports THAT
# under detail.captcha. So the blob is dropped rather than partially masked.
_BROWSER_LOG_RE = re.compile(r"\s*Browser logs:.*", re.IGNORECASE | re.DOTALL)
_LAUNCHING_RE = re.compile(r"\s*<launching>.*", re.DOTALL)
# Second line of defence for the same data appearing without either marker.
_RESOLVER_RULES_RE = re.compile(r"--host-resolver-rules=\S*")


def strip_browser_launch_noise(text) -> str:
    """Drop the Chromium launch command / browser log blob from *text*.

    See the comment above for what it contains and why none of it may be sent.
    """
    if not text:
        return text
    text = _BROWSER_LOG_RE.sub(" [browser-log removed]", text)
    text = _LAUNCHING_RE.sub(" [browser-log removed]", text)
    text = _RESOLVER_RULES_RE.sub("--host-resolver-rules=[REDACTED]", text)
    return text


def _clean_text(text) -> str:
    """Strip browser-launch noise, then URL-clean, collapse absolute paths and
    secret-redact (redact_secrets is a superset safety net so it always runs
    last).

    Path collapsing lives here rather than at the individual call sites so it
    covers exception messages and traceback source lines too, not just frame
    filenames -- see collapse_paths_in_text(). All four steps are plain
    non-recursive regex passes, so this stays cheap enough for the per-frame
    hot path.
    """
    text = strip_browser_launch_noise(text or "")
    text = redact_urls_in_text(text)
    text = collapse_paths_in_text(text)
    return redact_secrets(text)


def _hash_token(value) -> str:
    """Short, stable, non-reversible tag for a dropped value, so two reports
    about the same URL are still recognizable as the same one without the
    value itself ever being transmitted."""
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:8]


def strip_url_paths(text) -> str:
    """Reduce every URL in *text* to ``scheme://host/[path:<hash>]``.

    redact_urls_in_text()/clean_url() keep the URL path, which is fine for a
    traceback frame but not for a log line: this codebase logs content-
    identifying watch URLs directly (the download watchdog logs the episode
    URL), and the path is the part that says *what the user is watching*. In
    the crash channel -- stage 1, enabled on its own by users who never
    consented to any watch data -- the path therefore goes away entirely and
    only a hash is kept for correlating repeat reports.
    """
    if not text:
        return text

    def _replace(match):
        url = match.group(0)
        cleaned = clean_url(url)
        try:
            parts = urlsplit(cleaned)
        except Exception:
            return "[unparsable-url]"
        if not parts.scheme or not parts.netloc:
            return cleaned
        path = (parts.path or "").strip("/")
        if not path:
            return f"{parts.scheme}://{parts.netloc}"
        return f"{parts.scheme}://{parts.netloc}/[path:{_hash_token(path)}]"

    return _URL_RE.sub(_replace, text)


# Word-ish token inside free text -- hostnames included, since "." is part of
# the class. Used only to feed candidate tokens to is_adult_provider().
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def mentions_adult_provider(text) -> bool:
    """True if *text* names the age-gated provider -- by provider id
    (``hanime_tv``) or by host (``hanime.tv``, ``www.hanime.tv``).

    The is_adult_provider() guard is only reachable from the event builders in
    events.py, which all receive an explicit provider argument. The crash
    channel has no provider argument at all: it gets a free-text log message
    that may contain the watch URL. This gives that path a way to ask the SAME
    guard the same question -- it normalizes candidate tokens to the provider
    id shape and defers to is_adult_provider(); it is not a second list.
    """
    if not text:
        return False
    for token in _TOKEN_RE.findall(text):
        candidates = [token]
        labels = [p for p in token.split(".") if p]
        if len(labels) >= 2:
            candidates.append(".".join(labels[-2:]))  # host -> registrable part
        for candidate in candidates:
            if is_adult_provider(_NON_ALNUM_RE.sub("_", candidate.lower()).strip("_")):
                return True
    return False


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
