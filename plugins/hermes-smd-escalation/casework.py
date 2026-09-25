"""Case-manager tools: the task review, the date-prep brief, and the reply to either.

The spec is ss-console ``docs/specs/operator/case-manager-deadline-work.md``: a
message to a firm's attorney or paralegal is what a great case manager would
send. It says what is done and asks only for the decisions that need a person,
and the person answers in words ("yes to all", "leave 2", "yes on 1"). Three
tools carry it, and none of them takes an identifier from the model:

* ``casework_finish`` (task review, Jobs 1 and 1a). The routine's pre_run writes
  a tamper-fenced envelope (``$HERMES_HOME/.smd/pre_run/<skill>.casework.json``,
  unwritable from inside a turn). On the first call the tool loads the closes
  the firm's level permits into the replay queue (``shared.casework_acts``) and
  writes their ``closed_by_record`` rows. Once the turn has made those writes,
  the next call renders every message IN CODE from the envelope plus the
  outcome rows, so "Closed just now" lists only what actually closed. It sends
  them through the full gate and records one raise per numbered line.
* ``casework_brief`` (date prep, Job 2). This is the one message the turn
  composes, because only the turn read the documents (a witness's name is
  document content). It still composes only lines: the decisions come from a
  CLOSED catalog in the envelope, at most two, and code renders the frame,
  subject and numbering.
* ``reply_verdicts``. It resolves a verified reply's thread to the raise rows the
  broker stamped, parses the words (``reply_items.parse_reply_verdicts``),
  writes ``approved`` / ``held`` rows and queues the approved writes. The
  number-to-task map is code; the model never supplies an id. A thread that
  holds no casework rows falls through to ``escalation_reply_ack``, so one tool
  serves every reply.

What these tools refuse, and why each refusal writes nothing:
* a reply naming a number not on the list;
* a number both approved and held;
* a thread that carries two lists;
* a bare "yes" to a list of several lines.
Each case asks instead. A wrong approval closes a real task, and a question
costs one more email.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from shared import digest_reply_ref, send_dispatch
from shared.casework_acts import CASEWORK_ACTS, COMPLETED, CaseworkActs, TaskWrite

from .casework_reply import reply_verdicts
from .casework_rules import (
    _BRIEFED,
    _FINISH,
    _MAX_BRIEF_DONE,
    _MAX_BRIEF_DONE_CHARS,
    _MAX_DECISIONS,
    _MAX_QUESTION,
    BRIEF_SUFFIX,
    CASEWORK_SUFFIX,
    _append,
    _event,
    _queued_note,
    _routine,
    _text,
    close_payload,
    ledger_path,
    render_brief,
    render_review,
    step_payload,
    take_envelope,
    valid_brief_envelope,
    valid_casework_envelope,
)

logger = logging.getLogger(__name__)


EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "done": {
            "type": "array",
            "maxItems": _MAX_BRIEF_DONE,
            "items": {"type": "string", "maxLength": _MAX_BRIEF_DONE_CHARS},
            "description": (
                "What is already done on this matter for the date, one short line each "
                f"(at most {_MAX_BRIEF_DONE_CHARS} characters). Only facts you read this turn."
            ),
        },
        "decisions": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_DECISIONS,
            "items": {
                "type": "object",
                "properties": {
                    "catalog_id": {
                        "type": "string",
                        "description": "One catalog_id from this run's decision catalog.",
                    },
                    "question": {
                        "type": "string",
                        "maxLength": _MAX_QUESTION,
                        "description": "The question for the attorney, in plain words.",
                    },
                },
                "required": ["catalog_id", "question"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["decisions"],
    "additionalProperties": False,
}

FINISH_DESCRIPTION = (
    "Finish this run's task review. Takes NO arguments. The first call loads the "
    "task closes this run is allowed to make and tells you how many "
    "mcp_smokeball_update_task calls to make (their arguments are filled in for "
    "you). Make them, then call casework_finish again: it writes and sends the "
    "review messages itself. Never compose a task-review email yourself."
)
REPLY_DESCRIPTION = (
    "Record a person's plain-word reply to an Operator email (a task review, a "
    "date-prep brief, or a deadline digest), for example 'yes to all', 'all except "
    "3', 'leave 2', 'yes on 1', 'got it on 1'. Takes NO arguments: the reply, its "
    "sender, the email it answers and the numbered lines are read from the verified "
    "inbound message and the ledger. If it returns writes_queued, make that many "
    "mcp_smokeball_update_task calls (the arguments are filled in for you) and call "
    "reply_verdicts again. If it returns steps_to_run, run each step's skill on that "
    "matter with those params. Send confirmation_text back verbatim, and nothing "
    "when it is empty."
)
BRIEF_DESCRIPTION = (
    "Send this run's date-prep brief to the matter's attorney. Give what is already "
    f"done (short lines, facts you read this turn) and 1 or {_MAX_DECISIONS} decisions, "
    "each a catalog_id from this run's decision catalog plus the question in plain "
    "words. The subject, recipients, numbering and closing are written for you. "
    "One brief per run; with no decision, send nothing."
)


# ---------------------------------------------------------------------------
# casework_finish
# ---------------------------------------------------------------------------


def casework_finish(
    *,
    session_id: str,
    append: Callable[[dict], Any],
    dispatch: Callable[..., Any] = send_dispatch.dispatch,
    routine: Callable[[str], tuple[str, str | None] | None] = _routine,
    acts: CaseworkActs = CASEWORK_ACTS,
    hermes_home: str | None = None,
    now: datetime | None = None,
) -> str:
    state = _FINISH.get(session_id) if session_id else None
    if state is None:
        found = routine(session_id) if session_id else None
        if found is None:
            return _finish_result("no_envelope", "There is no task review to finish in this run.")
        skill, persona = found
        envelope = take_envelope(
            skill,
            CASEWORK_SUFFIX,
            valid_casework_envelope,
            persona=persona,
            hermes_home=hermes_home,
            now=now,
        )
        if envelope is None:
            return _finish_result("no_envelope", "There is no task review to finish in this run.")
        state = {"skill": skill, "envelope": envelope, "sent": False, "loaded": set()}
        _FINISH.put(session_id, state)
        writes: list[TaskWrite] = []
        for message in envelope["messages"]:
            for close in message.get("closes") or []:
                recorded = _append(
                    append,
                    _event(
                        "closed_by_record",
                        skill=skill,
                        matter_id=close["matter_id"],
                        kind="task",
                        source_id=close["task_id"],
                        item_key=close["item_key"],
                        session_id=session_id,
                        payload=close_payload(close),
                    ),
                )
                if not recorded:
                    continue
                state["loaded"].add(close["item_key"])
                writes.append(
                    TaskWrite(
                        task_id=close["task_id"],
                        staff_id=close["staff_id"],
                        item_key=close["item_key"],
                        matter_id=close["matter_id"],
                        skill=skill,
                        is_completed=True,
                        line=close["line"],
                    )
                )
        if writes:
            acts.load(session_id, writes)
    if state["sent"]:
        return _finish_result("already_sent", "This run's task review has already gone out.")
    remaining = acts.pending(session_id)
    if remaining:
        return _finish_result(
            "writes_queued", _queued_note(remaining, "casework_finish"), writes=remaining
        )
    acts.flush(session_id)
    return _send_reviews(session_id, state, append, dispatch, acts)


def _finish_result(status: str, note: str, **extra: Any) -> str:
    return json.dumps({"status": status, "note": note, **extra}, ensure_ascii=False)


def _send_reviews(
    session_id: str,
    state: dict,
    append: Callable[[dict], Any],
    dispatch: Callable[..., Any],
    acts: CaseworkActs,
) -> str:
    skill = state["skill"]
    outcome = {o.write.item_key: o.status for o in acts.outcomes(session_id)}
    state["sent"] = True
    sent: list[dict] = []
    for message in state["envelope"]["messages"]:
        items = message.get("items") or []
        if not items:
            # No decision, no message. Its closes happened; their
            # closed_by_record rows stay unmentioned for the next message.
            continue
        closes = [c for c in message.get("closes") or [] if c["item_key"] in state["loaded"]]
        closed = [c for c in closes if outcome.get(c["item_key"]) == COMPLETED]
        failed = [c for c in closes if outcome.get(c["item_key"]) not in (None, COMPLETED)]
        body = render_review(message, closed, failed)
        dispatch_ref = digest_reply_ref.mint_dispatch_ref()
        audit_extra = {"skill_name": skill, "dispatch_ref": dispatch_ref, "body_variant": "full"}
        if message.get("routing_leg"):
            audit_extra["routing_leg"] = message["routing_leg"]
        result = dispatch(
            to=list(message["recipients"]),
            subject=message["subject"],
            text=body,
            session_id=session_id,
            cc=list(message.get("cc") or []),
            templated=True,
            audit_extra=audit_extra,
        )
        report = {
            "to": list(message["recipients"]),
            "sent": bool(getattr(result, "sent", False)),
            "message_id": getattr(result, "message_id", ""),
            "lines": len(items),
            "raises_written": 0,
        }
        if report["sent"]:
            report["raises_written"] = _write_raises(append, skill, session_id, dispatch_ref, items)
            # What this body told a person about is not told again (Job 3).
            _write_mentions(
                append, skill, session_id, [*(message.get("done_since") or []), *closed]
            )
        else:
            report["reason"] = getattr(result, "reason", "")
        sent.append(report)
    delivered = sum(1 for r in sent if r["sent"])
    if not sent:
        note = "Nothing needed a person this run, so no review was sent. Send nothing else."
    elif delivered == len(sent):
        note = (
            f"The task review went out ({delivered} message(s)). Do not send or record "
            "anything else for it."
        )
    else:
        note = (
            f"{len(sent) - delivered} of {len(sent)} review message(s) could not be sent. "
            "Report that in one plain line and send nothing else."
        )
    memos = list(state["envelope"].get("memos") or [])
    if memos:
        note += " Then file each memo in memos with create_memo, verbatim, on its matter."
    return _finish_result("sent", note, messages=sent, memos=memos)


def _write_raises(
    append: Callable[[dict], Any],
    skill: str,
    session_id: str,
    dispatch_ref: str,
    items: list[dict],
) -> int:
    written = 0
    for item in items:
        event = _event(
            item["event"],
            skill=skill,
            matter_id=item["matter_id"],
            kind="task",
            source_id=item["task_id"],
            item_key=item["item_key"],
            session_id=session_id,
            payload=dict(item["payload"]),
        )
        digest_reply_ref.stamp_casework_raise(event, item["n"], dispatch_ref)
        written += 1 if _append(append, event) else 0
    return written


def _write_mentions(
    append: Callable[[dict], Any], skill: str, session_id: str, rows: list[dict]
) -> None:
    """What a sent body told a person about is not told again (Job 3)."""
    for row in rows:
        _append(
            append,
            _event(
                "mentioned",
                skill=skill,
                matter_id=row["matter_id"],
                kind="task",
                source_id=row["task_id"],
                item_key=row["item_key"],
                session_id=session_id,
            ),
        )


# ---------------------------------------------------------------------------
# casework_brief
# ---------------------------------------------------------------------------


def _brief_refusal(reason: str) -> str:
    return json.dumps({"status": "refused", "note": reason}, ensure_ascii=False)


def _check_brief_args(args: dict, envelope: dict) -> str | None:
    done = args.get("done") or []
    decisions = args.get("decisions") or []
    if not isinstance(done, list) or len(done) > _MAX_BRIEF_DONE:
        return f"Give at most {_MAX_BRIEF_DONE} done lines."
    for line in done:
        if not _text(line, _MAX_BRIEF_DONE_CHARS):
            return (
                f"Each done line must be plain text of at most {_MAX_BRIEF_DONE_CHARS} "
                "characters, with no long dashes."
            )
    if not isinstance(decisions, list) or not decisions:
        return "A brief needs at least one decision. With no decision, send nothing."
    if len(decisions) > _MAX_DECISIONS:
        return f"A brief carries at most {_MAX_DECISIONS} decisions. Keep the two that matter most."
    catalog = {entry["catalog_id"] for entry in envelope["catalog"]}
    seen: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            return "Each decision needs a catalog_id and a question."
        catalog_id = decision.get("catalog_id")
        if catalog_id not in catalog:
            return (
                f"{catalog_id!r} is not in this run's decision catalog. Choose from: "
                + ", ".join(sorted(catalog))
                + "."
            )
        if catalog_id in seen:
            return f"{catalog_id!r} is asked twice. Ask each decision once."
        seen.add(catalog_id)
        if not _text(decision.get("question"), _MAX_QUESTION):
            return (
                f"Each question must be plain text of at most {_MAX_QUESTION} characters, "
                "with no long dashes."
            )
    return None


def casework_brief(
    args: dict,
    *,
    session_id: str,
    append: Callable[[dict], Any],
    dispatch: Callable[..., Any] = send_dispatch.dispatch,
    routine: Callable[[str], tuple[str, str | None] | None] = _routine,
    hermes_home: str | None = None,
    now: datetime | None = None,
) -> str:
    if not session_id:
        return _brief_refusal("There is no date-prep brief to send in this run.")
    state = _BRIEFED.get(session_id)
    if state is not None and state.get("sent"):
        return _brief_refusal("This run's brief has already gone out. Send nothing else.")
    if state is None:
        found = routine(session_id)
        envelope = None
        if found is not None:
            skill, persona = found
            envelope = take_envelope(
                skill,
                BRIEF_SUFFIX,
                valid_brief_envelope,
                persona=persona,
                hermes_home=hermes_home,
                now=now,
            )
        if envelope is None:
            return _brief_refusal("There is no date-prep brief to send in this run.")
        state = {"skill": envelope["skill"], "envelope": envelope, "sent": False}
        _BRIEFED.put(session_id, state)
    envelope = state["envelope"]
    refusal = _check_brief_args(args if isinstance(args, dict) else {}, envelope)
    if refusal:
        return _brief_refusal(refusal)
    done = [str(line) for line in args.get("done") or []]
    decisions = list(args["decisions"])
    subject, body = render_brief(envelope, done, decisions)
    dispatch_ref = digest_reply_ref.mint_dispatch_ref()
    audit_extra = {
        "skill_name": state["skill"],
        "dispatch_ref": dispatch_ref,
        "body_variant": "full",
    }
    if envelope.get("routing_leg"):
        audit_extra["routing_leg"] = envelope["routing_leg"]
    result = dispatch(
        to=list(envelope["recipients"]),
        subject=subject,
        text=body,
        session_id=session_id,
        cc=list(envelope.get("cc") or []),
        templated=False,
        audit_extra=audit_extra,
    )
    if not getattr(result, "sent", False):
        return json.dumps(
            {
                "status": "not_sent",
                "reason": getattr(result, "reason", ""),
                "note": (
                    "The brief was not sent. Fix only what the reason names and try again, "
                    "or report it in one plain line."
                ),
            },
            ensure_ascii=False,
        )
    state["sent"] = True
    catalog = {entry["catalog_id"]: entry for entry in envelope["catalog"]}
    written = 0
    for n, decision in enumerate(decisions, start=1):
        event = _event(
            "briefed",
            skill=state["skill"],
            matter_id=envelope["matter_id"],
            kind="date",
            source_id=envelope["event_id"],
            item_key=envelope["item_key"],
            session_id=session_id,
            payload=step_payload(catalog[decision["catalog_id"]]),
        )
        digest_reply_ref.stamp_casework_raise(event, n, dispatch_ref)
        written += 1 if _append(append, event) else 0
    _write_mentions(append, state["skill"], session_id, envelope.get("done_since") or [])
    return json.dumps(
        {
            "status": "sent",
            "message_id": getattr(result, "message_id", ""),
            "decisions_recorded": written,
            "note": "The brief went out. Do not send or record anything else for it.",
        },
        ensure_ascii=False,
    )


__all__ = [
    "BRIEF_DESCRIPTION",
    "BRIEF_SCHEMA",
    "EMPTY_SCHEMA",
    "FINISH_DESCRIPTION",
    "REPLY_DESCRIPTION",
    "reply_verdicts",
    "casework_finish",
    "ledger_path",
    "casework_brief",
    "render_brief",
    "render_review",
    "take_envelope",
    "valid_brief_envelope",
    "valid_casework_envelope",
]
