"""Done since last time: work the Operator finished without asking, told once.

The contract sentences (ss-console ``docs/specs/operator/case-manager-deadline-work.md``,
Jobs 2 and 3):

* a date-prep step the turn ran at "handles" is recorded as ``step_ran`` only on
  a create_memo call that really landed on the brief's matter in this session,
  and the model names nothing: the step is the next handles entry of the
  tamper-fenced envelope;
* the brief lists a recorded step under Done in the catalog's own words and
  marks it told; a step never recorded is never listed;
* a date item's untold step rides the task review, the brief and the daily
  digest like a close does, and every body that carries it marks it
  ``mentioned`` after it is sent (the digest: after a FULL send only).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from shared import (
    casework_ledger,
    casework_mentions,
    cron_attribution,
    prerendered_dispatch,
    send_dispatch,
)
from shared.casework_steps import MEMO_TOOL, StepWitness
from shared.send_dispatch import DispatchResult
from tests.conftest import load_plugin

plugin = load_plugin("hermes-smd-escalation")
casework = plugin.casework

SESSION = "cron_job-dpb_20260925_150500"
RAW = "raw-session-dpb"
NOW = datetime(2026, 9, 25, 15, 6, tzinfo=timezone.utc)
MATTER = "m-105"
K_DATE = casework_ledger.item_key(matter_id=MATTER, kind="date", source_id="ev-1")
K_EARLIER = casework_ledger.item_key(matter_id=MATTER, kind="date", source_id="ev-0")
DONE_LINE = "I asked Valley Imaging for records dated after 2026-06-20"


class Sent:
    def __init__(self, sent: bool = True) -> None:
        self.sent, self.reason, self.message_id = sent, "", "msg-1" if sent else ""


@pytest.fixture(autouse=True)
def _clean():
    from plugin_hermes_smd_escalation import casework_rules

    for store in (casework_rules._FINISH, casework_rules._BRIEFED):
        store.pop(SESSION)
    yield


def _envelope(**over) -> dict:
    envelope = {
        "skill": "date-prep-brief",
        "started_at": (NOW - timedelta(minutes=1)).isoformat(),
        "matter_id": MATTER,
        "matter_number": "2026-PI-105",
        "event_id": "ev-1",
        "item_key": K_DATE,
        "subject_label": "2026-PI-105: Hearing, Oct 2",
        "recipients": ["atty@firm.example"],
        "cc": ["para@firm.example"],
        "routing_leg": "matter_staff",
        "catalog": [
            {
                "catalog_id": "records_refresh:t-reyes",
                "skill": "medical-records-chaser",
                "level": "handles",
                "params": {"provider": "Valley Imaging", "mode": "update"},
                "done_line": DONE_LINE,
            },
            {
                "catalog_id": "witness_list_finalize",
                "skill": "trial-binder-assembler",
                "level": "prepares",
                "params": {"file_id": "f-wl"},
            },
        ],
    }
    envelope.update(over)
    return envelope


def _write(tmp_path, envelope) -> None:
    directory = tmp_path / ".smd" / "pre_run"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "date-prep-brief.brief.json").write_text(json.dumps(envelope), encoding="utf-8")


def _memo(witness: StepWitness, *, call="toolu_memo", matter=MATTER, status=None, result=None):
    kwargs = {"tool_call_id": call, "session_id": RAW, "args": {"matter_id": matter, "text": "x"}}
    if status is not None:
        kwargs["status"] = status
    if result is not None:
        kwargs["result"] = result
    witness.on_post_tool(MEMO_TOOL, SESSION, kwargs)


def _step_done(tmp_path, broker: list, witness: StepWitness) -> dict:
    return json.loads(
        casework.casework_step_done(
            session_id=SESSION,
            append=lambda event: broker.append(event) or {"ok": True},
            routine=lambda _s: ("date-prep-brief", None),
            witness=witness,
            hermes_home=str(tmp_path),
            now=NOW,
        )
    )


def _brief(tmp_path, broker: list, dispatched: list) -> dict:
    def dispatch(**kwargs):
        dispatched.append(kwargs)
        return Sent(True)

    args = {
        "done": ["The draft trial binder is in the matter."],
        "decisions": [{"catalog_id": "witness_list_finalize", "question": "Is it final?"}],
    }
    return json.loads(
        casework.casework_brief(
            args,
            session_id=SESSION,
            append=lambda event: broker.append(event) or {"ok": True},
            dispatch=dispatch,
            routine=lambda _s: ("date-prep-brief", None),
            hermes_home=str(tmp_path),
            now=NOW,
        )
    )


def _validate(rows: list[dict]) -> None:
    """Every row the tools wrote passes the broker's own validator in order."""
    stamped: list[dict] = []
    for row in rows:
        casework_ledger.validate_append(
            stamped, row, send_witness=lambda _e: True, audit_witness=lambda _e: True
        )
        stamped.append(casework_ledger.stamp_event(row))


