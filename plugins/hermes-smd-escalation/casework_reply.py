"""``reply_verdicts``: a person's plain-word answer to an Operator list.

Split from ``casework.py`` (see its docstring for the three case-manager tools).
The reply's THREAD finds the list: the broker stamped each casework raise with
the ``thread_ref`` of the message that carried it, and the verified inbound
origin carries the same id. The reader's words give a verdict per number
(``reply_items.parse_reply_verdicts``), and code maps each number to its raise
rows. Approved task writes are queued for replay (``shared.casework_acts``); the
model never supplies an id. A thread with no casework rows is handed to
``escalation_reply_ack`` unchanged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from shared import digest_reply_ref, inbound
from shared.casework_acts import CASEWORK_ACTS, COMPLETED, CaseworkActs, TaskWrite

from . import reply_items
from .casework_rules import (
    _REPLIES,
    VERDICT_EVENTS,
    _append,
    _event,
    _numbers,
    _queued_note,
    read_rows,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# reply_verdicts
# ---------------------------------------------------------------------------

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


def _answered(rows: list[dict], raise_row: dict) -> bool:
    """A verdict already stands for this raise (same item, number and dispatch)."""
    for row in rows:
        if row.get("event") not in VERDICT_EVENTS:
            continue
        if row.get("item_key") != raise_row.get("item_key") or row.get("n") != raise_row.get("n"):
            continue
        ref = row.get("dispatch_ref")
        if ref is None or ref == raise_row.get("dispatch_ref"):
            return True
    return False


def _write_for(row: dict) -> TaskWrite | None:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    action = payload.get("action")
    task_id = row.get("task_id") or payload.get("task_id")
    staff_id = payload.get("staff_id")
    if not (isinstance(task_id, str) and task_id and isinstance(staff_id, str) and staff_id):
        return None
    common = {
        "task_id": task_id,
        "staff_id": staff_id,
        "item_key": str(row["item_key"]),
        "matter_id": row.get("matter_id"),
        "skill": str(row.get("skill") or ""),
        "n": row.get("n"),
    }
    if action == "close" and payload.get("class") != "at_stake":
        return TaskWrite(is_completed=True, **common)
    if action == "reassign" and isinstance(payload.get("to_staff_id"), str):
        return TaskWrite(assignee_ids=(payload["to_staff_id"],), **common)
    return None


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
    for level, numbers in (("handles", state["steps_handles"]), ("prepares", state["steps_prep"])):
        if not numbers:
            continue
        if level == "handles":
            parts.append(f"Starting on {_numbers(numbers)} now.")
        else:
            verb = "it" if len(numbers) == 1 else "them"
            parts.append(f"I'll prepare {_numbers(numbers)} and send {verb} to you for review.")
    if state["left"]:
        tail = "as it is" if len(state["left"]) == 1 else "as they are"
        parts.append(f"Leaving {_numbers(state['left'])} {tail}.")
    if state["answered"]:
        parts.append(f"I already had your answer on {_numbers(state['answered'])}.")
    if failed:
        pronoun = "it" if len(failed) == 1 else "them"
        parts.append(
            f"I couldn't update {_numbers(failed)} in Smokeball just now, so "
            f"{pronoun} {'is' if len(failed) == 1 else 'are'} unchanged."
        )
    if state["not_recorded"]:
        parts.append(
            f"I couldn't record {_numbers(state['not_recorded'])} just now; please send "
            f"{'it' if len(state['not_recorded']) == 1 else 'them'} again."
        )
    return " ".join(["Got it.", *parts]) if parts else "Got it."


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
            return _reply(
                WRITES_QUEUED, "", writes=remaining, note=_queued_note(remaining, "reply_verdicts")
            )
        _REPLIES.pop(session_id)
        outcomes = {o.write.item_key: o.status for o in acts.outcomes(session_id)}
        acts.clear(session_id)
        return _reply(DONE, render_reply_confirmation(state, outcomes), steps_to_run=state["steps"])

    origin = inbound.SESSION_INBOUND_ORIGIN.get(session_id) if session_id else None
    thread_ref = (getattr(origin, "conversation_id", "") or "") if origin is not None else ""
    rows = read_rows(casework_ledger_path) if thread_ref else []
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
    refs = {row.get("dispatch_ref") for group in listing.values() for row in group}
    if len(refs) > 1:
        return _reply(
            reply_items.AMBIGUOUS_THREAD,
            "I couldn't tell which list you're answering. Reply directly to the most recent one.",
        )

    valid = sorted(listing)
    verdicts = reply_items.parse_reply_verdicts(getattr(origin, "reply_text", ""))
    if verdicts["conflict"]:
        both = sorted(verdicts["approve"] & verdicts["hold"])
        return _reply(
            CONFLICT,
            f"I read {_numbers(both)} as both yes and leave it. Which did you mean? "
            "Nothing has changed yet.",
        )
    holds = set(verdicts["hold"])
    if verdicts["all"]:
        approve = {n for n in valid if n not in holds}
    elif verdicts["bare_yes"] and len(valid) == 1:
        approve = set(valid)
    else:
        approve = set(verdicts["approve"])
    unknown = sorted((approve | holds) - set(valid))
    if unknown:
        span = (
            f"The numbers were {valid[0]} to {valid[-1]}."
            if len(valid) > 1
            else (f"The only number was {valid[0]}.")
        )
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

    acker = verified_acker(session_id)
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
        if all(_answered(rows, row) for row in group):
            state["answered"].append(number)
            continue
        verdict = APPROVED if number in approve else HELD
        recorded = True
        for row in group:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            event = _event(
                verdict,
                skill=str(row.get("skill") or ""),
                matter_id=row.get("matter_id"),
                item_key=str(row["item_key"]),
                session_id=session_id,
                n=number,
                dispatch_ref=row.get("dispatch_ref"),
                acked_by=acker,
                step=payload.get("step") if verdict == APPROVED else None,
            )
            recorded = _append(append, event) and recorded
        if not recorded:
            state["not_recorded"].append(number)
            continue
        if verdict == HELD:
            state["left"].append(number)
            continue
        for row in group:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
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

    if state["writes"]:
        acts.load(session_id, [write for _, write in state["writes"]])
        _REPLIES.put(session_id, state)
        count = len(state["writes"])
        return _reply(WRITES_QUEUED, "", writes=count, note=_queued_note(count, "reply_verdicts"))
    return _reply(DONE, render_reply_confirmation(state, {}), steps_to_run=state["steps"])


__all__ = ["render_reply_confirmation", "reply_verdicts"]
