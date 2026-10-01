"""A person says a deadline-digest line is DONE, and the task closes in Smokeball.

THE CHANGE (2026-10-01). A reply to a ``[Deadlines]`` digest used to be an
acknowledgement only: "done with 1" and "got it on 1" both quieted item 1 for
the firm's snooze window, and only completion in Smokeball closed it. The
person the Operator works for could not tell it to close a task it had just
reminded them about. Now the digest's send raises each closable task line as a
casework ``proposed`` row with payload action ``complete`` (ss-console
``dispatch_envelope.casework_raises`` -> :mod:`shared.casework_raises`), joined
to the sent message by ``dispatch_ref`` and the broker-stamped thread. A
completion in the reply approves that line under the replier's authored name,
which authorizes exactly one ``update_task`` write, replayed by the trust gate
from the stored payload (:mod:`shared.casework_acts`); the model supplies no
id and composes no sentence.

WHAT EACH NUMBER GETS, in code, from the reader's own words
(:func:`reply_items.parse_reply_verdicts`):

* complete, and the line is a task the digest raised: ``approved`` + the write;
* complete, and the line is a date: quieted, and told it clears when it passes;
* complete, and the line is a group (one number over several tasks): quieted,
  and told a group cannot be closed from a reply;
* complete, and the line is a task with no raise (no owner could be resolved
  at send time): quieted, and told why it could not be closed;
* complete, and the replier has no authored name: quieted, and told so (the
  ledger refuses an anonymous approval of a complete);
* acknowledged ("got it", a bare number): quieted, exactly as before;
* uncertain (a completion word beside a hold or future word: "not done with
  1", "I'll close 1 after the FSC"): nothing written, one question;
* held ("leave 2"): nothing written, as before.

Clear numbers are acted on and unclear ones asked about in the SAME reply. A
number not on the list, or a bare "done" on a multi-line digest, writes
nothing and asks. The FIRST call writes every ack and every approval and
decides every sentence, so the acks stand even if the turn never calls back;
the second call (after the queued writes) only renders.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from shared import casework_ledger, sent_lines
from shared.casework_acts import CASEWORK_ACTS, COMPLETED

from . import reply_items
from .casework_rules import _REPLIES, _append, _event, _queued_note

logger = logging.getLogger(__name__)

DIGEST = "digest"
WRITES_QUEUED = "writes_queued"
DONE = "done"
ASK = "ask"

_OWNER_RE = re.compile(r";\s*owner\s+(.+?)\s*$")


# ---------------------------------------------------------------------------
# The raises a digest thread carries (casework ledger read)
# ---------------------------------------------------------------------------


def _complete_raises(rows: list[dict], thread_ref: str) -> dict[int, list[dict]]:
    """``{n: [proposed complete rows]}`` the digest's send wrote on this thread."""
    found: dict[int, list[dict]] = {}
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if (
            row.get("event") != "proposed"
            or row.get("skill") != casework_ledger.COMPLETE_SKILL
            or row.get("thread_ref") != thread_ref
            or row.get("kind") != "task"
            or payload.get("action") != "complete"
        ):
            continue
        number = row.get("n")
        if reply_items.valid_digest_number(number) and row.get("item_key"):
            found.setdefault(number, []).append(row)
    return found


def _answered(states: dict, row: dict) -> bool:
    state = states.get(row.get("item_key"))
    decision = state.decisions.get((row.get("thread_ref"), row.get("n"))) if state else None
    return decision is not None and decision.verdict is not None


def _owner_name(row: dict) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    match = _OWNER_RE.search(str(payload.get("reason") or ""))
    return match.group(1) if match else ""


def _write_for(row: dict):
    """The task write an approved complete authorizes (``casework_reply``)."""
    from .casework_reply import _write_for as write_for

    return write_for(row)


# ---------------------------------------------------------------------------
# Resolving the reader's words against the digest
# ---------------------------------------------------------------------------


def _selections(verdicts: dict, valid: list[int]) -> tuple[list[int], list[int], list[int]]:
    """``(complete, ack, uncertain)`` numbers, each sorted, holds removed."""
    holds = set(verdicts["hold"])
    uncertain = set(verdicts["uncertain"])
    if verdicts["all"]:
        chosen = [n for n in valid if n not in holds and n not in uncertain]
        if verdicts["all_complete"]:
            return chosen, [], sorted(uncertain)
        return [], chosen, sorted(uncertain)
    complete = set(verdicts["complete"]) - holds - uncertain
    ack = set(verdicts["approve"]) - complete - holds - uncertain
    return sorted(complete), sorted(ack), sorted(uncertain)


