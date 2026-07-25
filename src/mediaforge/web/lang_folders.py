"""Language-folder names for MEDIAFORGE_LANG_SEPARATION.

Thin re-export of :mod:`mediaforge.languages`, which is the single source of
truth for every language label / mapping. This module stays because a lot of
call sites (browse, library, search, the two workers) import from here; it must
not define any mapping of its own.
"""

from ..languages import (  # noqa: F401  (re-exported for existing call sites)
    LANG_FOLDER_MAP,
    LANG_FOLDERS,
    SYNC_ALL_LANGUAGES,
    lang_folder_for,
)

__all__ = [
    "LANG_FOLDER_MAP",
    "LANG_FOLDERS",
    "SYNC_ALL_LANGUAGES",
    "lang_folder_for",
]
