"""Is a third-party module's registered capability currently active?

Every secondary registry in MediaForge -- content providers, search sources,
hosters, mirrors, subtitle sources, home-feed sources, home panels, uptime
targets, image-host allowlist entries -- is a plain dict keyed by the module's
``item_id`` (the id it passed to ``register_thirdparty()``). Those dicts are
filled from a module's ``register(app)``.

The catch: ``web/thirdparties/__init__.py``'s ``_register_modules()`` calls
``register(app)`` for **every** discovered module, regardless of whether the
module is switched on. The enabled flag was only ever consulted when *rendering*
(sidebar entry, settings card, dashboard widget) and when starting a background
worker. So a module that was toggled off in the Modulmanager -- or that was
already off when the app booted -- kept its provider in the provider picker, its
site in the UpTime dashboard and the DNS test, and its hosts on the image
proxy's allowlist.

Cleaning the registries out on the disable *edge* would not fix the boot case
(nothing toggles, so no edge fires), and re-running ``register(app)`` on the
enable edge would mean re-registering blueprints on a live Flask app. Gating at
the point of use holds in both directions and needs no lifecycle bookkeeping.

Usage from a core module (lazy import inside the function keeps ``mediaforge``
importable without the web package)::

    from .module_gate import module_item_enabled

    def iter_things():
        return [t for item_id, t in _EXTRA_THINGS.items()
                if module_item_enabled(item_id)]

Fail-open is deliberate here: an id that is not a registered thirdparty item has
no enabled flag to read, and a transient DB error must not silently disable half
the app. A missing provider is a broken install; a provider that lingers one
request too long is not a security problem.

``web/routes/image_proxy.py`` needs the opposite default and therefore does
**not** use this module -- it spells the check out itself, fail-closed, because
its allowlist decides which hosts the server will fetch from on request. See the
helper there.
"""

from .logger import get_logger

logger = get_logger(__name__)


def module_item_enabled(item_id) -> bool:
    """True if the module capability registered under *item_id* should be used.

    Delegates to ``web.thirdparties.registry.item_enabled()``, which checks the
    item's ``enabled_setting_key`` plus its ``requires_enabled`` dependencies --
    the same condition that decides whether the module's sidebar link shows.

    Returns True when the registry is unavailable (CLI/headless use, tests) or
    when *item_id* is not a registered item.
    """
    try:
        from .web.thirdparties.registry import item_enabled
    except Exception:
        return True
    try:
        return item_enabled(item_id)
    except Exception:
        logger.exception("[ModuleGate] enabled check failed for %r", item_id)
        return True


def filter_enabled(mapping) -> dict:
    """``{item_id: value}`` reduced to the entries whose module is switched on."""
    return {k: v for k, v in list(mapping.items()) if module_item_enabled(k)}
