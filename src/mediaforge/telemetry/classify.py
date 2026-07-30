"""Classification helpers that decide what is NOT worth reporting.

Two independent questions live here, both answered without importing
anything from this package or from ``mediaforge.web`` -- this module has to
stay a dependency-free leaf so ``web/devinfos_monitor.py`` and the telemetry
event builders can both use it without an import cycle:

  1. ``is_user_cancellation()`` -- was this exception/message the direct
     result of the USER stopping something (cancelling a queue item, closing
     the captcha window, hitting Ctrl+C, navigating away from an SSE
     stream)? Per TELEMETRY_PLAN.md the crash/error channels exist for real
     defects only. A user pressing "Cancel" is the app working correctly;
     reporting it buries actual bugs in noise and inflates every install's
     crash count.

  2. ``is_server_unreachable()`` -- is this exception just "the devInfo
     server is not answering right now"? Maintenance windows, a filtered
     DNS, no internet at all: all completely routine for an optional,
     entirely non-essential side channel. Those must never produce console
     output, so the callers that talk to the devInfo server log them at
     DEBUG instead of WARNING/ERROR.

Both functions are deliberately generous in what they match: a false
positive here means one report is not sent (harmless), a false negative
means noise reaches the user's console or an admin's crash list (the exact
thing this module exists to prevent).
"""

import re

# ---------------------------------------------------------------------------
# 1. User-initiated cancellation
# ---------------------------------------------------------------------------

# Exception CLASS NAMES (not classes -- matching by name avoids importing
# yt_dlp/playwright/asyncio here just to have something to compare against,
# and keeps working when a dependency is absent or renames its module path).
_CANCEL_EXCEPTION_NAMES = frozenset({
    "CaptchaCancelled",        # playwright/captcha.py -- user cancelled during a solve
    "DownloadCancelled",       # yt_dlp.utils.DownloadCancelled
    "CancelledError",          # asyncio / concurrent.futures
    "KeyboardInterrupt",       # Ctrl+C in CLI mode
    "SystemExit",              # a clean shutdown, never a defect
    "GeneratorExit",           # a closed generator, i.e. a torn-down response
})

# Deliberately NOT in the set above: BrokenPipeError, ConnectionResetError and
# ConnectionAbortedError. They DO mean "the client went away" when they happen
# while writing a response -- but they are also the single most common way an
# ordinary network failure surfaces (a hoster dropping a download mid-stream,
# the devInfo server closing a connection). Treating them as cancellations
# globally silently swallowed real download failures: queue_worker.py logs those
# from inside the except block, and the log handler reads a live sys.exc_info()
# there, so the connection error itself decided the report's fate. They are
# therefore only recognized where the "client went away" reading is the correct
# one -- see is_client_disconnect() below, used by the SSE/streaming paths.
_CLIENT_DISCONNECT_EXCEPTION_NAMES = frozenset({
    "GeneratorExit",           # the response generator was closed
    "BrokenPipeError",         # client closed an in-flight response
    "ConnectionResetError",    # browser tab closed mid-stream
    "ConnectionAbortedError",  # same, Windows wording
    "ClientDisconnected",      # werkzeug.exceptions.ClientDisconnected
})

# Message fragments that mark a cancellation regardless of exception type --
# this codebase deliberately re-raises cancels as a plain RuntimeError with a
# fixed message (see playwright/captcha.py's CaptchaCancelled docstring and
# models/common/common.py), so type alone is not enough.
# Deliberately SPECIFIC phrases, never a bare word. An earlier version matched
# \bcancell?ed\b / \babgebrochen\b anywhere in the text, and these functions are
# handed the fully formatted log line -- which in this codebase embeds up to 4 KB
# of raw ffmpeg stderr (models/common/common.py), hoster error text and the
# episode URL. A show whose slug contains "cancelled", or an ffmpeg message like
# "muxing was cancelled by the demuxer", then silently suppressed a real crash
# report. Each pattern below names an action plus the cancellation, so foreign
# text quoted inside a log line cannot trigger it by accident.
_CANCEL_MESSAGE_PATTERNS = (
    re.compile(r"\bdownloads?\s+cancell?ed\b", re.IGNORECASE),
    re.compile(r"\b(cancell?ed|aborted|stopped)\s+by\s+(the\s+)?user\b", re.IGNORECASE),
    re.compile(r"\b(vom|durch\s+den)\s+(nutzer|benutzer|anwender)\s+abgebrochen\b", re.IGNORECASE),
    re.compile(r"\b(encoding|transcoding|upscaling|scan|upload|job|queue\s+item)\s+cancell?ed\b",
               re.IGNORECASE),
    re.compile(r"\bcancell?ed\s+by\s+request\b", re.IGNORECASE),
    re.compile(r"\boperation\s+was\s+cancell?ed\b", re.IGNORECASE),
)

