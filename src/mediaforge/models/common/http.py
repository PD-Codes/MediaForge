"""HTTP helpers for scraper models.

Thin wrappers over the project's shared HTTP session (see config._SessionProxy).
Provider models call :func:`get_html`/:func:`get_session` to fetch pages instead
of each re-implementing fetching.

These live natively in MediaForge so the burning-series / kinox / cineby /
mangafire models no longer fall back to the upstream ``aniworld`` package for
them — ``aniworld`` is only ever a site/provider, never a code dependency.

Failover note: :func:`get_html` deliberately talks to the RAW session, not the
mirror-aware GLOBAL_SESSION proxy. Its callers (burning-series' ``_DOMAINS``
loop, kinox's ``_kinox_get_html``) already walk a site's alternative domains
themselves, so routing through the proxy would stack a SECOND failover on top —
one slow/blocked host would then be retried across every mirror for every host
the caller already tries (e.g. burning-series 6×6 = 36 requests per page),
which is what made the browse lists hang. :func:`get_session` still returns the
mirror-aware session for callers (mangafire, cineby) that don't do their own
failover.
"""

from __future__ import annotations

from ...config import GLOBAL_SESSION

# Bounded default (connect, read) so a single fetch can't hang on the session's
# longer global default when a caller doesn't pass its own timeout.
_DEFAULT_GET_HTML_TIMEOUT = (8, 15)


def get_session():
    """Return the project's shared, mirror-aware HTTP session (GLOBAL_SESSION).

    Exposes ``.get()``/``.post()``/``.request()``; a request to a known mirror
    site is transparently retried across that site's alternative domains
    (see mirrors.py), while non-site URLs pass straight through.
    """
    return GLOBAL_SESSION


def _raw_session():
    """The underlying niquests session WITHOUT the mirror failover wrapper.

    Same configured session (DoH resolver, TLS, default headers) as
    GLOBAL_SESSION, just without mirrors.request_with_failover rewriting the
    host — so each get_html() call is a single bounded request and the caller's
    own domain loop stays in charge of trying alternates. Falls back to the
    proxy if the private accessor ever changes.
    """
    getter = getattr(GLOBAL_SESSION, "_get_session", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            pass
    return GLOBAL_SESSION


def get_html(url, headers=None, timeout=None, check_captcha=True,
             allow_redirects=True, **kwargs):
    """GET *url* (single request, no mirror failover) and return response text.

    Raises for HTTP error statuses (4xx/5xx) so a failed fetch surfaces as an
    exception, matching the previous behaviour callers rely on. A default
    ``Accept-Encoding: gzip, deflate`` is sent unless the caller overrides it,
    so a Brotli-only body (which the session can't always decode) doesn't come
    back as garbage. A bounded default timeout is applied when the caller passes
    none, so a slow/blocked host fails fast instead of hanging.

    ``check_captcha`` is accepted for call-site compatibility only; MediaForge's
    per-site models (e.g. kinox) do their own captcha handling, so it is a no-op
    here.
    """
    req_headers = {"Accept-Encoding": "gzip, deflate"}
    if headers:
        req_headers.update(headers)

    call_kwargs = dict(headers=req_headers, allow_redirects=allow_redirects, **kwargs)
    call_kwargs["timeout"] = timeout if timeout is not None else _DEFAULT_GET_HTML_TIMEOUT

    resp = _raw_session().get(url, **call_kwargs)
    resp.raise_for_status()
    return resp.text
