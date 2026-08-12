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
import os
from pathlib import Path
from typing import Any

from shared import matter_binding, provenance, report_render, spec_stamp
from shared.broker_audit import write_decision
from shared.pending_send import PENDING_SEND
from shared.secrets import get_secret
from shared.spec_status import SPEC_STATUS
from shared.tool_registration import register_wrapped_tool
from shared.workspace_broker import GRANT_ARG, authorize

from . import approval, enforce, outbound, outbound_send, spec_read

logger = logging.getLogger(__name__)

# ss#2258: the out-of-band confirmed-send audit binding that used to live here —
# `_AUDIT_CLIENT`, `_emit_confirm_event`, and their INSERT — is DELETED, not left
# unused. It was best-effort by construction and opened with
# `if _AUDIT_CLIENT is None: return`, so on any seat where the binding failed to
# configure, sends dispatched and rows silently did not. That is precisely the
# shape of the incident this work exists for: four messages, zero rows. Both
# transports are broker verbs now and the broker writes the row itself, before it
# answers, from the process that holds the credential — a writer with no early
# return and no way for the caller to skip it. Re-adding an emission here would
# not add safety; it would double-count in the console reconciler, which is the
# backstop that catches us when everything else is wrong.
#
# The slug survives because the dispatch path below needs it independently.
_AUDIT_CUSTOMER_SLUG: str | None = None

# The msgraph proactive-send tool (ADR 0078). Blocked at the registry since
# ss#2258, so no NEW pending record can carry it; kept here so a record captured
# before that change still dispatches down the Graph path rather than silently
# down the AgentMail one.
_MSGRAPH_SEND_TOOL = "mcp_msgraph_mail_send_message"
_ADAPTER_MSGRAPH = "msgraph"
_ADAPTER_AGENTMAIL = "agentmail"


def _attach_html_body(tool_name: str, args: dict) -> None:
    """Give a report send an html half, rendered from the markdown it already wrote.

    **Call this ONLY after every gate has returned allow.** The ordering is the
    safety argument, not an implementation detail:

    The ceiling, the taint gate, the content floor, and the fabrication scan all
    read the send body — ``enforce._SEND_BODY_ARG_KEYS`` and
    ``outbound._SEND_SCAN_KEYS`` both include ``html``, so an html body IS
    scannable and a fabricated citation could not hide in one. We render after
    those gates anyway, because the html is a PURE presentational transform of
    the ``text`` they just scanned (``report_render`` purity invariant, held by
    ``tests/test_report_render.py``). It therefore introduces no token of content
    any gate has not already evaluated, and rendering before the gates would only
    feed them the same content twice wearing markup.

    If the purity invariant is ever weakened so the renderer can ADD content,
    this call must move ahead of the gates or the argument collapses.

    Idempotent and non-destructive: a model-authored html body is never
    clobbered, and a send with no markdown block structure is left untouched.
    """
    if not outbound._is_gated_send_tool(tool_name):
        return
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    existing = args.get("html")
    if isinstance(existing, str) and existing.strip():
        return  # the composer supplied its own html; it wins
    if not report_render.looks_like_report(text):
        return  # prose reply, not a report — leave the send exactly as it was
    args["html"] = report_render.render_markdown(text)


_PAUSE_CACHE_TTL_S = 2.0
_pause_cache: dict[str, Any] = {"at": 0.0, "hard": False}


