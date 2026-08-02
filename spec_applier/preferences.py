"""Root-owned per-person preference install (ss ADR 0085 §6, ss#2067).

The per-person sibling of ``spec_applier.applier``: pull → parse → verify →
install root-owned → commit a root-computed manifest. Same five steps, same
fail-static rule, same hardening walk — a SEPARATE module so the firm-spec
path's logic (and its tests) stay byte-for-byte untouched. The SINGLE install
seam is the ``spec_applier`` package running as root: nothing else writes
``SMD_SPEC_DIR``, for preferences exactly as for class specs. The establishment
intake stays a VAULT writer on both paths; it never touches the install tree.

THE SOURCE OBJECTS. ``vaults/<slug>/preferences/<person-slug>.json``, one per
person, written by the root establishment intake (the mediated person-scoped
establishment path) and by nothing else. ``*.previous.json`` siblings are the
one-generation recovery copies and are never installed.

WHY A SEPARATE MANIFEST. ``preferences-manifest.json`` sits beside the spec
``manifest.json`` rather than inside it because the spec manifest is an
ENFORCEMENT surface — the spec gate and the read-mark parse it, and its
``source_digest`` is the establishment intake's converge signal for firm
installs. Folding preference state into it would change the meaning of that
digest under every existing consumer. Preferences are DELIVERY-refinement (no
gate reads them; ADR 0085 §6 — the firm layer is the floor), so they get their
own root-computed commit point with the same trust properties.

FAIL-STATIC, WHOLE CYCLE. Any rejected object refuses the ENTIRE cycle and
leaves the installed preference tree exactly as it stood — the same
all-or-nothing rule ``parse_and_verify`` applies within a document, applied
across the prefix. The alternative (adopt the clean objects, keep prior state
for the broken one) needs a merge of previous manifest entries whose failure
modes are subtler than the cost being avoided: nobody hostile can write the
vault, so a broken object is a torn write or a bug, and "everything waits,
loudly, serving prior state" is the recoverable direction.

AN EMPTY LIST INSTALLS EMPTY. Unlike the classes object — whose ABSENCE leaves
the installed tree alone, because absence cannot be distinguished from
never-authored — a SUCCESSFUL list of the preferences prefix returning zero
objects is a positive observation of authored state: every person's artifact
has been removed, so every installed file is pruned. A list FAULT skips the
cycle entirely (the poll loop's signature read returns ``None``), so an R2
outage can never read as "everyone deleted their preferences".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config_applier.applier import atomic_write
from shared.ids import iso_utc, sha256
from shared.person_prefs import (
    MAX_PREF_BODY_BYTES,
    PREFS_MANIFEST_NAME,
    PREFS_SUBDIR,
    normalize_person_address,
    person_slug,
)
from spec_applier.applier import (
    _DIR_MODE,
    _FILE_MODE,
    SCHEMA_VERSION,
    SpecApplyError,
    SpecApplyOutcome,
    _harden,
    _harden_ancestors,
)

logger = logging.getLogger(__name__)

_PREVIOUS_SUFFIX = ".previous.json"


def preferences_prefix(slug: str) -> str:
    """The R2 prefix under which a customer's per-person preferences live."""
    if not isinstance(slug, str) or not slug.strip():
        raise SpecApplyError("preferences_prefix: customer slug is required")
    return f"vaults/{slug.strip()}/preferences/"


def person_pref_key(slug: str, pslug: str) -> str:
    return f"{preferences_prefix(slug)}{pslug}.json"


def previous_person_pref_key(slug: str, pslug: str) -> str:
    """The single fixed recovery key — one generation deep, applier-parity."""
    return f"{preferences_prefix(slug)}{pslug}{_PREVIOUS_SUFFIX}"


@dataclass(frozen=True)
class ParsedPref:
    """One verified per-person preference object, ready to install verbatim."""

    slug: str
    person: str
    raw: bytes
    #: Digest of the OBJECT bytes as pulled — what the manifest records, what
    #: the intake's converge-wait polls for, and what the installed file hashes
    #: to (the object is installed verbatim).
    digest: str

    @property
    def rel_path(self) -> str:
        return f"{PREFS_SUBDIR}/{self.slug}.json"


@dataclass(frozen=True)
class PrefApplyResult:
    """Structured result of a preference apply cycle (applier-result parity)."""

    outcome: SpecApplyOutcome
    reasons: tuple[str, ...] = ()
    installed: tuple[str, ...] = ()
    pruned: tuple[str, ...] = ()
    source_digest: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return self.outcome is SpecApplyOutcome.APPLIED


# ---------------------------------------------------------------------------
# Step 1 — list + pull
# ---------------------------------------------------------------------------


