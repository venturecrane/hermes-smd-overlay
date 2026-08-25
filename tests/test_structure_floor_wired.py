"""The structure floor is WIRED into ``evaluate_tool_call``, not merely defined.

Every other test of this feature exercises ``spec_gate.check_structure_floor``
directly, and all of them would keep passing if the call site in ``enforce`` were
deleted. That is the built-but-not-wired failure class, and it is the one this
whole feature exists because of: on 2026-08-25 the digest's shape was governed by
a spec that read correctly and was enforced by nothing.

So this file drives the real ``evaluate_tool_call`` and asserts the floor ran.
"""

from __future__ import annotations

import pytest

from shared import spec_gate
from tests.conftest import load_plugin

trust = load_plugin("hermes-smd-trust")
enforce = trust.enforce

#: The seat's rostered internal addresses. A send to one of these resolves to
#: ``external_send_internal`` -> the ``staff`` output class, which is the one the
#: deadline digest actually travels as and the one whose disposition matters.
ROSTER = ["scott@smd.services"]

SID = "cron_abc123_20260825_070034"
SEND = "mcp_agentmail_send_message"
STAFF = "scott@smd.services"

#: No heading, no list marker, one horizontal rule. The 2026-08-25 shape.
SHAPELESS = (
    "Matter 2026-PI-101 has a task deadline authored for 2026-08-26, one day "
    "out, with no prior Operator raise on record. It needs attention today.\n\n"
    "NEEDS YOU TODAY\n\n"
    "2026-PI-101 | task-deadline | due 2026-08-26 | 1 day out\n"
    "ACK: ACK-YED4HY\n\n"
    "---\n\n"
    "UNDER ACTIVE ESCALATION ELSEWHERE (no action required from you)\n"
)

STRUCTURED = (
    "## Needs you today (1)\n\n"
    "1. matter 2026-PI-101, task-deadline 2026-08-26 (due in 1 day) [ACK-YED4HY]\n"
    "   An unverified response is treated as no response.\n"
)


@pytest.fixture()
def seat(tmp_path, monkeypatch):
    """A seat whose internal send is autonomous, as the pilot's is.

    Uses the roster/exposure stub idiom from ``test_recipient_aware_send.py``
    rather than a yaml fixture, because what this file needs is specifically an
    ``external_send_internal`` classification, and that comes from the recipient
    matching the roster.
    """
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {
            enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW,
            enforce.ActionClass.EXTERNAL_SEND_INTERNAL: enforce.Ceiling.AUTONOMOUS,
        },
    )
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: list(ROSTER))
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "testco")
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "operator")
    # The routine this session belongs to. Read from cron_attribution in
    # production; stubbed here so the test does not need a cron store on disk.
    monkeypatch.setattr(spec_gate, "_routine_skill", lambda _sid: "deadline-miss-escalator")


@pytest.fixture()
def floor_calls(monkeypatch):
    """Spy on the gate WITHOUT replacing it, so behaviour is still the real one."""
    calls: list[dict] = []
    original = spec_gate.check_structure_floor

    def spy(**kw):
        calls.append(kw)
        return original(**kw)

    monkeypatch.setattr(spec_gate, "check_structure_floor", spy)
    return calls


@pytest.fixture()
def rows(monkeypatch):
    emitted: list[dict] = []
    monkeypatch.setattr(spec_gate, "_emit_spec_gate_audit", lambda **kw: emitted.append(kw))
    return emitted


def _send(body: str):
    return enforce.evaluate_tool_call(
        SEND, {"to": [STAFF], "text": body}, "testco", session_id=SID, tool_call_id="tc-1"
    )


def test_evaluate_tool_call_reaches_the_structure_floor(seat, floor_calls, rows):
    """The wiring assertion. Delete the call site and this is the test that fails."""
    _send(SHAPELESS)
    assert floor_calls, (
        "evaluate_tool_call did not call check_structure_floor — the gate is "
        "defined but not wired, which is the exact failure class this feature "
        "was built to end"
    )


def test_the_body_the_floor_sees_is_the_body_being_sent(seat, floor_calls, rows):
    """A gate handed the wrong bytes measures nothing."""
    _send(SHAPELESS)
    assert "NEEDS YOU TODAY" in (floor_calls[0]["body"] or "")


def test_a_shapeless_staff_digest_still_sends_and_leaves_a_row(seat, floor_calls, rows):
    """End to end: proceed, and say so in the ledger."""
    verdict = _send(SHAPELESS)
    assert verdict is None, "a staff digest must not be withheld for being ugly"
    assert [r for r in rows if r["reason"] == "structure_floor"]


def test_a_structured_digest_leaves_no_structure_row(seat, floor_calls, rows):
    """The control half. Without this, the test above passes on a gate that
    stamps every send regardless of what it contains."""
    _send(STRUCTURED)
    assert floor_calls, "the floor should still have been consulted"
    assert not [r for r in rows if r["reason"] == "structure_floor"]
