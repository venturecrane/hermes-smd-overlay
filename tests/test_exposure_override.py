"""Tests for the runtime entitlement dial (ss#2003 Q7).

Three layers, all through real state:

1. ``shared.exposure_override`` — the volume store: atomic batch set, the
   write-time clamp against the authored ``exposure_ceiling``, persistence
   across re-open (the restart-survival property in miniature).
2. ``webhook_gate._entitlement_set`` — the gate's pure core: validation,
   rejection mapping (clamp refusal → 409), fault mapping (store fault → 500).
3. ``hermes-smd-trust`` enforce — the effective exposure the plugin actually
   resolves: an override changes the decision of ``evaluate_tool_call``
   through a REAL customer.yaml on disk and a REAL override db, in both
   directions, with the read-side clamp narrowing an excessive row.
"""

from __future__ import annotations

import sqlite3

import pytest

from shared import exposure_override
from tests.conftest import load_plugin

enforce = load_plugin("hermes-smd-trust").enforce


CUSTOMER_YAML = """
schema_version: "1"
customer_id: testco
personas:
  - slug: marcus
    entitlements:
      exposure:
        external_send_client: draft_for_review
        internal_write: autonomous
      exposure_ceiling:
        external_send_client: autonomous
"""


@pytest.fixture()
def seat(tmp_path, monkeypatch):
    """A provisioned-seat stand-in: real customer.yaml + real override db."""
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(CUSTOMER_YAML)
    db = tmp_path / "exposure_override.db"
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(yaml_path))
    monkeypatch.setenv("SMD_EXPOSURE_OVERRIDE_DB_PATH", str(db))
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "testco")
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    return db


# ---------------------------------------------------------------------------
# Layer 1 — the store
# ---------------------------------------------------------------------------


def test_set_and_read_round_trip(seat) -> None:
    result = exposure_override.set_overrides(
        persona="marcus",
        changes=[{"action_class": "external_send_client", "ceiling": "autonomous"}],
        actor_id="user-1",
        reason="graduating client verification",
    )
    assert result["applied"] == [{"action_class": "external_send_client", "ceiling": "autonomous"}]
    assert exposure_override.read_overrides("marcus") == {"external_send_client": "autonomous"}


def test_raise_above_authored_ceiling_rejected(seat) -> None:
    # internal_write has no exposure_ceiling → bound is its authored value
    # (autonomous), so autonomous is fine; but external_send has NO authored
    # exposure and NO ceiling → bound refused → any grant is rejected.
    with pytest.raises(ValueError, match="external_send.*exceeds the authored ceiling"):
        exposure_override.set_overrides(
            persona="marcus",
            changes=[{"action_class": "external_send", "ceiling": "draft_for_review"}],
            actor_id="user-1",
            reason="attempt",
        )


def test_batch_is_atomic_on_any_rejection(seat) -> None:
    with pytest.raises(ValueError):
        exposure_override.set_overrides(
            persona="marcus",
            changes=[
                {"action_class": "external_send_client", "ceiling": "autonomous"},  # allowed
                {"action_class": "external_send", "ceiling": "autonomous"},  # rejected
            ],
            actor_id="user-1",
            reason="mixed batch",
        )
    assert exposure_override.read_overrides("marcus") == {}


def test_lowering_always_allowed_and_reversible(seat) -> None:
    exposure_override.set_overrides(
        persona="marcus",
        changes=[{"action_class": "internal_write", "ceiling": "draft_for_review"}],
        actor_id="user-1",
        reason="dial down",
    )
    assert exposure_override.read_overrides("marcus") == {"internal_write": "draft_for_review"}
    # ...and back up to (but not past) the authored value.
    exposure_override.set_overrides(
        persona="marcus",
        changes=[{"action_class": "internal_write", "ceiling": "autonomous"}],
        actor_id="user-1",
        reason="dial back up",
    )
    assert exposure_override.read_overrides("marcus") == {"internal_write": "autonomous"}


def test_persists_across_reopen(seat) -> None:
    exposure_override.set_overrides(
        persona="marcus",
        changes=[{"action_class": "external_send_client", "ceiling": "confirm"}],
        actor_id="user-1",
        reason="restart survival",
    )
    # Fresh connection == process restart from the store's point of view.
    assert exposure_override.read_overrides("marcus") == {"external_send_client": "confirm"}
    rows = exposure_override.read_all()
    assert rows[0]["actor_id"] == "user-1"
    assert rows[0]["customer"] == "testco"


def test_unknown_vocabulary_rejected(seat) -> None:
    with pytest.raises(ValueError, match="unknown action class"):
        exposure_override.set_overrides(
            persona="marcus",
            changes=[{"action_class": "read", "ceiling": "refused"}],
            actor_id="u",
            reason="r",
        )
    with pytest.raises(ValueError, match="unknown ceiling"):
        exposure_override.set_overrides(
            persona="marcus",
            changes=[{"action_class": "internal_write", "ceiling": "yolo"}],
            actor_id="u",
            reason="r",
        )