def list_pref_keys(s3_client: Any, bucket: str, slug: str) -> list[str]:
    """Every current preference object key under the customer's prefix, sorted.

    ``*.previous.json`` recovery copies are excluded — they are the rollback
    state, not the authored state. Raises :class:`SpecApplyError` on any list
    fault: a failed list must SKIP the cycle, never read as an empty prefix
    (the module docstring's outage-vs-deletion distinction).
    """
    prefix = preferences_prefix(slug)
    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            page = s3_client.list_objects_v2(**kwargs)
        except Exception as exc:  # noqa: BLE001 — any list fault skips the cycle
            raise SpecApplyError(
                f"list_pref_keys: could not list s3://{bucket}/{prefix}: {exc}"
            ) from exc
        if not isinstance(page, dict):
            raise SpecApplyError(f"list_pref_keys: non-dict list response for {prefix}")
        for entry in page.get("Contents") or []:
            key = entry.get("Key") if isinstance(entry, dict) else None
            if not isinstance(key, str):
                continue
            if key.endswith(_PREVIOUS_SUFFIX) or not key.endswith(".json"):
                continue
            keys.append(key)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
    return sorted(keys)


def _pull(s3_client: Any, bucket: str, key: str) -> bytes:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        raise SpecApplyError(f"could not read s3://{bucket}/{key}: {exc}") from exc
    body = response.get("Body") if isinstance(response, dict) else getattr(response, "Body", None)
    if body is None:
        raise SpecApplyError(f"response for {key} has no Body")
    data = body.read()
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)):
        raise SpecApplyError(f"body of {key} is not bytes (got {type(data).__name__})")
    return bytes(data)


# ---------------------------------------------------------------------------
# Steps 2 + 3 — parse and verify
# ---------------------------------------------------------------------------


def parse_pref_object(key: str, data: bytes) -> tuple[ParsedPref | None, list[str]]:
    """Parse and verify one preference object. Returns ``(pref, errors)``.

    Three identity checks bind the object to exactly one person: the ``person``
    address must validate, the stored ``person_slug`` must equal the SERVER-SIDE
    derivation from that address, and the object's key basename must equal the
    slug. A mismatch anywhere is an object whose location and content disagree
    about whose preferences these are — refused whole, never repaired.
    """
    name = key.rsplit("/", 1)[-1]
    errors: list[str] = []
    try:
        doc = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        return None, [f"{name}: not valid UTF-8: {exc}"]
    except json.JSONDecodeError as exc:
        return None, [f"{name}: not valid JSON: {exc}"]
    if not isinstance(doc, dict):
        return None, [f"{name}: must be a JSON object"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        return None, [f"{name}: schema_version must be {SCHEMA_VERSION}"]

    person = normalize_person_address(doc.get("person"))
    if person is None:
        return None, [f"{name}: person is not a valid person address"]
    try:
        derived = person_slug(person)
    except ValueError as exc:  # pragma: no cover — normalize already passed
        return None, [f"{name}: {exc}"]
    stored_slug = doc.get("person_slug")
    if stored_slug != derived:
        errors.append(
            f"{name}: person_slug {stored_slug!r} does not match the server-side "
            f"derivation {derived!r} for {person}"
        )
    if name != f"{derived}.json":
        errors.append(f"{name}: object key does not match the derived slug {derived!r}")

    body = doc.get("body")
    if not isinstance(body, str) or not body.strip():
        errors.append(f"{name}: body must be a non-empty string")
    else:
        encoded = body.encode("utf-8")
        if len(encoded) > MAX_PREF_BODY_BYTES:
            errors.append(
                f"{name}: body is {len(encoded)} bytes; the ceiling is {MAX_PREF_BODY_BYTES}"
            )
        declared = doc.get("sha256")
        if not isinstance(declared, str) or sha256(encoded) != declared.strip().lower():
            errors.append(f"{name}: body does not hash to its declared sha256")
    assertions = doc.get("assertions", {})
    if not isinstance(assertions, dict):
        errors.append(f"{name}: assertions must be an object when present")

    if errors:
        return None, errors
    return ParsedPref(slug=derived, person=person, raw=data, digest=sha256(data) or ""), []


# ---------------------------------------------------------------------------
# Steps 4 + 5 — install root-owned, then commit the manifest
# ---------------------------------------------------------------------------


def _read_installed_manifest(spec_dir: Path) -> dict[str, Any]:
    path = spec_dir / PREFS_MANIFEST_NAME
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "spec_applier(preferences): manifest at %s unreadable (%s); treating as absent",
            path,
            exc,
        )
        return {}
    return raw if isinstance(raw, dict) else {}


