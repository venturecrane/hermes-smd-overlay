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

import json
import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any

from shared import (
    act_broker,
    cron_attribution,
    matter_binding,
    matter_gate,
    pre_run_handoff,
    prerendered_dispatch,
    provenance,
    read_volume,
    rendered_body_gate,
    report_render,
    spec_stamp,
)
from shared.broker_audit import write_decision
from shared.pending_acts import PENDING_ACTS, tool_call_failed
from shared.pending_send import PENDING_SEND
from shared.secrets import get_secret
from shared.send_dispatch import DispatchResult, set_sender
from shared.spec_gate import TEMPLATED_BODY_ARG
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
    # The text/plain half of the same fix (WS-RENDER): a reader whose client
    # shows the text part must not see raw markdown. render_plain is strictly
    # MARKER-SUBTRACTIVE (purity test), so the safety argument above covers it
    # identically: it introduces no token the gates did not already scan.
    args["text"] = report_render.render_plain(text)


# ---------------------------------------------------------------------------
# The .smd fence (ss-console#2547)
#
# ``$HERMES_HOME/.smd/`` is where the overlay's own processes keep state that
# other overlay processes TRUST: the audit-write-failure tally, the boot
# sentinels the heartbeat reports from, the routine-change spool, and — the
# reason this fence exists — the pre-run handoff, which tells the identifier
# gate what a routine's script read before the session began.
#
# A handoff the agent can author is not a handoff. It would let a turn write
# down any date it liked and then cite it as read, which is the exact inversion
# of the control: the gate would go on refusing composed identifiers, and pass
# the ones the model had thought to certify first. The same argument covers
# every other file in there, and it is not hypothetical — during the A&P
# rehearsal the identifier gate was defeated by nothing more exotic than the
# seat reading its own skill text (ss-console#2511).
#
# So no tool writes here, by any route: not the file tools, not a shell command,
# not a python snippet inside execute_code, not a subagent. READS are untouched —
# reading .smd is how an operator debugs a seat, and a read certifies nothing
# (``provenance.record_seat_text`` records exactly that kind of read as
# seat-sourced, i.e. as NOT a record).
#
# The refusal is audited by the SAME path every ceiling refusal is: a block
# returned from ``pre_tool_call`` surfaces as an error result, and the audit
# plugin writes the TOOL_CALL_COMPLETED row for it on the way out (see this
# module's docstring). No new action type, no second emitter.
# ---------------------------------------------------------------------------

#: Reading is free; anything that could put bytes on the volume is fenced. The
#: set is derived from the registry rather than named tool by tool, so a write
#: tool added later is fenced the day it lands instead of the day someone
#: remembers this list.
_FENCED_ACTION_CLASSES = frozenset(
    {
        enforce.ActionClass.INTERNAL_WRITE,
        enforce.ActionClass.CODE_EXECUTION,
        enforce.ActionClass.DESTRUCTIVE,
    }
)

#: The distinctive marker. Matched as a path SEGMENT so a relative target
#: (``.smd/pre_run/x.json``) is caught alongside an absolute one, and so a
#: command line or a code blob that mentions the directory is caught at all —
#: a path inside ``execute_code`` is a string in a program, not an argument that
#: could be resolved.
_SMD_FENCE_SEGMENT = ".smd/"

#: Bounds on the arg walk. A fence that can be made expensive is a way to slow
#: every tool call on a 1-vCPU seat.
_FENCE_MAX_STRINGS = 200
_FENCE_MAX_DEPTH = 6


def _fence_strings(value: Any, depth: int = 0):
    """Yield the strings in a tool's args, bounded in depth and count."""
    if depth > _FENCE_MAX_DEPTH:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _fence_strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _fence_strings(item, depth + 1)


def _targets_smd_dir(text: str) -> bool:
    """True iff ``text`` names something inside ``$HERMES_HOME/.smd/``."""
    if _SMD_FENCE_SEGMENT in text:
        return True
    fence_root = str(Path(os.environ.get("HERMES_HOME", "/opt/data")) / ".smd")
    return fence_root in text


