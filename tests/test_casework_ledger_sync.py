"""The casework ledger twin, and the rows the overlay writes against it.

``shared/casework_ledger.py`` is a byte-identical copy of ss-console
``operator/workspace_broker/casework_ledger.py``: the broker validates every row
with the canonical, and the overlay derives item keys and reads state with this
copy. If the two drift, a row the overlay composes is refused by the broker, or
a key it derives names nothing. Same discipline as
``tests/test_escalation_ledger_sync.py``: restamp from the canonical, never
hand-edit, and paste the new digest below.

The second half runs the real ``validate_append`` over the rows the tools write,
end to end: a review is sent, a reply approves all but one line, the queued
writes run, and every row lands. A row shape the broker would refuse fails here,
in this repo's own CI, before a seat ever sees it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shared import casework_acts, casework_ledger, inbound
from shared.casework_acts import CASEWORK_ACTS
from tests.conftest import load_plugin

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COPY = _REPO_ROOT / "shared" / "casework_ledger.py"

# sha256 of venturecrane/ss-console::operator/workspace_broker/casework_ledger.py
# at the commit this copy was stamped from.
#
# 2026-09-25 (case-manager deadline work): first stamp, from ss-console
# feat/casework-broker 2e511a11 (formatted at the overlay's 100-column line
# length so the byte-identical copy passes this repo's ruff format check).
#
# 2026-09-25b (case-manager done-since): restamped from ss-console
# feat/casework-done-since. Adds the ``step_ran`` event (a date-prep step the
# Operator ran itself, witnessed by its create_memo call) and lets ``mentioned``
# close a date item's untold steps.
#
# 2026-09-25c: restamped. _validate_payload splits its two kind-shape checks
# into _check_kind_shape (ss-console's function-complexity ceiling); rules and
# refusal text unchanged.
CANONICAL_SHA256 = "228f5a20366e6260ca0069dbf148b7b1e6841cfe428936b4d974ac6fd15b8b02"


def test_copy_matches_the_pinned_canonical_digest() -> None:
    actual = hashlib.sha256(_COPY.read_bytes()).hexdigest()
    assert actual == CANONICAL_SHA256, (
        "shared/casework_ledger.py no longer matches its pinned canonical digest. "
        "It is a byte-identical copy of venturecrane/ss-console::"
        "operator/workspace_broker/casework_ledger.py: restamp it from there and "
        "update CANONICAL_SHA256, never hand-edit it."
    )


# ---------------------------------------------------------------------------
# The rows the tools write, through the canonical validator
# ---------------------------------------------------------------------------

plugin = load_plugin("hermes-smd-escalation")
casework = plugin.casework

SESSION = "cron_job-tlk_20260928_080500"
REPLY_SESSION = "sess-reply-e2e"
THREAD = "conv-review-e2e"
NOW = datetime(2026, 9, 28, 15, 6, tzinfo=timezone.utc)
ATTORNEY = "atty@firm.example"


class Broker:
    """The broker's casework verb, in miniature: validate with the canonical,
    stamp, stamp a raise's thread from the send it witnessed, append."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[dict] = []
        self.refused: list[tuple[dict, str]] = []
        self.completed_calls: set[str] = set()

    def append(self, event: dict) -> dict:
        event = dict(event)
        if event.get("event") in casework_ledger.RAISING_EVENTS:
            event["thread_ref"] = THREAD
        try:
            casework_ledger.validate_append(
                self.rows,
                event,
                send_witness=lambda _e: True,
                audit_witness=lambda e: e.get("tool_call_id") in self.completed_calls,
            )
        except ValueError as exc:
            self.refused.append((event, str(exc)))
            return {"ok": False, "error": str(exc)}
        stamped = casework_ledger.stamp_event(event)
        self.rows.append(stamped)
        self.path.write_text(
            "".join(casework_ledger.serialize_event(r) + "\n" for r in self.rows), encoding="utf-8"
        )
        return {"ok": True, "id": stamped["id"]}


class _Sent:
    sent = True
    message_id = "msg-e2e"
    reason = ""


class _Config:
    def sender_on_roster(self, address):
        return address == ATTORNEY


def _key(task: str) -> str:
    return casework_ledger.item_key(matter_id="m-104", kind="task", source_id=task)


