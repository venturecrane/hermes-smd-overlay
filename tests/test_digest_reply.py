"""A person says a deadline-digest line is done, and the task closes in Smokeball.

The contract sentences (ss-console plan "completion by reply", 2026-10-01):

* "done with 1" on a line the digest raised as closable writes ``approved``
  under the replier's authored name, queues exactly that task's write, and the
  confirmation names what closed;
* "got it on 1" and a bare "1" still only quiet the item;
* a completion on a date, a group, or an unraised task quiets it and says why
  in words; a replier with no authored name closes nothing;
* "not done with 1", "I'll close 1 after the FSC" close nothing and are asked
  about; clear numbers in the same reply are still acted on;
* a bare "done" on a one-line digest closes it; on a longer one it asks;
* the first call writes every ack and approval; the second call only renders;
* ``reply_verdicts`` sends a digest thread here even though it now also holds
  casework raises, so a bare number can never close.
"""

from __future__ import annotations

import json

import pytest

from shared import casework_acts, casework_ledger, inbound, sent_lines
from shared.casework_acts import CASEWORK_ACTS
from tests.conftest import load_plugin

SESSION = "sess-digest-reply-1"
THREAD = "thread-digest-1001"
REF = "e" * 32
ADDRESS = "scott@firm.example"
NAME = "Scott Durgan"
MATTER = "m-105"
TASK = "t-exhibits"
TASK2 = "t-letter"
EVENT = "ev-fsc"
CW_KEY = casework_ledger.item_key(matter_id=MATTER, kind="task", source_id=TASK)
CW_KEY2 = casework_ledger.item_key(matter_id=MATTER, kind="task", source_id=TASK2)

LABELS = {
    1: "2026-PI-105 Okafor: Update exhibit list with Grand Valley incident photos, due Sep 18, 2026",
    2: "2026-PI-105 Okafor: Final Status Conference, Fri Oct 2, 2026",
    3: "2026-PI-101 Chen: 2 more open items",
    4: "2026-PI-105 Okafor: Send preservation letter, due Sep 20, 2026",
}


def _fired(key: str, n: int, matter: str = MATTER, token: str | None = "derived") -> dict:
    return {
        "v": 2,
        "ts": "2026-10-01T14:00:01Z",
        "id": f"id-{key}-{n}",
        "skill": "deadline-miss-escalator",
        "matter_id": matter,
        "item_key": key,
        "event": "fired",
        "attempt": 1,
        "token": f"ACK-{key.upper()[:6]}" if token == "derived" else token,
        "session_id": "cron_deadline-miss-escalator_20261001_070000",
        "thread_ref": THREAD,
        "dispatch_ref": REF,
        "n": n,
        "snooze_days": 7,
    }


def _digest_rows() -> list[dict]:
    """1: a task with a raise; 2: a date; 3: a group of two; 4: a task with no raise."""
    return [
        _fired("aaaaaa", 1),
        _fired("bbbbbb", 2),
        _fired("cccccc", 3, matter="m-101"),
        _fired("dddddd", 3, matter="m-101", token=None),
        _fired("eeeeee", 4),
    ]


