"""``reply_verdicts``: a person's plain-word answer to an Operator list.

Split from ``casework.py`` (see its docstring for the three case-manager tools).
The reply's THREAD finds the list: the broker stamped each casework raise with
the ``thread_ref`` of the message that carried it, and the verified inbound
origin carries the same id. The reader's words give a verdict per number
(``reply_items.parse_reply_verdicts``), and code maps each number to its raise
rows. Approved task writes are queued for replay (``shared.casework_acts``); the
model never supplies an id. A thread with no casework rows is handed to
``escalation_reply_ack`` unchanged.

Each verdict row names its line by the raise's own ``(thread_ref, n)``, which is
the slot the broker checks it against; a hold is followed by ``kept``, which
keeps the task quiet for the firm's authored number of days.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from shared import casework_ledger, digest_reply_ref, inbound
from shared.casework_acts import CASEWORK_ACTS, COMPLETED, CaseworkActs, TaskWrite

from . import reply_items
from .casework_rules import _REPLIES, _append, _event, _numbers, _queued_note

logger = logging.getLogger(__name__)

APPROVED = "approved"
HELD = "held"
WRITES_QUEUED = "writes_queued"
DONE = "done"
CONFLICT = "conflict"


def _thread_rows(rows: list[dict], thread_ref: str) -> dict[int, list[dict]]:
    """``{n: [raise rows]}`` for the casework raises the broker stamped on this thread."""
    found: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("event") not in digest_reply_ref.CASEWORK_RAISE_EVENTS:
            continue
        if row.get("thread_ref") != thread_ref:
            continue
        number = row.get("n")
        if digest_reply_ref.valid_digest_number(number) and row.get("item_key"):
            found.setdefault(number, []).append(row)
    return found


def _answered(states: dict, row: dict) -> bool:
    """A verdict already stands on this raise's line (the ledger's own fold)."""
    state = states.get(row.get("item_key"))
    decision = state.decisions.get((row.get("thread_ref"), row.get("n"))) if state else None
    return decision is not None and decision.verdict is not None


def _payload(row: dict) -> dict:
    return row["payload"] if isinstance(row.get("payload"), dict) else {}


def _write_for(row: dict) -> TaskWrite | None:
    """The task write an approved line authorizes, from the raise row alone."""
    payload = _payload(row)
    action = payload.get("action")
    task_id = row.get("source_id")
    staff_id = payload.get("staff_id")
    if row.get("kind") != "task" or not (isinstance(task_id, str) and task_id):
        return None
    if not (isinstance(staff_id, str) and staff_id):
        return None
    common = {
        "task_id": task_id,
        "staff_id": staff_id,
        "item_key": str(row["item_key"]),
        "matter_id": str(row.get("matter_id") or ""),
        "skill": str(row.get("skill") or ""),
        "n": row.get("n"),
    }
    if action == "close" and payload.get("class") != "at_stake":
        return TaskWrite(is_completed=True, **common)
    if action == "reassign" and isinstance(payload.get("to_staff_id"), str):
        return TaskWrite(assignee_ids=(payload["to_staff_id"],), **common)
    return None


def _row_event(name: str, row: dict, session_id: str, **fields: Any) -> dict[str, Any]:
    return _event(
        name,
        skill=str(row.get("skill") or ""),
        matter_id=str(row.get("matter_id") or ""),
        kind=str(row.get("kind") or ""),
        source_id=str(row.get("source_id") or ""),
        item_key=str(row["item_key"]),
        session_id=session_id,
        **fields,
    )


def _reply(status: str, text: str, **extra: Any) -> str:
    return json.dumps({"status": status, "confirmation_text": text, **extra}, ensure_ascii=False)


def render_reply_confirmation(state: dict, outcomes: dict[str, str]) -> str:
    """The sentence the seat sends back once every queued write has an outcome."""
    closed, reassigned, failed = [], [], []
    for number, write in state["writes"]:
        status = outcomes.get(write.item_key)
        if status == COMPLETED:
            (closed if write.is_completed else reassigned).append(number)
        else:
            failed.append(number)
    parts: list[str] = []
    if closed:
        parts.append(f"Closed {_numbers(closed)}.")
    if reassigned:
        parts.append(f"Reassigned {_numbers(reassigned)}.")
    if state["steps_handles"]:
        parts.append(f"Starting on {_numbers(state['steps_handles'])} now.")
    if state["steps_prep"]:
        verb = "it" if len(state["steps_prep"]) == 1 else "them"
        parts.append(
            f"I'll prepare {_numbers(state['steps_prep'])} and send {verb} to you for review."
        )
    if state["left"]:
        tail = "as it is" if len(state["left"]) == 1 else "as they are"
        parts.append(f"Leaving {_numbers(state['left'])} {tail}.")
    if state["answered"]:
        parts.append(f"I already had your answer on {_numbers(state['answered'])}.")
    if failed:
        pronoun, verb = ("it", "is") if len(failed) == 1 else ("them", "are")
        parts.append(
            f"I couldn't update {_numbers(failed)} in Smokeball just now, so {pronoun} {verb} "
            "unchanged."
        )
    if state["not_recorded"]:
        pronoun = "it" if len(state["not_recorded"]) == 1 else "them"
        parts.append(
            f"I couldn't record {_numbers(state['not_recorded'])} just now; please send "
            f"{pronoun} again."
        )
    return " ".join(["Got it.", *parts])


def _resolve_numbers(verdicts: dict, valid: list[int]) -> tuple[set[int], set[int]]:
    holds = set(verdicts["hold"])
    if verdicts["all"]:
        return {n for n in valid if n not in holds}, holds
    if verdicts["bare_yes"] and len(valid) == 1:
        return set(valid), holds
    return set(verdicts["approve"]), holds


def _ask(verdicts: dict, approve: set[int], holds: set[int], valid: list[int]) -> str | None:
    """The question to send instead of acting, or None when the reply is clear."""
    if verdicts["conflict"]:
        both = sorted(verdicts["approve"] & verdicts["hold"])
        return _reply(
            CONFLICT,
            f"I read {_numbers(both)} as both yes and leave it. Which did you mean? "
            "Nothing has changed yet.",
        )
    unknown = sorted((approve | holds) - set(valid))
    if unknown:
        if len(valid) > 1:
            span = f"The numbers were {valid[0]} to {valid[-1]}."
        else:
            span = f"The only number was {valid[0]}."
        return _reply(
            reply_items.UNKNOWN_NUMBERS,
            f"I don't see {_numbers(unknown, 'or')} on that list. {span} Nothing has changed yet.",
        )
    if not approve and not holds:
        return _reply(
            reply_items.NOTHING_PARSED,
            'Which ones should I go ahead with? Reply with the numbers, for example "yes '
            'to all" or "yes on 1, leave 2".',
        )
    return None


def _start_steps(
    append: Callable[[dict], Any], group: list[dict], session_id: str, number: int, thread: str
) -> None:
    """An approved step line starts now: this turn runs the step's skill, so the
    ledger records ``step_started`` on the same line (thread and number). A step
    at level surfaces is never offered, so it never starts here either."""
    for row in group:
        step = _payload(row).get("step")
        if _payload(row).get("action") != "step" or not isinstance(step, dict):
            continue
        if step.get("level") in ("prepares", "handles"):
            _append(
                append, _row_event("step_started", row, session_id, n=number, thread_ref=thread)
            )


def _act_on_approval(state: dict, number: int, group: list[dict]) -> None:
    for row in group:
        payload = _payload(row)
        action = payload.get("action")
        if action == "step" and isinstance(payload.get("step"), dict):
            step = payload["step"]
            state["steps"].append(
                {
                    "n": number,
                    "catalog_id": step.get("catalog_id"),
                    "skill": step.get("skill"),
                    "level": step.get("level"),
                    "matter_id": row.get("matter_id"),
                    "params": step.get("params") or {},
                }
            )
            bucket = "steps_handles" if step.get("level") == "handles" else "steps_prep"
            if number not in state[bucket]:
                state[bucket].append(number)
        elif action == "keep":
            if number not in state["left"]:
                state["left"].append(number)
        else:
            write = _write_for(row)
            if write is None:
                state["not_recorded"].append(number)
            else:
                state["writes"].append((number, write))


def reply_verdicts(
    *,
    session_id: str,
    load_config: Callable[[], Any],
    verified_acker: Callable[[str], dict[str, str] | None],
    append: Callable[[dict], Any],
    fallthrough: Callable[[], str],
    casework_ledger_path: str,
    acts: CaseworkActs = CASEWORK_ACTS,
) -> str:
    """Resolve this turn's verified reply to its casework rows and act on it.

    The second call in a turn (after the queued writes) returns the final
    confirmation, rendered from the recorded outcomes. A thread with no
    casework rows is handed to ``escalation_reply_ack`` unchanged."""
    state = _REPLIES.get(session_id) if session_id else None
    if state is not None:
        remaining = acts.pending(session_id)
        if remaining:
            note = _queued_note(remaining, "reply_verdicts")
            return _reply(WRITES_QUEUED, "", writes=remaining, note=note)
        acts.flush(session_id)
        _REPLIES.pop(session_id)
        outcomes = {o.write.item_key: o.status for o in acts.outcomes(session_id)}
        acts.clear(session_id)
        return _reply(DONE, render_reply_confirmation(state, outcomes), steps_to_run=state["steps"])

    origin = inbound.SESSION_INBOUND_ORIGIN.get(session_id) if session_id else None
    thread_ref = (getattr(origin, "conversation_id", "") or "") if origin is not None else ""
    rows = casework_ledger.read_ledger(casework_ledger_path) if thread_ref else []
    listing = _thread_rows(rows, thread_ref) if thread_ref else {}
    if not listing:
        return fallthrough()
    if not getattr(origin, "sender_address", ""):
        return _reply(reply_items.NO_VERIFIED_REPLY, "")
    if getattr(origin, "auto_submitted", False) is True:
        return _reply(reply_items.AUTO_REPLY, "")
    try:
        rostered = bool(load_config().sender_on_roster(origin.sender_address))
    except Exception:  # noqa: BLE001 — an unreadable roster authorizes nobody
        logger.warning("casework: roster unreadable for a reply")
        rostered = False
    if not rostered:
        return _reply(reply_items.NOT_ROSTERED, "")
    if len({row.get("dispatch_ref") for group in listing.values() for row in group}) > 1:
        return _reply(
            reply_items.AMBIGUOUS_THREAD,
            "I couldn't tell which list you're answering. Reply directly to the most recent one.",
        )

    valid = sorted(listing)
    verdicts = reply_items.parse_reply_verdicts(getattr(origin, "reply_text", ""))
    approve, holds = _resolve_numbers(verdicts, valid)
    question = _ask(verdicts, approve, holds, valid)
    if question is not None:
        return question

    acker = verified_acker(session_id)
    states = casework_ledger.derive_state(rows)
    state = {
        "writes": [],
        "steps": [],
        "steps_handles": [],
        "steps_prep": [],
        "left": [],
        "answered": [],
        "not_recorded": [],
    }
    for number in sorted(approve | holds):
        group = listing[number]
        if all(_answered(states, row) for row in group):
            state["answered"].append(number)
            continue
        verdict = APPROVED if number in approve else HELD
        recorded = True
        for row in group:
            event = _row_event(
                verdict, row, session_id, n=number, thread_ref=thread_ref, decided_by=acker
            )
            recorded = _append(append, event) and recorded
            if verdict == HELD and recorded:
                _append(append, _row_event("kept", row, session_id))
        if not recorded:
            state["not_recorded"].append(number)
        elif verdict == HELD:
            state["left"].append(number)
        else:
            _start_steps(append, group, session_id, number, thread_ref)
            _act_on_approval(state, number, group)

    if state["writes"]:
        acts.load(session_id, [write for _, write in state["writes"]])
        _REPLIES.put(session_id, state)
        count = len(state["writes"])
        return _reply(WRITES_QUEUED, "", writes=count, note=_queued_note(count, "reply_verdicts"))
    return _reply(DONE, render_reply_confirmation(state, {}), steps_to_run=state["steps"])


__all__ = ["render_reply_confirmation", "reply_verdicts"]
