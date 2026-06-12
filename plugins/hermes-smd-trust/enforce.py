"""Trust-ceiling enforcement — the safety floor under every tool call.

Ported from ``ss-console/operator/adapter/trust_ceiling.py`` (the policy
core). The per-tool classification vocabulary (``ActionClass``,
``BANNED_TOOLS``, ``TOOL_ACTION_CLASS_MAP``, ``BannedToolError``,
``ToolClassification``, ``classify_tool``) lives in
``shared.action_classes`` so the audit and trust plugins share one source
of truth (consolidation: task #33). This module imports those names and
re-exports them via ``__all__`` so downstream trust consumers keep working.

Per-customer ceiling resolution
-------------------------------

The ceiling for a given tool call is the lower of two values:

  1. ``customer.yaml.scope.trust_ceiling`` — the customer-authored cap.
  2. The SKILL.md frontmatter ``trust_ceiling`` for the current skill —
     the skill-author cap.

``customer.yaml`` cannot raise above the SKILL.md declaration; the SKILL.md
is the authoritative content-class ceiling for that skill. The customer
ceiling can only narrow.

The customer-yaml read is deferred to ``shared.customer_config`` (agent E
owns that port). Until that lands, this module reads the ceiling from a
process-environment override (``SMD_TRUST_CEILING``) so tests and dev
runtimes can exercise enforcement without a populated volume.

Per-tool action class
---------------------

Each tool name is classified into one of five action classes (READ,
INTERNAL_WRITE, EXTERNAL_SEND, COMMITMENT, DESTRUCTIVE). The registry is
closed-vocabulary: unmapped tool names default to READ and are flagged
so audit review can surface them. A second closed set, ``BANNED_TOOLS``,
captures Pattern-A / Pattern-B forbidden capabilities (email_send,
payments_*, delete_event, etc.); a banned tool is refused before policy
even runs.

Refusal shape
-------------

The plugin's ``pre_tool_call`` hook expects either ``None`` (allow) or a
block directive:

    {"action": "block", "message": "Refused: <reason>"}

This module's ``evaluate_tool_call`` returns exactly that shape. The audit
plugin observes the refusal via its own ``post_tool_call`` hook on the
error-result path — trust does not call into audit directly (loose
coupling, per AGENTS.md).
"""

import enum
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from shared import content_floor
from shared.action_classes import (
    BANNED_TOOLS,
    TOOL_ACTION_CLASS_MAP,
    ActionClass,
    BannedToolError,
    ToolClassification,
    classify_tool,
)
from shared.customer_config import CustomerConfigMissingError
from shared.inbound import SESSION_TAINT, TRUST_CLASS_INTERNAL

