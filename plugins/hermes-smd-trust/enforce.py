"""Trust enforcement — the safety floor under every tool call (ADR 0056).

The per-tool classification vocabulary (``ActionClass``, ``BANNED_TOOLS``,
``TOOL_ACTION_CLASS_MAP``, ``BannedToolError``, ``ToolClassification``,
``classify_tool``) lives in ``shared.action_classes`` so the audit and trust
plugins share one source of truth. This module imports those names and
re-exports them via ``__all__`` so downstream trust consumers keep working.

Entitlement model (ADR 0056)
----------------------------

Autonomy is authored as **persona-level exposure**: a sparse per-action-class
map (``personas[].entitlements.exposure``) of ceiling values. There is no skill
trust_ceiling scalar and no per-skill / scope / mailbox ``action_ceilings`` —
those are retired with no shim. The ceiling for one action class is resolved as:

  base = exposure[action] if authored else REFUSED        (ADR 0056 fail-closed)
  effective = most_restrictive(base, vertical_floor[action])

``read`` is never authored — enforcement always allows reads. Every other
(non-read) class with no authored exposure is REFUSED. A vertical-pack floor can
only narrow, never raise (ADR 0022 / ADR 0025). The current-turn approval floor
for COMMITMENT and DESTRUCTIVE is a hard runtime floor on top of exposure.

Trusted source
--------------

Exposure is read from the **trusted** ``customer.yaml`` via
``shared.customer_config`` — the keystone seam relocates that file to a
root-owned path (``SMD_CUSTOMER_YAML_PATH``) read-only to the hermes uid, so the
agent cannot rewrite its own exposure. The decision NEVER trusts tool args for
an entitlement: there is no ``_skill_trust_ceiling`` / ``_action_ceilings``
arg handling. The active persona is resolved from the runtime profile env
(``HERMES_ACTIVE_PROFILE`` / ``SMD_ACTIVE_PERSONA``), the same channel the audit
plugin uses, and its exposure is read from the trusted file.

Decision audit
--------------

Every decision is logged with the action class, the authored ceiling, the
vertical floor, the effective ceiling, the decision, and the reason — the trust
trail the audit review reads — and the same six fields are handed to
``shared.trust_decision`` so the audit row this call produces carries them too
(#2122). The log is for a human reading the seat; the register is for the
ledger, which is where a compliance review actually looks. The plugin's
``pre_tool_call`` hook expects either ``None`` (allow) or
``{"action": "block", "message": "Refused: <reason>"}``; ``evaluate_tool_call``
returns exactly that shape.
"""

import copy
import enum
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from shared import content_floor, matter_gate, spec_gate
from shared.action_classes import (
    BANNED_TOOLS,
    TOOL_ACTION_CLASS_MAP,
    ActionClass,
    BannedToolError,
    ToolClassification,
    classify_tool,
)
from shared.action_classes import (
    VERTICAL_FLOORS as _SHARED_VERTICAL_FLOORS,
)
from shared.customer_config import CustomerConfigMissingError
from shared.inbound import SESSION_TAINT, TRUST_CLASS_INTERNAL
from shared.pending_send import PENDING_SEND
from shared.trust_decision import (
    ACTION_CLASS_BANNED,
    TRUST_DECISIONS,
    TrustDecision,
)

from . import voice_gate

