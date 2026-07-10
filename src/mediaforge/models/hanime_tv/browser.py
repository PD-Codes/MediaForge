"""Headless-browser access layer for hanime.tv (post-Astro rewrite).

hanime signs every /api/v8 request (handshake + per-request signature) and the
player only fetches the HLS stream after the poster is clicked, so plain HTTP
can't reach the data.  We therefore drive a real browser (patchright /
Playwright, as VeeV does) and read what the page itself exposes:

  * metadata  – the ``application/ld+json`` block + DOM (title, poster,
                description, year, tags, censored, franchise episode links).
                No signed API call needed.
  * stream    – click the play overlay so the player loads the signed
                ``…highwinds-cdn.com/….m3u8`` and intercept that request.

All best-effort: if patchright is missing or the page changes, callers degrade
to empty results instead of crashing.
"""
try:
    from ...config import HANIME_BASE_URL, logger
except ImportError:  # pragma: no cover
    from mediaforge.config import HANIME_BASE_URL, logger

_BASE = HANIME_BASE_URL.rstrip("/")
_NAV_TIMEOUT = 45_000
_SETTLE_MS = 1_500


def _sync_playwright():
    try:
        from patchright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright
            return sync_playwright
        except ImportError:
            return None


def _new_page(p):
    """Open a hardened page for hanime.tv.

    hanime.tv now fronts its player with Cloudflare Turnstile
    (``window.AppConfig.turnstile_public_site_key``), so a bare, ephemeral,
    headless context gets flagged and the signed .m3u8 request never fires.
    Reuse the same hardened launch path every other Cloudflare-protected
    provider in this codebase already relies on (see
    ``mediaforge.playwright.captcha._launch_browser_context``): persistent
    profile (warm cf_clearance), stealth launch args/context kwargs and
    fingerprint hardening (overlay/WebGL defences via ``_install_stealth``).

    Turnstile requires a real (non-headless) renderer, so this — like the
    rest of the captcha infrastructure — runs headed and, on Linux, relies on
    ``autodeps._ensure_xvfb()`` to provide a virtual display when none is set.
    """
    try:
        from ...autodeps import _ensure_xvfb
        _ensure_xvfb()
    except Exception:
        pass
    try:
        from ...playwright.captcha import _launch_browser_context
    except ImportError:
        from mediaforge.playwright.captcha import _launch_browser_context

    handle = _launch_browser_context(p, offscreen=True)
    context = handle.context
    page = context.new_page()
    return handle, context, page


def _best_stream(detail):
    """Highest-resolution HLS URL from a raw API manifest (fallback path)."""
    manifest = (detail or {}).get("videos_manifest") or {}
    best_url, best_h = "", -1
    for server in manifest.get("servers") or []:
        for st in server.get("streams") or []:
            url = st.get("url") or ""
            if not url:
                continue
            try:
                h = int(st.get("height") or 0)
            except (TypeError, ValueError):
                h = 0
            if h > best_h:
                best_h, best_url = h, url
    return best_url or None


# JS run inside the loaded page to harvest everything the DOM/ld+json exposes.
_EXTRACT_JS = r"""
() => {
  const out = { title:'', description:'', poster_url:'', year:'', censored:'',
                genres:[], episodes:[] };
  try {
    const ld = document.querySelector('script[type="application/ld+json"]');
    if (ld) {
      const j = JSON.parse(ld.textContent);
      out.title = j.name || '';
      out.description = j.description || '';
      out.poster_url = j.thumbnailUrl || '';
      out.year = (j.uploadDate || '').slice(0, 4);
    }
  } catch (e) {}
  const seen = new Set();
  document.querySelectorAll('a[href*="/videos/hentai/"]').forEach(a => {
    const m = (a.getAttribute('href') || '').match(/\/videos\/hentai\/([a-zA-Z0-9._-]+)/);
    if (m && !seen.has(m[1])) {
      seen.add(m[1]);
      let name = (a.getAttribute('title') || a.textContent || '').trim().replace(/\s+/g, ' ');
      out.episodes.push({ slug: m[1], name: name.slice(0, 140) });
    }
  });
  document.querySelectorAll('a[href*="/browse/hentai-tags/"], a[href*="/browse/tags/"], a[href*="/browse/tag/"]').forEach(a => {
    const t = (a.textContent || '').trim();
    if (t && out.genres.indexOf(t) === -1) out.genres.push(t);
  });
  try {
    const low = (document.body.innerText || '').toLowerCase();
    out.censored = /\buncensored\b/.test(low) ? 'Uncensored'
                 : (/\bcensored\b/.test(low) ? 'Censored' : '');
  } catch (e) {}
  return out;
}
"""

# JS to start playback so the player requests the signed .m3u8.
# #HTVPlayerContainer is the current (Vue/Astro) player root; #HTVPlayerRoot
# and the video.js classes are kept as harmless fallbacks in case a cached or
# alternate page still serves the older markup.
_PLAY_JS = r"""
() => {
  const sels = ['[aria-label="Play video"]', '[aria-label="Play"]',
                '.vjs-big-play-button', '#HTVPlayerContainer', '#HTVPlayerRoot',
                '.vjs-poster'];
  for (const s of sels) { const el = document.querySelector(s); if (el) { try { el.click(); } catch (e) {} } }
  const scope = document.querySelector('#HTVPlayerContainer') || document;
  const v = scope.querySelector('#HTVPlayer_html5_api') || scope.querySelector('video')
            || document.querySelector('#HTVPlayer_html5_api') || document.querySelector('video');
  if (v) { try { v.muted = true; const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {} }
}
"""


