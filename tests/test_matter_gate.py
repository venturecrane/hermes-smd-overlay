"""Outbound matter-identity gate (ss#2167).

The P0 shape under test: a letter composed from case A's material, addressed to
someone attached to case B. Every test that asserts a withhold is paired with a
control asserting the correct pairing passes — a gate that withholds everything
would satisfy the first half alone and would have measured nothing.
"""

from __future__ import annotations

import pytest

from shared import matter_binding
from tests.conftest import load_plugin

matter_gate = load_plugin("hermes-smd-trust").matter_gate

SID = "s1"
M_A = "aaaaaaaa-1111-2222-3333-444444444444"
M_B = "bbbbbbbb-1111-2222-3333-444444444444"
CLIENT_A = "alvarez@example.com"
CLIENT_B = "okafor@example.com"


@pytest.fixture(autouse=True)
def _clean():
    matter_binding._reset_for_tests()
    yield
    matter_binding._reset_for_tests()


def _closed(matter_id: str, *emails: str) -> None:
    """The matter's own complete party list was read."""
    matter_binding.membership_for(SID).add(matter_id, emails, complete=True)


def _open(matter_id: str, *emails: str) -> None:
    """Contact-keyed: this person is a party; the full set is unknown."""
    matter_binding.membership_for(SID).add(matter_id, emails, complete=False)


# ---- the P0 -----------------------------------------------------------------


def test_case_a_body_to_case_b_client_is_a_mismatch() -> None:
    _closed(M_A, CLIENT_A)
    _closed(M_B, CLIENT_B)
    v = matter_gate.evaluate(
        session_id=SID,
        body=f"Regarding matter {M_A}, please find the deposition summary attached.",
        recipients={CLIENT_B},
    )
    assert v.is_mismatch and v.should_withhold
    assert CLIENT_B in v.reason


def test_control_correct_pairing_passes() -> None:
    # The half that makes the test above mean something.
    _closed(M_A, CLIENT_A)
    v = matter_gate.evaluate(
        session_id=SID,
        body=f"Regarding matter {M_A}, please find the deposition summary attached.",
        recipients={CLIENT_A},
    )
    assert v.status == "ok"
    assert not v.should_withhold


# ---- unresolved must never masquerade as non-membership ---------------------


def test_open_party_set_yields_unresolved_not_mismatch() -> None:
    # Contact-keyed capture proves CLIENT_A is on M_A; it says nothing about
    # whether CLIENT_B is. Absence from an open set is not evidence.
    _open(M_A, CLIENT_A)
    v = matter_gate.evaluate(session_id=SID, body=f"matter {M_A}", recipients={CLIENT_B})
    assert v.status == "unresolved"
    assert not v.should_withhold


def test_unresolved_does_not_withhold() -> None:
    # A control that blocks correct work gets removed rather than fixed.
    v = matter_gate.evaluate(session_id=SID, body=f"matter {M_A}", recipients={CLIENT_A})
    assert v.status == "unresolved"
    assert not v.should_withhold


def test_open_set_still_passes_a_proven_party() -> None:
    _open(M_A, CLIENT_A)
    v = matter_gate.evaluate(session_id=SID, body=f"matter {M_A}", recipients={CLIENT_A})
    assert v.status == "ok"


def test_closed_set_upgrade_is_monotonic() -> None:
    # An open read after a closed one must not reopen the set.
    _closed(M_A, CLIENT_A)
    _open(M_A, CLIENT_B)
    assert matter_binding.membership_for(SID).is_closed(M_A)


# ---- scope ------------------------------------------------------------------


def test_body_citing_no_matter_is_not_applicable() -> None:
    v = matter_gate.evaluate(session_id=SID, body="Thanks, will do.", recipients={CLIENT_B})
    assert v.status == "not_applicable"


def test_exempt_recipient_class_is_skipped() -> None:
    # Firm staff and records vendors are not expected to be parties; the roster
    # that says so is the client's, not ours.
    _closed(M_A, CLIENT_A)
    v = matter_gate.evaluate(
        session_id=SID, body=f"matter {M_A}", recipients={"records@vendor.example"},
        recipient_is_exempt=True,
    )
    assert v.status == "not_applicable"


def test_matter_never_read_is_unresolved_not_mismatch() -> None:
    # A number nobody read is not evidence about anybody.
    v = matter_gate.evaluate(session_id=SID, body="matter 2026-PI-999", recipients={CLIENT_A})
    assert v.status == "unresolved"


def test_mixed_recipients_one_offender_is_a_mismatch() -> None:
    _closed(M_A, CLIENT_A)
    v = matter_gate.evaluate(
        session_id=SID, body=f"matter {M_A}", recipients={CLIENT_A, CLIENT_B}
    )
    assert v.is_mismatch


# ---- posture ----------------------------------------------------------------


def test_mode_is_fail_closed_on_a_typo(monkeypatch) -> None:
    monkeypatch.setenv("SMD_MATTER_GATE_MODE", "repot")
    assert matter_gate.mode() == "block"
    monkeypatch.setenv("SMD_MATTER_GATE_MODE", "off")
    assert matter_gate.mode() == "block"
    monkeypatch.setenv("SMD_MATTER_GATE_MODE", "report")
    assert matter_gate.mode() == "report"


def test_evaluation_never_raises() -> None:
    v = matter_gate.evaluate(session_id=SID, body=None, recipients={CLIENT_A})  # type: ignore[arg-type]
    assert v.status in {"not_applicable", "unresolved"}


# ---- capture ----------------------------------------------------------------


def test_capture_reads_a_closed_party_list_from_get_matter() -> None:
    matter_binding.record_from_read(
        SID,
        {
            "id": M_A,
            "parties": [{"contact_id": "c1", "email": CLIENT_A, "side": "client"}],
            "parties_complete": True,
        },
    )
    assert matter_binding.membership_for(SID).is_closed(M_A)
    assert CLIENT_A in matter_binding.membership_for(SID).parties(M_A)


def test_capture_binds_contact_to_matters_across_two_reads() -> None:
    # The reply lane's shape: the contact is read first, the contact-filtered
    # matter listing second, as separate tool calls.
    matter_binding.record_from_read(SID, {"id": "c9", "person": {"email": CLIENT_A}})
    matter_binding.record_from_read(
        SID, {"matters_for_contact": "c9", "value": [{"id": M_A}, {"id": M_B}]}
    )
    m = matter_binding.membership_for(SID)
    assert m.matters_for(CLIENT_A) == {M_A, M_B}
    # Open by nature — it can prove membership, never non-membership.
    assert not m.is_closed(M_A)


def test_incomplete_party_list_is_captured_but_not_closed() -> None:
    matter_binding.record_from_read(
        SID,
        {"id": M_A, "parties": [{"email": CLIENT_A}], "parties_complete": False},
    )
    m = matter_binding.membership_for(SID)
    assert CLIENT_A in m.parties(M_A)
    assert not m.is_closed(M_A)
