"""The ``proposed complete`` rows a deadline digest's send writes, one per task
line a person may answer "done with 1" to (``shared/casework_raises.py``), and
the line kinds the send keeps beside the labels (``shared/sent_lines.py``)."""

from __future__ import annotations

import pytest

from shared import casework_ledger, casework_raises, prerendered_dispatch, send_dispatch, sent_lines
from shared.send_dispatch import DispatchResult
from tests.test_prerendered_dispatch import (  # noqa: F401 — _clean is the autouse harness
    SESSION,
    _appends_recorder,
    _clean,
    _dispatch_entry,
    _routine,
    _Sender,
    _write_envelope,
)

MATTER = "m-105"
TASK = "t-exhibits"
KEY = casework_ledger.item_key(matter_id=MATTER, kind="task", source_id=TASK)


def _raise(**over) -> dict:
    row = {
        "item_key": KEY,
        "matter_id": MATTER,
        "kind": "task",
        "source_id": TASK,
        "n": 1,
        "payload": {
            "action": "complete",
            "class": "at_stake",
            "staff_id": "st-scott",
            "reason": "deadline_digest; owner Scott Durgan",
            "evidence": [],
        },
    }
    row.update(over)
    return row


def test_a_well_formed_list_or_absence_is_valid():
    assert casework_raises.valid(None)
    assert casework_raises.valid([])
    assert casework_raises.valid([_raise()])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(kind="date"),
        lambda r: r.update(n=0),
        lambda r: r.update(n="1"),
        lambda r: r.update(item_key="f" * 16),
        lambda r: r.update(snooze_days=7),
        lambda r: r["payload"].update(action="close"),
        lambda r: r["payload"].update(**{"class": "urgent"}),
        lambda r: r["payload"].update(staff_id=""),
        lambda r: r["payload"].update(evidence=["a document"]),
        lambda r: r["payload"].pop("reason"),
    ],
)
def test_a_malformed_raise_is_invalid(mutate):
    row = _raise()
    mutate(row)
    assert not casework_raises.valid([row])


def test_write_sends_one_proposed_row_per_line_with_the_dispatch_fields():
    written: list[dict] = []
    ok = casework_raises.write(
        "deadline-miss-escalator",
        [_raise()],
        "sess-1",
        "a" * 32,
        append=lambda e: written.append(e) or {"ok": True},
    )
    assert ok == (1, 1)
    [event] = written
    assert event["event"] == "proposed" and event["session_id"] == "sess-1"
    assert event["n"] == 1 and event["dispatch_ref"] == "a" * 32
    assert "thread_ref" not in event
    assert event["payload"] == {
        "action": "complete",
        "class": "at_stake",
        "staff_id": "st-scott",
        "reason": "deadline_digest; owner Scott Durgan",
        "evidence": [],
        "to_staff_id": None,
    }
    assert "snooze_days" not in event


def test_a_refused_row_is_counted_not_raised():
    assert casework_raises.write(
        "deadline-miss-escalator", [_raise()], "sess-1", "a" * 32, append=lambda e: {"ok": False}
    ) == (0, 1)
    assert casework_raises.write(
        "deadline-miss-escalator", [_raise()], "sess-1", "a" * 32, append=lambda e: 1 / 0
    ) == (0, 1)


def _entry_with_raise():
    entry = _dispatch_entry()
    entry["appends"][0]["n"] = 1
    entry["appends"][0]["kind"] = "task"
    entry["casework_raises"] = [_raise()]
    return entry


def test_a_full_send_writes_the_raises_through_the_casework_verb(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(tmp_path, dispatches=[_entry_with_raise()])
    written = _appends_recorder(monkeypatch)
    casework: list[dict] = []
    monkeypatch.setattr(
        casework_raises, "broker_append", lambda e: casework.append(e) or {"ok": True}
    )
    sender = _Sender([DispatchResult(sent=True, message_id="m1")])
    send_dispatch.set_sender(sender)
    prerendered_dispatch.dispatch_prerendered(SESSION)
    ref = sender.calls[0]["audit_extra"]["dispatch_ref"]
    [event] = casework
    assert event["event"] == "proposed" and event["dispatch_ref"] == ref and event["n"] == 1
    assert event["session_id"] == written[0]["event"]["session_id"]
    # The line kinds ride beside the labels for the reply's words.
    assert sent_lines.kinds(ref) == {1: "task"}


def test_a_skeleton_delivery_writes_no_raise(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(tmp_path, dispatches=[_entry_with_raise()])
    _appends_recorder(monkeypatch)
    casework: list[dict] = []
    monkeypatch.setattr(
        casework_raises, "broker_append", lambda e: casework.append(e) or {"ok": True}
    )
    send_dispatch.set_sender(
        _Sender(
            [
                DispatchResult(sent=False, reason="refused"),
                DispatchResult(sent=True, message_id="m2"),
            ]
        )
    )
    prerendered_dispatch.dispatch_prerendered(SESSION)
    assert casework == []


def test_a_malformed_raise_refuses_the_whole_envelope(monkeypatch, tmp_path):
    _routine(monkeypatch)
    entry = _entry_with_raise()
    entry["casework_raises"][0]["payload"]["action"] = "close"
    _write_envelope(tmp_path, dispatches=[entry])
    sender = _Sender([DispatchResult(sent=True, message_id="m1")])
    send_dispatch.set_sender(sender)
    assert prerendered_dispatch.dispatch_prerendered(SESSION) is None
    assert sender.calls == []


def test_sent_lines_keeps_kinds_beside_labels_and_refuses_junk(monkeypatch, tmp_path):
    monkeypatch.setenv("SMD_SENT_LINES_DIR", str(tmp_path))
    ref = "c" * 32
    assert sent_lines.record(ref, {1: "A line"}, {1: "task", 2: "date", 3: "group", 0: "task"})
    assert sent_lines.kinds(ref) == {1: "task", 2: "date"}
    assert sent_lines.labels(ref) == {1: "A line"}
    assert sent_lines.kinds("d" * 32) == {}
    # Kinds alone are worth keeping (a body whose lines carried no usable label).
    assert sent_lines.record("e" * 32, {}, {1: "date"})
    assert sent_lines.kinds("e" * 32) == {1: "date"}
