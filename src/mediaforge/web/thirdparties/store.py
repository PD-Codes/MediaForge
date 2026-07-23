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

from . import PENDING_DIR, RESERVED_NAMES, modules_dir, pending_changes, stage_removal
from .registry import (
    REGISTRY_API_VERSION, check_api_compatibility, check_app_compatibility,
    module_entries,
)
from .signing import verify_module
from ...logger import get_logger

logger = get_logger(__name__)

# ── The official store: in code, not in settings ─────────────────────────────
# The main repository is a constant. A user cannot repoint it and cannot clear it,
# for the same reason they cannot edit trusted_keys.py from the UI: "just change
# this URL and the official modules will come from over here" is a one-line
# social-engineering script, and it would silently redirect the one repository whose
# modules this build is prepared to call official.
#
# The client appends /index.json itself, so the base URL is all that goes here.
#
# Pair it with that store's public key in trusted_keys.py's BUILTIN_KEYS -- without
# the key, everything from it arrives as "unverified", which is correct but not what
# you want from your own official store.
#
# "" is valid: a build with no store of its own. The Modulmanager then shows no
# official repository, only whatever the admin added themselves.
DEFAULT_STORE_URL = "https://mediaforge.softarchiv.com/store"

# What an admin CAN configure, and the limits of it. Both are strictly additive:
# neither can touch the official store above, and neither can make MediaForge trust a
# signing key it wasn't built with. The worst an admin can do to themselves here is
# add a repository nobody vouched for -- and then still have to tick
# ALLOW_UNVERIFIED_KEY before anything from it will install.
EXTRA_URLS_KEY = "module_store_extra_urls"
ALLOW_UNVERIFIED_KEY = "module_store_allow_unverified"

# Index format this client understands. A store announcing a higher store_api
# is refused rather than guessed at -- see fetch_index().
STORE_API_VERSION = 1

TRUST_LEVELS = ("official", "verified", "unverified")

# What a store entry can be. Modules are Python the app imports; themes are
# CSS/asset packs handled by web/themes.py. Same download/checksum/signature
# pipeline, different landing folder and lifecycle (themes always apply live).
# The store index's canonical value for a theme pack is "template"; "theme" is
# accepted as an alias. Both normalize to the internal value "theme", which is
# what every downstream consumer (catalog, install, the UI's filter and
# badges) switches on.
ENTRY_TYPES = ("module", "theme")
_TYPE_ALIASES = {"module": "module", "theme": "theme", "template": "theme"}

# Downloads are small (a module is a handful of Python/JS/CSS files); anything
# this far outside that is either a mistake or something we don't want to
# unpack into the app's own package directory.
MAX_PACKAGE_BYTES = 25 * 1024 * 1024
HTTP_TIMEOUT = 20

# Fetching an index is not the same errand as downloading a package, so it doesn't get
# the same patience. The index is a few KB and someone is sitting in front of the page
# waiting for it; a repo that hasn't answered in this long is, for that person's
# purposes, down. 20 seconds of "Loading store…" for a store whose domain doesn't even
# resolve is indistinguishable from a hung page — and with several repos configured and
# fetched one after another, those seconds used to add up.
INDEX_TIMEOUT = 6

# Process-lifetime cache of each repository's last fetched index, keyed by store URL, so
# opening the Modulmanager doesn't re-hit every configured repo on every page load.
# Refreshed on demand (the page's "Refresh store" button passes force=True).
#
# Failures are cached too, but only briefly: a 15-minute memory of "this repo is down"
# would mean an admin who fixes their repo, or plugs the network back in, sits there
# reloading a page that has decided not to ask again. One minute is long enough to stop
# a page load from re-waiting on a dead host, short enough to notice a fixed one.
_CACHE: dict = {}
_CACHE_TTL = 15 * 60
_FAIL_TTL = 60


def store_url() -> str:
    """The official store's base URL. Comes from DEFAULT_STORE_URL and nowhere else
    -- there is no setting, and therefore no route, that can change it. See that
    constant for why."""
    return DEFAULT_STORE_URL.strip()


