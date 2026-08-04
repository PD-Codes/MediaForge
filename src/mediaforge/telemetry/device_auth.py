"""Per-install device secret and HMAC request signing -- the client half.

The devInfo server used to authenticate telemetry calls with nothing but
``X-Project-Key``, a literal compiled into this open-source client. It is
public by construction, and knowing it plus an ``install_id`` (which is not a
secret either -- it is printed on the Settings page and pasted into support
tickets) was enough to post events as somebody else, to trigger the deletion
of their data, or to fetch their export. The fix, on both sides, is a
per-install secret handed out once at first check-in and used to sign every
later request.

This module owns exactly three things: storing that secret, obtaining one
(enrollment), and turning ``(method, path, body)`` into the five signature
headers. It knows nothing about *which* requests get signed or what happens
when signing fails -- that policy lives at the call sites (``client.py`` for
ingest, ``web/routes/settings.py`` for the data-request endpoints), for the
same reason the server splits its crypto module from its route module.

The protocol, which is the server's ``telemetry_device_auth.canonical_request``
and must match it byte for byte:

    mf-telemetry-sig-v1\\n<METHOD>\\n<path>\\n<install_id>\\n<timestamp>\\n<nonce>\\n<key_version>\\n<sha256hex(body)>

signed as ``hex(HMAC-SHA256(secret_utf8, canonical))``. Three details are easy
to get subtly wrong and each would present as "every signature is invalid",
the least diagnosable failure this feature has:

  * **The key is the secret's raw UTF-8 bytes.** The secret *looks* like
    base64 (43 url-safe characters), and decoding it before keying the HMAC is
    the obvious mistake. The server does ``str(secret).encode("utf-8")``; so
    do we.
  * **``<path>`` carries no query string.** The server signs Flask's
    ``request.path``, which stops at the ``?``. The install_id on the status
    GET therefore travels as a query parameter that is *not* covered by the
    signature -- acceptable, because a verified signature is the identity the
    server uses anyway, and it is what the server actually verifies.
  * **The body is hashed as the raw bytes that go on the wire**, never as a
    re-serialisation of the parsed JSON. So the call sites serialise once,
    sign those bytes and post those same bytes (``data=`` and not ``json=``).

Everything here is best-effort. Telemetry that cannot enroll or cannot sign
must degrade to "this batch is dropped", never to an exception that reaches a
download worker.
"""

import hashlib
import hmac
import json
import platform
import secrets
import threading
import time
from urllib.parse import urlsplit

from ..logger import get_logger
from ..web.db import get_setting, set_setting
from . import settings as tel_settings
from .registry import TELEMETRY_PROJECT_KEY, TELEMETRY_REGISTER_URL

logger = get_logger(__name__)

# --- protocol constants, mirrored from the server's telemetry_device_auth.py ---

#: Domain-separation tag, first line of every canonical string.
CANONICAL_VERSION = "mf-telemetry-sig-v1"

HEADER_INSTALL_ID = "X-Install-Id"
HEADER_SIGNATURE = "X-Install-Signature"
HEADER_TIMESTAMP = "X-Install-Timestamp"
HEADER_NONCE = "X-Install-Nonce"
HEADER_KEY_VERSION = "X-Install-Key-Version"

#: The server bounds the nonce at 8..64 printable-ASCII characters (it pays for
#: each one in its replay cache). token_urlsafe(16) is 22 characters of
#: url-safe base64 -- comfortably inside both ends and 128 bits of entropy,
#: which is what actually matters: a repeated nonce inside the skew window is a
#: request the server refuses as a replay.
NONCE_BYTES = 16

# --- storage keys (app_settings, via web/db.py) ---

#: The device secret. Listed in db.SENSITIVE_KEYS, so set_setting() stores it
#: encrypted like every other secret this app holds.
SECRET_KEY_NAME = "telemetry_device_secret"
KEY_VERSION_KEY_NAME = "telemetry_device_key_version"

#: How long to wait after a failed enrollment before trying again. The server
#: rate-limits /telemetry/register per IP, and an install that cannot enroll
#: (server down, project key rejected, registration closed) would otherwise
#: retry on every single batch flush -- once every few seconds, forever. The
#: delay doubles per consecutive failure up to the ceiling, so a permanently
#: refusing server costs one request an hour rather than thousands a day.
_RETRY_BASE_SECONDS = 300.0
_RETRY_MAX_SECONDS = 3600.0

_REGISTER_TIMEOUT = 8  # seconds -- never let a hung devInfo server stall a caller

# One lock for the whole module: enrollment is rare, and a lock per concern
# would only create an ordering question nobody would remember to answer. It is
# held across the registration HTTP call on purpose -- that is precisely the
# window in which a second thread must not start its own registration, since
# two concurrent POSTs would rotate the secret twice and leave one of them
# holding a key_version the server has already superseded.
_lock = threading.Lock()

