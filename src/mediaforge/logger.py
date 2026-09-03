"""Process-wide logging setup.

Provides a single shared "mediaforge" logger (:func:`get_logger`) that logs
to a colored stdout stream, a plain-text file in the temp directory (rewritten
on every start) and a rotating ``mf.err`` in the config directory that keeps
ERROR and above **across** restarts, plus a level tagged with the source
file/line/function of each call. The
DEBUG/WARNING level is driven by the ``MEDIAFORGE_DEBUG_MODE`` environment
variable and can also be flipped at runtime via :func:`set_debug_mode`.
"""

import logging
import os
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

_global_logger = None


def set_debug_mode(enabled: bool):
    """Enable or disable DEBUG level on the global logger at runtime.

    Used by: the settings route (``web/routes/settings.py``) when the user
    toggles debug logging in the WebUI (unless locked via --debug/
    MEDIAFORGE_DEBUG_FORCED, see arguments.py).
    """
    # No `global` needed: the module-level name is only read here, never
    # rebound. Declaring it anyway trips flake8's F824 and fails CI.
    if _global_logger is not None:
        _global_logger.setLevel(logging.DEBUG if enabled else logging.WARNING)

# ANSI color codes for console output
RESET = "\033[0m"

COLORS = {
    logging.DEBUG: "\033[36m",  # Cyan
    logging.INFO: "\033[32m",  # Green
    logging.WARNING: "\033[33m",  # Yellow
    logging.ERROR: "\033[31m",  # Red
    logging.CRITICAL: "\033[41m",  # Red background
}

TIME_COLOR = "\033[35m"  # Magenta
FUNC_COLOR = "\033[34m"  # Blue
MSG_COLOR = "\033[37m"  # White/Gray


class ColorFormatter(logging.Formatter):
    """Formatter for colored stdout logs."""

    def format(self, record):
        level_color = COLORS.get(record.levelno, RESET)
        record.levelname = f"{level_color}{record.levelname}{RESET}"

        cwd = os.getcwd()
        try:
            rel_path = os.path.relpath(record.pathname, cwd)
        except ValueError:
            # On Windows, relpath fails when paths span different drives (e.g. C: vs L:)
            rel_path = record.pathname
        record.func_info = (
            f"{FUNC_COLOR}{rel_path}:{record.lineno}:{record.funcName}{RESET}"
        )

        record.msg = f"{MSG_COLOR}{record.getMessage()}{RESET}"
        record.args = None

        formatted = super().format(record)

        # Color timestamp
        parts = formatted.split(" - ", 1)
        if len(parts) == 2:
            timestamp, rest = parts
            formatted = f"{TIME_COLOR}{timestamp}{RESET} - {rest}"

        return formatted


class PlainFormatter(logging.Formatter):
    """Formatter for plain file logs (no color)."""

    def format(self, record):
        cwd = os.getcwd()
        try:
            rel_path = os.path.relpath(record.pathname, cwd)
        except ValueError:
            # On Windows, relpath fails when paths span different drives (e.g. C: vs L:)
            rel_path = record.pathname
        record.func_info = f"{rel_path}:{record.lineno}:{record.funcName}"
        record.msg = record.getMessage()
        record.args = None
        return super().format(record)


# get_logger reads MEDIAFORGE_DEBUG_MODE on every call so runtime changes take effect
def get_logger(name=__name__, level=None):
    """Return the shared "mediaforge" logger, writing to both a file and
    colored stdout. The *name* argument is accepted for the familiar
    ``get_logger(__name__)`` call pattern but does not create a separate
    logger — handlers/level are configured once (singleton) and every
    caller across the codebase gets the same logger instance.
    """
    global _global_logger
    if _global_logger is None:
        _global_logger = logging.getLogger("mediaforge")
        _global_logger.handlers.clear()
        _global_logger.propagate = False  # prevent double output via root logger

        log_format = "%(asctime)s - %(levelname)s - %(func_info)s - %(message)s"
        date_format = "%Y-%m-%d %H:%M:%S"

        # ------------------ File handler ------------------ #
        temp_dir = tempfile.gettempdir()
        log_file_path = Path(temp_dir) / "mediaforge.log"
        file_handler = logging.FileHandler(log_file_path, mode="w", encoding="utf-8")
        file_handler.setFormatter(PlainFormatter(log_format, datefmt=date_format))
        _global_logger.addHandler(file_handler)

        # ------------------ Error file handler ------------------ #
        # The handler above opens the log with mode="w", so every start wipes
        # the previous run: by the time anyone asks why the last run died, the
        # answer is gone. That is exactly what happened when a self-update was
        # killed mid-flight — the app came back, truncated the log, and left
        # nothing to read. This second handler keeps ERROR and above in the
        # config directory, appended and rotated, so the evidence survives a
        # restart. The whole thing is best effort: a read-only home must not
        # stop the app from logging at all.
        #
        # The directory is resolved here rather than imported from config.py,
        # because config.py imports THIS module.
        try:
            override = os.environ.get("MEDIAFORGE_CONFIG_DIR", "").strip()
            cfg_dir = (Path(override).expanduser() if override
                       else Path.home() / ".mediaforge")
            cfg_dir.mkdir(parents=True, exist_ok=True)
            err_handler = RotatingFileHandler(
                cfg_dir / "mf.err", maxBytes=1_000_000, backupCount=3,
                encoding="utf-8", delay=True,
            )
            err_handler.setLevel(logging.ERROR)
            err_handler.setFormatter(PlainFormatter(log_format, datefmt=date_format))
            _global_logger.addHandler(err_handler)
        except OSError:
            pass

        # ------------------ Console handler ------------------ #
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(ColorFormatter(log_format, datefmt=date_format))
        _global_logger.addHandler(console_handler)

        # Determine log level from env or argument
        env_debug = os.getenv("MEDIAFORGE_DEBUG_MODE", "0")
        level = level or (logging.DEBUG if env_debug == "1" else logging.WARNING)
        _global_logger.setLevel(level)

        # Reduce noise from urllib3
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

    # Always re-check env at call time so runtime changes to MEDIAFORGE_DEBUG_MODE take effect
    if os.getenv("MEDIAFORGE_DEBUG_MODE", "0") == "1":
        _global_logger.setLevel(logging.DEBUG)
    else:
        _global_logger.setLevel(logging.WARNING)

    return _global_logger