# Action classes that must never fire autonomously on a turn that ingested
# untrusted (non-internal) inbound content — the taint-gate. READ and
# INTERNAL_WRITE (drafts) stay allowed: an EA reads untrusted mail and DRAFTS a
# reply; it never autonomously sends / files / executes BECAUSE of it.
_TAINT_GATED_CLASSES: frozenset[ActionClass] = frozenset(
    {
        ActionClass.EXTERNAL_SEND,
        ActionClass.DESTRUCTIVE,
        ActionClass.COMMITMENT,
        ActionClass.CODE_EXECUTION,
    }
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trust-ceiling enum
#
# String values match the ss-console adapter exactly so the two enforcement
# surfaces (TS validators on the authoring side, Python enforcement here)
# round-trip through their string representations.
# ---------------------------------------------------------------------------


class Ceiling(str, enum.Enum):
    """Three content classes per ADR 0035."""

    AUTONOMOUS = "autonomous"
    DRAFT_FOR_REVIEW = "draft_for_review"
    REFUSED = "refused"


# NOTE: ceiling comparison lives in ``_most_restrictive`` below. A second
# implementation (``_min_ceiling`` over an ordered tuple) coexisted here
# until the 2026-06-12 code review — two copies of the same comparison that
# had to agree on any future ceiling tier. One survives.


# ---------------------------------------------------------------------------
# Per-action-class ceiling resolution (ADR 0025)
#
# Mirrors the canonical policy core in
# ``ss-console/operator/adapter/trust_ceiling.py`` (the boot-invariant
# imports that one; this overlay copy runs live in the gateway pre_tool_call
# hook — the two must agree). Autonomy is configured per ActionClass, not by
# one skill scalar. Per ADR 0035 there is NO imposed default: ``external_send``
# (and any unrecognized entitled class) is fail-closed (``refused`` — no send,
# no draft) when no ``action_ceilings`` entry is authored. ``draft_for_review``
# is a value an engagement authors explicitly, never a
# fallback; a vertical-pack floor can only narrow, never widen.
# ---------------------------------------------------------------------------


# Restrictiveness ordering: higher number == more restrictive. Used to combine a
# configured value with a vertical-pack floor (a floor can only narrow).
_RESTRICTIVENESS: dict[Ceiling, int] = {
    Ceiling.AUTONOMOUS: 0,
    Ceiling.DRAFT_FOR_REVIEW: 1,
    Ceiling.REFUSED: 2,
}


def _most_restrictive(a: Ceiling, b: Ceiling) -> Ceiling:
    return a if _RESTRICTIVENESS[a] >= _RESTRICTIVENESS[b] else b


def _unauthored_resolution(action: ActionClass, skill_ceiling: Ceiling) -> Ceiling:
    """How an action class resolves when the engagement authored NO ceiling for
    it. There is no imposed posture (ADR 0035): an unauthored entitled action is
    fail-closed (``refused``) — it does not execute, and no draft is produced.
    ``draft_for_review`` is a value an engagement authors
    explicitly, never a fallback.

    ``READ`` resolves to ``autonomous`` at this layer because read *breadth* is
    governed by the authored scope envelope one layer over. ``INTERNAL_WRITE``
    follows the skill's authored scalar ceiling. Every other entitled class with
    no authored ceiling is ``refused``."""
    if action == ActionClass.READ:
        return Ceiling.AUTONOMOUS
    if action == ActionClass.INTERNAL_WRITE:
        return skill_ceiling
    # EXTERNAL_SEND and any unrecognized entitled class: no authored grant means
    # no action (ADR 0035 fail-closed). COMMITMENT / DESTRUCTIVE additionally
    # carry their own current-turn-approval reversibility floors in enforce().
    return Ceiling.REFUSED


def resolve_ceiling(
    action: ActionClass,
    skill_ceiling: Ceiling,
    action_ceilings: Mapping[ActionClass, Ceiling] | None = None,
    vertical_floors: Mapping[ActionClass, Ceiling] | None = None,
) -> Ceiling:
    """Resolve the effective ceiling for one action class.

    Effective = most restrictive of:
      - the customer's explicit per-action override (if present), else the
        unauthored resolution (fail-closed for entitled classes, ADR 0035); and
      - the vertical-pack floor for that class (if present).

    A vertical floor can only make the result *more* restrictive — customer
    config can never raise above it (ADR 0025 / ADR 0022 compliance floors).
    """
    explicit = action_ceilings.get(action) if action_ceilings else None
    base = explicit if explicit is not None else _unauthored_resolution(action, skill_ceiling)
    floor = vertical_floors.get(action) if vertical_floors else None
    return _most_restrictive(base, floor) if floor is not None else base


# ---------------------------------------------------------------------------
# Banned-tool refusal messages
#
# ``shared.action_classes.BANNED_REASON`` carries the closed-vocabulary
# category code (``"banned_tool_pattern_a"`` / ``"banned_tool_destructive"``)
# that the audit plugin persists in ``metadata.banned_reason``. Trust renders
# its own user-visible refusal sentence at the policy boundary so the
# operator-facing block message stays readable; the categorical code stays
# the source of truth on the audit side.
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
    """Internal decision shape. ``audit_action`` is the hint for the audit
    plugin's downstream classification of this row.
    """

    allowed: bool
    reason: str
    audit_action: str  # "allow" | "draft" | "refuse"


# ---------------------------------------------------------------------------
# Policy core
# ---------------------------------------------------------------------------


def enforce(
    *,
    ceiling: Ceiling,
    action: ActionClass,
    skill_name: str,
    tool_name: str,
    current_turn_approval: bool = False,
    action_ceilings: Mapping[ActionClass, Ceiling] | None = None,
    vertical_floors: Mapping[ActionClass, Ceiling] | None = None,
    inbound_trust_class: str = TRUST_CLASS_INTERNAL,
) -> EnforcementDecision:
    """Return whether this tool call is allowed under the configured ceilings.

    ``ceiling`` is the skill-level scalar (governs ``internal_write`` and acts
    as the REFUSED cap). ``action_ceilings`` are the customer's explicit
    per-action-class overrides; ``vertical_floors`` are non-raisable per-class
    floors from the vertical pack. Both optional — when omitted, the unauthored
    resolution applies (ADR 0035): entitled classes such as ``external_send``
    are fail-closed (``refused`` — no send, no draft) until the engagement
    authors a ceiling. ``draft_for_review`` is an authored
    value, not a fallback posture.

    ``current_turn_approval`` is True iff the operator explicitly approved
    THIS specific action in the CURRENT invocation. Approvals from prior
    turns or prior sessions are NOT valid (safety invariant #1). It gates the
    REVERSIBILITY classes (COMMITMENT, DESTRUCTIVE) only; ``external_send``
    autonomy is governed by the configured ceiling, NOT by an in-turn approval
    (ADR 0025 — the hardcoded "always require approval" send refusal is gone).

    Logic:
      - REFUSED scalar refuses everything (including READ).
      - READ is always allowed under non-REFUSED ceilings.
      - COMMITMENT requires AUTONOMOUS + current-turn approval (invariant #3).
      - DESTRUCTIVE requires AUTONOMOUS + current-turn approval (invariant #1).
      - EXTERNAL_SEND: resolved per-action ceiling — autonomous → send;
        authored draft_for_review → draft; refused (incl. unauthored,
        fail-closed per ADR 0035) → block.
      - INTERNAL_WRITE: resolved per-action ceiling — autonomous → write;
        draft_for_review → route to draft folder; refused → block.

    The content-sensitivity floor (``shared.content_floor``) is applied a layer
    up, in ``evaluate_tool_call``, where the tool ``args`` (and thus the message
    body) are available — it can only narrow an autonomous send to a draft.
    """
    # REFUSED ceiling: nothing executes
    if ceiling == Ceiling.REFUSED:
        return EnforcementDecision(
            allowed=False,
            reason=(f"skill {skill_name} has trust_ceiling=refused; tool {tool_name} blocked"),
            audit_action="refuse",
        )

    # READ always allowed regardless of ceiling (non-REFUSED). Reading more
    # untrusted content is harmless; taint applies to ACTIONS, not reads.
    if action == ActionClass.READ:
        return EnforcementDecision(allowed=True, reason="read action", audit_action="allow")

    # TAINT-GATE (OP-P0-4 / OP-P0-5 / OP-P1-1). This turn's session ingested
    # untrusted inbound content (an email body, a connector record, a fetched
    # page). A sensitive action on such a turn cannot be autonomous — an injected
    # "send/archive/run this" must never execute BECAUSE of untrusted content.
    # The action is refused here (the agent may still READ and DRAFT). This is
    # the structural tie between injection-ingress and action-egress; it does not
    # remove an authored autonomous capability, it withholds it for the tainted
    # turn only.
    if inbound_trust_class != TRUST_CLASS_INTERNAL and action in _TAINT_GATED_CLASSES:
        return EnforcementDecision(
            allowed=False,
            reason=(
                f"{action.value} refused: this turn ingested untrusted inbound "
                f"content (trust_class={inbound_trust_class}); a sensitive action "
                f"cannot fire autonomously on a tainted turn — read and draft only"
            ),
            audit_action="refuse",
        )

    # COMMITMENT — never autonomous without approval (invariant #3).
    if action == ActionClass.COMMITMENT:
        if ceiling == Ceiling.DRAFT_FOR_REVIEW:
            return EnforcementDecision(
                allowed=False,
                reason=(
                    "draft_for_review skills do not originate commitments; produce draft instead"
                ),
                audit_action="draft",
            )
        if not current_turn_approval:
            return EnforcementDecision(
                allowed=False,
                reason="commitment action requires explicit current-turn approval",
                audit_action="refuse",
            )
        return EnforcementDecision(
            allowed=True,
            reason="commitment with current-turn approval",
            audit_action="allow",
        )

    # DESTRUCTIVE — never autonomous without approval (invariant #1).
    if action == ActionClass.DESTRUCTIVE:
        if ceiling == Ceiling.DRAFT_FOR_REVIEW:
            return EnforcementDecision(
                allowed=False,
                reason=(
                    "draft_for_review skills do not originate destructive actions; report instead"
                ),
                audit_action="refuse",
            )
        if not current_turn_approval:
            return EnforcementDecision(
                allowed=False,
                reason="destructive action requires explicit current-turn approval",
                audit_action="refuse",
            )
        return EnforcementDecision(
            allowed=True,
            reason="destructive with current-turn approval",
            audit_action="allow",
        )

    # EXTERNAL_SEND — governed by the resolved per-action ceiling (ADR 0025/0035).
    # autonomous → send; authored draft_for_review → draft; refused → block.
    # Unauthored external_send is fail-closed (refused), not draft (ADR 0035 —
    # no imposed default). No in-turn-approval escape: exposure autonomy is
    # configured, not approved per message. The content-sensitivity floor
    # (evaluate_tool_call) can still narrow an autonomous send to a draft.
    if action == ActionClass.EXTERNAL_SEND:
        eff = resolve_ceiling(action, ceiling, action_ceilings, vertical_floors)
        if eff == Ceiling.AUTONOMOUS:
            return EnforcementDecision(
                allowed=True,
                reason="external_send permitted: configured ceiling is autonomous",
                audit_action="allow",
            )
        if eff == Ceiling.REFUSED:
            return EnforcementDecision(
                allowed=False,
                reason="external_send refused: configured ceiling (or vertical floor) is refused",
                audit_action="refuse",
            )
        # draft_for_review — an AUTHORED ceiling (not a default).
        return EnforcementDecision(
            allowed=False,
            reason="external_send at authored draft_for_review ceiling; routing to draft",
            audit_action="draft",
        )

    # CODE_EXECUTION — arbitrary code / shell / subagent / OS control. Governed
    # by its OWN resolved per-action ceiling (ADR 0035): unauthored is fail-closed
    # (refused — the agent's back door is shut unless the engagement opens a
    # ``code_execution`` ceiling). autonomous → allow; anything else → refuse
    # (there is no "draft" of a code execution).
    #
    # Deliberately NOT gated by the skill's output trust_ceiling scalar: code
    # execution is an internal MECHANISM, not an output class. A draft-for-review
    # skill (e.g. ar-chaser, whose OUTPUT is drafts) still runs its authored
    # ADR-0021 fetch loop. The skill scalar only blocks everything via the
    # REFUSED check at the top; the taint-gate above already withheld code
    # execution on any untrusted-fed turn.
    if action == ActionClass.CODE_EXECUTION:
        eff = resolve_ceiling(action, ceiling, action_ceilings, vertical_floors)
        if eff == Ceiling.AUTONOMOUS:
            return EnforcementDecision(
                allowed=True,
                reason="code_execution permitted: authored code_execution ceiling is autonomous",
                audit_action="allow",
            )
        return EnforcementDecision(
            allowed=False,
            reason=(
                "code_execution refused: no authored code_execution ceiling "
                "(fail-closed, ADR 0035) or a vertical floor narrows it"
            ),
            audit_action="refuse",
        )

    # INTERNAL_WRITE — governed by the resolved per-action ceiling (defaults to
    # the skill scalar). autonomous → write; draft_for_review → route to draft
    # folder; refused (only if explicitly set) → block.
    if action == ActionClass.INTERNAL_WRITE:
        eff = resolve_ceiling(action, ceiling, action_ceilings, vertical_floors)
        if eff == Ceiling.AUTONOMOUS:
            return EnforcementDecision(
                allowed=True,
                reason="autonomous internal write",
                audit_action="allow",
            )
        if eff == Ceiling.REFUSED:
            return EnforcementDecision(
                allowed=False,
                reason="internal_write refused by configured ceiling",
                audit_action="refuse",
            )
        # draft_for_review: allow write but route to notes folder.
        return EnforcementDecision(
            allowed=True,
            reason="internal write routed to draft folder",
            audit_action="draft",
        )

    # Unknown action class — fail closed.
    return EnforcementDecision(
        allowed=False,
        reason=f"unknown action class {action}; defaulting to refuse",
        audit_action="refuse",
    )


# ---------------------------------------------------------------------------
# Ceiling resolution
#
# Customer ceiling source order:
#   1. ``customer.yaml.scope.trust_ceiling`` (via shared.customer_config).
#   2. ``SMD_TRUST_CEILING`` env var (override for dev / test).
#   3. Default: DRAFT_FOR_REVIEW (the most restrictive non-refused ceiling).
#
# SKILL.md source order:
#   1. ``args["_skill_trust_ceiling"]`` if the runtime stamps it onto the
#      tool args (Hermes plugin contract — the active skill is observable
#      to the pre-hook through tool args).
#   2. Default: AUTONOMOUS (matches Hermes-skill authoring default; the
#      customer-cap will narrow it).
# ---------------------------------------------------------------------------


_DEFAULT_CUSTOMER_CEILING = Ceiling.DRAFT_FOR_REVIEW
_DEFAULT_SKILL_CEILING = Ceiling.AUTONOMOUS


def _parse_ceiling(value: str | None, fallback: Ceiling) -> Ceiling:
    if not value:
        return fallback
    try:
        return Ceiling(value)
    except ValueError:
        logger.warning(
            "trust ceiling value %r is not one of %s; using fallback %s",
            value,
            sorted(c.value for c in Ceiling),
            fallback.value,
        )
        return fallback


def _resolve_customer_ceiling() -> Ceiling:
    """Resolve the customer-authored ceiling.

    Reads from ``shared.customer_config.CustomerConfig.from_volume()`` when
    available; falls back to the ``SMD_TRUST_CEILING`` env var; finally
    defaults to ``DRAFT_FOR_REVIEW``.

    Only two read outcomes fall through to the env path: the stub state
    (``NotImplementedError``) and a genuinely absent file
    (``CustomerConfigMissingError`` — dev / test boxes with no provisioned
    volume). Any OTHER failure (unreadable file, YAML parse error,
    attribute miss) propagates so ``evaluate_tool_call``'s outer handler
    fails CLOSED for sensitive actions. Before the 2026-06-12 code review
    this caught broad ``Exception``, which silently downgraded an authored
    ``refused`` ceiling to the DRAFT_FOR_REVIEW default on any I/O fault —
    a fail-open relative to the authored posture (ADR 0035).
    """
    # Try the shared loader first.
    try:
        from shared.customer_config import CustomerConfig  # local import

        cfg = CustomerConfig.from_volume()
        scope = getattr(cfg, "scope", None) or {}
        value = scope.get("trust_ceiling") if isinstance(scope, dict) else None
        if value:
            return _parse_ceiling(value, _DEFAULT_CUSTOMER_CEILING)
    except NotImplementedError:
        # Stub state — customer_config not yet ported. Fall through to env.
        pass
    except CustomerConfigMissingError:
        # No customer.yaml on the volume (dev / test). Fall through to env.
        logger.debug(
            "no customer.yaml on volume for ceiling resolution; falling back to env",
            exc_info=True,
        )

    env_value = os.environ.get("SMD_TRUST_CEILING")
    return _parse_ceiling(env_value, _DEFAULT_CUSTOMER_CEILING)


def _resolve_skill_ceiling(args: dict | None) -> Ceiling:
    """Resolve the SKILL.md-declared ceiling for the active skill.

    The runtime is expected to stamp ``_skill_trust_ceiling`` onto the tool
    args before the pre-hook runs. Absent that, fall back to AUTONOMOUS so
    the customer cap dominates.
    """
    if not isinstance(args, dict):
        return _DEFAULT_SKILL_CEILING
    value = args.get("_skill_trust_ceiling")
    return _parse_ceiling(value, _DEFAULT_SKILL_CEILING)


def _resolve_skill_name(args: dict | None) -> str:
    """Best-effort skill-name resolution for the audit reason string."""
    if isinstance(args, dict):
        name = args.get("_skill_name")
        if isinstance(name, str) and name:
            return name
    return "(unknown)"


def _resolve_current_turn_approval(args: dict | None) -> bool:
    """Whether the operator approved THIS action in THIS turn.

    The runtime stamps ``_current_turn_approval`` onto the tool args when
    an approval has been registered for this exact call. Approvals from
    prior turns/sessions never carry over.
    """
    if isinstance(args, dict):
        return bool(args.get("_current_turn_approval"))
    return False


def _parse_action_ceiling_map(raw: object) -> dict[ActionClass, Ceiling]:
    """Parse a ``{action_class_str: ceiling_str}`` map into typed enums.

    Unparseable keys/values are DROPPED with a warning, never coerced — a
    garbled ``external_send`` entry must fall back to the safe class default
    (draft), not silently grant autonomy.
    """
    out: dict[ActionClass, Ceiling] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            action = ActionClass(str(k))
        except ValueError:
            logger.warning("action_ceilings: unknown action class %r; dropping", k)
            continue
        try:
            ceiling = Ceiling(str(v))
        except ValueError:
            logger.warning(
                "action_ceilings: invalid ceiling %r for %s; dropping (safe default applies)",
                v,
                k,
            )
            continue
        out[action] = ceiling
    return out


def _resolve_action_ceilings(args: dict | None) -> dict[ActionClass, Ceiling]:
    """Resolve the explicit per-action-class ceiling overrides (ADR 0025).

    Source order (later overrides earlier — the active skill is most specific):
      1. ``customer.yaml.scope.action_ceilings`` — a customer-wide override.
      2. ``args["_action_ceilings"]`` — the active skill's per-action map,
         stamped onto the tool args by the runtime (the same channel as
         ``_skill_trust_ceiling``).

    Absent both, returns ``{}`` so the safe class defaults apply (notably
    ``external_send`` → draft_for_review). The agent can never raise its own
    ceiling — these come from authored config, never from model output.
    """
    merged: dict[ActionClass, Ceiling] = {}
    # 1. Customer-wide override from customer.yaml scope.
    try:
        from shared.customer_config import CustomerConfig

        cfg = CustomerConfig.from_volume()
        scope = getattr(cfg, "scope", None) or {}
        if isinstance(scope, dict):
            merged.update(_parse_action_ceiling_map(scope.get("action_ceilings")))
    except NotImplementedError:
        pass
    except CustomerConfigMissingError:
        # No customer.yaml on the volume (dev / test): args / defaults apply.
        # Any other read fault propagates — evaluate_tool_call fails closed
        # rather than silently dropping authored per-class overrides.
        logger.debug(
            "action_ceilings: no customer.yaml on volume; using args / defaults",
            exc_info=True,
        )
    # 2. Active-skill override from args (wins over customer-wide).
    if isinstance(args, dict):
        merged.update(_parse_action_ceiling_map(args.get("_action_ceilings")))
    return merged


# ---------------------------------------------------------------------------
# Vertical-pack safety floors (ADR 0022 / ADR 0037 Tenet 3)
#
# A vertical pack declares non-raisable safety floors in its manifest
# (``operator/verticals/<vertical>/vertical.yaml`` -> ``compliance:``). A floor
# can only *narrow* a customer's authored ceiling, never raise it
# (``resolve_ceiling``). The floor SEMANTICS are encoded here keyed by vertical
# slug: each pack-declared compliance slug maps to the action-class ceiling it
# pins. The pack's slug list is the source of truth; this registry is its
# runtime realization — the Machine reads the customer's ``vertical`` field
# (cheap, always present), not the full pack manifest.
#
#   law-firm / ``external-send-draft-floor`` -> EXTERNAL_SEND pinned to
#     draft_for_review: client- and tribunal-bound mail ships under a human
#     reviewer's identity (ADR 0005), non-raisable. See
#     ``operator/verticals/law-firm/{vertical.yaml,compliance-floor.md}``.
# ---------------------------------------------------------------------------


_VERTICAL_FLOORS: Mapping[str, Mapping[ActionClass, Ceiling]] = MappingProxyType(
    {
        "law-firm": MappingProxyType({ActionClass.EXTERNAL_SEND: Ceiling.DRAFT_FOR_REVIEW}),
    }
)


def _resolve_vertical() -> str:
    """Resolve the customer's vertical slug.

    Source order: ``customer.yaml`` (via ``shared.customer_config``) ->
    ``SMD_VERTICAL`` env override (dev / test) -> ``""`` (no vertical).
    Stub state and a genuinely absent file fall through to the env path;
    any other read fault (unreadable / unparseable file on a provisioned
    Machine) propagates so the outer ``evaluate_tool_call`` handler fails
    closed. A floor must never be silently dropped on an I/O fault — a law
    customer who authored ``external_send: autonomous`` relies on the pack
    floor to narrow it back to draft (2026-06-12 code review).
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

    The law-firm pack's ``external-send-draft-floor`` pins ``external_send`` to
    ``draft_for_review`` — a floor a customer's authored ceiling can only narrow,
    never raise (ADR 0025 / ADR 0022). Returns ``{}`` for verticals with no
    declared floor (e.g. customer-zero ``mixed``).

    Closes the prior HONEST GAP: a law customer who forgets to author the
    ``external_send`` ceiling now still gets the floor from the pack, so a
    client/tribunal-bound send can never go out autonomously on a law Machine.
    """
    floors = _VERTICAL_FLOORS.get(_resolve_vertical())
    return dict(floors) if floors else {}


# Body-bearing arg keys on AgentMail / generic send tools. ``subject`` is
# included so a sensitive subject line alone (e.g. "Invoice #1200") trips the
# floor. ``send_draft`` (sends a pre-composed draft by id) and a bodyless
# forward carry NO inspectable content here — ``_extract_send_body`` returns
# ``None`` and ``content_floor.classify(None)`` fails toward draft.
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
# Hook entry point
# ---------------------------------------------------------------------------


def evaluate_tool_call(
    tool_name: str,
    args: dict,
    customer_slug: str,
    session_id: str = "",
) -> dict | None:
    """Decide whether a tool call may proceed.

    Returns:
        ``None`` to allow the call.
        ``{"action": "block", "message": "Refused: <reason>"}`` to block.

    Block precedence (first match wins):
      1. Tool name is in ``BANNED_TOOLS`` — refused regardless of ceiling.
      2. Resolved ceiling refuses the action class via ``enforce()``. The
         per-session taint (``SESSION_TAINT``, set by the inbound chokepoints
         when untrusted content is ingested) is read by ``session_id`` and
         passed to ``enforce()`` as the taint-gate input.

    Exception safety: any exception in this function is caught at the
    hook boundary; this function may raise internally and the caller's
    try/except in ``__init__.py`` translates raises into a None (allow)
    return so a misbehaving policy module cannot break the agent loop.
    """
    if not tool_name:
        # Defensive: an empty tool name is a malformed pre-hook kwarg, not a
        # genuine refusal. Allow and let downstream surfaces complain.
        return None

    # 1. Banned tools — refuse before policy runs. ``err.reason`` is the
    # categorical code from ``shared.action_classes.BANNED_REASON`` (e.g.
    # ``"banned_tool_pattern_a"``); render the user-visible message from
    # ``_BANNED_REFUSAL_MESSAGE`` keyed by tool name, falling back to the
    # categorical code when an unknown banned tool slips through.
    try:
        classification = classify_tool(tool_name)
    except BannedToolError as err:
        message = _BANNED_REFUSAL_MESSAGE.get(err.tool_name, err.reason)
        return {
            "action": "block",
            "message": f"Refused: {message}",
        }

    # 2. Resolve customer + skill ceilings; take the more restrictive.
    #
    # FAIL CLOSED on resolution failure (issue #12). A raise here — a
    # customer.yaml parse error, a garbled/missing secret, an unexpected
    # None — must NOT silently allow a sensitive action. READ is the one
    # exception: it is always permitted under any non-refused ceiling and
    # carries no external blast radius, so a transient config error must
    # not brick read-only tooling. Every other action class refuses when
    # the ceiling is indeterminate.
    try:
        customer_ceiling = _resolve_customer_ceiling()
        skill_ceiling = _resolve_skill_ceiling(args)
        effective_ceiling = _most_restrictive(customer_ceiling, skill_ceiling)
        action_ceilings = _resolve_action_ceilings(args)
        vertical_floors = _resolve_vertical_floors()

        decision = enforce(
            ceiling=effective_ceiling,
            action=classification.action_class,
            skill_name=_resolve_skill_name(args),
            tool_name=tool_name,
            current_turn_approval=_resolve_current_turn_approval(args),
            action_ceilings=action_ceilings,
            vertical_floors=vertical_floors,
            inbound_trust_class=SESSION_TAINT.trust_class(session_id),
        )
    except Exception:  # noqa: BLE001
        if classification.action_class == ActionClass.READ:
            logger.warning(
                "trust: ceiling resolution failed for READ tool %r; allowing "
                "(read is low-risk and always permitted under non-refused ceilings)",
                tool_name,
                exc_info=True,
            )
            return None
        logger.exception(
            "trust: ceiling resolution failed for sensitive tool %r (action=%s); "
            "FAILING CLOSED — refusing the call",
            tool_name,
            classification.action_class,
        )
        return {
            "action": "block",
            "message": (
                f"Refused: trust-ceiling decision unavailable for this "
                f"{classification.action_class} action; failing closed"
            ),
        }

    # Content-sensitivity floor (ADR 0031). Only an EXTERNAL_SEND the ceiling
    # would ALLOW (i.e. resolved to autonomous send) is subject to the floor —
    # a draft/refuse decision already withholds the send. A money / contract /
    # scope / legal body is downgraded to a draft even under autonomous.
    if decision.allowed and classification.action_class == ActionClass.EXTERNAL_SEND:
        floor_block = _apply_content_floor(tool_name, args)
        if floor_block is not None:
            return floor_block

    if decision.allowed:
        return None

    return {
        "action": "block",
        "message": f"Refused: {decision.reason}",
    }


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