def _smd_dir_fence(tool_name: str, args: dict) -> dict | None:
    """Block directive when a write-class call names the seat's own state dir.

    Fail-OPEN on an unclassifiable tool, deliberately: this fence is a second
    wall behind the ceiling, and a classification failure already refuses the
    call one layer up (``enforce.evaluate_tool_call`` fails closed). Turning a
    classification error into a fence refusal here would misname the fault, and
    a refusal that names the wrong subsystem costs a diagnosis (ss#2103).
    """
    try:
        classification = enforce.classify_tool(tool_name)
    except Exception:  # noqa: BLE001 — see docstring; the ceiling owns this fault
        return None
    if classification.action_class not in _FENCED_ACTION_CLASSES:
        return None
    for index, text in enumerate(_fence_strings(args)):
        if index >= _FENCE_MAX_STRINGS:
            break
        if _targets_smd_dir(text):
            logger.warning(
                "hermes-smd-trust: %s targets the .smd state directory; refusing (ss#2547)",
                tool_name,
            )
            return {
                "action": "block",
                "message": (
                    "Refused: .smd is this seat's own health and provenance state, "
                    "written only by the processes that own it. Nothing you run may "
                    "write there — not a file tool, not a shell command, not code. "
                    "If you are trying to establish where a value came from, read it "
                    "from the firm's own records; a note you write to yourself is not "
                    "a source. Do not retry."
                ),
            }
    return None


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

        # The .smd fence (ss#2547). Ahead of the ceiling because it is not a
        # question about this seat's entitlements: no exposure any customer
        # could author permits a turn to write the seat's own provenance, so
        # there is nothing for the ceiling resolver to say about it.
        fence_block = _smd_dir_fence(tool_name, args)
        if fence_block is not None:
            return fence_block

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

        # WS-RENDER: the in-turn rendered-body check, POST-ceiling. A cron
        # session whose consumed dispatch envelope declared enforced in-turn
        # templates may send only a body that IS one of those templates with
        # its slots filled — the model selects, it does not write. Binds only
        # gated send tools and only sessions with an enforcing declaration;
        # everything else passes untouched (the module fails open internally).
        if outbound._is_gated_send_tool(tool_name):
            body_block = rendered_body_gate.check_body(
                session_id,
                args.get("text") or "",
                prerendered_dispatch.in_turn_templates(session_id),
            )
            if body_block is not None:
                return body_block

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


