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
M_B = "bbbbbbbb-1111-2222-3333-444444444444"
MEMOS = "mcp_smokeball_get_memos_on_matter"
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


# A seat where a CLIENT send is actually permitted. The seat fixture above
# authors draft_for_review, which means decision.allowed is False for every send
# — fine for the placement tests, useless for the falsifier below, which needs a
# permitted send for an enforcing regression to show up in.
AUTONOMOUS_YAML = """
schema_version: "1"
customer_id: testco
scope:
  outbound_roster:
    - address: alvarez@example.com
      class: client
personas:
  - slug: marcus
    entitlements:
      exposure:
        external_send_client: autonomous
        internal_write: autonomous
"""


@pytest.fixture()
def autonomous_seat(tmp_path, monkeypatch):
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(AUTONOMOUS_YAML)
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(yaml_path))
    monkeypatch.setenv("SMD_EXPOSURE_OVERRIDE_DB_PATH", str(tmp_path / "ovr.db"))
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "testco")
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    monkeypatch.setenv("SMD_MATTER_GATE_MODE", "block")
    matter_binding._reset_for_tests()
    yield
    matter_binding._reset_for_tests()


# ---------------------------------------------------------------------------
# The MIXING signal (ss#2167) — observe-only
# ---------------------------------------------------------------------------


def _content_read(matter_id: str) -> None:
    matter_binding.record_from_read(SID, "{}", tool_name=MEMOS, args={"matter_id": matter_id})


def test_multi_matter_session_is_stamped(seat, recorded) -> None:
    _content_read(M_A)
    _content_read(M_B)
    _send(CLIENT_A, "Following up as discussed.")

    stamped = [c for c in recorded if "MULTI_MATTER_SESSION" in (c.get("reason") or "")]
    assert stamped, (
        "a send in a session that read two matters' content was not annotated — "
        "the mixing signal is not reaching the audit row"
    )


def test_control_single_matter_read_stamps_nothing(seat, recorded) -> None:
    """The half that makes the test above mean something. Without it, a detector
    that annotated EVERY send would pass."""
    _content_read(M_A)
    _send(CLIENT_A, "Following up as discussed.")

    stamped = [c for c in recorded if "MULTI_MATTER_SESSION" in (c.get("reason") or "")]
    assert not stamped


def test_both_fragments_survive_on_one_send(seat, recorded) -> None:
    """The worst send there is: it cites a matter the recipient is not a party to
    AND was composed in a session that read two matters.

    ``TrustDecisionRegister.record`` overwrites unconditionally by
    ``tool_call_id``, so two ``_record_decision`` calls would not produce two
    annotations — the second would erase the first. This test fails if anyone
    splits the re-record back into two calls."""
    _seed_closed_party_set()
    _content_read(M_A)
    _content_read(M_B)
    _send(CLIENT_B, f"Regarding matter {M_A}, please see attached.")

    both = [
        c
        for c in recorded
        if "MATTER_MISMATCH" in (c.get("reason") or "")
        and "MULTI_MATTER_SESSION" in (c.get("reason") or "")
    ]
    assert both, (
        "the mismatch and mixing annotations did not survive on the same row — "
        "one _record_decision call clobbered the other"
    )


def test_multi_matter_changes_no_decision(autonomous_seat, recorded) -> None:
    """THE FALSIFIER for Phase 1 — and it is run on an AUTONOMOUS posture on
    purpose.

    The first version of this test ran on the seat's authored
    ``draft_for_review``, where ``decision.allowed`` is False for every send. A
    deliberate mutation making the mixing signal ENFORCING (``if multi_read and
    decision.allowed: return block``) left all eight tests green — the mutant
    branch simply never executed. The test could not fail, so it had measured
    nothing, which is the exact defect class this file's header warns about.

    ``autonomous_seat`` types the recipient as a client and authors that class
    autonomous, so ``decision.allowed`` is True and an enforcing regression has
    somewhere to show up. Comparing with-signal against without-signal then pins that the mixing
    verdict alters no outcome."""
    _content_read(M_A)
    baseline = _send(CLIENT_A, "Following up as discussed.")
    assert baseline is None or baseline.get("action") != "block", (
        "the baseline send was not permitted — this test cannot detect an "
        "enforcing regression and would pass vacuously"
    )

    matter_binding._reset_for_tests()
    _content_read(M_A)
    _content_read(M_B)
    with_signal = _send(CLIENT_A, "Following up as discussed.")

    assert with_signal == baseline, (
        "a multi-matter session changed the send decision — Phase 1 is supposed "
        "to be observe-only and is now enforcing"
    )
