"""Per-person preference identity + root-manifest reader (ss ADR 0085 §6, ss#2067).

Any rostered person customizes how their OWN work sounds and is shaped by
telling the Operator. This module is the shared vocabulary of that feature:

* :func:`normalize_person_address` — the one address validator, mirroring
  ``CustomerConfig.admins``'s rules (a person, never a domain), so the plugin
  gate, the intake, and the applier all refuse the same malformed inputs.
* :func:`person_slug` — the SERVER-SIDE slug derivation. The slug is derived
  from the rostered address by the root intake and re-derived by the applier as
  a cross-check; it is never accepted from the wire, because the slug is a path
  segment in both R2 (``vaults/<slug>/preferences/<person-slug>.json``) and the
  root-owned install tree (``<SMD_SPEC_DIR>/preferences/<person-slug>.json``).
* :func:`load_person_entries` / :func:`entry_for_sender` — the reader of the
  root-owned ``preferences-manifest.json``, the ENFORCEMENT half of the same
  trust split ``shared.spec_manifest`` documents: the manifest is root-written
  and the agent cannot forge it; whatever pointer the model is shown is
  DELIVERY and is never believed by anything that checks.

Personal preferences REFINE the firm layer; they never satisfy or replace a
firm spec's read requirement. Nothing in ``shared.spec_gate`` reads this module
— that absence is deliberate and load-bearing (ADR 0085 §6: the firm layer is
the floor).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Subdirectory of ``SMD_SPEC_DIR`` holding installed per-person preference
#: files. Sibling of ``classes/`` — same root-owned, agent-readable tree.
PREFS_SUBDIR = "preferences"

#: The root-computed manifest for installed preferences. A SIBLING of the spec
#: ``manifest.json``, deliberately not a key inside it: the firm-spec manifest
#: is an enforcement surface read by the spec gate and the read-mark, and its
#: parser, fingerprint, and fail-static rules must not grow a second concern.
PREFS_MANIFEST_NAME = "preferences-manifest.json"

#: Ceiling on one person's preference body — spec_applier parity.
MAX_PREF_BODY_BYTES = 256 * 1024

#: Truncation point of the readable half of the slug; the hash suffix carries
#: uniqueness, the readable half carries greppability.
_SLUG_READABLE_MAX = 40


def normalize_person_address(value: object) -> str | None:
    """Normalize + validate a person's email address, or ``None``.

    The same person-not-domain rules as ``CustomerConfig.admins``: exactly one
    ``@``, a non-empty local part, and a dotted domain. Refuses (returns
    ``None``) rather than repairing — a malformed subject must never become a
    quietly different subject.
    """
    if not isinstance(value, str):
        return None
    norm = value.strip().lower()
    if not norm or norm.count("@") != 1 or norm.startswith("@"):
        return None
    local, _, domain = norm.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return norm


def person_slug(address: str) -> str:
    """Derive the filesystem/R2 slug for a person's preference artifact.

    Deterministic and collision-free: a readable sanitized prefix (lowercase
    alphanumerics, runs of anything else collapsed to ``-``) plus the first 8
    hex of the address's sha256. The suffix is not decoration — two rostered
    addresses that sanitize identically (``a.b@x.com`` / ``a-b@x.com``) would
    otherwise MERGE two people's preferences, which is a correctness failure,
    not a cosmetic one.

    Raises ``ValueError`` on an address :func:`normalize_person_address`
    refuses, so no caller can derive a slug for a subject the validator would
    not accept.
    """
    norm = normalize_person_address(address)
    if norm is None:
        raise ValueError(f"person_slug: not a valid person address: {address!r}")
    out: list[str] = []
    for ch in norm:
        if ch.isascii() and (ch.islower() or ch.isdigit()):
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    readable = "".join(out).strip("-")[:_SLUG_READABLE_MAX].rstrip("-")
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}" if readable else digest


@dataclass(frozen=True)
class PersonPrefEntry:
    """One installed per-person preference file, as the root manifest records it."""

    slug: str
    person: str
    rel_path: str
    sha256: str
    size: int

    def path_under(self, spec_dir: Path) -> Path:
        return spec_dir / self.rel_path


def load_person_entries(directory: Path | None = None) -> dict[str, PersonPrefEntry]:
    """Load the preferences manifest, keyed by person slug.

    Returns ``{}`` on any absence or malformation — same posture as
    ``spec_manifest.load_entries``: an unreadable manifest is indistinguishable
    from no manifest for every consumer, and every consumer treats an empty
    result as "no preferences installed", never as an error to act on.
    """
    from shared import spec_manifest

    base = directory if directory is not None else spec_manifest.spec_dir()
    if base is None:
        return {}
    manifest_path = base / PREFS_MANIFEST_NAME
    try:
        doc = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("person_prefs: %s unreadable (%s); treating as empty", manifest_path, exc)
        return {}
    if not isinstance(doc, dict):
        return {}
    raw = doc.get("preferences")
    if not isinstance(raw, dict):
        return {}
    entries: dict[str, PersonPrefEntry] = {}
    for slug, meta in raw.items():
        if not isinstance(slug, str) or not isinstance(meta, dict):
            continue
        person = meta.get("person")
        rel_path = meta.get("rel_path")
        digest = meta.get("sha256")
        size = meta.get("bytes")
        if not (isinstance(person, str) and isinstance(rel_path, str) and isinstance(digest, str)):
            continue
        entries[slug] = PersonPrefEntry(
            slug=slug,
            person=person,
            rel_path=rel_path,
            sha256=digest,
            size=size if isinstance(size, int) else 0,
        )
    return entries


def entry_for_sender(sender: object, directory: Path | None = None) -> PersonPrefEntry | None:
    """The installed preference entry for an attributed sender, or ``None``.

    Matches on the normalized address, so the manifest's stored form and the
    channel's sender attribution agree on case and whitespace. A sender the
    validator refuses matches nothing.
    """
    norm = normalize_person_address(sender)
    if norm is None:
        return None
    for entry in load_person_entries(directory).values():
        if normalize_person_address(entry.person) == norm:
            return entry
    return None


__all__ = [
    "MAX_PREF_BODY_BYTES",
    "PREFS_MANIFEST_NAME",
    "PREFS_SUBDIR",
    "PersonPrefEntry",
    "entry_for_sender",
    "load_person_entries",
    "normalize_person_address",
    "person_slug",
]