#: Cached (secret, key_version). ``None`` means "not read from the DB yet";
#: ``(None, None)`` means "read, and there is none stored".
_cached = None

_next_attempt = 0.0      # time.monotonic() before which enrollment is not retried
_failures = 0            # consecutive enrollment failures, for the backoff


# ---------------------------------------------------------------------------
# Stored credential
# ---------------------------------------------------------------------------

def _load_locked():
    """Read the stored credential into the cache. Caller holds ``_lock``."""
    global _cached
    if _cached is None:
        secret = get_setting(SECRET_KEY_NAME) or None
        try:
            version = int(get_setting(KEY_VERSION_KEY_NAME) or 1)
        except (TypeError, ValueError):
            # A corrupt row must not make telemetry raise. Version 1 is the
            # value a first registration issues, so it is the right guess; if
            # it is wrong the server answers 401 and the ingest path
            # re-registers, which is the recovery this whole scheme has anyway.
            version = 1
        _cached = (secret, version) if secret else (None, None)
    return _cached


def get_credential():
    """``(secret, key_version)`` for this install, or ``(None, None)``."""
    with _lock:
        return _load_locked()


def has_secret() -> bool:
    return get_credential()[0] is not None


def _store_locked(secret, key_version):
    """Persist a freshly issued credential. Caller holds ``_lock``."""
    global _cached
    set_setting(SECRET_KEY_NAME, secret)
    set_setting(KEY_VERSION_KEY_NAME, str(int(key_version or 1)))
    _cached = (secret, int(key_version or 1))


def clear():
    """Forget the stored device secret.

    Called when the identity it belongs to goes away -- regenerating the
    install_id, or withdrawing consent. Keeping a secret whose install_id no
    longer exists is worse than having none: every signed request would be
    refused, and the client would read that as "the server is broken" rather
    than "re-enroll".
    """
    global _cached, _next_attempt, _failures
    with _lock:
        try:
            set_setting(SECRET_KEY_NAME, "")
            set_setting(KEY_VERSION_KEY_NAME, "")
        except Exception:
            logger.debug("[Telemetry] could not clear the device secret", exc_info=True)
        _cached = (None, None)
        # A deliberate reset also clears the backoff: the user just did
        # something that should take effect now, not in an hour.
        _next_attempt = 0.0
        _failures = 0


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def canonical_request(method, path, install_id, timestamp, nonce, key_version, body):
    """The exact bytes a signature is computed over. Mirrors the server."""
    body_digest = hashlib.sha256(body or b"").hexdigest()
    parts = [
        CANONICAL_VERSION,
        str(method or "").upper(),
        str(path or ""),
        str(install_id or ""),
        str(timestamp or ""),
        str(nonce or ""),
        str(key_version if key_version is not None else ""),
        body_digest,
    ]
    return "\n".join(parts).encode("utf-8")


def sign_request(secret, method, path, install_id, timestamp, nonce, key_version, body):
    """Hex HMAC-SHA256 of ``canonical_request`` keyed with the secret's UTF-8
    bytes -- not its base64 decoding, see the module docstring."""
    message = canonical_request(method, path, install_id, timestamp, nonce,
                                key_version, body)
    return hmac.new(str(secret).encode("utf-8"), message, hashlib.sha256).hexdigest()


def request_path(url: str) -> str:
    """The ``<path>`` component of a URL, query string excluded -- what the
    server signs (Flask's ``request.path``)."""
    return urlsplit(url).path or "/"


def sign_headers(method, path, body_bytes, secret=None, key_version=None,
                 install_id=None):
    """The five signature headers for one request, or ``{}`` when this install
    has no device secret yet.

    ``path`` may be a full URL or just a path; a query string is stripped
    either way, because the server does not sign one. A fresh nonce and a
    fresh unix timestamp are generated per call -- reusing either across two
    requests is a replay as far as the server is concerned.
    """
    if secret is None or key_version is None:
        secret, key_version = get_credential()
    if not secret:
        return {}
    if install_id is None:
        install_id = tel_settings.get_install_id()
    if "://" in str(path) or "?" in str(path):
        path = request_path(str(path))
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(NONCE_BYTES)
    signature = sign_request(secret, method, path, install_id, timestamp, nonce,
                             key_version, body_bytes or b"")
    return {
        HEADER_INSTALL_ID: str(install_id),
        HEADER_SIGNATURE: signature,
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
        HEADER_KEY_VERSION: str(int(key_version or 1)),
    }


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