# ---------------------------------------------------------------------------
# casework_step_done
# ---------------------------------------------------------------------------


def test_no_memo_no_step(tmp_path):
    _write(tmp_path, _envelope())
    witness, broker = StepWitness(), []
    _memo(witness, matter="m-other")  # a memo on another matter backs nothing here
    _memo(
        witness,
        call="toolu_failed",
        status="error",
    )
    _memo(witness, call="toolu_err_result", result=json.dumps({"error": "denied"}))
    out = _step_done(tmp_path, broker, witness)
    assert out["status"] == "no_trace" and broker == []


def test_a_memo_backs_one_recorded_step(tmp_path):
    _write(tmp_path, _envelope())
    witness, broker = StepWitness(), []
    _memo(witness)
    out = _step_done(tmp_path, broker, witness)
    assert out["status"] == "recorded"
    [row] = broker
    assert row["event"] == "step_ran" and row["kind"] == "date" and row["item_key"] == K_DATE
    assert row["tool_call_id"] == "toolu_memo"
    assert row["session_id"] == RAW, "the audit row the broker joins carries the raw session id"
    assert row["payload"]["step"]["catalog_id"] == "records_refresh:t-reyes"
    assert row["payload"]["step"]["level"] == "handles"
    assert "done_line" not in row["payload"]["step"]
    _validate(broker)
    # Only one handles entry: nothing left, and the memo is not claimed twice.
    assert _step_done(tmp_path, broker, witness)["status"] == "nothing_left"
    assert len(broker) == 1


def test_the_brief_lists_a_recorded_step_and_marks_it_told(tmp_path):
    earlier = {
        "item_key": K_EARLIER,
        "matter_id": MATTER,
        "kind": "date",
        "source_id": "ev-0",
        "line": "matter 2026-PI-105: on 2026-09-24 I refreshed the motion calendar",
    }
    _write(tmp_path, _envelope(done_since=[earlier]))
    witness, broker, dispatched = StepWitness(), [], []
    _memo(witness)
    _step_done(tmp_path, broker, witness)
    out = _brief(tmp_path, broker, dispatched)
    assert out["status"] == "sent"
    assert dispatched[0]["text"].startswith(
        "Done:\n- " + DONE_LINE + "\n- The draft trial binder is in the matter.\n\n"
        "Done since last time: matter 2026-PI-105: on 2026-09-24 I refreshed the motion calendar.\n\n"
    )
    told = [(e["item_key"], e["kind"]) for e in broker if e["event"] == "mentioned"]
    assert told == [(K_EARLIER, "date"), (K_DATE, "date")]


def test_an_unrecorded_step_is_never_listed(tmp_path):
    _write(tmp_path, _envelope())
    broker, dispatched = [], []
    _brief(tmp_path, broker, dispatched)
    assert DONE_LINE not in dispatched[0]["text"]
    assert not [e for e in broker if e["event"] == "mentioned"]


