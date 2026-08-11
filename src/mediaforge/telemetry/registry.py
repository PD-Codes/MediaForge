"""Telemetry data-point registry.

One entry per ``data_key`` from ``TELEMETRY_PLAN.md`` §2 -- the single source
of truth for what a data point means. The ``explain`` text is written for an
end user (no jargon, no "helps us improve the product" hand-waving) because
it is rendered *verbatim* in the Settings confirmation dialog (see
``static/telemetry.js``) as well as in the first-run consent dialog -- there
is deliberately no second, separately-maintained copy of this wording
anywhere in the frontend.

``title``/``description``/``label``/``explain`` values below are
``{"de": ..., "en": ...}`` dicts rather than plain strings: this registry is
serialized straight to JSON for the frontend (``registry_export()``), never
routed through a Jinja template, so it falls outside ``babel.cfg``'s
``[jinja2: ...]``-only extraction mapping (see that file's own comment on why
``.py`` sources aren't scanned) and thus outside the normal
``pybabel extract`` / ``.po``/``.mo`` catalog workflow entirely. Keeping both
languages inline here -- picked by ``registry_export(lang=...)`` using the
same session-based locale ``web/app.py``'s ``get_locale()`` already resolves
-- keeps this the single place that ever needs updating, instead of a second
hand-copied English set living in ``static/telemetry.js``.

HARD RULE for future editors of this file -- not a setting, not a toggle:
``DATA_REGISTRY`` contains data points of MediaForge itself and nothing else.
No ``data_key`` is ever added here for a third-party module/extension, and no
telemetry interface is offered to module code. Modules are foreign code that
the user can write themselves, so their errors and their usage are none of
this project's business -- reporting them would mean shipping numbers we
cannot interpret about code we did not write, under MediaForge's install_id.
The two ``*.extensions`` keys below are not an exception to this: they are
collected by the core *about* the set of loaded modules (how many, and their
folder names -- see ``web/thirdparties/registry.py``), never by a module
about itself.

Also home to the small set of constants that tie the client to the devInfo
server: the ingest/request endpoints and the shared "project key" anti-spam
header value.

Deliberately does NOT import ``web.devinfos_monitor.DEVINFOS_SERVER_URL``
here even though it names the same server: merely importing anything under
``mediaforge.web`` forces Python to first fully execute
``mediaforge/web/__init__.py``, which imports ``web/app.py`` at module level,
which in turn drags in the queue worker, every provider/model and their
third-party dependencies (ffmpeg-python, playwright, ...) -- a huge,
fragile transitive import just to read one URL string. This package is
meant to stay a lightweight leaf (its only heavier dependency is
``web.db`` for settings persistence, imported from ``settings.py``, and
that one is unavoidable per the DB-first settings requirement). Keep this
value in sync with ``web/devinfos_monitor.py``'s ``DEVINFOS_SERVER_URL`` by
hand if that one ever changes -- both name the same devInfo server.
"""

# Same server web/devinfos_monitor.py polls for changelog posts -- kept as
# an independent literal on purpose, see module docstring above.
DEVINFOS_SERVER_URL = "https://mediaforge.softarchiv.com"

# ---------------------------------------------------------------------------
# devInfo server endpoints (telemetry side)
# ---------------------------------------------------------------------------

_BASE = DEVINFOS_SERVER_URL.rstrip("/")

TELEMETRY_INGEST_URL = f"{_BASE}/telemetry/ingest"
# One-time enrollment: exchanges the (public) project key for a per-install
# device secret every later request is signed with -- see telemetry/device_auth.py.
TELEMETRY_REGISTER_URL = f"{_BASE}/telemetry/register"
TELEMETRY_REQUEST_URL = f"{_BASE}/telemetry/request-from-app"
TELEMETRY_REQUEST_STATUS_URL = f"{_BASE}/telemetry/request-from-app/status"

# Static "project key" sent as the X-Project-Key header on every telemetry
# call. This is NOT a secret -- it ships in plain sight in the client source
# code (and in every built/installed copy of this package) and is trivially
# extractable by anyone who looks. Its only job is to filter out the random
# background noise of bots/scanners hitting the ingest endpoint that have no
# relationship to MediaForge whatsoever; it is not a substitute for a real
# per-device credential and must never be treated as one (see
# TELEMETRY_PLAN.md §7b/§7.4 for the honestly-documented limitation of this
# whole verification scheme).
TELEMETRY_PROJECT_KEY = "mediaforge-telemetry-v1"

# ---------------------------------------------------------------------------
# Stage metadata (0-6, TELEMETRY_PLAN.md §2)
# ---------------------------------------------------------------------------

