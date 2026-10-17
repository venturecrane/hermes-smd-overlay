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


# ---------------------------------------------------------------------------
# The approved-send lane meets the outbound scans (overlay PR #399)
#
# evaluate_tool_call alone never ran the fabrication scan, the identifier gate
# or the output checklist. An approval approves recipients and intent; it must
# not launder a UTC clock into a sent email.
# ---------------------------------------------------------------------------

STAFF = "scott@smd.services"


@pytest.fixture
def checklist_rows(monkeypatch):
    import json

    from shared import spec_gate

    calls: list = []

    class _Fake:
        def execute(self, _sql, *params):
            calls.append(params)
            return 1

    monkeypatch.setattr(spec_gate, "_AUDIT_CLIENT", _Fake())
    monkeypatch.setattr(spec_gate, "_AUDIT_CUSTOMER_SLUG", "scott")
    monkeypatch.setattr(spec_gate, "_AUDIT_WIRED", True)

    def rows():
        out = []
        for params in calls:
            if "SPEC_GATE_TRIGGERED" in params:
                out.append(json.loads(params[-1]))
        return out

    return rows


@pytest.fixture
def notices(monkeypatch):
    from shared import send_dispatch

    sent: list = []

    def fake_sender(**kwargs):
        sent.append(kwargs)
        return send_dispatch.DispatchResult(sent=True, recipients=tuple(kwargs["to"]))

    monkeypatch.setattr(send_dispatch, "_SENDER", fake_sender)
    return sent


def _arm_staff(monkeypatch, trust, *, sent):
    _arm(monkeypatch, trust, sent=sent)
    enforce = trust.enforce
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {
            enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.CONFIRM,
            enforce.ActionClass.EXTERNAL_SEND_INTERNAL: enforce.Ceiling.CONFIRM,
        },
    )


def _capture_staff(trust, body, approver=f"telegram:{ALLOWED}"):
    trust.enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": [STAFF], "subject": "Garcia hearing", "text": body},
        "scott",
        session_id="s1",
    )
    assert PENDING_SEND.peek() is not None
    PENDING_SEND.mark_approved(approver)


def test_an_approved_send_carrying_a_utc_clock_is_not_sent(monkeypatch, checklist_rows, notices):
    trust = _trust()
    sent = []
    _arm_staff(monkeypatch, trust, sent=sent)
    _capture_staff(trust, "The Garcia hearing is at 16:30.")
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert sent == []
    assert ctx is not None and "not dispatched" in ctx and "'16:30'" in ctx
    assert [r["reason"] for r in checklist_rows()] == ["output_checklist"]
    assert len(notices) == 1
    assert notices[0]["to"] == [STAFF]
    assert notices[0]["subject"] == "Withheld: Garcia hearing"
    assert "16:30" not in notices[0]["text"]


def test_an_approver_with_an_address_is_told_too(monkeypatch, checklist_rows, notices):
    trust = _trust()
    _arm_staff(monkeypatch, trust, sent=[])
    _capture_staff(trust, "The Garcia hearing is at 16:30.", approver="email:christa@firm.example")
    trust._dispatch_approved_send("s1", "scott")
    (notice,) = notices
    assert notice["to"] == ["christa@firm.example", STAFF]


def test_an_approved_send_with_a_clean_body_still_goes_out(monkeypatch, checklist_rows, notices):
    trust = _trust()
    sent = []
    _arm_staff(monkeypatch, trust, sent=sent)
    _capture_staff(trust, "The Garcia hearing is at 9:30 a.m. in Dept 31.")
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None and "Dispatched" in ctx
    assert len(sent) == 1
    assert checklist_rows() == []
    assert notices == []


# ---------------------------------------------------------------------------
# The approved send is scanned under the session that COMPOSED it (OT-2)
# ---------------------------------------------------------------------------

_VISIT_BODY = "Thanks for visiting the open house on 2026-09-12. Happy to answer questions"


def _arm_identifier_gate(monkeypatch, trust, *, sent):
    """Confirm exposure with the identifier gate live and every other content
    gate stubbed open, so only the read register decides the scan."""
    enforce = trust.enforce
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "agent")
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.CONFIRM},
    )
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: [])
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: [])
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setattr(enforce, "_apply_content_floor", lambda *a, **k: None)
    monkeypatch.setattr(enforce.voice_gate, "check_voice_gate", lambda **k: None)
    monkeypatch.setattr(enforce.spec_gate, "check_spec_gate", lambda **k: None)
    monkeypatch.setattr(enforce.spec_gate, "check_structure_floor", lambda **k: None)
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: "agentmail")
    monkeypatch.setattr(
        trust.outbound_send, "send_message", lambda *, payload, **k: sent.append(payload) or "m"
    )


def _hold_and_approve(trust, compose_session):
    trust.enforce.evaluate_tool_call(
        "smd_send_message",
        {"to": ["visitor@example.com"], "subject": "Open house", "text": _VISIT_BODY},
        "brokery",
        session_id=compose_session,
    )
    assert PENDING_SEND.peek() is not None
    PENDING_SEND.mark_approved(f"telegram:{ALLOWED}")


def test_an_approval_in_another_session_scans_under_the_composing_session(monkeypatch):
    """The approval arrives on a Telegram DM whose read register is empty. The
    identifier gate must judge the body against what the COMPOSING session read,
    or every correct body citing a read date is refused after approval.

    FALSIFIER: scan under the dispatch ``session_id`` instead of
    ``rec.session_id`` and this send is refused IDENTIFIER_UNVERIFIED.
    """
    from shared import provenance

    trust = _trust()
    sent: list = []
    _arm_identifier_gate(monkeypatch, trust, sent=sent)
    provenance.record_read("cron_compose", "Visitor record: visit date 2026-09-12")
    _hold_and_approve(trust, "cron_compose")
    ctx = trust._dispatch_approved_send("telegram_dm", "brokery")
    assert len(sent) == 1, ctx
    assert sent[0]["text"] == _VISIT_BODY
    assert PENDING_SEND.peek() is None  # spent only once it passed


def test_a_body_that_fails_in_its_own_session_is_still_refused(monkeypatch):
    """The fix moves WHERE the scan runs, not WHETHER: a date the composing
    session never read is refused, nothing is sent, and the approval is
    withdrawn rather than spent (the record stays pending for a fresh yes)."""
    trust = _trust()
    sent: list = []
    _arm_identifier_gate(monkeypatch, trust, sent=sent)
    _hold_and_approve(trust, "cron_unread")
    ctx = trust._dispatch_approved_send("telegram_dm", "brokery")
    assert sent == []
    assert ctx is not None and "not dispatched" in ctx
    rec = PENDING_SEND.peek()
    assert rec is not None and not rec.consumed and not rec.approved
