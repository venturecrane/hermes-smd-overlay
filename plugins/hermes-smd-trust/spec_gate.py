"""Authored-spec live gate (ss ADR 0083, ss-console #2084).

Closes the loop the spec loader opens. Delivery puts a pointer to the seat's
authored spec in the model's context; ``shared.spec_status`` records whether the
model followed it; this gate is what makes not-following-it cost something.

Binding — three conditions, ALL required
----------------------------------------
1. The seat's live ``customer.yaml`` authors ``output_classes.<class>.voice_spec:
   expected`` for the class this send belongs to. Absent block, absent class, or
   ``none`` ⇒ the gate does not bind. ``none`` is a legitimate authored choice
   (persona judgment produces the shape), not an unauthored default, and the
   whole reason the declaration exists is to keep it distinguishable from a
   broken sync.
2. The send would actually LEAVE without human review — the effective ceiling is
   ``autonomous``. A draft or confirm send has a person between the model and the
   reader, and that person is a better spec check than this gate.
3. Nothing else. In particular the gate does NOT require the spec to be
   installed: a class that declares ``expected`` whose spec never arrived is a
   BROKEN CONTROL, and refusing is the entire point of the declaration.

Class resolution
----------------
Derived from the resolved recipient class the trust decision already computed,
never from a skill's guess about who will read its output:

    external_send_internal → staff             (persona voice)
    external_send_client   → outbound_client   (firm voice)
    external_send_vendor   → outbound_vendor   (firm voice)
    external_send          → outbound_external (firm voice)

Persona voice — the ``staff`` class — is the one provable today: it needs no
customer corpus, so it exists from day one, and it is also the highest-volume
class by a wide margin. That is why this gate covers ``external_send_internal``,
which the ADR 0028 voice gate deliberately does not: that gate protects the
principal's voice from leaving the firm un-reviewed, while this one enforces that
an authored spec was consulted at all.

Fail behavior
-------------
Downgrade to draft through the same block-directive plumbing the content floor
and the voice gate use, and write a ``SPEC_GATE_TRIGGERED`` audit row. Any
internal error downgrades. The gate never silently passes.
"""

from __future__ import annotations

import logging
from typing import Any

from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.audit_contract import agent_event_params
from shared.audit_status import NoAuditWarner
from shared.customer_config import CustomerConfig
from shared.spec_status import SPEC_STATUS

logger = logging.getLogger(__name__)

#: The one disposition that binds this gate. ``none`` is the other legal value
#: and is a deliberate authored choice, not an absence.
_EXPECTED = "expected"

#: Fail reasons, carried into the audit row and the operator-facing message.
_REASON_SPEC_NOT_READ = "spec_not_read"
_REASON_GATE_ERROR = "gate_error"
#: The output does not have the shape the customer authored. Distinct from
#: `spec_not_read` on purpose: that one means the model never looked, this one
#: means it looked and produced something else, and the fixes differ.
_REASON_FORMAT_VIOLATION = "format_violation"


def resolve_output_class(action_class_value: str) -> str | None:
    """Map an ``ActionClass`` value to its output class, or ``None``.

    Takes the enum's string VALUE rather than the enum, so this module does not
    import ``enforce`` and create a cycle (``enforce`` calls into here).
    """
    return {
        "external_send_internal": "staff",
        "external_send_client": "outbound_client",
        "external_send_vendor": "outbound_vendor",
        "external_send": "outbound_external",
    }.get(action_class_value)


def _declared(output_class: str, prop: str) -> bool:
    """True iff the seat declares ``<prop>_spec: expected`` for this class."""
    try:
        declared = CustomerConfig.from_volume().output_classes.get(output_class)
    except Exception:  # noqa: BLE001 — unconfirmed => not declared => gate silent
        logger.debug(
            "spec gate: output_classes unresolved; treating as not declared (gate silent)",
            exc_info=True,
        )
        return False
    if not isinstance(declared, dict):
        return False
    return str(declared.get(f"{prop}_spec", "")).strip().lower() == _EXPECTED


def _format_violations(output_class: str, body: str) -> list:
    """Every way ``body`` breaks the class's authored shape rules.

    Reads the assertions out of the ROOT-OWNED manifest — the same surface the
    body digest comes from — so the agent cannot widen its own shape rules by
    writing anywhere it can reach.

    A class that declares `format_spec: expected` but whose installed spec
    carries no assertions yields no violations HERE; the missing-spec case is
    the declaration check's job, not this one. Two different failures, two
    different reasons.
    """
    from shared import format_check, spec_manifest

    entries = [e for e in spec_manifest.entries_for_class(output_class) if e.prop == "format"]
    violations: list = []
    for entry in entries:
        violations.extend(format_check.check(body, entry.assertions))
    return violations


def _spec_expected(output_class: str) -> bool:
    """True iff the seat declares ``voice_spec: expected`` for this class.

    Positively-confirm-or-silent, exactly like the voice gate's binding check: a
    missing or unreadable config reads as NOT declared and the gate stays silent
    rather than imposing a downgrade on a seat that authored nothing. That is
    distinct from the gate's INTERNAL posture, which once bound fails toward a
    draft on any error — the same reconciliation the voice gate documents, and
    for the same reason: the ceiling resolver already read this config to get
    here, so a config it read is a config this check can read.
    """
    try:
        declared = CustomerConfig.from_volume().output_classes.get(output_class)
    except Exception:  # noqa: BLE001 — unconfirmed ⇒ not declared ⇒ gate silent
        logger.debug(
            "spec gate: output_classes unresolved; treating as not declared (gate silent)",
            exc_info=True,
        )
        return False
    if not isinstance(declared, dict):
        return False
    return str(declared.get("voice_spec", "")).strip().lower() == _EXPECTED