def _label(names: dict[int, str], number: int) -> str:
    return sent_lines.name_numbers([number], names)


# ---------------------------------------------------------------------------
# The sentences (authored constants; values from the ledger and the sent lines)
# ---------------------------------------------------------------------------


def _period(snooze_days: int | None) -> str:
    if reply_items.valid_snooze_days(snooze_days):
        return f"for {snooze_days} day" + ("" if snooze_days == 1 else "s")
    return "for now"


def _explain(kind: str | None, group: bool, number: int, names: dict, period: str) -> str:
    """Why a completion could not close this line, and what happened instead."""
    who = _label(names, number)
    if kind == "date":
        return f"{who} is a date; it clears when it passes. Quiet {period}."
    if group:
        return (
            f"{who} covers several items; I can't close a group from a reply. "
            f"Quiet {period}; close the finished ones in Smokeball."
        )
    return (
        f"I can't close {who} from here: nobody is set on the matter in Smokeball and I "
        f"have no staff record for you. Quiet {period}."
    )


_NO_NAME = (
    "I have no authored name for you on this seat, so I can't close {who} from a reply; "
    "it is quiet {period}."
)


def render_digest_confirmation(state: dict, outcomes: dict[str, str]) -> str:
    """One sentence per outcome, in the order a reader acts on them: what
    closed, what could not, what went quiet, what is still open, then the one
    question. Every value is a sent label or a ledger fact."""
    names = state.get("names") or {}
    period = state.get("period") or "for now"

    def named(values: list[int], conjunction: str = "and") -> str:
        return sent_lines.name_numbers(sorted(values), names, conjunction)

    closed: list[int] = []
    failed: list[int] = []
    for number, write in state.get("writes") or []:
        (closed if outcomes.get(write.item_key) == COMPLETED else failed).append(number)
    parts: list[str] = []
    if closed:
        parts.append(f"Closed {named(closed)}.")
        owners = {state["owners"].get(n) for n in closed} - {None, "", state.get("acker_name")}
        for owner in sorted(owners):
            parts.append(f"Recorded in Smokeball under {owner}.")
    if failed:
        pronoun, verb = ("it", "is") if len(failed) == 1 else ("them", "are")
        parts.append(
            f"I couldn't update {named(failed)} in Smokeball just now, so {pronoun} {verb} "
            "unchanged."
        )
    for number in sorted(state.get("explained") or {}):
        parts.append(state["explained"][number])
    if state.get("acked"):
        verb = "is" if len(state["acked"]) == 1 else "are"
        parts.append(f"{named(state['acked'])} {verb} quiet {period}.")
    if state.get("answered"):
        parts.append(f"I already had your answer on {named(state['answered'])}.")
    if state.get("not_recorded"):
        pronoun = "it" if len(state["not_recorded"]) == 1 else "them"
        parts.append(
            f"I couldn't record {named(state['not_recorded'])} just now; please send "
            f"{pronoun} again."
        )
    if state.get("still_open"):
        parts.append(f"Still open: {named(state['still_open'])}.")
    if state.get("uncertain"):
        for number in state["uncertain"]:
            parts.append(f"Is {_label(names, number)} done, or still open?")
    return " ".join(["Got it.", *parts]) if parts else "Got it."


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


def _reply(status: str, text: str, **extra: Any) -> str:
    return json.dumps({"status": status, "confirmation_text": text, **extra}, ensure_ascii=False)


def _confirmed(session_id: str, state: dict, outcomes: dict[str, str]) -> str:
    text = render_digest_confirmation(state, outcomes)
    sent_lines.seed_provenance(session_id, text, state.get("names") or {})
    return _reply(
        DONE, text, acked=state.get("acked_all") or [], still_open=state.get("still_open") or []
    )


def second_call(session_id: str, acts: Any = None) -> str | None:
    """The turn's second call, after the queued writes: render from the recorded
    outcomes. ``None`` when this session holds no digest reply state."""
    state = _REPLIES.get(session_id) if session_id else None
    if not (isinstance(state, dict) and state.get("kind") == DIGEST):
        return None
    acts = acts or CASEWORK_ACTS
    remaining = acts.pending(session_id)
    if remaining:
        return _reply(
            WRITES_QUEUED, "", writes=remaining, note=_queued_note(remaining, "reply_verdicts")
        )
    acts.flush(session_id)
    _REPLIES.pop(session_id)
    outcomes = {o.write.item_key: o.status for o in acts.outcomes(session_id)}
    acts.clear(session_id)
    return _confirmed(session_id, state, outcomes)