def extra_urls() -> list:
    """Additional repositories an admin added, one per line in
    ``module_store_extra_urls``.

    This is the part that *is* theirs to decide: their own store, a company-internal
    one, a fork's. An extra repo is just another store -- same index format, same
    signature check, no special standing. In practice its modules come out
    `unverified`, because a third-party repo doesn't hold the keys this build was
    compiled with, and that is the correct outcome rather than a limitation.

    The official store can't be duplicated in here (it's filtered out), so no amount
    of pasting can quietly shadow it.
    """
    from ..db import get_setting

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
    """True when there is any repository at all to talk to -- the official one this
    build ships with, or one the admin added. Everything store-related in the UI and
    the routes checks this first.

    Note it is *not* gated on the official store alone: a build with an empty
    DEFAULT_STORE_URL still has to let a self-hoster add their own repository, or the
    field to add one would be behind the very condition adding one is meant to
    satisfy.
    """
    return bool(store_urls())


def allow_unverified() -> bool:
    from ..db import get_setting

    return (get_setting(ALLOW_UNVERIFIED_KEY, "0") or "0") == "1"


def _index_url(url: str, include_unapproved: bool = False) -> str:
    """Accept either a full ``.../index.json`` or the store's base URL and
    normalize to the former, so an admin can paste whichever they were given.

    With *include_unapproved*, ask for the store's second catalog — ``index-all.json`` —
    which also lists modules nobody has reviewed yet. That is the file behind the "allow
    unverified modules" switch: without it the switch would only permit unverified modules
    to be *installed*, while the catalog it reads from never mentioned any, which is a
    setting that appears to do nothing.

    A pasted ``.../index.json`` is rewritten too. Somebody who typed the normal index and
    then turned the switch on meant "show me everything from that store", not "show me
    everything, except keep reading the file that has none of it".
    """
    url = url.strip().rstrip("/")
    if url.endswith(".json"):
        if include_unapproved and url.endswith("/index.json"):
            return url[: -len("/index.json")] + "/index-all.json"
        return url
    return url + ("/index-all.json" if include_unapproved else "/index.json")


def _http_get(url: str, max_bytes: int, timeout: int = HTTP_TIMEOUT) -> bytes:
    """Plain GET with a size cap. Reads max_bytes + 1 so an oversized body is
    *detected* rather than silently truncated into a corrupt package.

    Certificate verification is skipped for MediaForge's own store hosts (see
    config.TLS_INSECURE_HOSTS) -- an expired certificate there must not take the
    Modulmanager offline. What actually guards a package is the signature check
    against the built-in keys (see trusted_keys.py), which is unaffected by how
    the bytes were transported. Every other (admin-added) repository keeps full
    TLS verification: insecure_ssl_context_for() returns None for those, i.e.
    Python's default verifying context.
    """
    from ...config import insecure_ssl_context_for

    req = urllib.request.Request(url, headers={"User-Agent": "MediaForge-ModuleStore/1.0"})
    with urllib.request.urlopen(
        req, timeout=timeout, context=insecure_ssl_context_for(url)
    ) as resp:
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
    # Index "type": "module" (default), or "template"/"theme" for theme packs
    # (both normalize to the internal "theme"). Anything unknown is treated as
    # a module, which is the strict choice: a module goes through the full
    # compat gates and never installs live into the themes folder.
    entry_type = _TYPE_ALIASES.get(
        str(entry.get("type") or "").strip().lower(), "module")
    module_id = str(entry.get("id") or "").strip()
    download_url = str(entry.get("download_url") or "").strip()
    if download_url:
        download_url = urllib.parse.urljoin(base_url, download_url)
    description = entry.get("description") or {}
    if isinstance(description, str):
        description = {"en": description}
    return {
        "id": module_id,
        "type": entry_type,
        # Free-form grouping label from the index (e.g. "notifications",
        # "integration"). Display-only: shown in the store row's meta line —
        # never used for any decision here.
        "category": str(entry.get("category") or "").strip(),
        # Who the index *claims* packaged/signed this. Display-only, like the
        # claimed trust tier: the tier that counts is still proven by
        # verify_module() against the package's own MODULE.sig at install.
        "signed_by": str(entry.get("signed_by") or "").strip(),
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
        # Only present in a store's index-all.json, and only on entries nobody has reviewed
        # yet ("pending", "draft"). It is the store telling on itself, and it earns a badge
        # of its own in the Modulmanager: "unverified" says nobody signed this, while
        # "unreviewed" says nobody *read* it. Those are different warnings and a user
        # deserves both.
        "review_status": str(entry.get("review_status") or ""),
    }