STAGE_META = {
    0: {
        "title": {"de": "Aus", "en": "Off"},
        "description": {
            "de": "Keine Verbindung zum devInfo-Server, überhaupt keine Daten verlassen dieses Gerät.",
            "en": "No connection to the devInfo server at all -- no data leaves this device.",
        },
    },
    1: {
        "title": {"de": "Absturz & System", "en": "Crash & System"},
        "description": {
            "de": (
                "Technische Fehlerprotokolle und Basis-Systeminfo, nur nach ausdrücklicher "
                "Zustimmung im Erstkonsens-Dialog. Kein Opt-out-Default -- siehe PRIVACY.md."
            ),
            "en": (
                "Technical error logs and basic system info, only after explicit consent in the "
                "first-run dialog. No opt-out default -- see PRIVACY.md."
            ),
        },
    },
    2: {
        "title": {"de": "Feature-Flags", "en": "Feature flags"},
        "description": {
            "de": "Reine Ja/Nein- bzw. Zähler-Ebene pro Feature -- keine Titel, keine Inhalte.",
            "en": "Plain yes/no or counter level per feature -- no titles, no content.",
        },
    },
    3: {
        "title": {"de": "Feature-Details & Fehler", "en": "Feature details & errors"},
        "description": {
            "de": "Mehr Kontext zu denselben Features (Laufzeiten, Fehlerzahlen), weiterhin ohne Titel/Inhalte.",
            "en": "More context on the same features (run times, error counts), still without titles/content.",
        },
    },
    4: {
        "title": {"de": "Download-Inhalte", "en": "Download content"},
        "description": {
            "de": "Welche Serien/Filme heruntergeladen wurden, inkl. Provider und Erfolg/Fehlschlag.",
            "en": "Which shows/movies were downloaded, incl. provider and success/failure.",
        },
    },
    5: {
        "title": {"de": "Wiedergabe-Kontext", "en": "Playback context"},
        "description": {
            "de": "Welcher Titel gerade gestartet wird -- noch ohne Watchtime.",
            "en": "Which title is being started -- still without watchtime.",
        },
    },
    6: {
        "title": {"de": "Sehverhalten / Watchtime", "en": "Watch behaviour / watchtime"},
        "description": {
            "de": (
                "Wiedergabefortschritt, Watchtime-Summen und Abschlussquote. In Kombination mit der "
                "install_id und den Titel-Daten aus Stufe 4/5 ein echtes Verhaltensprofil -- deutlich "
                "näher an klassischer Streaming-Analytics als an Crash-Reporting. Nicht Teil von "
                "'alles aktivieren', Default für alle drei Punkte: aus."
            ),
            "en": (
                "Playback progress, watchtime totals and completion rate. Combined with the "
                "install_id and the title data from stage 4/5, a real behaviour profile -- much "
                "closer to classic streaming analytics than to crash reporting. Not part of "
                "'enable everything', default for all three points: off."
            ),
        },
    },
}

# ---------------------------------------------------------------------------
# Data-point registry
# ---------------------------------------------------------------------------
# stage:   0-6, TELEMETRY_PLAN.md §2
# group:   coarse feature grouping, used to cluster rows in the Settings UI
# label:   short UI label for the individual toggle row
# explain: full end-user explanation, reused 1:1 in the confirmation dialog
# always_on: True for the one field that has no toggle of its own (install_id
#            is a technical prerequisite, not a data point the user picks)

