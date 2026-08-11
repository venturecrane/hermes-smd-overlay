"""Where the matter-identity gate sits in ``evaluate_tool_call`` (ss#2167).

This file exists because the defect it pins was shipped twice in design review
and is invisible in a unit test of the gate itself.

The gate must run even when the trust ceiling has ALREADY withheld the send. On
a seat where every outbound send sits at ``draft_for_review`` — which is the
day-one posture of the client seat this control is for — ``decision.allowed`` is
False for every send. A check placed inside the ``if decision.allowed`` block
(where the content floor and voice gate correctly live, since they only need to
downgrade an otherwise-permitted send) would therefore never execute on the seat
that most needs it, and the whole control would be dead while every unit test
of its verdict logic passed.

Second thing pinned here: the re-record on that path must carry the FULL
original trail. Rebuilding the row from scratch would write ``authored_ceiling``
/ ``vertical_floor`` / ``effective_ceiling`` back to None and re-break the
null-ceiling defect #2122 fixed.
"""

from __future__ import annotations

import pytest

from shared import matter_binding
from tests.conftest import load_plugin
from tests.test_exposure_override import CUSTOMER_YAML

trust = load_plugin("hermes-smd-trust")
enforce = trust.enforce

SID = "s-placement"
M_A = "aaaaaaaa-1111-2222-3333-444444444444"
CLIENT_A = "alvarez@example.com"
CLIENT_B = "okafor@example.com"
SEND = "mcp_agentmail_send_message"


@pytest.fixture()
def seat(tmp_path, monkeypatch):
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(CUSTOMER_YAML)
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(yaml_path))
    monkeypatch.setenv("SMD_EXPOSURE_OVERRIDE_DB_PATH", str(tmp_path / "ovr.db"))
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "testco")
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    monkeypatch.setenv("SMD_MATTER_GATE_MODE", "block")
    matter_binding._reset_for_tests()
    yield
    matter_binding._reset_for_tests()


@pytest.fixture()
def recorded(monkeypatch):
    """Capture every _record_decision call so the re-record can be inspected."""
    calls: list[dict] = []
    original = enforce._record_decision

    def spy(tool_call_id, tool_name, persona_slug, **kw):
        calls.append(kw)
        return original(tool_call_id, tool_name, persona_slug, **kw)

    monkeypatch.setattr(enforce, "_record_decision", spy)
    return calls


def _seed_closed_party_set() -> None:
    matter_binding.membership_for(SID).add(M_A, [CLIENT_A], complete=True)


def _send(to: str, body: str):
    return enforce.evaluate_tool_call(
        SEND, {"to": to, "text": body}, "testco", session_id=SID, tool_call_id="tc-1"
    )


def test_gate_runs_even_though_the_ceiling_already_withheld(seat, recorded) -> None:
    # The seat authors external_send_client = draft_for_review, so decision.allowed
    # is False before the matter gate is reached. The verdict must still be
    # computed and stamped — otherwise the control is dead on this posture.
    _seed_closed_party_set()
    _send(CLIENT_B, f"Regarding matter {M_A}, please see attached.")

    stamped = [c for c in recorded if "MATTER_MISMATCH" in (c.get("reason") or "")]
    assert stamped, (
        "no MATTER_MISMATCH was recorded on a draft-ceiling send — the gate is "
        "sitting inside the `if decision.allowed` block again, where it can never "
        "run on the client seat's day-one posture"
    )


def test_the_restamp_preserves_the_full_ceiling_trail(seat, recorded) -> None:
    # #2122: a row rebuilt from scratch nulls these and re-breaks the ledger.
    _seed_closed_party_set()
    _send(CLIENT_B, f"Regarding matter {M_A}, please see attached.")

    stamped = [c for c in recorded if "MATTER_MISMATCH" in (c.get("reason") or "")]
    assert stamped
    row = stamped[-1]
    assert row.get("effective_ceiling") is not None
    assert row.get("action_class")
    assert row.get("audit_action")


def test_control_correct_pairing_stamps_nothing(seat, recorded) -> None:
    # The half that makes the two tests above mean something: if every send were
    # stamped, the assertions would pass with a gate that had no verdict logic.
    _seed_closed_party_set()
    _send(CLIENT_A, f"Regarding matter {M_A}, please see attached.")

    stamped = [c for c in recorded if "MATTER_MISMATCH" in (c.get("reason") or "")]
    assert not stamped


def test_report_mode_stamps_nothing(seat, recorded, monkeypatch) -> None:
    monkeypatch.setenv("SMD_MATTER_GATE_MODE", "report")
    _seed_closed_party_set()
    _send(CLIENT_B, f"Regarding matter {M_A}, please see attached.")

    stamped = [c for c in recorded if "MATTER_MISMATCH" in (c.get("reason") or "")]
    assert not stamped