# Action classes that must never fire autonomously on a turn that ingested
# untrusted (non-internal) inbound content — the taint-gate. READ and
# INTERNAL_WRITE (drafts) stay allowed: an EA reads untrusted mail and DRAFTS a
# reply; it never autonomously sends / files / executes BECAUSE of it.
_TAINT_GATED_CLASSES: frozenset[ActionClass] = frozenset(
    {
        ActionClass.EXTERNAL_SEND,
        ActionClass.EXTERNAL_SEND_INTERNAL,
        ActionClass.EXTERNAL_SEND_CLIENT,
        ActionClass.EXTERNAL_SEND_VENDOR,
        ActionClass.DESTRUCTIVE,
        ActionClass.COMMITMENT,
        ActionClass.CODE_EXECUTION,
    }
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ceiling enum + restrictiveness ordering
#
# String values match the ss-console ACCEPTED_EXPOSURE_CEILINGS exactly so the
# authoring side (TS validators) and this enforcement side round-trip through
# their string representations.
# ---------------------------------------------------------------------------


class Ceiling(str, enum.Enum):
    """The content classes per ADR 0035 / ADR 0056 / ADR 0071."""

    AUTONOMOUS = "autonomous"
    CONFIRM = "confirm"  # external_send: execute after an explicit current-turn approval (ADR 0071)
    DRAFT_FOR_REVIEW = "draft_for_review"
    REFUSED = "refused"


# Restrictiveness ordering: higher number == more restrictive. Used to combine a
# configured value with a vertical-pack floor (a floor can only narrow).
_RESTRICTIVENESS: dict[Ceiling, int] = {
    Ceiling.AUTONOMOUS: 0,
    Ceiling.CONFIRM: 1,
    Ceiling.DRAFT_FOR_REVIEW: 2,
    Ceiling.REFUSED: 3,
}


def _most_restrictive(a: Ceiling, b: Ceiling) -> Ceiling:
    return a if _RESTRICTIVENESS[a] >= _RESTRICTIVENESS[b] else b


def resolve_ceiling(
    action: ActionClass,
    exposure: Mapping[ActionClass, Ceiling] | None,
    vertical_floors: Mapping[ActionClass, Ceiling] | None = None,
) -> Ceiling:
    """Resolve the effective ceiling for one (non-read) action class.

    base = the persona's authored exposure for the class, or REFUSED when
    unauthored (ADR 0056 fail-closed — there is no imposed posture). effective =
    most restrictive of base and the vertical-pack floor for that class (a floor
    can only narrow, never raise). ``read`` is handled by the caller (always
    allowed) and never routed here.
    """
    base = exposure.get(action) if exposure else None
    if base is None:
        base = Ceiling.REFUSED
    floor = vertical_floors.get(action) if vertical_floors else None
    return _most_restrictive(base, floor) if floor is not None else base


# ---------------------------------------------------------------------------
# Banned-tool refusal messages
# ---------------------------------------------------------------------------


_BANNED_REFUSAL_MESSAGE: Mapping[str, str] = MappingProxyType(
    {
        "email_send": "autonomous email send is forbidden (ADR 0035)",
        "email_send_message": "autonomous email send is forbidden (ADR 0005)",
        "email_reply": "autonomous email reply is forbidden (ADR 0005)",
        "email_reply_all": "autonomous email reply-all is forbidden (ADR 0005)",
        "email_forward": "autonomous email forward is forbidden (ADR 0005)",
        "sms_send": "autonomous SMS send is forbidden (ADR 0005)",
        "sms_send_message": "autonomous SMS send is forbidden (ADR 0005)",
        "payments_initiate_transfer": "autonomous money movement is forbidden",
        "payments_send_payment": "autonomous money movement is forbidden",
        "payments_refund": "autonomous money movement is forbidden",
        "payments_authorize_charge": "autonomous money movement is forbidden",
        "payments_void_authorization": "autonomous money movement is forbidden",
        "calendar_delete_event": "calendar event deletion is forbidden",
        "practice_management_delete_matter": "matter deletion is forbidden",
        "practice_management_close_matter_permanent": "permanent matter close is forbidden",
        "connector_revoke_oauth": "connector OAuth revocation is forbidden",
        "connector_unbind_permanent": "permanent connector unbind is forbidden",
    }
)


# ---------------------------------------------------------------------------
# Decision shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnforcementDecision:
    """Internal decision shape + the structured audit fields (ADR 0056).

    ``audit_action`` is the hint for the audit plugin's downstream
    classification of this row (``allow`` | ``draft`` | ``refuse`` |
    ``await_approval``). The four
    ceiling fields carry the full trust trail: ``authored_ceiling`` is what the
    persona authored for this class (``None`` = unauthored / fail-closed),
    ``vertical_floor`` is the pack floor for this class (``None`` = no floor),
    ``effective_ceiling`` is the most-restrictive combination actually applied.
    """

    allowed: bool
    reason: str
    audit_action: str  # "allow" | "draft" | "refuse" | "await_approval"
    action_class: ActionClass
    authored_ceiling: Ceiling | None = None
    vertical_floor: Ceiling | None = None
    effective_ceiling: Ceiling | None = None


# ---------------------------------------------------------------------------
# Policy core
# ---------------------------------------------------------------------------


def enforce(
    *,
    action: ActionClass,
    exposure: Mapping[ActionClass, Ceiling] | None,
    tool_name: str,
    persona_slug: str = "",
    current_turn_approval: bool = False,
    vertical_floors: Mapping[ActionClass, Ceiling] | None = None,
    inbound_trust_class: str = TRUST_CLASS_INTERNAL,
) -> EnforcementDecision:
    """Return whether this tool call is allowed under the persona's exposure.

    ``exposure`` is the active persona's authored per-action-class ceiling map
    (read from the trusted customer.yaml). ``vertical_floors`` are non-raisable
    per-class floors from the vertical pack. Per ADR 0056 every non-read class
    with no authored exposure is REFUSED (fail-closed — no send, no draft, no
    write). ``read`` is always allowed.

    ``current_turn_approval`` is True iff the operator explicitly approved THIS
    action in the CURRENT invocation. It is the hard runtime floor for the
    REVERSIBILITY classes (COMMITMENT, DESTRUCTIVE) — required on top of an
    autonomous exposure. EXTERNAL_SEND autonomy is governed by exposure, not by
    an in-turn approval.

    The content-sensitivity floor (``shared.content_floor``) is applied a layer
    up, in ``evaluate_tool_call``, where the message body is available — it can
    only narrow an autonomous send to a draft.
    """
    floor = vertical_floors.get(action) if vertical_floors else None

    # READ — always allowed regardless of exposure. Reading more untrusted
    # content is harmless; taint applies to ACTIONS, not reads. Not authored.
    if action == ActionClass.READ:
        return EnforcementDecision(
            allowed=True,
            reason="read action",
            audit_action="allow",
            action_class=action,
            authored_ceiling=None,
            vertical_floor=floor,
            effective_ceiling=Ceiling.AUTONOMOUS,
        )

    # REFUSED action class — an UNKNOWN/unmapped tool (issue #1327). Terminal
    # fail-closed class, NOT routed through resolve_ceiling: no authored exposure
    # can widen it. Refused on every turn, tainted or not.
    if action == ActionClass.REFUSED:
        return EnforcementDecision(
            allowed=False,
            reason=(
                f"tool {tool_name} is not in the action-class registry; "
                f"unknown tools fail closed (issue #1327) — add it to "
                f"TOOL_ACTION_CLASS_MAP or BANNED_TOOLS to govern it"
            ),
            audit_action="refuse",
            action_class=action,
            effective_ceiling=Ceiling.REFUSED,
        )

    authored = exposure.get(action) if exposure else None
    effective = resolve_ceiling(action, exposure, vertical_floors)

    # TAINT-GATE (OP-P0-4 / OP-P0-5 / OP-P1-1). This turn's session ingested
    # untrusted inbound content. A sensitive action on such a turn cannot be
    # autonomous — an injected "send/archive/run this" must never execute BECAUSE
    # of untrusted content. The action is refused here (the agent may still READ
    # and DRAFT). It does not remove an authored capability, it withholds it for
    # the tainted turn only.
    if inbound_trust_class != TRUST_CLASS_INTERNAL and action in _TAINT_GATED_CLASSES:
        return EnforcementDecision(
            allowed=False,
            reason=(
                f"{action.value} refused: this turn ingested untrusted inbound "
                f"content (trust_class={inbound_trust_class}); a sensitive action "
                f"cannot fire autonomously on a tainted turn — read and draft only"
            ),
            audit_action="refuse",
            action_class=action,
            authored_ceiling=authored,
            vertical_floor=floor,
            effective_ceiling=effective,
        )

    decision = _enforce_resolved(
        action=action,
        effective=effective,
        tool_name=tool_name,
        current_turn_approval=current_turn_approval,
    )
    # Splice the audit ceiling trail onto the class-specific decision.
    return EnforcementDecision(
        allowed=decision.allowed,
        reason=decision.reason,
        audit_action=decision.audit_action,
        action_class=action,
        authored_ceiling=authored,
        vertical_floor=floor,
        effective_ceiling=effective,
    )


def _enforce_resolved(
    *,
    action: ActionClass,
    effective: Ceiling,
    tool_name: str,
    current_turn_approval: bool,
) -> EnforcementDecision:
    """Decide allow/draft/refuse for one non-read class at its effective ceiling.

    The returned decision's ceiling-trail fields are filled by the caller; only
    ``allowed`` / ``reason`` / ``audit_action`` are meaningful here.
    """
    if action == ActionClass.COMMITMENT:
        return _decide_approval_class(
            effective=effective,
            approved=current_turn_approval,
            label="commitment",
            draft_reason="draft_for_review skills do not originate commitments; produce draft instead",
            draft_audit="draft",
        )
    if action == ActionClass.DESTRUCTIVE:
        return _decide_approval_class(
            effective=effective,
            approved=current_turn_approval,
            label="destructive",
            draft_reason="draft_for_review skills do not originate destructive actions; report instead",
            draft_audit="refuse",
        )
    if action in (
        ActionClass.EXTERNAL_SEND,
        ActionClass.EXTERNAL_SEND_INTERNAL,
        ActionClass.EXTERNAL_SEND_CLIENT,
        ActionClass.EXTERNAL_SEND_VENDOR,
    ):
        # All four send classes resolve identically against their OWN authored
        # ceiling (external_send = outside recipient; external_send_internal =
        # rostered staff; external_send_client / external_send_vendor = the firm's
        # own rostered client / records vendor). The recipient axis is decided
        # upstream in evaluate_tool_call; by here the class is definite.
        if effective == Ceiling.AUTONOMOUS:
            return _allow(f"{action.value} permitted: authored exposure is autonomous", action)
        if effective == Ceiling.CONFIRM:
            # confirm (ADR 0071): send only with an explicit current-turn approval
            # captured by a TRUSTED runtime path; otherwise WITHHELD pending approval
            # (not drafted, not refused). The taint-gate upstream already blocked this
            # class on a tainted turn, so an inbound/injected "approval" cannot reach
            # here. Approval-capture round-trip is #1806; until then confirm withholds.
            if current_turn_approval:
                return _allow(
                    f"{action.value} confirmed by explicit current-turn approval (ADR 0071)", action
                )
            return _await_approval(
                f"{action.value} at authored confirm ceiling; withheld pending current-turn approval",
                action,
            )
        if effective == Ceiling.DRAFT_FOR_REVIEW:
            return _draft(
                f"{action.value} at authored draft_for_review ceiling; routing to draft", action
            )
        return _refuse(
            f"{action.value} refused: no authored exposure (fail-closed, ADR 0056) "
            "or a vertical floor refuses it",
            action,
        )
    if action == ActionClass.CODE_EXECUTION:
        if effective == Ceiling.AUTONOMOUS:
            return _allow("code_execution permitted: authored exposure is autonomous", action)
        return _refuse(
            "code_execution refused: no authored exposure (fail-closed, ADR 0056) "
            "or a vertical floor narrows it",
            action,
        )
    if action == ActionClass.INTERNAL_WRITE:
        if effective == Ceiling.AUTONOMOUS:
            return _allow("autonomous internal write", action)
        if effective == Ceiling.DRAFT_FOR_REVIEW:
            # The write proceeds, but routed to the draft/notes folder — unlike an
            # external_send draft (withheld), an internal write at draft is allowed.
            return EnforcementDecision(
                allowed=True,
                reason="internal write routed to draft folder",
                audit_action="draft",
                action_class=action,
            )
        return _refuse(
            "internal_write refused: no authored exposure (fail-closed, ADR 0056)", action
        )

    # Unknown action class — fail closed.
    return _refuse(f"unknown action class {action}; defaulting to refuse", action)


def _decide_approval_class(
    *,
    effective: Ceiling,
    approved: bool,
    label: str,
    draft_reason: str,
    draft_audit: str,
) -> EnforcementDecision:
    """COMMITMENT / DESTRUCTIVE: an exposure that does not reach autonomous never
    originates the action; an autonomous exposure still needs current-turn
    approval (the hard runtime floor, ADR 0056)."""
    action = ActionClass(label)  # "commitment" / "destructive" — both valid; caller re-stamps it
    if effective == Ceiling.REFUSED:
        return _refuse(
            f"{label} refused: no authored exposure (fail-closed, ADR 0056) or a floor refuses it",
            action,
        )
    if effective == Ceiling.DRAFT_FOR_REVIEW:
        return EnforcementDecision(
            allowed=False, reason=draft_reason, audit_action=draft_audit, action_class=action
        )
    if not approved:
        return _refuse(f"{label} action requires explicit current-turn approval", action)
    return _allow(f"{label} with current-turn approval", action)


def _allow(reason: str, action: ActionClass) -> EnforcementDecision:
    return EnforcementDecision(
        allowed=True, reason=reason, audit_action="allow", action_class=action
    )


def _draft(reason: str, action: ActionClass) -> EnforcementDecision:
    return EnforcementDecision(
        allowed=False, reason=reason, audit_action="draft", action_class=action
    )


def _refuse(reason: str, action: ActionClass) -> EnforcementDecision:
    return EnforcementDecision(
        allowed=False, reason=reason, audit_action="refuse", action_class=action
    )


def _await_approval(reason: str, action: ActionClass) -> EnforcementDecision:
    # ADR 0071: external_send at the confirm ceiling, withheld pending an explicit
    # current-turn approval. allowed=False (the gate blocks on `allowed`, so the
    # send does not fire); audit_action distinguishes it from a draft or a refusal.
    return EnforcementDecision(
        allowed=False, reason=reason, audit_action="await_approval", action_class=action
    )


# ---------------------------------------------------------------------------
# Active-persona exposure resolution (trusted source)
# ---------------------------------------------------------------------------


def _resolve_active_persona() -> str:
    """Resolve the active persona (profile) slug from the runtime env.

    Hermes runs each persona as its own profile (``hermes -p <slug>``) and
    exposes the active profile via ``HERMES_ACTIVE_PROFILE``; the
    ``SMD_ACTIVE_PERSONA`` secret is the fallback. Same channel the audit plugin
    uses. Empty when neither is set — the caller then resolves an empty exposure
    (fail-closed for every non-read class)."""
    return os.environ.get("HERMES_ACTIVE_PROFILE") or os.environ.get("SMD_ACTIVE_PERSONA") or ""


def _parse_exposure_map(raw: object) -> dict[ActionClass, Ceiling]:
    """Parse a ``{action_class_str: ceiling_str}`` exposure map to typed enums.

    ``read`` and unknown action classes are DROPPED (read is never authored;
    enforcement always allows it). An invalid ceiling string is DROPPED with a
    warning, never coerced — a garbled entry must fall back to the fail-closed
    REFUSED default for that class, never silently grant autonomy.
    """
    out: dict[ActionClass, Ceiling] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            action = ActionClass(str(k))
        except ValueError:
            logger.warning("exposure: unknown action class %r; dropping", k)
            continue
        if action == ActionClass.READ:
            # read is never authored; enforcement always allows it.
            continue
        try:
            ceiling = Ceiling(str(v))
        except ValueError:
            logger.warning(
                "exposure: invalid ceiling %r for %s; dropping (fail-closed default applies)",
                v,
                k,
            )
            continue
        out[action] = ceiling
    return out


def _overlay_runtime_overrides(
    persona_slug: str,
    authored: dict[ActionClass, Ceiling],
    ceiling_map: dict[ActionClass, Ceiling],
) -> dict[ActionClass, Ceiling]:
    """Layer the client-set runtime overrides onto the authored exposure.

    The entitlement dial (ss#2003 Q7): a Named Administrator's tier change is
    persisted by the gate into ``shared.exposure_override``; the EFFECTIVE
    exposure for each overridden class is the override value clamped to the
    authored bound — ``exposure_ceiling`` when authored for the class, else the
    authored exposure value, else REFUSED (ADR 0056: absence of an authored
    ceiling is absence of permission to raise). The write path applies the same
    clamp; this read-side clamp is defense in depth — a store row that exceeds
    the bound is NARROWED, never honored.

    Fault posture: a missing store file is "no override was ever set" (authored
    stands). Any other store fault PROPAGATES, same as a customer.yaml read
    fault — ``evaluate_tool_call``'s outer handler fails CLOSED for sensitive
    actions. Falling back to authored on a broken read is not safe in either
    direction: it could re-raise a posture the client lowered.
    """
    from shared.exposure_override import read_overrides  # local import, test-patchable

    overrides = read_overrides(persona_slug)
    if not overrides:
        return authored
    effective = dict(authored)
    for action, ceiling in _parse_exposure_map(overrides).items():
        bound = ceiling_map.get(action) or authored.get(action) or Ceiling.REFUSED
        effective[action] = _most_restrictive(ceiling, bound)
    return effective


def _resolve_persona_exposure(persona_slug: str) -> dict[ActionClass, Ceiling]:
    """Resolve the active persona's EFFECTIVE exposure from the trusted config.

    Reads ``customer.yaml`` via ``shared.customer_config`` (the keystone seam
    relocates it to a root-owned path), parses the named persona's
    ``entitlements.exposure`` to typed enums, then layers the client-set
    runtime overrides (``shared.exposure_override``) clamped to the authored
    ``entitlements.exposure_ceiling`` — see :func:`_overlay_runtime_overrides`.
    Returns ``{}`` (fail-closed for every non-read class) when:

      * no active persona is resolvable from the env;
      * there is no customer.yaml on the volume (dev / test — stub or absent);
      * the named persona is not present.

    Any OTHER read fault (unreadable / unparseable file, malformed personas on a
    provisioned Machine, a broken override store) PROPAGATES so
    ``evaluate_tool_call``'s outer handler fails CLOSED for sensitive actions
    rather than silently resolving an empty map. An empty map is itself
    fail-closed (every non-read class REFUSED), so a propagated fault and an
    empty map land on the same safe side — the propagate path exists only to
    surface the fault loudly.
    """
    if not persona_slug:
        return {}
    try:
        from shared.customer_config import CustomerConfig  # local import

        personas = CustomerConfig.from_volume().personas
    except NotImplementedError:
        return {}
    except CustomerConfigMissingError:
        logger.debug(
            "no customer.yaml on volume for exposure resolution; empty exposure",
            exc_info=True,
        )
        return {}
    for persona in personas:
        if isinstance(persona, dict) and persona.get("slug") == persona_slug:
            entitlements = persona.get("entitlements")
            raw = entitlements.get("exposure") if isinstance(entitlements, dict) else None
            raw_ceiling = (
                entitlements.get("exposure_ceiling") if isinstance(entitlements, dict) else None
            )
            authored = _parse_exposure_map(raw)
            return _overlay_runtime_overrides(
                persona_slug, authored, _parse_exposure_map(raw_ceiling)
            )
    logger.warning(
        "trust: active persona %r not found in customer.yaml; empty exposure (fail-closed)",
        persona_slug,
    )
    return {}


def _resolve_current_turn_approval(args: dict | None) -> bool:
    """Whether the operator approved THIS action in THIS turn.

    The runtime stamps ``_current_turn_approval`` onto the tool args when an
    approval has been registered for this exact call. Approvals from prior
    turns/sessions never carry over. This is an approval SIGNAL, not an
    entitlement — entitlements are resolved only from the trusted config.
    """
    if isinstance(args, dict):
        return bool(args.get("_current_turn_approval"))
    return False


def _resolve_skill_name(args: dict | None) -> str:
    """Best-effort skill-name for the audit reason string only (never an
    entitlement input)."""
    if isinstance(args, dict):
        name = args.get("_skill_name")
        if isinstance(name, str) and name:
            return name
    return "(unknown)"


# ---------------------------------------------------------------------------
# Vertical-pack safety floors (ADR 0022 / ADR 0037 Tenet 3)
#
# DERIVED, NOT DUPLICATED: the source of truth is the string-keyed
# ``shared.action_classes.VERTICAL_FLOORS``, which ``config_applier.safety`` also
# consumes for the apply-time floor check. This module builds the enum-keyed
# runtime map from it so the live ceiling resolver and the apply-time gate can
# never disagree about which floors are in force.
# ---------------------------------------------------------------------------


def _derive_vertical_floors() -> Mapping[str, Mapping[ActionClass, Ceiling]]:
    """Build the enum-keyed runtime floor map from the shared string source.

    A malformed entry (unknown action class or ceiling string) raises at import
    — a floor that cannot be enforced is a fail-closed boot error, never silently
    dropped.
    """
    out: dict[str, Mapping[ActionClass, Ceiling]] = {}
    for vertical, floors in _SHARED_VERTICAL_FLOORS.items():
        out[vertical] = MappingProxyType(
            {ActionClass(ac): Ceiling(ceiling) for ac, ceiling in floors.items()}
        )
    return MappingProxyType(out)


_VERTICAL_FLOORS: Mapping[str, Mapping[ActionClass, Ceiling]] = _derive_vertical_floors()


def _resolve_vertical() -> str:
    """Resolve the customer's vertical slug.

    Source order: ``customer.yaml`` (via ``shared.customer_config``) ->
    ``SMD_VERTICAL`` env override (dev / test) -> ``""`` (no vertical). Stub
    state and a genuinely absent file fall through to the env path; any other
    read fault propagates so the outer ``evaluate_tool_call`` handler fails
    closed — a floor must never be silently dropped on an I/O fault.
    """
    try:
        from shared.customer_config import CustomerConfig  # local import

        vertical = CustomerConfig.from_volume().vertical
        if vertical:
            return vertical
    except NotImplementedError:
        pass
    except CustomerConfigMissingError:
        logger.debug(
            "vertical resolution: no customer.yaml on volume; falling back to env",
            exc_info=True,
        )
    return os.environ.get("SMD_VERTICAL", "")


def _resolve_vertical_floors() -> dict[ActionClass, Ceiling]:
    """Resolve non-raisable per-action-class floors from the vertical pack.

    A declared floor is one a persona's authored exposure can only narrow,
    never raise (ADR 0025 / ADR 0022). No vertical currently declares one —
    the law-firm external-send floor was removed 2026-07 per ADR 0035 (see
    ``shared.action_classes.VERTICAL_FLOORS``). Returns ``{}`` for verticals
    with no declared floor.
    """
    floors = _VERTICAL_FLOORS.get(_resolve_vertical())
    return dict(floors) if floors else {}


# ---------------------------------------------------------------------------
# Content-sensitivity floor (ADR 0031)
# ---------------------------------------------------------------------------


_SEND_BODY_ARG_KEYS: tuple[str, ...] = (
    "subject",
    "text",
    "html",
    "body",
    "body_plain",
    "body_text",
    "content",
    "message",
)


def _extract_send_body(args: dict | None) -> str | None:
    """Concatenate the inspectable content of a send call (subject + body).

    Returns ``None`` when no recognizable content key is present — the caller
    treats that as INDETERMINATE and fails toward draft (an autonomous send we
    cannot inspect must not be certified non-sensitive).
    """
    if not isinstance(args, dict):
        return None
    parts: list[str] = []
    for key in _SEND_BODY_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
        elif value is not None and not isinstance(value, (dict, list)):
            coerced = str(value).strip()
            if coerced:
                parts.append(coerced)
    if not parts:
        return None
    return "\n".join(parts)


def _apply_content_floor(tool_name: str, args: dict | None) -> dict | None:
    """Content-sensitivity floor (ADR 0031) for an otherwise-autonomous send.

    Returns a draft-routing block directive when the send body touches money /
    contracts / scope / legal, or ``None`` to let the autonomous send proceed.
    Fails toward draft (block) on any scan error or uninspectable body.
    """
    try:
        body = _extract_send_body(args)
        result = content_floor.classify(body)
    except Exception:  # noqa: BLE001 — a send we cannot scan must not go out
        logger.exception(
            "content floor: scan failed for %s; failing toward draft (ADR 0031)",
            tool_name,
        )
        return {
            "action": "block",
            "message": (
                "Refused: content-sensitivity floor could not evaluate this send; "
                "routing to draft for review (ADR 0031)"
            ),
        }
    if not result.sensitive:
        return None
    cats = ", ".join(result.categories) if result.categories else "sensitive content"
    return {
        "action": "block",
        "message": (
            f"Refused: this message touches {cats}; routing to draft for human "
            f"review instead of autonomous send (content-sensitivity floor, "
            f"ADR 0031). Create a draft instead."
        ),
    }


# ---------------------------------------------------------------------------
# Decision audit
# ---------------------------------------------------------------------------


def _ceiling_str(value: Ceiling | None) -> str:
    return value.value if isinstance(value, Ceiling) else "unauthored"


def _audit_decision(tool_name: str, persona_slug: str, decision: EnforcementDecision) -> None:
    """Emit the structured trust-decision audit line (ADR 0056).

    Carries the six fields the audit review reads: action class, authored
    ceiling, vertical floor, effective ceiling, decision (audit_action), and the
    reason. Best-effort and never raises — observability must not perturb the
    tool path.
    """
    try:
        logger.info(
            "trust-decision tool=%s persona=%s action_class=%s authored_ceiling=%s "
            "vertical_floor=%s effective_ceiling=%s decision=%s reason=%s",
            tool_name,
            persona_slug or "(none)",
            decision.action_class.value,
            _ceiling_str(decision.authored_ceiling),
            _ceiling_str(decision.vertical_floor),
            _ceiling_str(decision.effective_ceiling),
            decision.audit_action,
            decision.reason,
        )
    except Exception:  # noqa: BLE001 — audit logging must never break the path
        logger.debug("trust: decision audit log failed", exc_info=True)


def _record_decision(
    tool_call_id: str,
    tool_name: str,
    persona_slug: str,
    *,
    action_class: str,
    audit_action: str,
    allowed: bool,
    reason: str,
    authored_ceiling: Ceiling | None = None,
    vertical_floor: Ceiling | None = None,
    effective_ceiling: Ceiling | None = None,
    session_id: str = "",
    session_match: str = "",
) -> None:
    """Hand this decision to the audit plugin's ``post_tool_call`` (#2122).

    The log line above is for a human reading the seat; this is for the ledger.
    Until it existed the trail was computed on every call and written to none of
    them, which is why ``ceiling_level`` was null on every live row.

    ``None`` ceilings are carried through as ``None``, never as the
    ``"unauthored"`` placeholder ``_ceiling_str`` renders for the log — the row
    must distinguish "no exposure was authored for this class" from "the
    resolver could not decide", and only the first is fail-closed by design.

    ``session_match`` says HOW the session this call was gated under resolved
    (ss-console #2288). Core drops ``session_id`` on this path (#141), so the
    value keying the matter gate's party set and the spec/voice marks may be an
    inference — and until now nothing recorded that it was one.

    Best-effort and never raises: the ledger is downstream of the decision, and
    a register fault must not change what the gate returns.
    """
    try:
        TRUST_DECISIONS.record(
            tool_call_id,
            tool_name,
            TrustDecision(
                action_class=action_class,
                audit_action=audit_action,
                allowed=allowed,
                authored_ceiling=authored_ceiling.value if authored_ceiling else None,
                vertical_floor=vertical_floor.value if vertical_floor else None,
                effective_ceiling=effective_ceiling.value if effective_ceiling else None,
                persona=persona_slug,
                reason=reason,
                session_match=session_match,
                session_resolved=session_id,
            ),
        )
    except Exception:  # noqa: BLE001 — the ledger handoff must never break the path
        logger.debug("trust: decision handoff to the audit row failed", exc_info=True)


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------


def _resolve_roster() -> list[str]:
    """Live organization roster (``scope.inbound_allow_from``) from the trusted
    config, for OUTBOUND recipient classification (ADR 0044 — live-read so
    authoring the roster takes effect with no restart).

    Empty on missing/stub config — fail-closed: with no roster, every send
    classifies OUTSIDE (gated), never INTERNAL. Any OTHER read fault propagates
    to ``evaluate_tool_call``'s outer handler, which fails closed for the send.
    The roster is documented human-authored (never appended from inbound), which
    is what makes it safe to use as OUTBOUND authorization, not just inbound trust.
    """
    from shared.customer_config import CustomerConfig  # local import

    try:
        return CustomerConfig.from_volume().inbound_roster
    except NotImplementedError:
        return []
    except CustomerConfigMissingError:
        return []


def _resolve_typed_roster() -> list[tuple[str, str]]:
    """Live typed outbound roster (``scope.outbound_roster``) from the trusted
    config, for OUTBOUND recipient classification (ADR 0075 — live-read so
    authoring it takes effect with no restart).

    Each entry is ``(address, class)`` with ``class`` in the closed set
    ``client`` / ``records_vendor``. Empty on missing/stub config — fail-closed:
    with no typed roster, every outside recipient stays on the outside
    ``external_send`` ceiling. Same fail-closed posture as :func:`_resolve_roster`;
    any OTHER read fault propagates to ``evaluate_tool_call``'s outer handler,
    which fails closed for the send. The roster is human-authored OUTBOUND
    authorization (never grown from inbound).
    """
    from shared.customer_config import CustomerConfig  # local import

    try:
        return CustomerConfig.from_volume().outbound_roster
    except NotImplementedError:
        return []
    except CustomerConfigMissingError:
        return []


def _reclassify_send(
    tool_name: str,
    args: dict,
    base_action: ActionClass,
    session_id: str,
    tainted: bool,
) -> ActionClass:
    """Route a proactive send to its recipient-scoped action class.

    Only proactive sends (``send_message`` / ``forward_message`` / ``send_draft``)
    that ``classify_tool`` tagged EXTERNAL_SEND are re-routed. A rostered internal
    recipient → EXTERNAL_SEND_INTERNAL; a typed-roster CLIENT / VENDOR recipient →
    EXTERNAL_SEND_CLIENT / EXTERNAL_SEND_VENDOR; anyone else → EXTERNAL_SEND. An
    UNRESOLVED recipient (a ``send_draft`` of a draft this session never observed,
    or an empty/garbage ``to``) → EXTERNAL_SEND (outside/draft), **never** a
    rostered class: a send is never promoted to a graduatable ceiling on an unknown
    recipient. ``reply_to_message`` is not a CLASSIFIED_SEND_TOOL — the reply plugin
    owns that recipient-locked path.
    """
    from shared.outbound_recipient import CLASSIFIED_SEND_TOOLS, send_recipients
    from shared.recipient_classifier import RecipientClass, classify_recipients_typed

    if base_action is not ActionClass.EXTERNAL_SEND or tool_name not in CLASSIFIED_SEND_TOOLS:
        return base_action
    recips = send_recipients(tool_name, args, session_id)
    if not recips:
        logger.info(
            "trust: proactive send %s has an unresolved recipient; routing OUTSIDE (draft)",
            tool_name,
        )
        return ActionClass.EXTERNAL_SEND
    cls = classify_recipients_typed(
        list(recips), _resolve_roster(), _resolve_typed_roster(), from_tainted=tainted
    )
    if cls is RecipientClass.INTERNAL:
        return ActionClass.EXTERNAL_SEND_INTERNAL
    if cls is RecipientClass.CLIENT:
        return ActionClass.EXTERNAL_SEND_CLIENT
    if cls is RecipientClass.VENDOR:
        return ActionClass.EXTERNAL_SEND_VENDOR
    return ActionClass.EXTERNAL_SEND


def _resolve_send_recipients(tool_name: str, args: dict, session_id: str) -> set[str]:
    """Normalized recipient set for a proactive send — the confirm-approval
    match key (ADR 0071 #1806). Empty when the recipient is unresolvable (a
    ``send_draft`` for a draft this session never saw, or a malformed ``to``);
    such a send is never approvable (stays fail-closed). Best-effort: any
    resolution error yields the empty set, not a raise."""
    from shared.outbound_recipient import send_recipients

    try:
        recips = send_recipients(tool_name, args or {}, session_id)
    except Exception:  # noqa: BLE001 — recipient resolution must not break the gate
        return set()
    return set(recips) if recips else set()


def evaluate_tool_call(
    tool_name: str,
    args: dict,
    customer_slug: str,
    session_id: str = "",
    tool_call_id: str = "",
    session_match: str = "",
) -> dict | None:
    """Decide whether a tool call may proceed.

    Returns ``None`` to allow the call, or
    ``{"action": "block", "message": "Refused: <reason>"}`` to block.

    Block precedence (first match wins):
      1. Tool name is in ``BANNED_TOOLS`` — refused regardless of exposure.
      2. The active persona's exposure refuses the action class via ``enforce()``.
         The per-session taint (``SESSION_TAINT``) is read by ``session_id`` and
         passed to ``enforce()`` as the taint-gate input.

    Every one of those outcomes — including the banned refusal and the
    fail-closed one — is handed to ``shared.trust_decision`` under
    ``tool_call_id`` so the audit row this call produces can state what
    authorized it (#2122). ``tool_call_id`` is optional because the out-of-band
    confirmed-send dispatch re-authorizes a stored payload with no tool call
    behind it; that path records under the empty key, which no ``post_tool_call``
    can collect ahead of its own pre-hook decision.

    ``session_id`` arrives ALREADY RESOLVED — the caller runs it through
    ``shared.provenance`` because core drops the kwarg on this path (#141) — and
    ``session_match`` is HOW it resolved. Both go onto the decision so the row
    states which session gated the call and whether that session was keyed or
    inferred (ss-console #2288). Every register read by session below is keyed
    off that single value: the taint gate, the matter gate's party set, and the
    spec and voice marks.

    Exception safety: any exception here is caught at the hook boundary; this
    function may raise internally and the caller's try/except in ``__init__.py``
    translates a raise into a fail-closed block.
    """
    if not tool_name:
        # Defensive: an empty tool name is a malformed pre-hook kwarg, not a
        # genuine refusal. Allow and let downstream surfaces complain.
        return None

    # 1. Banned tools — refuse before policy runs.
    try:
        classification = classify_tool(tool_name)
    except BannedToolError as err:
        message = _BANNED_REFUSAL_MESSAGE.get(err.tool_name, err.reason)
        banned_persona = _resolve_active_persona()
        logger.info(
            "trust-decision tool=%s persona=%s action_class=banned decision=refuse reason=%s",
            tool_name,
            banned_persona or "(none)",
            message,
        )
        # The audit plugin has its own defense-in-depth banned path at
        # post_tool_call; if a banned name ever reaches it, the INVARIANT_VIOLATION
        # row should carry the refusal that was actually made here.
        _record_decision(
            tool_call_id,
            tool_name,
            banned_persona,
            action_class=ACTION_CLASS_BANNED,
            audit_action="refuse",
            allowed=False,
            reason=message,
            effective_ceiling=Ceiling.REFUSED,
            session_id=session_id,
            session_match=session_match,
        )
        return {"action": "block", "message": f"Refused: {message}"}

    # 2. Resolve the active persona's exposure from the TRUSTED config + the
    # vertical floors, then enforce.
    #
    # FAIL CLOSED on resolution failure (issue #12). A raise here — a customer.yaml
    # parse error, malformed personas, an unexpected None — must NOT silently allow
    # a sensitive action. READ is the one exception: it is always permitted and
    # carries no external blast radius, so a transient config error must not brick
    # read-only tooling. Every other action class refuses when exposure is
    # indeterminate.
    persona_slug = _resolve_active_persona()
    try:
        exposure = _resolve_persona_exposure(persona_slug)
        vertical_floors = _resolve_vertical_floors()
        session_taint = SESSION_TAINT.trust_class(session_id)

        # Recipient axis: a proactive send to a rostered internal recipient is
        # governed by its own external_send_internal ceiling; anyone else (or an
        # unresolved recipient) stays external_send. Decided here where the args
        # (and, via the registry, the draft) are available.
        effective_action = _reclassify_send(
            tool_name,
            args or {},
            classification.action_class,
            session_id,
            tainted=session_taint != TRUST_CLASS_INTERNAL,
        )

        # Confirm-approval round-trip (ADR 0071 #1806). A send withheld at the
        # confirm ceiling is CAPTURED below (on await_approval); a matching
        # current-turn approval — marked by the trusted pre_llm_call path when the
        # allowlisted owner replies over Telegram — is PEEKED here (not yet
        # consumed) so it flows into enforce() as the current-turn approval. When
        # approved, the STORED payload is replayed over the live args BEFORE
        # enforce and every downstream scan (content-floor / voice / fabrication),
        # so what ships and what is inspected is exactly the reviewed content —
        # never the LLM's (possibly drifted) re-composition. Only a
        # resolved-recipient send participates; an unresolved recipient stays
        # fail-closed (never approvable). Consume happens once the send clears
        # every gate (below).
        # All send classes that can resolve at the confirm ceiling participate —
        # the outside class, the internal-staff class, and the typed client /
        # vendor classes (ADR 0075). _enforce_resolved routes all of them through
        # the same confirm branch, so all must capture/replay identically.
        is_send = effective_action in (
            ActionClass.EXTERNAL_SEND,
            ActionClass.EXTERNAL_SEND_INTERNAL,
            ActionClass.EXTERNAL_SEND_CLIENT,
            ActionClass.EXTERNAL_SEND_VENDOR,
        )
        send_recips = _resolve_send_recipients(tool_name, args, session_id) if is_send else set()
        approved_replay = False
        if send_recips and PENDING_SEND.has_approved_match(tool_name, send_recips):
            stored = PENDING_SEND.peek()
            if stored is not None and isinstance(args, dict):
                args.clear()
                args.update(copy.deepcopy(stored.args))
            approved_replay = True

        decision = enforce(
            action=effective_action,
            exposure=exposure,
            tool_name=tool_name,
            persona_slug=persona_slug,
            current_turn_approval=approved_replay or _resolve_current_turn_approval(args),
            vertical_floors=vertical_floors,
            inbound_trust_class=session_taint,
        )
    except Exception:  # noqa: BLE001
        if classification.action_class == ActionClass.READ:
            logger.warning(
                "trust: exposure resolution failed for READ tool %r; allowing "
                "(read is low-risk and always permitted)",
                tool_name,
                exc_info=True,
            )
            # An allow the resolver did not actually decide. The row says so —
            # effective_ceiling stays None (indeterminate, not "authored
            # autonomous"), which is exactly the distinction an auditor needs.
            _record_decision(
                tool_call_id,
                tool_name,
                persona_slug,
                action_class=ActionClass.READ.value,
                audit_action="allow",
                allowed=True,
                reason="read allowed despite an indeterminate exposure resolution",
                session_id=session_id,
                session_match=session_match,
            )
            return None
        logger.exception(
            "trust: exposure resolution failed for sensitive tool %r (action=%s); "
            "FAILING CLOSED — refusing the call",
            tool_name,
            classification.action_class,
        )
        _record_decision(
            tool_call_id,
            tool_name,
            persona_slug,
            action_class=classification.action_class.value,
            audit_action="refuse",
            allowed=False,
            reason="trust decision unavailable; failing closed",
            session_id=session_id,
            session_match=session_match,
        )
        return {
            "action": "block",
            "message": (
                f"Refused: trust decision unavailable for this "
                f"{classification.action_class} action; failing closed"
            ),
        }

    _audit_decision(tool_name, persona_slug, decision)
    # The same six fields the line above logs, handed to the row this call
    # produces (#2122). ``decision.action_class`` is the RESOLVED class — after
    # recipient reclassification — so the typed send classes entitlements
    # actually govern reach the ledger instead of stopping at the gate.
    _record_decision(
        tool_call_id,
        tool_name,
        persona_slug,
        action_class=decision.action_class.value,
        audit_action=decision.audit_action,
        allowed=decision.allowed,
        reason=decision.reason,
        authored_ceiling=decision.authored_ceiling,
        vertical_floor=decision.vertical_floor,
        effective_ceiling=decision.effective_ceiling,
        session_id=session_id,
        session_match=session_match,
    )

    # Capture a send withheld at the confirm ceiling so a later current-turn
    # approval releases exactly THIS payload (ADR 0071 #1806). A new compose
    # supersedes any prior pending; only a resolved-recipient send is captured.
    if is_send and send_recips and decision.audit_action == "await_approval":
        PENDING_SEND.capture(tool_name, args, send_recips)

    # ---- Outbound matter identity (ss#2167) --------------------------------
    # Deliberately OUTSIDE the ``decision.allowed`` guard below. On a seat where
    # every send sits at the draft_for_review ceiling, ``allowed`` is False for
    # EVERY send, so a check placed inside that block would never execute on the
    # seat that most needs it — the same defect, one layer in, as placing it
    # after the pre_tool_call early return.
    #
    # Neither side of the check is the model's word: the matter identifiers are
    # the ones physically in the body it wrote, and membership comes from
    # connector reads. A send declaring its own matter would be circular — the
    # model resolves the recipient's matter to address them, so it would declare
    # the recipient's matter and always agree with itself.
    #
    # EXTERNAL_SEND_INTERNAL is absent from this tuple by design: firm staff are
    # not expected to be parties, and an internal alert CARRIES matter context to
    # a colleague on purpose (ADR 0072).
    if is_send and effective_action in (
        ActionClass.EXTERNAL_SEND,
        ActionClass.EXTERNAL_SEND_CLIENT,
        ActionClass.EXTERNAL_SEND_VENDOR,
    ):
        matter_verdict = matter_gate.evaluate(
            session_id=session_id,
            body=matter_gate.body_from_args(args),
            recipients=send_recips,
            # A records vendor is not a party to the matter it is being written
            # about; the roster that types it is the CLIENT's, not ours.
            recipient_is_exempt=effective_action is ActionClass.EXTERNAL_SEND_VENDOR,
        )
        if matter_verdict.should_withhold and matter_gate.mode() == "block":
            if decision.allowed:
                return {
                    "action": "block",
                    "message": (
                        "Refused: this message cites "
                        f"{', '.join(matter_verdict.matters) or 'a matter'} but "
                        f"{matter_verdict.reason}; routing to draft for human review "
                        "(ss#2167 matter identity)"
                    ),
                }
            # The ceiling already withheld this send, so there is nothing left to
            # stop — but a reviewer working the draft queue must be able to tell a
            # suspected cross-matter send from ordinary review traffic. Re-record
            # carries the FULL original trail plus the augmented reason: rebuilding
            # the row from scratch would write the ceiling fields back to None and
            # re-break the null-ceiling defect #2122 fixed.
            _record_decision(
                tool_call_id,
                tool_name,
                persona_slug,
                action_class=decision.action_class.value,
                audit_action=decision.audit_action,
                allowed=decision.allowed,
                reason=f"{decision.reason} | MATTER_MISMATCH: {matter_verdict.reason}",
                authored_ceiling=decision.authored_ceiling,
                vertical_floor=decision.vertical_floor,
                effective_ceiling=decision.effective_ceiling,
                session_id=session_id,
                session_match=session_match,
            )

    # Content-sensitivity floor (ADR 0031). Applies to sends that LEAVE the firm:
    # the outside class plus the typed client / records-vendor classes. Money /
    # contract / scope / legal content bound for a client or vendor is downgraded
    # to a draft even under an autonomous exposure — a settlement dollar figure to
    # a client must draft. Only the INTERNAL send (external_send_internal, to the
    # firm's own rostered staff) is deliberately NOT content-floored — the whole
    # value of an internal alert is that it CARRIES the matter/deadline/dollar
    # context to a colleague; drafting it would re-break the ADR 0072 fix. Only a
    # send the exposure would ALLOW is floored (a draft/refuse already withholds it).
    if decision.allowed and effective_action in (
        ActionClass.EXTERNAL_SEND,
        ActionClass.EXTERNAL_SEND_CLIENT,
        ActionClass.EXTERNAL_SEND_VENDOR,
    ):
        floor_block = _apply_content_floor(tool_name, args)
        if floor_block is not None:
            return floor_block

        # Voice live-gate (ADR 0028 §2, #855; repointed per-class by ss#2086
        # step 1). ONLY an AUTONOMOUS send that LEAVES the firm (outside, or the
        # typed client / records-vendor classes) impersonates the principal's
        # voice with no human review — confirm / draft / refused already route to
        # a human, and external_send_internal is ops traffic, so it is out of
        # scope. The gate resolves its binding regime per (seat × output class),
        # ADDITIVELY: a class declared `output_classes.<class>.voice_spec:
        # expected` is governed by the authored-spec binding (installed +
        # hash-verified + read this turn), and every OTHER class keeps the
        # original voice_library / transform-ran binding — silent only on a seat
        # that authored neither. When bound it downgrades to draft (same
        # block-directive plumbing as the content floor).
        if decision.effective_ceiling == Ceiling.AUTONOMOUS:
            voice_block = voice_gate.check_voice_gate(
                tool_name=tool_name,
                action_class_value=getattr(effective_action, "value", ""),
                session_id=session_id,
            )
            if voice_block is not None:
                return voice_block

    # Authored-spec gate (ss ADR 0083, #2084). Distinct from the voice gate on
    # both axes, which is why it is a separate block rather than another clause
    # above. SCOPE: it covers external_send_internal too — the `staff` class,
    # whose persona voice needs no customer corpus and is therefore the one
    # provable from day one, and which is also the highest-volume output the
    # firm forms its daily impression from. BINDING: it fires only where the
    # seat DECLARES `output_classes.<class>.voice_spec: expected`, so a seat
    # that authored nothing is untouched. A declared-but-never-READ spec
    # downgrades to draft. A declared-but-never-INSTALLED one is a broken
    # control, and since 2026-08-10 what that costs depends on who is waiting:
    # `staff` proceeds in the persona's own register (a person is waiting on ops
    # mail, and six days of silent refusals proved that refusing costs them the
    # message — ss-console #2228), outbound routes to a human, work_product and
    # record still refuse. Tamper and an unreadable manifest refuse everywhere.
    # The gate owns that fan-out; this call site does not need to know it.
    if decision.allowed and decision.effective_ceiling == Ceiling.AUTONOMOUS:
        spec_block = spec_gate.check_spec_gate(
            tool_name=tool_name,
            action_class_value=getattr(effective_action, "value", ""),
            session_id=session_id,
            # The composed text, or None meaning INDETERMINATE — passed through
            # UNCOERCED. It used to arrive here as `or ""`, which made the gate
            # skip its format check on exactly the sends the content floor
            # treats as most suspect: one value, two adjacent call sites,
            # opposite dispositions (ss-console #2234).
            #
            # The gate used to ask only "did the model read
            # its spec"; with the body it can also answer "does what it wrote
            # have the authored shape" — the binary half of ADR 0083 §3, and
            # the only half a machine can decide rather than grade.
            body=_extract_send_body(args),
        )
        if spec_block is not None:
            return spec_block

    if decision.allowed:
        # A confirm-approved send has cleared the ceiling, the content-floor, and
        # the voice-gate and is about to ship — consume the single-use approval so
        # it can never release a second send (ADR 0071 #1806). The live args were
        # already overwritten with the stored payload above.
        if is_send and approved_replay:
            PENDING_SEND.take_for_send(tool_name, send_recips)
        return None

    return {"action": "block", "message": f"Refused: {decision.reason}"}


__all__ = [
    "ActionClass",
    "BANNED_TOOLS",
    "BannedToolError",
    "Ceiling",
    "EnforcementDecision",
    "TOOL_ACTION_CLASS_MAP",
    "ToolClassification",
    "classify_tool",
    "enforce",
    "evaluate_tool_call",
    "resolve_ceiling",
]
