"""Module signatures — what makes "official" mean something.

Without this, a module's trust tier is a string somebody typed into a store's
index.json, and anyone who can serve an index can call their own module
official. With it, the tier is a *cryptographic claim that travels inside the
module itself*: a signature file MediaForge can check against a whitelist of
public keys it ships. Delete the signature, edit one line of the module's code,
or sign it with a key MediaForge doesn't know, and the module is simply not
official any more — it still loads (if the admin wants it to), it just stops
claiming something it can't back up.

Verification only. MediaForge never signs anything: the private keys live on the
maintainers' own machines and nowhere else, which is the entire point. Signing is
the store tooling's job (mfstore's `key gen` / `sign` commands).

═══════════════════════════════════════════════════════════════════════════
THE FORMAT (v1) — this docstring is the spec. mfstore/signing.py implements
the exact same thing on the signing side; the two MUST agree byte for byte, so
change nothing here without changing it there (and bump SIG_VERSION).
═══════════════════════════════════════════════════════════════════════════

A signed module carries one extra file, ``MODULE.sig``, a UTF-8 JSON document:

    {
      "sig_version": 1,
      "module_id": "anime_seasons",
      "version": "1.0.1",
      "tier": "official",                       # official | verified
      "content_hash": "<sha256 hex>",           # see content_hash() below
      "signed_at": "2026-07-11T09:00:00Z",
      "signatures": [
        {"key_id": "3f2a9c…", "signer": "Domekologe", "signature": "<base64>"}
      ]
    }

**What is signed.** The canonical JSON of that same document *without* the
"signatures" key: ``json.dumps(doc, sort_keys=True, separators=(",", ":"),
ensure_ascii=False).encode("utf-8")``. Every signer signs those identical bytes,
which is what makes a future N-of-M rule (two maintainers must sign an
`official` module) a pure policy change rather than a format change.

**What content_hash covers.** Every file in the module folder except
``MODULE.sig`` itself, hashed as a sorted manifest (see :func:`content_hash`).
So the signature covers the module's entire contents, not just its name: change
one byte of one file and the hash no longer matches the signed document.

**Two deliberate exceptions in the hash**, both of which would otherwise make
honest modules fail verification for no security benefit:

1. ``__pycache__``/``*.pyc`` are excluded — Python *creates* those inside the
   module folder the moment MediaForge imports it, so including them would mean
   every module became "tampered with" immediately after its first successful
   start.
2. CRLF is normalized to LF before hashing. A module checked out through git on
   Windows (``core.autocrlf=true``) has different bytes on disk than the same
   module unpacked from a zip, and failing verification because of a line ending
   would train everyone to ignore the warning. An attacker who can only flip
   line endings can't change what the code *does*, so nothing is lost.

**Tiers.** Only "official" and "verified" can be signed — "unverified" is the
*absence* of a valid signature, so there is nothing to sign. A key is whitelisted
for specific tiers (see trusted_keys.py): a key allowed to sign `verified`
cannot mint an `official` module by claiming that tier, because the tier is
inside the signed payload and checked against the key's own permissions here.
"""

import base64
import fnmatch
import hashlib
import json
from pathlib import Path

from .trusted_keys import trusted_keys
from ...logger import get_logger

logger = get_logger(__name__)

SIG_FILENAME = "MODULE.sig"
SIG_VERSION = 1

# The only tiers a signature can assert. "unverified" is what you get when there
# is no valid signature at all, so it is deliberately not signable.
SIGNABLE_TIERS = ("official", "verified")

# Excluded from content_hash.
#
# This list must be identical in three places or signatures break in confusing
# ways: here, mfstore/signing.py (which computes the hash that gets signed), and
# mfstore/config.py's EXCLUDE_PATTERNS (which decides what goes into the .mfmod).
# The third one is the subtle one: a file that is *hashed* but not *packaged*
# exists on the signer's disk and not on the user's, so the module would verify
# for the person who signed it and be reported as "modified" for everyone else.
# Hence: hash exactly what ships, and nothing else.
#
# Two of these earn their place for reasons beyond tidiness — see this module's
# docstring: __pycache__/*.pyc (Python creates them the moment the module is
# imported, so hashing them would mark every module as tampered with right after
# its first start) and *.db/*.log (a module that writes a cache or a log into its
# own folder would otherwise invalidate its own signature by running).
HASH_EXCLUDE = (
    "__pycache__", ".git", ".gitignore", ".DS_Store", ".env",
    "*.pyc", "*.pyo", "*.db", "*.sqlite", "*.sqlite3", "*.log",
)


def _excluded(rel: Path) -> bool:
    return any(fnmatch.fnmatch(part, pattern)
               for part in rel.parts for pattern in HASH_EXCLUDE)