def fetch_video(slug, want_stream=False, timeout_ms=_NAV_TIMEOUT):
    """Return (detail, m3u8) for a hanime video slug.

    ``detail`` is a normalised dict:
        {title, description, poster_url, year, censored, genres[], episodes[]}
    ``m3u8`` is the HLS URL (only fetched when ``want_stream`` – requires
    clicking the player), else None.
    """
    spw = _sync_playwright()
    if spw is None:
        logger.warning("hanime: patchright/playwright not installed — cannot fetch video")
        return {}, None

    detail = {}
    m3u8 = [None]
    seen = []
    title = ""
    with spw() as p:
        handle, context, page = _new_page(p)
        try:
            def _on_response(resp):
                try:
                    u = resp.url
                    # hanime no longer always puts ".m3u8" in the manifest URL
                    # (current player fetches "/hls/<id>/<token>" with no file
                    # extension at all) — the reliable signal is the response's
                    # Content-Type, which the HLS manifest always sends as
                    # application/x-mpegurl (also accept the literal ".m3u8"
                    # URL as a fallback in case an older/alternate path is used).
                    if m3u8[0] is None:
                        is_manifest = ".m3u8" in u
                        if not is_manifest and "/hls/" in u:
                            try:
                                ct = (resp.header_value("content-type") or "").lower()
                            except Exception:
                                ct = ""
                            is_manifest = "mpegurl" in ct or "vnd.apple.mpegurl" in ct
                        if is_manifest:
                            m3u8[0] = u
                            seen.append((u[:70], resp.status))
                            return
                    if "handshake" in u or "sign.bin" in u:
                        seen.append((u[:70], resp.status))
                except Exception:
                    pass
            page.on("response", _on_response)
            try:
                page.goto(f"{_BASE}/videos/hentai/{slug}", wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as e:
                logger.debug("hanime goto failed: %s", e)

            # hanime.tv now fronts the page with Cloudflare Turnstile
            # (window.AppConfig.turnstile_public_site_key). If a challenge
            # widget is present, click it and give it a moment to validate
            # before doing anything else — a no-op (returns False quickly)
            # when no Turnstile widget exists on this page.
            try:
                from ...playwright.captcha import _click_turnstile, _is_turnstile_token_ready
            except ImportError:
                from mediaforge.playwright.captcha import _click_turnstile, _is_turnstile_token_ready
            try:
                if not _is_turnstile_token_ready(page):
                    if _click_turnstile(page, logger):
                        for _ in range(10):
                            if _is_turnstile_token_ready(page):
                                break
                            page.wait_for_timeout(500)
            except Exception as e:
                logger.debug("hanime turnstile handling failed: %s", e)

            # The SPA hydrates the ld+json block + episode links asynchronously
            # after domcontentloaded, so *some* wait is unavoidable. Rather than
            # always blocking for the full _SETTLE_MS regardless of how fast the
            # page actually loaded, wait for the ld+json tag specifically (the
            # earliest reliable "hydration happened" signal) and only fall back
            # to the fixed settle time if it never shows up — this is a real
            # speedup on a fast connection without weakening the slow-page case.
            try:
                page.wait_for_selector('script[type="application/ld+json"]', timeout=_SETTLE_MS)
                page.wait_for_timeout(300)  # let the episode-links section catch up
            except Exception:
                page.wait_for_timeout(_SETTLE_MS)
            try:
                detail = page.evaluate(_EXTRACT_JS) or {}
            except Exception as e:
                logger.debug("hanime page extract failed: %s", e)
                detail = {}
            eps = detail.get("episodes") or []
            if slug and not any(e.get("slug") == slug for e in eps):
                eps.insert(0, {"slug": slug, "name": detail.get("title") or ""})
                detail["episodes"] = eps

            if want_stream:
                try:
                    page.wait_for_selector(
                        "#HTVPlayerContainer, #HTVPlayerRoot, #HTVPlayer_html5_api, video",
                        timeout=8000,
                    )
                except Exception:
                    pass
                try:
                    page.evaluate(_PLAY_JS)
                except Exception:
                    pass
                try:
                    box = (page.query_selector("#HTVPlayerContainer")
                           or page.query_selector("#HTVPlayerRoot")
                           or page.query_selector("#HTVPlayer_html5_api"))
                    if box:
                        bb = box.bounding_box()
                        if bb:
                            page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
                except Exception:
                    pass
                for _ in range(20):
                    if m3u8[0]:
                        break
                    page.wait_for_timeout(500)
            try:
                title = page.title()
            except Exception:
                pass
        finally:
            try:
                handle.close()
            except Exception:
                pass

    if not detail or (want_stream and not m3u8[0]):
        logger.warning(
            "hanime fetch_video(%s, want_stream=%s): detail_keys=%s m3u8=%s page_title=%r seen=%s",
            slug, want_stream, list(detail.keys()) if detail else "NONE",
            m3u8[0], title, seen or "NONE",
        )
    return detail or {}, m3u8[0]