def _paused_hard() -> bool:
    """True while the sticky-stop ladder reads HARD_STOP (ss#2003 pause wall).

    Fail-open by design HERE ONLY: this wall is an additional chokepoint on
    top of the ADR 0062 stop surface, and the breaker's own read fails toward
    "unknown". A read failure must not brick every tool call on a healthy
    Machine — the primary stop enforcement (gate 503s, wake guard, job
    assert) stands regardless.
    """
    import time as _time

    now = _time.monotonic()
    if now - _pause_cache["at"] < _PAUSE_CACHE_TTL_S:
        return bool(_pause_cache["hard"])
    hard = False
    try:
        from shared.cost_breaker import read_level

        hard = read_level() == "HARD_STOP"
    except Exception:  # noqa: BLE001 — see docstring
        hard = False
    _pause_cache["at"] = now
    _pause_cache["hard"] = hard
    return hard


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
        # only); resolve to the id THIS THREAD is working under (noted at
        # pre_llm_call / post_tool_call) so the provenance register is consulted
        # under the SAME key reads were recorded under.
        #
        # This is the resolution that matters, and the only one that can be an
        # inference: by post_tool_call core supplies the real id again. So the
        # mode is captured HERE and carried onto the audit row through the
        # trust decision (ss-console #2288) — a row must not present an inferred
        # session as a keyed one, for the same reason it may not present an
        # inferred trust join as a keyed one.
        session_id, session_match = provenance.resolve_session_with_mode(
            kwargs.get("session_id") or ""
        )

        # SEC-36/16: strip any agent-supplied `_current_turn_approval` flag before
        # the ceiling check. No trusted runtime path stamps it (grep: it is read,
        # never written), so a value present here is an agent forgery — removing it
        # prevents the agent from self-approving a DESTRUCTIVE/COMMITMENT action.
        # A genuine current-turn approval must arrive via a human-tied channel, not
        # model-composed tool args.
        args.pop("_current_turn_approval", None)

        # Operator-pause wall (ss#2003). While the sticky-stop ladder is at
        # HARD_STOP — a system trip OR an operator-initiated pause — every
        # tool call refuses, whatever woke the agent. The gate already 503s
        # /mcp/turn + /webhooks/handoff and the wake guard parks vendor
        # webhooks; this wall is the chokepoint that covers the remaining
        # wake paths (Hermes-native cron above all): a paused Machine may
        # wake, but it cannot ACT. Read is cached briefly — the level flips
        # rarely and the read is a per-tool-call sqlite open otherwise.
        if _paused_hard():
            return {
                "action": "block",
                "message": (
                    "The Operator is paused (sticky stop at HARD_STOP). No tools run "
                    "until an authorized person resumes it. Do not retry; end the turn."
                ),
            }

        # Authored-spec read observation (ss ADR 0083 #2084). Runs BEFORE the
        # ceiling check because it is pure observation on a READ-class tool that
        # enforcement always allows, and because the ordering makes it obvious
        # it can never influence a trust decision. It marks ONLY after verifying
        # the file against the ROOT-OWNED manifest, so the agent cannot satisfy
        # its own spec gate by reading something it wrote.
        spec_read.observe_read(tool_name, args, session_id)

        # tool_call_id is the key the audit plugin's post_tool_call looks the
        # decision up under (#2122). It may be absent — core drops session_id on
        # this path (#141) and the same fire sites carry tool_call_id — so the
        # register falls back to the sequential slot and the row declares which
        # way it matched. Nothing here depends on the kwarg being populated.
        ceiling_block = enforce.evaluate_tool_call(
            tool_name,
            args,
            customer_slug,
            session_id=session_id,
            tool_call_id=kwargs.get("tool_call_id") or "",
            session_match=session_match,
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

        # Every gate allowed. A report send gets its html half here — after the
        # scans, mirroring the GRANT_ARG mutation above (pre_tool_call arg
        # mutation reaches the tool; that is the established, live-proven path).
        _attach_html_body(tool_name, args)
        return None
    except Exception as exc:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.exception(
            "hermes-smd-trust: pre_tool_call raised; FAILING CLOSED — blocking "
            "the tool call (safety: an indeterminate trust decision must not "
            "allow a sensitive action; issue #12)"
        )
        # NAME THE ACTUAL FAULT. This is the catch-all around the WHOLE hook —
        # roughly a hundred lines spanning session resolution, the ceiling
        # resolver, the content floor, the outbound scans, the workspace broker
        # and the audit transport. It said "trust-ceiling evaluation failed" for
        # every one of them, and the real exception went only to the log above.
        #
        # That wording has a measured cost, not a theoretical one. On
        # 2026-07-31 it sent an agent to a production-severity bug report
        # against a WORKING security control: the broker had correctly refused a
        # caller that was not the gateway process, and the message named a
        # subsystem that was never involved. Diagnosis, a wrong report relayed
        # to the Captain, and a rebuild cycle, all spent on a message that
        # described the wrong thing (ss#2103, vfy_01KYX1SHS2ZDCNNB6KR3PNSYQY).
        #
        # The exception TYPE and its origin are safe to surface: they name
        # machinery, not payload. The message body is deliberately excluded —
        # it can carry recipient or content fragments from whatever raised.
        origin = "unknown"
        tb = exc.__traceback__
        while tb is not None:  # innermost frame — where it actually raised
            origin = f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}"
            tb = tb.tb_next
        return {
            "action": "block",
            "message": (
                "Refused: the trust hook raised and failed closed — "
                f"{type(exc).__name__} at {origin}. This is an indeterminate "
                "decision blocked for safety, NOT necessarily a ceiling or "
                "entitlement problem; see the seat log for the traceback."
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
        # ss#2167 — matter membership rides the SAME read stream. The outbound
        # matter-identity gate cannot call the connector at send time (this
        # process cannot synchronously drive an MCP server from pre_tool_call),
        # so "who is a party to which matter" has to be captured while the agent
        # reads it. Structured capture, deliberately separate from the
        # provenance register above, which stringifies.
        matter_binding.record_from_read(resolved, result)
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("hermes-smd-trust: post_tool_call provenance record failed", exc_info=True)


# ---------------------------------------------------------------------------
# The broker-mediated send tool (ss#2258)
# ---------------------------------------------------------------------------
#
# The four AgentMail MCP send tools left the menu because the gateway's AgentMail
# key is now inbox-scoped WITHOUT message_send — it would 403, and an advertised
# tool that always fails is worse than an absent one. But removing them without a
# replacement would have SILENTLY BROKEN an authored capability: both live seats
# author `external_send_internal: autonomous`, i.e. the Operator may answer a
# rostered colleague without a human in the loop. Taking that away by deleting a
# tool would be the same class of mistake as the incident itself — a change whose
# real effect is invisible from the diff.
#
# So the capability survives, and only the executor changes. This tool carries the
# same EXTERNAL_SEND action class (shared/action_classes.py), so the SAME ceiling
# governs it: draft_for_review still withholds and routes through the Telegram
# confirm round-trip; autonomous still sends. Underneath, it reaches the broker,
# which fences the recipient against the seat's authored surface and writes the
# audit row itself.
_SEND_TOOL_NAME = "smd_send_message"
_BROKER_SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"
_SEND_TOOL_DESCRIPTION = (
    "Send an email from this Operator's own mailbox. Recipients must be people "
    "this engagement's configuration names; anyone else is refused. Subject to "
    "the authored send posture — an external send may be held for the owner's "
    "approval rather than sent immediately."
)
_SEND_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Recipient email addresses.",
        },
        "cc": {"type": "array", "items": {"type": "string"}},
        "bcc": {"type": "array", "items": {"type": "string"}},
        "subject": {"type": "string"},
        "text": {"type": "string", "description": "Plain-text body."},
        "html": {"type": "string", "description": "Optional HTML body."},
        "reply_to": {"type": "string"},
    },
    "required": ["to", "subject"],
}


