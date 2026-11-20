# Hyperframes Composition Brief: MediaForge — Modules & Theme Packs

## Output
- Composition: `02-module/composition/` (single root timeline, scenes as timed groups)
- Video: `02-module/brag.mp4` — vertical 1080x1920, 22 s; poster `brag.jpg`
- Language: English (user request, overrides the German copy in brag-plan.md)

## Source material
README.md highlights, `web/templates/*.html`, `web/static/autosync.js`, `web/notifications.py`, `web/media_kinds.py`, the German/English UI strings, and the live official module store index (mediaforge.pd-codes.net/store, read 2026-10-28). Series titles, file names, URLs, dates and hoster response times are fictional stand-ins.

## Look
Tokens from `web/static/variables.css`: ground #0d0d14, card #16162a, accent #7c3aed, blue #2563eb, ink #f0f0f8, success #22c55e. Inter 400/700/900, JetBrains Mono for URLs, the real `static/icon.svg`. Shared CSS/helpers: `_shared/mf.css`, `_shared/mf.js`.

## Scenes
Theme flip hook LCARS/Matrix/default 'Make it yours.' (0-3) · Module Manager store with real entries (3-6.6) · Resolution Badge install (6.6-11) · 28 theme packs, live recolour Cyberpunk 2077 -> Nord (11-15.6) · Build your own (15.6-17.85) · Outro (17.85-22)

## Audio
- Music: happy-beats-business-moves-vol-11 (warm, 115 BPM) by ende.app (CC BY 4.0) — not used by any other video in the series
- Audio-reactive: the violet background glow follows per-frame RMS/bass (`assets/audio-env.js`)
- Beat locks marked `// beat-locked` / `// beat-grid` in index.html
- SFX: Kenney CC0 + HyperFrames bundled set, motion-matched; final mix normalised to about -14 LUFS