def _envelope() -> dict:
    def item(n, task, action, klass, **extra):
        return {
            "n": n,
            "line": f"Task {task}. Suggest: {action}.",
            "event": "proposed",
            "item_key": _key(task),
            "matter_id": "m-104",
            "task_id": task,
            "payload": {
                "action": action,
                "class": klass,
                "staff_id": "staff-atty",
                "reason": "the record shows it",
                "evidence": ["2026-06-18"],
                **extra,
            },
        }

    return {
        "skill": "task-list-keeper",
        "started_at": (NOW - timedelta(minutes=1)).isoformat(),
        "messages": [
            {
                "recipients": [ATTORNEY],
                "subject": "Task review",
                "closes": [
                    {
                        "item_key": _key("t-own"),
                        "matter_id": "m-104",
                        "task_id": "t-own",
                        "staff_id": "staff-atty",
                        "evidence": ["Proof of Service 2026-06-18"],
                        "line": "2026-PI-104: Serve discovery responses",
                    }
                ],
                "items": [
                    item(1, "t-1", "close", "done"),
                    item(2, "t-2", "close", "stale"),
                    item(3, "t-3", "reassign", "open", to_staff_id="staff-para"),
                    item(4, "t-4", "keep", "at_stake"),
                ],
            }
        ],
    }


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setattr(inbound, "SESSION_INBOUND_ORIGIN", inbound.SessionInboundOrigin())
    from plugin_hermes_smd_escalation import casework_rules

    for session in (SESSION, REPLY_SESSION):
        CASEWORK_ACTS.clear(session)
        for store in (casework_rules._FINISH, casework_rules._REPLIES):
            store.pop(session)
    b = Broker(tmp_path / "casework-ledger.jsonl")
    CASEWORK_ACTS.set_writer(b.append)
    yield b
    CASEWORK_ACTS.set_writer(None)


def _run_writes(session: str, broker: Broker, count: int, *, first_call: int = 0) -> list[dict]:
    executed = []
    for i in range(count):
        args = {"task_id": "model-chosen"}
        CASEWORK_ACTS.replay(session, casework_acts.UPDATE_TASK_TOOL, args)
        executed.append(args)
        call_id = f"call-{first_call + i}"
        broker.completed_calls.add(call_id)
        CASEWORK_ACTS.on_post_tool(
            casework_acts.UPDATE_TASK_TOOL, session, {"tool_call_id": call_id, "result": "{}"}
        )
    return executed


def test_review_then_reply_every_row_is_one_the_broker_accepts(tmp_path, broker):
    directory = tmp_path / ".smd" / "pre_run"
    directory.mkdir(parents=True)
    (directory / "task-list-keeper.casework.json").write_text(json.dumps(_envelope()))

    def finish():
        return json.loads(
            casework.casework_finish(
                session_id=SESSION,
                append=broker.append,
                dispatch=lambda **_k: _Sent(),
                routine=lambda _s: ("task-list-keeper", None),
                hermes_home=str(tmp_path),
                now=NOW,
            )
        )

    assert finish()["status"] == "writes_queued"
    assert _run_writes(SESSION, broker, 1) == [
        {"task_id": "t-own", "staff_id": "staff-atty", "is_completed": True}
    ]
    assert finish()["status"] == "sent"

    inbound.SESSION_INBOUND_ORIGIN.record(
        REPLY_SESSION,
        inbound.InboundOrigin(
            sender_address=ATTORNEY,
            message_id="msg-reply",
            conversation_id=THREAD,
            reply_text="yes to all except 2",
        ),
    )

    def answer():
        return json.loads(
            casework.reply_verdicts(
                session_id=REPLY_SESSION,
                load_config=_Config,
                verified_acker=lambda _s: {"name": "Dana Whitfield", "key": "a" * 64},
                append=broker.append,
                fallthrough=lambda: "{}",
                casework_ledger_path=str(broker.path),
            )
        )

    first = answer()
    assert first["status"] == "writes_queued" and first["writes"] == 2
    assert _run_writes(REPLY_SESSION, broker, 2, first_call=10) == [
        {"task_id": "t-1", "staff_id": "staff-atty", "is_completed": True},
        {"task_id": "t-3", "staff_id": "staff-atty", "assignee_ids": ["staff-para"]},
    ]
    final = answer()
    assert final["confirmation_text"] == (
        "Got it. Closed 1. Reassigned 3. Leaving 2 and 4 as they are."
    )

    assert broker.refused == []
    kinds = [r["event"] for r in broker.rows]
    assert kinds == [
        "closed_by_record",
        "completed",
        "proposed",
        "proposed",
        "proposed",
        "proposed",
        "mentioned",
        "approved",
        "held",
        "kept",
        "approved",
        "approved",
        "completed",
        "completed",
    ]
    states = casework_ledger.derive_state(broker.rows)
    assert states[_key("t-1")].completed and states[_key("t-3")].completed
    assert not states[_key("t-2")].completed and states[_key("t-2")].kept
    assert states[_key("t-own")].completed_via == "closed_by_record"


