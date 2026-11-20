# Brag Plan: MediaForge — Folge 1/5: Downloads

> Final copy: English (changed mid-run at the user's request). The rendered video and composition-brief.md hold the English text; German lines below are the original draft.

## What is this app?
MediaForge is a free, self-hosted, open-source media downloader and media-server companion with a web UI: pick a series, a season or one episode from 8 sources (aniworld.to, s.to, filmpalast.to, MegaKino, filmo.to, 9anime.or.at, aniwaves.ru, hanime.tv) or paste any direct link, and it lands finished in your library.

## The angle
"Ein Klick, und der Rest passiert." The whole download pipeline in one breath: source → episodes → queue → post-processing (H.265/NVENC, Anime4K, soft subtitles, merged audio tracks). Every step is the real UI, not a diagram.

## Hook (0–2.6s)
Huge type slot-machine: source names roll through one line ("aniworld.to → s.to → filmpalast.to → MegaKino → filmo.to …") and lock on "8 Quellen." Then a second line slams under it: "1 Klick."

## Key moments
- Download modal: "Was willst du laden?" → season chips, 4 episode checkboxes ticking one by one, "Deutsch" language pill, cursor clicks "Jetzt laden".
- Download-Queue: three rows, progress bars racing, hoster pills (VOE, Vidoza, Filemoon), first row flips to green ✓.
- Direkt Link: a `.m3u8` URL types itself in, quality variants 1080p / 720p / 480p appear, 1080p is picked.
- Post-processing chips stack: "H.265 · NVENC", "Anime4K Upscaling", "Untertitel als Spuren", "Sprache als Audiospur".

## Outro / punchline
Icon + "MediaForge" + "Kostenlos. Self-hosted. Open Source." + `pip install mediaforge`.

## User flow worth showing
Pick episodes in the download modal → watch them run in the queue → finished file with subtitles/encoding.

## Tone
- Preset: chaotic → softened to premium
- Creative direction: punchy TikTok product ad, dark premium UI, fast cuts but every line held long enough to read
- Interpretation: fast whips between scenes, snappy arrivals, UI acts constantly; no clutter, one accent colour.

## Format: vertical — 1080x1920
## Duration: 22s

## Visual identity (from the project, `static/variables.css`, `static/icon.svg`)
- Background: #0d0d14 (surface #13131e, card #16162a, elevated #1a1a28)
- Accent: #7c3aed (icon gradient #7c3aed → #2563eb)
- Text: #f0f0f8 / secondary #9191b0
- Success: #22c55e
- Display + body font: Inter (the project ships Inter)
- Strongest visual element: the violet play-triangle icon, dark cards with violet glow

## Share copy (draft)
8 Quellen, 1 Klick: MediaForge lädt Serien, Filme und Anime, encodiert sie und packt die Untertitel gleich mit rein. Kostenlos & self-hosted.

## Audio direction
- Role: dense rhythmic layer
- Music: happy-beats-business-moves-vol-10 (punchy, 110 BPM) — only this video uses it
- Music treatment: start at 0 (beat at 0.27s), 0.38 volume, fade out under the outro
- Music cue guidance: preset `vol-10.music-cues.json`; beat grid 0.27, 0.82, 1.37, 1.90, 2.46 …; strong cues 15.82, 18.01, 20.19 → lock the post-processing payoff near 15.82 and the outro logo near 20.19
- Audio-reactive treatment: subtle; bass energy breathes the violet glow behind the cards
- SFX posture: moderate, motion-matched — UI clicks, ticks on checkboxes, whooshes on whips, one impact on the logo. Palette: ui/click + interface/switch + impactPunch for the hook.
- Restraint rule: no sound on every progress tick; one take per sound rotated

## Storyboard
### Scene 1 — Hook — 2.6s
Source names roll like a slot machine and lock on "8 Quellen." then "1 Klick." slams in accent.
Sequential/interaction: yes — roll of source names. Audio: punch on lock. Transition: whip up → S2

### Scene 2 — Download modal — 4.6s
Modal "Was willst du laden?" with a fictional series title, season chips, 4 episodes ticked one by one, "Deutsch" pill, cursor clicks "Jetzt laden".
Sequential/interaction: checkbox ticks + click. Audio: soft switch ticks, click. Transition: button grows into next scene → S3

### Scene 3 — Queue — 4.4s
"Download-Queue" with 3 rows, hoster pills, bars filling at different speeds, row 1 turns ✓ green.
Audio: whoosh in, one success pop. Transition: whip → S4

### Scene 4 — Direkt Link — 3.8s
URL types into the input, 3 quality chips arrive, 1080p selected.
Audio: soft key ticks, click. Transition: whip → S5

### Scene 5 — Danach — 3.2s
Four post-processing chips stack in (held together on screen).
Audio: pops on every other beat. Transition: dip → S6

### Scene 6 — Outro — 3.4s
Logo, name, claim, install line.
Audio: impact on the logo, music fades.

**Music mood:** upbeat punchy. **Audio summary:** punchy bed from frame one, UI sounds on every visible action, one logo hit.

Fictional stand-ins: series title "Sternenpfad" and the URL `cdn.example.net/stream/master.m3u8` are invented; no real user data appears.