def _seat_email_adapter() -> str:
    """The seat's authored Email adapter, re-read live from customer.yaml.

    Which transport a send takes is a property of the ENGAGEMENT, not of the tool
    name — that was true before this tool existed and only became visible once one
    tool served both channels. Reading it here (rather than branching on the tool
    that fired) is also what keeps the confirm-dispatch path and the direct tool
    path from disagreeing about the same seat.

    Defaults to ``agentmail``, matching ``hermes-smd-reply``'s identical read: two
    plugins that disagreed about a seat's transport would be worse than either
    default, and every seat but one authors agentmail today.
    """
    from shared.customer_config import CustomerConfig  # local import (enforce.py idiom)

    try:
        record = CustomerConfig.from_volume().connectors.get("Email")
    except Exception:  # noqa: BLE001 — an unreadable config must not raise into a send
        return _ADAPTER_AGENTMAIL
    if isinstance(record, dict):
        adapter = record.get("adapter")
        if isinstance(adapter, str) and adapter.strip():
            return adapter.strip().lower()
    return _ADAPTER_AGENTMAIL


def _smd_send_message(**kwargs: Any) -> str:
    """Execute a send the gate has already authorized.

    By the time a handler runs, ``pre_tool_call`` has classified the recipients,
    applied the exposure ceiling, run the content and fabrication floors, and
    either allowed this call or blocked it. So there is no authorization decision
    left here — this is transport, and the broker independently re-fences the
    recipient anyway. Two checks in two processes, on purpose.

    ONE tool, either transport. The msgraph connector's own ``send_message`` left
    the menu for the same reason the AgentMail four did, and for a sharper one: on
    a seat whose posture is ``autonomous`` the gate returns allow and the MCP tool
    simply executes, so that path reached Graph directly — no recipient fence, no
    broker row. Leaving it advertised would have meant the fence covered only
    seats that withhold, which is the opposite of who needs it.
    """
    payload = dict(kwargs)
    try:
        if _seat_email_adapter() == _ADAPTER_MSGRAPH:
            message_id = outbound_send.send_via_msgraph(payload)
        else:
            message_id = outbound_send.send_message(payload=payload)
    except outbound_send.OutboundSendError as exc:
        # Returned, not raised: a refused send is information the agent should
        # act on (pick a different recipient, ask the owner), not a tool crash.
        # The broker has already recorded the attempt and the reason.
        return f"Not sent: {exc}"
    return f"Sent (message {message_id})."


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
        block = enforce.evaluate_tool_call(
            rec.tool_name, payload, customer_slug, session_id=session_id
        )
    except Exception:  # noqa: BLE001 — an indeterminate gate must not send
        logger.exception(
            "hermes-smd-trust: out-of-band send gate raised; NOT dispatching (fail-safe)"
        )
        return None
    if block is not None:
        # taint-gate / content-floor withheld the approved send. Not sent; the
        # record was not consumed. Tell the agent why, plainly.
        reason = block.get("message", "withheld") if isinstance(block, dict) else "withheld"
        logger.info(
            "hermes-smd-trust: approved send withheld by gate for %s (%s)", recipients, reason
        )
        return f"[Your approved send to {recipients} was not dispatched: {reason}]"
    # Gate allowed + consumed the approval; `payload` now holds the approved payload.
    # Same post-gate html attach as the tool path. This path needs its own call:
    # the pending record was stored by the gate BEFORE the tool path's attach ran,
    # so a withheld-then-approved report arrives here as markdown-only.
    _attach_html_body(rec.tool_name, payload)
    # Provider dispatch (ADR 0078), keyed on what the SEAT authors — not on which
    # tool fired. It used to key on the tool name, which was right while each
    # channel had its own send tool and became wrong the moment one broker-backed
    # tool served both: a withheld send from `smd_send_message` on an msgraph seat
    # would have dispatched down the AgentMail path. The legacy msgraph tool name
    # is still honoured so a pending record captured before this change dispatches
    # correctly. Each transport fails closed on ITS missing credential; neither
    # cross-falls.
    is_msgraph = rec.tool_name == _MSGRAPH_SEND_TOOL or _seat_email_adapter() == _ADAPTER_MSGRAPH
    try:
        if is_msgraph:
            message_id = outbound_send.send_via_msgraph(payload)
        else:
            # ss#2258: AgentMail transmit is a broker verb now. No key is read
            # here and no inbox is resolved here — the broker pins the seat's own
            # inbox from the customer.yaml it trusts, fences the recipient against
            # the seat's authored counterparty surface, and writes the audit row
            # itself. The previous shape read an account-wide key and resolved the
            # inbox from an account listing in THIS process, which is how a seat
            # could send as (and to) someone it was never authored to touch.
            message_id = outbound_send.send_message(payload=payload)
    except outbound_send.OutboundSendError as exc:
        logger.error("hermes-smd-trust: approved send to %s failed (%s)", recipients, exc)
        # NOTE: NEITHER path emits a row here any more. Both transports are broker
        # verbs now, and the broker writes CONFIRM_SEND_DISPATCHED /
        # CONFIRM_SEND_FAILED itself, before it answers. A second row from this
        # process would make the console reconciler count one attempt twice — and
        # the reconciler is the backstop for this whole control, so double-counting
        # would corrupt the instrument that exists to catch us.
        return f"[Your approved send to {recipients} could not be delivered; it was not sent. You can ask me to retry.]"
    logger.info(
        "hermes-smd-trust: dispatched approved send to %s (source=%s, message=%s)",
        recipients,
        rec.approval_source,
        message_id,
    )
    return f"[Dispatched your approved send to {recipients} (message {message_id}). Do not send it again.]"