# ---------------------------------------------------------------------------
# Layer 2 — the gate pure core
# ---------------------------------------------------------------------------


def test_gate_core_success_and_rejection(seat) -> None:
    import webhook_gate

    status, body = webhook_gate._entitlement_set(
        {
            "persona": "marcus",
            "changes": [{"action_class": "external_send_client", "ceiling": "autonomous"}],
            "actor_id": "user-1",
            "reason": "graduate",
        }
    )
    assert status == 200
    assert body["applied"]

    status, body = webhook_gate._entitlement_set(
        {
            "persona": "marcus",
            "changes": [{"action_class": "external_send", "ceiling": "autonomous"}],
            "actor_id": "user-1",
            "reason": "too far",
        }
    )
    assert status == 409
    assert "exceeds the authored ceiling" in body["detail"]


def test_gate_core_validation(seat) -> None:
    import webhook_gate

    assert webhook_gate._entitlement_set({})[0] == 400
    assert webhook_gate._entitlement_set({"persona": "m", "actor_id": "a", "reason": "r"})[0] == 400


def test_gate_core_store_fault_is_500(seat, monkeypatch) -> None:
    import webhook_gate

    # Point the store at a path whose parent is a FILE — connect() must fail.
    blocker = seat.parent / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("SMD_EXPOSURE_OVERRIDE_DB_PATH", str(blocker / "x.db"))
    status, body = webhook_gate._entitlement_set(
        {
            "persona": "marcus",
            "changes": [{"action_class": "internal_write", "ceiling": "autonomous"}],
            "actor_id": "u",
            "reason": "r",
        }
    )
    assert status == 500


# ---------------------------------------------------------------------------
# Layer 3 — enforcement through the real plugin
# ---------------------------------------------------------------------------


def _decision(tool: str) -> dict | None:
    return enforce.evaluate_tool_call(tool, {"to": "client@example.com"}, session_id="s1")


def test_enforce_honors_raised_override(seat) -> None:
    # Authored: external_send_client = draft_for_review → a send is blocked.
    exposure = enforce._resolve_persona_exposure("marcus")
    assert exposure[enforce.ActionClass.EXTERNAL_SEND_CLIENT] is enforce.Ceiling.DRAFT_FOR_REVIEW

    exposure_override.set_overrides(
        persona="marcus",
        changes=[{"action_class": "external_send_client", "ceiling": "autonomous"}],
        actor_id="user-1",
        reason="graduate client sends",
    )
    exposure = enforce._resolve_persona_exposure("marcus")
    assert exposure[enforce.ActionClass.EXTERNAL_SEND_CLIENT] is enforce.Ceiling.AUTONOMOUS


def test_enforce_honors_lowered_override(seat) -> None:
    exposure_override.set_overrides(
        persona="marcus",
        changes=[{"action_class": "internal_write", "ceiling": "draft_for_review"}],
        actor_id="user-1",
        reason="dial down",
    )
    exposure = enforce._resolve_persona_exposure("marcus")
    assert exposure[enforce.ActionClass.INTERNAL_WRITE] is enforce.Ceiling.DRAFT_FOR_REVIEW


def test_enforce_read_side_clamp_narrows_excessive_row(seat) -> None:
    # Bypass the write-time clamp by inserting directly — simulate a corrupt
    # or tampered store row granting autonomy with no authored permission.
    conn = sqlite3.connect(str(seat))
    conn.execute(exposure_override._CREATE_TABLE_SQL)
    conn.execute(
        "INSERT INTO exposure_override VALUES (?,?,?,?,?,?,?)",
        ("testco", "marcus", "external_send", "autonomous", "attacker", "tamper", "now"),
    )
    conn.commit()
    conn.close()
    exposure = enforce._resolve_persona_exposure("marcus")
    # No authored exposure, no ceiling → bound REFUSED → the row is narrowed.
    assert exposure[enforce.ActionClass.EXTERNAL_SEND] is enforce.Ceiling.REFUSED


def test_enforce_no_override_resolves_authored(seat) -> None:
    exposure = enforce._resolve_persona_exposure("marcus")
    assert exposure[enforce.ActionClass.EXTERNAL_SEND_CLIENT] is enforce.Ceiling.DRAFT_FOR_REVIEW
    assert exposure[enforce.ActionClass.INTERNAL_WRITE] is enforce.Ceiling.AUTONOMOUS


def test_enforce_store_fault_propagates(seat, monkeypatch) -> None:
    # A corrupt store must NOT silently fall back to authored (which could be
    # more autonomous than the client's lowered posture).
    seat.write_text("this is not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        enforce._resolve_persona_exposure("marcus")
