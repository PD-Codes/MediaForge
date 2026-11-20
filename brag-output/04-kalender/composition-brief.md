# Hyperframes Composition Brief: MediaForge — Calendar

## Output
- Composition: `04-kalender/composition/` (single root timeline, scenes as timed groups)
- Video: `04-kalender/brag.mp4` — vertical 1080x1920, 22 s; poster `brag.jpg`
- Language: English (user request, overrides the German copy in brag-plan.md)

## Source material
README.md highlights, `web/templates/*.html`, `web/static/autosync.js`, `web/notifications.py`, `web/media_kinds.py`, the German/English UI strings, and the live official module store index (mediaforge.pd-codes.net/store, read 2026-10-28). Series titles, file names, URLs, dates and hoster response times are fictional stand-ins.

## Look
Tokens from `web/static/variables.css`: ground #0d0d14, card #16162a, accent #7c3aed, blue #2563eb, ink #f0f0f8, success #22c55e. Inter 400/700/900, JetBrains Mono for URLs, the real `static/icon.svg`. Shared CSS/helpers: `_shared/mf.css`, `_shared/mf.js`.

## Scenes
Month grid fills itself 'When's the next episode?' (0-3.7) · 'Already on the calendar.' + Today (3.7-6.3) · Month -> Week -> Agenda (6.3-10.4) · Detail modal (10.4-14.6) · Calendar subscription, Google/Apple/Thunderbird/DAVx5 (14.6-18.8) · Outro (18.8-22)

## Audio
- Music: happy-beats-business-moves-vol-9 (laid-back, 115 BPM) by ende.app (CC BY 4.0) — not used by any other video in the series
- Audio-reactive: the violet background glow follows per-frame RMS/bass (`assets/audio-env.js`)
- Beat locks marked `// beat-locked` / `// beat-grid` in index.html
- SFX: Kenney CC0 + HyperFrames bundled set, motion-matched; final mix normalised to about -14 LUFS