def test_a_done_line_only_rides_a_handles_step(tmp_path):
    envelope = _envelope()
    envelope["catalog"][1]["done_line"] = "I staged a finalized witness list for review"
    _write(tmp_path, envelope)
    out = _step_done(tmp_path, [], StepWitness())
    assert out["status"] == "no_envelope", "a prepares step with a done line refuses the envelope"


def test_the_task_review_tells_a_date_step_once(tmp_path):
    step_row = {
        "item_key": K_EARLIER,
        "matter_id": MATTER,
        "kind": "date",
        "source_id": "ev-0",
        "line": "matter 2026-PI-105: on 2026-09-24 I refreshed the motion calendar",
    }
    envelope = {
        "skill": "task-list-keeper",
        "started_at": (NOW - timedelta(minutes=1)).isoformat(),
        "messages": [
            {
                "recipients": ["atty@firm.example"],
                "subject": "[Tasks] 1 task to review on your matters",
                "items": [
                    {
                        "n": 1,
                        "line": "A task due 2026-09-01. Suggest: close it.",
                        "event": "proposed",
                        "item_key": casework_ledger.item_key(
                            matter_id=MATTER, kind="task", source_id="t-1"
                        ),
                        "matter_id": MATTER,
                        "task_id": "t-1",
                        "payload": {"action": "close", "class": "stale", "staff_id": "s-1"},
                    }
                ],
                "done_since": [step_row],
            }
        ],
    }
    directory = tmp_path / ".smd" / "pre_run"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "task-list-keeper.casework.json").write_text(json.dumps(envelope))
    broker, dispatched = [], []

    def dispatch(**kwargs):
        dispatched.append(kwargs)
        return Sent(True)

    out = json.loads(
        casework.casework_finish(
            session_id=SESSION,
            append=lambda event: broker.append(event) or {"ok": True},
            dispatch=dispatch,
            routine=lambda _s: ("task-list-keeper", None),
            hermes_home=str(tmp_path),
            now=NOW,
        )
    )
    assert out["status"] == "sent"
    assert "Done since last time: matter 2026-PI-105: on 2026-09-24" in dispatched[0]["text"]
    [told] = [e for e in broker if e["event"] == "mentioned"]
    assert (told["kind"], told["source_id"], told["item_key"]) == ("date", "ev-0", K_EARLIER)


@pytest.mark.parametrize(
    "row",
    [
        {"item_key": K_EARLIER, "matter_id": MATTER, "kind": "date", "source_id": "ev-9"},
        {"item_key": K_EARLIER, "matter_id": MATTER, "kind": "event", "source_id": "ev-0"},
        {
            "item_key": K_EARLIER,
            "matter_id": MATTER,
            "kind": "date",
            "source_id": "ev-0",
            "task_id": "ev-0",
        },
    ],
)
def test_a_miskeyed_done_row_refuses_the_envelope(tmp_path, row):
    _write(tmp_path, _envelope(done_since=[{**row, "line": "x"}]))
    assert _step_done(tmp_path, [], StepWitness())["status"] == "no_envelope"


# ---------------------------------------------------------------------------
# The daily digest (shared/prerendered_dispatch.py + shared/casework_mentions.py)
# ---------------------------------------------------------------------------

DIGEST_SESSION = "cron_deadline-miss-escalator_20260925_070026"
MENTION = {"item_key": K_EARLIER, "matter_id": MATTER, "kind": "date", "source_id": "ev-0"}


