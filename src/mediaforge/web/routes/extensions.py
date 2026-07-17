"""Extensions overview — admin page listing every discovered
web/thirdparties/<name>/ folder and its load status, plus the module store.

# TODO(telemetry): wire up flag.extensions (usage counter, how many
# extensions loaded) and detail.extensions (names of loaded thirdparty
# folders) -- see telemetry/registry.py. Registry-only for now. Note: this
# is about THIS route module only, not the Module Store/Manager code under
# web/thirdparties/ itself, which is explicitly out of scope for telemetry.

Extracted as a plain route-registration function (no Flask blueprint,
same convention as routes/integrations.py) so it can be dropped into
create_app() with one register_extensions_routes(app) call.

Unlike a plugin's own settings card (which only shows up once it
successfully registered), this page reads
web/thirdparties/registry.py's resolve_extensions_overview(), which is
fed by every phase of web/thirdparties/__init__.py's
discover_and_register() — so a folder that failed to import, had no
register(app), or was skipped for an unmet DEPENDS_ON, an unsupported
MediaForge/registry-API version or a missing pip dependency still shows
up here, with the reason. It's meant as the "why isn't my integration
showing up" page.

The /api/store/* half is the module store client (see
web/thirdparties/store.py). The official store's address and the keys whose
signatures make a module "Official" are both compiled into the build
(store.py's DEFAULT_STORE_URL, trusted_keys.py's BUILTIN_KEYS) — this file
exposes them read-only and refuses to write them. An admin may add their own
extra repositories and opt into unverified modules; that is the whole of what
is configurable, and neither can promote anything to Official.
"""

from flask import jsonify, render_template, request

from ..thirdparties import (
    MODULES_DIR,
    install_module_requirements,
    install_staged_live,
    pending_changes,
    rescan_new_modules,
    uninstall_module_live,
)
from ..thirdparties import deps as module_deps
from .. import restart as web_restart
from ..thirdparties import store as module_store
from ..thirdparties.registry import REGISTRY_API_VERSION, resolve_extensions_overview
from ..thirdparties.trusted_keys import trusted_keys


def _active_job_count() -> int:
    """How many downloads/upscales a restart would cancel right now.

    Imported lazily and defensively: this is a courtesy warning for the confirm dialog,
    and a failure to count must never be the reason an admin cannot finish an upgrade.
    """
    try:
        from ..runtime_state import (
            _active_cancel_events,
            _active_cancel_events_lock,
            _upscale_active_cancel_events,
            _upscale_cancel_lock,
        )
        with _active_cancel_events_lock:
            downloads = len(_active_cancel_events)
        with _upscale_cancel_lock:
            upscales = len(_upscale_active_cancel_events)
        return downloads + upscales
    except Exception:
        return 0


def _page_context():
    """Everything both the initial render and a post-action re-render need."""
    return {
        "extensions": resolve_extensions_overview(),
        "registry_api": REGISTRY_API_VERSION,
        "store_enabled": module_store.store_enabled(),
        "store_url": module_store.store_url(),
        "extra_urls": module_store.extra_urls(),
        "allow_unverified": module_store.allow_unverified(),
        "pending": pending_changes(),
        # Where modules actually live now — outside the source tree, next to the
        # database and image_cache. Worth showing: "drop a folder here" is the whole
        # manual-install procedure, and an admin should not have to read the source to
        # find out where "here" is.
        "modules_dir": str(MODULES_DIR),
        # Whether the "Restart now" button can do anything here. A button that silently
        # does nothing is worse than no button, so the template hides it and says how to
        # restart instead — see web/restart.py's restart_supported().
        "restart_supported": web_restart.restart_supported(),
        # Read-only: the keys this *build* ships (thirdparties/trusted_keys.py). Shown
        # so an admin can see why a module is (or isn't) official — not so they can
        # change it. There is deliberately no route that writes this list; a trust root
        # a user can edit is one an attacker can talk them into editing.
        "trusted_keys": sorted(trusted_keys().values(), key=lambda k: k.get("name", "")),
    }


