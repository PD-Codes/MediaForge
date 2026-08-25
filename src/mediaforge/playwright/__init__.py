"""Public exports for the ``mediaforge.playwright`` package.

Browser-automation helpers (via patchright, a Playwright fork) for solving
Cloudflare Turnstile / CAPTCHA challenges encountered while scraping
streaming sites. See ``captcha.py`` for the implementation.

``captcha.py`` is a separately licensed, optional component (see
LICENSE-CAPTCHA in the repository root); modified distributions must ship
without it. When the file is absent, this package registers a lightweight
fallback module under the same import path so every existing
``from mediaforge.playwright.captcha import ...`` call site keeps working:
bookkeeping state (thread-locals, cancel events, session registry) becomes
inert, detection helpers report "no captcha", and the actual solver entry
points raise a clear error instead of an opaque ImportError.
"""

import importlib.util as _importlib_util

# Explicit existence check instead of a blind try/except ImportError: if
# captcha.py exists but fails to import (e.g. a missing dependency inside
# it), that error must propagate instead of silently activating the stub.
if _importlib_util.find_spec(__name__ + ".captcha") is not None:
    from .captcha import playwright_get_page_url
else:
    import logging
    import sys
    import threading
    import types

    logging.getLogger(__name__).warning(
        "Captcha solver (playwright/captcha.py) not present - captcha "
        "solving is disabled; captcha-protected pages will fail."
    )

    _UNAVAILABLE_MSG = (
        "Captcha solving is unavailable in this MediaForge distribution "
        "(the separately licensed playwright/captcha.py component is not "
        "included - see LICENSE-CAPTCHA)."
    )

    def _unavailable(*_args, **_kwargs):
        """Solver entry point stub: raises a clear error instead of ImportError."""
        raise RuntimeError(_UNAVAILABLE_MSG)

    def _no_captcha_detected(*_args, **_kwargs):
        """Detection/interaction stub: reports that no captcha is present."""
        return False

    def _solve_sto_modal_stub(*_args, **_kwargs):
        """s.to modal stub: callers fall back to the plain response URL."""
        logging.getLogger(__name__).warning(
            "Cannot resolve the s.to provider modal without the captcha "
            "solver component - falling back to the plain response URL."
        )
        return None

    _stub = types.ModuleType(__name__ + ".captcha")
    _stub.__doc__ = "Fallback for the absent, separately licensed captcha solver."

    # Solver entry points - raise a clear error when actually invoked.
    _stub.solve_captcha = _unavailable
    _stub.playwright_get_page_url = _unavailable
    _stub._launch_browser_context = _unavailable

    # Detection / interaction helpers - benign no-ops.
    _stub.is_captcha_page = _no_captcha_detected
    _stub._click_turnstile = _no_captcha_detected
    _stub._is_turnstile_token_ready = _no_captcha_detected
    _stub._require_display = lambda *a, **k: None
    _stub.solve_sto_modal = _solve_sto_modal_stub

    # Shared bookkeeping surface used by the queue worker, web routes and
    # app startup (see queue_worker.py, routes/captcha.py, app.py).
    _stub._local = threading.local()
    _stub._active_sessions = {}
    _stub._active_sessions_lock = threading.Lock()
    _stub._on_captcha_start = None
    _stub._on_captcha_end = None
    _stub.set_cancel_event = lambda *a, **k: None
    _stub.clear_cancel_event = lambda *a, **k: None

    sys.modules[_stub.__name__] = _stub
    captcha = _stub
    playwright_get_page_url = _stub.playwright_get_page_url

__all__ = ["playwright_get_page_url"]
