"""An ack records WHO confirmed, from evidence, or records nobody (ss#2152).

The commitment made to the firm is that every confirmation is logged on the
matter with the attorney's name and a timestamp. Before this, the ack recorded
that somebody quoting a valid code confirmed and threw the identity away: the
sender was tested as a boolean roster gate and dropped, and the ledger schema
had no field to hold it.

The failure this prevents is specific and legal, not cosmetic. A write-back
built on a guess produces an affirmative FALSE record on a matter -- "Chris
confirmed" when Dana replied -- and the obvious sources for that guess are
all wrong: the model composes, Smokeball ``createdBy`` under
``auth_mode: authorization_code`` is whoever clicked Allow during setup, and an
email display name is attacker-controlled.

So the tests below pin four properties:

  * the name comes from the firm's OWN authored ``users[].full_name``;
  * the identity comes from the turn's Svix-verified inbound origin, never from
    a tool argument the model can name;
  * an unverified or unauthored sender attributes NOBODY, and the row simply
    carries no ``acked_by``;
  * the broker refuses a malformed payload and refuses the field entirely on any
    event kind that is not an ack.
"""

from __future__ import annotations

import json

import pytest

from shared import escalation_ledger, inbound
from tests.conftest import load_plugin

ADDRESS = "dana@firm.example"
NAME = "Dana Whitfield"


@pytest.fixture
def escalation(monkeypatch):
    plugin = load_plugin("hermes-smd-escalation")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "id": "evt-1"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    monkeypatch.setattr(plugin, "_resolved_session", lambda _kwargs: "sess-1")
    # A prior raise the ack can reference; the plugin resolves the token against
    # the ledger, so without one the append is refused for an unrelated reason.
    monkeypatch.setattr(
        plugin, "_resolve_token_identity", lambda _token: ("item-key-1", "matter-1")
    )
    return plugin, requests


def _origin(session_id: str = "sess-1", address: str = ADDRESS) -> None:
    inbound.SESSION_INBOUND_ORIGIN.record(
        session_id,
        inbound.InboundOrigin(sender_address=address, message_id=f"msg-{address}"),
    )


def _authored(monkeypatch, plugin, users: list[dict]) -> None:
    """Stand in for the seat volume with an authored users list.

    The REAL ``CustomerConfig`` does the resolving -- only ``from_volume`` is
    replaced. A fake that reimplemented the lookup would be testing the fake.
    (Captured before the patch: reading ``plugin.CustomerConfig`` inside the
    stand-in would resolve to the stand-in itself.)
    """
    real = plugin.CustomerConfig

    class _Config:
        @staticmethod
        def from_volume():
            return real({"users": users})

    monkeypatch.setattr(plugin, "CustomerConfig", _Config)


def _ack(plugin) -> dict:
    return json.loads(
        plugin._escalation_append(
            {"skill": "deadline-miss-escalator", "event": "acked", "attempt": 0,
             "ack_token": "ACK-ABC123"}
        )
    )


def _written(requests: list[dict]) -> dict:
    appends = [r for r in requests if r.get("action") == "escalation_event_append"]
    assert appends, "no append reached the broker"
    return appends[-1]["event"]


# ---------------------------------------------------------------------------
# The capture
# ---------------------------------------------------------------------------


def test_an_ack_from_a_verified_authored_sender_names_them(escalation, monkeypatch):
    plugin, requests = escalation
    _origin()
    _authored(monkeypatch, plugin, [{"email": ADDRESS, "full_name": NAME}])

    _ack(plugin)

    acked_by = _written(requests)["acked_by"]
    assert acked_by["name"] == NAME
    assert len(acked_by["key"]) == 64


def test_the_name_is_the_firms_authored_one_not_anything_off_the_wire(escalation, monkeypatch):
    """The seat has exactly one sanctioned source for a person's name."""
    plugin, requests = escalation
    _origin()
    _authored(monkeypatch, plugin, [{"email": ADDRESS, "full_name": "D. Whitfield, Esq."}])

    _ack(plugin)
    assert _written(requests)["acked_by"]["name"] == "D. Whitfield, Esq."


def test_the_model_cannot_name_the_confirmer(escalation):
    """Same posture as ``session_id``: ``additionalProperties: false`` means the
    field is unreachable from a tool argument. If the model could pass it, the
    control would be an instruction rather than a control."""
    plugin, _ = escalation
    schema = plugin._APPEND_SCHEMA
    assert schema.get("additionalProperties") is False
    assert "acked_by" not in schema.get("properties", {})


def test_an_unverified_session_attributes_nobody(escalation, monkeypatch):
    """No bound origin means no evidence of who replied. The row carries no
    attribution rather than a plausible one -- AC4's negative control."""
    plugin, requests = escalation
    _authored(monkeypatch, plugin, [{"email": ADDRESS, "full_name": NAME}])
    monkeypatch.setattr(plugin, "_resolved_session", lambda _kwargs: "no-origin-session")

    _ack(plugin)
    assert "acked_by" not in _written(requests)


