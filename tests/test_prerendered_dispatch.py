"""Out-of-turn dispatch of a routine's pre-rendered envelope (WS-RENDER).

Pins the safety-critical behavior: binds only cron sessions of the named
skill inside the recency window; consume-once BEFORE dispatch; the
full -> skeleton -> failure-note ladder; appends only after a successful
FULL send, carrying the resolved session id; withheld-for-approval writes no
append and tries no skeleton; and no context string ever names a gate or a
rule.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from shared import cron_attribution, prerendered_dispatch, send_dispatch
from shared.send_dispatch import DispatchResult

SKILL = "deadline-miss-escalator"
SESSION = "cron_deadline-miss-escalator_20260831_070026"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prerendered_dispatch._IN_TURN.clear()
    yield
    send_dispatch.set_sender(None)
    prerendered_dispatch._IN_TURN.clear()


def _routine(monkeypatch, skill=SKILL, persona=None):
    identity = cron_attribution.RoutineIdentity(
        job_id="j1", job_name=f"op-managed:operator:{skill}", persona=persona, skill=skill
    )
    monkeypatch.setattr(
        prerendered_dispatch.cron_attribution, "resolve_routine", lambda sid: identity
    )
    monkeypatch.setattr(
        prerendered_dispatch.cron_attribution,
        "parse_cron_session_started_at",
        lambda sid: datetime.now(timezone.utc),
    )


def _write_envelope(tmp_path, *, skill=SKILL, started_at=None, dispatches=None, **extra):
    directory = tmp_path / ".smd" / "pre_run"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "skill": skill,
        "render_mode": "templated",
        "started_at": (started_at or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "dispatches": dispatches if dispatches is not None else [_dispatch_entry()],
        **extra,
    }
    (directory / f"{skill}.dispatch.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _dispatch_entry(**overrides):
    entry = {
        "recipients": ["ops@firm.example"],
        "cc": [],
        "routing_leg": "central",
        "subject": "[Deadlines] 1 need you, 2026-08-31",
        "full_body": "## Needs you today (1)\n\n1. matter 2026-PI-101, task-deadline 2026-08-29 (overdue by 2 days) [ACK-AAAAAA]\n",
        "skeleton_body": "## Deadline digest (details unavailable)\n\n1 item needs a person now.\n",
        "body_sha256_full": "a" * 64,
        "body_sha256_skeleton": "b" * 64,
        "appends": [
            {
                "item_key": "k" * 16,
                "matter_id": "m-1",
                "event": "fired",
                "attempt": 1,
                "token": "ACK-AAAAAA",
            }
        ],
    }
    entry.update(overrides)
    return entry


class _Sender:
    """Scripted sender double: one DispatchResult per call, recorded."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return (
            self.results.pop(0)
            if self.results
            else DispatchResult(sent=False, reason="script exhausted")
        )


def _appends_recorder(monkeypatch):
    written = []

    def fake_request(payload):
        written.append(payload)
        return {"ok": True, "id": "row-1"}

    monkeypatch.setattr(prerendered_dispatch, "_broker_request", fake_request)
    return written


# ---------------------------------------------------------------------------
# canonical_body_sha256 — pinned to the mirrored arbiter fixture.
# ---------------------------------------------------------------------------


def test_canonical_hash_matches_mirrored_arbiter_vectors():
    from pathlib import Path

    fixture = Path(__file__).resolve().parent / "fixtures" / "body-canon-vectors.json"
    vectors = json.loads(fixture.read_text(encoding="utf-8"))["vectors"]
    assert {v["name"] for v in vectors} >= {"trailing_newline", "crlf_line_endings"}
    for vector in vectors:
        assert prerendered_dispatch.canonical_body_sha256(vector["input"]) == vector["sha256"], (
            vector["name"]
        )


# ---------------------------------------------------------------------------
# Binding + consume-once
# ---------------------------------------------------------------------------


def test_non_cron_session_dispatches_nothing(monkeypatch, tmp_path):
    _write_envelope(tmp_path)
    monkeypatch.setattr(prerendered_dispatch.cron_attribution, "resolve_routine", lambda sid: None)
    sender = _Sender([DispatchResult(sent=True, message_id="m1")])
    send_dispatch.set_sender(sender)
    assert prerendered_dispatch.dispatch_prerendered("interactive-session") is None
    assert sender.calls == []
    # The envelope stays in place for an operator to inspect.
    assert prerendered_dispatch.envelope_path(SKILL).exists()


