"""``casework_step_done``: a date-prep step the Operator ran itself, on the record.

A step the firm set to "Handles it" is run by the date-prep turn before it
writes the brief (ss-console ``operator/skills/date-prep-brief/SKILL.md``). When
no decision is left there is no brief, and until now the work showed up
nowhere a person reads. This tool records it: one ``step_ran`` row in the
casework ledger, which the next message to that person (the task review, the
next brief on the matter, or the daily deadline digest) renders as a "Done
since last time" line from the row alone, then marks told.

What the model cannot forge, and why:

* **No arguments.** The step recorded is the next ``handles`` entry of this
  run's tamper-fenced brief envelope, in catalog order, so the turn cannot
  name a step the pre_run did not derive, a level the firm did not set, or
  another matter or date.
* **A real trace behind it.** Each prep routine files its ``[Operator]`` memo on
  the matter when it runs. The row claims one successful create_memo call on
  the brief's matter from this session (:mod:`shared.casework_steps`), and the
  broker re-checks that call against its own audit log before it writes. No
  memo, no row: the tool says so and records nothing. One memo backs one step.

The envelope is shared with ``casework_brief`` through one per-session state
(:func:`brief_state`), so a step recorded here is listed under Done in the
brief's own words (the entry's ``done_line``) and marked told when the brief
goes out.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from shared.casework_steps import STEP_WITNESS, StepWitness

from .casework_rules import (
    _BRIEFED,
    BRIEF_SUFFIX,
    _append,
    _event,
    _routine,
    step_payload,
    take_envelope,
    valid_brief_envelope,
)

STEP_DONE_DESCRIPTION = (
    "Record that you just ran one of this run's date-prep steps at 'handles'. Takes NO "
    "arguments. Run the handles steps in the order the catalog lists them, and call this "
    "once right after each one finishes: it records the next handles step in the catalog, "
    "backed by the memo that step's routine filed on this matter. If no such memo was filed "
    "since the last step you recorded, it records nothing and tells you. Do not also write "
    "a done line for a recorded step: the brief lists it for you."
)


def brief_state(
    session_id: str,
    *,
    routine: Callable[[str], tuple[str, str | None] | None] = _routine,
    hermes_home: str | None = None,
    now: datetime | None = None,
) -> dict | None:
    """This session's date-prep state: ``{skill, envelope, sent, steps}``,
    taking the brief envelope on first use. None when the run has none."""
    if not session_id:
        return None
    state = _BRIEFED.get(session_id)
    if state is not None:
        return state
    found = routine(session_id)
    if found is None:
        return None
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
        return None
    state = {"skill": envelope["skill"], "envelope": envelope, "sent": False, "steps": []}
    _BRIEFED.put(session_id, state)
    return state


def recorded_lines(state: dict) -> list[str]:
    """The Done lines for the steps recorded this session, in the order run."""
    return [step["done_line"] for step in state.get("steps") or [] if step.get("done_line")]


def recorded_mentions(state: dict) -> list[dict]:
    """The date item the recorded steps belong to, as a mention row (once)."""
    if not state.get("steps"):
        return []
    envelope = state["envelope"]
    return [
        {
            "matter_id": envelope["matter_id"],
            "kind": "date",
            "source_id": envelope["event_id"],
            "item_key": envelope["item_key"],
        }
    ]


def _result(status: str, note: str) -> str:
    return json.dumps({"status": status, "note": note}, ensure_ascii=False)


def casework_step_done(
    *,
    session_id: str,
    append: Callable[[dict], Any],
    routine: Callable[[str], tuple[str, str | None] | None] = _routine,
    witness: StepWitness = STEP_WITNESS,
    hermes_home: str | None = None,
    now: datetime | None = None,
) -> str:
    state = brief_state(session_id, routine=routine, hermes_home=hermes_home, now=now)
    if state is None:
        return _result("no_envelope", "There is no date-prep step to record in this run.")
    envelope = state["envelope"]
    done = {step["catalog_id"] for step in state["steps"]}
    pending = [
        e for e in envelope["catalog"] if e["level"] == "handles" and e["catalog_id"] not in done
    ]
    if not pending:
        return _result("nothing_left", "Every handles step in this run is already recorded.")
    memos = witness.unclaimed(session_id, envelope["matter_id"])
    if not memos:
        return _result(
            "no_trace",
            "Nothing was recorded: no memo was filed on this matter since the last step you "
            "recorded. Run the step first (its routine files its memo), then call this again. "
            "If the step could not finish, say so as a done line in the brief instead.",
        )
    entry, memo = pending[0], memos[0]
    event = _event(
        "step_ran",
        skill=state["skill"],
        matter_id=envelope["matter_id"],
        kind="date",
        source_id=envelope["event_id"],
        item_key=envelope["item_key"],
        session_id=memo.raw_session or session_id,
        payload=step_payload(entry),
        tool_call_id=memo.call_id,
    )
    if not _append(append, event):
        return _result(
            "not_recorded",
            "The step could not be recorded. Say what you did as a done line in the brief.",
        )
    witness.claim(session_id, memo.call_id)
    state["steps"].append({"catalog_id": entry["catalog_id"], "done_line": entry.get("done_line")})
    return _result("recorded", f"Recorded {entry['catalog_id']}. Continue with the next step.")


__all__ = [
    "STEP_DONE_DESCRIPTION",
    "brief_state",
    "casework_step_done",
    "recorded_lines",
    "recorded_mentions",
]
