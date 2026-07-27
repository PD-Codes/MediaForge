"""Application entry point.

Defines :func:`mediaforge`, the function invoked by:
  - the installed ``mediaforge`` console script (see ``pyproject.toml``),
  - ``python -m mediaforge`` (see ``__main__.py``),
  - the PyInstaller build (``_pyinstaller_entry.py``).

It always starts the WebUI directly -- the standalone CLI was removed (see
``arguments.py``) -- and returns a process exit code instead of raising.
"""

import os
import sys
import warnings

# authlib internally uses its deprecated jose module -- suppress until they fix it
warnings.filterwarnings("ignore", category=DeprecationWarning, module="authlib")
try:
    from authlib.deprecate import AuthlibDeprecationWarning
    warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
except ImportError:
    pass


def _silence_insecure_request_warnings():
    """Keep urllib3(-future)'s InsecureRequestWarning out of the console.

    MediaForge itself no longer disables certificate verification anywhere
    except for an explicitly configured bare-IP site mirror (mirrors.py), where
    the certificate cannot match by definition and the warning carries no new
    information. Some setups (TLS-inspecting security suites, for instance)
    additionally make the warning appear spuriously, several times per page
    load, which drowns out the log.

    Set MEDIAFORGE_SHOW_TLS_WARNINGS=1 to get the warnings back.
    """
    if os.environ.get("MEDIAFORGE_SHOW_TLS_WARNINGS", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    for module in ("urllib3_future.exceptions", "urllib3.exceptions"):
        try:
            warning_cls = __import__(module, fromlist=["InsecureRequestWarning"]).InsecureRequestWarning
        except Exception:
            continue
        warnings.filterwarnings("ignore", category=warning_cls)


_silence_insecure_request_warnings()

from .arguments import parse_args
from .autodeps import ensure_patchright_chromium
from .config import MEDIAFORGE_CONFIG_DIR, VERSION
from .env import prepare_env
from .logger import get_logger

prepare_env(MEDIAFORGE_CONFIG_DIR / ".env")

logger = get_logger(__name__)


def set_terminal_title():
    """Set the terminal title to "MediaForge v.<version>" if running in a TTY.

    No-op when stdout is redirected/piped (no terminal to update).
    Called once from :func:`mediaforge` before the WebUI starts.
    """
    if sys.stdout.isatty():
        title = f"MediaForge v.{VERSION}"
        print(f"\033]0;{title}\007", end="", flush=True)


def mediaforge() -> int:
    """Main entry point -- always starts the WebUI directly.

    Runs pre-flight setup (terminal title, Chromium/mpv dependency checks,
    one-time legacy ``~/.aniworld`` data import), then blocks inside
    :func:`mediaforge.web.start_web_ui` until the server stops.

    Returns a process exit code: 0 on normal shutdown, 130 on Ctrl-C, 1 on an
    unhandled error. Used as the target of the ``mediaforge`` console script,
    ``python -m mediaforge``, and the PyInstaller build.
    """
    try:
        logger.debug("Starting WebUI...")
        set_terminal_title()
        ensure_patchright_chromium()

        args = parse_args()

        # Seamlessly carry over data from a previous "AniWorld Downloader"
        # install (~/.aniworld) so nobody loses their history/settings on
        # the rename. No-op once the new database exists.
        try:
            from .legacy_import import import_legacy_if_needed
            import_legacy_if_needed()
        except Exception:  # never block startup on an import hiccup
            logger.warning("Legacy data import skipped due to an error", exc_info=True)

        from .web import start_web_ui

        start_web_ui(
            host=args.web_host,
            port=args.web_port,
            open_browser=not args.no_browser,
            auth_enabled=True,
            sso_enabled=False,
            force_sso=False,
        )
        return 0

    except KeyboardInterrupt:
        print("\nQuitting.", file=sys.stderr)
        return 130

    except Exception as err:
        logger.error("Unexpected error occurred", exc_info=True)
        print(f"\nAn unexpected error occurred: {err}", file=sys.stderr)
        print("Please check the logs for more details.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(mediaforge())