def _install(
    spec_dir: Path, prefs: list[ParsedPref], source_digest: str, slug: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Write every object verbatim, commit the manifest, prune the rest.

    Same transactional ordering as the spec install: bodies first (a reader in
    the window mismatches against the old manifest and fails closed), manifest
    second and atomically (the commit point), prune last.
    """
    spec_dir.mkdir(parents=True, exist_ok=True)
    _harden(spec_dir, _DIR_MODE)

    entries: dict[str, dict[str, Any]] = {}
    installed: list[str] = []
    for pref in prefs:
        target = spec_dir / pref.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        _harden_ancestors(spec_dir, target.parent)
        atomic_write(target, pref.raw)
        _harden(target, _FILE_MODE)
        installed.append(pref.rel_path)
        entries[pref.slug] = {
            "person": pref.person,
            "rel_path": pref.rel_path,
            # Computed HERE over the bytes this process wrote (which are the
            # object bytes verbatim) — the intake's converge-wait polls this
            # value, and the pointer injection cites it.
            "sha256": sha256(pref.raw),
            "bytes": len(pref.raw),
        }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "customer": slug,
        "source_digest": source_digest,
        "installed_at": iso_utc(),
        "preferences": entries,
    }
    manifest_path = spec_dir / PREFS_MANIFEST_NAME
    atomic_write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n")
    _harden(manifest_path, _FILE_MODE)

    keep = {entry["rel_path"] for entry in entries.values()}
    pruned = _prune(spec_dir, keep)
    return tuple(installed), pruned


def _prune(spec_dir: Path, keep: set[str]) -> tuple[str, ...]:
    """Remove installed preference files the new manifest no longer names.

    Scoped to ``preferences/*.json`` so nothing outside this module's tree is
    ever deleted by it. This is the "gone means gone" path for a removed
    person: delete the vault object, and the next cycle prunes the seat file.
    """
    root = spec_dir / PREFS_SUBDIR
    if not root.is_dir():
        return ()
    pruned: list[str] = []
    for path in sorted(root.glob("*.json")):
        rel = path.relative_to(spec_dir).as_posix()
        if rel in keep:
            continue
        try:
            path.unlink()
            pruned.append(rel)
        except OSError as exc:
            logger.warning("spec_applier(preferences): could not prune %s: %s", path, exc)
    return tuple(pruned)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def apply_preferences(
    *,
    s3_client: Any,
    bucket: str,
    slug: str,
    spec_dir: str | Path,
) -> PrefApplyResult:
    """Pull, verify, and install every per-person preference for ``slug``.

    Returns APPLIED / REJECTED / UNCHANGED. Whole-cycle fail-static: one
    rejected object refuses everything and the installed tree stands.

    Raises :class:`SpecApplyError` on a list/read/write fault the cycle cannot
    recover from — the caller skips the tick and retries.
    """
    target = Path(spec_dir)
    keys = list_pref_keys(s3_client, bucket, slug)
    pulled: list[tuple[str, bytes]] = [(key, _pull(s3_client, bucket, key)) for key in keys]

    joined = "\n".join(f"{key}:{sha256(data)}" for key, data in pulled)
    source_digest = sha256(joined.encode("utf-8")) or ""
    if _read_installed_manifest(target).get("source_digest") == source_digest:
        return PrefApplyResult(outcome=SpecApplyOutcome.UNCHANGED, source_digest=source_digest)

    prefs: list[ParsedPref] = []
    errors: list[str] = []
    for key, data in pulled:
        pref, pref_errors = parse_pref_object(key, data)
        errors.extend(pref_errors)
        if pref is not None:
            prefs.append(pref)
    seen: dict[str, str] = {}
    for pref in prefs:
        if pref.slug in seen:  # pragma: no cover — key==slug makes this unreachable
            errors.append(f"duplicate person slug {pref.slug!r}")
        seen[pref.slug] = pref.person
    if errors:
        return PrefApplyResult(
            outcome=SpecApplyOutcome.REJECTED,
            reasons=tuple(errors),
            source_digest=source_digest,
        )

    try:
        installed, pruned = _install(target, prefs, source_digest, slug)
    except OSError as exc:
        raise SpecApplyError(f"apply_preferences: installing into {target} failed: {exc}") from exc
    return PrefApplyResult(
        outcome=SpecApplyOutcome.APPLIED,
        installed=installed,
        pruned=pruned,
        source_digest=source_digest,
        metadata={"preference_count": len(installed)},
    )


__all__ = [
    "ParsedPref",
    "PrefApplyResult",
    "apply_preferences",
    "list_pref_keys",
    "parse_pref_object",
    "person_pref_key",
    "preferences_prefix",
    "previous_person_pref_key",
]
