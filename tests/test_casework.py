"""Case-manager tools: the task review, the date-prep brief, and the reply to either.

The contract sentences (ss-console ``docs/specs/operator/case-manager-deadline-work.md``):

* a reply "yes except 2" to a task review writes ``approved`` for every line but 2
  and ``held`` for 2, and the task writes that follow are EXACTLY the approved
  tasks, taken from the raise rows, never from the model;
* a close the firm's level permits happens before the review is rendered, and
  the review says "Closed just now" only for what actually closed;
* a brief carries at most two decisions, each from the envelope's closed catalog;
* every refusal writes nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from shared import casework_acts, casework_ledger, inbound, send_dispatch
from shared.casework_acts import CASEWORK_ACTS, TaskWrite
from tests.conftest import load_plugin

plugin = load_plugin("hermes-smd-escalation")
casework = plugin.casework
reply_items = plugin.reply_items

SESSION = "cron_job-tlk_20260928_080500"
REPLY_SESSION = "sess-reply-cw"
THREAD = "thread-review-0928"
REF = "e" * 32
ATTORNEY = "atty@firm.example"
PARALEGAL = "para@firm.example"
NOW = datetime(2026, 9, 28, 15, 6, tzinfo=timezone.utc)


def _key(task: str, matter: str = "m-104", kind: str = "task") -> str:
    return casework_ledger.item_key(matter_id=matter, kind=kind, source_id=task)


K_CLOSE = _key("t-close-1")
K1, K2, K3, K9 = _key("t-1"), _key("t-2"), _key("t-3"), _key("t-9")
K_OLD = _key("t-old")
K_DATE = _key("ev-1", "m-105", "date")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class Sent:
    def __init__(self, sent: bool = True, reason: str = "") -> None:
        self.sent = sent
        self.reason = reason
        self.message_id = "msg-1" if sent else ""


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(inbound, "SESSION_INBOUND_ORIGIN", inbound.SessionInboundOrigin())
    for session in (SESSION, REPLY_SESSION):
        CASEWORK_ACTS.clear(session)
    from plugin_hermes_smd_escalation import casework_rules

    for store in (casework_rules._FINISH, casework_rules._BRIEFED, casework_rules._REPLIES):
        for session in (SESSION, REPLY_SESSION):
            store.pop(session)
    rows: list[dict] = []
    CASEWORK_ACTS.set_writer(lambda event: rows.append(event) or {"ok": True})
    yield rows
    CASEWORK_ACTS.set_writer(None)


@pytest.fixture
def rows(_clean):
    return _clean


def _review_envelope(**overrides) -> dict:
    envelope = {
        "skill": "task-list-keeper",
        "started_at": (NOW - timedelta(minutes=1)).isoformat(),
        "messages": [
            {
                "recipients": [ATTORNEY],
                "cc": [PARALEGAL],
                "subject": "Task review: 2 overdue tasks on 2026-PI-104",
                "lead": "Two overdue tasks on your matters look ready to tidy up.",
                "closes": [
                    {
                        "item_key": K_CLOSE,
                        "matter_id": "m-104",
                        "task_id": "t-close-1",
                        "staff_id": "staff-atty",
                        "evidence": ["Proof of Service 2026-06-18"],
                        "line": "2026-PI-104: Serve discovery responses",
                    }
                ],
                "items": [
                    {
                        "n": 1,
                        "group": "2026-PI-104",
                        "line": "Discovery follow-up. Suggest: close (proof of service on file).",
                        "event": "proposed",
                        "item_key": K1,
                        "matter_id": "m-104",
                        "task_id": "t-1",
                        "payload": {
                            "action": "close",
                            "staff_id": "staff-atty",
                            "to_staff_id": None,
                            "class": "done",
                            "reason": "proof of service on file",
                            "evidence": ["2026-06-18"],
                        },
                    },
                    {
                        "n": 2,
                        "group": "2026-PI-104",
                        "line": "Records request. Suggest: keep.",
                        "event": "proposed",
                        "item_key": K2,
                        "matter_id": "m-104",
                        "task_id": "t-2",
                        "payload": {
                            "action": "keep",
                            "staff_id": "staff-atty",
                            "class": "open",
                            "reason": "still open",
                            "evidence": [],
                        },
                    },
                ],
                "done_since": [
                    {
                        "item_key": K_OLD,
                        "matter_id": "m-104",
                        "task_id": "t-old",
                        "line": "Closed the July intake task",
                    }
                ],
            }
        ],
    }
    envelope.update(overrides)
    return envelope


def _write_envelope(tmp_path, suffix: str, payload: dict, skill: str) -> None:
    directory = tmp_path / ".smd" / "pre_run"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{skill}.{suffix}.json").write_text(json.dumps(payload), encoding="utf-8")


def _finish(tmp_path, dispatched: list, broker: list, *, sent: bool = True) -> dict:
    def dispatch(**kwargs):
        dispatched.append(kwargs)
        return Sent(sent)

    return json.loads(
        casework.casework_finish(
            session_id=SESSION,
            append=lambda event: broker.append(event) or {"ok": True},
            dispatch=dispatch,
            routine=lambda _s: ("task-list-keeper", None),
            hermes_home=str(tmp_path),
            now=NOW,
        )
    )


# ---------------------------------------------------------------------------
# parse_reply_verdicts (pure)
# ---------------------------------------------------------------------------


def _verdicts(text):
    v = reply_items.parse_reply_verdicts(text)
    return (sorted(v["approve"]), sorted(v["hold"]), v["all"], v["conflict"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes to all", ([], [], True, False)),
        ("All", ([], [], True, False)),
        ("yes except 3", ([], [3], True, False)),
        ("all except 3", ([], [3], True, False)),
        ("all but 2", ([], [2], True, False)),
        ("yes to all except 2 and 4", ([], [2, 4], True, False)),
        ("leave 2", ([], [2], False, False)),
        ("keep 2, close the rest? no, just 1", ([1], [2], False, False)),
        ("not 3", ([], [3], False, False)),
        ("skip 4", ([], [4], False, False)),
        ("hold 5", ([], [5], False, False)),
        ("yes on 1", ([1], [], False, False)),
        ("1", ([1], [], False, False)),
        ("yes on 1, leave 2", ([1], [2], False, False)),
        ("leave 2 yes on 3", ([3], [2], False, False)),
        ("yes on 2, leave 2", ([2], [2], False, True)),
        ("Hi all, yes on 2", ([2], [], False, False)),
        # A leaked quoted list is nothing at all.
        ("yes\n> 1. 2026-PI-104: Discovery follow-up", ([], [], False, False)),
        ("thanks\n1. matter 2026-PI-101, due 2026-09-20", ([], [], False, False)),
        # A signature's digits are not lines.
        ("yes on 1\n\nThanks,\nDana\nSuite 200\n(602) 555-1234", ([1], [], False, False)),
        ("2\n-- \nDana | 602 555 1234", ([2], [], False, False)),
        ("call me at 602-555-1234", ([], [], False, False)),
    ],
)
def test_parse_reply_verdicts(text, expected):
    assert _verdicts(text) == expected


@pytest.mark.parametrize("text", ["yes", "Yes please", "ok", "sounds good", "go ahead."])
def test_a_bare_yes_is_flagged(text):
    assert reply_items.parse_reply_verdicts(text)["bare_yes"] is True


@pytest.mark.parametrize("text", ["yes on 1", "yes to all", "thanks", ""])
def test_not_a_bare_yes(text):
    assert reply_items.parse_reply_verdicts(text)["bare_yes"] is False


def test_the_wrapper_is_approve_or_all_minus_holds():
    assert reply_items.parse_reply_items("yes except 3") == {
        "all": True,
        "numbers": [],
        "except": [3],
    }
    assert reply_items.parse_reply_items("yes on 2, leave 2") == {"all": False, "numbers": []}
    assert reply_items.parse_reply_items("1, not 2") == {"all": False, "numbers": [1]}


# ---------------------------------------------------------------------------
# The replay register
# ---------------------------------------------------------------------------


def _write(task_id="t-1", key=K1) -> TaskWrite:
    return TaskWrite(
        task_id=task_id,
        staff_id="staff-atty",
        item_key=key,
        matter_id="m-104",
        skill="task-list-keeper",
        is_completed=True,
    )


def test_a_session_with_no_queue_is_untouched(rows):
    args = {"task_id": "model-chosen", "is_completed": True}
    replay = CASEWORK_ACTS.replay("other-session", casework_acts.UPDATE_TASK_TOOL, args)
    assert replay.act is None and replay.refusal is None
    assert args == {"task_id": "model-chosen", "is_completed": True}


def test_the_model_task_id_is_replaced_by_the_queue_head(rows):
    CASEWORK_ACTS.load(SESSION, [_write()])
    args = {"task_id": "model-chosen", "subject": "rename it", "staff_id": "someone"}
    replay = CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, args)
    assert replay.act is not None
    assert args == {"task_id": "t-1", "staff_id": "staff-atty", "is_completed": True}


def test_a_call_beyond_the_queue_is_refused(rows):
    CASEWORK_ACTS.load(SESSION, [_write()])
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    CASEWORK_ACTS.on_post_tool(casework_acts.UPDATE_TASK_TOOL, SESSION, {"tool_call_id": "tc-1"})
    replay = CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {"task_id": "t-9"})
    assert replay.refusal == casework_acts.BEYOND_QUEUE


def test_post_tool_writes_completed_with_its_call_id(rows):
    CASEWORK_ACTS.load(SESSION, [_write()])
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    CASEWORK_ACTS.on_post_tool(
        casework_acts.UPDATE_TASK_TOOL, SESSION, {"tool_call_id": "tc-1", "result": "{}"}
    )
    [row] = rows
    assert row["event"] == "completed"
    assert row["tool_call_id"] == "tc-1"
    assert row["kind"] == "task" and row["source_id"] == "t-1"
    assert row["item_key"] == K1 and row["ts"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "error"},
        {"error_type": "ToolError"},
        {"result": json.dumps({"error": "400 Bad Request"})},
    ],
)
def test_a_failed_call_writes_write_failed(rows, kwargs):
    CASEWORK_ACTS.load(SESSION, [_write()])
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    CASEWORK_ACTS.on_post_tool(casework_acts.UPDATE_TASK_TOOL, SESSION, kwargs)
    assert [r["event"] for r in rows] == ["write_failed"]


def test_an_outcome_that_never_arrived_is_not_a_success(rows):
    CASEWORK_ACTS.load(SESSION, [_write("t-1", K1), _write("t-2", K2)])
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    assert [(r["item_key"], r["event"]) for r in rows] == [(K1, "write_failed")]


def test_a_malformed_write_is_never_queued(rows):
    bad = TaskWrite(
        task_id="t-1",
        staff_id="s",
        item_key="k",
        matter_id=None,
        skill="x",
        is_completed=True,
        assignee_ids=("s2",),
    )
    assert CASEWORK_ACTS.load(SESSION, [bad]) == 0


# ---------------------------------------------------------------------------
# Through the trust gate
# ---------------------------------------------------------------------------


trust = load_plugin("hermes-smd-trust")
enforce = trust.enforce


def test_the_gate_replays_and_a_refused_ceiling_is_write_failed(rows, monkeypatch):
    CASEWORK_ACTS.load(SESSION, [_write()])
    monkeypatch.setattr(
        enforce,
        "_evaluate_tool_call",
        lambda *a, **k: {"action": "block", "message": "Refused: internal_write is refused"},
    )
    args = {"task_id": "model-chosen"}
    verdict = enforce.evaluate_tool_call(
        casework_acts.UPDATE_TASK_TOOL, args, "pilot", session_id=SESSION
    )
    assert verdict["action"] == "block"
    assert args["task_id"] == "t-1"
    assert [r["event"] for r in rows] == ["write_failed"]


def test_the_gate_refuses_beyond_the_queue(rows, monkeypatch):
    CASEWORK_ACTS.load(SESSION, [_write()])
    monkeypatch.setattr(enforce, "_evaluate_tool_call", lambda *a, **k: None)
    assert (
        enforce.evaluate_tool_call(casework_acts.UPDATE_TASK_TOOL, {}, "p", session_id=SESSION)
        is None
    )
    trust.on_post_tool_call(
        tool_name=casework_acts.UPDATE_TASK_TOOL,
        session_id=SESSION,
        tool_call_id="tc-7",
        result="{}",
    )
    verdict = enforce.evaluate_tool_call(
        casework_acts.UPDATE_TASK_TOOL, {"task_id": "t-9"}, "p", session_id=SESSION
    )
    assert verdict["action"] == "block"
    assert "no task update waiting" in verdict["message"]
    assert [r["event"] for r in rows] == ["completed"]


def test_other_tools_are_untouched_by_a_queue(rows, monkeypatch):
    CASEWORK_ACTS.load(SESSION, [_write()])
    seen = {}
    monkeypatch.setattr(
        enforce, "_evaluate_tool_call", lambda name, args, *a, **k: seen.update(args)
    )
    enforce.evaluate_tool_call("mcp_smokeball_create_task", {"subject": "x"}, "p", SESSION)
    assert seen == {"subject": "x"}
    assert CASEWORK_ACTS.pending(SESSION) == 1


# ---------------------------------------------------------------------------
# casework_finish
# ---------------------------------------------------------------------------


def test_finish_closes_first_then_renders_and_sends(tmp_path, rows):
    _write_envelope(tmp_path, "casework", _review_envelope(), "task-list-keeper")
    dispatched: list = []
    broker: list = []
    first = _finish(tmp_path, dispatched, broker)
    assert first["status"] == "writes_queued" and first["writes"] == 1
    assert [e["event"] for e in broker] == ["closed_by_record"]
    assert dispatched == []
    # The consumed envelope cannot be taken again.
    assert not (tmp_path / ".smd/pre_run/task-list-keeper.casework.json").exists()

    args = {"task_id": "whatever"}
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, args)
    assert args == {"task_id": "t-close-1", "staff_id": "staff-atty", "is_completed": True}
    CASEWORK_ACTS.on_post_tool(casework_acts.UPDATE_TASK_TOOL, SESSION, {"tool_call_id": "tc"})

    second = _finish(tmp_path, dispatched, broker)
    assert second["status"] == "sent"
    [message] = dispatched
    assert message["to"] == [ATTORNEY] and message["cc"] == [PARALEGAL]
    assert message["templated"] is True
    assert message["text"] == (
        "Two overdue tasks on your matters look ready to tidy up.\n\n"
        "Done since last time: Closed the July intake task.\n\n"
        "Closed just now:\n- 2026-PI-104: Serve discovery responses\n\n"
        "2026-PI-104\n"
        "1. Discovery follow-up. Suggest: close (proof of service on file).\n"
        "2. Records request. Suggest: keep.\n\n"
        'Reply here in words, for example "yes to all", "all except 3" or "leave 2".'
    )
    ref = message["audit_extra"]["dispatch_ref"]
    raises = [e for e in broker if e["event"] == "proposed"]
    assert [(e["item_key"], e["n"], e["dispatch_ref"]) for e in raises] == [
        (K1, 1, ref),
        (K2, 2, ref),
    ]
    assert all("thread_ref" not in e for e in broker)
    assert sorted(e["item_key"] for e in broker if e["event"] == "mentioned") == sorted(
        [K_CLOSE, K_OLD]
    )
    assert all("dispatch_ref" not in e for e in broker if e["event"] == "mentioned")
    assert _finish(tmp_path, dispatched, broker)["status"] == "already_sent"


def test_a_failed_close_is_not_reported_closed(tmp_path, rows):
    _write_envelope(tmp_path, "casework", _review_envelope(), "task-list-keeper")
    dispatched: list = []
    broker: list = []
    _finish(tmp_path, dispatched, broker)
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    CASEWORK_ACTS.on_post_tool(casework_acts.UPDATE_TASK_TOOL, SESSION, {"status": "error"})
    _finish(tmp_path, dispatched, broker)
    text = dispatched[0]["text"]
    assert "Closed just now" not in text
    assert (
        "I couldn't update these in Smokeball just now: 2026-PI-104: Serve discovery responses."
        in text
    )
    assert K_CLOSE not in [e["item_key"] for e in broker if e["event"] == "mentioned"]


def test_no_decision_no_message(tmp_path, rows):
    envelope = _review_envelope()
    envelope["messages"][0]["items"] = []
    _write_envelope(tmp_path, "casework", envelope, "task-list-keeper")
    dispatched: list = []
    broker: list = []
    _finish(tmp_path, dispatched, broker)
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    CASEWORK_ACTS.on_post_tool(casework_acts.UPDATE_TASK_TOOL, SESSION, {})
    out = _finish(tmp_path, dispatched, broker)
    assert out["status"] == "sent" and out["messages"] == []
    assert dispatched == []
    assert [e["event"] for e in broker] == ["closed_by_record"]


def test_an_unsent_review_writes_no_raise(tmp_path, rows):
    envelope = _review_envelope()
    envelope["messages"][0]["closes"] = []
    _write_envelope(tmp_path, "casework", envelope, "task-list-keeper")
    dispatched: list = []
    broker: list = []
    out = _finish(tmp_path, dispatched, broker, sent=False)
    assert out["messages"][0]["sent"] is False
    assert broker == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["messages"][0]["items"][0]["payload"].__setitem__("class", "at_stake"),
        lambda e: e["messages"][0]["items"][1].update(n=1),
        lambda e: e["messages"][0]["items"][0].update(line="Close it — done"),
        lambda e: e["messages"][0].update(recipients=[]),
        lambda e: e["messages"][0]["items"][0]["payload"].update(action="delete"),
        lambda e: e.update(started_at=(NOW - timedelta(hours=2)).isoformat()),
        lambda e: e.update(skill="someone-else"),
    ],
)
def test_a_bad_envelope_sends_and_writes_nothing(tmp_path, rows, mutate):
    envelope = _review_envelope()
    mutate(envelope)
    _write_envelope(tmp_path, "casework", envelope, "task-list-keeper")
    dispatched: list = []
    broker: list = []
    assert _finish(tmp_path, dispatched, broker)["status"] == "no_envelope"
    assert dispatched == [] and broker == []


# ---------------------------------------------------------------------------
# casework_brief
# ---------------------------------------------------------------------------


def _brief_envelope() -> dict:
    return {
        "skill": "date-prep-brief",
        "started_at": (NOW - timedelta(minutes=1)).isoformat(),
        "matter_id": "m-105",
        "matter_number": "2026-PI-105",
        "event_id": "ev-1",
        "item_key": K_DATE,
        "subject_label": "2026-PI-105: status conference Fri Oct 2",
        "recipients": [ATTORNEY],
        "cc": [PARALEGAL],
        "catalog": [
            {
                "catalog_id": "witness_list_finalize",
                "skill": "trial-binder-assembler",
                "level": "prepares",
                "params": {"document": "witness list"},
            },
            {
                "catalog_id": "records_refresh:reyes",
                "skill": "medical-records-chaser",
                "level": "handles",
                "params": {"provider": "reyes"},
            },
        ],
    }


def _brief(tmp_path, args, dispatched, broker):
    def dispatch(**kwargs):
        dispatched.append(kwargs)
        return Sent(True)

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


_ARGS = {
    "done": ["The draft trial binder is in the matter."],
    "decisions": [
        {"catalog_id": "witness_list_finalize", "question": "Is the June 18 witness list final?"},
        {"catalog_id": "records_refresh:reyes", "question": "Request updated records?"},
    ],
}


def test_the_brief_is_framed_in_code(tmp_path, rows):
    _write_envelope(tmp_path, "brief", _brief_envelope(), "date-prep-brief")
    dispatched: list = []
    broker: list = []
    out = _brief(tmp_path, _ARGS, dispatched, broker)
    assert out["status"] == "sent"
    [message] = dispatched
    assert message["subject"] == "2026-PI-105: status conference Fri Oct 2, two questions for you"
    assert message["templated"] is False
    assert message["text"] == (
        "Done:\n- The draft trial binder is in the matter.\n\n"
        "Needs you:\n1. Is the June 18 witness list final?\n2. Request updated records?\n\n"
        "Reply here and I'll take it from there."
    )
    briefed = [e for e in broker if e["event"] == "briefed"]
    assert [(e["n"], e["payload"]["step"]["catalog_id"]) for e in briefed] == [
        (1, "witness_list_finalize"),
        (2, "records_refresh:reyes"),
    ]
    assert all(e["item_key"] == K_DATE and "thread_ref" not in e for e in briefed)
    again = _brief(tmp_path, _ARGS, dispatched, broker)
    assert again["status"] == "refused" and len(dispatched) == 1


@pytest.mark.parametrize(
    ("args", "phrase"),
    [
        ({"decisions": [{"catalog_id": "invented", "question": "?"}]}, "not in this run's"),
        ({"decisions": []}, "at least one decision"),
        (
            {"decisions": [{"catalog_id": "witness_list_finalize", "question": "q"}] * 3},
            "at most 2",
        ),
        (
            {
                "decisions": [
                    {"catalog_id": "witness_list_finalize", "question": "q"},
                    {"catalog_id": "witness_list_finalize", "question": "q2"},
                ]
            },
            "asked twice",
        ),
        (
            {
                "done": ["x" * 121],
                "decisions": [{"catalog_id": "witness_list_finalize", "question": "q"}],
            },
            "at most 120",
        ),
        (
            {"decisions": [{"catalog_id": "witness_list_finalize", "question": "Final — yes?"}]},
            "no long dashes",
        ),
    ],
)
def test_a_bad_brief_is_refused_and_sends_nothing(tmp_path, rows, args, phrase):
    _write_envelope(tmp_path, "brief", _brief_envelope(), "date-prep-brief")
    dispatched: list = []
    broker: list = []
    out = _brief(tmp_path, args, dispatched, broker)
    assert out["status"] == "refused" and phrase in out["note"]
    assert dispatched == [] and broker == []


# ---------------------------------------------------------------------------
# reply_verdicts
# ---------------------------------------------------------------------------


class _Config:
    def sender_on_roster(self, address):
        return address == ATTORNEY


def _raise_row(n, key, task, action="close", *, event="proposed", thread=THREAD, ref=REF, **p):
    payload = {"action": action, "staff_id": "staff-atty", "class": "done", **p}
    return {
        "ts": "2026-09-28T15:07:00Z",
        "skill": "task-list-keeper",
        "matter_id": "m-104",
        "item_key": key,
        "event": event,
        "n": n,
        "kind": "task" if task else "date",
        "source_id": task or "ev-1",
        "dispatch_ref": ref,
        "thread_ref": thread,
        "payload": payload,
    }


def _ledger(tmp_path, rows_):
    path = tmp_path / "casework-ledger.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows_), encoding="utf-8")
    return str(path)


def _reply_origin(text, *, thread=THREAD, address=ATTORNEY):
    inbound.SESSION_INBOUND_ORIGIN.record(
        REPLY_SESSION,
        inbound.InboundOrigin(
            sender_address=address,
            message_id="msg-r",
            conversation_id=thread,
            reply_text=text,
        ),
    )


def _answer(path, broker, fallthrough=lambda: json.dumps({"status": "digest"})):
    return json.loads(
        casework.reply_verdicts(
            session_id=REPLY_SESSION,
            load_config=_Config,
            verified_acker=lambda _s: {"name": "Dana Whitfield", "key": "k" * 64},
            append=lambda event: broker.append(event) or {"ok": True},
            fallthrough=fallthrough,
            casework_ledger_path=path,
        )
    )


def _three(tmp_path):
    return _ledger(
        tmp_path,
        [
            _raise_row(1, K1, "t-1"),
            _raise_row(2, K2, "t-2"),
            _raise_row(3, K3, "t-3", "reassign", to_staff_id="staff-para"),
        ],
    )


def test_yes_except_2_changes_exactly_the_approved_tasks(tmp_path, rows):
    path = _three(tmp_path)
    _reply_origin("yes except 2")
    broker: list = []
    out = _answer(path, broker)
    assert out["status"] == "writes_queued" and out["writes"] == 2
    assert [(e["event"], e.get("n"), e["item_key"]) for e in broker] == [
        ("approved", 1, K1),
        ("held", 2, K2),
        ("kept", None, K2),
        ("approved", 3, K3),
    ]
    verdicts = [e for e in broker if e["event"] in ("approved", "held")]
    assert all(e["decided_by"]["name"] == "Dana Whitfield" for e in verdicts)
    assert all(e["thread_ref"] == THREAD for e in verdicts)
    assert all("dispatch_ref" not in e and "step" not in e for e in verdicts)
    executed = []
    for _ in range(2):
        args = {"task_id": "model-chosen"}
        CASEWORK_ACTS.replay(REPLY_SESSION, casework_acts.UPDATE_TASK_TOOL, args)
        executed.append(args)
        CASEWORK_ACTS.on_post_tool(
            casework_acts.UPDATE_TASK_TOOL, REPLY_SESSION, {"tool_call_id": "tc"}
        )
    assert executed == [
        {"task_id": "t-1", "staff_id": "staff-atty", "is_completed": True},
        {"task_id": "t-3", "staff_id": "staff-atty", "assignee_ids": ["staff-para"]},
    ]
    assert (
        CASEWORK_ACTS.replay(REPLY_SESSION, casework_acts.UPDATE_TASK_TOOL, {}).refusal is not None
    )
    final = _answer(path, broker)
    assert final["status"] == "done"
    assert final["confirmation_text"] == "Got it. Closed 1. Reassigned 3. Leaving 2 as it is."


def test_a_failed_write_is_named_in_the_confirmation(tmp_path, rows):
    path = _three(tmp_path)
    _reply_origin("yes on 1")
    broker: list = []
    _answer(path, broker)
    CASEWORK_ACTS.replay(REPLY_SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    CASEWORK_ACTS.on_post_tool(casework_acts.UPDATE_TASK_TOOL, REPLY_SESSION, {"status": "failed"})
    final = _answer(path, broker)
    assert final["confirmation_text"] == (
        "Got it. I couldn't update 1 in Smokeball just now, so it is unchanged."
    )


@pytest.mark.parametrize(
    ("text", "status", "phrase"),
    [
        ("yes on 1 and 9", "unknown_numbers", "I don't see 9 on that list"),
        ("yes on 2, leave 2", "conflict", "both yes and leave it"),
        ("yes", "nothing_parsed", "Which ones should I go ahead with"),
        ("thanks", "nothing_parsed", "Which ones"),
    ],
)
def test_ambiguity_writes_nothing_and_asks(tmp_path, rows, text, status, phrase):
    path = _three(tmp_path)
    _reply_origin(text)
    broker: list = []
    out = _answer(path, broker)
    assert out["status"] == status and phrase in out["confirmation_text"]
    assert broker == [] and CASEWORK_ACTS.pending(REPLY_SESSION) == 0


def test_a_bare_yes_to_a_one_line_list_approves_it(tmp_path, rows):
    path = _ledger(tmp_path, [_raise_row(1, K1, "t-1")])
    _reply_origin("yes please")
    broker: list = []
    assert _answer(path, broker)["writes"] == 1


def test_a_thread_with_two_lists_is_ambiguous(tmp_path, rows):
    path = _ledger(tmp_path, [_raise_row(1, K1, "t-1"), _raise_row(1, K9, "t-9", ref="f" * 32)])
    _reply_origin("yes on 1")
    broker: list = []
    assert _answer(path, broker)["status"] == "ambiguous_thread"
    assert broker == []


def test_a_thread_with_no_casework_rows_falls_through(tmp_path, rows):
    path = _ledger(tmp_path, [_raise_row(1, K1, "t-1", thread="another-thread")])
    _reply_origin("got it on 1")
    assert _answer(path, [])["status"] == "digest"


def test_an_off_roster_sender_gets_nothing(tmp_path, rows):
    path = _three(tmp_path)
    _reply_origin("yes to all", address="stranger@elsewhere.example")
    broker: list = []
    out = _answer(path, broker)
    assert out["status"] == "not_rostered" and out["confirmation_text"] == ""
    assert broker == []


def test_an_answered_line_is_not_acted_on_twice(tmp_path, rows):
    rows_ = [_raise_row(1, K1, "t-1"), _raise_row(2, K2, "t-2")]
    rows_.append(
        {
            "ts": "2026-09-28T16:00:00Z",
            "event": "approved",
            "item_key": K1,
            "n": 1,
            "thread_ref": THREAD,
            "skill": "x",
        }
    )
    path = _ledger(tmp_path, rows_)
    _reply_origin("yes to all")
    broker: list = []
    out = _answer(path, broker)
    assert out["writes"] == 1
    assert [e["item_key"] for e in broker] == [K2]


def test_an_approved_step_is_returned_for_the_router(tmp_path, rows):
    step = {
        "catalog_id": "witness_list_finalize",
        "skill": "trial-binder-assembler",
        "level": "prepares",
        "params": {"document": "witness list"},
    }
    path = _ledger(
        tmp_path,
        [
            _raise_row(1, K_DATE, None, "step", event="briefed", step=step),
            _raise_row(2, K_DATE, None, "step", event="briefed", step={**step, "level": "handles"}),
        ],
    )
    _reply_origin("yes on 1, leave 2")
    broker: list = []
    out = _answer(path, broker)
    assert out["status"] == "done"
    assert out["steps_to_run"] == [
        {
            "n": 1,
            "catalog_id": "witness_list_finalize",
            "skill": "trial-binder-assembler",
            "level": "prepares",
            "matter_id": "m-104",
            "params": {"document": "witness list"},
        }
    ]
    assert out["confirmation_text"] == (
        "Got it. I'll prepare 1 and send it to you for review. Leaving 2 as it is."
    )
    approved = next(e for e in broker if e["event"] == "approved")
    assert approved["n"] == 1 and approved["kind"] == "date" and "step" not in approved


def test_the_tool_schemas_take_no_ids():
    assert casework.EMPTY_SCHEMA["properties"] == {}
    brief = casework.BRIEF_SCHEMA["properties"]["decisions"]["items"]["properties"]
    assert set(brief) == {"catalog_id", "question"}


def test_no_rendered_text_carries_an_em_dash(tmp_path, rows):
    subject, body = casework.render_brief(
        _brief_envelope(), ["done"], [{"catalog_id": "x", "question": "q"}]
    )
    review = casework.render_review(_review_envelope()["messages"][0], [], [])
    for text in (subject, body, review, casework.FINISH_DESCRIPTION, casework.REPLY_DESCRIPTION):
        assert "—" not in text


def test_the_published_sender_is_the_default_dispatch():
    assert casework.casework_finish.__kwdefaults__["dispatch"] is send_dispatch.dispatch


def test_memos_come_back_to_the_turn_once_sent(tmp_path, rows):
    memos = [
        {"matter_id": "m-104", "text": "Closed the July intake task: proof of service on file."}
    ]
    envelope = _review_envelope(memos=memos)
    envelope["messages"][0]["closes"] = []
    _write_envelope(tmp_path, "casework", envelope, "task-list-keeper")
    out = _finish(tmp_path, [], [])
    assert out["status"] == "sent" and out["memos"] == memos
    assert "create_memo" in out["note"]


def test_a_malformed_memo_refuses_the_envelope(tmp_path, rows):
    envelope = _review_envelope(memos=[{"matter_id": "m-104", "text": "x", "extra": 1}])
    _write_envelope(tmp_path, "casework", envelope, "task-list-keeper")
    assert _finish(tmp_path, [], [])["status"] == "no_envelope"


def test_a_close_may_carry_its_payload_whole(tmp_path, rows):
    envelope = _review_envelope()
    close = envelope["messages"][0]["closes"][0]
    evidence = close.pop("evidence")
    close["payload"] = {
        "action": "close",
        "class": "done",
        "staff_id": "staff-atty",
        "to_staff_id": None,
        "reason": "document_on_file",
        "evidence": evidence,
    }
    _write_envelope(tmp_path, "casework", envelope, "task-list-keeper")
    broker: list = []
    assert _finish(tmp_path, [], broker)["status"] == "writes_queued"
    assert broker[0]["payload"] == close["payload"]