def _digest(tmp_path, monkeypatch, results, **entry_over):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    identity = cron_attribution.RoutineIdentity(
        job_id="j1",
        job_name="op-managed:operator:deadline-miss-escalator",
        persona=None,
        skill="deadline-miss-escalator",
    )
    monkeypatch.setattr(cron_attribution, "resolve_routine", lambda sid: identity)
    monkeypatch.setattr(
        cron_attribution, "parse_cron_session_started_at", lambda sid: datetime.now(timezone.utc)
    )
    monkeypatch.setattr(prerendered_dispatch, "_broker_request", lambda payload: {"ok": True})
    mentions: list = []
    monkeypatch.setattr(
        casework_mentions, "broker_append", lambda event: mentions.append(event) or {"ok": True}
    )
    entry = {
        "recipients": ["atty@firm.example"],
        "cc": [],
        "routing_leg": "matter_staff_responsible",
        "subject": "[Deadlines] 1 need you, 2026-09-25",
        "full_body": "Done since last time: matter 2026-PI-105: on 2026-09-24 I x.\n\n1. item\n",
        "skeleton_body": "## Deadline digest (details unavailable)\n",
        "appends": [],
        "casework_mentions": [MENTION],
        **entry_over,
    }
    directory = tmp_path / ".smd" / "pre_run"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "skill": "deadline-miss-escalator",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dispatches": [entry],
    }
    (directory / "deadline-miss-escalator.dispatch.json").write_text(json.dumps(payload))
    results = list(results)
    send_dispatch.set_sender(lambda **kw: results.pop(0))
    try:
        note = prerendered_dispatch.dispatch_prerendered(DIGEST_SESSION)
    finally:
        send_dispatch.set_sender(None)
    return note, mentions


def test_a_full_digest_send_marks_its_done_lines_told(tmp_path, monkeypatch):
    note, mentions = _digest(tmp_path, monkeypatch, [DispatchResult(sent=True, message_id="m1")])
    assert "already delivered" in note
    [row] = mentions
    assert row["event"] == "mentioned" and row["skill"] == "deadline-miss-escalator"
    assert {k: row[k] for k in MENTION} == MENTION
    assert row["session_id"] and row["ts"] is None
    _validate(
        [
            {
                **{k: MENTION[k] for k in MENTION},
                "skill": "date-prep-brief",
                "event": "step_ran",
                "session_id": "s",
                "tool_call_id": "toolu_1",
                "payload": {
                    "action": "step",
                    "class": "open",
                    "step": {
                        "catalog_id": "binder_assemble",
                        "skill": "trial-binder-assembler",
                        "level": "handles",
                        "params": {},
                    },
                },
            },
            row,
        ]
    )


def test_a_skeleton_digest_tells_nothing(tmp_path, monkeypatch):
    results = [DispatchResult(sent=False, reason="refused"), DispatchResult(sent=True)]
    _note, mentions = _digest(tmp_path, monkeypatch, results)
    assert mentions == []


def test_a_miskeyed_mention_refuses_the_digest(tmp_path, monkeypatch):
    bad = {**MENTION, "source_id": "ev-9"}
    note, mentions = _digest(tmp_path, monkeypatch, [], casework_mentions=[bad])
    assert note is None and mentions == []
    assert casework_mentions.valid(None) and not casework_mentions.valid([{**MENTION, "x": 1}])


def test_the_trust_hook_feeds_the_step_witness():
    """The wiring: the trust plugin's post-tool hook is what sees a memo land."""
    from shared import provenance
    from shared.casework_steps import STEP_WITNESS

    hook = load_plugin("hermes-smd-trust")
    session = "sess-witness-wiring"
    resolved = provenance.resolve_session(session)
    STEP_WITNESS.clear(resolved)
    hook.on_post_tool_call(
        tool_name=MEMO_TOOL,
        args={"matter_id": MATTER, "text": "[Operator] Trial binder index assembled"},
        result=json.dumps({"id": "memo-1"}),
        session_id=session,
        tool_call_id="toolu_wired",
    )
    [memo] = STEP_WITNESS.unclaimed(provenance.resolve_session(session), MATTER)
    assert memo.call_id == "toolu_wired" and memo.raw_session == session
    STEP_WITNESS.clear(provenance.resolve_session(session))