# Status values a feature-detail event may carry that mean "the user stopped
# it", checked by events.build_feature_detail_event().
CANCEL_STATUSES = frozenset({"cancelled", "canceled", "aborted", "user_cancelled"})


def _exception_chain_names(exc_value, follow_context=False):
    """Walk an exception's chain and yield every class name in it.

    Only ``__cause__`` is followed by default -- i.e. explicit
    ``raise X from Y``, where the author stated the relationship. ``__context__``
    is set implicitly by Python for ANY exception raised inside ANY except
    block, so following it means "an unrelated error that happened while
    handling a connection reset" inherits that connection reset's
    classification. That is how a genuine hoster failure ended up classified as
    a user cancellation. ``follow_context=True`` is available for the
    unreachable-server check, where the implicit relationship is the useful one
    (a niquests wrapper re-raising an underlying socket error).
    """
    seen = set()
    current = exc_value
    depth = 0
    while current is not None and depth < 10:  # bounded: chains can be cyclic
        if id(current) in seen:
            break
        seen.add(id(current))
        yield type(current).__name__
        nxt = getattr(current, "__cause__", None)
        if nxt is None and follow_context:
            nxt = getattr(current, "__context__", None)
        current = nxt
        depth += 1


def is_user_cancellation(exc_type=None, exc_value=None, message=None) -> bool:
    """True when this error was caused by the user stopping something.

    Accepts any subset of (exc_type, exc_value, message) so the same check
    serves the excepthook paths (full triple available), the logging handler
    (a formatted message, sometimes without any exception at all) and the
    event builders (a status string / error message only).

    Fully guarded: a weird exception object whose ``str()`` raises must not
    turn "should this be reported" into a second failure -- in that case the
    answer is simply False and the event is built as usual.
    """
    try:
        if exc_type is not None and getattr(exc_type, "__name__", "") in _CANCEL_EXCEPTION_NAMES:
            return True
        if exc_value is not None:
            for name in _exception_chain_names(exc_value):
                if name in _CANCEL_EXCEPTION_NAMES:
                    return True
        texts = []
        if exc_value is not None:
            texts.append(str(exc_value))
        if message:
            texts.append(str(message))
        for text in texts:
            for pattern in _CANCEL_MESSAGE_PATTERNS:
                if pattern.search(text):
                    return True
    except Exception:
        return False
    return False


def is_client_disconnect(exc_type=None, exc_value=None) -> bool:
    """True when a response/stream ended because the CLIENT went away.

    Separate from is_user_cancellation() on purpose: inside a streaming
    response (the SyncPlay SSE generator, a proxied video stream) a reset
    connection means the user closed the tab and there is nothing to report.
    Anywhere else the exact same exception usually means a network failure
    that IS worth reporting, so this check must be applied by the streaming
    code that knows the context -- never globally.
    """
    try:
        if exc_type is not None and getattr(exc_type, "__name__", "") in _CLIENT_DISCONNECT_EXCEPTION_NAMES:
            return True
        if exc_value is not None:
            for name in _exception_chain_names(exc_value, follow_context=True):
                if name in _CLIENT_DISCONNECT_EXCEPTION_NAMES:
                    return True
    except Exception:
        return False
    return False


