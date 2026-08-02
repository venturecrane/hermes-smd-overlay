"""Voice live-gate — ADR 0028 §2 (#855), repointed per-class per ss#2086 step 1.

Closes the "primed, not gated" gap. An autonomous OUTSIDE send could ship in
generic model prose, not the firm's authored voice, with no human to catch it.
This gate is that enforcement.

Binding — TWO regimes, resolved per (seat × output class), ADDITIVELY
---------------------------------------------------------------------
The output class for a send derives from the recipient class the trust decision
already resolved (``shared.spec_gate.resolve_output_class``). For that ONE
class:

* **SPEC regime** — the seat's live ``customer.yaml`` declares
  ``output_classes.<class>.voice_spec: expected``. The authored-spec binding
  governs (ss ADR 0083 / ADR 0085): pass = the class's voice spec is installed,
  its bytes hash to what the ROOT-OWNED manifest recorded, and the agent read it
  THIS turn (``shared.spec_status`` — a mark is only ever set after
  ``shared.spec_manifest`` verified the file, so the mark IS all three
  conditions). Reasons: ``no_spec`` | ``spec_hash_mismatch`` | ``spec_not_read``.

* **MECHANISM-B fallback** — the class is NOT declared ``expected`` (undeclared
  class, ``output_classes`` absent entirely, or ``voice_spec: none``). The
  ORIGINAL binding applies EXACTLY as before this repoint: the gate binds iff
  the config authors a non-empty ``voice_library`` block, and passes iff the
  sample-driven voice transform demonstrably ran on this turn
  (``shared.voice_status``). Reasons: ``no_samples`` | ``transform_not_applied``.

The resolution is ADDITIVE, never substitutive: a seat that declares specs for
SOME classes keeps the Mechanism-B fallback on every class it did not declare.
A seat declaring only ``work_product`` still has its autonomous
``outbound_client`` sends governed by the B fallback — no (seat × class) loses
its downgrade relative to the pre-repoint gate. ``resolve_binding_regime`` is
the single pure function that decides this, and
``tests/test_voice_gate_binding_coverage.py`` asserts the never-weaker property
over a checked-in snapshot of every real seat's config.

Failure to read the config keeps the ORIGINAL posture: an unresolvable config
reads as "not authored / not declared" and the gate stays silent (ADR 0035 — an
unauthored capability is never imposed; the ceiling resolver already fail-closes
an unresolvable config upstream). A resolvable config whose ``output_classes``
is malformed falls back to the B regime — never silently unbound.

Firing site (``enforce.evaluate_tool_call``)
--------------------------------------------
Fires on the SAME path as the content floor: only when the resolved decision is
an ALLOWED **outside** ``EXTERNAL_SEND`` (outside / client / vendor classes)
whose effective ceiling is ``autonomous``. Draft-for-review / confirm paths have
a human in the loop; ``external_send_internal`` is ops traffic to rostered
staff, not client-voice impersonation. Those are deliberately out of scope (the
authored-spec gate covers ``staff`` separately — ``shared.spec_gate``).

Fail behavior (fail-closed, ADR 0028 §4) — BOTH regimes
-------------------------------------------------------
Downgrade the send to draft-for-review — using the EXACT same block-directive
plumbing the content floor uses (a ``{"action": "block", ...}`` return that the
agent turns into a draft) — and write a ``VOICE_GATE_TRIGGERED`` audit row. The
action_type and row shape are UNCHANGED by the repoint; the new reason strings
ride the existing ``reason`` field (``no_spec`` | ``spec_not_read`` |
``spec_hash_mismatch`` alongside the fallback's ``no_samples`` |
``transform_not_applied`` | ``gate_error``). ANY internal error in a bound gate
downgrades. The gate never silently passes.

Overlay-only (like the content floor, ADR 0031 §5). NOT mirrored into
ss-console's ``trust_ceiling.py`` adapter.
"""

import logging
from typing import Any

from shared import spec_manifest
from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.audit_contract import agent_event_params
from shared.audit_status import NoAuditWarner
from shared.customer_config import CustomerConfig
from shared.spec_gate import resolve_output_class
from shared.spec_status import SPEC_STATUS
from shared.voice_status import VOICE_STATUS

logger = logging.getLogger(__name__)


# Fail reasons (audit + the operator-facing draft message).
# Mechanism-B fallback regime:
_REASON_NO_SAMPLES = "no_samples"
_REASON_TRANSFORM_NOT_APPLIED = "transform_not_applied"
# Spec regime (ss#2086 step 1):
_REASON_NO_SPEC = "no_spec"
_REASON_SPEC_NOT_READ = "spec_not_read"
_REASON_SPEC_HASH_MISMATCH = "spec_hash_mismatch"
# Either regime:
_REASON_GATE_ERROR = "gate_error"


