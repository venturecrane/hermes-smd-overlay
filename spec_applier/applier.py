"""Root-owned authored-spec install (ss ADR 0083, ss-console #2084).

Five steps, every side effect injected so the module is fully unit-testable:

    pull (R2) → parse → hash-verify → install root-owned → write manifest

:func:`apply` never raises on a rejected document — it returns a structured
:class:`SpecApplyResult`. It raises only on an R2 fault the caller cannot
recover from, and even then the installed tree is untouched.

THE SOURCE OBJECT. ``vaults/<slug>/output-classes.json``, written by the portal
and by nothing else. The git→R2 config publisher (ss-console #2082) writes
``customer.yaml`` and is structurally barred from this key: two writers, two key
spaces, never the same object. That separation is why a portal edit can reach a
seat at all without a portal actor being able to write git.

WHAT THE DECLARED HASH IS AND IS NOT. Each spec body carries a ``sha256`` its
author computed. Verifying it catches a truncated body, a torn portal write, and
a body/hash pair that disagree with each other. It does NOT authenticate the
document — a writer who can put bytes in the vault can put a matching hash next
to them. Authentication comes from elsewhere and is structural: the vault object
is writable only with an operator R2 credential the agent never holds, and the
install target is root-owned on a box where the agent is not root. The hash is
an integrity check on a trusted channel, not a signature, and calling it one
would be the kind of overclaim this substrate exists to avoid.

FAIL-STATIC. Any rejection installs NOTHING and leaves the previous tree exactly
as it stood. There is no partial install and no blanking: a seat that cannot
adopt a new spec keeps serving the spec it already had. "Refuse the update" and
"remove the spec" are different outcomes, and only the first is ever correct
here — the second would silently convert an authored class into an unauthored
one, which is the precise confusion ``output_classes:`` was added to end.

THE MANIFEST IS ROOT-COMPUTED, AND IS THE COMMIT POINT. ``manifest.json`` is
written LAST, atomically, after every body has landed. Its hashes are computed
by THIS process over the bytes it wrote — never copied from the source document
— because the manifest is what the runtime read-mark verifies against, and a
manifest that merely echoed the source's claims would verify the source against
itself. A reader that catches the window between new bodies and the new manifest
sees a hash mismatch and fails closed, which is the safe direction.
"""

from __future__ import annotations

import enum
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config_applier.applier import atomic_write
from shared.ids import iso_utc, sha256

logger = logging.getLogger(__name__)

#: Schema version of both the source document and the installed manifest.
SCHEMA_VERSION = 1

#: Filename of the root-computed manifest inside ``SMD_SPEC_DIR``.
MANIFEST_NAME = "manifest.json"

#: Subdirectory holding the per-class spec bodies inside ``SMD_SPEC_DIR``.
CLASSES_SUBDIR = "classes"

#: The two spec properties an output class can carry. Gates and delivery are
#: deliberately NOT here: they already have an authority (persona exposure
#: ceilings and the routine grid), and a second authority over one behaviour is
#: the drift the output-class registry exists to end.
SPEC_PROPERTIES = ("voice", "format")

#: Upper bound on a single spec body. A spec is prose a person wrote; anything
#: past this is a mistake or an attempt to flood the drafting context, and
#: either way the document is refused rather than truncated.
MAX_SPEC_BYTES = 256 * 1024

#: Ownership + permissions of everything this package installs. The dir is
#: traversable and the files readable by the agent uid; NOTHING here is
#: writable by it. That asymmetry is the whole security property.
_DIR_MODE = 0o755
_FILE_MODE = 0o644


class SpecApplyError(RuntimeError):
    """An unrecoverable fault (R2 read failure, install write failure).

    A REJECTED document is not an error — it is returned in
    :class:`SpecApplyResult`. This is raised only when the caller cannot
    proceed. The installed tree is never left half-replaced: bodies are written
    atomically and the manifest, which is what any reader verifies against, is
    replaced last.
    """


class SpecApplyOutcome(str, enum.Enum):
    """Terminal outcome of an apply attempt."""

    APPLIED = "applied"  # installed root-owned + manifest written
    REJECTED = "rejected"  # malformed / hash mismatch — nothing installed
    UNCHANGED = "unchanged"  # source digest equals the installed manifest's