DATA_REGISTRY = {
    # ---- Stage 1: Crash & System --------------------------------------
    "install_id": {
        "stage": 1, "group": "system",
        "label": {"de": "Installations-ID", "en": "Installation ID"},
        "always_on": True,
        "explain": {
            "de": (
                "Eine zufällige, auf diesem Gerät einmalig erzeugte Kennung (UUID). Sie wird nur "
                "zusammen mit anderen von dir aktivierten Datenpunkten verschickt, niemals allein, "
                "und ist die technische Voraussetzung dafür, dass Absturzberichte überhaupt einem "
                "wiederkehrenden Gerät zugeordnet werden können. Du kannst sie in den Einstellungen "
                "jederzeit einsehen und per Klick neu generieren (\"Identität zurücksetzen\")."
            ),
            "en": (
                "A random identifier (UUID) generated once for this device. It is only ever sent "
                "together with other data points you've enabled, never alone, and is the technical "
                "prerequisite for crash reports being attributable to a recurring device at all. You "
                "can view it in Settings at any time and regenerate it with one click "
                "(\"Reset identity\")."
            ),
        },
    },
    "crash_reports": {
        "stage": 1, "group": "system",
        "label": {"de": "Absturzberichte", "en": "Crash reports"},
        "explain": {
            "de": (
                "Technischer Fehlerbericht (Programmzeile, Dateiname, Fehlertyp), wenn MediaForge "
                "unerwartet abstürzt oder eine interne Ausnahme auftritt. Enthält niemals Passwörter, "
                "Zugangsdaten, Variableninhalte oder komplette URLs mit Tokens -- nur den bereinigten "
                "technischen Ablauf, der zum Fehler geführt hat. Zusätzlich wird eine kleine "
                "Momentaufnahme des Gerätezustands im Fehlermoment angehängt (freier Arbeitsspeicher "
                "und dessen Auslastung, freier Speicherplatz auf dem Download-Ziel, Systemlast sowie "
                "Anzahl aktiver Threads/Dateihandles), damit erkennbar ist, ob z. B. der Speicher voll "
                "war -- keine Titel, Pfade oder Inhalte."
            ),
            "en": (
                "Technical error report (source line, file name, error type) when MediaForge crashes "
                "unexpectedly or an internal exception occurs. Never includes passwords, credentials, "
                "variable contents or complete URLs with tokens -- only the sanitized technical "
                "sequence that led to the error. A small snapshot of device state at the moment of "
                "the error is attached as well (free memory and its usage, free disk space on the "
                "download target, system load and number of active threads/file handles), so it's "
                "clear whether e.g. memory was full -- no titles, paths or content."
            ),
        },
    },
    "system_info": {
        "stage": 1, "group": "system",
        "label": {"de": "System-Info", "en": "System info"},
        "explain": {
            "de": (
                "Technische Eckdaten dieser Installation, um Absturz- und Fehlerberichte richtig "
                "einordnen zu können (z. B. ein Fehler, der nur unter Windows, nur im Docker-Container "
                "oder nur ohne Hardware-Beschleunigung auftritt): App-Version, Betriebssystem und "
                "-Version, ob MediaForge in einem Container läuft (Docker/Podman/Kubernetes) und wie es "
                "installiert wurde (Docker/pip/pipx/PyInstaller), ob mit Administrator-/Root-Rechten und "
                "auf einem schreibgeschützten Dateisystem, ob ein VPN-Netzwerk erkannt wurde, Zeitzone; "
                "unter Linux zusätzlich Distribution, C-Bibliothek und Kernel; Python-Version und "
                "-Variante, Oberflächensprache, Prozessorarchitektur, CPU-Modell und Kernanzahl, "
                "Arbeitsspeicher-Gesamtgröße, erkannte Grafikkarte(n) samt Treiberversion, die von "
                "ffmpeg unterstützten sowie die tatsächlich funktionierenden Hardware-Beschleunigungen, "
                "und die Versionen zentraler Komponenten (ffmpeg, yt-dlp, mpv, Captcha-Browser). Enthält "
                "keinen Geräte- oder Benutzernamen, keine IP-Adresse und keine Dateipfade. "
                "Hinweis: Die App überträgt keine IP-Adresse. Der Server sieht die Adresse "
                "deiner Verbindung jedoch technisch bedingt und speichert sie befristet – "
                "siehe PRIVACY.md, Abschnitt „Deine Netzwerkadresse“."
            ),
            "en": (
                "Technical baseline data of this installation, to correctly classify crash and error "
                "reports (e.g. an error that only occurs on Windows, only inside a Docker container, "
                "or only without hardware acceleration): app version, OS and version, whether "
                "MediaForge runs in a container (Docker/Podman/Kubernetes) and how it was installed "
                "(Docker/pip/pipx/PyInstaller), whether run with administrator/root rights and on a "
                "read-only filesystem, whether a VPN network was detected, timezone; on Linux "
                "additionally distribution, C library and kernel; Python version and variant, UI "
                "language, processor architecture, CPU model and core count, total memory size, "
                "detected graphics card(s) with driver version, the hardware accelerations ffmpeg "
                "supports as well as the ones that actually work, and the versions of core "
                "components (ffmpeg, yt-dlp, mpv, captcha browser). Never includes a device or user "
                "name, IP address or file paths. Note: the app transmits no IP address. The server "
                "does see your connection's address as a technical necessity and stores it for a "
                "limited time — see PRIVACY.md, section \"Your network address\"."
            ),
        },
    },
    # ---- Stage 2: Feature flags (usage yes/no + counter) --------------
    "flag.autosync": {
        "stage": 2, "group": "autosync",
        "label": {"de": "AutoSync genutzt", "en": "AutoSync used"},
        "explain": {
            "de": "Nur, dass die AutoSync-Funktion genutzt wird und wie oft -- keine Serientitel.",
            "en": "Only that the AutoSync feature is used and how often -- no show titles.",
        },
    },
    "flag.syncplay": {
        "stage": 2, "group": "syncplay",
        "label": {"de": "SyncPlay genutzt", "en": "SyncPlay used"},
        "explain": {
            "de": "Nur, dass gemeinsame SyncPlay-Wiedergabesitzungen genutzt werden und wie oft -- kein Rauminhalt.",
            "en": "Only that shared SyncPlay playback sessions are used and how often -- no room content.",
        },
    },
    "flag.upscale": {
        "stage": 2, "group": "upscale",
        "label": {"de": "Upscaling genutzt", "en": "Upscaling used"},
        "explain": {
            "de": "Nur, dass die KI-Videoupscaling-Funktion genutzt wird und wie oft.",
            "en": "Only that the AI video-upscaling feature is used and how often.",
        },
    },
    "flag.transcoding": {
        "stage": 2, "group": "transcoding",
        "label": {"de": "Transcoding genutzt", "en": "Transcoding used"},
        "explain": {
            "de": "Nur, dass Video-Transcoding (Codec-Umwandlung) genutzt wird und wie oft.",
            "en": "Only that video transcoding (codec conversion) is used and how often.",
        },
    },
    "flag.library": {
        "stage": 2, "group": "library",
        "label": {"de": "Mediathek genutzt", "en": "Library used"},
        "explain": {
            "de": (
                "Nur, dass die Mediathek geöffnet wird, wie oft und welcher Bereich davon: die "
                "Übersicht, \"Filme & Serien\", \"eBooks\" oder einer der noch nicht fertigen "
                "Bereiche (Manga/Comics/Musik). Übertragen wird ausschließlich der Name des "
                "Bereichs aus einer festen, im Programm hinterlegten Liste -- keine Titel, keine "
                "Ordner-, Pfad- oder Dateinamen und keine Angabe, wie viel darin liegt."
            ),
            "en": (
                "Only that the library is opened, how often, and which of its sections: the "
                "overview, \"Movies & Series\", \"eBooks\" or one of the sections that aren't "
                "finished yet (manga/comics/music). Nothing but the section name from a fixed list "
                "built into the program is transmitted -- no titles, no folder, path or file names, "
                "and no indication of how much is in there."
            ),
        },
    },
    "flag.library_scan": {
        "stage": 2, "group": "library_scan",
        "label": {"de": "Bibliotheks-Scan genutzt", "en": "Library scan used"},
        "explain": {
            "de": "Nur, dass ein Bibliotheks-Scan (Mediathek-Abgleich) durchgeführt wurde und wie oft.",
            "en": "Only that a library scan (media library sync) was run and how often.",
        },
    },
    "flag.calendar": {
        "stage": 2, "group": "calendar",
        "label": {"de": "Kalender genutzt", "en": "Calendar used"},
        "explain": {
            "de": "Nur, dass die Erscheinungskalender-Funktion geöffnet/genutzt wird und wie oft.",
            "en": "Only that the release calendar feature is opened/used and how often.",
        },
    },
    "flag.integrations.crunchyroll": {
        "stage": 2, "group": "integrations",
        "label": {"de": "Crunchyroll-Integration genutzt", "en": "Crunchyroll integration used"},
        "explain": {
            "de": "Nur, dass die Crunchyroll-Integration aktiv verbunden ist und genutzt wird.",
            "en": "Only that the Crunchyroll integration is actively connected and used.",
        },
    },
    "flag.integrations.fernsehserien": {
        "stage": 2, "group": "integrations",
        "label": {"de": "Fernsehserien-Integration genutzt", "en": "Fernsehserien integration used"},
        "explain": {
            "de": "Nur, dass die Fernsehserien.de-Integration aktiv verbunden ist und genutzt wird.",
            "en": "Only that the Fernsehserien.de integration is actively connected and used.",
        },
    },
    "flag.integrations.seerr": {
        "stage": 2, "group": "integrations",
        "label": {"de": "Jellyseerr/Overseerr-Integration genutzt", "en": "Jellyseerr/Overseerr integration used"},
        "explain": {
            "de": "Nur, dass eine Jellyseerr/Overseerr-Integration aktiv verbunden ist und genutzt wird.",
            "en": "Only that a Jellyseerr/Overseerr integration is actively connected and used.",
        },
    },
    "flag.integrations.mediascan": {
        "stage": 2, "group": "integrations",
        "label": {"de": "MediaScan-Integration genutzt", "en": "MediaScan integration used"},
        "explain": {
            "de": "Nur, dass die MediaScan (Jellyfin/Plex-Abgleich)-Integration aktiv verbunden ist.",
            "en": "Only that the MediaScan (Jellyfin/Plex sync) integration is actively connected.",
        },
    },
    "flag.integrations.mediaplayer": {
        "stage": 2, "group": "integrations",
        "label": {"de": "Mediaplayer-Anbindung genutzt", "en": "Media player connection used"},
        "explain": {
            "de": "Nur, dass eine Jellyfin/Plex-Verbindung eingerichtet ist und getestet/genutzt wurde -- keine Server-URL, kein Servername, keine Bibliotheks- oder Titeldaten.",
            "en": "Only that a Jellyfin/Plex connection is configured and was tested/used -- no server URL, no server name, no library or title data.",
        },
    },
    "flag.push_notifications": {
        "stage": 2, "group": "push_notifications",
        "label": {"de": "Push-Benachrichtigungen genutzt", "en": "Push notifications used"},
        "explain": {
            "de": "Nur, dass Push-Benachrichtigungen (Telegram/Discord/Pushover/ntfy/...) eingerichtet sind und ausgelöst wurden.",
            "en": "Only that push notifications (Telegram/Discord/Pushover/ntfy/...) are configured and were triggered.",
        },
    },
    # ONE consent toggle for the whole source dimension, on purpose.
    #
    # The events it authorises are NOT sent under this key: each one is sent as
    # "flag.source.<id>" (see events.build_source_usage_event), because the
    # server counts feature flags per feature_key and ignores the payload -- a
    # single key would collapse every source into one meaningless counter.
    #
    # Splitting the CONSENT the same way would mean eight near-identical
    # switches in the privacy dialog for one question the user only wants to
    # answer once ("may MediaForge see which sites I use?"). So consent is
    # asked once here; is_source_key_enabled() below is what the builder
    # checks, and it maps every flag.source.* key back onto this toggle.
    #
    # hanime.tv is deliberately NOT part of this: it has its own, hard-limited
    # flag.hanime_tv and must never gain a second data point (see
    # sanitize.is_adult_provider).
    "flag.sources": {
        "stage": 2, "group": "providers",
        "label": {"de": "Genutzte Quellen-Seiten", "en": "Source sites used"},
        "explain": {
            "de": ("Welche der eingebauten Quellen-Seiten (AniWorld, SerienStream, "
                   "FilmPalast, MegaKino, filmo.to, 9anime, Aniwaves) genutzt werden, "
                   "und wie oft -- nur der Name der Seite und ein Zähler. Keine Titel, "
                   "keine Suchbegriffe, keine URLs. Beantwortet für die Entwicklung, "
                   "welche Quellen sich zu pflegen lohnen. Die 18+-Quelle ist hier "
                   "ausdrücklich NICHT enthalten, die hat ihren eigenen, strikt "
                   "begrenzten Punkt."),
            "en": ("Which of the built-in source sites (AniWorld, SerienStream, "
                   "FilmPalast, MegaKino, filmo.to, 9anime, Aniwaves) are used, and how "
                   "often -- the site name and a counter, nothing else. No titles, no "
                   "search terms, no URLs. It answers which sources are worth "
                   "maintaining. The 18+ source is explicitly NOT included; it has its "
                   "own, strictly limited data point."),
        },
    },
    "flag.catalogue": {
        "stage": 2, "group": "catalogue",
        "label": {"de": "Katalog-Sammelauswahl genutzt", "en": "Catalogue bulk selection used"},
        "explain": {
            "de": ("Nur, dass die Katalog-Seite genutzt wurde, um mehrere Serien auf "
                   "einmal in die Warteschlange oder zu Auto-Sync zu geben, und wie oft. "
                   "Keine Titel, keine Anzahl, keine Quelle."),
            "en": ("Only that the Catalogue page was used to hand several series to the "
                   "queue or to Auto-Sync at once, and how often. No titles, no counts, "
                   "no source."),
        },
    },
    "flag.uptime_monitor": {
        "stage": 2, "group": "uptime_monitor",
        "label": {"de": "UpTime-Monitoring genutzt", "en": "UpTime monitoring used"},
        "explain": {
            "de": "Nur, dass das eingebaute UpTime-Monitoring der Quellen aktiv ist.",
            "en": "Only that the built-in uptime monitoring of sources is active.",
        },
    },
    "flag.extensions": {
        "stage": 2, "group": "extensions",
        "label": {"de": "Erweiterungen genutzt", "en": "Extensions used"},
        "explain": {
            "de": "Nur, dass mindestens eine Drittanbieter-Erweiterung (Modul) geladen ist und wie viele.",
            "en": "Only that at least one third-party extension (module) is loaded, and how many.",
        },
    },
    "flag.self_update": {
        "stage": 2, "group": "self_update",
        "label": {"de": "Selbst-Update genutzt", "en": "Self-update used"},
        "explain": {
            "de": "Nur, dass die Selbst-Update-Funktion ausgeführt wurde und wie oft.",
            "en": "Only that the self-update feature was run and how often.",
        },
    },
    "flag.direct_link": {
        "stage": 2, "group": "direct_link",
        "label": {"de": "Direct-Link genutzt", "en": "Direct link used"},
        "explain": {
            "de": "Nur, dass die Direct-Link-Download-Funktion genutzt wird und wie oft -- ohne die verwendeten URLs.",
            "en": "Only that the direct-link download feature is used and how often -- without the URLs used.",
        },
    },
    "flag.captcha": {
        "stage": 2, "group": "captcha",
        "label": {"de": "Captcha-Lösung genutzt", "en": "Captcha solving used"},
        "explain": {
            "de": "Nur, dass die automatische Captcha-Lösung ausgelöst wurde und wie oft.",
            "en": "Only that automatic captcha solving was triggered and how often.",
        },
    },
    "flag.v1_api": {
        "stage": 2, "group": "v1_api",
        "label": {"de": "Externe REST-API genutzt", "en": "External REST API used"},
        "explain": {
            "de": "Nur, dass die externe REST-API (z. B. für Home Assistant) angesprochen wird und wie oft.",
            "en": "Only that the external REST API (e.g. for Home Assistant) is called and how often.",
        },
    },
    "flag.hanime_tv": {
        "stage": 2, "group": "hanime_tv",
        "label": {"de": "hanime.tv genutzt (18+)", "en": "hanime.tv used (18+)"},
        "explain": {
            "de": (
                "Nur ein reiner Nutzungszähler (\"wird genutzt: ja/nein\", wie oft) für den "
                "altersgegateten 18+-Anbieter hanime.tv. Das ist der EINZIGE Telemetrie-Datenpunkt, "
                "der für diesen Anbieter jemals erhoben wird -- keine Titel, keine Fehlermeldungen, "
                "keine Wiedergabezeiten, keine Fortschritts- oder Abschlussdaten, unabhängig davon, "
                "welche anderen Stufen du sonst aktiviert hast. Diese Ausnahme ist fest im Programmcode "
                "verankert (siehe sanitize.is_adult_provider()), keine Einstellung, die versehentlich "
                "hochgestuft werden könnte."
            ),
            "en": (
                "Only a plain usage counter (\"used: yes/no\", how often) for the age-gated 18+ "
                "provider hanime.tv. This is the ONLY telemetry data point ever collected for this "
                "provider -- no titles, no error messages, no playback times, no progress or "
                "completion data, regardless of which other stages you've otherwise enabled. This "
                "exception is hard-coded in the program (see sanitize.is_adult_provider()), not a "
                "setting that could accidentally be raised."
            ),
        },
    },
    # ---- Stage 3: Feature details & errors -----------------------------
    # Network-level trouble the app worked around on its own. Stage 3 because
    # it is the "feature details & errors" level and these ARE errors -- just
    # errors that were survivable, which is exactly why nothing else reports
    # them today: a DNS fallback or a source that failed to load leaves a
    # WARNING in one user's log and is invisible everywhere else.
    "detail.network": {
        "stage": 3, "group": "system",
        "label": {"de": "Netzwerk-/Quellen-Störungen", "en": "Network / source problems"},
        "explain": {
            "de": ("Wenn der eingestellte DNS-Resolver einen Host nicht auflösen konnte "
                   "und auf den System-Resolver ausgewichen wurde, und wenn eine "
                   "Quellen-Seite beim Laden ausgefallen ist. Übertragen wird die "
                   "Art des Problems und -- nur bei Quellen-Ausfällen -- der Name der "
                   "Quelle. Niemals der Hostname, der DNS-Server, deine IP oder eine URL."),
            "en": ("When the configured DNS resolver could not resolve a host and the "
                   "system resolver was used instead, and when a source site failed to "
                   "load. What is sent is the kind of problem and -- for source outages "
                   "only -- the name of the source. Never the hostname, the DNS server, "
                   "your IP or any URL."),
        },
    },
    "detail.autosync": {
        "stage": 3, "group": "autosync",
        "label": {"de": "AutoSync-Statistik", "en": "AutoSync statistics"},
        "explain": {
            "de": "Lauf-Statistik von AutoSync: Anzahl Läufe, Dauer, Fehleranzahl -- weiterhin ohne Serientitel.",
            "en": "AutoSync run statistics: number of runs, duration, error count -- still without show titles.",
        },
    },
    "detail.syncplay": {
        "stage": 3, "group": "syncplay",
        "label": {"de": "SyncPlay-Sitzungsstatistik", "en": "SyncPlay session statistics"},
        "explain": {
            "de": "Anzahl SyncPlay-Sitzungen und grobe Teilnehmerzahl-Kategorie -- ohne Rauminhalt/Titel.",
            "en": "Number of SyncPlay sessions and a rough participant-count bracket -- without room content/titles.",
        },
    },
    "detail.upscale": {
        "stage": 3, "group": "upscale",
        "label": {"de": "Upscaling-Details", "en": "Upscaling details"},
        "explain": {
            "de": "Welches Upscaling-Preset verwendet wurde und ob der Vorgang erfolgreich war.",
            "en": "Which upscaling preset was used and whether the operation succeeded.",
        },
    },
    "detail.transcoding": {
        "stage": 3, "group": "transcoding",
        "label": {"de": "Transcoding-Fehler", "en": "Transcoding errors"},
        "explain": {
            "de": "Fehlermeldungen, wenn ein Transcoding-Vorgang (Codec-Umwandlung) fehlschlägt.",
            "en": "Error messages when a transcoding operation (codec conversion) fails.",
        },
    },
    "detail.library": {
        "stage": 3, "group": "library",
        "label": {"de": "Mediathek-Pfadverteilung", "en": "Library path distribution"},
        "explain": {
            "de": (
                "Wie viele deiner eingerichteten Download-Pfade welcher Medienart zugeordnet sind "
                "(z. B. \"3 Pfade Filme & Serien, 1 Pfad eBooks\"), dazu die Gesamtzahl der Pfade "
                "und wie viele davon mehreren Medienarten gleichzeitig zugeordnet sind. "
                "Ausschließlich diese Zahlen -- keine Pfade, keine Ordner-, Laufwerks- oder "
                "Freigabenamen, keine Titel und keine Angabe, wie viele Dateien darin liegen."
            ),
            "en": (
                "How many of your configured download paths are assigned to which media kind "
                "(e.g. \"3 paths movies & series, 1 path eBooks\"), plus the total number of paths "
                "and how many of them are assigned to more than one kind at once. Those numbers and "
                "nothing else -- no paths, no folder, drive or share names, no titles, and no "
                "indication of how many files they hold."
            ),
        },
    },
    "detail.library_scan": {
        "stage": 3, "group": "library_scan",
        "label": {"de": "Bibliotheks-Scan-Details", "en": "Library scan details"},
        "explain": {
            "de": "Scan-Dauer, Anzahl neu gefundener Titel und aufgetretene Fehler bei einem Bibliotheks-Scan.",
            "en": "Scan duration, number of newly found titles and errors encountered during a library scan.",
        },
    },
    "detail.integrations": {
        "stage": 3, "group": "integrations",
        "label": {"de": "Integrations-Verbindungsfehler", "en": "Integration connection errors"},
        "explain": {
            "de": "Verbindungsfehler pro Integration (z. B. \"Crunchyroll-Login fehlgeschlagen\") -- niemals Zugangsdaten.",
            "en": "Connection errors per integration (e.g. \"Crunchyroll login failed\") -- never credentials.",
        },
    },
    "detail.extensions": {
        "stage": 3, "group": "extensions",
        "label": {"de": "Namen geladener Erweiterungen", "en": "Names of loaded extensions"},
        "explain": {
            "de": "Die Namen der geladenen Drittanbieter-Erweiterungsordner (nicht deren Inhalt).",
            "en": "The names of the loaded third-party extension folders (not their content).",
        },
    },
    "detail.self_update": {
        "stage": 3, "group": "self_update",
        "label": {"de": "Selbst-Update-Ergebnis", "en": "Self-update result"},
        "explain": {
            "de": "Ob ein Selbst-Update erfolgreich war oder fehlgeschlagen ist.",
            "en": "Whether a self-update succeeded or failed.",
        },
    },
    "detail.captcha": {
        "stage": 3, "group": "captcha",
        "label": {"de": "Captcha-Lösestatistik", "en": "Captcha solving statistics"},
        "explain": {
            "de": "Erfolgsquote und Häufigkeit der automatischen Captcha-Lösung.",
            "en": "Success rate and frequency of automatic captcha solving.",
        },
    },
    "detail.v1_api": {
        "stage": 3, "group": "v1_api",
        "label": {"de": "API-Nutzungshäufigkeit", "en": "API usage frequency"},
        "explain": {
            "de": "Wie oft die externe REST-API angesprochen wird (welcher Endpunkt, keine übertragenen Inhalte).",
            "en": "How often the external REST API is called (which endpoint, no transferred content).",
        },
    },
    # ---- Stage 4: Download content --------------------------------------
    "downloads.titles": {
        "stage": 4, "group": "downloads",
        "label": {"de": "Download-Titel", "en": "Download titles"},
        "explain": {
            "de": (
                "Welche Serie/welcher Film heruntergeladen wurde, inklusive Anbieter, Staffel/Episode "
                "und Erfolg/Fehlschlag (z. B. \"Serie X, Staffel 2, Episode 4, Anbieter VOE, "
                "erfolgreich\")."
            ),
            "en": (
                "Which show/movie was downloaded, including provider, season/episode and "
                "success/failure (e.g. \"Show X, season 2, episode 4, provider VOE, successful\")."
            ),
        },
    },
    "downloads.errors": {
        "stage": 4, "group": "downloads",
        "label": {"de": "Download-Fehlermeldungen", "en": "Download error messages"},
        "explain": {
            "de": (
                "Die Fehlermeldung zu einer einzelnen fehlgeschlagenen Download-Datei (z. B. "
                "\"Episode 4 konnte nicht heruntergeladen werden: Verbindungsfehler\")."
            ),
            "en": (
                "The error message for a single failed download file (e.g. \"Episode 4 could not "
                "be downloaded: connection error\")."
            ),
        },
    },
    "direct_link.urls": {
        "stage": 4, "group": "direct_link",
        "label": {"de": "Direct-Link-URLs", "en": "Direct link URLs"},
        "explain": {
            "de": "Die über die Direct-Link-Funktion verwendeten URLs (ohne Zugangs-Tokens/Query-Parameter).",
            "en": "The URLs used via the direct-link feature (without access tokens/query parameters).",
        },
    },
    # ---- Stage 5: Playback context --------------------------------------
    "stream.play_events": {
        "stage": 5, "group": "stream",
        "label": {"de": "Play-Events", "en": "Play events"},
        "explain": {
            "de": (
                "Welcher Titel/welche Episode gestartet wurde und wann -- ohne wie lange geschaut "
                "wurde (das ist erst Stufe 6)."
            ),
            "en": (
                "Which title/episode was started and when -- without how long it was watched "
                "(that's stage 6 only)."
            ),
        },
    },
    "syncplay.room_content": {
        "stage": 5, "group": "syncplay",
        "label": {"de": "SyncPlay-Rauminhalt", "en": "SyncPlay room content"},
        "explain": {
            "de": (
                "Welcher Titel in einer SyncPlay-Sitzung läuft (Stufe 3 kennt nur die Sitzung an sich "
                "-- Anzahl/Teilnehmer -- hier kommt der tatsächliche Inhalt/Titel dazu)."
            ),
            "en": (
                "Which title is playing in a SyncPlay session (stage 3 only knows the session "
                "itself -- count/participants -- here the actual content/title is added)."
            ),
        },
    },
    # ---- Stage 6: Watch behaviour -----------------------------------------
    "watch.progress": {
        "stage": 6, "group": "watch",
        "label": {"de": "Wiedergabefortschritt", "en": "Playback progress"},
        "explain": {
            "de": (
                "Der Wiedergabefortschritt (in Prozent) pro Episode/Film -- zusammen mit Titel-Daten "
                "aus Stufe 4/5 ein echtes Nutzungsprofil, was genau du wie weit geschaut hast."
            ),
            "en": (
                "The playback progress (in percent) per episode/movie -- combined with the title "
                "data from stage 4/5, a real usage profile of exactly what you watched and how far."
            ),
        },
    },
    "watch.duration": {
        "stage": 6, "group": "watch",
        "label": {"de": "Watchtime-Summen", "en": "Watchtime totals"},
        "explain": {
            "de": "Wie viele Sekunden/Minuten eines Titels tatsächlich angesehen wurden, aufsummiert.",
            "en": "How many seconds/minutes of a title were actually watched, summed up.",
        },
    },
    "watch.completion": {
        "stage": 6, "group": "watch",
        "label": {"de": "Abschlussquote", "en": "Completion rate"},
        "explain": {
            "de": "Ob eine Episode/ein Film bis zum Ende angesehen wurde (Abschlussquote).",
            "en": "Whether an episode/movie was watched through to the end (completion rate).",
        },
    },
}


