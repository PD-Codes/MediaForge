"""Public exports for the ``mediaforge.playwright`` package.

Browser-automation helpers (via patchright, a Playwright fork) for solving
Cloudflare Turnstile / CAPTCHA challenges encountered while scraping
streaming sites. See ``captcha.py`` for the implementation.
"""

from .captcha import playwright_get_page_url

__all__ = ["playwright_get_page_url"]