def _complete_raise(n: int = 1, key: str = CW_KEY, task: str = TASK, **payload) -> dict:
    return {
        "v": 1,
        "ts": "2026-10-01T14:00:02Z",
        "id": f"cw-{key}",
        "skill": "deadline-miss-escalator",
        "matter_id": MATTER,
        "kind": "task",
        "source_id": task,
        "item_key": key,
        "event": "proposed",
        "session_id": "cron_deadline-miss-escalator_20261001_070000",
        "n": n,
        "dispatch_ref": REF,
        "thread_ref": THREAD,
        "payload": {
            "action": "complete",
            "class": "at_stake",
            "staff_id": "st-scott",
            "to_staff_id": None,
            "reason": "deadline_digest; owner Scott Durgan",
            "evidence": [],
            **payload,
        },
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    plugin = load_plugin("hermes-smd-escalation")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "id": f"evt-{len(requests)}"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    monkeypatch.setattr(inbound, "SESSION_INBOUND_ORIGIN", inbound.SessionInboundOrigin())
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("SMD_SENT_LINES_DIR", str(tmp_path / "sent_lines"))
    esc = tmp_path / "escalation-ledger.jsonl"
    cw = tmp_path / "casework-ledger.jsonl"
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(esc))
    monkeypatch.setenv("SMD_CASEWORK_LEDGER_PATH", str(cw))
    config = {
        "scope": {"inbound_allow_from": [ADDRESS]},
        "users": [{"email": ADDRESS, "full_name": NAME}],
    }
    real = plugin.CustomerConfig

    class _Config:
        @staticmethod
        def from_volume(*_a, **_k):
            return real(config)

    monkeypatch.setattr(plugin, "CustomerConfig", _Config)
    CASEWORK_ACTS.clear(SESSION)
    outcomes: list[dict] = []
    CASEWORK_ACTS.set_writer(lambda event: outcomes.append(event) or {"ok": True})
    from plugin_hermes_smd_escalation import casework_rules

    casework_rules._REPLIES.pop(SESSION)

    class Env:
        pass

    e = Env()
    e.plugin = plugin
    e.requests = requests
    e.outcomes = outcomes
    e.config = config
    e.write_esc = lambda rows: esc.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    e.write_cw = lambda rows: cw.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    e.write_esc(_digest_rows())
    e.write_cw([_complete_raise()])
    sent_lines.record(REF, LABELS, {1: "task", 2: "date", 4: "task"})
    yield e
    CASEWORK_ACTS.set_writer(None)
    CASEWORK_ACTS.clear(SESSION)
    casework_rules._REPLIES.pop(SESSION)


def _reply(text: str, *, address: str = ADDRESS, thread: str = THREAD) -> None:
    inbound.SESSION_INBOUND_ORIGIN.record(
        SESSION,
        inbound.InboundOrigin(
            sender_address=address,
            message_id=f"msg-{SESSION}",
            conversation_id=thread,
            reply_text=text,
        ),
    )


def _ack_call(env) -> dict:
    return json.loads(env.plugin._escalation_reply_ack({}, session_id=SESSION))


def _verdicts_call(env) -> dict:
    return json.loads(env.plugin._reply_verdicts({}, session_id=SESSION))


def _rows(env, action: str, event: str | None = None) -> list[dict]:
    return [
        r["event"]
        for r in env.requests
        if r["action"] == action and (event is None or r["event"]["event"] == event)
    ]


def _land(env) -> None:
    """The queued write runs: the gate replays it and the post-tool hook records it."""
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    CASEWORK_ACTS.on_post_tool(casework_acts.UPDATE_TASK_TOOL, SESSION, {"tool_call_id": "t-1"})


# ---------------------------------------------------------------------------
# The close
# ---------------------------------------------------------------------------


def test_done_with_1_approves_the_raised_line_and_queues_exactly_its_write(env):
    _reply("Done with 1, got it on 2.")
    out = _ack_call(env)
    assert out["status"] == "writes_queued" and out["writes"] == 1
    [approved] = _rows(env, "casework_event_append", "approved")
    assert approved["item_key"] == CW_KEY and approved["n"] == 1
    assert approved["thread_ref"] == THREAD
    assert approved["decided_by"] == {"name": NAME, "key": approved["decided_by"]["key"]}
    assert approved["source_id"] == TASK and approved["kind"] == "task"
    # The ack for 2 was written on the FIRST call, before any write ran.
    [acked] = _rows(env, "escalation_event_append", "acked")
    assert acked["item_key"] == "bbbbbb" and acked["acked_by"]["name"] == NAME
    # The queued write is the raise's task under the raise's owner; the model's
    # own arguments are replaced.
    replay = CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {"task_id": "whatever"})
    assert replay.refusal is None
    assert replay.act.tool_arguments() == {
        "task_id": TASK,
        "staff_id": "st-scott",
        "is_completed": True,
    }
    CASEWORK_ACTS.on_post_tool(casework_acts.UPDATE_TASK_TOOL, SESSION, {"tool_call_id": "t-1"})
    [completed] = env.outcomes
    assert completed["event"] == "completed" and completed["item_key"] == CW_KEY
    final = _ack_call(env)
    assert final["status"] == "done"
    assert final["confirmation_text"] == (
        "Got it. Closed 1 (2026-PI-105 Okafor: Update exhibit list with Grand Valley incident "
        "photos, due Sep 18, 2026). 2 (2026-PI-105 Okafor: Final Status Conference, Fri Oct 2, "
        "2026) is quiet for 7 days. Still open: 3 (2026-PI-101 Chen: 2 more open items) and 4 "
        "(2026-PI-105 Okafor: Send preservation letter, due Sep 20, 2026)."
    )
    assert final["acked"] == [2]