def fetch_index(url: str = None, force: bool = False, include_unapproved: bool = None) -> dict:
    """Fetch (or return the cached) index of one repository: ``{"ok", "error",
    "name", "updated_at", "url", "modules": [...]}``.

    *include_unapproved* picks which of the store's two catalogs to read — the reviewed one
    (``index.json``) or everything, unreviewed submissions included (``index-all.json``).
    Defaults to whatever the admin's "allow unverified modules" setting says, because those
    two things are the same decision wearing different hats: a switch that permits
    unverified installs while the catalog it reads never lists any would appear to do
    nothing at all.

    A store that offers no ``index-all.json`` (an older one, or a third-party repo that
    never had a review queue) is not an error — we fall back to the normal index and log it
    once. "This repo doesn't have unreviewed modules" is an answer, not a failure.

    Never raises: a store that's down, a firewall, a typo'd URL or a malformed index all
    come back as ``ok: False`` with a message, because the only caller is a page an admin is
    looking at, and "that repo is unreachable" is a perfectly ordinary thing for it to say —
    especially with several repos configured, where one being down must not take the others
    with it.
    """
    import time

    url = (url or store_url()).strip()
    if not url:
        return {"ok": False, "error": "no store configured", "url": "", "modules": []}

    if include_unapproved is None:
        include_unapproved = allow_unverified()

    # The variant is part of the cache key. Without that, flipping the switch would keep
    # serving the catalog fetched under the old setting for up to fifteen minutes — the
    # setting would look broken, and the obvious next move (click Refresh, see no change)
    # would confirm it.
    cache_key = (url, bool(include_unapproved))

    cached = _CACHE.get(cache_key)
    if cached and not force:
        ttl = _CACHE_TTL if cached["index"].get("ok") else _FAIL_TTL
        if (time.time() - cached["fetched_at"]) < ttl:
            return cached["index"]

    index_url = _index_url(url, include_unapproved)
    try:
        try:
            raw = _http_get(index_url, 4 * 1024 * 1024, timeout=INDEX_TIMEOUT)
        except Exception:
            if not include_unapproved:
                raise
            # No index-all.json here. Read the reviewed catalog instead of showing this repo
            # as broken — an old store, or one that simply has no review queue, still has
            # modules to offer.
            fallback = _index_url(url, include_unapproved=False)
            logger.info("[ModuleStore] %s offers no index-all.json — falling back to %s",
                        url, fallback)
            index_url = fallback
            raw = _http_get(index_url, 4 * 1024 * 1024, timeout=INDEX_TIMEOUT)

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
            "includes_unapproved": bool(data.get("includes_unapproved")),
            "modules": modules,
        }
    except Exception as exc:
        logger.warning("[ModuleStore] Could not fetch index from %s: %s", index_url, exc)
        index = {"ok": False, "error": str(exc), "name": url, "url": index_url,
                 "store_url": url, "includes_unapproved": False, "modules": []}

    _CACHE[cache_key] = {"index": index, "fetched_at": time.time()}
    return index


def _compat_reason(entry: dict) -> str:
    """Why this store entry can't be installed on this MediaForge, or "" if it
    can. Same three gates the loader applies at startup
    (registry.check_api_compatibility / check_app_compatibility, and the
    module's pip requirements), applied *before* downloading rather than after
    -- offering an admin an install button for a module that will refuse to
    load is just a slower way of saying no.

    Themes get only the app-version gate: they carry no Python, so the
    registry-API version and pip requirements simply do not apply to them.
    """
    if entry.get("type") == "theme":
        return check_app_compatibility(
            entry.get("min_app_version"), entry.get("max_app_version")) or ""
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


