"""MediaForge telemetry client SDK.

Optional, consent-gated reporting of crashes and (depending on what the user
has explicitly opted into) feature usage, download activity and watch
behaviour to the devInfo server (the same server ``web/devinfos_monitor.py``
already polls for changelog posts, see ``registry.py``'s
``DEVINFOS_SERVER_URL`` import). Nothing in this package ever sends a single
byte unless the user has actively granted consent (see ``settings.py``) —
before that, every event builder in ``events.py`` returns ``None`` and
``client.TelemetryClient.submit()`` refuses to enqueue anything.

Module map:
    registry.py   Data-point registry (data_key -> stage/group/label/explain
                  text shown verbatim in the Settings confirmation dialog)
                  and the shared, non-secret project key/URLs.
    sanitize.py   Traceback/path/URL sanitizing + the hard-coded
                  ``is_adult_provider()`` guard for the hanime_tv exclusion.
    settings.py   Thin wrapper around the existing DB-first settings store
                  (install_id, consent, enabled_keys).
    client.py     Bounded queue + background worker thread that batches and
                  POSTs events via GLOBAL_SESSION.
    events.py     Typed event builders (crash, feature flag/detail, download,
                  play, watch) — the only place event payloads are assembled.
    hooks.py      sys.excepthook / Flask error handler / worker-thread
                  decorator wiring, plus init_telemetry(app) called once from
                  create_app().

See ``TELEMETRY_PLAN.md`` and ``TELEMETRY_IMPLEMENTATION_PLAN.md`` (in the
sibling devinfo_server checkout) for the full design this package implements.
"""