def test_the_confirmation_says_whom_the_close_was_recorded_under_when_it_is_not_the_replier(env):
    env.write_cw([_complete_raise(reason="deadline_digest; owner Amy Ng")])
    _reply("1 is done")
    assert _ack_call(env)["status"] == "writes_queued"
    _land(env)
    text = _ack_call(env)["confirmation_text"]
    assert text.startswith("Got it. Closed 1 (")
    assert "Recorded in Smokeball under Amy Ng." in text


def test_reply_verdicts_sends_a_digest_thread_here_even_with_casework_raises_on_it(env):
    """The routing hole: once digest raises are casework rows, the casework
    path would read a bare "1" as an approval and close the task."""
    _reply("1")
    out = _verdicts_call(env)
    assert out["status"] == "acked"
    assert _rows(env, "casework_event_append") == []
    [acked] = _rows(env, "escalation_event_append", "acked")
    assert acked["item_key"] == "aaaaaa"


def test_reply_verdicts_runs_the_close_and_its_second_call_renders(env):
    _reply("close 1")
    assert _verdicts_call(env)["status"] == "writes_queued"
    _land(env)
    final = _verdicts_call(env)
    assert final["status"] == "done"
    assert final["confirmation_text"].startswith("Got it. Closed 1 (")


def test_a_failed_write_is_named_and_the_task_is_unchanged(env):
    _reply("done with 1")
    assert _ack_call(env)["status"] == "writes_queued"
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    CASEWORK_ACTS.on_post_tool(
        casework_acts.UPDATE_TASK_TOOL, SESSION, {"error": "Smokeball said no"}
    )
    text = _ack_call(env)["confirmation_text"]
    assert text.startswith("Got it. I couldn't update 1 (")
    assert "in Smokeball just now, so it is unchanged." in text


def _approved(raise_row: dict) -> dict:
    return {
        **{k: raise_row[k] for k in ("v", "skill", "matter_id", "kind", "source_id", "item_key")},
        "ts": "2026-10-01T15:00:00Z",
        "id": "cw-approved",
        "event": "approved",
        "n": 1,
        "thread_ref": THREAD,
        "session_id": "sess-earlier",
        "decided_by": {"name": NAME, "key": "k" * 64},
    }


def test_an_already_closed_line_is_not_closed_twice(env):
    raise_row = _complete_raise()
    approved = _approved(raise_row)
    completed = {
        **{k: raise_row[k] for k in ("v", "skill", "matter_id", "kind", "source_id", "item_key")},
        "ts": "2026-10-01T15:00:05Z",
        "id": "cw-completed",
        "event": "completed",
        "session_id": "sess-earlier",
        "tool_call_id": "toolu_earlier",
    }
    env.write_cw([raise_row, approved, completed])
    _reply("done with 1")
    out = _ack_call(env)
    assert out["status"] == "done"
    assert _rows(env, "casework_event_append") == []
    assert "I already had your answer on 1 (" in out["confirmation_text"]