@dataclass(frozen=True)
class SpecApplyResult:
    """Structured result of an apply attempt.

    ``reasons`` carries the human-readable rejection reasons (empty on success).
    ``installed`` lists the manifest-relative paths written this cycle;
    ``pruned`` lists paths removed because the new document no longer declares
    them. ``source_digest`` is the sha256 of the pulled object bytes, which is
    also the change-detection key.
    """

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
# Step 1 — pull from R2
# ---------------------------------------------------------------------------


def spec_object_key(slug: str) -> str:
    """The R2 object key for a customer's authored specs.

    ``vaults/<slug>/output-classes.json`` — the same ``vaults/<slug>/`` isolation
    prefix every other per-customer object uses. A blank slug is refused so a
    pull can never address the vault root.
    """
    if not isinstance(slug, str) or not slug.strip():
        raise SpecApplyError("spec_object_key: customer slug is required")
    return f"vaults/{slug.strip()}/output-classes.json"


class SpecObjectMissing(SpecApplyError):
    """The vault object does not exist.

    Distinguished from every other R2 fault because it is the ORDINARY state of
    a seat whose customer has authored no spec. The caller logs it at info and
    leaves the (empty) installed tree alone; it is not a failure.
    """


def pull_specs(s3_client: Any, bucket: str, slug: str) -> bytes:
    """Read ``vaults/<slug>/output-classes.json`` from R2 and return raw bytes.

    ``s3_client`` is an injected boto3-style client exposing ``get_object``.
    Raises :class:`SpecObjectMissing` when the object is absent (an expected,
    non-failing state) and :class:`SpecApplyError` on any other fault.
    """
    key = spec_object_key(slug)
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 — classified below, then wrapped
        if _is_missing_object(exc):
            raise SpecObjectMissing(f"no spec object at s3://{bucket}/{key}") from exc
        raise SpecApplyError(f"pull_specs: could not read s3://{bucket}/{key}: {exc}") from exc

    body = response.get("Body") if isinstance(response, dict) else getattr(response, "Body", None)
    if body is None:
        raise SpecApplyError(f"pull_specs: response for {key} has no Body")
    try:
        data = body.read()
    except Exception as exc:  # noqa: BLE001
        raise SpecApplyError(f"pull_specs: reading body of {key} failed: {exc}") from exc

    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)):
        raise SpecApplyError(f"pull_specs: body of {key} is not bytes (got {type(data).__name__})")
    return bytes(data)