def test_a_verified_sender_the_firm_never_authored_attributes_nobody(escalation, monkeypatch):
    """They may well be entitled to reply. This seat still has no sanctioned name
    for them, and must not invent one."""
    plugin, requests = escalation
    _origin(address="stranger@elsewhere.example")
    monkeypatch.setattr(plugin, "_resolved_session", lambda _kwargs: "sess-stranger")
    inbound.SESSION_INBOUND_ORIGIN.record(
        "sess-stranger",
        inbound.InboundOrigin(sender_address="stranger@elsewhere.example", message_id="m-x"),
    )
    _authored(monkeypatch, plugin, [{"email": ADDRESS, "full_name": NAME}])

    _ack(plugin)
    assert "acked_by" not in _written(requests)


def test_an_unreadable_config_attributes_nobody_rather_than_raising(escalation, monkeypatch):
    plugin, requests = escalation
    _origin()

    class _Boom:
        @staticmethod
        def from_volume():
            raise RuntimeError("volume unreadable")

    monkeypatch.setattr(plugin, "CustomerConfig", _Boom)

    _ack(plugin)
    assert "acked_by" not in _written(requests)


def test_only_acks_carry_a_confirmer(escalation, monkeypatch):
    """A raise has no confirming person. Stamping one would assert that whoever
    happened to open the session endorsed an alarm they only triggered."""
    plugin, requests = escalation
    _origin()
    _authored(monkeypatch, plugin, [{"email": ADDRESS, "full_name": NAME}])

    derived = json.loads(
        plugin._escalation_append(
            {"skill": "deadline-miss-escalator", "event": "fired", "attempt": 1,
             "matter_id": "m-1", "source_id": "task-1", "label": "task-deadline",
             "authored_date": "2026-09-02", "derive_only": True}
        )
    )
    json.loads(
        plugin._escalation_append(
            {"skill": "deadline-miss-escalator", "event": "fired", "attempt": 1,
             "append_handle": derived["append_handle"]}
        )
    )
    assert "acked_by" not in _written(requests)


# ---------------------------------------------------------------------------
# The broker's half
# ---------------------------------------------------------------------------


def _base_ack() -> dict:
    return {
        "v": escalation_ledger.SCHEMA_VERSION,
        "ts": None,
        "skill": "deadline-miss-escalator",
        "matter_id": "m-1",
        "item_key": "k1",
        "event": "acked",
        "attempt": 0,
        "token": "ACK-ABC123",
    }


def _prior_raise() -> dict:
    return {**_base_ack(), "event": "fired", "attempt": 1}


def _validate(event: dict) -> None:
    escalation_ledger.validate_append(
        [_prior_raise()], event, send_witness=lambda _e: True
    )


def test_the_broker_accepts_a_well_formed_confirmer():
    _validate({**_base_ack(), "acked_by": {"name": NAME, "key": "a" * 64}})


def test_the_broker_refuses_a_confirmer_on_a_non_ack():
    with pytest.raises(ValueError, match="no such person"):
        escalation_ledger.validate_append(
            [],
            {**_base_ack(), "event": "fired", "attempt": 1,
             "acked_by": {"name": NAME, "key": "a" * 64}},
            send_witness=lambda _e: True,
        )


def test_the_broker_refuses_a_name_with_no_key():
    """Unjoinable: nothing ties the assertion to the message that carried it."""
    with pytest.raises(ValueError, match="acked_by.key"):
        _validate({**_base_ack(), "acked_by": {"name": NAME}})


def test_the_broker_refuses_a_key_with_no_name():
    """A hash cannot be written into a memo a human reads, which is the point."""
    with pytest.raises(ValueError, match="acked_by.name"):
        _validate({**_base_ack(), "acked_by": {"key": "a" * 64}})


def test_the_broker_refuses_unknown_fields():
    with pytest.raises(ValueError, match="unknown fields"):
        _validate(
            {**_base_ack(), "acked_by": {"name": NAME, "key": "a" * 64, "role": "principal"}}
        )


def test_the_broker_refuses_a_key_that_is_not_a_sha256():
    with pytest.raises(ValueError, match="64 lowercase hex"):
        _validate({**_base_ack(), "acked_by": {"name": NAME, "key": "not-a-hash"}})


def test_the_broker_refuses_a_name_long_enough_to_be_a_body():
    with pytest.raises(ValueError, match="acked_by.name"):
        _validate({**_base_ack(), "acked_by": {"name": "x" * 200, "key": "a" * 64}})


def test_an_ack_with_no_confirmer_is_still_valid():
    """Unattributed is a state the record is allowed to be in, and must stay
    writable: the alternative is refusing to record a confirmation that really
    happened because the seat could not prove who sent it."""
    _validate(_base_ack())
