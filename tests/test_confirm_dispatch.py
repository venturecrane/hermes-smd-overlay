"""Out-of-band dispatch of an approved confirm send (ADR 0071 / #1806 harden).

The overlay dispatches the approved send itself rather than waiting for the LLM to
re-invoke the send tool. These tests pin the safety-critical behavior: the dispatch
re-authorizes through the SAME gate (so taint / content-floor still withhold),
sends the STORED payload, is single-use (consumes), and fails safe.
"""

from __future__ import annotations

import pytest

from shared.inbound import SESSION_TAINT, TRUST_CLASS_UNKNOWN_EXTERNAL
from shared.outbound_recipient import DRAFT_RECIPIENTS
from shared.pending_send import PENDING_SEND
from tests.conftest import load_plugin

ALLOWED = "7367659986"
TO = "client@example.com"


def _trust():
    return load_plugin("hermes-smd-trust")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ALLOWED)
    yield
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()


def _arm(monkeypatch, trust, *, sent):
    """Author confirm exposure, stub creds + the AgentMail transport (no network)."""
    enforce = trust.enforce
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.CONFIRM},
    )
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: ["scott@smd.services"])
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "agent-crane")
    monkeypatch.setattr(trust, "get_secret", lambda k: "am_key")

    # ss#2258: no api_key and no inbox_id to arm — the broker holds the key and
    # pins the inbox from the seat's own config. The dispatch path can express
    # only the payload, which is the point.
    def _send(*, payload, **k):
        sent.append(payload)
        return "msg_1"

    monkeypatch.setattr(trust.outbound_send, "send_message", _send)


def _capture_and_approve(trust, body="the reviewed body", session="s1"):
    """Withhold a send (captures a pending) then mark it approved."""
    trust.enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": [TO], "subject": "Report", "text": body},
        "scott",
        session_id=session,
    )
    assert PENDING_SEND.peek() is not None
    PENDING_SEND.mark_approved(f"telegram:{ALLOWED}")


def test_dispatch_sends_stored_payload_and_consumes(monkeypatch):
    trust = _trust()
    sent = []
    _arm(monkeypatch, trust, sent=sent)
    _capture_and_approve(trust)
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None and "Dispatched" in ctx and TO in ctx
    # The STORED payload was sent (not a re-composition).
    assert len(sent) == 1
    assert sent[0]["text"] == "the reviewed body" and sent[0]["to"] == [TO]
    # Single-use: the approval is consumed.
    assert PENDING_SEND.peek() is None


def test_taint_blocks_dispatch_and_preserves_pending(monkeypatch):
    trust = _trust()
    sent = []
    _arm(monkeypatch, trust, sent=sent)
    _capture_and_approve(trust, session="s-clean")
    # Re-dispatch on a TAINTED session — the gate must withhold before sending.
    SESSION_TAINT.mark("s-tainted", TRUST_CLASS_UNKNOWN_EXTERNAL)
    ctx = trust._dispatch_approved_send("s-tainted", "scott")
    assert ctx is not None and "not dispatched" in ctx.lower()
    assert sent == []  # nothing left the box
    assert PENDING_SEND.peek() is not None  # approval preserved, not burned


def test_send_failure_reports_and_does_not_claim_delivery(monkeypatch):
    trust = _trust()
    _arm(monkeypatch, trust, sent=[])

    def _boom(*a, **k):
        raise trust.outbound_send.AgentMailSendError("HTTP 500")

    monkeypatch.setattr(trust.outbound_send, "send_message", _boom)
    _capture_and_approve(trust)
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None and "could not be delivered" in ctx.lower()


def test_no_approved_pending_is_noop(monkeypatch):
    trust = _trust()
    _arm(monkeypatch, trust, sent=[])
    # A pending exists but is NOT approved → nothing to dispatch.
    trust.enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": [TO], "text": "x"}, "scott", session_id="s1"
    )
    assert trust._dispatch_approved_send("s1", "scott") is None


def test_pre_llm_call_end_to_end_dispatches(monkeypatch):
    trust = _trust()
    sent = []
    _arm(monkeypatch, trust, sent=sent)
    # Turn 1: withhold (captures pending).
    trust.enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": [TO], "text": "body one"}, "scott", session_id="s1"
    )
    # Turn 2: the owner's Telegram affirmative flows through the real hook.
    out = trust.on_pre_llm_call(
        session_id="s1", platform="telegram", sender_id=ALLOWED, user_message="Yes, send it"
    )
    assert isinstance(out, dict) and "Dispatched" in out["context"]
    assert len(sent) == 1 and sent[0]["text"] == "body one"
    assert PENDING_SEND.peek() is None
