"""Where the structure floor sits, and what it costs per class (ss#2090).

Two properties this suite pins by convention rather than by hope, following
``test_matter_gate_placement.py``:

1. **Placement.** The check runs OUTSIDE the ``decision.allowed`` guard. My first
   draft of this feature put it beside the spec gate, which sits under
   ``if decision.allowed and decision.effective_ceiling == Ceiling.AUTONOMOUS``.
   That would have been inert on every ``draft_for_review`` seat — the cautious
   ones — while the plan argued the supervised path mattered most.

2. **Disposition.** ``staff`` proceeds. A refusal there costs the firm the
   message itself, silently, which is the 2026-08-04..09 outage and the
   2026-08-19 five-refusals-in-a-row loop. The audit row IS the alert.
"""

from __future__ import annotations

from shared import message_structure as ms
from shared import spec_gate

UNSTRUCTURED = (
    "Something happened today and here is a long flat sentence about it with no structure at all."
)


def _emitted(monkeypatch):
    """Capture the audit rows this gate writes instead of writing them."""
    rows: list[dict] = []

    def _fake(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(spec_gate, "_emit_spec_gate_audit", _fake)
    return rows


def _as_escalator(monkeypatch):
    monkeypatch.setattr(spec_gate, "_routine_skill", lambda _sid: "deadline-miss-escalator")


def test_staff_proceeds_and_the_row_is_written(monkeypatch):
    """The disposition that keeps the six-day outage from recurring."""
    rows = _emitted(monkeypatch)
    _as_escalator(monkeypatch)

    verdict = spec_gate.check_structure_floor(
        tool_name="smd_send_message",
        action_class_value="external_send_internal",
        session_id="cron_x_20260825_070034",
        body=UNSTRUCTURED,
        allowed=True,
    )

    assert verdict is None, "a staff digest must still send"
    assert len(rows) == 1
    assert rows[0]["reason"] == "structure_floor"
    assert rows[0]["output_class"] == "staff"


def test_the_audit_detail_is_rule_names_and_carries_no_body_text(monkeypatch):
    rows = _emitted(monkeypatch)
    _as_escalator(monkeypatch)
    spec_gate.check_structure_floor(
        tool_name="smd_send_message",
        action_class_value="external_send_internal",
        session_id="cron_x_20260825_070034",
        body=UNSTRUCTURED,
        allowed=True,
    )
    detail = rows[0]["detail"]
    assert isinstance(detail, str)
    assert "no_heading" in detail
    assert "Something happened" not in detail


def test_an_outbound_class_routes_to_a_human(monkeypatch):
    """The firm's name on a shapeless message is worth the delay."""
    _emitted(monkeypatch)
    monkeypatch.setattr(spec_gate, "_routine_skill", lambda _sid: "medical-records-chaser")

    verdict = spec_gate.check_structure_floor(
        tool_name="smd_send_message",
        action_class_value="external_send_vendor",
        session_id="cron_x_20260825_070034",
        body=UNSTRUCTURED,
        allowed=True,
    )
    assert verdict is not None
    assert verdict["action"] == "block"
    assert "draft" in verdict["message"].lower()


def test_a_withheld_send_is_not_blocked_twice(monkeypatch):
    """``allowed=False`` already means a person is reading it."""
    rows = _emitted(monkeypatch)
    monkeypatch.setattr(spec_gate, "_routine_skill", lambda _sid: "medical-records-chaser")

    verdict = spec_gate.check_structure_floor(
        tool_name="smd_send_message",
        action_class_value="external_send_vendor",
        session_id="cron_x_20260825_070034",
        body=UNSTRUCTURED,
        allowed=False,
    )
    assert verdict is None
    # ...but the row is still written. This is the placement property: the CHECK
    # ran on a draft-ceiling send. Inside `decision.allowed` it never would have.
    assert len(rows) == 1


def test_the_check_runs_on_a_draft_ceiling_seat(monkeypatch):
    """The placement pin, stated as the property rather than the mechanism.

    Falsifier: if the call site is ever moved inside the ``decision.allowed``
    guard, ``allowed=False`` produces no row and this fails.
    """
    rows = _emitted(monkeypatch)
    _as_escalator(monkeypatch)
    spec_gate.check_structure_floor(
        tool_name="smd_send_message",
        action_class_value="external_send_internal",
        session_id="cron_x_20260825_070034",
        body=UNSTRUCTURED,
        allowed=False,
    )
    assert len(rows) == 1, "the floor must observe sends the ceiling already withheld"


def test_an_unmapped_routine_writes_nothing(monkeypatch):
    rows = _emitted(monkeypatch)
    monkeypatch.setattr(spec_gate, "_routine_skill", lambda _sid: "inbox-triage")
    verdict = spec_gate.check_structure_floor(
        tool_name="smd_send_message",
        action_class_value="external_send_internal",
        session_id="s",
        body=UNSTRUCTURED,
        allowed=True,
    )
    assert verdict is None
    assert rows == []


def test_a_conforming_body_writes_nothing(monkeypatch):
    rows = _emitted(monkeypatch)
    _as_escalator(monkeypatch)
    verdict = spec_gate.check_structure_floor(
        tool_name="smd_send_message",
        action_class_value="external_send_internal",
        session_id="cron_x_20260825_070034",
        body="## Needs you today (1)\n\n1. matter 2026-PI-101, due 2026-08-26.\n",
        allowed=True,
    )
    assert verdict is None
    assert rows == []


def test_a_thrown_error_proceeds_rather_than_costing_the_message(monkeypatch):
    """A legibility check must never be the reason a deadline alert vanishes."""
    _emitted(monkeypatch)

    def _boom(_sid):
        raise RuntimeError("attribution exploded")

    monkeypatch.setattr(spec_gate, "_routine_skill", _boom)
    assert (
        spec_gate.check_structure_floor(
            tool_name="smd_send_message",
            action_class_value="external_send_internal",
            session_id="s",
            body=UNSTRUCTURED,
            allowed=True,
        )
        is None
    )


def test_the_reason_is_not_the_customers_format_violation():
    """Misattribution guard.

    ``format_violation`` means the CUSTOMER's authored rules broke. Reusing it
    would tell an operator the firm's format rule failed on a seat where the firm
    authored nothing.
    """
    assert spec_gate._REASON_STRUCTURE_FLOOR == "structure_floor"
    assert spec_gate._REASON_STRUCTURE_FLOOR != spec_gate._REASON_FORMAT_VIOLATION


def test_the_escalator_and_the_daily_digest_both_bind():
    """Both banded skills, including the one with no pre-run handoff at all.

    ``daily-needs-you-digest`` never calls ``write_handoff``. A design that read
    the family from the handoff would have lost it silently, forever.
    """
    assert ms.family_for_skill("daily-needs-you-digest") == ms.BANDED_DIGEST
    assert ms.family_for_skill("deadline-miss-escalator") == ms.BANDED_DIGEST
