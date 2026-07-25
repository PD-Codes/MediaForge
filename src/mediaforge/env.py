"""Environment-variable handling.

Two jobs live here:

1. Mirroring legacy ``ANIWORLD_*`` environment variables onto their
   ``MEDIAFORGE_*`` equivalents (the project was renamed from "AniWorld
   Downloader" to "MediaForge").
2. Loading a *not yet migrated* ``~/.mediaforge/.env`` once, so
   ``web.settings_migration._migrate_dotenv_to_db()`` can import its values
   into the ``app_settings`` table.

**The DB is the source of truth for configuration.** A ``.env`` file is a
legacy input that is imported once and then retired (renamed to
``.env.imported`` by the migration), never a permanent config source.
Real environment variables — Docker ``-e``, shell exports, systemd — keep
working normally and are what remains for the handful of settings that have
no WebUI equivalent (``MEDIAFORGE_REDIS_URL``, ``MEDIAFORGE_HTTPS``,
``MEDIAFORGE_USER_AGENT``, ...).

History: this module used to hold ``merge_env()``, which rewrote the user's
``.env`` from a shipped ``.env.example`` template and re-loaded it into
``os.environ`` on *every* start. That predates the DB-backed settings and
actively fought them:

* the rewrite dropped every key that was not in the template, silently
  deleting user configuration on the next start, and
* the reload meant a stale ``.env`` permanently overrode the WebUI for any
  key that ``_sync_db_settings_to_env()`` does not write back.

Both the function and the ``.env.example`` template are gone; the supported
variables are documented in the wiki instead.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Backwards compatibility: all configuration variables moved from the
# ANIWORLD_ prefix to MEDIAFORGE_ on the rename. Old ANIWORLD_ variables are
# still honoured as a fallback so existing Docker setups and shell exports
# keep working.
LEGACY_PREFIX = "ANIWORLD_"
NEW_PREFIX = "MEDIAFORGE_"


def mirror_legacy_env():
    """Mirror any legacy ANIWORLD_* variables to their MEDIAFORGE_* counterpart.

    Only fills in a MEDIAFORGE_* value when it is not already set, so an
    explicit new-style variable always wins over the legacy one. Safe to call
    multiple times.
    """
    for key, value in list(os.environ.items()):
        if key.startswith(LEGACY_PREFIX):
            new_key = NEW_PREFIX + key[len(LEGACY_PREFIX):]
            os.environ.setdefault(new_key, value)


def prepare_env(env_path: Path):
    """Prepare ``os.environ`` at startup.

    Always mirrors legacy ``ANIWORLD_*`` variables. Additionally loads
    *env_path* when it still exists, which — because the migration renames the
    file to ``.env.imported`` once it has been read — only happens on an
    install whose ``.env`` has not been imported into the DB yet. The values
    are needed in ``os.environ`` at that point because the app boots (and the
    CLI runs) before ``_migrate_dotenv_to_db()`` gets a chance to run.

    ``load_dotenv`` does not override variables that are already set, so a
    real environment variable always beats the file.

    This function never writes to *env_path*.

    Used by: ``config.py`` and ``entry.py``, both at import time with
    ``~/.mediaforge/.env``.
    """
    mirror_legacy_env()

    if not env_path.exists():
        return

    load_dotenv(env_path)
    # Mirror once more: a legacy .env may still use ANIWORLD_ keys, which only
    # land in os.environ after the file has been loaded.
    mirror_legacy_env()