# ---------------------------------------------------------------------------
# Binding-regime resolution — the additive per-class decision
# ---------------------------------------------------------------------------

#: The class's declared spec governs (ss ADR 0083 binding).
REGIME_SPEC = "spec"
#: The original voice_library / transform-ran binding governs, exactly as
#: before the ss#2086 repoint.
REGIME_MECHANISM_B = "mechanism_b"
#: The gate does not fire for this (seat × class).
REGIME_UNBOUND = "unbound"

#: The one declaration value that selects the spec regime. ``none`` is the
#: other legal authored value and deliberately falls through to the B fallback.
_EXPECTED = "expected"


def resolve_binding_regime(*, voice_library_authored: bool, class_declaration: Any) -> str:
    """Which regime binds this (seat × resolved-output-class) send. PURE.

    The additive rule (ss#2086 step 1, ss plan Decision 4), in full:

    * declaration is a mapping with ``voice_spec: expected`` → ``REGIME_SPEC``;
    * ANY other declaration state (absent class, absent/malformed
      ``output_classes``, ``voice_spec: none``) → the ORIGINAL predicate,
      verbatim: ``voice_library`` authored ⇒ ``REGIME_MECHANISM_B``, else
      ``REGIME_UNBOUND``.

    Additivity, stated as the invariant the coverage test enforces: for every
    input where the OLD predicate bound the gate (``voice_library_authored``),
    this function returns a bound regime — never ``REGIME_UNBOUND``. A
    substitutive variant ("seat declares specs somewhere ⇒ spec logic only")
    cannot be expressed through these inputs without violating that invariant,
    which is what makes the extraction of this function the enforcement seam.
    """
    if isinstance(class_declaration, dict):
        if str(class_declaration.get("voice_spec", "")).strip().lower() == _EXPECTED:
            return REGIME_SPEC
    return REGIME_MECHANISM_B if voice_library_authored else REGIME_UNBOUND


# ---------------------------------------------------------------------------
# Binding condition — voice authored for this seat?
# ---------------------------------------------------------------------------


def _voice_authored() -> bool:
    """True iff the seat's live config authors a non-empty ``voice_library`` block.

    The gate BINDS only when voice is positively confirmed part of the
    engagement (ADR 0035 — an unauthored capability is never imposed). Any
    failure to confirm (missing / unreadable config) reads as NOT authored, so
    the gate stays silent rather than imposing a voice downgrade on a seat that
    may not use voice at all.

    This "positively-confirm-or-silent" posture governs BINDING; it is distinct
    from the gate's INTERNAL fail-closed posture (ADR 0028 §4) which, once bound,
    downgrades to draft on any evaluation error. The two are reconcilable because
    the gate only runs after the ceiling resolver ALREADY read the same config
    to resolve an autonomous exposure — a config the resolver read is a config
    this check can read, so the silent path here is not a fail-open hole."""
    try:
        return bool(CustomerConfig.from_volume().voice_library)
    except Exception:  # noqa: BLE001 — unconfirmed ⇒ not authored ⇒ gate silent
        logger.debug(
            "voice gate: voice_library unresolved; treating as not authored (gate silent)",
            exc_info=True,
        )
        return False