def is_cancel_exception_name(name) -> bool:
    """True for a bare exception CLASS NAME that means "the user stopped it".

    Call sites frequently record only ``type(e).__name__`` in an event's
    metadata rather than the exception itself. The message patterns above
    cannot help there: "CaptchaCancelled" has no word boundary in front of
    "Cancelled", so a substring/word match either misses it or would have to
    be loose enough to also match unrelated words. Matching the known names
    exactly is both precise and cheap.
    """
    return str(name or "").strip() in _CANCEL_EXCEPTION_NAMES


def is_cancel_status(status) -> bool:
    """True for a feature-detail ``status`` value that means "user stopped it"."""
    return str(status or "").strip().lower() in CANCEL_STATUSES


# ---------------------------------------------------------------------------
# 2. devInfo server simply not reachable
# ---------------------------------------------------------------------------

# Again by class name: niquests/urllib3/socket all raise their own types and
# importing every one of them here would defeat the point of a leaf module.
# Transport-level failures only: the request never got an answer. Deliberately
# EXCLUDED are HTTPError / SSLError / TooManyRedirects -- those mean the server
# (or something claiming to be it) DID answer, and answered wrongly. A 401 from
# a project-key mismatch and an invalid TLS certificate are exactly the kind of
# misconfiguration that has to stay visible; classifying them as "server is
# offline" is how such a problem goes unnoticed for weeks.
_UNREACHABLE_EXCEPTION_NAMES = frozenset({
    "ConnectionError", "ConnectTimeout", "ConnectTimeoutError", "ConnectionRefusedError",
    "ReadTimeout", "ReadTimeoutError", "Timeout", "TimeoutError", "socket.timeout",
    "NewConnectionError", "MaxRetryError", "ProtocolError", "ProxyError",
    "NameResolutionError", "DNSError", "gaierror", "herror",
    "RemoteDisconnected", "IncompleteRead", "ChunkedEncodingError",
    "ConnectionClosed", "ConnectionResetError", "ConnectionAbortedError",
})

# Same principle as the cancellation patterns: specific enough that they cannot
# fire on quoted foreign text. In particular there is no bare "\b50[0-4]\b"
# here -- that matched the column number in a JSON parse error
# ("Expecting ',' delimiter: line 1 column 502"), turning a corrupt response
# from the server into "server unreachable" and hiding it at DEBUG level.
_UNREACHABLE_MESSAGE_PATTERNS = (
    re.compile(r"name or service not known", re.IGNORECASE),
    re.compile(r"temporary failure in name resolution", re.IGNORECASE),
    re.compile(r"(failed|unable) to resolve", re.IGNORECASE),
    re.compile(r"connection (refused|reset|aborted|timed out)", re.IGNORECASE),
    re.compile(r"(connect|read|request)\s*timed?\s*out", re.IGNORECASE),
    re.compile(r"^timed?\s*out$", re.IGNORECASE),
    re.compile(r"network is unreachable", re.IGNORECASE),
    re.compile(r"no route to host", re.IGNORECASE),
    re.compile(r"max retries exceeded", re.IGNORECASE),
    # Free-text gateway wording only, never a bare status number: a reverse
    # proxy serving a maintenance page is exactly the "server is not answering
    # right now" case. A 401/403/404 does NOT match any of these and therefore
    # stays visible at WARNING -- a project-key mismatch in the devInfo server's
    # own .env is a real misconfiguration, not a maintenance window.
    re.compile(r"bad gateway", re.IGNORECASE),
    re.compile(r"service (temporarily )?unavailable", re.IGNORECASE),
    re.compile(r"gateway time-?out", re.IGNORECASE),
)


def is_server_unreachable(exc) -> bool:
    """True when *exc* is the ordinary "remote side is not answering" case.

    Callers use this to decide the LOG LEVEL only -- never whether to retry.
    The devInfo server is optional infrastructure (changelog feed + opt-in
    telemetry); it being down during maintenance is expected and must stay
    invisible in the user's terminal.
    """
    try:
        for name in _exception_chain_names(exc, follow_context=True):
            if name in _UNREACHABLE_EXCEPTION_NAMES:
                return True
        text = str(exc)
        for pattern in _UNREACHABLE_MESSAGE_PATTERNS:
            if pattern.search(text):
                return True
    except Exception:
        return False
    return False
