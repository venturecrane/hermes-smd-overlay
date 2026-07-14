"""hermes-smd-trust — content-class trust ceilings.

Attaches to one hook at the pinned Hermes ref (v2026.5.16):

- ``pre_tool_call`` (model_tools.py:778 via ``get_pre_tool_call_block_message``
  at hermes_cli/plugins.py:1396) — blocks tools that exceed the per-customer
  trust ceiling by returning ``{"action": "block", "message": "<reason>"}``.

Per AGENTS.md hard rule #3 the callback is exception-safe: a raise from
the policy module is caught at the hook boundary so a faulty plugin cannot
break the agent loop. Audit observation of refusals happens downstream via
the audit plugin's ``post_tool_call`` hook on the resulting error result;
this plugin does not cross-import the audit plugin.
"""

import logging
from typing import Any

from shared import provenance
from shared.audit_client import audit_client_from_env
from shared.audit_contract import INSERT_SQL as _AUDIT_INSERT_SQL
from shared.audit_contract import agent_event_params
from shared.broker_audit import write_decision
from shared.pending_send import PENDING_SEND
from shared.secrets import get_secret
from shared.workspace_broker import GRANT_ARG, authorize

from . import approval, enforce, outbound, outbound_send

logger = logging.getLogger(__name__)

# Out-of-band confirmed-send audit binding (ADR 0071 #1806 harden). The overlay
# dispatches an approved send itself (the LLM does not reliably re-invoke), so the
# send never passes through a tool call and the audit plugin's post_tool_call
# never sees it — this plugin emits the row directly, mirroring hermes-smd-reply's
# out-of-band relay audit. Best-effort: audit is observability, never a gate.
_AUDIT_CLIENT: Any = None
_AUDIT_CUSTOMER_SLUG: str | None = None