# ---------------------------------------------------------------------------
# Audit emission — SPEC_GATE_TRIGGERED
#
# Mirrors voice_gate's writer: one audit_log row through the shared audit
# client and the canonical INSERT contract, preserving the trust/audit loose
# coupling. Best-effort RELATIVE TO THE DOWNGRADE — a write failure logs and
# the downgrade still stands.
# ---------------------------------------------------------------------------

_AUDIT_CLIENT: Any = None
_AUDIT_CUSTOMER_SLUG: str | None = None
_AUDIT_WIRED: bool = False

_NO_AUDIT_WARNER = NoAuditWarner()


def _audit_client() -> tuple[Any, str | None]:
    """Lazily resolve ``(client, customer_slug)``; cached across calls."""
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
        logger.debug("spec gate: audit client unconfigured (%s); downgrades won't emit a row", exc)
        _AUDIT_CLIENT = None
        _AUDIT_CUSTOMER_SLUG = None
    return _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG


def _emit_spec_gate_audit(
    *,
    tool_name: str,
    output_class: str,
    reason: str,
    session_id: str,
    tool_call_id: str,
    detail: str = "",
) -> None:
    """Write one ``SPEC_GATE_TRIGGERED`` row. Provenance only, never the body.

    ``detail`` carries RULE NAMES for a format violation — never the offending
    text. The audit row is durable and read by people who were not in the
    session; the fragment that helps the model fix its draft has no business
    persisting there.
    """
    client, slug = _audit_client()
    if client is None or slug is None:
        _NO_AUDIT_WARNER.warn(
            logger,
            f"SPEC_GATE_TRIGGERED on tool={tool_name} (class={output_class} "
            f"reason={reason}) not recorded",
        )
        return
    try:
        metadata: dict = {
            "spec_gate": True,
            "customer": slug,
            "tool": tool_name,
            "output_class": output_class,
            "reason": reason,
        }
        if detail:
            metadata["rules"] = detail
        if session_id:
            metadata["session_id"] = session_id
        if tool_call_id:
            metadata["tool_call_id"] = tool_call_id
        params = agent_event_params(action_type="SPEC_GATE_TRIGGERED", metadata=metadata)
        client.execute(_INSERT_SQL, *params)
    except Exception as exc:  # noqa: BLE001 — best-effort vs the downgrade
        logger.warning(
            "spec gate: SPEC_GATE_TRIGGERED emission failed (tool=%s class=%s err=%s); "
            "the draft downgrade still stands",
            tool_name,
            output_class,
            exc,
        )


def _draft_message(output_class: str, reason: str) -> str:
    if reason == _REASON_SPEC_NOT_READ:
        return (
            f"Refused: this seat declares an authored voice spec for the '{output_class}' "
            "output class, and this turn did not read it. Read the spec named in your "
            "skill's authored-spec pointer, compose against it, then send — or create a "
            "draft for review. (ss ADR 0083)"
        )
    return (
        f"Refused: the authored-spec gate could not certify this '{output_class}' send "
        f"(reason={reason}); routing to draft for human review. Create a draft instead."
    )


def check_spec_gate(
    *,
    tool_name: str,
    action_class_value: str,
    session_id: str = "",
    tool_call_id: str = "",
    body: str = "",
) -> dict | None:
    """Gate an allowed autonomous send on having read its class's authored spec.

    The caller invokes this ONLY for an allowed send whose effective ceiling is
    ``autonomous``. Returns a draft-routing block directive to downgrade, or
    ``None`` to let the send proceed.
    """
    output_class = resolve_output_class(action_class_value)
    if output_class is None:
        return None

    try:
        voice_bound = _spec_expected(output_class)
        format_bound = _declared(output_class, "format")
        if not voice_bound and not format_bound:
            return None  # not bound for this class on this seat

        # FORMAT FIRST, and it is checked against the actual text. Voice asks
        # whether the model consulted its spec; format asks whether the thing it
        # produced has the authored shape. ADR 0083 §3: format is binary where
        # voice is probabilistic, so this is the half that can be decided rather
        # than graded — and a shape honoured only most of the time is worse than
        # one never promised, because the reader stops trusting all of it.
        if format_bound and body:
            violations = _format_violations(output_class, body)
            if violations:
                from shared import format_check

                detail = format_check.describe(violations)
                _emit_spec_gate_audit(
                    tool_name=tool_name,
                    output_class=output_class,
                    reason=_REASON_FORMAT_VIOLATION,
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    detail=format_check.rule_names(violations),
                )
                return {
                    "action": "block",
                    "message": (
                        f"Refused: this output does not have the shape the firm authored for "
                        f"the '{output_class}' class — {detail}. Fix the shape and send again, "
                        "or create a draft for review. (ss ADR 0083 §3)"
                    ),
                }

        if not voice_bound:
            return None
        if SPEC_STATUS.was_read(session_id, output_class, "voice"):
            return None
        reason = _REASON_SPEC_NOT_READ
    except Exception:  # noqa: BLE001 — a bound-seat evaluation fault fails closed
        logger.exception(
            "spec gate: evaluation failed for %s (%s); failing toward draft",
            tool_name,
            output_class,
        )
        reason = _REASON_GATE_ERROR

    _emit_spec_gate_audit(
        tool_name=tool_name,
        output_class=output_class,
        reason=reason,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    return {"action": "block", "message": _draft_message(output_class, reason)}


__all__ = ["check_spec_gate", "resolve_output_class"]