def test_an_approval_whose_write_never_ran_is_written_on_the_next_done(env):
    """Pilot, 2026-10-01: the Smokeball connector did not come back after a
    restart, so the reply turn that approved line 1 had no update_task to call
    and the authorization stayed open. A second "done with 1" must run that
    write, not answer "I already had your answer" over an open task."""
    raise_row = _complete_raise()
    env.write_cw([raise_row, _approved(raise_row)])
    _reply("done with 1")
    out = _ack_call(env)
    assert out["status"] == "writes_queued" and out["writes"] == 1
    # No second approval: the open one authorizes this write.
    assert _rows(env, "casework_event_append") == []
    _land(env)
    assert _ack_call(env)["confirmation_text"].startswith("Got it. Closed 1 (")


# ---------------------------------------------------------------------------
# What cannot close, said in words
# ---------------------------------------------------------------------------


def test_a_completion_on_a_date_quiets_it_and_says_it_clears_when_it_passes(env):
    _reply("done with 2")
    out = _ack_call(env)
    assert out["status"] == "done" and out["acked"] == [2]
    assert _rows(env, "casework_event_append") == []
    assert out["confirmation_text"] == (
        "Got it. 2 (2026-PI-105 Okafor: Final Status Conference, Fri Oct 2, 2026) is a date; it "
        "clears when it passes. Quiet for 7 days. Still open: 1 (2026-PI-105 Okafor: Update "
        "exhibit list with Grand Valley incident photos, due Sep 18, 2026), 3 (2026-PI-101 Chen: "
        "2 more open items) and 4 (2026-PI-105 Okafor: Send preservation letter, due Sep 20, 2026)."
    )


def test_a_completion_on_a_group_quiets_the_group_and_says_a_group_cannot_close(env):
    _reply("3 is done")
    out = _ack_call(env)
    assert out["acked"] == [3]
    assert {e["item_key"] for e in _rows(env, "escalation_event_append", "acked")} == {
        "cccccc",
        "dddddd",
    }
    assert out["confirmation_text"].startswith(
        "Got it. 3 (2026-PI-101 Chen: 2 more open items) covers several items; I can't close a "
        "group from a reply. Quiet for 7 days; close the finished ones in Smokeball."
    )


def test_a_completion_on_a_task_the_send_did_not_raise_quiets_it_and_says_why(env):
    _reply("finished 4")
    out = _ack_call(env)
    assert out["acked"] == [4]
    assert _rows(env, "casework_event_append") == []
    assert out["confirmation_text"].startswith(
        "Got it. I can't close 4 (2026-PI-105 Okafor: Send preservation letter, due Sep 20, "
        "2026) from here: nobody is set on the matter in Smokeball and I have no staff record "
        "for you. Quiet for 7 days."
    )


def test_a_replier_with_no_authored_name_closes_nothing(env):
    env.config["users"] = []
    _reply("done with 1")
    out = _ack_call(env)
    assert out["status"] == "done" and out["acked"] == [1]
    assert _rows(env, "casework_event_append") == []
    assert out["confirmation_text"].startswith(
        "Got it. I have no authored name for you on this seat, so I can't close 1 (2026-PI-105 "
        "Okafor: Update exhibit list with Grand Valley incident photos, due Sep 18, 2026) from a "
        "reply; it is quiet for 7 days."
    )


# ---------------------------------------------------------------------------
# Asking, and acting on what is clear
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["not done with 1", "1 not done yet", "will get 1 done today", "I'll close 1 after the FSC"],
)
def test_a_guarded_completion_closes_nothing_and_asks(env, text):
    _reply(text)
    out = _ack_call(env)
    assert out["status"] == "done"
    assert env.requests == []
    assert out["confirmation_text"].endswith(
        "Is 1 (2026-PI-105 Okafor: Update exhibit list with Grand Valley incident photos, due "
        "Sep 18, 2026) done, or still open?"
    )


def test_clear_numbers_are_acted_on_and_the_unclear_one_is_asked_about(env):
    _reply("2 done, 1 not done yet")
    out = _ack_call(env)
    assert out["status"] == "done" and out["acked"] == [2]
    assert _rows(env, "casework_event_append") == []
    text = out["confirmation_text"]
    assert "is a date; it clears when it passes." in text
    assert text.endswith("done, or still open?")