def test_stale_envelope_does_not_bind(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(tmp_path, started_at=datetime.now(timezone.utc) - timedelta(hours=2))
    sender = _Sender([DispatchResult(sent=True, message_id="m1")])
    send_dispatch.set_sender(sender)
    assert prerendered_dispatch.dispatch_prerendered(SESSION) is None
    assert sender.calls == []


def test_envelope_for_another_skill_does_not_bind(monkeypatch, tmp_path):
    _routine(monkeypatch, skill="client-verification-tracker")
    _write_envelope(tmp_path, skill=SKILL)
    sender = _Sender([])
    send_dispatch.set_sender(sender)
    assert prerendered_dispatch.dispatch_prerendered(SESSION) is None
    assert sender.calls == []


def test_consume_once_a_second_pass_finds_nothing(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(tmp_path)
    _appends_recorder(monkeypatch)
    send_dispatch.set_sender(_Sender([DispatchResult(sent=True, message_id="m1")]))
    assert prerendered_dispatch.dispatch_prerendered(SESSION) is not None
    assert not prerendered_dispatch.envelope_path(SKILL).exists()
    assert prerendered_dispatch.consumed_path(SKILL).exists()
    sender2 = _Sender([DispatchResult(sent=True, message_id="m2")])
    send_dispatch.set_sender(sender2)
    assert prerendered_dispatch.dispatch_prerendered(SESSION) is None
    assert sender2.calls == []


def test_malformed_dispatch_entry_refuses_the_whole_envelope(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(
        tmp_path,
        dispatches=[_dispatch_entry(), {"recipients": [], "subject": "x", "full_body": "y"}],
    )
    sender = _Sender([DispatchResult(sent=True, message_id="m1")])
    send_dispatch.set_sender(sender)
    assert prerendered_dispatch.dispatch_prerendered(SESSION) is None
    assert sender.calls == []  # nothing sends from a partially trusted file


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_full_send_writes_appends_with_the_session_id(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(tmp_path)
    written = _appends_recorder(monkeypatch)
    send_dispatch.set_sender(
        _Sender([DispatchResult(sent=True, message_id="m1", recipients=("ops@firm.example",))])
    )
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    assert "already delivered to ops@firm.example" in note
    assert "message m1" in note
    assert "1 of 1 item record(s) written" in note
    [request] = written
    assert request["action"] == "escalation_event_append"
    event = request["event"]
    assert event["event"] == "fired"
    assert event["item_key"] == "k" * 16
    assert event["attempt"] == 1
    assert event["token"] == "ACK-AAAAAA"
    assert event["session_id"]  # the witness join key (ss#2603)
    assert event["ts"] is None  # the broker stamps; the agent cannot backdate
    # The sends went through the published sender with the conformance stamps.


def test_full_send_carries_body_variant_and_routing_leg(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(tmp_path)
    _appends_recorder(monkeypatch)
    sender = _Sender([DispatchResult(sent=True, message_id="m1")])
    send_dispatch.set_sender(sender)
    prerendered_dispatch.dispatch_prerendered(SESSION)
    [call] = sender.calls
    assert call["templated"] is True
    # Exact equality on purpose: audit_extra is the broker's closed allowlist
    # seen from this side, and a key added here without its broker twin
    # vanishes silently between the repos. `skill_name` (ss-console claims
    # review 2026-09-04, B3) is the cron-resolved routine, not an agent claim.
    assert call["audit_extra"] == {
        "skill_name": SKILL,
        "routing_leg": "central",
        "body_variant": "full",
    }
    assert call["to"] == ["ops@firm.example"]


def test_the_skill_stamp_is_the_cron_resolved_routine_not_the_envelope(monkeypatch, tmp_path):
    """The stamp names the routine the SESSION is, which is what the console's
    wake<->confirm join keys on. A routine resolved as the tracker stamps the
    tracker even though the envelope on disk was written under the escalator's
    name -- FALSIFIER: read the skill off the envelope instead and this passes
    the escalator through."""
    _routine(monkeypatch, skill="client-verification-tracker")
    _write_envelope(tmp_path, skill="client-verification-tracker")
    _appends_recorder(monkeypatch)
    sender = _Sender([DispatchResult(sent=True, message_id="m1")])
    send_dispatch.set_sender(sender)
    prerendered_dispatch.dispatch_prerendered("cron_client-verification-tracker_20260831_070026")
    [call] = sender.calls
    assert call["audit_extra"]["skill_name"] == "client-verification-tracker"


def test_refused_full_falls_back_to_skeleton_with_no_appends(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(tmp_path)
    written = _appends_recorder(monkeypatch)
    sender = _Sender(
        [
            DispatchResult(sent=False, reason="refused: content"),
            DispatchResult(sent=True, message_id="m2"),
        ]
    )
    send_dispatch.set_sender(sender)
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    assert "reduced" in note
    assert "retried on the next run" in note
    assert written == []  # per-item codes never reached a person
    # The skeleton is still this routine's send: same column, same join.
    assert sender.calls[1]["audit_extra"] == {
        "skill_name": SKILL,
        "routing_leg": "central",
        "body_variant": "skeleton",
    }
    assert sender.calls[1]["text"].startswith("## Deadline digest")


def test_both_refused_yields_the_failure_instruction(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(tmp_path)
    written = _appends_recorder(monkeypatch)
    send_dispatch.set_sender(
        _Sender(
            [
                DispatchResult(sent=False, reason="refused: content"),
                DispatchResult(sent=False, reason="refused: content"),
            ]
        )
    )
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    assert "could not be delivered" in note
    assert "failure instruction" in note
    assert written == []


def test_withheld_for_approval_is_a_disposition_not_a_failure(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(tmp_path)
    written = _appends_recorder(monkeypatch)
    sender = _Sender(
        [
            DispatchResult(
                sent=False,
                reason="external_send at authored confirm ceiling; withheld pending current-turn approval",
            )
        ]
    )
    send_dispatch.set_sender(sender)
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    assert "held for the owner's approval" in note
    assert len(sender.calls) == 1  # NO skeleton attempt
    assert written == []
    # ONE withheld set: no supersession caveat (the single-slot note is only
    # honest when a later capture actually superseded an earlier one).
    assert "superseded" not in note


def test_draft_routed_is_its_own_disposition(monkeypatch, tmp_path):
    """DRAFT_FOR_REVIEW is not the confirm round-trip: nothing is queued, the
    reviewed draft IS the delivery. No skeleton, no failure note, no appends —
    the note tells the turn to compose the one draft for review. Both real
    reason texts (the ceiling's and the content floor's) take this branch."""
    for reason in (
        "external_send at authored draft_for_review ceiling; routing to draft",
        "Refused: this message touches money; routing to draft for human "
        "review instead of autonomous send (content-sensitivity floor, ADR 0031).",
    ):
        prerendered_dispatch._IN_TURN.clear()
        _routine(monkeypatch)
        _write_envelope(tmp_path)
        written = _appends_recorder(monkeypatch)
        sender = _Sender([DispatchResult(sent=False, reason=reason)])
        send_dispatch.set_sender(sender)
        note = prerendered_dispatch.dispatch_prerendered(SESSION)
        assert "draft for review" in note, reason
        assert "compose ONE draft" in note, reason
        assert "could not be delivered" not in note, reason  # not the failure rung
        assert len(sender.calls) == 1, reason  # NO skeleton attempt
        assert written == [], reason
        assert "held for the owner's approval" not in note, reason


def test_multiple_withheld_sets_disclose_the_single_pending_slot(monkeypatch, tmp_path):
    """PENDING_SEND keeps one send; of N withheld dispatches only the LAST is
    queued. The note must say the earlier sets were superseded — never claim
    all are held."""
    _routine(monkeypatch)
    withheld = "external_send at authored confirm ceiling; withheld pending current-turn approval"
    _write_envelope(
        tmp_path,
        dispatches=[
            _dispatch_entry(),
            _dispatch_entry(
                recipients=["amy@firm.example"], routing_leg="matter_staff_responsible"
            ),
        ],
    )
    _appends_recorder(monkeypatch)
    send_dispatch.set_sender(
        _Sender(
            [
                DispatchResult(sent=False, reason=withheld),
                DispatchResult(sent=False, reason=withheld),
            ]
        )
    )
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    assert note.count("held for the owner's approval") == 2
    assert "superseded" in note
    assert "surface again on the next run" in note


def test_context_strings_name_no_gates_or_rules(monkeypatch, tmp_path):
    """Refusal/context text shown to the model carries corrective action only."""
    _routine(monkeypatch)
    _write_envelope(tmp_path)
    _appends_recorder(monkeypatch)
    send_dispatch.set_sender(
        _Sender(
            [
                DispatchResult(sent=False, reason="taint gate refused: fabricated citation"),
                DispatchResult(sent=False, reason="content floor refused"),
            ]
        )
    )
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    for banned in ("gate", "rule", "ceiling", "taint", "floor", "fabricat"):
        assert banned not in note.lower()


def test_memo_duty_and_in_turn_templates_ride_the_note_and_cache(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _write_envelope(
        tmp_path,
        memo_matters=["m-2", "m-3"],
        in_turn=[{"name": "failure_note", "template": "The run failed.", "slots": {}}],
        in_turn_enforce=True,
    )
    _appends_recorder(monkeypatch)
    send_dispatch.set_sender(_Sender([DispatchResult(sent=True, message_id="m1")]))
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    assert "m-2, m-3" in note
    assert "memo" in note
    from shared import provenance

    declaration = prerendered_dispatch.in_turn_templates(provenance.resolve_session(SESSION))
    assert declaration["enforce"] is True
    assert declaration["templates"][0]["template"] == "The run failed."


def test_append_refusal_is_survivable_and_counted(monkeypatch, tmp_path):
    _routine(monkeypatch)
    entry = _dispatch_entry(
        appends=[
            {
                "item_key": "a" * 16,
                "matter_id": "m-1",
                "event": "fired",
                "attempt": 1,
                "token": None,
            },
            {
                "item_key": "b" * 16,
                "matter_id": "m-2",
                "event": "fired",
                "attempt": 1,
                "token": None,
            },
        ]
    )
    _write_envelope(tmp_path, dispatches=[entry])
    calls = []

    def flaky(payload):
        calls.append(payload)
        if payload["event"]["item_key"] == "a" * 16:
            return {"ok": False, "error": "witness"}
        return {"ok": True, "id": "row-2"}

    monkeypatch.setattr(prerendered_dispatch, "_broker_request", flaky)
    send_dispatch.set_sender(_Sender([DispatchResult(sent=True, message_id="m1")]))
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    assert "1 of 2 item record(s) written" in note
    assert len(calls) == 2  # one refusal does not lose the rest
