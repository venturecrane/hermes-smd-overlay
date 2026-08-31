"""Tests for the out-of-band AgentMail send seam (ADR 0071 / #1806, ss#2258).

This module used to test a REST transport: inbox resolution from an account
listing, Bearer auth, and HTTP error mapping. **All of that moved to the broker**
after four fabricated messages reached a real client principal with no audit row
(ss#2258) — proof that a recipient check living in the agent process can be
skipped by a path that never reaches it, and that a credential living here can be
used by whatever gets to it.

So what is left to test is deliberately small, and the most valuable assertions
are about ABSENCE: this module can no longer name a sending inbox, cannot carry a
credential, and cannot resolve identity from an account listing. The behaviour
that remains is payload shaping and error typing.

The fence itself (which recipients are permitted, which inbox is used, that a row
is written) is tested broker-side in ss-console
``operator/workspace_broker/tests/test_agentmail_send.py``, where it is enforced.
"""

from __future__ import annotations

import pytest

from tests.conftest import load_plugin


def _load():
    return load_plugin("hermes-smd-trust").outbound_send


def _sender(captured, message_id="msg_out", joins=None):
    """A stand-in for the broker client that records the body it was handed.

    Accepts the ss-console#2497 audit kwargs the real client takes, and records
    them separately when a test asks: they must reach the BROKER (which writes
    the row) and must never join the vendor body, which is what the allowlist
    assertions below prove.
    """

    def _send(body, *, session_id="", matter_ref=None):
        captured.append(body)
        if joins is not None:
            joins.append((session_id, matter_ref))
        return message_id

    return _send


# ---------------------------------------------------------------------------
# The authority that is no longer here (ss#2258)
# ---------------------------------------------------------------------------


def test_the_module_cannot_resolve_a_sending_inbox_anymore():
    """Identity resolution is gone, not merely unused.

    ``resolve_inbox_id`` picked this seat's mailbox out of an account-wide
    listing, and a bug in it had every seat ready to send as whichever inbox was
    created most recently. A dead copy left behind is an invitation to call it,
    so its absence is the assertion.
    """
    mod = _load()
    for gone in ("resolve_inbox_id", "seat_inbox_address", "_request_json"):
        assert not hasattr(mod, gone), f"{gone} must not come back into the agent process"


def test_send_takes_no_credential_and_no_inbox():
    """The signature is the control: there is no argument through which a caller
    could specify who the message is from."""
    import inspect

    params = set(inspect.signature(_load().send_message).parameters)
    # audit_extra (WS-RENDER) is attribution FOR the broker's row, filtered
    # broker-side through a closed allowlist — not an identity or credential
    # channel, so the control below still holds.
    assert params == {"payload", "sender", "session_id", "matter_ref", "audit_extra"}
    # The control is what is ABSENT: no way to name the From or hand over a key.
    assert not params & {"from", "sender_address", "inbox_id", "api_key", "token"}


# ---------------------------------------------------------------------------
# Payload shaping — the part that stayed
# ---------------------------------------------------------------------------


def test_send_message_forwards_only_allowlisted_fields():
    """Nothing beyond the closed body allowlist reaches the wire.

    Internal keys (a broker grant, the stripped approval marker) live on the
    stored args; forwarding one would leak overlay bookkeeping to the vendor.
    """
    mod = _load()
    captured: list[dict] = []
    mod.send_message(
        payload={
            "to": ["a@b.com"],
            "cc": ["c@d.com"],
            "subject": "s",
            "text": "t",
            "html": "<p>t</p>",
            "reply_to": "r@s.com",
            "_smd_workspace_grant": "internal",
            "_approved": True,
            "from": "someone-else@agentmail.to",
        },
        sender=_sender(captured),
    )
    assert captured[0] == {
        "to": ["a@b.com"],
        "cc": ["c@d.com"],
        "subject": "s",
        "text": "t",
        "html": "<p>t</p>",
        "reply_to": "r@s.com",
    }


def test_send_message_refuses_without_recipient():
    mod = _load()
    captured: list[dict] = []
    with pytest.raises(mod.AgentMailSendError):
        mod.send_message(payload={"text": "hi"}, sender=_sender(captured))
    assert captured == []


def test_send_message_tolerates_missing_message_id():
    """A 2xx with no id is still a successful send; the turn must not fail."""
    mod = _load()
    out = mod.send_message(payload={"to": "a@b.com", "text": "hi"}, sender=lambda _b, **_k: "")
    assert out == "(sent, id unavailable)"


# ---------------------------------------------------------------------------
# Error typing — a refusal and an outage must not read alike
# ---------------------------------------------------------------------------


def test_a_broker_refusal_maps_to_send_error_and_keeps_its_reason():
    """The operator sees WHY, not a generic delivery failure."""
    mod = _load()
    broker = load_plugin("hermes-smd-trust").outbound_send.agentmail_broker

    def _refuse(_body, **_kwargs):
        raise broker.BrokerError("recipient is not on this seat's authored surface")

    with pytest.raises(mod.AgentMailSendError, match="authored surface"):
        mod.send_message(payload={"to": "a@b.com", "text": "hi"}, sender=_refuse)


def test_an_unreachable_broker_is_not_reported_as_a_refusal():
    """ "You may not write to this person" is a lie when the socket is down."""
    mod = _load()
    broker = load_plugin("hermes-smd-trust").outbound_send.agentmail_broker

    def _down(_body, **_kwargs):
        raise broker.AgentMailBrokerUnavailable("socket missing")

    with pytest.raises(mod.AgentMailSendError, match="unavailable"):
        mod.send_message(payload={"to": "a@b.com", "text": "hi"}, sender=_down)