def test_a_completed_the_broker_has_not_witnessed_is_retried(tmp_path, broker):
    """The broker refuses ``completed`` until the audit row for the call exists;
    the row is kept and retried at the turn's next casework call."""
    directory = tmp_path / ".smd" / "pre_run"
    directory.mkdir(parents=True)
    (directory / "task-list-keeper.casework.json").write_text(json.dumps(_envelope()))
    kwargs = {
        "session_id": SESSION,
        "append": broker.append,
        "dispatch": lambda **_k: _Sent(),
        "routine": lambda _s: ("task-list-keeper", None),
        "hermes_home": str(tmp_path),
        "now": NOW,
    }
    casework.casework_finish(**kwargs)
    CASEWORK_ACTS.replay(SESSION, casework_acts.UPDATE_TASK_TOOL, {})
    CASEWORK_ACTS.on_post_tool(
        casework_acts.UPDATE_TASK_TOOL, SESSION, {"tool_call_id": "late", "result": "{}"}
    )
    assert [e["event"] for e, _ in broker.refused] == ["completed"]
    broker.completed_calls.add("late")
    casework.casework_finish(**kwargs)
    assert [r["event"] for r in broker.rows][:2] == ["closed_by_record", "completed"]


def test_a_brief_and_its_answer_are_rows_the_broker_accepts(tmp_path, broker):
    from plugin_hermes_smd_escalation import casework_rules

    casework_rules._BRIEFED.pop(SESSION)
    date_key = casework_ledger.item_key(matter_id="m-105", kind="date", source_id="ev-1")
    envelope = {
        "skill": "date-prep-brief",
        "started_at": (NOW - timedelta(minutes=1)).isoformat(),
        "matter_id": "m-105",
        "event_id": "ev-1",
        "item_key": date_key,
        "subject_label": "2026-PI-105: status conference Fri Oct 2",
        "recipients": [ATTORNEY],
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
    directory = tmp_path / ".smd" / "pre_run"
    directory.mkdir(parents=True)
    (directory / "date-prep-brief.brief.json").write_text(json.dumps(envelope))
    sent = json.loads(
        casework.casework_brief(
            {
                "done": ["The draft trial binder is in the matter."],
                "decisions": [
                    {"catalog_id": "witness_list_finalize", "question": "Is it final?"},
                    {"catalog_id": "records_refresh:reyes", "question": "Refresh records?"},
                ],
            },
            session_id=SESSION,
            append=broker.append,
            dispatch=lambda **_k: _Sent(),
            routine=lambda _s: ("date-prep-brief", None),
            hermes_home=str(tmp_path),
            now=NOW,
        )
    )
    assert sent["status"] == "sent" and sent["decisions_recorded"] == 2
    inbound.SESSION_INBOUND_ORIGIN.record(
        REPLY_SESSION,
        inbound.InboundOrigin(
            sender_address=ATTORNEY,
            message_id="msg-reply-b",
            conversation_id=THREAD,
            reply_text="yes on 2",
        ),
    )
    out = json.loads(
        casework.reply_verdicts(
            session_id=REPLY_SESSION,
            load_config=_Config,
            verified_acker=lambda _s: None,
            append=broker.append,
            fallthrough=lambda: "{}",
            casework_ledger_path=str(broker.path),
        )
    )
    assert broker.refused == []
    assert out["confirmation_text"] == "Got it. Starting on 2 now."
    assert [s["catalog_id"] for s in out["steps_to_run"]] == ["records_refresh:reyes"]
    assert [r["event"] for r in broker.rows] == ["briefed", "briefed", "approved", "step_started"]
    started = broker.rows[-1]
    assert (started["n"], started["thread_ref"]) == (2, THREAD)
