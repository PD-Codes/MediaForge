"""Example Hooks -- reference module for
web/thirdparties/registry.py's register_notification_channel() and
register_event_hook(): the two extension points a module uses to react to
MediaForge's own lifecycle events (on_completed, on_errors, on_autosync, ...)
without polling anything itself.

Settings-only, same "smallest possible" shape as example_attach_tab/ next to
this folder: no Blueprint, no routes.py, no templates/ -- the generic
GET/PUT /api/settings/thirdparty/<item_id> pair is enough for this module's
one demo toggle. Safe to enable: both hooks below only ever log, they never
make a network call or write outside the log.

Copy this folder into ``web/thirdparties/`` (or ship it as a module) to
activate it; the auto-discovery loader picks up any folder exposing
``register(app)``.
"""
from ..registry import (
    register_thirdparty,
    register_notification_channel,
    register_event_hook,
)
from ...logger import get_logger

logger = get_logger(__name__)

ITEM_ID = "example_hooks"
ENABLED_KEY = "example_hooks_enabled"

# See ../example_integration/__init__.py for the full meaning of every
# MODULE_* constant.
MODULE_NAME = "Example Hooks"
MODULE_DESCRIPTION = ("Reference module for register_notification_channel() and "
                      "register_event_hook() -- reacting to MediaForge's own events "
                      "(download completed/errored, AutoSync found episodes, ...).")
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False


def _demo_channel(title, body, event, username=None, status=None,
                   episode_count=0, errors=None, is_movie=False):
    """A "notification channel": called by notify_all() with the exact same
    arguments every built-in channel (WebPush/Telegram/Pushover/ntfy/
    WhatsApp/Discord) gets. A real channel would send *body* somewhere
    (a webhook, a chat API, ...) -- this one just logs, so the example is
    safe to enable and never makes a network call.

    Always start with your own enabled check, same as every built-in
    notify_* function -- registering the hook does not imply "always on".
    """
    if not _enabled():
        return
    logger.info("[ExampleHooks] channel: event=%s title=%r status=%s", event, title, status)


def _demo_event_hook(title, body, event, username=None, status=None,
                      episode_count=0, errors=None, is_movie=False):
    """A "lifecycle event hook": same call signature as a notification
    channel, but for a reaction that isn't itself a message -- auto-tagging,
    kicking off an external automation, updating this module's own state.
    Registered per-event (see register(app) below); a module can register
    more than one, one per event of interest.
    """
    if not _enabled():
        return
    logger.info("[ExampleHooks] on_completed hook fired: title=%r episode_count=%s", title, episode_count)


def _enabled() -> bool:
    from ...db import get_setting
    return get_setting(ENABLED_KEY, "0") == "1"


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app). No
    app.register_blueprint(...) call here -- there is no Blueprint, because
    there's no page or API route to serve (same as example_attach_tab/)."""
    register_thirdparty(
        item_id=ITEM_ID,
        label="Example Hooks",
        # No endpoint / icon_svg -- settings-only, no page, no sidebar item.
        enabled_setting_key=ENABLED_KEY,
        badges=[("Demo", "#2e51a2"), ("Hooks", "#16a34a")],
        description=(
            "Settings-only reference: registers one notification channel and one "
            "event hook. Both just log -- enable it and trigger a download/"
            "AutoSync event to see the log lines, no network calls made."
        ),
        enable_label="Enable Example Hooks",
        enable_desc="Logs every notify_all() event through a demo channel and a demo on_completed hook.",
        # No settings_tab given -> default ("thirdparty", the shared Third
        # Party tab), same default as an integration that doesn't need its
        # own tab.
    )

    # A channel: fired for every notify_all() call, same as the six built-in
    # channels (WebPush/Telegram/Pushover/ntfy/WhatsApp/Discord).
    register_notification_channel(ITEM_ID, _demo_channel)

    # An event hook: fired only for "on_completed" (queue_worker.py's
    # download-finished event). Register once per event you care about --
    # see web/notifications.py's module docstring for the full event list
    # (on_completed, on_errors, on_cancelled, on_autosync, on_sync_hold,
    # on_sync_resume).
    register_event_hook(ITEM_ID, "on_completed", _demo_event_hook)