def _is_missing_object(exc: BaseException) -> bool:
    """True when an S3 exception means 'the key does not exist'.

    boto3 raises a generated ``NoSuchKey``/``404`` ClientError whose class is not
    importable without botocore, so the check is on the shape of the error rather
    than its type — the same posture the rest of the overlay takes toward boto3.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            if str(error.get("Code", "")) in {"NoSuchKey", "404", "NotFound"}:
                return True
    return type(exc).__name__ in {"NoSuchKey", "NoSuchKeyError"}


# ---------------------------------------------------------------------------
# Steps 2 + 3 — parse and hash-verify
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedSpec:
    """One verified spec body, ready to install."""

    output_class: str
    prop: str
    body: bytes
    digest: str

    @property
    def rel_path(self) -> str:
        return f"{CLASSES_SUBDIR}/{self.output_class}/{self.prop}.md"


def parse_and_verify(data: bytes) -> tuple[list[ParsedSpec], list[str]]:
    """Parse the source document and verify every declared hash.

    Returns ``(specs, errors)``. A non-empty ``errors`` means the WHOLE document
    is refused — there is no partial adoption, because a document half of which
    failed integrity is a document whose author and whose bytes disagree, and
    nothing in it can be trusted more than the part that failed.
    """
    errors: list[str] = []
    try:
        doc = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        return [], [f"output-classes.json is not valid UTF-8: {exc}"]
    except json.JSONDecodeError as exc:
        return [], [f"output-classes.json is not valid JSON: {exc}"]

    if not isinstance(doc, dict):
        return [], ["output-classes.json must be a JSON object"]

    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        return [], [
            f"schema_version must be {SCHEMA_VERSION}; got {version!r} "
            "(an unknown schema is refused, never best-effort parsed)"
        ]

    classes = doc.get("classes")
    if not isinstance(classes, dict):
        return [], ["classes must be an object mapping class slug -> properties"]

    specs: list[ParsedSpec] = []
    for raw_slug, entry in sorted(classes.items()):
        slug = str(raw_slug)
        if not _safe_slug(slug):
            errors.append(f"classes.{slug!r}: class slug must match [a-z0-9_-]+")
            continue
        if not isinstance(entry, dict):
            errors.append(f"classes.{slug}: must be an object")
            continue
        for prop in SPEC_PROPERTIES:
            raw = entry.get(prop)
            if raw is None:
                continue  # this class authors nothing for this property
            spec, spec_errors = _parse_one(slug, prop, raw)
            errors.extend(spec_errors)
            if spec is not None:
                specs.append(spec)

    if not specs and not errors:
        errors.append("output-classes.json declares no spec bodies")
    if errors:
        # Discard the specs that DID verify. The all-or-nothing rule is enforced
        # here rather than only in the caller so no future caller can adopt a
        # partial document by reading the list and ignoring the errors.
        return [], errors
    return specs, errors


def _parse_one(slug: str, prop: str, raw: Any) -> tuple[ParsedSpec | None, list[str]]:
    """Parse and hash-verify one ``classes.<slug>.<prop>`` entry."""
    path = f"classes.{slug}.{prop}"
    if not isinstance(raw, dict):
        return None, [f"{path}: must be an object with `body` and `sha256`"]
    body = raw.get("body")
    declared = raw.get("sha256")
    if not isinstance(body, str) or not body.strip():
        return None, [f"{path}.body: must be a non-empty string"]
    if not isinstance(declared, str) or not declared.strip():
        return None, [f"{path}.sha256: must be the hex sha256 of `body`"]
    encoded = body.encode("utf-8")
    if len(encoded) > MAX_SPEC_BYTES:
        return None, [
            f"{path}.body: {len(encoded)} bytes exceeds the {MAX_SPEC_BYTES}-byte ceiling"
        ]
    actual = sha256(encoded)
    if actual != declared.strip().lower():
        return None, [
            f"{path}: declared sha256 {declared.strip().lower()!r} does not match the "
            f"body's actual digest {actual!r} — the document and its own integrity "
            "claim disagree, so the whole document is refused"
        ]
    return ParsedSpec(output_class=slug, prop=prop, body=encoded, digest=actual or ""), []


def _safe_slug(slug: str) -> bool:
    """True for a class slug safe to use as a path segment.

    Deliberately strict rather than sanitizing: a slug carrying ``/`` or ``..``
    is an escape attempt or a bug, and both deserve a refusal rather than a
    quietly-rewritten path.
    """
    if not slug or len(slug) > 64:
        return False
    return all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in slug) and slug.islower()


# ---------------------------------------------------------------------------
# Steps 4 + 5 — install root-owned, then commit the manifest
# ---------------------------------------------------------------------------


def _harden(path: Path, mode: int) -> None:
    """Force ``root:root`` + ``mode`` onto a path this process just created.

    ``atomic_write`` preserves an EXISTING target's owner/mode and, for a new
    target, leaves ``mkstemp``'s ``root:root 0600`` — which for this tree would
    mean the agent cannot read the spec it is required to read. So ownership and
    mode are asserted explicitly after every write, on new and existing files
    alike. chown failure is logged rather than raised: off-box (tests, a dev
    run) the process is not root and cannot chown, and the boot invariant
    — not this function — is what refuses to serve a tree with the wrong owner.
    """
    try:
        os.chown(path, 0, 0)
    except OSError as exc:
        logger.debug("spec_applier: could not chown %s to root:root (%s)", path, exc)
    try:
        os.chmod(path, mode)
    except OSError as exc:
        logger.warning("spec_applier: could not chmod %s to %o: %s", path, mode, exc)


def _read_installed_manifest(spec_dir: Path) -> dict[str, Any]:
    """Best-effort read of the currently installed manifest, or ``{}``.

    An unreadable or corrupt manifest reads as absent: every path then looks
    changed and is rewritten, which is the recoverable direction.
    """
    manifest_path = spec_dir / MANIFEST_NAME
    try:
        raw = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "spec_applier: installed manifest at %s unreadable (%s); treating as absent",
            manifest_path,
            exc,
        )
        return {}
    return raw if isinstance(raw, dict) else {}


def _install(
    spec_dir: Path, specs: list[ParsedSpec], source_digest: str, slug: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Write every body, commit the manifest, then prune what is no longer declared.

    Ordering is the transactional argument:

    1. Bodies first. A reader in this window verifies a NEW body against the OLD
       manifest, mismatches, and fails closed — the safe direction.
    2. Manifest second, atomically. This is the commit point; the tree is
       consistent the instant the rename lands.
    3. Prune last. A file removed after the manifest stopped naming it can only
       be read as "not in the manifest", which is also a fail-closed read.

    Returns ``(installed_rel_paths, pruned_rel_paths)``.
    """
    spec_dir.mkdir(parents=True, exist_ok=True)
    _harden(spec_dir, _DIR_MODE)

    entries: dict[str, dict[str, Any]] = {}
    installed: list[str] = []
    for spec in specs:
        target = spec_dir / spec.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        _harden(target.parent, _DIR_MODE)
        atomic_write(target, spec.body)
        _harden(target, _FILE_MODE)
        installed.append(spec.rel_path)
        entries[spec.rel_path] = {
            "class": spec.output_class,
            "property": spec.prop,
            # Computed HERE, over the bytes this process wrote — never copied
            # from the source document. The runtime read-mark verifies against
            # this value, so echoing the source's claim would verify the source
            # against itself.
            "sha256": sha256(spec.body),
            "bytes": len(spec.body),
        }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "customer": slug,
        "source_digest": source_digest,
        "installed_at": iso_utc(),
        "specs": entries,
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    manifest_path = spec_dir / MANIFEST_NAME
    atomic_write(manifest_path, manifest_bytes)
    _harden(manifest_path, _FILE_MODE)

    pruned = _prune(spec_dir, set(entries))
    return tuple(installed), pruned


