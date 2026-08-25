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
   BROKEN CONTROL.

   WHAT A BROKEN CONTROL COSTS — amended 2026-08-10 (Captain ruling, ss-console
   #2228/#2234; ADR 0083 amended alongside). Refusing used to be "the entire
   point of the declaration", full stop. It was not: on ``pilot-smokeball`` the
   ``staff`` class declared a voice spec that was never installed, and for six
   days every autonomous internal send refused with a remedy the model could not
   perform — there was no spec to read — while the firm's escalations and
   digests fell into matter memos nobody watches, and nothing alerted. A control
   that can only fail silently and permanently is not a control.

   So the cost is now paid by whoever can afford it, per class:

   * ``staff`` — a person inside the firm is waiting on ops mail. **Proceed** in
     the persona's own authored register (ADR 0083: "authored by the customer or
     fails closed to the persona's own authored judgment"), and alert.
   * ``outbound_client`` / ``outbound_vendor`` / ``outbound_external`` — the
     FIRM's voice to someone outside it. The persona's register is the wrong
     voice there, not a neutral one, so these route to a human, and alert.
   * ``work_product`` / ``record`` — artifacts with nobody blocked on them.
     Refusing costs nothing, so they still refuse.

   Two states are NOT broken controls and still refuse everywhere: a spec whose
   bytes no longer match the root-recorded digest (tamper must not become an
   escape hatch), and a manifest this process cannot read at all (absence of
   evidence is not evidence of absence — see ``shared.spec_manifest``).

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

INTERNAL ARTIFACTS RESOLVE NOWHERE, and that is why ``output_class`` exists
-------------------------------------------------------------------------
The map above covers the four classes whose recipient is in the tool call. The
two INTERNAL-artifact classes — ``work_product`` and ``record`` — have no
recipient, so nothing in a tool call resolves them. Probed 2026-08-01
(ss-console ``vfy_01KYZF6CYFRQ9SJDWQF0FDNX7W``): ``pre_tool_call`` carries only
``tool_name, args, task_id, session_id, tool_call_id``; ``content_ceiling``,
which ss-console's ``output-classes.yaml`` names as ``work_product``'s
``declared_by``, has no runtime counterpart anywhere in this overlay; and the
one skill-name resolver is dead code documented "never an entitlement input".
``mcp_smokeball_create_memo`` carrying a demand letter is indistinguishable here
from the same call carrying a chronology row, and the two classes share every
seam.

So a caller that GENUINELY KNOWS the class passes it. Today that is
``hermes-smd-drafting``'s ``smd_deliver_draft``, a mediated tool whose whole
purpose is to be the drafting lane's declared exit: the class stops being
inferred from a generic write and becomes a property of which tool was called.

The trust boundary does not move. An explicit class only ever selects WHICH
authored declaration is consulted; it cannot manufacture one. A caller naming a
class the seat never declared gets ``None`` from ``_spec_expected`` and the gate
stays silent, exactly as it would for an unbound recipient class — and a caller
naming a class the seat DID declare has asked for a stricter check, not a
weaker one. There is no value of ``output_class`` that turns a refusal into a
pass, which is what makes accepting it from a tool handler safe where accepting
``_skill_name`` from model-composed args would not be.

Fail behavior
-------------
Downgrade to draft through the same block-directive plumbing the content floor
and the voice gate use, and write a ``SPEC_GATE_TRIGGERED`` audit row. Any
internal error downgrades. The gate never silently passes.
"""

from __future__ import annotations

import logging
from typing import Any

from shared import spec_manifest
from shared.audit_contract import CANONICAL_TOOL_CALL_KEY, agent_event_params
from shared.audit_contract import INSERT_SQL as _INSERT_SQL
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
#: Declared, and affirmatively never installed — a BROKEN CONTROL. Named apart
#: from `spec_not_read` because the remedy differs absolutely: not-read is the
#: model's to fix by reading, and no_spec cannot be fixed by the model at all.
#: Telling an agent to "read the spec named in your pointer" when no such spec
#: exists is an instruction that cannot be followed, which is what six days of
#: refusals looked like from inside the seat (ss-console #2228).
_REASON_NO_SPEC = "no_spec"
#: This process cannot see the spec tree, so it can prove nothing. NEVER folded
#: into `no_spec`: absence of evidence would otherwise unlock a send.
_REASON_SPEC_UNPROVABLE = "spec_unprovable"
#: Entries exist and none matches the digest root recorded. Reads as tamper and
#: must never take the broken-control path — that would make deleting bytes a
#: way to escape the gate.
_REASON_SPEC_HASH_MISMATCH = "spec_hash_mismatch"
#: A format-bound send whose body this gate cannot inspect. `_extract_send_body`
#: returns None to mean INDETERMINATE and `_apply_content_floor` fails toward
#: draft on it; this gate used to coerce that None to "" and skip the format
#: check entirely — the same value, two adjacent call sites, opposite
#: dispositions (ss-console #2234).
_REASON_BODY_INDETERMINATE = "body_indeterminate"

#: The repo-authored structure floor broke (ss#2090 refiled, the 2026-08-25
#: digest). Deliberately NOT ``_REASON_FORMAT_VIOLATION``: that reason means the
#: CUSTOMER's authored shape rules broke, read out of the root-owned manifest.
#: Reusing it would tell an operator the firm's format rule failed on a seat
#: where the firm authored nothing — the same misattribution class as the
#: 2026-07-31 "trust-ceiling evaluation failed" wording.
_REASON_STRUCTURE_FLOOR = "structure_floor"

#: Per-property control states. The question is not "what is installed" but
#: "what can this process PROVE about what is installed" — see
#: ``shared.spec_manifest.manifest_state``.
_STATE_PRESENT = "present"
_STATE_MISSING = "missing"
_STATE_TAMPERED = "tampered"
_STATE_UNPROVABLE = "unprovable"

#: The one output class that PROCEEDS when its control is broken (Captain,
#: 2026-08-10; ADR 0083 amended).
#:
#: The distinction is who is waiting. `staff` is ops mail to a person inside the
#: firm — a digest, an escalation, a deadline they asked to be told about — and
#: a refusal there costs them the message itself, silently. The three outbound
#: classes carry the FIRM's voice to someone outside it, where the persona's own
#: register is the wrong voice rather than a neutral one, so they route to a
#: human instead. `work_product` and `record` are artifacts with no reader
#: blocked on them; refusing costs nothing and `draft_delivery_gate` in
#: ss-console's runtime-controls registry is `enforced` on exactly that
#: behaviour (observed live, vfy_01KYZNTJAEST5HEVJATYFY9ED3).
#:
#: Membership is by explicit class name, never by "internal" — that word already
#: means work_product/record here (`_INTERNAL_ARTIFACT_CLASSES` below), and
#: conflating the two senses would invert a certified control.
_PROCEED_ON_BROKEN_CONTROL = frozenset({"staff"})


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
            metadata[CANONICAL_TOOL_CALL_KEY] = tool_call_id
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


# Classes with no recipient. Their refusal cannot say "create a draft for review"
# — the artifact already IS a draft, and offering that as the remedy would send
# the model looking for an escape hatch instead of reading the spec.
_INTERNAL_ARTIFACT_CLASSES = frozenset({"work_product", "record"})

#: Payload key marking a body as a FIXED TEMPLATE this repo authored, not prose
#: a model composed (ss-console#2546). Set by the out-of-turn dispatcher in
#: ``plugins/hermes-smd-trust`` and read here through ``check_spec_gate``'s
#: ``templated`` argument. It is a per-payload key rather than a module flag
#: deliberately: a sweeper thread and a live turn can both be in flight, and a
#: flag would let one turn's posture leak into another's.
TEMPLATED_BODY_ARG = "_smd_templated_body"


def _refuse(
    *,
    tool_name: str,
    output_class: str,
    reason: str,
    session_id: str,
    tool_call_id: str,
) -> dict:
    """Audit the refusal and return its block directive. Never silently passes."""
    _emit_spec_gate_audit(
        tool_name=tool_name,
        output_class=output_class,
        reason=reason,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    return {"action": "block", "message": _draft_message(output_class, reason)}


def _control_state(output_class: str, prop: str) -> str:
    """What can be PROVEN about this (class, property)'s installed spec.

    ``manifest_state`` is consulted first and its ``unreadable`` verdict wins
    outright: an empty entry list means "nothing installed" ONLY when the
    manifest was actually readable. Reading emptiness as absence without that
    check is how a lost ``SMD_SPEC_DIR`` would come to look like an authored
    choice.
    """
    if spec_manifest.manifest_state() == spec_manifest.STATE_UNREADABLE:
        return _STATE_UNPROVABLE
    entries = [e for e in spec_manifest.entries_for_class(output_class) if e.prop == prop]
    if not entries:
        return _STATE_MISSING
    if not any(spec_manifest.verify(e) for e in entries):
        return _STATE_TAMPERED
    return _STATE_PRESENT


def _broken_control_message(output_class: str, props: list[str]) -> str:
    """What a refused output is told when its control was never installed.

    Names the fault and who can fix it. It deliberately does NOT tell the model
    to read the spec: there is nothing to read, and an unfollowable instruction
    is what turned this failure into six days of silence.

    The remedy splits the same way ``_draft_message`` splits it: an internal
    artifact already IS a draft, so offering "create a draft" there would send
    the model hunting for an escape hatch instead of stopping.
    """
    named = " and ".join(sorted(props))
    remedy = (
        "Nothing is delivered; this needs a person, not a retry."
        if output_class in _INTERNAL_ARTIFACT_CLASSES
        else "Create a draft for human review instead."
    )
    return (
        f"Refused: this seat declares an authored {named} spec for the "
        f"'{output_class}' output class and no such spec is installed — a broken "
        f"control, not something you can fix by reading. {remedy} The fault has "
        "been reported to the firm's operators; do not route around it. "
        "(ss ADR 0083)"
    )


def _draft_message(output_class: str, reason: str) -> str:
    internal = output_class in _INTERNAL_ARTIFACT_CLASSES
    if reason == _REASON_SPEC_NOT_READ:
        remedy = (
            "Read the spec named in your skill's authored-spec pointer, compose against "
            "it, and deliver again."
            if internal
            else "Read the spec named in your skill's authored-spec pointer, compose "
            "against it, then send — or create a draft for review."
        )
        return (
            f"Refused: this seat declares an authored voice spec for the '{output_class}' "
            f"output class, and this turn did not read it. {remedy} (ss ADR 0083)"
        )
    if internal:
        return (
            f"Refused: the authored-spec gate could not certify this '{output_class}' "
            f"artifact (reason={reason}). Nothing is delivered. Resolve the fault and "
            "deliver again rather than routing around this."
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
    body: str | None = "",
    output_class: str | None = None,
    templated: bool = False,
) -> dict | None:
    """Gate an output on having read its class's authored spec.

    ``body`` is the composed text, or ``None`` meaning INDETERMINATE — the same
    contract ``enforce._extract_send_body`` states and the content floor honours.
    Do not coerce it: a format-bound send whose body cannot be inspected refuses.

    For a SEND, the caller invokes this only for an allowed send whose effective
    ceiling is ``autonomous``, and leaves ``output_class`` unset so the class
    derives from the resolved recipient.

    For an INTERNAL ARTIFACT there is no recipient to derive from, so a caller
    that knows the class states it. See the module docstring for why that is
    safe: an explicit class selects which authored declaration is consulted and
    can only ever make the check stricter.

    ``templated`` says the body is a fixed template from this repo rather than
    prose a model composed (ss-console#2546). It skips EXACTLY ONE branch: the
    voice branch's "was the spec read this session" test, which asks whether the
    model consulted the firm's voice before writing, a question with no meaning
    about bytes the model did not write, and one that would otherwise refuse
    every deterministic notification on a seat that declares a voice spec.
    Nothing else is skipped: a tampered or unprovable spec still refuses, a
    missing one is still a broken control, and every FORMAT assertion still runs
    against the template's actual text. It also grants nothing to the session:
    a model-composed send later in the same turn, with no spec read, is refused
    exactly as it was before.

    Returns a block directive to refuse, or ``None`` to let the output proceed.
    """
    output_class = output_class or resolve_output_class(action_class_value)
    if output_class is None:
        return None

    try:
        voice_bound = _spec_expected(output_class)
        format_bound = _declared(output_class, "format")
        if not voice_bound and not format_bound:
            return None  # not bound for this class on this seat

        # A control that IS installed binds exactly as it always did. Only a
        # control that was never installed takes the broken-control path at the
        # end, and a missing voice spec never excuses a real format violation.
        broken: list[str] = []

        # FORMAT FIRST, and it is checked against the actual text. Voice asks
        # whether the model consulted its spec; format asks whether the thing it
        # produced has the authored shape. ADR 0083 §3: format is binary where
        # voice is probabilistic, so this is the half that can be decided rather
        # than graded — and a shape honoured only most of the time is worse than
        # one never promised, because the reader stops trusting all of it.
        if format_bound:
            format_state = _control_state(output_class, "format")
            if format_state in (_STATE_TAMPERED, _STATE_UNPROVABLE):
                return _refuse(
                    tool_name=tool_name,
                    output_class=output_class,
                    reason=(
                        _REASON_SPEC_HASH_MISMATCH
                        if format_state == _STATE_TAMPERED
                        else _REASON_SPEC_UNPROVABLE
                    ),
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                )
            if format_state == _STATE_MISSING:
                broken.append("format")
            elif body is None:
                # INDETERMINATE, and it fails the way the content floor fails on
                # this exact value: an autonomous send whose body this gate
                # cannot read must not be certified as carrying the authored
                # shape. Coercing it to "" (as this gate used to) silently
                # skipped the check for every tool whose arg shape is
                # unrecognised.
                return _refuse(
                    tool_name=tool_name,
                    output_class=output_class,
                    reason=_REASON_BODY_INDETERMINATE,
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                )
            else:
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
                            f"Refused: this output does not have the shape the firm authored "
                            f"for the '{output_class}' class — {detail}. Fix the shape and send "
                            "again, or create a draft for review. (ss ADR 0083 §3)"
                        ),
                    }

        if voice_bound:
            voice_state = _control_state(output_class, "voice")
            if voice_state in (_STATE_TAMPERED, _STATE_UNPROVABLE):
                return _refuse(
                    tool_name=tool_name,
                    output_class=output_class,
                    reason=(
                        _REASON_SPEC_HASH_MISMATCH
                        if voice_state == _STATE_TAMPERED
                        else _REASON_SPEC_UNPROVABLE
                    ),
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                )
            if voice_state == _STATE_MISSING:
                broken.append("voice")
            elif templated:
                # ss-console#2546. The one branch a fixed template is exempt
                # from, and only this one. The spec IS installed and IS
                # hash-verified (both checked above); what cannot be asked is
                # whether the model read it, because the model did not write
                # this body. Marking the spec read instead would have been the
                # tempting fix and is the one this deliberately avoids: it would
                # leave the session holding a read it never performed, and the
                # next model-composed send would sail through the gate.
                pass
            elif not SPEC_STATUS.was_read(session_id, output_class, "voice"):
                return _refuse(
                    tool_name=tool_name,
                    output_class=output_class,
                    reason=_REASON_SPEC_NOT_READ,
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                )

        if not broken:
            return None

        # BROKEN CONTROL — declared, and affirmatively never installed. Recorded
        # either way; the send's fate depends on who is waiting. The audit row is
        # the seat-local record; the alert that reaches a PERSON is
        # `shared.spec_control_check` on the heartbeat, which does not depend on
        # anyone happening to send.
        _emit_spec_gate_audit(
            tool_name=tool_name,
            output_class=output_class,
            reason=_REASON_NO_SPEC,
            session_id=session_id,
            tool_call_id=tool_call_id,
            detail=sorted(broken),
        )
        if output_class in _PROCEED_ON_BROKEN_CONTROL:
            # Proceed in the persona's own authored register — ADR 0083's own
            # decision sentence: each property "is authored by the customer or
            # fails closed to the persona's own authored judgment". Nothing here
            # PRODUCES that register; `bootstrap/translate.py` renders the
            # authored `personas[].tone` into SOUL on every turn. This only
            # declines to refuse on account of a control nobody installed.
            return None
        return {"action": "block", "message": _broken_control_message(output_class, broken)}
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


__all__ = ["TEMPLATED_BODY_ARG", "check_spec_gate", "resolve_output_class"]


def check_structure_floor(
    *,
    tool_name: str,
    action_class_value: str,
    session_id: str,
    tool_call_id: str = "",
    body: str | None,
    allowed: bool,
) -> dict | None:
    """The repo-authored structure floor (ss#2090 refiled; the 2026-08-25 digest).

    Distinct from :func:`check_spec_gate` on both axes that matter. That gate
    asks whether the CUSTOMER's authored spec was consulted, and binds only where
    the seat declares one. This asks whether the message arrived as a document at
    all, and binds wherever the routine is one we map — including the seats that
    have authored nothing, which is most of them and all of them on day one.

    WHAT IT COSTS, and why that is not the same answer as everywhere else
    --------------------------------------------------------------------
    ``staff`` PROCEEDS. Not as a concession, but as the same ruling
    :data:`_PROCEED_ON_BROKEN_CONTROL` already encodes for the broken-control
    case, for the same reason in the Captain's own words: a refusal there costs
    them the message itself, silently. A person is waiting on ops mail. An ugly
    digest is a bad morning; a missing one is the six days of 2026-08-04..09,
    and the escalator was refused five times in a row on 2026-08-19 when a gate
    blocked a cron turn that then simply recomposed the same body. Proceeding
    also means this check cannot open a retry loop, because it never blocks the
    class that runs on a schedule.

    The outbound classes route to a human, because a shapeless message carrying
    the FIRM's name to someone outside it is worth the delay.

    Either way the row is written. For ``staff`` the row IS the disposition: the
    2026-08-25 audit row read ``rules=["voice"]``, and the structural failure it
    was sitting on had no field at all.

    Returns a draft-routing block directive, or ``None`` to let the send proceed.
    """
    from shared import message_structure  # local import (enforce.py idiom)

    try:
        output_class = resolve_output_class(action_class_value)
        if output_class is None:
            return None

        family = message_structure.family_for_skill(_routine_skill(session_id))
        if family is None:
            return None

        violations = message_structure.check(body, family)
        if not violations:
            return None

        _emit_spec_gate_audit(
            tool_name=tool_name,
            output_class=output_class,
            reason=_REASON_STRUCTURE_FLOOR,
            session_id=session_id,
            tool_call_id=tool_call_id,
            detail=message_structure.rule_names(violations),
        )

        if output_class in _PROCEED_ON_BROKEN_CONTROL:
            return None
        if not allowed:
            # Already routed to a person; nothing left for this gate to add.
            return None
        return {
            "action": "block",
            "message": (
                f"Refused: this {output_class} message does not carry the "
                f"structure its reader needs: {message_structure.describe(violations)}. "
                "Re-compose it with headings and list items per this skill's "
                "references/output-format.md. Create a draft for human review "
                "instead. (ss#2090)"
            ),
        }
    except Exception:  # noqa: BLE001 — a structure check must never break a send
        logger.exception(
            "spec gate: structure floor failed for %s; proceeding (the floor is a "
            "legibility check, and failing it closed would cost the message)",
            tool_name,
        )
        return None


def _routine_skill(session_id: str) -> str | None:
    """The skill of the cron routine this session belongs to, or ``None``.

    Read from ``cron_attribution`` rather than the pre-run handoff, which cannot
    serve: ``take_handoff`` renames the file to ``<skill>.consumed.json`` BEFORE
    returning, so by send time it is gone; it returns ``{dates, records}`` and
    nothing else by design; and ``daily-needs-you-digest`` never writes one at
    all. A channel that cannot be read at the moment it is needed is worth
    nothing, however unforgeable.

    The trade, stated rather than hidden: ``cron/jobs.json`` is not inside the
    ``.smd`` fence, so this is the weaker channel against a model that wanted to
    evade the check. The threat model here is a model that FORGOT its format,
    not one rewriting the cron store to publish an ugly digest.
    """
    from shared import cron_attribution  # local import (enforce.py idiom)

    try:
        routine = cron_attribution.resolve_routine(session_id)
    except Exception:  # noqa: BLE001 — attribution is best-effort
        return None
    return getattr(routine, "skill", None) if routine is not None else None
