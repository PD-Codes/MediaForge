"""Telemetry data-point registry.

One entry per ``data_key`` from ``TELEMETRY_PLAN.md`` §2 -- the single source
of truth for what a data point means. The ``explain`` text is written for an
end user (no jargon, no "helps us improve the product" hand-waving) because
it is rendered *verbatim* in the Settings confirmation dialog (see
``static/telemetry.js``) as well as in the first-run consent dialog -- there
is deliberately no second, separately-maintained copy of this wording
anywhere in the frontend.

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
        "title": "Aus",
        "description": "Keine Verbindung zum devInfo-Server, überhaupt keine Daten verlassen dieses Gerät.",
    },
    1: {
        "title": "Absturz & System",
        "description": (
            "Technische Fehlerprotokolle und Basis-Systeminfo, nur nach ausdrücklicher "
            "Zustimmung im Erstkonsens-Dialog. Kein Opt-out-Default -- siehe PRIVACY.md."
        ),
    },
    2: {
        "title": "Feature-Flags",
        "description": "Reine Ja/Nein- bzw. Zähler-Ebene pro Feature -- keine Titel, keine Inhalte.",
    },
    3: {
        "title": "Feature-Details & Fehler",
        "description": "Mehr Kontext zu denselben Features (Laufzeiten, Fehlerzahlen), weiterhin ohne Titel/Inhalte.",
    },
    4: {
        "title": "Download-Inhalte",
        "description": "Welche Serien/Filme heruntergeladen wurden, inkl. Provider und Erfolg/Fehlschlag.",
    },
    5: {
        "title": "Wiedergabe-Kontext",
        "description": "Welcher Titel gerade gestartet wird -- noch ohne Watchtime.",
    },
    6: {
        "title": "Sehverhalten / Watchtime",
        "description": (
            "Wiedergabefortschritt, Watchtime-Summen und Abschlussquote. In Kombination mit der "
            "install_id und den Titel-Daten aus Stufe 4/5 ein echtes Verhaltensprofil -- deutlich "
            "näher an klassischer Streaming-Analytics als an Crash-Reporting. Nicht Teil von "
            "'alles aktivieren', Default für alle drei Punkte: aus."
        ),
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
        "stage": 1, "group": "system", "label": "Installations-ID",
        "always_on": True,
        "explain": (
            "Eine zufällige, auf diesem Gerät einmalig erzeugte Kennung (UUID). Sie wird nur "
            "zusammen mit anderen von dir aktivierten Datenpunkten verschickt, niemals allein, "
            "und ist die technische Voraussetzung dafür, dass Absturzberichte überhaupt einem "
            "wiederkehrenden Gerät zugeordnet werden können. Du kannst sie in den Einstellungen "
            "jederzeit einsehen und per Klick neu generieren (\"Identität zurücksetzen\")."
        ),
    },
    "crash_reports": {
        "stage": 1, "group": "system", "label": "Absturzberichte",
        "explain": (
            "Technischer Fehlerbericht (Programmzeile, Dateiname, Fehlertyp), wenn MediaForge "
            "unerwartet abstürzt oder eine interne Ausnahme auftritt. Enthält niemals Passwörter, "
            "Zugangsdaten, Variableninhalte oder komplette URLs mit Tokens -- nur den bereinigten "
            "technischen Ablauf, der zum Fehler geführt hat. Zusätzlich wird eine kleine "
            "Momentaufnahme des Gerätezustands im Fehlermoment angehängt (freier Arbeitsspeicher "
            "und dessen Auslastung, freier Speicherplatz auf dem Download-Ziel, Systemlast sowie "
            "Anzahl aktiver Threads/Dateihandles), damit erkennbar ist, ob z. B. der Speicher voll "
            "war -- keine Titel, Pfade oder Inhalte."
        ),
    },
    "system_info": {
        "stage": 1, "group": "system", "label": "System-Info",
        "explain": (
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
            "keinen Geräte- oder Benutzernamen, keine IP-Adresse und keine Dateipfade."
        ),
    },
    # ---- Stage 2: Feature flags (usage yes/no + counter) --------------
    "flag.autosync": {
        "stage": 2, "group": "autosync", "label": "AutoSync genutzt",
        "explain": "Nur, dass die AutoSync-Funktion genutzt wird und wie oft -- keine Serientitel.",
    },
    "flag.syncplay": {
        "stage": 2, "group": "syncplay", "label": "SyncPlay genutzt",
        "explain": "Nur, dass gemeinsame SyncPlay-Wiedergabesitzungen genutzt werden und wie oft -- kein Rauminhalt.",
    },
    "flag.upscale": {
        "stage": 2, "group": "upscale", "label": "Upscaling genutzt",
        "explain": "Nur, dass die KI-Videoupscaling-Funktion genutzt wird und wie oft.",
    },
    "flag.transcoding": {
        "stage": 2, "group": "transcoding", "label": "Transcoding genutzt",
        "explain": "Nur, dass Video-Transcoding (Codec-Umwandlung) genutzt wird und wie oft.",
    },
    "flag.library_scan": {
        "stage": 2, "group": "library_scan", "label": "Bibliotheks-Scan genutzt",
        "explain": "Nur, dass ein Bibliotheks-Scan (Mediathek-Abgleich) durchgeführt wurde und wie oft.",
    },
    "flag.calendar": {
        "stage": 2, "group": "calendar", "label": "Kalender genutzt",
        "explain": "Nur, dass die Erscheinungskalender-Funktion geöffnet/genutzt wird und wie oft.",
    },
    "flag.integrations.crunchyroll": {
        "stage": 2, "group": "integrations", "label": "Crunchyroll-Integration genutzt",
        "explain": "Nur, dass die Crunchyroll-Integration aktiv verbunden ist und genutzt wird.",
    },
    "flag.integrations.fernsehserien": {
        "stage": 2, "group": "integrations", "label": "Fernsehserien-Integration genutzt",
        "explain": "Nur, dass die Fernsehserien.de-Integration aktiv verbunden ist und genutzt wird.",
    },
    "flag.integrations.seerr": {
        "stage": 2, "group": "integrations", "label": "Jellyseerr/Overseerr-Integration genutzt",
        "explain": "Nur, dass eine Jellyseerr/Overseerr-Integration aktiv verbunden ist und genutzt wird.",
    },
    "flag.integrations.mediascan": {
        "stage": 2, "group": "integrations", "label": "MediaScan-Integration genutzt",
        "explain": "Nur, dass die MediaScan (Jellyfin/Plex-Abgleich)-Integration aktiv verbunden ist.",
    },
    "flag.push_notifications": {
        "stage": 2, "group": "push_notifications", "label": "Push-Benachrichtigungen genutzt",
        "explain": "Nur, dass Push-Benachrichtigungen (Telegram/Discord/Pushover/ntfy/...) eingerichtet sind und ausgelöst wurden.",
    },
    "flag.uptime_monitor": {
        "stage": 2, "group": "uptime_monitor", "label": "UpTime-Monitoring genutzt",
        "explain": "Nur, dass das eingebaute UpTime-Monitoring der Quellen aktiv ist.",
    },
    "flag.extensions": {
        "stage": 2, "group": "extensions", "label": "Erweiterungen genutzt",
        "explain": "Nur, dass mindestens eine Drittanbieter-Erweiterung (Modul) geladen ist und wie viele.",
    },
    "flag.self_update": {
        "stage": 2, "group": "self_update", "label": "Selbst-Update genutzt",
        "explain": "Nur, dass die Selbst-Update-Funktion ausgeführt wurde und wie oft.",
    },
    "flag.direct_link": {
        "stage": 2, "group": "direct_link", "label": "Direct-Link genutzt",
        "explain": "Nur, dass die Direct-Link-Download-Funktion genutzt wird und wie oft -- ohne die verwendeten URLs.",
    },
    "flag.captcha": {
        "stage": 2, "group": "captcha", "label": "Captcha-Lösung genutzt",
        "explain": "Nur, dass die automatische Captcha-Lösung ausgelöst wurde und wie oft.",
    },
    "flag.v1_api": {
        "stage": 2, "group": "v1_api", "label": "Externe REST-API genutzt",
        "explain": "Nur, dass die externe REST-API (z. B. für Home Assistant) angesprochen wird und wie oft.",
    },
    "flag.hanime_tv": {
        "stage": 2, "group": "hanime_tv", "label": "hanime.tv genutzt (18+)",
        "explain": (
            "Nur ein reiner Nutzungszähler (\"wird genutzt: ja/nein\", wie oft) für den "
            "altersgegateten 18+-Anbieter hanime.tv. Das ist der EINZIGE Telemetrie-Datenpunkt, "
            "der für diesen Anbieter jemals erhoben wird -- keine Titel, keine Fehlermeldungen, "
            "keine Wiedergabezeiten, keine Fortschritts- oder Abschlussdaten, unabhängig davon, "
            "welche anderen Stufen du sonst aktiviert hast. Diese Ausnahme ist fest im Programmcode "
            "verankert (siehe sanitize.is_adult_provider()), keine Einstellung, die versehentlich "
            "hochgestuft werden könnte."
        ),
    },
    # ---- Stage 3: Feature details & errors -----------------------------
    "detail.autosync": {
        "stage": 3, "group": "autosync", "label": "AutoSync-Statistik",
        "explain": "Lauf-Statistik von AutoSync: Anzahl Läufe, Dauer, Fehleranzahl -- weiterhin ohne Serientitel.",
    },
    "detail.syncplay": {
        "stage": 3, "group": "syncplay", "label": "SyncPlay-Sitzungsstatistik",
        "explain": "Anzahl SyncPlay-Sitzungen und grobe Teilnehmerzahl-Kategorie -- ohne Rauminhalt/Titel.",
    },
    "detail.upscale": {
        "stage": 3, "group": "upscale", "label": "Upscaling-Details",
        "explain": "Welches Upscaling-Preset verwendet wurde und ob der Vorgang erfolgreich war.",
    },
    "detail.transcoding": {
        "stage": 3, "group": "transcoding", "label": "Transcoding-Fehler",
        "explain": "Fehlermeldungen, wenn ein Transcoding-Vorgang (Codec-Umwandlung) fehlschlägt.",
    },
    "detail.library_scan": {
        "stage": 3, "group": "library_scan", "label": "Bibliotheks-Scan-Details",
        "explain": "Scan-Dauer, Anzahl neu gefundener Titel und aufgetretene Fehler bei einem Bibliotheks-Scan.",
    },
    "detail.integrations": {
        "stage": 3, "group": "integrations", "label": "Integrations-Verbindungsfehler",
        "explain": "Verbindungsfehler pro Integration (z. B. \"Crunchyroll-Login fehlgeschlagen\") -- niemals Zugangsdaten.",
    },
    "detail.extensions": {
        "stage": 3, "group": "extensions", "label": "Namen geladener Erweiterungen",
        "explain": "Die Namen der geladenen Drittanbieter-Erweiterungsordner (nicht deren Inhalt).",
    },
    "detail.self_update": {
        "stage": 3, "group": "self_update", "label": "Selbst-Update-Ergebnis",
        "explain": "Ob ein Selbst-Update erfolgreich war oder fehlgeschlagen ist.",
    },
    "detail.captcha": {
        "stage": 3, "group": "captcha", "label": "Captcha-Lösestatistik",
        "explain": "Erfolgsquote und Häufigkeit der automatischen Captcha-Lösung.",
    },
    "detail.v1_api": {
        "stage": 3, "group": "v1_api", "label": "API-Nutzungshäufigkeit",
        "explain": "Wie oft die externe REST-API angesprochen wird (welcher Endpunkt, keine übertragenen Inhalte).",
    },
    # ---- Stage 4: Download content --------------------------------------
    "downloads.titles": {
        "stage": 4, "group": "downloads", "label": "Download-Titel",
        "explain": (
            "Welche Serie/welcher Film heruntergeladen wurde, inklusive Anbieter, Staffel/Episode "
            "und Erfolg/Fehlschlag (z. B. \"Serie X, Staffel 2, Episode 4, Anbieter VOE, "
            "erfolgreich\")."
        ),
    },
    "downloads.errors": {
        "stage": 4, "group": "downloads", "label": "Download-Fehlermeldungen",
        "explain": (
            "Die Fehlermeldung zu einer einzelnen fehlgeschlagenen Download-Datei (z. B. "
            "\"Episode 4 konnte nicht heruntergeladen werden: Verbindungsfehler\")."
        ),
    },
    "direct_link.urls": {
        "stage": 4, "group": "direct_link", "label": "Direct-Link-URLs",
        "explain": "Die über die Direct-Link-Funktion verwendeten URLs (ohne Zugangs-Tokens/Query-Parameter).",
    },
    # ---- Stage 5: Playback context --------------------------------------
    "stream.play_events": {
        "stage": 5, "group": "stream", "label": "Play-Events",
        "explain": (
            "Welcher Titel/welche Episode gestartet wurde und wann -- ohne wie lange geschaut "
            "wurde (das ist erst Stufe 6)."
        ),
    },
    "syncplay.room_content": {
        "stage": 5, "group": "syncplay", "label": "SyncPlay-Rauminhalt",
        "explain": (
            "Welcher Titel in einer SyncPlay-Sitzung läuft (Stufe 3 kennt nur die Sitzung an sich "
            "-- Anzahl/Teilnehmer -- hier kommt der tatsächliche Inhalt/Titel dazu)."
        ),
    },
    # ---- Stage 6: Watch behaviour -----------------------------------------
    "watch.progress": {
        "stage": 6, "group": "watch", "label": "Wiedergabefortschritt",
        "explain": (
            "Der Wiedergabefortschritt (in Prozent) pro Episode/Film -- zusammen mit Titel-Daten "
            "aus Stufe 4/5 ein echtes Nutzungsprofil, was genau du wie weit geschaut hast."
        ),
    },
    "watch.duration": {
        "stage": 6, "group": "watch", "label": "Watchtime-Summen",
        "explain": "Wie viele Sekunden/Minuten eines Titels tatsächlich angesehen wurden, aufsummiert.",
    },
    "watch.completion": {
        "stage": 6, "group": "watch", "label": "Abschlussquote",
        "explain": "Ob eine Episode/ein Film bis zum Ende angesehen wurde (Abschlussquote).",
    },
}


def keys_for_stage(stage: int):
    """Return the sorted data_keys registered at exactly this stage."""
    return sorted(k for k, v in DATA_REGISTRY.items() if v["stage"] == stage)


def all_togglable_keys():
    """Every data_key the settings UI should render its own toggle for
    (everything except install_id, which is always-on/no-toggle)."""
    return sorted(k for k, v in DATA_REGISTRY.items() if not v.get("always_on"))


def registry_export():
    """JSON-serializable snapshot of the registry + stage metadata, handed to
    the frontend once per settings-page load (see routes/settings.py's
    api_settings_telemetry_get()) so the confirmation dialog's explain texts
    come from this single source, never a second hand-copied string in a
    template."""
    return {
        "stages": STAGE_META,
        "data_points": {
            k: {"stage": v["stage"], "group": v["group"], "label": v["label"],
                "explain": v["explain"], "always_on": bool(v.get("always_on"))}
            for k, v in DATA_REGISTRY.items()
        },
    }
