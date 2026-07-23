<a id="readme-top"></a>

# MediaForge

**MediaForge** is a cross-platform **WebUI** tool for downloading anime from aniworld.to, series from s.to, and movies from filmpalast.to. It runs on Windows, macOS, and Linux.

> Fork of the original [AniWorld-Downloader](https://github.com/phoenixthrush/AniWorld-Downloader) by [phoenixthrush](https://github.com/phoenixthrush), [SiroxCW](https://github.com/SiroxCW) and [Tmaster055](https://github.com/Tmaster055) — maintained and extended here by [TheMRX13](https://github.com/TheMRX13) and [Domekologe](https://github.com/Domekologe). The legacy CLI has been **removed**; everything runs through the WebUI.

![GitHub Release](https://img.shields.io/github/v/release/PD-Codes/MediaForge)
![GitHub License](https://img.shields.io/github/license/PD-Codes/MediaForge)
![GitHub Issues or Pull Requests](https://img.shields.io/github/issues/PD-Codes/MediaForge)
![GitHub Repo stars](https://img.shields.io/github/stars/PD-Codes/MediaForge)
![GitHub forks](https://img.shields.io/github/forks/PD-Codes/MediaForge)
[![PyPI](https://img.shields.io/pypi/v/mediaforge?label=PyPI&color=blue)](https://pypi.org/project/mediaforge/)
[![Docker](https://img.shields.io/badge/ghcr.io-mediaforge-2496ED?logo=docker&logoColor=white)](https://ghcr.io/pd-codes/mediaforge)
[![Discord](https://img.shields.io/badge/Discord-Join%20Server-5865F2?logo=discord&logoColor=white)](https://discord.gg/TGaZ9hFFhC)
[![Wiki](https://img.shields.io/badge/Docs-Wiki-8A2BE2)](https://github.com/PD-Codes/MediaForge/wiki)

WebUI (Landing Page) | WebUI (Auto-Sync)
:-------------------------:|:-------------------------:
![MediaForge - Demo](https://cdn.domekologe.eu/d6c3daa9-2e80-4cdb-8191-5c700b811e2e/6f0d3c9b-cc70-415d-a069-875585ff0886/8ca6cce9-911d-445e-a907-aebfdb7c95a8.png) | ![MediaForge - Demo](https://cdn.domekologe.eu/d6c3daa9-2e80-4cdb-8191-5c700b811e2e/6f0d3c9b-cc70-415d-a069-875585ff0886/b0741266-1687-4dd1-a8f5-0ea0150a4220.png)


> [!NOTE]  
> This images contains some modules and settings which can be enabled within the settings.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Quick Start

> **Requirements:** Python ≥ 3.10 and **ffmpeg** available in your `PATH`. New to Python? See the [Installation](https://github.com/PD-Codes/MediaForge/wiki/Installation) wiki for per-OS instructions.

```bash
# Install the latest stable release
pip install mediaforge

# Launch — starts the WebUI and opens your browser
mediaforge
```

Open `http://localhost:8080`. Authentication is enabled by default and **all settings live in the WebUI** — no `.env` file needed.

Useful launch flags: `-wP <port>` (custom port) · `-wH 0.0.0.0` (expose to LAN/Docker/reverse proxy) · `-wN` (don't open the browser) · `-d` (debug logging).

📖 Full setup — including **how to install Python & ffmpeg**, the data directory and first-run steps — is on the **[Installation](https://github.com/PD-Codes/MediaForge/wiki/Installation)** and **[Getting Started](https://github.com/PD-Codes/MediaForge/wiki/Getting-Started)** wiki pages.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Highlights

- **Browse, search & download** full series, seasons or single episodes from aniworld.to, s.to and filmpalast.to
- **AutoSync** — keep series up to date automatically on an **interval or weekly schedule**, with per-job **season/episode filters** and a separate path for movies/specials
- **Download queue & history** — a real-time queue plus a searchable, filterable **history** (failed/cancelled/skipped, retry, bulk delete, export, auto-retention)
- **Media library, favourites & statistics** built in
- **CineInfo (TMDB)** metadata, a **Calendar** of upcoming episodes, plus **Jellyfin/Plex** and **Jellyseerr/Overseerr** integration
- **Notifications** via Web Push, Telegram, Pushover, Discord or WhatsApp
- **Encoding** (Stream Copy / H.264 / H.265, with NVENC, VAAPI, VideoToolbox) and **Anime4K** upscaling
- **Full & Selective Backup** — export/import settings and user data as a password-protected file to back up or migrate a MediaForge install (admin only)
- **Modules & Theme Packs** — extend MediaForge with store-installable modules, and reskin the whole UI (fonts, animations, checkboxes, inputs, calendar, images) with CSS-only theme packs from the same store
- **Multi-user auth & OIDC SSO**, a full **REST API**, **Docker**-ready, and a UI in **English & German**

→ Full feature tour in the **[Wiki](https://github.com/PD-Codes/MediaForge/wiki/Web-UI)**.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Documentation

Everything is documented in the **[Wiki](https://github.com/PD-Codes/MediaForge/wiki)** — available in **English** and **Deutsch**:

| Getting started | Features | Reference |
|---|---|---|
| [Installation](https://github.com/PD-Codes/MediaForge/wiki/Installation) · [Getting Started](https://github.com/PD-Codes/MediaForge/wiki/Getting-Started) · [Docker](https://github.com/PD-Codes/MediaForge/wiki/Docker) | [AutoSync](https://github.com/PD-Codes/MediaForge/wiki/AutoSync) · [Download History](https://github.com/PD-Codes/MediaForge/wiki/Download-History) · [Library](https://github.com/PD-Codes/MediaForge/wiki/Library) · [Integrations](https://github.com/PD-Codes/MediaForge/wiki/Integrations) · [SyncPlay](https://github.com/PD-Codes/MediaForge/wiki/SyncPlay) · [Encoding](https://github.com/PD-Codes/MediaForge/wiki/Encoding) | [Configuration](https://github.com/PD-Codes/MediaForge/wiki/Configuration) · [Authentication](https://github.com/PD-Codes/MediaForge/wiki/Authentication) · [Supported Sites](https://github.com/PD-Codes/MediaForge/wiki/Supported-Sites) · [API Reference](https://github.com/PD-Codes/MediaForge/wiki/API-Reference) · [Architecture](https://github.com/PD-Codes/MediaForge/wiki/Architecture) |

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Docker

```bash
docker pull ghcr.io/pd-codes/mediaforge:latest

docker run -it --rm -p 8080:8080 \
  -v "${PWD}/Downloads:/app/Downloads" \
  -v mediaforge-data:/home/mediaforge/.mediaforge \
  ghcr.io/pd-codes/mediaforge:latest
```

Mount your `Downloads` folder for the files and the `mediaforge-data` volume for config/database. For **Docker Compose**, **reverse proxy (nginx)**, **LAN access** and **env-based admin setup**, see the **[Docker](https://github.com/PD-Codes/MediaForge/wiki/Docker)** wiki page.

Running behind a VPN? A ready-to-use **Gluetun** setup is in [`docker-compose.gluetun.yaml`](docker-compose.gluetun.yaml) — see the [Docker wiki](https://github.com/PD-Codes/MediaForge/wiki/Docker#routing-through-a-vpn-gluetun) for the details.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Compatibility

> [!WARNING]
> **NAS devices (UGREEN, Synology):** Both are **supported**, but MediaForge's code is **not optimized** for these two NAS platforms and will not be — so **expect recurring issues**. We're happy to **help with installation**, but we do not optimize MediaForge for UGREEN or Synology. For the smoothest experience, run it via [Docker](https://github.com/PD-Codes/MediaForge/wiki/Docker) on a standard Linux, Windows or macOS host.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Roadmap

Actively in development — current work in progress:

- [x] Multi-Language Support — UI available in German and English
- [ ] More Extractors — additional video host support beyond the current providers
- [x] Live Transcode / In-Browser Playback — watch downloaded files directly in the UI
- [ ] Discord Rich Presence — show what you're currently watching on your Discord profile
- [ ] More Sources — additional anime, series and movie providers (open a feature request)
- [x] AutoSync Episode Filter — configure per job which seasons/episodes to sync, include/skip movies & specials, separate movie download path
- [x] AutoSync Schedule — run on a fixed interval or a weekly plan (weekdays + times)
- [x] Provider Fallback Order — automatically try the next provider if the primary one fails
- [x] Calendar View — show upcoming episode air dates for AutoSync jobs based on TMDB data
- [x] Bandwidth Limit / Download Time Window — throttle speed or restrict downloads to specific hours
- [x] Download History — searchable log of all completed downloads with date, size and duration
- [ ] Generic Outgoing Webhook — send a configurable POST request on download completion (Home Assistant, n8n, etc.)
- [ ] Subtitle Support — additional language and subtitle download options
- [ ] Integrated VPN/Proxy — download through VPN or proxy servers to increase privacy
- [x] Adding Pills to Advanced Search if a Media is already downloaded, and more
- [x] Auto-Sync "Waitlist". If an Auto-Sync Title was not found for X Days, it will be tried again in Y week(s)

**Go here for more information**: [Progress in Work](https://github.com/orgs/PD-Codes/projects/4/views/1)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Supported Sites & Extractors

URLs from **aniworld.to**, **s.to** and **filmpalast.to** are supported, delivered behind the scenes via hosters such as VOE, Vidoza, Vidmoly, Filemoon, Doodstream, Vidara and Veev.

→ See **[Supported Sites](https://github.com/PD-Codes/MediaForge/wiki/Supported-Sites)** for the live status of each site and extractor, and which hosters are prioritized per site.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Contributing

Contributions are **highly appreciated** — report bugs, suggest features, submit pull requests, or improve the docs. Please check existing [issues](https://github.com/PD-Codes/MediaForge/issues) first to avoid duplicates, and feel free to discuss ideas on [Discord](https://discord.gg/TGaZ9hFFhC) before opening a PR.

### Translations
Help us translate MediaForge into your language! We use Weblate to manage all localizations. You can easily contribute translations online without any coding knowledge here:
**[Translate MediaForge on Weblate](https://webplate.softarchiv.com/projects/mediaforge/)**

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Credits

Builds upon the work of several outstanding open-source projects and individuals:

**Original authors** — [phoenixthrush](https://github.com/phoenixthrush) (creator of [AniWorld-Downloader](https://github.com/phoenixthrush/AniWorld-Downloader)), [SiroxCW](https://github.com/SiroxCW) and [Tmaster055](https://github.com/Tmaster055).

**Libraries & tools** — [mpv](https://github.com/mpv-player/mpv.git), [Flask](https://flask.palletsprojects.com/) and [Waitress](https://docs.pylonsproject.org/projects/waitress/).

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Other Cool Projects

- **[Jellyfin AniWorld Downloader](https://github.com/SiroxCW/Jellyfin-AniWorld-Downloader)** by **[SiroxCW](https://github.com/SiroxCW)** — a Jellyfin plugin to browse and download anime & series directly from AniWorld, integrated into your media server.
- **[AniBridge](https://github.com/Zzackllack/AniBridge)** by **[Zzackllack](https://github.com/Zzackllack)** — a minimal FastAPI service bridging anime and series catalogues (AniWorld, SerienStream/s.to, MegaKino) with automation tools.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Support

- **[Discord Server](https://discord.gg/TGaZ9hFFhC)** — the fastest way to get help and chat with other users.
- **[GitHub Issues](https://github.com/PD-Codes/MediaForge/issues)** — preferred for installation problems, bug reports and feature requests.

If you find MediaForge useful, please ⭐ the repository — it's greatly appreciated and motivates continued development.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Legal Disclaimer

MediaForge is a **client-side** tool that enables access to content hosted on third-party websites. It **does not host, upload, store, or distribute any media itself**.

This software is **not intended to promote piracy or copyright infringement**. You are solely responsible for how you use it and for ensuring that your use **complies with applicable laws** and the **terms of service of the websites you access**.

The developer provides this project **"as is"** and is **not responsible for** third-party content, external links, or the availability, accuracy, legality or reliability of any third-party service. If you have concerns about specific content, please contact the respective website or rights holder directly.

<p align="right">(<a href="#readme-top">back to top</a>)</p>