def _act_reference(result: Any) -> str:
    """The vendor's id for what a committed act created, or ``""``.

    Best-effort by design. The reference is what makes the ledger row point at a
    real record, and not finding one is worth a row that says so; it is not worth
    failing a commitment that already happened.
    """
    if isinstance(result, dict):
        payload: Any = result
    elif isinstance(result, str) and result.strip():
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return ""
    else:
        return ""
    while isinstance(payload, dict) and "result" in payload and "id" not in payload:
        payload = payload["result"]
    if not isinstance(payload, dict):
        return ""
    for key in ("id", "matter_id", "matterId", "ref"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _commit_confirmed_act(tool_name: str, session_id: str, kwargs: dict[str, Any]) -> None:
    """Commit the broker row for an act that just executed, if one did.

    Two decisions live here, and both are one-directional:

    * a call that POSITIVELY reported failure commits NOTHING. A committed row
      says the firm's system of record holds a thing; committing one for a call
      that errored would put that claim in the ledger with an administrator's
      name attached to it;
    * the in-process record is dropped either way, so a retry has to be proposed
      and confirmed again. The broker row simply stays open until its own TTL.

    Exception-safe and observational: a commit that cannot be written costs the
    ledger a row, never the turn.
    """
    try:
        if not tool_name or not act_broker.is_act_tool(tool_name):
            return
        record = PENDING_ACTS.finish(session_id, tool_name)
        if record is None:
            return
        if tool_call_failed(kwargs.get("status"), kwargs.get("error_type")):
            logger.warning(
                "hermes-smd-trust: %s failed; act %s NOT committed (the broker row "
                "stays open until its TTL)",
                tool_name,
                record.proposal_id,
            )
            return
        act_broker.commit(
            proposal_id=record.proposal_id,
            tool=record.tool,
            payload=record.payload,
            confirmed_by=record.confirmed_by,
            confirmed_message_id=record.confirmed_message_id,
            ok=True,
            ref=_act_reference(kwargs.get("result")),
        )
        logger.info(
            "hermes-smd-trust: act %s committed (confirmed by %s)",
            record.proposal_id,
            record.confirmed_by,
        )
    except Exception:  # noqa: BLE001 - a missed commit costs a ledger row, not the turn
        logger.warning("hermes-smd-trust: act commit failed", exc_info=True)


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

        # Close a confirmed commitment's broker row against what actually
        # happened (ss-console operator-own-matter). Runs BEFORE the READ-class
        # early return below, because a commitment is by definition not a read.
        _commit_confirmed_act(tool_name, resolved, kwargs)

        try:
            classification = enforce.classify_tool(tool_name)
        except enforce.BannedToolError:
            return  # a banned tool never executes; never record from one
        if classification.action_class is not enforce.ActionClass.READ:
            return  # only reads establish provenance
        result = kwargs.get("result")
        if result is None:
            return
        # ...and only reads that reach the TENANT's records (ss-console#2511).
        # The action class is necessary but not sufficient: ``read_file`` is
        # READ-class, so before this the seat's own skill text was a source of
        # record, and a sentinel case number named in a skill verified against
        # the register that skill had just seeded. See the rule and the incident
        # in shared/provenance.py.
        #
        # The matter-membership capture below is deliberately NOT gated on this
        # predicate. It is a different register with a different failure mode,
        # it keys on structured connector record shapes rather than on free
        # text, and narrowing it belongs with the matter gate's own review — not
        # carried along by a change to the identifier register.
        text = result if isinstance(result, str) else str(result)
        if provenance.seeds_provenance(tool_name):
            provenance.record_read(resolved, text)
        else:
            # NOT discarded. A read the seat performed on its own text is
            # evidence too — of the opposite thing. Recording it is what lets
            # the outbound gate tell "nothing was read this session" apart from
            # "this number came out of your own instructions", and refuse the
            # second even on a turn where the first would be carved out. See the
            # negative-register note in shared/provenance.py.
            provenance.record_seat_text(resolved, text)
        # ss#2167 — matter membership rides the SAME read stream. The outbound
        # matter-identity gate cannot call the connector at send time (this
        # process cannot synchronously drive an MCP server from pre_tool_call),
        # so "who is a party to which matter" has to be captured while the agent
        # reads it. Structured capture, deliberately separate from the
        # provenance register above, which stringifies.
        # tool_name + args are needed for the content-read set (ss#2167 mixing):
        # which matter a memo listing or a document read was performed AGAINST
        # lives in the args, and the result shape alone cannot say which tool
        # produced it. Party capture below is unchanged.
        matter_binding.record_from_read(
            resolved, result, tool_name=tool_name, args=kwargs.get("args")
        )
        # Read-volume gate (agreement §2.8): two mechanical observations ride
        # the same read stream — a read_file of the gated skill's SKILL.md
        # marks the session as a review (the spine path), and a counted
        # document read accumulates the review's pages from the connector's
        # own envelope (pageCount / total_chars). Refused reads never reach
        # post_tool_call, so they never accumulate.
        read_volume.note_read(resolved, tool_name, kwargs.get("args"), result)
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


#: Returned by :func:`_authored_email_adapter` when customer.yaml could not be
#: read at all. Deliberately distinct from ``None`` ("read fine, and this seat
#: authors no Email connector"): the first is a degraded seat, the second is an
#: authoring fact. Only the second is safe to refuse on — see
#: :func:`_smd_send_message`.
_ADAPTER_UNREADABLE = "__unreadable__"


def _authored_email_adapter() -> str | None:
    """The Email adapter this seat AUTHORS, without substituting a default.

    Returns the authored adapter string; ``None`` when the config was read and
    names no Email connector; :data:`_ADAPTER_UNREADABLE` when the config could
    not be read at all.

    Split out from :func:`_seat_email_adapter` so a caller that needs to tell
    "authored agentmail" from "authored nothing" can, which the defaulting
    version structurally cannot.
    """
    from shared.customer_config import CustomerConfig  # local import (enforce.py idiom)

    try:
        record = CustomerConfig.from_volume().connectors.get("Email")
    except Exception:  # noqa: BLE001 — an unreadable config must not raise into a send
        return _ADAPTER_UNREADABLE
    if isinstance(record, dict):
        adapter = record.get("adapter")
        if isinstance(adapter, str) and adapter.strip():
            return adapter.strip().lower()
    return None


def _seat_email_adapter() -> str:
    """The seat's authored Email adapter, re-read live from customer.yaml.

    Which transport a send takes is a property of the ENGAGEMENT, not of the tool
    name — that was true before this tool existed and only became visible once one
    tool served both channels. Reading it here (rather than branching on the tool
    that fired) is also what keeps the confirm-dispatch path and the direct tool
    path from disagreeing about the same seat.

    Defaults to ``agentmail``, matching ``hermes-smd-reply``'s identical read: two
    plugins that disagreed about a seat's transport would be worse than either
    default, and every seat but one authors agentmail today. That default is
    unchanged and deliberately shared; the send tool checks
    :func:`_authored_email_adapter` separately rather than tightening it here.
    """
    authored = _authored_email_adapter()
    if authored is None or authored == _ADAPTER_UNREADABLE:
        return _ADAPTER_AGENTMAIL
    return authored


def _resolved_session(kwargs: dict[str, Any]) -> str:
    """The session id the per-session registers were written under.

    Core drops ``session_id`` at some tool fire sites and passes only ``task_id``
    (overlay #141); ``provenance.resolve_session`` is the single place that
    reconciles them, and it is the key ``matter_binding`` was recorded under
    (``on_post_tool_call`` passes the resolved id). Consulting the raw kwargs id
    instead would look up an empty register and report a send with no matter on a
    turn that read one. Never raises — an unresolvable session degrades to
    whatever kwargs carried, which is what the row then says.
    """
    raw = str(kwargs.get("session_id") or "")
    try:
        return provenance.resolve_session(raw)
    except Exception:  # noqa: BLE001 — an audit enrichment must not break a send
        logger.debug("hermes-smd-trust: send session resolution failed", exc_info=True)
        return raw


def _send_cited_matters(session_id: str, payload: dict[str, Any]) -> set[str]:
    """The matters a send's body cites, extracted WITH the membership's
    known-number aliases (ss#2458 pair, overlay#335): a firm whose matter
    numbers are bare digit runs is findable only through what this session
    actually read, so the matter_ref the CONFIRM row carries stops going
    blank on exactly those firms. Never raises — matter_ref is attribution,
    and an enrichment fault must not break a send."""
    body = matter_gate.body_from_args(payload)
    try:
        known = matter_binding.membership_for(session_id).known_numbers()
    except Exception:  # noqa: BLE001 — enrichment only; the plain extraction stands
        known = frozenset()
    try:
        return matter_gate.cited_matters(body, known)
    except Exception:  # noqa: BLE001
        return matter_gate.cited_matters(body)


def _smd_send_message(args: dict[str, Any], **kwargs: Any) -> str:
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

    ``args`` IS POSITIONAL, and the payload is read from it. Hermes dispatches
    ``entry.handler(args, **kwargs)``; this handler originally declared
    ``(**kwargs)`` and so raised ``TypeError`` on every call, which is Sentry
    SMD-OPERATOR-1B — the seat's only send tool, dead from the day it shipped.
    Reading ``args`` rather than ``kwargs`` is also what makes the gate's own
    mutations visible: ``on_pre_tool_call`` writes ``args[GRANT_ARG]`` and runs
    ``_attach_html_body`` on this same dict before dispatch, so the html half of
    a send and the grant marker arrive here only through the positional object.
    """
    authored = _authored_email_adapter()
    if authored is None:
        # The config was READ and names no Email connector. Refuse plainly rather
        # than falling through to the shared `agentmail` default, which on such a
        # seat means a broker call for a mailbox and credential that were never
        # provisioned — a soft "Not sent" the agent reads as a mild failure. The
        # crash this tool used to raise was at least loud; a fix must not trade
        # loud-and-broken for quiet-and-broken (ashton-price and smd both author
        # no Email connector today). `_ADAPTER_UNREADABLE` deliberately does NOT
        # refuse: a transient config read fault on a properly authored seat must
        # not start declining sends.
        return (
            "Not sent: this engagement authors no Email connector, so there is no "
            "mailbox to send from. Report this rather than retrying."
        )

    payload = dict(args)
    # ss-console#2497 — the two facts the broker's CONFIRM_SEND_* row cannot
    # learn on its own. The session is resolved through ``provenance`` rather
    # than read raw from kwargs because core drops ``session_id`` at some tool
    # fire sites (overlay #141), and the matter registers this consults are keyed
    # by the resolver's answer: looking them up under any other key finds an
    # empty set and reports a send with no matter on a turn that had one.
    send_session_id = _resolved_session(kwargs)
    send_matter_ref = matter_gate.matter_ref_for(
        send_session_id, _send_cited_matters(send_session_id, payload)
    )
    try:
        if _seat_email_adapter() == _ADAPTER_MSGRAPH:
            message_id = outbound_send.send_via_msgraph(
                payload, session_id=send_session_id, matter_ref=send_matter_ref
            )
        else:
            message_id = outbound_send.send_message(
                payload=payload, session_id=send_session_id, matter_ref=send_matter_ref
            )
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
    # ss-console#2497. The broker writes CONFIRM_SEND_DISPATCHED / _FAILED and is
    # the only writer of them, so these two facts have to travel with the request
    # or the row cannot carry them at all. The matter is resolved from what this
    # session READ and from the identifiers in the approved body, never declared
    # by the model — the same rule the matter gate is built on. Both resolve to
    # nothing on an ambiguous turn, and nothing is what the row then says.
    approved_session = _resolved_session({"session_id": session_id})
    send_matter_ref = matter_gate.matter_ref_for(
        approved_session, _send_cited_matters(approved_session, payload)
    )
    try:
        if is_msgraph:
            message_id = outbound_send.send_via_msgraph(
                payload, session_id=session_id, matter_ref=send_matter_ref
            )
        else:
            # ss#2258: AgentMail transmit is a broker verb now. No key is read
            # here and no inbox is resolved here — the broker pins the seat's own
            # inbox from the customer.yaml it trusts, fences the recipient against
            # the seat's authored counterparty surface, and writes the audit row
            # itself. The previous shape read an account-wide key and resolved the
            # inbox from an account listing in THIS process, which is how a seat
            # could send as (and to) someone it was never authored to touch.
            message_id = outbound_send.send_message(
                payload=payload, session_id=session_id, matter_ref=send_matter_ref
            )
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


def _dispatch_internal_message(
    *,
    to: list[str],
    subject: str,
    text: str,
    session_id: str = "",
    cc: list[str] | None = None,
    templated: bool = True,
    audit_extra: dict[str, str] | None = None,
) -> DispatchResult:
    """Send one seat-authored message OUT OF TURN, through the full gate.

    Published to :mod:`shared.send_dispatch` at register so the establishment
    plugin and its sweeper can reach it; they cannot import this package (see
    that module's header for why). Same shape and the same safety argument as
    :func:`_dispatch_approved_send`, which is the precedent: the overlay is
    deterministic where the model is not, so when the overlay knows a specific
    message must go, it sends it rather than asking the model to.

    IT DOES NOT BYPASS ANYTHING. The payload is authorized through the SAME
    ``evaluate_tool_call`` a model's own ``smd_send_message`` goes through, on
    the SAME session — exposure ceiling, taint gate, content floor — and then
    through the SAME ``outbound.check_*`` scans (fabrication markers, the
    identifier-provenance gate) the pre_tool_call hook runs, wired here because
    an out-of-turn send never crosses that hook. A tainted turn refuses this
    send exactly as it refuses a composed one, and the caller is required to
    say so rather than claim the notification went.

    ``templated`` says the body is a FIXED template from this repo. It is passed
    down to the spec gate and skips exactly one branch there, the one that asks
    whether the MODEL consulted the firm's voice spec, which is a meaningless
    question about bytes the model did not write. Every format assertion still
    runs. See ``shared.spec_gate.check_spec_gate``.

    ``audit_extra`` (WS-RENDER) is the caller's contribution to the broker's
    CONFIRM row (``routing_leg``, ``body_variant``); this function ALWAYS adds
    ``rendered_body_sha256`` — the canonical hash of the text the gate
    ALLOWED, computed BEFORE ``_attach_html_body`` mutates the payload, so the
    console's wake<->confirm hash join compares the same bytes the pre_run
    stamped, not the down-rendered plain half.
    """
    recipients = tuple(a for a in (to or ()) if isinstance(a, str) and a.strip())
    if not recipients:
        return DispatchResult(sent=False, reason="no recipient")
    payload: dict[str, Any] = {
        "to": list(recipients),
        "subject": subject,
        "text": text,
    }
    if cc:
        payload["cc"] = [a for a in cc if isinstance(a, str) and a.strip()]
    if templated:
        # Read by enforce -> spec_gate. A key rather than a global because two
        # of these can be in flight in one process (the sweeper thread and a
        # turn), and a global would let one turn's posture leak into another's.
        payload[TEMPLATED_BODY_ARG] = True
    customer_slug = _AUDIT_CUSTOMER_SLUG or ""
    if not customer_slug:
        try:
            customer_slug = get_secret("SMD_CUSTOMER_SLUG")
        except KeyError:
            customer_slug = ""
    try:
        block = enforce.evaluate_tool_call(
            _SEND_TOOL_NAME, payload, customer_slug, session_id=session_id
        )
    except Exception as exc:  # noqa: BLE001 (an indeterminate gate must not send)
        logger.exception("hermes-smd-trust: out-of-turn send gate raised; NOT dispatching")
        return DispatchResult(
            sent=False,
            reason=f"the seat could not authorize the send ({exc})",
            recipients=recipients,
        )
    if block is not None:
        reason = block.get("message", "withheld") if isinstance(block, dict) else "withheld"
        logger.info("hermes-smd-trust: out-of-turn send withheld by gate (%s)", reason)
        return DispatchResult(sent=False, reason=str(reason), recipients=recipients)
    # The fabrication + identifier gates (WS-RENDER review fix). They live in
    # ``outbound.check_*`` and fire from ``on_pre_tool_call`` — a hook an
    # out-of-turn send never crosses — so until this call, "through the full
    # gate" was a sentence, not a control. Wired HERE, at the one choke point
    # every out-of-turn sender shares (the prerendered dispatcher, the
    # rule-request loop, the establishment sweeper). An indeterminate scan
    # fails toward not sending, same as the hook's own posture.
    gate_session = _resolved_session({"session_id": session_id})
    for outbound_check in (outbound.check_outbound_draft, outbound.check_outbound_send):
        try:
            scan_block = outbound_check(
                tool_name=_SEND_TOOL_NAME, args=payload, session_id=gate_session
            )
        except Exception:  # noqa: BLE001 — an indeterminate scan must not send
            logger.exception("hermes-smd-trust: out-of-turn outbound scan raised; NOT dispatching")
            return DispatchResult(
                sent=False,
                reason="the seat could not scan the send",
                recipients=recipients,
            )
        if scan_block is not None:
            reason = (
                scan_block.get("message", "withheld")
                if isinstance(scan_block, dict)
                else "withheld"
            )
            logger.info("hermes-smd-trust: out-of-turn send blocked by outbound scan (%s)", reason)
            return DispatchResult(sent=False, reason=str(reason), recipients=recipients)
    # The gate may have rewritten the payload (it consumes approvals and stores
    # its own copy); everything below reads what the gate allowed.
    payload.pop(TEMPLATED_BODY_ARG, None)
    # The CONFIRM row's body stamp (WS-RENDER): the canonical hash of the text
    # the gate just allowed, taken BEFORE the html/plain attach mutates it —
    # the console verifier joins this against the pre_run's EMITTED_WAKE stamp.
    send_audit_extra = dict(audit_extra or {})
    allowed_text = payload.get("text")
    if isinstance(allowed_text, str) and allowed_text:
        send_audit_extra["rendered_body_sha256"] = prerendered_dispatch.canonical_body_sha256(
            allowed_text
        )
    _attach_html_body(_SEND_TOOL_NAME, payload)
    send_session_id = _resolved_session({"session_id": session_id})
    send_matter_ref = matter_gate.matter_ref_for(
        send_session_id, _send_cited_matters(send_session_id, payload)
    )
    try:
        if _seat_email_adapter() == _ADAPTER_MSGRAPH:
            message_id = outbound_send.send_via_msgraph(
                payload,
                session_id=send_session_id,
                matter_ref=send_matter_ref,
                audit_extra=send_audit_extra,
            )
        else:
            message_id = outbound_send.send_message(
                payload=payload,
                session_id=send_session_id,
                matter_ref=send_matter_ref,
                audit_extra=send_audit_extra,
            )
    except outbound_send.OutboundSendError as exc:
        logger.error("hermes-smd-trust: out-of-turn send failed (%s)", exc)
        return DispatchResult(sent=False, reason=str(exc), recipients=recipients)
    logger.info(
        "hermes-smd-trust: dispatched out-of-turn message to %s (message %s)",
        ", ".join(recipients),
        message_id,
    )
    return DispatchResult(sent=True, message_id=message_id, recipients=recipients)


def _profiles_root() -> Path:
    """Where the per-persona profile trees live: ``$HERMES_HOME/profiles``.

    Same derivation ``bootstrap/translate.py`` uses when it writes them, so the
    turn-time refresh addresses exactly the files boot stamped. Falls back to
    the image default rather than raising — a wrong path yields zero refreshed
    files and a debug line, never a blocked turn, because delivery is
    best-effort and the send-site gate is the actual guarantee.
    """
    return Path(os.environ.get("HERMES_HOME", "/opt/data")) / "profiles"


# ---------------------------------------------------------------------------
# Pre-run handoff seeding (ss-console#2547)
#
# A cron routine's ``pre_run.py`` reads the firm's records BEFORE the session
# exists and hands what it read to the model as prompt text. Until now nothing
# carried that read into the provenance register, so on 2026-08-19 the deadline
# escalator was refused five times on the very dates its own script had just
# pulled out of Smokeball. The gate was right about what it could see; the read
# simply had no representative inside the session.
#
# ONE SESSION, ONE ATTEMPT. The mark goes down BEFORE the take, so a session
# whose handoff is missing, stale, or out of window does not go looking again on
# its next turn — by then it would be outside the binding window anyway, and a
# per-turn retry is how a bounded lookup becomes a per-turn file stat forever.
# ---------------------------------------------------------------------------

#: Resolved session ids that have already been offered their handoff. Bounded
#: the same way and for the same reason as ``provenance._registers``: a
#: long-lived Machine must not grow a set of every session it ever ran. Oldest
#: out first — an evicted session's only cost is that it could take a handoff
#: twice, and ``take_handoff``'s consume-once rename makes the second take
#: return nothing anyway. Belt on a brace.
_HANDOFF_SEEDED: "OrderedDict[str, bool]" = OrderedDict()
_MAX_HANDOFF_SEEDED = 256


def _seed_from_pre_run_handoff(session_id: str) -> None:
    """Seed this cron session's register from its routine's pre-run read.

    Returns silently on every path that is not "a cron session, whose routine
    names a skill, whose handoff was written for this session". A non-cron
    session never reaches the take at all: an interactive turn must not inherit
    a routine's reads.

    ONLY DATE ATOMS AND VALIDATED RECORDS ARE SEEDED, and that is enforced
    twice — ``take_handoff`` returns nothing but dates that scan as dates and
    ``(matterNumber, dates)`` records whose number scans as a case number
    (``pre_run_handoff._record_entries``), and this passes dates to
    ``record_read`` and records to ``record_records``. Records seed through
    ``add_record`` so the (number, date) PAIRINGS register too: a seeded number
    cannot certify a date from a different matter. The projection is what makes
    a script's output safe to trust: the script reads authored values out of the
    firm's system of record verbatim, and everything else it emits (subjects,
    ACK codes, its own sentences) is composition that must go on failing the
    gate.

    ``persona`` rides along because the scheduler runs the writer with the
    PERSONA home as ``HERMES_HOME`` (2026-08-24 pilot probe, defect A) — the
    reader must look where the writer wrote.
    """
    resolved = provenance.resolve_session(session_id)
    if not resolved or resolved in _HANDOFF_SEEDED:
        return
    routine = cron_attribution.resolve_routine(session_id)
    if routine is None or not routine.skill:
        return
    started_at = cron_attribution.parse_cron_session_started_at(session_id)
    if started_at is None:
        return
    _HANDOFF_SEEDED[resolved] = True
    while len(_HANDOFF_SEEDED) > _MAX_HANDOFF_SEEDED:
        _HANDOFF_SEEDED.popitem(last=False)
    taken = pre_run_handoff.take_handoff(routine.skill, started_at, persona=routine.persona)
    dates = (taken or {}).get("dates") or []
    records = (taken or {}).get("records") or []
    if not dates and not records:
        return
    if dates:
        provenance.record_read(resolved, " ".join(dates))
    if records:
        provenance.record_records(resolved, records)
    logger.info(
        "hermes-smd-trust: seeded %d pre-run date(s) + %d record(s) for %s from %s's handoff (ss#2547)",
        len(dates),
        len(records),
        resolved,
        routine.skill,
    )


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
    context_notes: list[str] = []
    try:
        provenance.note_session(session_id)
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("hermes-smd-trust: pre_llm_call session note failed", exc_info=True)
    try:
        # 3. Seed this cron session's register from its routine's pre-run read
        # (ss#2547). AFTER note_session, because the seeding keys on the resolved
        # id and note_session is what makes the resolution this turn's own. Its
        # OWN try: a handoff that cannot be read must cost a refusal the human
        # can clear, never the turn.
        _seed_from_pre_run_handoff(session_id)
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("hermes-smd-trust: pre-run handoff seeding failed", exc_info=True)
    try:
        # 4. Dispatch this cron session's PRE-RENDERED outbound (WS-RENDER),
        # AFTER the handoff seeding so the provenance register already holds
        # the dates and matter numbers the rendered body carries. Out of turn,
        # templated, through the full gate; the module writes the ledger
        # appends post-dispatch and returns one context note. A failure here
        # costs the dispatch (the skill's failure-note instruction + the
        # heartbeat pager cover it), never the turn.
        dispatch_note = prerendered_dispatch.dispatch_prerendered(session_id)
        if dispatch_note:
            context_notes.append(dispatch_note)
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("hermes-smd-trust: prerendered dispatch failed", exc_info=True)
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
        # Read-volume gate (agreement §2.8): claim any fresh dispatch-unkeyed
        # review routes onto this session. Marks ALL fresh claimants while a
        # route is pending (over-applying a read gate is recoverable;
        # under-applying breaches the agreement — the inverse of
        # claim_unbound's exactly-one rule, argued in shared/read_volume.py).
        read_volume.claim_unbound_routes(provenance.resolve_session(session_id))
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("hermes-smd-trust: read-volume route claim failed", exc_info=True)
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
                context_notes.append(context)
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("hermes-smd-trust: approval capture/dispatch failed", exc_info=True)
    if context_notes:
        return {"context": " ".join(context_notes)}
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
    # ss-console#2546. The rule-request loop sends from three places that are
    # not this plugin and cannot import it. Publishing the sender here keeps
    # the gate and the transport in one file and stops anyone reimplementing
    # them against the raw broker clients, which ARE importable from shared.
    set_sender(_dispatch_internal_message)
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