def _class_declaration(output_class: str | None) -> Any:
    """The seat's ``output_classes.<class>`` declaration, or ``None``.

    ``None`` means "no declaration I can positively confirm" — unresolved class,
    absent block, unreadable config — and ``resolve_binding_regime`` reads it as
    "the B fallback applies". A read failure here therefore lands on the OLD
    regime's exact behavior, never on a silently unbound gate and never on the
    spec regime: fail-closed toward what shipped before the repoint.
    """
    if not output_class:
        return None
    try:
        return CustomerConfig.from_volume().output_classes.get(output_class)
    except Exception:  # noqa: BLE001 — unconfirmed ⇒ undeclared ⇒ B fallback binds
        logger.debug(
            "voice gate: output_classes unresolved; falling back to the voice_library binding",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Audit emission — VOICE_GATE_TRIGGERED
#
# Mirrors the FABRICATION_FILTER_TRIGGERED writer in ``outbound.py``: one
# audit_log row via the shared D1Client + the canonical audit_log INSERT
# contract, preserving the trust/audit loose coupling (no cross-import of the
# audit plugin's hook surface). Best-effort RELATIVE TO THE DOWNGRADE — a write
# failure logs a warning but the draft downgrade still stands.
# ---------------------------------------------------------------------------


_AUDIT_CLIENT: Any = None
_AUDIT_CUSTOMER_SLUG: str | None = None
_AUDIT_WIRED: bool = False

_NO_AUDIT_WARNER = NoAuditWarner()


def _audit_client() -> tuple[Any, str | None]:
    """Lazily resolve ``(D1Client, customer_slug)``. Cached across calls.

    Returns ``(None, None)`` when the audit env is not configured — the gate
    still downgrades; the row is skipped (rate-limited WARNING per skip, #64).
    Tests reset the cache by restoring the module globals."""
    global _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG, _AUDIT_WIRED
    if _AUDIT_WIRED:
        return _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG
    _AUDIT_WIRED = True
    try:
        from shared.audit_client import audit_client_from_env
        from shared.secrets import require

        secrets_map = require("SMD_CUSTOMER_SLUG", "SMD_D1_AUDIT_BINDING")
        slug = secrets_map["SMD_CUSTOMER_SLUG"]
        _AUDIT_CLIENT = audit_client_from_env(customer_slug=slug)
        _AUDIT_CUSTOMER_SLUG = slug
    except Exception as exc:  # noqa: BLE001 — audit is best-effort vs the downgrade
        logger.debug("voice gate: audit client unconfigured (%s); downgrades won't emit a row", exc)
        _AUDIT_CLIENT = None
        _AUDIT_CUSTOMER_SLUG = None
    return _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG


def _emit_voice_gate_audit(
    *,
    tool_name: str,
    reason: str,
    session_id: str,
    tool_call_id: str,
) -> None:
    """Write one ``VOICE_GATE_TRIGGERED`` row. Best-effort, never raises.

    Provenance only — never the draft body. Carries the fail ``reason`` so the
    audit review can tell a no-samples seat from a transform-skipped turn."""
    client, slug = _audit_client()
    if client is None or slug is None:
        _NO_AUDIT_WARNER.warn(
            logger, f"VOICE_GATE_TRIGGERED on tool={tool_name} (reason={reason}) not recorded"
        )
        return
    try:
        metadata: dict = {
            "voice_gate": True,
            "customer": slug,
            "tool": tool_name,
            "reason": reason,
        }
        if session_id:
            metadata["session_id"] = session_id
        if tool_call_id:
            metadata["tool_call_id"] = tool_call_id
        params = agent_event_params(action_type="VOICE_GATE_TRIGGERED", metadata=metadata)
        client.execute(_INSERT_SQL, *params)
    except Exception as exc:  # noqa: BLE001 — audit row is best-effort vs downgrade
        logger.warning(
            "voice gate: VOICE_GATE_TRIGGERED emission failed (tool=%s reason=%s err=%s); "
            "the draft downgrade still stands",
            tool_name,
            reason,
            exc,
        )


# ---------------------------------------------------------------------------
# The gate entry point — called from enforce.evaluate_tool_call
# ---------------------------------------------------------------------------


def _draft_message(reason: str) -> str:
    return (
        "Refused: this autonomous send is not certified to be in the principal's "
        f"authored voice (voice live-gate reason={reason}); routing to draft for "
        "human review instead of autonomous send (ADR 0028 §2). Create a draft instead."
    )


def _spec_draft_message(output_class: str, reason: str) -> str:
    if reason == _REASON_SPEC_NOT_READ:
        return (
            f"Refused: this seat declares an authored voice spec for the '{output_class}' "
            "output class, and this turn did not read it. Read the spec named in your "
            "skill's authored-spec pointer, compose against it, then send — or create a "
            "draft for review. (voice live-gate, ss ADR 0083)"
        )
    return (
        f"Refused: the voice live-gate could not certify this '{output_class}' send "
        f"against its authored voice spec (reason={reason}); routing to draft for human "
        "review instead of autonomous send. Create a draft instead."
    )


def _spec_fail_reason(output_class: str) -> str:
    """Distinguish WHY the spec pass condition failed. Off the happy path only.

    ``was_read`` being False is the refusal; this probe only names it — the same
    shape as the fallback regime consulting the samples probe only to pick
    between ``no_samples`` and ``transform_not_applied``. No manifest entry for
    (class, voice) ⇒ ``no_spec`` (declared-but-never-installed is a broken
    control, and refusing is the point of the declaration — ``shared.spec_gate``
    binding condition 3). Entries exist but none verifies ⇒
    ``spec_hash_mismatch`` (disk no longer matches what root recorded — reads as
    tamper). A verified spec existed and simply was not read ⇒ ``spec_not_read``.
    """
    entries = [e for e in spec_manifest.entries_for_class(output_class) if e.prop == "voice"]
    if not entries:
        return _REASON_NO_SPEC
    if not any(spec_manifest.verify(e) for e in entries):
        return _REASON_SPEC_HASH_MISMATCH
    return _REASON_SPEC_NOT_READ


def _check_spec_regime(
    *,
    output_class: str,
    tool_name: str,
    session_id: str,
    tool_call_id: str,
) -> dict | None:
    """The spec regime's pass/fail evaluation. The seat is BOUND on entry.

    Pass = the class's voice spec is installed, hash-matches the root-owned
    manifest, and was read this turn. ``SPEC_STATUS.was_read`` is all three at
    once: ``spec_read.observe_read`` only ever marks after
    ``shared.spec_manifest`` resolved the path to a manifest entry AND verified
    the bytes against the recorded digest — reusing that machinery, not
    duplicating it. Fail-closed (ADR 0028 §4): any evaluation error downgrades.
    """
    try:
        if SPEC_STATUS.was_read(session_id, output_class, "voice"):
            return None
        reason = _spec_fail_reason(output_class)
    except Exception:  # noqa: BLE001 — a bound-seat evaluation fault fails closed
        logger.exception(
            "voice gate: spec-regime evaluation failed for %s (%s); failing toward draft "
            "(ADR 0028 §4)",
            tool_name,
            output_class,
        )
        reason = _REASON_GATE_ERROR

    _emit_voice_gate_audit(
        tool_name=tool_name,
        reason=reason,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    return {"action": "block", "message": _spec_draft_message(output_class, reason)}


def check_voice_gate(
    *,
    tool_name: str,
    action_class_value: str = "",
    session_id: str = "",
    tool_call_id: str = "",
) -> dict | None:
    """Voice live-gate on an allowed autonomous OUTSIDE send.

    The caller (``enforce.evaluate_tool_call``) invokes this ONLY when the
    decision is an allowed ``EXTERNAL_SEND`` whose effective ceiling is
    ``autonomous`` — this function resolves WHICH binding regime governs the
    send's output class (see ``resolve_binding_regime``) and runs that regime's
    pass/fail evaluation. Returns a draft-routing block directive
    ``{"action": "block", "message": ...}`` to downgrade, or ``None`` to let the
    autonomous send proceed.

    ``action_class_value`` is the resolved ``ActionClass`` value (post recipient
    reclassification); its output class comes from the same map the spec gate
    uses. An empty / unresolvable value yields no output class, which resolves
    to the ORIGINAL voice_library binding — the pre-repoint behavior, exactly.

    Fail-closed (ADR 0028 §4): a bound seat that cannot certify its regime's
    pass condition this turn downgrades; any internal error downgrades."""
    output_class = resolve_output_class(action_class_value) if action_class_value else None
    regime = resolve_binding_regime(
        voice_library_authored=_voice_authored(),
        class_declaration=_class_declaration(output_class),
    )
    if regime == REGIME_UNBOUND:
        # Not bound for this (seat × class) — voice is not part of the engagement.
        return None

    if regime == REGIME_SPEC and output_class is not None:
        return _check_spec_regime(
            output_class=output_class,
            tool_name=tool_name,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )

    # Mechanism-B fallback — the ORIGINAL evaluation, verbatim (ss#2086 keeps
    # this until Mechanism B's removal PR, which lands only after the repointed
    # gate is observed passing live sends).
    try:
        if VOICE_STATUS.was_applied(session_id):
            # The transform demonstrably reshaped this turn's output. A successful
            # transform implies samples were retrieved, so both pass conditions hold.
            return None
        # The transform did NOT apply this turn. Distinguish the reason so the
        # audit review separates a no-samples seat from a transform-skipped turn.
        reason = (
            _REASON_TRANSFORM_NOT_APPLIED
            if VOICE_STATUS.samples_available()
            else _REASON_NO_SAMPLES
        )
    except Exception:  # noqa: BLE001 — a bound-seat evaluation fault fails closed
        logger.exception(
            "voice gate: bound-seat evaluation failed for %s; failing toward draft (ADR 0028 §4)",
            tool_name,
        )
        reason = _REASON_GATE_ERROR

    _emit_voice_gate_audit(
        tool_name=tool_name,
        reason=reason,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    return {"action": "block", "message": _draft_message(reason)}


__all__ = [
    "REGIME_MECHANISM_B",
    "REGIME_SPEC",
    "REGIME_UNBOUND",
    "check_voice_gate",
    "resolve_binding_regime",
]
