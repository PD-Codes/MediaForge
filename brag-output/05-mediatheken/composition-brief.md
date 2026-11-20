# Hyperframes Composition Brief: MediaForge — Libraries

## Output
- Composition: `05-mediatheken/composition/` (single root timeline, scenes as timed groups)
- Video: `05-mediatheken/brag.mp4` — vertical 1080x1920, 23 s; poster `brag.jpg`
- Language: English (user request, overrides the German copy in brag-plan.md)

## Source material
README.md highlights, `web/templates/*.html`, `web/static/autosync.js`, `web/notifications.py`, `web/media_kinds.py`, the German/English UI strings, and the live official module store index (mediaforge.pd-codes.net/store, read 2026-10-28). Series titles, file names, URLs, dates and hoster response times are fictional stand-ins.

## Look
Tokens from `web/static/variables.css`: ground #0d0d14, card #16162a, accent #7c3aed, blue #2563eb, ink #f0f0f8, success #22c55e. Inter 400/700/900, JetBrains Mono for URLs, the real `static/icon.svg`. Shared CSS/helpers: `_shared/mf.css`, `_shared/mf.js`.

## Scenes
Library hub 'One app. Every library.' (0-3.3) · Web player, Skip intro, source picker (3.3-8.4) · eBook with EPUB/MOBI/AZW3/PDF + sepia reader (8.4-12.9) · Comics issues + double-page spread (12.9-17.4) · Jellyfin & Plex, .nfo, subtitle tracks (17.4-19.6) · Outro (19.6-23)

## Audio
- Music: happy-beats-business-moves-vol-12 (steady, 110 BPM) by ende.app (CC BY 4.0) — not used by any other video in the series
- Audio-reactive: the violet background glow follows per-frame RMS/bass (`assets/audio-env.js`)
- Beat locks marked `// beat-locked` / `// beat-grid` in index.html
- SFX: Kenney CC0 + HyperFrames bundled set, motion-matched; final mix normalised to about -14 LUFS