def test_a_bare_done_on_a_long_digest_asks_which(env):
    _reply("Done, thanks!")
    out = _ack_call(env)
    assert out["status"] == "ask" and env.requests == []
    assert (
        out["confirmation_text"] == "Which ones are done? Reply with the numbers, or say all done."
    )


def test_a_bare_done_on_a_one_line_digest_closes_it(env):
    env.write_esc([_fired("aaaaaa", 1)])
    _reply("Done.")
    out = _ack_call(env)
    assert out["status"] == "writes_queued" and out["writes"] == 1
    _land(env)
    assert _ack_call(env)["confirmation_text"] == (
        "Got it. Closed 1 (2026-PI-105 Okafor: Update exhibit list with Grand Valley incident "
        "photos, due Sep 18, 2026)."
    )


def test_all_done_closes_the_raised_line_and_quiets_the_rest(env):
    _reply("all done")
    out = _ack_call(env)
    assert out["status"] == "writes_queued" and out["writes"] == 1
    assert {e["item_key"] for e in _rows(env, "escalation_event_append", "acked")} == {
        "bbbbbb",
        "cccccc",
        "dddddd",
        "eeeeee",
    }


def test_an_unknown_number_writes_nothing_at_all(env):
    _reply("done with 1 and 9")
    out = _ack_call(env)
    assert out["status"] == "unknown_numbers" and env.requests == []


def test_a_hold_on_a_digest_line_writes_nothing(env):
    _reply("done with 1, leave 2")
    assert _ack_call(env)["status"] == "writes_queued"
    assert _rows(env, "escalation_event_append") == []
    assert [r["event"] for r in _rows(env, "casework_event_append")] == ["approved"]


def test_the_confirmation_seeds_provenance_for_the_labels_it_names(env, monkeypatch):
    from shared import provenance

    seeded: list[str] = []
    monkeypatch.setattr(provenance, "record_read", lambda _s, text: seeded.append(text))
    _reply("done with 2")
    _ack_call(env)
    assert seeded and LABELS[2] in seeded[0]


# ---------------------------------------------------------------------------
# The parser's completion verdicts (pure)
# ---------------------------------------------------------------------------


def _verdicts(text):
    v = load_plugin("hermes-smd-escalation").reply_items.parse_reply_verdicts(text)
    return (
        sorted(v["complete"]),
        sorted(v["uncertain"]),
        v["all_complete"],
        v["bare_complete"],
        sorted(v["approve"]),
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("done with 1", ([1], [], False, False, [1])),
        ("1 is done", ([1], [], False, False, [1])),
        ("closed out 1 and 3", ([1, 3], [], False, False, [1, 3])),
        ("I handled 2 last week", ([2], [], False, False, [2])),
        ("2 is taken care of", ([2], [], False, False, [2])),
        ("got it on 1", ([], [], False, False, [1])),
        ("1", ([], [], False, False, [1])),
        ("done, thanks", ([], [], False, True, [])),
        ("done with those", ([], [], False, True, [])),
        ("all done", ([], [], True, False, [])),
        ("done with all of them", ([], [], True, False, [])),
        # ``approve`` keeps its old positional reading (the task review's path
        # is unchanged); on a digest ``uncertain`` wins, so 1 is asked about.
        ("not done with 1", ([], [1], False, False, [1])),
        ("1 done, 2 not done", ([1], [2], False, False, [1, 2])),
        # One clause, one hold word: both numbers are asked about rather than
        # one of them closed on a guess.
        ("1 done 2 not yet", ([], [1, 2], False, False, [1, 2])),
        ("will get 1 done today", ([], [1], False, False, [1])),
        ("I'll close 1 after the FSC", ([], [1], False, False, [1])),
        ("yes on 1, done with 2", ([2], [], False, False, [1, 2])),
        ("got it on 1. 1 is done", ([1], [], False, False, [1])),
        ("done with 1\n\nThanks,\nScott\n(602) 555-1234", ([1], [], False, False, [1])),
    ],
)
def test_parse_completion_verdicts(text, expected):
    assert _verdicts(text) == expected