def register_extensions_routes(app):
    """Register the Extensions overview page and the store API on the app."""

    @app.route("/extensions")
    def extensions_page():
        """Render the Module Manager. Route: GET /extensions.

        Deliberately does *not* fetch the store index server-side: a slow or
        unreachable store would then delay (or, worse, hang) a page whose main
        job is showing locally installed modules. The store section loads
        itself over /api/store/catalog once the page is up — and only if a
        store is configured at all.
        """
        return render_template("extensions.html", **_page_context())

    @app.route("/module-settings")
    def module_settings_page():
        """Render the Module Settings page. Route: GET /module-settings.

        Menu rework: modules that register settings for the main Settings page
        (settings_host="settings") no longer show up inside the Settings tabs.
        Their cards are collected by registry.resolve_module_settings() (exposed
        to the template as module_settings_cards via app.py's context
        processor) and rendered here instead, reachable from the Module Manager
        sub-menu. Admin-only, gated by app.py's _admin_only set exactly like
        extensions_page / settings_page.
        """
        return render_template("module_settings.html")

    @app.route("/api/extensions/rescan", methods=["POST"])
    def api_extensions_rescan():
        """Modulmanager's "Refresh" button -- scans web/thirdparties/ for
        folders not yet registered and registers them live, no app restart
        needed. See web/thirdparties/__init__.py's rescan_new_modules()
        docstring for exactly what this can and can't do (adding a new
        folder: yes; picking up code changes to or fully removing an
        already-registered one: no, both still need a restart)."""
        new_names = rescan_new_modules(app)
        return jsonify({"new_modules": new_names, "extensions": resolve_extensions_overview()})

    @app.route("/api/extensions/install-deps", methods=["POST"])
    def api_extensions_install_deps():
        """Install the Python dependencies a module declares in
        MODULE_REQUIREMENTS, then bring the module up -- the Modulmanager's
        "Install dependency" button.

        Admin-only (see app.py's _admin_only): this downloads code from PyPI and
        makes it importable in the MediaForge process. That is a decision an
        admin makes deliberately, which is the whole reason it is a button and
        not something a module can trigger by being installed.

        The request names a *module folder*, never a package: the requirement
        strings come from the module's own MODULE_REQUIREMENTS, so this endpoint
        cannot be used to pip-install an arbitrary package. The packages land in
        ~/.mediaforge/module_deps/, which is appended to sys.path -- MediaForge's
        own dependencies always win an import (see thirdparties/deps.py).
        """
        data = request.get_json(silent=True) or {}
        name = str(data.get("folder") or data.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "missing module folder"}), 400

        result = install_module_requirements(app, name)
        result["extensions"] = resolve_extensions_overview()
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.route("/api/extensions/deps")
    def api_extensions_deps():
        """Where module dependencies live and whether this build can install any
        -- so the Modulmanager can say "no pip in this build" instead of showing
        a button that does nothing."""
        installable, reason = module_deps.pip_available()
        return jsonify({
            "ok": True,
            "installable": installable,
            "reason": reason,
            "dir": str(module_deps.MODULE_DEPS_DIR),
        })

    # ---- Module store ------------------------------------------------------
    # All admin-only (see app.py's _admin_only, which also gates the page these
    # back). They still each re-check store_enabled() rather than trusting the
    # UI to hide the buttons — a store that isn't configured has to be closed at
    # the door, not merely invisible.

    @app.route("/api/store/config", methods=["GET", "PUT"])
    def api_store_config():
        """The two things an admin may configure: their own extra repositories, and
        whether unverified modules may be installed at all.

        Deliberately NOT writable here, and with no route anywhere else that writes
        them either:

        - **the official store URL** — it is a constant in thirdparties/store.py.
          A settings field for it would mean "talk someone into pasting a URL and
          their official modules now come from you".
        - **the trusted signing keys** — they are BUILTIN_KEYS in
          thirdparties/trusted_keys.py, shipped with the build. A trust root a user
          can edit is a trust root an attacker can talk them into editing, and the
          "Official" badge would then mean nothing at all.

        Both are still *readable* (the GET below, and the page context), because an
        admin should be able to see what their install trusts. Seeing is not editing.
        """
        from ..db import set_setting

        if request.method == "GET":
            return jsonify({
                "url": module_store.store_url(),            # read-only, from code
                "extra_urls": module_store.extra_urls(),
                "allow_unverified": module_store.allow_unverified(),
                "registry_api": REGISTRY_API_VERSION,
            })

        data = request.get_json(silent=True) or {}

        # An older client (or a curious admin with curl) sending "url"/"trusted_keys"
        # gets told no, rather than having it silently ignored — a request that looks
        # like it worked but didn't is how you end up debugging the wrong thing.
        for locked, where in (("url", "thirdparties/store.py (DEFAULT_STORE_URL)"),
                              ("trusted_keys", "thirdparties/trusted_keys.py (BUILTIN_KEYS)")):
            if locked in data:
                return jsonify({
                    "error": f"'{locked}' is not configurable — it is compiled into this build. "
                             f"Change it in {where} and ship a new release."
                }), 400

        if "extra_urls" in data:
            # One repo per line. Anything that isn't an http(s) URL is dropped rather
            # than saved and silently ignored later.
            lines = [line.strip() for line in str(data["extra_urls"] or "").splitlines()]
            urls = [line for line in lines if line.startswith(("http://", "https://"))]
            set_setting(module_store.EXTRA_URLS_KEY, "\n".join(urls))
        if "allow_unverified" in data:
            set_setting(module_store.ALLOW_UNVERIFIED_KEY,
                        "1" if str(data["allow_unverified"]) == "1" else "0")

        # Any config change invalidates every cached index -- the set of repos we were
        # caching for may not even exist any more.
        module_store._CACHE.clear()
        return jsonify({
            "ok": True,
            "url": module_store.store_url(),
            "extra_urls": module_store.extra_urls(),
            "allow_unverified": module_store.allow_unverified(),
            "enabled": module_store.store_enabled(),
        })

    @app.route("/api/store/requirements", methods=["POST"])
    def api_store_requirements():
        """Install the pip dependencies of a module that is still in the store.

        The other half of the button that already exists for installed modules
        (/api/extensions/requirements). Without this one, the store showed a module as
        "Incompatible — missing dependency: discord.py>=2.0" and left the admin to work out
        how to fix that from outside the app. In Docker that is a genuinely awkward errand,
        and the answer people reach for — pip install into the container — is undone by the
        next image pull.

        The request names a *module*, never a package. The requirement strings come from that
        module's own declared MODULE_REQUIREMENTS as published in the store index, so this
        cannot be turned into "pip install anything you like on my server" by an admin with a
        curl command, let alone by anyone else. They land in ~/.mediaforge/thirdparty-deps/,
        which is appended (never prepended) to sys.path — MediaForge's own copy of a library
        always wins an import.
        """
        if not module_store.store_enabled():
            return jsonify({"ok": False, "error": "no store configured"}), 400

        installable, reason = module_deps.pip_available()
        if not installable:
            return jsonify({"ok": False, "error": reason}), 400

        data = request.get_json(silent=True) or {}
        module_id = str(data.get("id") or "").strip()
        if not module_id:
            return jsonify({"ok": False, "error": "missing module id"}), 400

        result = module_store.install_requirements(module_id)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.route("/api/store/catalog")
    def api_store_catalog():
        """The store index merged with what's installed here. ?refresh=1
        bypasses the 15-minute cache."""
        if not module_store.store_enabled():
            return jsonify({"ok": False, "error": "no store configured", "modules": []}), 200
        force = request.args.get("refresh") == "1"
        return jsonify(module_store.catalog(force=force))

    @app.route("/api/store/install", methods=["POST"])
    def api_store_install():
        """Download + verify + install a module — live, without a restart.

        The download is still staged into ``_pending/`` first (that is where
        signature verification happens, and a package that fails it never
        reaches the live folder), but a module the running process has not
        imported yet is then moved into place and registered immediately:
        install_staged_live() → rescan_new_modules() → its blueprint, settings
        card, sidebar link and translations are all live on the next request.

        The one case that still needs a restart is an UPGRADE/reinstall of a
        module that is already loaded: Flask can add a blueprint to a running
        app but never replace one, so that stays staged and the "restart
        required" banner appears for it exactly as before. ``restart_required``
        in the response says which of the two happened.
        """
        if not module_store.store_enabled():
            return jsonify({"ok": False, "error": "no store configured"}), 400
        data = request.get_json(silent=True) or {}
        module_id = str(data.get("id") or "").strip()
        if not module_id:
            return jsonify({"ok": False, "error": "missing module id"}), 400
        result = module_store.install(module_id, force=str(data.get("force")) == "1")

        if result.get("ok"):
            applied = install_staged_live(app, result.get("folder"))
            result["live"] = result.get("folder") in applied["live"]
            result["restart_required"] = not result["live"]
            if applied["failed"]:
                # Downloaded and verified, but it won't run here (bad code,
                # unmet DEPENDS_ON, ...). Still "ok" — it IS installed — but the
                # admin gets told, and the Modulmanager card carries the reason.
                result["warning"] = "; ".join(str(f) for f in applied["failed"])

        result["pending"] = pending_changes()
        result["extensions"] = resolve_extensions_overview()
        return jsonify(result), (200 if result["ok"] else 400)

    @app.route("/api/store/uninstall", methods=["POST"])
    def api_store_uninstall():
        """Remove a module — live, without a restart.

        It is switched OFF before anything is deleted (master toggle to "0" +
        on_disable(app), so its workers stop while its code still exists), then
        unregistered from the UI, its settings purged and its folder deleted.
        See web/thirdparties/__init__.py's uninstall_module_live().

        Only if the folder itself cannot be deleted (a file still held open —
        Windows) does the deletion fall back to being staged for the next start;
        the module is off and gone from the UI either way. ``restart_required``
        reports which.

        Works for hand-installed and broken modules too, not just store ones --
        so it is deliberately not gated on store_enabled().
        """
        data = request.get_json(silent=True) or {}
        result = uninstall_module_live(app, str(data.get("folder") or ""))
        result["pending"] = pending_changes()
        result["extensions"] = resolve_extensions_overview()
        return jsonify(result), (200 if result["ok"] else 400)

    @app.route("/api/store/pending", methods=["GET", "DELETE"])
    def api_store_pending():
        """What's staged for the next restart -- and, on DELETE, drop it all
        again (nothing in _pending/ is live, so discarding it is free)."""
        if request.method == "DELETE":
            result = module_store.cancel_pending()
            result["pending"] = pending_changes()
            return jsonify(result), (200 if result["ok"] else 400)
        return jsonify({"ok": True, "pending": pending_changes()})

    @app.route("/api/store/restart", methods=["POST"])
    def api_store_restart():
        """Finish a staged upgrade by replacing this process (see web/restart.py).

        The restart is the mechanism, not a convenience: an upgrade cannot be applied
        to a process that has already imported the old version of the module, so the
        only correct way to run the new code is to stop being this process. The button
        exists so that "restart required" ends in a restart instead of in a banner the
        admin learns to ignore while running last week's module.

        Answers *before* it goes: the response is flushed, then the socket closes. The
        page polls /api/health until the new process answers.
        """
        if not web_restart.restart_supported():
            return jsonify({
                "ok": False,
                "error": "This MediaForge cannot restart itself (debug mode, or it is "
                         "embedded in another server). Restart it the way you started it — "
                         "the staged changes are applied on the next start either way.",
            }), 400

        # What a restart costs right now, so the UI can ask rather than surprise. Running
        # downloads and upscales are cancelled by the shutdown (their ffmpeg/Chromium
        # children would otherwise be orphaned), so this is a real price, not a formality.
        result = web_restart.request_restart(delay=1.0)
        result["active_jobs"] = _active_job_count()
        return jsonify(result), (200 if result["ok"] else 400)

    @app.route("/api/health")
    def api_health():
        """Cheap liveness probe. Exists because the restart button needs something to
        poll: "is the new process up yet" has to be answerable without a session, a
        database round-trip or a template render — otherwise the page reloads into a
        half-started app and the admin sees an error instead of their module.

        Also the endpoint a Docker HEALTHCHECK or a reverse proxy would want, which is
        why it is deliberately not under /api/store/.
        """
        return jsonify({
            "ok": True,
            "restart_pending": web_restart.restart_pending(),
            "restart_supported": web_restart.restart_supported(),
        })
