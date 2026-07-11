"""Module store client — the half of the store that lives inside MediaForge.

The store itself is deliberately dumb: a static ``index.json`` plus a folder of
``.mfmod`` packages (a zip of one ``web/thirdparties/<name>/`` folder), servable
from GitHub Pages, an S3 bucket, or the small Flask admin that generates them.
Everything interesting happens here, on the client:

- **Nothing is shown until a store URL is configured.** ``module_store_url`` is
  empty on a fresh install, and while it is, the Modulmanager renders no store
  UI at all and this module never makes a network request. Pointing MediaForge
  at a store is an explicit, deliberate act — a module is arbitrary Python
  running in the app's own process, so opting in is a decision, not a default.

- **Three trust tiers — proven, not claimed.** The index carries a ``trust``
  field, but it is only ever a *claim*: anyone who can serve an index.json can
  type "official" into it. What decides the tier is the signature file inside
  the package itself (``MODULE.sig``, see signing.py), checked against the
  public keys MediaForge ships. :func:`install` verifies it after download and
  before staging, and an entry whose signature doesn't back its claim is
  treated as `unverified` no matter what the store said.

  The three tiers answer two different questions — *who wrote it* and *who
  vouches for it*:

  * ``official``   — **written by the MediaForge team.** Signed with a maintainer
    key. Ours, end to end.
  * ``verified``   — **written by someone else, accepted by us.** Third-party
    code that a maintainer read, re-packaged and signed. The signature says "we
    looked at this and we stand behind this exact copy of it" — it does not say
    we wrote it, and it does not make the author our responsibility.
  * ``unverified`` — **everything else**, and there is a lot of everything else:
    a module from somebody's own repository, a whole third-party store MediaForge
    was pointed at, an unsigned package, one signed by a key we don't know, or one
    that was signed and then modified. Nobody MediaForge trusts has vouched for
    this code. Installing one requires the admin to explicitly turn on
    ``module_store_allow_unverified``.

  The tier shown in the catalog *before* download is the store's claim (that's
  all there is to go on until the bytes are here); the tier shown on an
  installed module's card is the proven one.

- **More than one repository.** ``module_store_url`` is the *main* repo — while
  it is unset, no store UI exists at all. ``module_store_extra_urls`` adds
  further repositories (one URL per line): someone's own module repo, a fork's
  store, an internal one. They are ordinary stores and get no special treatment;
  what a module from them can prove is decided by exactly the same signature
  check, which in practice means "unverified", because a third-party repo does
  not hold MediaForge's maintainer keys.

- **A download never touches the running app.** It lands in
  ``web/thirdparties/_pending/<folder>/`` and is applied by
  :func:`..apply_pending_changes` at the next start. See that function for why
  live install/replace/remove isn't a thing on Flask.

Index format (``store_api: 1``) — see the MediaForge_Modulestore project's
docs/INDEX_SCHEMA.md for the authoritative version:

    {
      "store_api": 1,
      "name": "MediaForge Official Store",
      "updated_at": "2026-07-11T10:00:00Z",
      "modules": [
        {
          "id": "anime_seasons",            # MODULE_ID
          "folder": "anime_seasons",        # target folder in web/thirdparties/
          "name": "Anime Seasons",          # MODULE_NAME
          "version": "1.0.1",               # MODULE_VERSION
          "author": "PD Codes",
          "trust": "official",
          "description": {"en": "...", "de": "..."},
          "api_version": 1,                 # MODULE_API_VERSION
          "min_app_version": "1.1.0",
          "max_app_version": "",
          "requirements": [],
          "homepage": "https://...",
          "license": "GPL-3.0",
          "source_url": "https://github.com/...",   # unverified: where it came from
          "download_url": "packages/anime_seasons-1.0.1.mfmod",  # relative to index
          "sha256": "…",
          "size": 20480
        }
      ]
    }
"""

