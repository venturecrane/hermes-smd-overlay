"""Reader for the root-owned spec manifest (ss ADR 0083, ss-console #2084).

The single place that answers "what specs are installed on this seat, and does
the file on disk still match what root wrote?" Two consumers, deliberately:

* ``bootstrap/translate.py`` — renders the per-profile SKILL.md POINTER stamp
  from these entries at boot.
* ``plugins/hermes-smd-trust`` — verifies a ``read_file`` of a spec against
  these entries before marking the read as having happened.

WHY THE TRUST CONSUMER MUST NOT READ THE STAMP. ``<profile>/skills/`` is
hermes-owned by construction: ``translate.py`` writes it, the agent's own
``skill_manage`` can edit it, and the whole skills tree is refreshed from the
catalog every boot. An agent could therefore rewrite its own stamp and forge
both the pointer and the hash. The stamp is DELIVERY — it tells the model where
to look. This manifest is ENFORCEMENT — it is root-owned, the agent cannot write
it, and it is the only thing the read-mark is allowed to believe. Two artifacts,
two trust levels, and conflating them would make the enforcement half
self-certifying.

Nothing here caches. A spec can be replaced under a running Machine by the
root poller, and a cached manifest would serve a stale hash — which reads
identically to a tamper. The files are a few KB and the read happens on a
``read_file`` of a spec path, not on every tool call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Env var naming the root-owned installed-spec directory. Exported by
#: ``operator/templates/entrypoint.sh``; declared in
#: ``operator/contracts/env-consumption.yaml`` and ``contracts/consumes.yaml``.
SPEC_DIR_ENV = "SMD_SPEC_DIR"

_MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class SpecEntry:
    """One installed spec, as the root-owned manifest describes it."""

    rel_path: str
    output_class: str
    prop: str
    sha256: str
    #: Machine-checkable shape rules, or {}. Root-recorded by the applier from
    #: the customer's vault object; the format gate reads them from here and
    #: never from anything the agent can write.
    assertions: dict = field(default_factory=dict)

    def path_under(self, spec_dir: Path) -> Path:
        return spec_dir / self.rel_path


def spec_dir() -> Path | None:
    """The configured spec dir, or ``None`` when unset / absent.

    ``None`` means "this seat has no installed spec tree", which every caller
    must read as "nothing is authored here that I can verify" — never as "the
    check passed".
    """
    raw = os.environ.get(SPEC_DIR_ENV)
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


#: ``manifest_state`` results. See that function for why the distinction exists.
STATE_OK = "ok"
STATE_ABSENT = "absent"
STATE_UNREADABLE = "unreadable"


def manifest_state(directory: Path | None = None) -> str:
    """Can this process PROVE what is installed? ``ok`` / ``absent`` / ``unreadable``.

    ``load_entries`` deliberately collapses every failure into ``{}`` because its
    consumers all fail closed on empty. One consumer now needs the opposite
    question — not "what is installed" but "is an empty answer EVIDENCE of
    nothing installed, or evidence of nothing readable" — and the collapse
    cannot express it (ss-console #2234).

    The distinction is load-bearing, not pedantic. ``spec_gate`` lets a `staff`
    send proceed when a declared spec was never installed; if a process that
    simply cannot SEE the spec tree reported the same state, a lost
    ``SMD_SPEC_DIR`` would silently unlock autonomous sends. That failure would
    be invisible: ``operator/safety-substrate/invariants/spec_dir_ownership.py``
    documents that an ABSENT directory PASSES the boot gate, and the heartbeat
    that reports seat health runs in the gateway process while this gate runs in
    the agent process — so the two can disagree about the env with every health
    signal green. Hence:

    * ``unreadable`` — ``SMD_SPEC_DIR`` unset, or not a directory, or the
      manifest is present but unparseable. **This process cannot prove
      anything.** Never treat it as absence.
    * ``absent`` — the spec dir exists and holds no manifest. The applier writes
      ``manifest.json`` LAST as its commit point and ``entrypoint.sh`` creates
      the dir on every boot, so a dir with no manifest is the ordinary
      nothing-was-ever-installed state.
    * ``ok`` — the manifest parsed. An entry's absence from a parsed manifest is
      affirmative evidence that spec is not installed.
    """
    base = directory if directory is not None else spec_dir()
    if base is None:
        return STATE_UNREADABLE
    try:
        doc = json.loads((base / _MANIFEST_NAME).read_text())
    except FileNotFoundError:
        return STATE_ABSENT
    except (OSError, json.JSONDecodeError):
        return STATE_UNREADABLE
    if not isinstance(doc, dict) or not isinstance(doc.get("specs"), dict):
        return STATE_UNREADABLE
    return STATE_OK


def load_entries(directory: Path | None = None) -> dict[str, SpecEntry]:
    """Load the manifest, keyed by manifest-relative path.

    Returns ``{}`` on any absence or malformation — an unreadable manifest is
    indistinguishable from no manifest for every purpose THIS function's
    consumers have, and all of them fail closed on an empty result rather than
    open. A caller that needs to tell those apart calls ``manifest_state``
    alongside this, and must not infer the difference from emptiness here.
    """
    base = directory if directory is not None else spec_dir()
    if base is None:
        return {}
    manifest_path = base / _MANIFEST_NAME
    try:
        doc = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("spec_manifest: %s unreadable (%s); treating as empty", manifest_path, exc)
        return {}
    if not isinstance(doc, dict):
        return {}
    raw_specs = doc.get("specs")
    if not isinstance(raw_specs, dict):
        return {}

    entries: dict[str, SpecEntry] = {}
    for rel, meta in raw_specs.items():
        if not isinstance(rel, str) or not isinstance(meta, dict):
            continue
        output_class = meta.get("class")
        prop = meta.get("property")
        digest = meta.get("sha256")
        if not (
            isinstance(output_class, str) and isinstance(prop, str) and isinstance(digest, str)
        ):
            continue
        raw_assertions = meta.get("assertions")
        entries[rel] = SpecEntry(
            rel_path=rel,
            output_class=output_class,
            prop=prop,
            sha256=digest,
            assertions=raw_assertions if isinstance(raw_assertions, dict) else {},
        )
    return entries


def entry_for_path(read_path: str, directory: Path | None = None) -> SpecEntry | None:
    """Resolve a path the agent is about to read to its manifest entry.

    Returns ``None`` when the path is outside the spec dir or is not named by
    the manifest. A path under the spec dir that the manifest does not name is
    NOT an error — it is a file the applier pruned or never wrote — but it is
    also not a spec, so reading it establishes nothing.

    Symlinks are resolved before the containment check so a link planted inside
    the spec dir cannot make an arbitrary file answer to a spec's identity. The
    spec dir is root-owned, so planting one requires root; resolving anyway
    costs nothing and removes the need to reason about it.
    """
    base = directory if directory is not None else spec_dir()
    if base is None or not isinstance(read_path, str) or not read_path:
        return None
    try:
        resolved = Path(read_path).resolve()
        root = base.resolve()
        rel = resolved.relative_to(root).as_posix()
    except (ValueError, OSError):
        return None
    return load_entries(base).get(rel)


def verify(entry: SpecEntry, directory: Path | None = None) -> bool:
    """True iff the file on disk still hashes to what root recorded.

    False on any read failure. The caller treats False as "this read does not
    count", which leaves an expected-spec gate refusing — the safe direction.
    """
    base = directory if directory is not None else spec_dir()
    if base is None:
        return False
    try:
        data = entry.path_under(base).read_bytes()
    except OSError as exc:
        logger.debug("spec_manifest: cannot read %s for verification (%s)", entry.rel_path, exc)
        return False
    return hashlib.sha256(data).hexdigest() == entry.sha256


def entries_for_class(output_class: str, directory: Path | None = None) -> list[SpecEntry]:
    """Every installed spec belonging to ``output_class``, sorted by property."""
    return sorted(
        (e for e in load_entries(directory).values() if e.output_class == output_class),
        key=lambda e: e.prop,
    )


__all__ = [
    "SPEC_DIR_ENV",
    "STATE_ABSENT",
    "STATE_OK",
    "STATE_UNREADABLE",
    "SpecEntry",
    "entries_for_class",
    "entry_for_path",
    "load_entries",
    "manifest_state",
    "spec_dir",
    "verify",
]