# ---------------------------------------------------------------------------
# The source dimension (flag.sources -> flag.source.<id>)
# ---------------------------------------------------------------------------
# The built-in source ids that may appear as a flag.source.<id> data_key. A
# closed list, not a pattern, and deliberately not derived from
# web/source_policy.py at runtime: that list also carries whatever sources
# INSTALLED MODULES registered, and a module id is attacker-controlled text
# that would end up as a feature_key on a public server. Adding a built-in
# source here is a conscious act, exactly like adding any other data_key.
#
# "hanime" is absent on purpose and must stay absent -- see flag.hanime_tv.
SOURCE_FLAG_IDS = (
    "aniworld", "sto", "filmpalast", "megakino", "filmo", "nineanime", "aniwaves",
)

SOURCE_FLAG_PREFIX = "flag.source."

# The single toggle that authorises every one of them.
SOURCE_FLAG_CONSENT_KEY = "flag.sources"


def source_flag_key(source_id) -> str | None:
    """``"filmo"`` -> ``"flag.source.filmo"``; None for anything not in
    SOURCE_FLAG_IDS (including the adult source and every module source)."""
    sid = str(source_id or "").strip().lower()
    if sid not in SOURCE_FLAG_IDS:
        return None
    return SOURCE_FLAG_PREFIX + sid