import hashlib
import io
import json
import shutil
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import PENDING_DIR, pending_changes, stage_removal
from .registry import (
    REGISTRY_API_VERSION, check_api_compatibility, check_app_compatibility,
    module_entries,
)
from .signing import verify_module
from ...logger import get_logger

logger = get_logger(__name__)

# app_settings keys. An empty STORE_URL_KEY is the "store is off" state: no UI,
# no requests, no way to install anything from a remote. That's the default, and
# EXTRA_URLS_KEY is deliberately gated behind it: extra repositories are an
# addition to a configured store, never a way to sneak one in.
STORE_URL_KEY = "module_store_url"
EXTRA_URLS_KEY = "module_store_extra_urls"
ALLOW_UNVERIFIED_KEY = "module_store_allow_unverified"

# Index format this client understands. A store announcing a higher store_api
# is refused rather than guessed at -- see fetch_index().
STORE_API_VERSION = 1

TRUST_LEVELS = ("official", "verified", "unverified")

# Downloads are small (a module is a handful of Python/JS/CSS files); anything
# this far outside that is either a mistake or something we don't want to
# unpack into the app's own package directory.
MAX_PACKAGE_BYTES = 25 * 1024 * 1024
HTTP_TIMEOUT = 20

# Process-lifetime cache of each repository's last successfully fetched index,
# keyed by store URL, so opening the Modulmanager doesn't re-hit every configured
# repo on every page load. Refreshed on demand (the page's "Refresh store" button
# passes force=True).
_CACHE: dict = {}
_CACHE_TTL = 15 * 60


def store_url() -> str:
    """The configured store's index URL, or "" when no store is configured --
    which is the state every fresh install starts in, and the one the whole
    store UI is gated on."""
    from ..db import get_setting

    return (get_setting(STORE_URL_KEY, "") or "").strip()


def extra_urls() -> list:
    """Additional repositories, one per line in ``module_store_extra_urls``.

    Empty unless a main store is configured — see STORE_URL_KEY. An extra repo is
    just another store: same index format, same signature check, no special
    standing. In practice its modules come out `unverified`, because a
    third-party repo doesn't hold MediaForge's maintainer keys — which is the
    correct outcome, not a limitation.
    """
    from ..db import get_setting

    if not store_url():
        return []
    raw = get_setting(EXTRA_URLS_KEY, "") or ""
    seen, out = {store_url()}, []
    for line in raw.replace(",", "\n").splitlines():
        url = line.strip()
        if url and url not in seen and url.startswith(("http://", "https://")):
            seen.add(url)
            out.append(url)
    return out


def store_urls() -> list:
    """Every configured repository, main first (so it wins any id collision)."""
    main = store_url()
    return ([main] if main else []) + extra_urls()


def store_enabled() -> bool:
    """True once an admin has pointed MediaForge at a store. Everything
    store-related in the UI and the routes checks this first."""
    return bool(store_url())


def allow_unverified() -> bool:
    from ..db import get_setting

    return (get_setting(ALLOW_UNVERIFIED_KEY, "0") or "0") == "1"


def _index_url(url: str) -> str:
    """Accept either a full ``.../index.json`` or the store's base URL and
    normalize to the former, so an admin can paste whichever they were given."""
    url = url.strip().rstrip("/")
    if url.endswith(".json"):
        return url
    return url + "/index.json"