def missing_requirements(entry: dict) -> list:
    """The requirement strings of a *store entry* that this MediaForge cannot satisfy.

    The same check _compat_reason() does, but returning the raw PEP 508 strings instead of a
    sentence — because a sentence is something to show a person, and these are something to
    hand to pip. Keeping the two apart is what lets the Modulmanager put an "Install
    dependencies" button next to the red text instead of only the red text.
    """
    from importlib.metadata import PackageNotFoundError, version as dist_version
    from packaging.requirements import InvalidRequirement, Requirement

    out = []
    for raw in entry.get("requirements") or []:
        try:
            req = Requirement(str(raw))
            have = dist_version(req.name)
        except InvalidRequirement:
            continue                      # unparseable: not something we can hand to pip
        except PackageNotFoundError:
            out.append(str(raw))
            continue
        if req.specifier and not req.specifier.contains(have, prereleases=True):
            out.append(str(raw))
    return out


def install_requirements(module_id: str) -> dict:
    """Install the pip dependencies of a store entry — before the module itself is installed.

    The route the Modulmanager's store section calls. It exists separately from the one for
    an already-installed module (routes/extensions.py's /api/extensions/requirements, which
    reads MODULE_REQUIREMENTS off the folder on disk) for the obvious reason: here there is no
    folder on disk yet. The requirement strings come from the catalog entry.

    The important property is the same in both cases: **the caller names a module, never a
    package.** The strings handed to pip are the ones the module declared, not anything a
    request can put in them — so this endpoint cannot be turned into "pip install whatever I
    like on your server", which is what it would be if it took a package name.
    """
    from . import deps as module_deps

    entry = None
    for candidate in catalog().get("modules", []):
        if candidate["id"] == module_id:
            entry = candidate
            break
    if entry is None:
        return {"ok": False, "error": f"unknown module '{module_id}'"}

    missing = missing_requirements(entry)
    if not missing:
        return {"ok": True, "installed": [], "log": "",
                "message": "nothing to install — every dependency is already satisfied"}

    result = module_deps.install(missing)
    result.setdefault("installed", missing if result.get("ok") else [])
    return result


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

    # Theme packs live in their own folder (web/themes.py) and are matched by
    # manifest id, exactly like modules are matched by MODULE_ID.
    from ..themes import themes_by_id
    installed_themes_by_id = themes_by_id()

    unverified_ok = allow_unverified()
    from packaging.version import InvalidVersion, Version

    # Fetch every repo at once, not one after another, and put a wall clock on the whole
    # thing.
    #
    # Two separate problems, one mechanism. Sequentially, a dead repo spent its entire
    # timeout before the next was even asked, so the page waited for the *sum* of the slow
    # repos rather than the slowest. And socket timeouts do not bound everything: name
    # resolution happens before the socket exists, so a host whose DNS hangs (a VPN, a
    # firewall that drops instead of refusing) blocks urlopen for as long as the resolver
    # feels like — INDEX_TIMEOUT never gets a say, and the Modulmanager sits on "Loading
    # store…" forever with no error to show.
    #
    # A thread per repo with a hard deadline fixes both. The deadline is what an admin
    # actually experiences, so it is the number that matters; a worker that is still stuck
    # in getaddrinfo is abandoned (daemon threads, they die with the process) rather than
    # waited on. The results are consumed in store_urls() order below — main store first —
    # so which repo wins a duplicate module id stays deterministic and has nothing to do
    # with who answered first.
    import threading
    import time

    urls = store_urls()
    indexes = {}
    deadline = INDEX_TIMEOUT + 2

    def _unreachable(url, error):
        return {"ok": False, "error": error, "name": url, "url": url,
                "store_url": url, "modules": []}

    def _worker(url):
        try:
            indexes[url] = fetch_index(url, force=force)
        except Exception as exc:  # pragma: no cover - fetch_index already swallows these
            logger.exception("[ModuleStore] Fetching %s failed unexpectedly", url)
            indexes[url] = _unreachable(url, str(exc))

    # Plain daemon threads rather than a ThreadPoolExecutor, deliberately: since Python
    # 3.9 the pool's workers are non-daemon and the interpreter *joins them at exit*, so a
    # worker stuck in getaddrinfo would hold up MediaForge's shutdown — trading a hung page
    # for a hung Ctrl+C. A daemon thread we can simply walk away from.
    threads = [threading.Thread(target=_worker, args=(url,), daemon=True,
                                name=f"store-index-{url}") for url in urls]
    for thread in threads:
        thread.start()

    # One deadline for all of them, not one each: they run concurrently, so joining each
    # for the full `deadline` would quietly restore the very summing this replaced.
    expires_at = time.monotonic() + deadline
    for thread in threads:
        thread.join(timeout=max(0.0, expires_at - time.monotonic()))

    for url in urls:
        if url not in indexes:
            # Its thread is still in there somewhere, blocked. Name resolution happens
            # before the socket exists, so INDEX_TIMEOUT never got a say — this is the
            # backstop that keeps "Loading store…" from being forever.
            logger.warning(
                "[ModuleStore] %s did not answer within %ss (DNS or a dropped "
                "connection) — reporting it as unreachable", url, deadline)
            indexes[url] = _unreachable(url, f"no answer within {deadline}s (DNS or network)")

    repos, raw_entries, seen_ids = [], [], set()
    for url in urls:
        index = indexes[url]
        repos.append({"url": url, "name": index.get("name", url),
                      "ok": index.get("ok", False), "error": index.get("error")})
        for entry in index.get("modules", []):
            if entry["id"] in seen_ids:
                continue
            seen_ids.add(entry["id"])
            raw_entries.append({**entry, "store": index.get("name", url), "store_url": url})

    modules = []
    for entry in raw_entries:
        if entry.get("type") == "theme":
            local = installed_themes_by_id.get(entry["id"])
        else:
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

        # Unreviewed is not the same as unverified, and conflating them would waste the one
        # piece of information the store went out of its way to give us. "Unverified" means
        # nobody signed this. "Unreviewed" means nobody *read* it — it is sitting in a queue
        # waiting for a human, and it is on offer here only because this install asked to see
        # the queue. It shows up as its own badge, and it is never installable while
        # unverified modules are switched off (it cannot be: the store forces those entries
        # to unverified, so blocked_by_trust already covers it — this is belt and braces on
        # the one decision worth being paranoid about).
        unreviewed = bool(entry.get("review_status")) and entry["review_status"] != "approved"

        # Which of the two kinds of "incompatible" this is. They look the same in the UI today
        # — a red word — and they are not the same thing at all: a missing pip package is a
        # button away, an unsupported MediaForge version is a wait. Telling them apart is the
        # whole point of the Install-dependencies button, and it has to be decided here,
        # because only here do we know what the module asked for.
        missing_deps = missing_requirements(entry)
        deps_only = bool(missing_deps) and compat.startswith("missing dependency")

        modules.append({
            **entry,
            "installed": bool(local),
            "installed_version": installed_ver,
            "update_available": update,
            "compat_reason": compat,
            "blocked_by_trust": blocked_by_trust,
            "unreviewed": unreviewed,
            # Fixable from here: pip can get these, and then the module installs normally.
            "missing_requirements": missing_deps if deps_only else [],
            "installable": (not compat and not blocked_by_trust
                            and not (unreviewed and not unverified_ok)
                            and bool(entry["download_url"])),
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


def _safe_extract(data: bytes, folder: str, target_root: Path,
                  required_file: str = "__init__.py",
                  kind: str = "MediaForge module") -> Path:
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
    if not any(m.filename.replace("\\", "/") == prefix + required_file for m in members):
        raise ValueError(f"archive has no {folder}/{required_file} — not a {kind}")

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

    # Theme packs branch off here: same download/checksum above and the same
    # signature verification below, but they stage into the themes folder,
    # must pass the CSS-only validator, and always apply live (a theme is
    # inert data — there is no blueprint that would need a restart).
    if entry.get("type") == "theme":
        return _install_theme_package(entry, data, module_id, force)

    # A folder name that would collide with this package's own submodules must never
    # reach the disk — see RESERVED_NAMES. Checked here as well as at discovery,
    # because "refused at install" is a message an admin can act on, while "installed
    # but silently ignored forever" is not.
    if entry["folder"] in RESERVED_NAMES:
        return {"ok": False,
                "error": f"'{entry['folder']}' is a reserved name in MediaForge's module "
                         f"system and cannot be installed"}

    pending_root = modules_dir() / PENDING_DIR
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


def _install_theme_package(entry: dict, data: bytes, module_id: str, force: bool) -> dict:
    """The theme-typed tail of install(): stage, verify, validate, go live.

    The caller has already gated trust, compatibility and the checksum, so this
    only handles what is different about themes: the landing folder, the
    theme.json requirement instead of __init__.py, the CSS-only validation
    (web/themes.py — the security boundary of the whole feature), and the
    always-live application at the end.
    """
    from ..themes import (
        _safe_theme_folder,
        install_theme_from_staged,
        themes_dir,
        validate_theme_dir,
    )

    # Reserved names, dot-names, separators — one gate for everything a store
    # index must never be able to name a folder (same check the uninstall and
    # asset paths apply).
    if _safe_theme_folder(entry["folder"]) is None:
        return {"ok": False,
                "error": f"'{entry['folder']}' is not an allowed theme folder name "
                         f"and cannot be installed"}

    pending_root = themes_dir() / PENDING_DIR
    try:
        staged = _safe_extract(data, entry["folder"], pending_root,
                               required_file="theme.json", kind="MediaForge theme pack")
    except Exception as exc:
        logger.exception("[ModuleStore] Rejected theme package for '%s'", module_id)
        return {"ok": False, "error": f"invalid package: {exc}"}

    # Same signature story as modules: the tier the package can *prove* is the
    # only one that counts, and an unproven claim collapses to unverified.
    signature = verify_module(staged, module_id=entry["id"], version=entry["version"])
    effective = signature["tier"]
    if effective != entry["trust"]:
        logger.warning("[ModuleStore] theme '%s': store claims %r, signature proves %r (%s)",
                       module_id, entry["trust"], effective, signature["reason"])
    if effective == "unverified" and not allow_unverified() and not force:
        shutil.rmtree(staged, ignore_errors=True)
        detail = signature["reason"] or "not signed"
        return {"ok": False,
                "error": (f"the store lists this as '{entry['trust']}', but the package is "
                          f"not signed by a key this MediaForge trusts ({detail}) — "
                          "it can only be installed with unverified modules allowed")}

    # The CSS-only contract. A "theme" carrying a .js or .py file is not a
    # theme, whoever signed it — refused before it ever reaches the live dir.
    problems = validate_theme_dir(staged)
    if problems:
        shutil.rmtree(staged, ignore_errors=True)
        return {"ok": False, "error": "invalid theme pack: " + "; ".join(problems[:5])}

    ok, error = install_theme_from_staged(staged)
    if not ok:
        return {"ok": False, "error": error}

    logger.info("[ModuleStore] Installed theme '%s' v%s (%s%s) live",
                module_id, entry["version"], effective,
                f", signed by {signature['signer']}" if signature["valid"] else ", unsigned")
    return {
        "ok": True,
        "error": None,
        "type": "theme",
        "folder": entry["folder"],
        "version": entry["version"],
        "trust": effective,
        "signer": signature["signer"],
        # Live already — themes never need the restart dance.
        "restart_required": False,
        "live": True,
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
    if not (modules_dir() / folder).is_dir():
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
    pending_root = modules_dir() / PENDING_DIR
    try:
        if pending_root.is_dir():
            shutil.rmtree(pending_root)
    except Exception as exc:
        logger.exception("[ModuleStore] Could not clear pending changes")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "error": None}