def content_hash(module_dir: Path) -> str:
    """sha256 over a canonical manifest of every file in the module folder.

    The manifest is ``"<relpath>\\0<sha256 of that file's normalized bytes>\\n"``
    for each file, sorted by POSIX relative path, concatenated, hashed. Sorting
    is what makes it independent of filesystem iteration order; hashing the paths
    alongside the contents is what stops a file from being *renamed* (or a new
    one added, or one deleted) without the hash changing.
    """
    entries = []
    for path in sorted(module_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(module_dir)
        if rel.name == SIG_FILENAME or _excluded(rel):
            continue
        data = path.read_bytes().replace(b"\r\n", b"\n")
        entries.append(f"{rel.as_posix()}\0{hashlib.sha256(data).hexdigest()}\n")
    digest = hashlib.sha256()
    digest.update("".join(sorted(entries)).encode("utf-8"))
    return digest.hexdigest()


def signed_payload(doc: dict) -> bytes:
    """The exact bytes every signer signs: the document minus its signatures,
    canonically encoded. Any disagreement between the two implementations about
    *these bytes* shows up as "signature invalid", so they are defined in exactly
    one place (this function, and its twin in mfstore) and nowhere else."""
    payload = {k: v for k, v in doc.items() if k != "signatures"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _verify_one(payload: bytes, public_key_b64: str, signature_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), payload)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_module(module_dir: Path, module_id: str = None, version: str = None) -> dict:
    """Verify a module folder's signature. Never raises; always returns:

        {"tier": "official"|"verified"|"unverified",
         "signed": bool,          # a MODULE.sig file exists at all
         "valid": bool,           # ...and it checks out against a trusted key
         "signer": str, "key_id": str,
         "reason": str}           # why it isn't valid, when it isn't

    Note what this does *not* do: it never refuses to load a module. An unsigned
    module is a perfectly normal module — most third-party ones will be — it just
    comes out as "unverified", which is what the store's install policy and the
    Modulmanager's badges then act on. The signature answers "who vouches for
    this code", not "may this code run"; the admin answers the second question.

    `module_id`/`version` (when the caller knows them, e.g. from the module's own
    MODULE_* constants) are cross-checked against the signed document: a valid
    signature over *some other module* must not launder this one. That's the
    attack the content_hash already blocks, but checking the identity fields too
    turns a confusing hash mismatch into a clear "signature is for a different
    module".
    """
    result = {"tier": "unverified", "signed": False, "valid": False,
              "signer": "", "key_id": "", "reason": ""}

    sig_file = module_dir / SIG_FILENAME
    if not sig_file.is_file():
        result["reason"] = "not signed"
        return result
    result["signed"] = True

    try:
        doc = json.loads(sig_file.read_text(encoding="utf-8"))
    except Exception as exc:
        result["reason"] = f"unreadable {SIG_FILENAME}: {exc}"
        return result

    if int(doc.get("sig_version") or 0) != SIG_VERSION:
        result["reason"] = (f"signature format v{doc.get('sig_version')}, "
                            f"this MediaForge understands v{SIG_VERSION}")
        return result

    tier = str(doc.get("tier") or "")
    if tier not in SIGNABLE_TIERS:
        result["reason"] = f"signature claims unknown tier {tier!r}"
        return result

    if module_id and str(doc.get("module_id") or "") != module_id:
        result["reason"] = (f"signature is for module {doc.get('module_id')!r}, "
                            f"not {module_id!r}")
        return result
    if version and str(doc.get("version") or "") != version:
        result["reason"] = (f"signature is for version {doc.get('version')!r}, "
                            f"but this module is {version!r}")
        return result

    actual = content_hash(module_dir)
    if actual != str(doc.get("content_hash") or ""):
        # The interesting failure: the signature is real, the files aren't the
        # ones that were signed. Loud, and never downgraded to "unsigned".
        result["reason"] = "content does not match the signature — the module was modified"
        return result

    payload = signed_payload(doc)
    keys = trusted_keys()
    for entry in (doc.get("signatures") or []):
        key_id = str(entry.get("key_id") or "")
        key = keys.get(key_id)
        if not key:
            # Signed by someone this MediaForge doesn't know: not a failure, just
            # not trusted. The module still loads; it just isn't official.
            continue
        if tier not in key.get("tiers", ()):
            # A key allowed to sign `verified` cannot mint `official` modules.
            logger.warning("[Signing] Key %s is not allowed to sign tier %r", key_id, tier)
            continue
        if _verify_one(payload, key["public_key"], str(entry.get("signature") or "")):
            result.update({
                "tier": tier,
                "valid": True,
                "signer": key.get("name", key_id),
                "key_id": key_id,
                "reason": "",
            })
            return result

    known = [s.get("key_id", "?") for s in (doc.get("signatures") or [])]
    result["reason"] = ("signed, but by no key this MediaForge trusts "
                        f"({', '.join(known) or 'no signatures in file'})")
    return result