def _http_get(url: str, max_bytes: int) -> bytes:
    """Plain GET with a size cap. Reads max_bytes + 1 so an oversized body is
    *detected* rather than silently truncated into a corrupt package."""
    req = urllib.request.Request(url, headers={"User-Agent": "MediaForge-ModuleStore/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"response larger than {max_bytes} bytes")
    return data


def _normalize(entry: dict, base_url: str) -> dict:
    """One raw index entry -> the shape the rest of MediaForge uses.

    Unknown/missing trust is forced to "unverified" rather than dropped: a
    store that forgets to label an entry gets the *most* restrictive treatment,
    never the least. download_url is resolved against the index's own URL so a
    store can use relative package paths (which is what makes the whole thing
    movable between GitHub Pages, a CDN, or localhost without rewriting it).
    """
    trust = str(entry.get("trust") or "").lower()
    if trust not in TRUST_LEVELS:
        trust = "unverified"
    module_id = str(entry.get("id") or "").strip()
    download_url = str(entry.get("download_url") or "").strip()
    if download_url:
        download_url = urllib.parse.urljoin(base_url, download_url)
    description = entry.get("description") or {}
    if isinstance(description, str):
        description = {"en": description}
    return {
        "id": module_id,
        # The folder the module must be installed into. Defaults to the id --
        # they're normally the same, and MODULE_ID exists precisely so they
        # don't *have* to be.
        "folder": str(entry.get("folder") or module_id).strip(),
        "name": str(entry.get("name") or module_id),
        "version": str(entry.get("version") or "0.0.0"),
        "author": str(entry.get("author") or ""),
        "trust": trust,
        "description": description,
        "api_version": entry.get("api_version"),
        "min_app_version": str(entry.get("min_app_version") or ""),
        "max_app_version": str(entry.get("max_app_version") or ""),
        "requirements": list(entry.get("requirements") or []),
        "homepage": str(entry.get("homepage") or ""),
        "license": str(entry.get("license") or ""),
        "source_url": str(entry.get("source_url") or ""),
        "download_url": download_url,
        "sha256": str(entry.get("sha256") or "").lower(),
        "size": entry.get("size"),
    }


def fetch_index(url: str = None, force: bool = False) -> dict:
    """Fetch (or return the cached) index of one repository: ``{"ok", "error",
    "name", "updated_at", "url", "modules": [...]}``.

    Never raises: a store that's down, a firewall, a typo'd URL or a malformed
    index all come back as ``ok: False`` with a message, because the only caller
    is a page an admin is looking at, and "that repo is unreachable" is a
    perfectly ordinary thing for it to say — especially with several repos
    configured, where one being down must not take the others with it.
    """
    import time

    url = (url or store_url()).strip()
    if not url:
        return {"ok": False, "error": "no store configured", "url": "", "modules": []}

    cached = _CACHE.get(url)
    if cached and not force and (time.time() - cached["fetched_at"]) < _CACHE_TTL:
        return cached["index"]

    index_url = _index_url(url)
    try:
        raw = _http_get(index_url, 4 * 1024 * 1024)
        data = json.loads(raw.decode("utf-8"))
        announced = int(data.get("store_api") or 0)
        if announced > STORE_API_VERSION:
            raise ValueError(
                f"store speaks index format v{announced}, this MediaForge understands "
                f"v{STORE_API_VERSION} — update MediaForge")
        modules = [_normalize(e, index_url) for e in (data.get("modules") or [])]
        modules = [m for m in modules if m["id"] and m["folder"]]
        index = {
            "ok": True,
            "error": None,
            "name": str(data.get("name") or url),
            "updated_at": str(data.get("updated_at") or ""),
            "url": index_url,
            "store_url": url,
            "modules": modules,
        }
    except Exception as exc:
        logger.warning("[ModuleStore] Could not fetch index from %s: %s", index_url, exc)
        index = {"ok": False, "error": str(exc), "name": url, "url": index_url,
                 "store_url": url, "modules": []}

    _CACHE[url] = {"index": index, "fetched_at": time.time()}
    return index


def _compat_reason(entry: dict) -> str:
    """Why this store entry can't be installed on this MediaForge, or "" if it
    can. Same three gates the loader applies at startup
    (registry.check_api_compatibility / check_app_compatibility, and the
    module's pip requirements), applied *before* downloading rather than after
    -- offering an admin an install button for a module that will refuse to
    load is just a slower way of saying no.
    """
    reason = check_api_compatibility(entry.get("api_version")) or check_app_compatibility(
        entry.get("min_app_version"), entry.get("max_app_version"))
    if reason:
        return reason
    missing = []
    for raw in entry.get("requirements") or []:
        from importlib.metadata import PackageNotFoundError, version as dist_version
        from packaging.requirements import InvalidRequirement, Requirement

        try:
            req = Requirement(str(raw))
            have = dist_version(req.name)
        except InvalidRequirement:
            missing.append(f"{raw} (unparseable)")
            continue
        except PackageNotFoundError:
            missing.append(f"{raw} (not installed)")
            continue
        if req.specifier and not req.specifier.contains(have, prereleases=True):
            missing.append(f"{raw} (have {have})")
    return ("missing dependency: " + ", ".join(missing)) if missing else ""


def catalog(force: bool = False) -> dict:
    """Every configured repository's index, merged with what's installed here —
    one list, ready for the Modulmanager's Store section:

        {"ok", "error", "repos": [{url, name, ok, error}...], "allow_unverified",
         "registry_api": 1, "pending": {...},
         "modules": [{..entry.., "store", "store_url", "installed",
                      "installed_version", "update_available", "compat_reason",
                      "installable"}]}

    ``ok`` is true if *at least one* repo answered: with several configured, one
    being down is a per-repo error (surfaced in "repos"), not a dead page.

    A module id offered by more than one repo is taken from the first one that
    offers it, main repo first — so a third-party repo cannot shadow a module the
    main store also ships. (It couldn't forge its trust tier either, since that
    comes from the signature, but it could waste an admin's afternoon.)

    ``installed`` is matched on MODULE_ID (falling back to folder name), not on
    the folder, so renaming a folder locally doesn't make the store think the
    module is gone and offer it again as a fresh install.
    """
    installed_by_id = {}
    for name, mod in module_entries().items():
        installed_by_id[mod.get("module_id") or name] = mod

    unverified_ok = allow_unverified()
    from packaging.version import InvalidVersion, Version

    repos, raw_entries, seen_ids = [], [], set()
    for url in store_urls():
        index = fetch_index(url, force=force)
        repos.append({"url": url, "name": index.get("name", url),
                      "ok": index.get("ok", False), "error": index.get("error")})
        for entry in index.get("modules", []):
            if entry["id"] in seen_ids:
                continue
            seen_ids.add(entry["id"])
            raw_entries.append({**entry, "store": index.get("name", url), "store_url": url})

    modules = []
    for entry in raw_entries:
        local = installed_by_id.get(entry["id"])
        installed_ver = local.get("version") if local else None
        update = False
        if local and installed_ver:
            try:
                update = Version(entry["version"]) > Version(installed_ver)
            except InvalidVersion:
                # A store or a module using a non-PEP-440 version string: fall
                # back to "different means newer", which is wrong only in the
                # rare downgrade case and still surfaces *a* difference.
                update = entry["version"] != installed_ver
        compat = _compat_reason(entry)
        blocked_by_trust = entry["trust"] == "unverified" and not unverified_ok
        modules.append({
            **entry,
            "installed": bool(local),
            "installed_version": installed_ver,
            "update_available": update,
            "compat_reason": compat,
            "blocked_by_trust": blocked_by_trust,
            "installable": not compat and not blocked_by_trust and bool(entry["download_url"]),
        })
    modules.sort(key=lambda m: (TRUST_LEVELS.index(m["trust"]), m["name"].lower()))

    reachable = [r for r in repos if r["ok"]]
    return {
        # One dead repo among several is a per-repo error, not a dead page.
        "ok": bool(reachable),
        "error": None if reachable else (repos[0]["error"] if repos else "no store configured"),
        "repos": repos,
        "name": reachable[0]["name"] if reachable else "",
        "allow_unverified": unverified_ok,
        "registry_api": REGISTRY_API_VERSION,
        "pending": pending_changes(),
        "modules": modules,
    }


def _safe_extract(data: bytes, folder: str, target_root: Path) -> Path:
    """Unpack a .mfmod (a zip of exactly one module folder) into
    ``target_root/<folder>/``, refusing anything that doesn't look like one.

    A module is arbitrary Python that MediaForge is about to import into its
    own process — this can't stop a malicious *module*, and doesn't pretend to
    (that's what the trust tiers and the explicit opt-in are for). What it does
    stop is a malicious *archive*: absolute paths, ``..`` traversal and symlinks
    would let a package write outside the folder it claims to be, e.g. over
    MediaForge's own db.py, without anyone ever enabling the module. Zip is
    perfectly happy to carry all three, so every member is checked by hand.
    """
    staged = target_root / folder
    zf = zipfile.ZipFile(io.BytesIO(data))

    bad = zf.testzip()
    if bad is not None:
        raise ValueError(f"corrupt archive (bad member: {bad})")

    members = [m for m in zf.infolist() if not m.is_dir()]
    if not members:
        raise ValueError("archive is empty")

    prefix = folder + "/"
    for member in members:
        name = member.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"archive member escapes its folder: {member.filename}")
        # 0xA000 == S_IFLNK in the high 16 bits of external_attr (unix mode).
        if (member.external_attr >> 16) & 0xF000 == 0xA000:
            raise ValueError(f"archive contains a symlink: {member.filename}")
        if not name.startswith(prefix):
            raise ValueError(
                f"archive member outside the '{folder}/' folder: {member.filename}")
    if not any(m.filename.replace("\\", "/") == prefix + "__init__.py" for m in members):
        raise ValueError(f"archive has no {folder}/__init__.py — not a MediaForge module")

    if staged.exists():
        shutil.rmtree(staged)
    target_root.mkdir(parents=True, exist_ok=True)
    # Extracting into target_root (not into staged) because every member is
    # already prefixed with the folder name, which the loop above enforced.
    zf.extractall(target_root, members=members)
    return staged


def install(module_id: str, force: bool = False) -> dict:
    """Download a store module and stage it for the next restart. Returns
    ``{"ok", "error", "folder", "version", "restart_required"}``.

    Order matters here, and every step is a refusal point:
    1. store configured at all (no URL -> nothing to install from);
    2. entry exists in the index;
    3. trust: an ``unverified`` entry needs ``module_store_allow_unverified``;
    4. compatibility: registry API, MediaForge version, pip requirements --
       checked *before* the download, see _compat_reason();
    5. sha256: mandatory for official/verified (a package whose checksum the
       store vouches for but that doesn't match it is the one case where you
       stop and shout); for unverified it's optional-but-enforced-if-present,
       since those often point at a moving GitHub archive URL that no one can
       checksum ahead of time. `force` skips nothing here -- it only overrides
       the trust gate in (3) for an admin who knows exactly what they're doing.
    6. archive sanity: see _safe_extract().
    """
    if not store_enabled():
        return {"ok": False, "error": "no store configured"}

    # Main repo first, so it wins if two repos offer the same id -- same order
    # catalog() lists them in, so the entry an admin clicked is the entry that
    # gets installed.
    entry = None
    for url in store_urls():
        index = fetch_index(url)
        entry = next((m for m in index.get("modules", []) if m["id"] == module_id), None)
        if entry:
            break
    if not entry:
        return {"ok": False, "error": f"unknown module '{module_id}'"}

    if entry["trust"] == "unverified" and not allow_unverified() and not force:
        return {"ok": False, "error": "unverified modules are not allowed on this install"}

    compat = _compat_reason(entry)
    if compat:
        return {"ok": False, "error": compat}

    if not entry["download_url"]:
        return {"ok": False, "error": "store entry has no download_url"}

    try:
        data = _http_get(entry["download_url"], MAX_PACKAGE_BYTES)
    except Exception as exc:
        logger.exception("[ModuleStore] Download failed for '%s'", module_id)
        return {"ok": False, "error": f"download failed: {exc}"}

    digest = hashlib.sha256(data).hexdigest()
    if entry["sha256"]:
        if digest != entry["sha256"]:
            logger.error("[ModuleStore] Checksum mismatch for '%s': expected %s, got %s",
                         module_id, entry["sha256"], digest)
            return {"ok": False, "error": "checksum mismatch — package rejected"}
    elif entry["trust"] in ("official", "verified"):
        # A reviewed tier that ships no checksum means the store is
        # misconfigured, and "reviewed" is exactly the promise a checksum is
        # what makes verifiable. Refuse rather than quietly downgrade the
        # guarantee to the unverified one.
        return {"ok": False, "error": f"{entry['trust']} module without sha256 — refused"}

    pending_root = Path(__file__).parent / PENDING_DIR
    try:
        staged = _safe_extract(data, entry["folder"], pending_root)
    except Exception as exc:
        logger.exception("[ModuleStore] Rejected package for '%s'", module_id)
        return {"ok": False, "error": f"invalid package: {exc}"}

    # 7. The tier the *package itself* can prove, which is the only one that
    # counts. The index said "official"; this is where that claim is either
    # backed by a signature from a key MediaForge ships (signing.py) or quietly
    # collapses to "unverified" — in which case the admin has to have opted into
    # unverified modules, exactly as if the store had been honest about it. A
    # store cannot promote its own modules by editing a string.
    signature = verify_module(staged, module_id=entry["id"], version=entry["version"])
    effective = signature["tier"]
    if effective != entry["trust"]:
        logger.warning("[ModuleStore] '%s': store claims %r, signature proves %r (%s)",
                       module_id, entry["trust"], effective, signature["reason"])
    if effective == "unverified" and not allow_unverified() and not force:
        shutil.rmtree(staged, ignore_errors=True)
        detail = signature["reason"] or "not signed"
        return {"ok": False,
                "error": (f"the store lists this as '{entry['trust']}', but the package is "
                          f"not signed by a key this MediaForge trusts ({detail}) — "
                          "it can only be installed with unverified modules allowed")}

    logger.info("[ModuleStore] Staged '%s' v%s (%s%s, sha256=%s) at %s",
                module_id, entry["version"], effective,
                f", signed by {signature['signer']}" if signature["valid"] else ", unsigned",
                digest[:12], staged)
    return {
        "ok": True,
        "error": None,
        "folder": entry["folder"],
        "version": entry["version"],
        "trust": effective,
        "signer": signature["signer"],
        "restart_required": True,
    }


def uninstall(folder: str) -> dict:
    """Stage thirdparties/<folder>/ for removal at the next start (the folder
    itself, plus its namespaced settings -- see
    ..apply_pending_changes/registry.purge_module_settings).

    Works whether or not the module came from a store, and whether or not it
    loaded: a module too broken to import is one you especially want to be able
    to remove from the UI.
    """
    folder = (folder or "").strip()
    if not folder or folder.startswith("_") or "/" in folder or "\\" in folder:
        return {"ok": False, "error": "invalid module folder"}
    if not (Path(__file__).parent / folder).is_dir():
        return {"ok": False, "error": f"no such module folder: {folder}"}
    try:
        stage_removal(folder)
    except Exception as exc:
        logger.exception("[ModuleStore] Could not stage removal of '%s'", folder)
        return {"ok": False, "error": str(exc)}
    logger.info("[ModuleStore] Staged '%s' for removal on next start", folder)
    return {"ok": True, "error": None, "restart_required": True}


def cancel_pending() -> dict:
    """Throw away everything staged but not yet applied -- the "actually, no"
    button next to the restart-required banner. Deletes _pending/ entirely,
    which is safe precisely because nothing in it is live yet.
    """
    pending_root = Path(__file__).parent / PENDING_DIR
    try:
        if pending_root.is_dir():
            shutil.rmtree(pending_root)
    except Exception as exc:
        logger.exception("[ModuleStore] Could not clear pending changes")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "error": None}
