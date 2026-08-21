"""The agent-side client for the broker's transmit verbs (ss#2258).

What these tests are really pinning is an ABSENCE. Before this seam, sending was
a REST call from the agent process holding an account-wide key, and the recipient
check lived in the same process a rogue path had already proven it could skip —
four fabricated messages reached a real client principal with no audit row.

So the valuable assertions here are about what this module can no longer do or
say: it carries no credential, it cannot name a sending inbox, and it cannot name
a reply's recipient. Whether a given recipient is ALLOWED is not tested here at
all, because it is not decided here — that lives broker-side in ss-console's
``operator/workspace_broker/tests/test_agentmail_send.py``.
"""

from __future__ import annotations

import inspect

import pytest

from shared import agentmail_broker


def test_no_function_here_accepts_a_credential():
    """A key parameter would mean the agent still holds sending authority."""
    for fn in (agentmail_broker.send_message, agentmail_broker.send_reply):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"api_key", "key", "token", "auth"}


def test_reply_cannot_name_its_recipient():
    """Anyone on the internet can email a seat's inbox.

    The reply's recipient is derived by the broker from the source message, so
    this module deliberately has no argument for it — a caller cannot aim a
    reply at an address of its choosing.

    Pinned as an EXACT set, still: an allowlist that merely forbids today's
    recipient spellings would pass the day someone adds ``deliver_to``. The two
    audit joins added by ss-console#2497 (``session_id`` / ``matter_ref``) are
    named here deliberately, so extending this signature stays a decision
    somebody makes on purpose rather than one that slips through. Neither can
    address a message: they are written to the audit row and never forwarded to
    the vendor body, which ``test_audit_joins_ride_beside_the_payload`` proves.
    """
    params = set(inspect.signature(agentmail_broker.send_reply).parameters)
    assert params == {"message_id", "text", "html", "session_id", "matter_ref"}
    assert not params & {"to", "cc", "bcc", "recipient", "recipients", "deliver_to"}


def test_transmit_is_unavailable_without_a_broker_socket(monkeypatch):
    """Fail closed with a nameable reason, rather than raising from deep inside."""
    monkeypatch.delenv(agentmail_broker.SOCKET_ENV, raising=False)
    assert agentmail_broker.transmit_available() is False
    with pytest.raises(agentmail_broker.AgentMailBrokerUnavailable):
        agentmail_broker.send_message({"to": ["a@b.example"], "text": "x"})
    with pytest.raises(agentmail_broker.AgentMailBrokerUnavailable):
        agentmail_broker.send_reply("m1", text="x")


def test_send_forwards_the_payload_and_returns_the_id(monkeypatch):
    captured: dict = {}

    def _request(payload, timeout=None):
        captured.update(payload=payload, timeout=timeout)
        return {"ok": True, "message_id": "msg_1"}

    monkeypatch.setenv(agentmail_broker.SOCKET_ENV, "/run/broker.sock")
    monkeypatch.setattr(agentmail_broker, "request", _request)
    out = agentmail_broker.send_message({"to": ["a@b.example"], "text": "hi"})
    assert out == "msg_1"
    assert captured["payload"]["action"] == "agentmail_send"
    assert captured["payload"]["payload"] == {"to": ["a@b.example"], "text": "hi"}


def test_reply_sends_only_the_body_parts_it_was_given(monkeypatch):
    captured: dict = {}

    def _request(payload, timeout=None):
        captured.update(payload)
        return {"ok": True, "message_id": "msg_2"}

    monkeypatch.setenv(agentmail_broker.SOCKET_ENV, "/run/broker.sock")
    monkeypatch.setattr(agentmail_broker, "request", _request)
    assert agentmail_broker.send_reply("m1", text="hello") == "msg_2"
    assert captured["action"] == "agentmail_reply"
    # No empty html key: an empty body part must not look like an authored one.
    assert captured["payload"] == {"message_id": "m1", "text": "hello"}


def test_a_socket_failure_is_not_a_refusal(monkeypatch):
    """The distinction the ledger depends on.

    "You may not write to this person" and "the socket was down" are different
    facts, and recording the second as the first would put a false statement in
    the audit trail in the trail's own vocabulary.
    """

    def _boom(payload, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setenv(agentmail_broker.SOCKET_ENV, "/run/broker.sock")
    monkeypatch.setattr(agentmail_broker, "request", _boom)
    with pytest.raises(agentmail_broker.AgentMailBrokerUnavailable):
        agentmail_broker.send_message({"to": ["a@b.example"], "text": "x"})


def test_a_broker_refusal_propagates_as_a_broker_error(monkeypatch):
    """Refusals must stay distinguishable from outages all the way up."""

    def _refuse(payload, timeout=None):
        raise agentmail_broker.BrokerError("recipient not on the authored surface")

    monkeypatch.setenv(agentmail_broker.SOCKET_ENV, "/run/broker.sock")
    monkeypatch.setattr(agentmail_broker, "request", _refuse)
    with pytest.raises(agentmail_broker.BrokerError, match="authored surface"):
        agentmail_broker.send_message({"to": ["a@b.example"], "text": "x"})