def _approve(
    rows: list[dict],
    number: int,
    *,
    session_id: str,
    thread_ref: str,
    acker: dict,
    append: Callable,
) -> bool:
    recorded = True
    for row in rows:
        event = _event(
            "approved",
            skill=str(row.get("skill") or ""),
            matter_id=str(row.get("matter_id") or ""),
            kind=str(row.get("kind") or ""),
            source_id=str(row.get("source_id") or ""),
            item_key=str(row["item_key"]),
            session_id=session_id,
            n=number,
            thread_ref=thread_ref,
            decided_by=acker,
        )
        recorded = _append(append, event) and recorded
    return recorded


def handle(
    *,
    session_id: str,
    verdicts: dict,
    digest: dict[int, dict[str, dict]],
    states: dict,
    valid: list[int],
    open_now: list[int],
    thread_ref: str,
    dispatch_ref: str | None,
    acker: dict[str, str] | None,
    broker_request: Callable[[dict], dict],
    casework_ledger_path: str | None,
    casework_append: Callable[[dict], Any] | None,
    acts: Any = None,
) -> str:
    """Act on a digest reply that says something is finished."""
    acts = acts or CASEWORK_ACTS
    names = sent_lines.labels(dispatch_ref)
    kinds = sent_lines.kinds(dispatch_ref)
    complete, ack, uncertain = _selections(verdicts, valid)
    if verdicts["bare_complete"] and len(valid) == 1:
        complete = [valid[0]]
    elif verdicts["bare_complete"]:
        return _reply(ASK, "Which ones are done? Reply with the numbers, or say all done.")
    unknown = sorted({*complete, *ack, *uncertain, *verdicts["hold"]} - set(valid))
    if unknown:
        # All or nothing: one number off the list writes no row at all.
        return reply_items.render_unknown(unknown, valid)
    if not complete and not ack and not uncertain:
        return _reply(
            ASK, "Which numbers do you have? Reply with the numbers from the list, or say all."
        )

    period = _period(reply_items.one_snooze(digest, [n for n in valid]))
    rows = casework_ledger.read_ledger(casework_ledger_path) if casework_ledger_path else []
    raises = _complete_raises(rows, thread_ref)
    cw_states = casework_ledger.derive_state(rows)
    append = casework_append or (lambda event: {"ok": False, "error": "no casework append"})

    state: dict[str, Any] = {
        "kind": DIGEST,
        "writes": [],
        "owners": {},
        "acked": [],
        "explained": {},
        "answered": [],
        "not_recorded": [],
        "uncertain": uncertain,
        "names": names,
        "period": period,
        "acker_name": (acker or {}).get("name"),
        "still_open": [],
    }
    to_ack = list(ack)
    for number in complete:
        raised = raises.get(number)
        group = len(digest[number]) > 1
        if raised and not group and acker is not None:
            if all(_answered(cw_states, row) for row in raised):
                state["answered"].append(number)
                continue
            if not _approve(
                raised,
                number,
                session_id=session_id,
                thread_ref=thread_ref,
                acker=acker,
                append=append,
            ):
                state["not_recorded"].append(number)
                continue
            for row in raised:
                write = _write_for(row)
                if write is None:
                    state["not_recorded"].append(number)
                else:
                    state["writes"].append((number, write))
                    state["owners"][number] = _owner_name(row)
            continue
        # A completion that cannot close: quiet it, and say why in words.
        to_ack.append(number)
        if raised and acker is None:
            state["explained"][number] = _NO_NAME.format(who=_label(names, number), period=period)
        else:
            state["explained"][number] = _explain(kinds.get(number), group, number, names, period)

    acked, failed = reply_items.ack_numbers(
        digest,
        sorted(set(to_ack)),
        session_id=session_id,
        acker=acker,
        broker_request=broker_request,
    )
    state["acked"] = [n for n in acked if n not in state["explained"]]
    state["acked_all"] = sorted(acked)
    for number in failed:
        state["explained"].pop(number, None)
        state["not_recorded"].append(number)
    touched = {
        *acked,
        *failed,
        *(n for n, _w in state["writes"]),
        *state["answered"],
        *state["not_recorded"],
    }
    state["still_open"] = [n for n in open_now if n not in touched and n not in uncertain]

    if state["writes"]:
        acts.load(session_id, [write for _, write in state["writes"]])
        _REPLIES.put(session_id, state)
        count = len(state["writes"])
        return _reply(WRITES_QUEUED, "", writes=count, note=_queued_note(count, "reply_verdicts"))
    return _confirmed(session_id, state, {})


__all__ = ["handle", "render_digest_confirmation", "second_call"]