def on_pre_tool_call(**kwargs: Any) -> dict | None:
    """Block a tool call that exceeds the per-customer trust ceiling.

    Returns ``{"action": "block", "message": "<reason>"}`` to refuse, or
    ``None`` to allow the call.

    FAIL CLOSED (issue #12): if the policy path raises unexpectedly, this
    hook returns a block directive rather than ``None``. Safety must not
    degrade to "allow" on error — a transient or config-induced fault in
    the trust path must never silently let a sensitive action through on
    the live per-customer Machine. ``evaluate_tool_call`` already handles
    its own resolution failures (allowing low-risk READs, refusing
    sensitive actions); this handler is the backstop for anything it
    cannot catch. The pre_tool_call helper's contract
    (hermes_cli/plugins.py:1428-1437) honors the block-directive shape.

    TWO evaluations run in this one callback (ADR 0028). First the
    trust-ceiling check (``evaluate_tool_call``). If — and ONLY if — that
    allows the call AND the tool is a body-bearing draft-creating tool, a
    SECOND evaluation runs: the outbound provenance gate
    (``outbound.check_outbound_draft``) scans the draft body for banned
    fabrication markers / fabricated citations and blocks before the draft
    tool executes. ``pre_tool_call`` is the only hook that can block via
    return value, and send tools are permanently banned by the ceiling
    layer, so "drafted OR sent" reduces to "drafted" here. The gate is a
    second check in the same callback, not a new plugin or hook.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, task_id, session_id, tool_call_id
    """
    try:
        tool_name = kwargs.get("tool_name") or ""
        raw_args = kwargs.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        customer_slug = kwargs.get("customer_slug")
        if not isinstance(customer_slug, str) or not customer_slug:
            # Fall back to the env var the Machine boots with. The slug is
            # not load-bearing for the policy decision itself (the customer
            # ceiling resolves from customer.yaml / env), but downstream
            # audit observation expects it.
            try:
                customer_slug = get_secret("SMD_CUSTOMER_SLUG")
            except KeyError:
                customer_slug = ""

        # Overlay #141: core's pre_tool_call fire sites drop session_id (task_id
        # only); resolve to the last real id seen (noted at pre_llm_call /
        # post_tool_call) so the provenance register is consulted under the
        # SAME key reads were recorded under.
        session_id = provenance.resolve_session(kwargs.get("session_id") or "")

        # SEC-36/16: strip any agent-supplied `_current_turn_approval` flag before
        # the ceiling check. No trusted runtime path stamps it (grep: it is read,
        # never written), so a value present here is an agent forgery — removing it
        # prevents the agent from self-approving a DESTRUCTIVE/COMMITMENT action.
        # A genuine current-turn approval must arrive via a human-tied channel, not
        # model-composed tool args.
        args.pop("_current_turn_approval", None)

        ceiling_block = enforce.evaluate_tool_call(
            tool_name, args, customer_slug, session_id=session_id
        )
        if ceiling_block is not None:
            # The trust ceiling already refuses this call; no need to scan a
            # draft body that will never be written.
            return ceiling_block

        # Ceiling allowed the call. Run the outbound provenance gate as a
        # SECOND evaluation — it no-ops for non-draft tools and blocks a draft
        # whose body carries a banned fabrication marker / fabricated citation.
        outbound_block = outbound.check_outbound_draft(
            tool_name=tool_name,
            args=args,
            session_id=session_id,
            tool_call_id=kwargs.get("tool_call_id") or "",
        )
        if outbound_block is not None:
            return outbound_block

        # EFF-01 (ADR 0028): an autonomous EXTERNAL_SEND delivers content with no
        # human review — scan its body for fabrication too (the draft gate above
        # only covers INTERNAL_WRITE drafts).
        send_block = outbound.check_outbound_send(
            tool_name=tool_name,
            args=args,
            session_id=session_id,
            tool_call_id=kwargs.get("tool_call_id") or "",
        )
        if send_block is not None:
            return send_block

        if tool_name.startswith("workspace_"):
            broker_payload = {key: value for key, value in args.items() if key != GRANT_ARG}
            authorization = authorize(
                tool_name,
                broker_payload,
                customer_slug=customer_slug,
                session_id=session_id,
                tool_call_id=kwargs.get("tool_call_id") or "",
            )
            write_decision(
                operation=tool_name,
                payload_digest=str(authorization["payload_digest"]),
                session_id=session_id,
                tool_call_id=kwargs.get("tool_call_id") or "",
            )
            args[GRANT_ARG] = authorization["grant"]
        return None
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.exception(
            "hermes-smd-trust: pre_tool_call raised; FAILING CLOSED — blocking "
            "the tool call (safety: an indeterminate trust decision must not "
            "allow a sensitive action; issue #12)"
        )
        return {
            "action": "block",
            "message": (
                "Refused: trust-ceiling evaluation failed; failing closed "
                "(indeterminate decision blocked for safety)"
            ),
        }


def on_post_tool_call(**kwargs: Any) -> None:
    """Populate the per-session identifier provenance register from READ-tool
    results (A1).

    For a read-class tool, the identifiers in its result are things the agent
    actually READ — the outbound gate later checks a draft's identifiers against
    this register (report-only) to surface any the agent composed without
    reading. Exception-safe + best-effort: provenance recording must never raise
    out of a hook or perturb the tool path.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, result, task_id, session_id, tool_call_id
    """
    try:
        tool_name = kwargs.get("tool_name") or ""
        if not tool_name:
            return
        sid = kwargs.get("session_id") or ""
        provenance.note_session(sid)  # post_tool_call carries the REAL id (#141)
        resolved = provenance.resolve_session(sid)

        # Record a created/updated draft's recipients under the RESOLVED session id
        # — the same key the pre_tool_call send-gate looks up — so a later
        # send_draft resolves its recipient. Best-effort; no-ops for non-draft
        # tools. Must run BEFORE the READ-only provenance gate below, since
        # create_draft/update_draft are INTERNAL_WRITE.
        try:
            from shared.outbound_recipient import record_draft_from_post_tool_call

            record_draft_from_post_tool_call(
                tool_name, kwargs.get("args"), kwargs.get("result"), resolved
            )
        except Exception:  # noqa: BLE001
            logger.debug("hermes-smd-trust: draft-recipient record failed", exc_info=True)

        try:
            classification = enforce.classify_tool(tool_name)
        except enforce.BannedToolError:
            return  # a banned tool never executes; never record from one
        if classification.action_class is not enforce.ActionClass.READ:
            return  # only reads establish provenance
        result = kwargs.get("result")
        if result is None:
            return
        provenance.record_read(
            resolved,
            result if isinstance(result, str) else str(result),
        )
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("hermes-smd-trust: post_tool_call provenance record failed", exc_info=True)