def is_source_flag_key(data_key) -> bool:
    """True for a well-formed, known ``flag.source.<id>`` key."""
    key = str(data_key or "")
    if not key.startswith(SOURCE_FLAG_PREFIX):
        return False
    return key[len(SOURCE_FLAG_PREFIX):] in SOURCE_FLAG_IDS


def consent_key_for(data_key) -> str:
    """The registry key whose toggle governs *data_key*.

    Identical to *data_key* for everything except the flag.source.* family,
    which is authorised by the single flag.sources toggle -- see its entry
    above for why consent is asked once but sent per source.
    """
    return SOURCE_FLAG_CONSENT_KEY if is_source_flag_key(data_key) else data_key


def keys_for_stage(stage: int):
    """Return the sorted data_keys registered at exactly this stage."""
    return sorted(k for k, v in DATA_REGISTRY.items() if v["stage"] == stage)


def all_togglable_keys():
    """Every data_key the settings UI should render its own toggle for
    (everything except install_id, which is always-on/no-toggle)."""
    return sorted(k for k, v in DATA_REGISTRY.items() if not v.get("always_on"))


def _pick(value, lang):
    """Resolve one of this registry's ``{"de": ..., "en": ...}`` text fields
    to a plain string for ``lang``. Falls back to German (the field always
    has that key) so a typo'd/unsupported lang code degrades gracefully
    instead of raising."""
    if isinstance(value, dict):
        return value.get(lang) or value.get("de") or next(iter(value.values()))
    return value  # already a plain string -- defensive, not expected to hit


def registry_export(lang="de"):
    """JSON-serializable snapshot of the registry + stage metadata, handed to
    the frontend once per settings-page load (see routes/settings.py's
    api_settings_telemetry_get()) so the confirmation dialog's explain texts
    come from this single source, never a second hand-copied string in a
    template. ``lang`` picks which of each field's ``{"de", "en"}`` values to
    serialize -- callers pass the request's current UI locale (see
    ``web/app.py``'s ``get_locale()``) so the frontend never has to
    translate this content on its own."""
    lang = lang if lang in ("de", "en") else "de"
    return {
        "stages": {
            stage: {
                "title": _pick(meta["title"], lang),
                "description": _pick(meta["description"], lang),
            }
            for stage, meta in STAGE_META.items()
        },
        "data_points": {
            k: {"stage": v["stage"], "group": v["group"], "label": _pick(v["label"], lang),
                "explain": _pick(v["explain"], lang), "always_on": bool(v.get("always_on"))}
            for k, v in DATA_REGISTRY.items()
        },
    }