def _registration_payload(install_id):
    """The envelope POST /telemetry/register needs.

    The server runs the same ``_get_or_create_install`` it runs on ingest, and
    that refuses an envelope missing app_version / os / python_version / arch.
    So registration carries the same four fields the ingest batch does, from
    the same sources -- a registration that is refused for an incomplete
    envelope would look, from here, exactly like a server outage.
    """
    from ..config import VERSION
    return {
        "install_id": install_id,
        "app_version": VERSION or "unknown",
        "os": platform.system(),
        "python_version": platform.python_version(),
        "arch": platform.machine(),
    }


def _register_locked(rotate=False):
    """Do the actual POST /telemetry/register. Caller holds ``_lock``.

    Returns True when a secret was issued and stored. Never raises.
    """
    global _next_attempt, _failures
    from ..config import GLOBAL_SESSION

    install_id = tel_settings.get_install_id()
    body = json.dumps(_registration_payload(install_id)).encode("utf-8")
    headers = {
        "X-Project-Key": TELEMETRY_PROJECT_KEY,
        "Content-Type": "application/json",
    }
    if rotate:
        # Rotating an existing secret is only permitted to somebody who can
        # prove possession of the current one -- otherwise this endpoint would
        # itself be the takeover primitive the whole scheme exists to close.
        current_secret, current_version = _load_locked()
        if current_secret:
            headers.update(sign_headers(
                "POST", request_path(TELEMETRY_REGISTER_URL), body,
                secret=current_secret, key_version=current_version,
                install_id=install_id))

    try:
        resp = GLOBAL_SESSION.post(TELEMETRY_REGISTER_URL, data=body,
                                   headers=headers, timeout=_REGISTER_TIMEOUT)
        status = getattr(resp, "status_code", None)
        if status != 201:
            logger.debug("[Telemetry] device registration refused with HTTP %s", status)
            _note_failure_locked()
            return False
        data = resp.json() or {}
        secret = data.get("secret")
        if not secret or not isinstance(secret, str):
            logger.debug("[Telemetry] device registration returned no secret")
            _note_failure_locked()
            return False
        _store_locked(secret, data.get("key_version") or 1)
    except Exception as e:
        # An unreachable devInfo server is routine and entirely harmless here.
        logger.debug("[Telemetry] device registration failed: %s", e)
        _note_failure_locked()
        return False

    _next_attempt = 0.0
    _failures = 0
    logger.debug("[Telemetry] device secret enrolled (key version %s)",
                 _cached[1] if _cached else "?")
    return True


def _note_failure_locked():
    """Arm the backoff after a failed enrollment. Caller holds ``_lock``."""
    global _next_attempt, _failures
    _failures = min(_failures + 1, 8)
    delay = min(_RETRY_BASE_SECONDS * (2 ** (_failures - 1)), _RETRY_MAX_SECONDS)
    _next_attempt = time.monotonic() + delay


def ensure_enrolled() -> bool:
    """True when a device secret is available for signing.

    Registers on first call if none is stored. Safe to call from several
    threads (the lock serialises the whole read-decide-register sequence, so a
    second caller sees the first one's result instead of starting a competing
    rotation) and safe to call on every flush: after a failure it refuses to
    try again until the backoff expires and simply reports False.

    **Consent is not checked here.** That is deliberate and the gate is not
    missing -- it lives at the call sites, which are the places that know
    whether they are about to send anything at all. ``client.py`` only reaches
    this after ``telemetry_active()``, and the Settings routes only after the
    user pressed a button that submits data. Re-checking here would duplicate
    a policy decision in a module whose job is cryptography.
    """
    with _lock:
        secret, _version = _load_locked()
        if secret:
            return True
        if _next_attempt and time.monotonic() < _next_attempt:
            return False
        return _register_locked(rotate=False)


def needs_registration(resp) -> bool:
    """Whether a 401 response is the server saying "(re-)register".

    The server distinguishes a bad project key ("invalid or missing project
    key"), a bad signature ("signature could not be verified") and an install
    that is enrolled but sent an unsigned/stale request -- only the last one
    is fixable from here, and only its message names ``/telemetry/register``.
    Matching on that substring rather than on the bare status keeps a wrong
    project key from turning into a registration loop.
    """
    try:
        if getattr(resp, "status_code", None) != 401:
            return False
        body = resp.json()
        message = str(body.get("error", "")) if isinstance(body, dict) else ""
    except Exception:
        try:
            message = str(getattr(resp, "text", "") or "")
        except Exception:
            return False
    return "/telemetry/register" in message


def rotate() -> bool:
    """Ask the server for a new secret, proving possession of the current one.

    Used as the recovery path when the server answers 401 "this install is
    registered for signed requests": either our stored secret is stale or the
    server has none for this install. Both are fixed by re-registering, and
    when we hold a secret the request is signed so the server will accept it
    as a rotation rather than refusing it as a takeover attempt.
    """
    with _lock:
        secret, _version = _load_locked()
        return _register_locked(rotate=bool(secret))