def _emit_confirm_event(action_type: str, metadata: dict) -> None:
    """Write one confirmed-send audit row directly (ADR 0071 #1806 harden).

    Mirrors hermes-smd-reply's out-of-band relay audit: shares the
    ``shared.audit_contract`` row shape with the audit plugin so they never
    desync. Metadata carries approval source + recipients + message id + reason
    ONLY — never the send body. Best-effort; a failed emission is logged and
    swallowed (the send decision already happened)."""
    if _AUDIT_CLIENT is None or _AUDIT_CUSTOMER_SLUG is None:
        return
    try:
        params = agent_event_params(
            action_type=action_type,
            metadata={"customer": _AUDIT_CUSTOMER_SLUG, "confirm_channel": True, **metadata},
        )
        _AUDIT_CLIENT.execute(_AUDIT_INSERT_SQL, *params)
    except Exception as exc:  # noqa: BLE001 — audit must never break the hook
        logger.warning("hermes-smd-trust: %s audit emission failed (%s)", action_type, exc)


def _dispatch_approved_send(session_id: str, customer_slug: str) -> str | None:
    """Execute an approved confirm send OUT OF BAND (ADR 0071 #1806 harden).

    Called from pre_llm_call the moment a trusted approval is captured. The LLM
    does not reliably re-invoke the send tool on "yes" (it sometimes reasons /
    investigates instead); the overlay is deterministic, so it dispatches the send
    itself. Returns a short context string to inject (so the agent knows the send
    is done / why not), or ``None`` when there is nothing to dispatch.

    SAFETY: this does NOT bypass the gate. The approved payload is re-authorized
    through the SAME ``evaluate_tool_call`` (taint-gate, content-floor, fabrication
    scan, confirm-approval) — which also CONSUMES the pending record — and the REST
    send fires only when the gate returns allow. So an approved-but-tainted or
    approved-but-money/legal send still withholds/drafts exactly as on the tool
    path. The consume also means a later agent re-invoke finds no approval and
    withholds, so there is no double-send.
    """
    rec = PENDING_SEND.peek()
    if rec is None or not rec.approved:
        return None
    recipients = ", ".join(sorted(rec.recipients)) or "(unresolved)"
    # Re-authorize through the gate on a payload COPY (the gate overwrites it with
    # the stored payload and consumes the approval). Fail-safe: any gate error →
    # do not send.
    payload = dict(rec.args)
    try:
        block = enforce.evaluate_tool_call(rec.tool_name, payload, customer_slug, session_id=session_id)
    except Exception:  # noqa: BLE001 — an indeterminate gate must not send
        logger.exception("hermes-smd-trust: out-of-band send gate raised; NOT dispatching (fail-safe)")
        return None
    if block is not None:
        # taint-gate / content-floor withheld the approved send. Not sent; the
        # record was not consumed. Tell the agent why, plainly.
        reason = block.get("message", "withheld") if isinstance(block, dict) else "withheld"
        logger.info("hermes-smd-trust: approved send withheld by gate for %s (%s)", recipients, reason)
        return f"[Your approved send to {recipients} was not dispatched: {reason}]"
    # Gate allowed + consumed the approval; `payload` now holds the approved payload.
    try:
        api_key = get_secret("AGENTMAIL_API_KEY")
    except KeyError:
        logger.error("hermes-smd-trust: AGENTMAIL_API_KEY unset; cannot dispatch approved send")
        return None
    try:
        inbox_id = outbound_send.resolve_inbox_id(api_key)
        message_id = outbound_send.send_message(api_key=api_key, inbox_id=inbox_id, payload=payload)
    except outbound_send.AgentMailSendError as exc:
        logger.error("hermes-smd-trust: approved send to %s failed (%s)", recipients, exc)
        _emit_confirm_event(
            "CONFIRM_SEND_FAILED",
            {"recipients": sorted(rec.recipients), "source": rec.approval_source, "reason": str(exc)},
        )
        return f"[Your approved send to {recipients} could not be delivered; it was not sent. You can ask me to retry.]"
    logger.info(
        "hermes-smd-trust: dispatched approved send to %s (source=%s, message=%s)",
        recipients,
        rec.approval_source,
        message_id,
    )
    _emit_confirm_event(
        "CONFIRM_SEND_DISPATCHED",
        {"recipients": sorted(rec.recipients), "source": rec.approval_source, "message_id": message_id},
    )
    return f"[Dispatched your approved send to {recipients} (message {message_id}). Do not send it again.]"


