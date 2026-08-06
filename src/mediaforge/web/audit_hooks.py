"""Wire the audit log into the events that were not being recorded.

The audit log started as a record of *administrative* actions -- logins, user
and group changes, backups, API keys. That is the conventional scope, and it is
the wrong one here, because it answers a question nobody was asking. The
questions people actually bring to this log are "why did that download never
finish", "who turned that setting off", "when did this module get disabled" --
and none of those left a trace. Worse, the ones that *were* recorded became
misleading in isolation: a permission change at 03:12 looks meaningful until you
learn a setting was changed at 03:11, which the log did not know about.

So the scope is now "every state change", with one deliberate exception:
navigation. Clicking through pages is not a state change, it is a page view, and
recording it would bury the events that matter under noise -- which is the other
way to make an audit log useless.

Everything here hangs off a hook that already existed, rather than a call added
to each site:

* settings -- ``db.settings.add_setting_listener()``, so *every* write is
  covered, including ones from code paths written after this.
* downloads -- ``download_history._record_download_history()``, the single
  function every outcome (done, failed, cancelled) already funnels through.
* modules, workers, lifecycle -- called directly from the few places that own
  those transitions.

Sensitive values never reach the log: the settings hook runs each value through
``audit.redact()`` and treats anything ``is_sensitive_key()`` says is a secret as
unloggable, so extending the audit log did not quietly turn it into a place
where API tokens accumulate in plain text.
"""

from __future__ import annotations

import contextlib
import threading

from ..logger import get_logger
from . import audit as _audit

logger = get_logger(__name__)

_installed = False

# Settings that change on their own, constantly, as a side effect of normal
# operation rather than because somebody decided something. Logging these would
# add thousands of rows a day and push the interesting ones out of the retention
# window -- the exact failure mode that makes people stop reading an audit log.
_SETTING_NOISE_PREFIXES = (
    "uptime_last_",        # written by every monitor round
    "devinfos_last_",      # written by every poll
    "_internal_",
)

_SETTING_NOISE_KEYS = frozenset({
    "library_cache_updated",
    "tmdb_cache_last_evicted",
    "last_seen_version",
})


def _is_noise(key: str) -> bool:
    if key in _SETTING_NOISE_KEYS:
        return True
    return any(key.startswith(p) for p in _SETTING_NOISE_PREFIXES)


# Set while a bulk replay of settings is in progress (a backup restore, a
# settings-profile import). See suppressed_setting_audit().
_suppress_depth = 0
_suppress_lock = threading.Lock()


@contextlib.contextmanager
def suppressed_setting_audit():
    """Silence the per-setting audit hook for a bulk replay.

    A restore writes every stored setting in one go. Recording each one is
    technically accurate and practically useless: it buries the events somebody
    would actually go looking for under a wall of identical rows, in a log that
    has a retention window. The caller writes one summary entry instead.

    Deliberately process-wide rather than thread-local: the settings listener
    chain dispatches some handlers onto their own threads, and a suppression
    that did not follow them would leak exactly the rows it was meant to
    suppress. A restore is not concurrent with normal settings edits in any
    realistic deployment, and the failure mode if it were -- one manual change
    missing from the log during a restore -- is much smaller than the one being
    fixed.
    """
    global _suppress_depth
    with _suppress_lock:
        _suppress_depth += 1
    try:
        yield
    finally:
        with _suppress_lock:
            _suppress_depth -= 1


def _on_setting_changed(key: str, value) -> None:
    """Record one settings write. Must never raise -- it runs inside the HTTP
    request that saved the setting, and a broken audit hook has no business
    making a settings save fail."""
    try:
        if _suppress_depth > 0:
            return
        if _is_noise(key):
            return

        from .db import is_sensitive_key

        # A secret is recorded as having changed, never as what it changed to.
        # "The Telegram token was replaced at 14:02" is the useful fact; the
        # token itself would turn the audit log into a second place a stolen
        # database hands over credentials.
        if is_sensitive_key(key):
            shown = "<secret changed>"
        else:
            shown = _audit.redact({key: value}).get(key)

        # A module's own setting is namespaced "module:<id>:<name>" -- filed
        # under "module" so "what did this module change" is one filter away.
        category = "module" if str(key).startswith("module:") else "settings"
        _audit.audit(category, "setting_changed", target=str(key)[:200],
                     detail={"value": shown}, severity="info")
    except Exception:
        logger.debug("[Audit] settings hook failed for %r", key, exc_info=True)


def record_download(item: dict, status: str) -> None:
    """One finished download, whatever the outcome.

    Called from download_history._record_download_history(), which is the one
    place every exit path already meets -- hooking the worker instead would
    have meant seven call sites and a guarantee that the eighth, added later,
    would be missed.
    """
    try:
        outcome = "success" if str(status).lower() in ("completed", "done", "success") else "failure"
        severity = "info" if outcome == "success" else "warning"
        _audit.audit("download", "download_" + str(status).lower()[:40],
                     target=str(item.get("title") or item.get("url") or "")[:200],
                     detail={
                         "provider": item.get("provider"),
                         "episodes": item.get("episode_count"),
                         "queue_id": item.get("id"),
                     },
                     severity=severity, outcome=outcome)
    except Exception:
        logger.debug("[Audit] download hook failed", exc_info=True)


def record_job(category: str, action: str, title: str, **detail) -> None:
    """An encoding/upscale job transition. Thin on purpose -- the worker knows
    what happened, this only decides how it is filed."""
    try:
        severity = "warning" if action.endswith("failed") else "info"
        _audit.audit(category, action, target=str(title or "")[:200],
                     detail=detail, severity=severity,
                     outcome="failure" if action.endswith("failed") else "success")
    except Exception:
        logger.debug("[Audit] job hook failed for %s/%s", category, action, exc_info=True)


def record_module(action: str, name: str, **detail) -> None:
    """Install, upgrade, uninstall, enable, disable.

    The "module" category has existed since the audit log was written and was
    never once used, which is why a module silently disabling itself left no
    trace at all.
    """
    try:
        _audit.audit("module", action, target=str(name or "")[:200],
                     detail=detail, severity="notice")
    except Exception:
        logger.debug("[Audit] module hook failed for %s", action, exc_info=True)


def record_lifecycle(action: str, **detail) -> None:
    """App start, restart, shutdown, self-update.

    Worth its own category because it is the first thing to check when a gap
    appears in the rest of the log: "nothing happened for two hours" and "the
    app was not running for two hours" look identical otherwise.
    """
    try:
        _audit.audit("lifecycle", action, detail=detail, severity="notice",
                     actor_type="system", actor_name="mediaforge")
    except Exception:
        logger.debug("[Audit] lifecycle hook failed for %s", action, exc_info=True)


def install() -> None:
    """Subscribe the hooks that are subscriptions rather than direct calls.

    Idempotent: app.py may be imported more than once under the reloader, and
    a double subscription would log every setting twice.
    """
    global _installed
    if _installed:
        return
    _installed = True
    try:
        from .db import add_setting_listener

        add_setting_listener(_on_setting_changed)
    except Exception:
        logger.exception("[Audit] Could not install the settings listener")
