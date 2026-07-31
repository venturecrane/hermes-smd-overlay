"""Root-owned authored-spec applier (ss ADR 0083, ss-console #2084).

The sibling of :mod:`config_applier`, for the OTHER authored artifact a seat
obeys: the customer's per-output-class voice and format specifications.

WHY THIS EXISTS. `customer.yaml` declares, per output class, whether an authored
spec is EXPECTED (``output_classes.<class>.voice_spec``). It deliberately does
not carry the spec CONTENT — that is prose the customer edits through the
portal, and a portal edit cannot reach git. The content lives in the customer's
own vault object, and until this package there was no code path that read it:
the vault sample reader globs ``*.json`` only, so a ``.md`` spec placed in the
vault was never read by anything, and four drafting skills conditioned on "if
the seat carries an authored voice profile, apply it" — a condition nothing made
true.

WHY ROOT, and why this is the part not to ship without. The install target must
not be writable by the hermes uid. ``read_file`` is a READ-class tool: it is
unfenced and does not taint the session. An agent-writable spec is therefore a
persistent, untainted, self-authored prompt-injection channel that survives
restarts — strictly worse than a tainted inbound email, which at least fences.
This repo paid for that lesson once already: ``operator/templates/entrypoint.sh``
records the self-loopback hole proven live on hermes-smd-staging 2026-06-15,
where an agent-writable copy of ``customer.yaml`` let the agent rewrite its own
trust ceiling with one ``sed``. The fix then was root ownership, not policy, and
it is root ownership here too.

FAIL-STATIC, never blank. A malformed document, a hash mismatch, or an R2 fault
leaves the previously installed spec tree exactly as it was. A seat keeps
serving the spec it was serving; a bad publish costs the seat its update, never
its correctness. Refusing to install is never the same as installing nothing.

Layers, mirroring :mod:`config_applier`:

* :mod:`spec_applier.applier` — pull → parse → hash-verify → install (root-owned)
  → root-computed manifest, returning a structured :class:`SpecApplyResult`.
  Every side effect is injected so the module is unit-testable with no network
  and no real volume.
* :mod:`spec_applier.__main__` — the poll loop and the boot-time ``--once``
  fetch, wired from env by ``operator/templates/entrypoint.sh``.
"""

from spec_applier.applier import (
    MANIFEST_NAME,
    SCHEMA_VERSION,
    SpecApplyError,
    SpecApplyOutcome,
    SpecApplyResult,
    apply,
    spec_object_key,
)

__all__ = [
    "MANIFEST_NAME",
    "SCHEMA_VERSION",
    "SpecApplyError",
    "SpecApplyOutcome",
    "SpecApplyResult",
    "apply",
    "spec_object_key",
]
