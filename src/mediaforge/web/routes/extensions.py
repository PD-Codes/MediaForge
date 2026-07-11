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
web/thirdparties/store.py). Every one of those routes short-circuits when
no store URL is configured — which is the default — so a MediaForge that
was never pointed at a store never talks to one, and its Modulmanager
renders no store UI at all.
"""

from flask import jsonify, render_template, request

from ..db import get_setting
from ..thirdparties import pending_changes, rescan_new_modules
from ..thirdparties import store as module_store
from ..thirdparties.registry import REGISTRY_API_VERSION, resolve_extensions_overview
from ..thirdparties.trusted_keys import ADMIN_KEYS_SETTING as TRUSTED_KEYS_SETTING
from ..thirdparties.trusted_keys import trusted_keys


def _page_context():
    """Everything both the initial render and a post-action re-render need."""
    keys = trusted_keys()
    return {
        "extensions": resolve_extensions_overview(),
        "registry_api": REGISTRY_API_VERSION,
        "store_enabled": module_store.store_enabled(),
        "store_url": module_store.store_url(),
        "extra_urls": module_store.extra_urls(),
        "allow_unverified": module_store.allow_unverified(),
        "pending": pending_changes(),
        # Trusted signing keys: what MediaForge ships, plus what this admin added.
        # Shown so an admin can see *why* a module is (or isn't) official, without
        # reading source or guessing.
        "trusted_keys_raw": get_setting(TRUSTED_KEYS_SETTING, "") or "",
        "trusted_keys": sorted(keys.values(), key=lambda k: (k.get("admin_added", False),
                                                             k.get("name", ""))),
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

    # ---- Module store ------------------------------------------------------
    # All admin-only (see app.py's _admin_only, which also gates the page these
    # back). They still each re-check store_enabled() rather than trusting the
    # UI to hide the buttons — a store that isn't configured has to be closed at
    # the door, not merely invisible.

    @app.route("/api/store/config", methods=["GET", "PUT"])
    def api_store_config():
        """Read/write the store URL + the unverified opt-in.

        Setting the URL to "" is how you turn the store back off completely:
        the UI disappears and every route below starts refusing again.
        """
        from ..db import get_setting, set_setting

        if request.method == "GET":
            return jsonify({
                "url": module_store.store_url(),
                "extra_urls": module_store.extra_urls(),
                "allow_unverified": module_store.allow_unverified(),
                "registry_api": REGISTRY_API_VERSION,
            })

        data = request.get_json(silent=True) or {}
        if "url" in data:
            url = str(data["url"] or "").strip()
            if url and not url.startswith(("http://", "https://")):
                return jsonify({"error": "store URL must start with http:// or https://"}), 400
            set_setting(module_store.STORE_URL_KEY, url)
        if "extra_urls" in data:
            # One repo per line. Anything that isn't an http(s) URL is dropped
            # rather than saved and silently ignored later.
            lines = [line.strip() for line in str(data["extra_urls"] or "").splitlines()]
            urls = [line for line in lines if line.startswith(("http://", "https://"))]
            set_setting(module_store.EXTRA_URLS_KEY, "\n".join(urls))
        if "allow_unverified" in data:
            set_setting(module_store.ALLOW_UNVERIFIED_KEY,
                        "1" if str(data["allow_unverified"]) == "1" else "0")
        if "trusted_keys" in data:
            # The keys whose signatures this install believes, on top of the ones
            # MediaForge ships (see thirdparties/trusted_keys.py). Pasting a key
            # here is the act of deciding to trust it -- so it is validated, not
            # merely stored: a malformed blob would otherwise be silently ignored
            # at verification time and the admin would wonder why their own
            # modules still say "unsigned".
            raw = str(data["trusted_keys"] or "").strip()
            if raw:
                import base64
                import json as _json

                try:
                    entries = _json.loads(raw)
                    if not isinstance(entries, list):
                        raise ValueError("expected a JSON list of keys")
                    for entry in entries:
                        if not entry.get("key_id") or not entry.get("public_key"):
                            raise ValueError("each key needs a key_id and a public_key")
                        if len(base64.b64decode(str(entry["public_key"]), validate=True)) != 32:
                            raise ValueError(
                                f"{entry.get('key_id')}: not an Ed25519 public key "
                                "(expected 32 bytes). If that was a PRIVATE key, treat it "
                                "as compromised and generate a new one.")
                except Exception as exc:
                    return jsonify({"error": f"invalid trusted keys: {exc}"}), 400
            set_setting(TRUSTED_KEYS_SETTING, raw)
        # Any config change invalidates every cached index -- the set of repos we
        # were caching for may not even exist any more.
        module_store._CACHE.clear()
        return jsonify({
            "ok": True,
            "url": module_store.store_url(),
            "extra_urls": module_store.extra_urls(),
            "allow_unverified": module_store.allow_unverified(),
            "trusted_keys": get_setting(TRUSTED_KEYS_SETTING, "") or "",
            "enabled": module_store.store_enabled(),
        })

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
        """Download + verify + stage a module for the next start. The install
        is NOT live -- see web/thirdparties/__init__.py's
        apply_pending_changes()."""
        if not module_store.store_enabled():
            return jsonify({"ok": False, "error": "no store configured"}), 400
        data = request.get_json(silent=True) or {}
        module_id = str(data.get("id") or "").strip()
        if not module_id:
            return jsonify({"ok": False, "error": "missing module id"}), 400
        result = module_store.install(module_id, force=str(data.get("force")) == "1")
        result["pending"] = pending_changes()
        return jsonify(result), (200 if result["ok"] else 400)

    @app.route("/api/store/uninstall", methods=["POST"])
    def api_store_uninstall():
        """Stage a module folder for removal at the next start. Works for
        hand-installed and broken modules too, not just store ones -- so it is
        deliberately not gated on store_enabled()."""
        data = request.get_json(silent=True) or {}
        result = module_store.uninstall(str(data.get("folder") or ""))
        result["pending"] = pending_changes()
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