def _profiles_root() -> Path:
    """Where the per-persona profile trees live: ``$HERMES_HOME/profiles``.

    Same derivation ``bootstrap/translate.py`` uses when it writes them, so the
    turn-time refresh addresses exactly the files boot stamped. Falls back to
    the image default rather than raising — a wrong path yields zero refreshed
    files and a debug line, never a blocked turn, because delivery is
    best-effort and the send-site gate is the actual guarantee.
    """
    return Path(os.environ.get("HERMES_HOME", "/opt/data")) / "profiles"


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
        # Clear the authored-spec read marks at the start of every turn (ss ADR
        # 0083 #2084). A spec read three turns ago must not certify THIS turn's
        # composition — the spec governs the text being written now, and a
        # sticky mark would certify a draft the spec never touched. Cleared
        # under the resolved id so producer and consumer agree on the key.
        SPEC_STATUS.clear_turn(provenance.resolve_session(session_id))
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("hermes-smd-trust: spec-read turn clear failed", exc_info=True)
    try:
        # Keep the authored-spec POINTER current on a RUNNING Machine.
        #
        # Without this, a client's FIRST spec did not reach the model until a
        # reboot. The root poller installs it within seconds and writes the
        # manifest, but the pointer was rendered only at boot by translate, and
        # the renderer emits nothing when no specs are installed — so a Machine
        # that booted with none carried no pointer at all and was never told the
        # spec existed. That is the gap between "type it and from then on it
        # comes out that way" and "type it, then reboot".
        #
        # HERE, not in the root applier, and the distinction is load-bearing:
        # this hook runs as hermes in the agent's own process, and the profile
        # tree is hermes-owned. A root re-stamp would leave root-owned SKILL.md
        # files the next boot's hermes-run copytree cannot overwrite — the
        # 2026-07-16 outage exactly, where a root-written cron store left the
        # scheduler unable to read its own jobs for eight days while every
        # health signal stayed green.
        #
        # Costs one manifest read on the common path: the refresh compares a
        # fingerprint against the last one it stamped and returns immediately
        # when unchanged.
        spec_stamp.refresh_profile_stamps(_profiles_root())
    except Exception:  # noqa: BLE001 — delivery is best-effort; the GATE is the guarantee
        logger.debug("hermes-smd-trust: spec pointer refresh failed", exc_info=True)
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
    global _AUDIT_CUSTOMER_SLUG
    # The seat's own slug, resolved once at register so the out-of-band dispatch
    # path below does not re-read a secret per turn. No audit client is bound here
    # any more (ss#2258): the broker writes every transmit row itself.
    try:
        _AUDIT_CUSTOMER_SLUG = get_secret("SMD_CUSTOMER_SLUG")
    except KeyError as exc:
        _AUDIT_CUSTOMER_SLUG = None
        logger.info(
            "hermes-smd-trust: SMD_CUSTOMER_SLUG unset (%s); the out-of-band "
            "dispatch path will resolve it per call",
            exc,
        )
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    register_wrapped_tool(
        ctx,
        name=_SEND_TOOL_NAME,
        toolset="email",
        schema=_SEND_TOOL_SCHEMA,
        handler=_smd_send_message,
        requires_env=[_BROKER_SOCKET_ENV],
        description=_SEND_TOOL_DESCRIPTION,
        emoji="",
    )
    logger.info(
        "hermes-smd-trust registered: pre_tool_call + post_tool_call + pre_llm_call + %s",
        _SEND_TOOL_NAME,
    )