def on_pre_llm_call(**kwargs: Any) -> dict | None:
    """Note the turn's REAL session id, and capture a current-turn send approval.

    Two observational duties, both exception-safe (always return None):

    1. Note the REAL session id before any tool pre-hook fires (#141). pre_llm_call
       is the earliest hook core passes session_id to; noting it here closes the
       resolver's first-tool-call gap and refreshes the id at every turn.
    2. Confirm-approval capture (ADR 0071 #1806). This is the trusted seam where an
       allowlisted owner's Telegram DM ("yes send it") arrives as native principal
       input. ``approval.maybe_capture_approval`` marks the single pending send
       approved iff platform is Telegram, ``sender_id`` is allowlisted, and the whole
       message is a bare affirmative — so the send's re-invocation (pre_tool_call,
       later this turn) can release exactly the withheld payload. The agent cannot
       forge this: SEC-36 strips agent-supplied approval, and an agent/sub-agent
       message never presents an allowlisted telegram sender_id.
    """
    session_id = kwargs.get("session_id") or ""
    try:
        provenance.note_session(session_id)
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("hermes-smd-trust: pre_llm_call session note failed", exc_info=True)
    try:
        source = approval.maybe_capture_approval(
            kwargs.get("platform"),
            kwargs.get("sender_id"),
            kwargs.get("user_message"),
        )
        if source is not None:
            # Approval captured — dispatch the send OUT OF BAND (the LLM does not
            # reliably re-invoke). The gate re-authorizes + consumes; on success we
            # inject a note so the agent knows it is done and does not re-send.
            customer_slug = _AUDIT_CUSTOMER_SLUG or ""
            if not customer_slug:
                try:
                    customer_slug = get_secret("SMD_CUSTOMER_SLUG")
                except KeyError:
                    customer_slug = ""
            context = _dispatch_approved_send(session_id, customer_slug)
            if context:
                return {"context": context}
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("hermes-smd-trust: approval capture/dispatch failed", exc_info=True)
    return None


def register(ctx) -> None:
    """Plugin entry point. Wires pre_tool_call (ceiling + outbound gate),
    post_tool_call (provenance recording for the A1 identifier gate), and
    pre_llm_call (session-id note + confirm-approval capture + out-of-band
    dispatch of the approved send)."""
    global _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG
    # Confirmed-send audit binding — best-effort (observability, not a gate). Mirror
    # hermes-smd-reply: the broker-aware factory returns a tamper-resistant client
    # when the broker is configured, else a D1 client; same .execute(sql, *params).
    try:
        _AUDIT_CUSTOMER_SLUG = get_secret("SMD_CUSTOMER_SLUG")
        _AUDIT_CLIENT = audit_client_from_env(customer_slug=_AUDIT_CUSTOMER_SLUG)
    except KeyError as exc:
        _AUDIT_CUSTOMER_SLUG = None
        _AUDIT_CLIENT = None
        logger.info(
            "hermes-smd-trust: confirmed-send audit binding unconfigured (%s); "
            "out-of-band sends will dispatch without emitting audit rows",
            exc,
        )
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    logger.info("hermes-smd-trust registered: pre_tool_call + post_tool_call + pre_llm_call")