def _prune(spec_dir: Path, keep: set[str]) -> tuple[str, ...]:
    """Remove installed spec bodies the new manifest no longer names.

    Scoped to ``classes/**/*.md`` under the spec dir so a stray file elsewhere is
    never deleted by this process. A removal failure is logged, not raised: a
    leftover body is unreachable through the manifest anyway (an unlisted path
    cannot be marked as read), so it is stale clutter rather than a live risk.
    """
    root = spec_dir / CLASSES_SUBDIR
    if not root.is_dir():
        return ()
    pruned: list[str] = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(spec_dir).as_posix()
        if rel in keep:
            continue
        try:
            path.unlink()
            pruned.append(rel)
        except OSError as exc:
            logger.warning("spec_applier: could not prune stale spec %s: %s", path, exc)
    return tuple(pruned)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def apply(
    *,
    s3_client: Any,
    bucket: str,
    slug: str,
    spec_dir: str | Path,
) -> SpecApplyResult:
    """Pull, verify, and install the authored specs for ``slug``.

    Returns APPLIED / REJECTED / UNCHANGED. Fail-static throughout: a REJECTED
    document leaves the installed tree exactly as it was.

    Raises:
        SpecObjectMissing: no spec object in the vault (the ordinary state of a
            seat whose customer authored nothing). The caller treats this as a
            non-event, not a failure.
        SpecApplyError: an unrecoverable R2 read or install write fault.
    """
    target = Path(spec_dir)
    raw = pull_specs(s3_client, bucket, slug)
    source_digest = sha256(raw) or ""

    installed_manifest = _read_installed_manifest(target)
    if installed_manifest.get("source_digest") == source_digest and source_digest:
        return SpecApplyResult(
            outcome=SpecApplyOutcome.UNCHANGED,
            source_digest=source_digest,
        )

    specs, errors = parse_and_verify(raw)
    if errors:
        # Fail-static. The previously installed tree stands untouched; the seat
        # keeps serving the spec it was serving.
        return SpecApplyResult(
            outcome=SpecApplyOutcome.REJECTED,
            reasons=tuple(errors),
            source_digest=source_digest,
        )

    try:
        installed, pruned = _install(target, specs, source_digest, slug)
    except OSError as exc:
        raise SpecApplyError(f"apply: installing specs into {target} failed: {exc}") from exc

    return SpecApplyResult(
        outcome=SpecApplyOutcome.APPLIED,
        installed=installed,
        pruned=pruned,
        source_digest=source_digest,
        metadata={"spec_count": len(installed)},
    )


__all__ = [
    "CLASSES_SUBDIR",
    "MANIFEST_NAME",
    "MAX_SPEC_BYTES",
    "SCHEMA_VERSION",
    "SPEC_PROPERTIES",
    "ParsedSpec",
    "SpecApplyError",
    "SpecApplyOutcome",
    "SpecApplyResult",
    "SpecObjectMissing",
    "apply",
    "parse_and_verify",
    "pull_specs",
    "spec_object_key",
]
