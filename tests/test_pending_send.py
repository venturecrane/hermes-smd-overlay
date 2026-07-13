"""Tests for the pending-send approval register (ADR 0071 / #1806).

The register is the process-wide handoff for the confirm-over-channel round-trip:
the trust gate captures a withheld send and later consumes an approved record,
replaying the STORED payload; the approval-capture step marks it approved. These
tests pin the invariants the critique demanded: stored-payload replay (no reliance
on the LLM reproducing content), single-outstanding-pending, content-bound
consume, single-use, and TTL.
"""

from __future__ import annotations

from shared.pending_send import PENDING_SEND, PendingSendRegister

TOOL = "mcp_agentmail_send_message"


def _reg() -> PendingSendRegister:
    """A fresh register per test — never the process singleton, so tests don't
    contaminate each other."""
    return PendingSendRegister()


def _args(body: str = "the report is ready") -> dict:
    return {"to": "bob@acme.com", "subject": "Report", "text": body}


# ---------------------------------------------------------------------------
# Happy path: capture -> approve -> consume, replaying the stored payload
# ---------------------------------------------------------------------------


def test_capture_approve_consume_returns_stored_payload():
    reg = _reg()
    reg.capture(TOOL, _args(), {"bob@acme.com"})
    assert reg.mark_approved("telegram:7367659986") is True
    rec = reg.take_for_send(TOOL, {"bob@acme.com"})
    assert rec is not None
    assert rec.args == _args()
    assert rec.approval_source == "telegram:7367659986"
    # single-use: the record is consumed and gone.
    assert reg.take_for_send(TOOL, {"bob@acme.com"}) is None
    assert reg.peek() is None


def test_stored_payload_survives_mutation_of_the_original_args():
    """The store deep-copies at capture, so later mutation of the live tool-args
    dict (SEC-36 strip, broker grant, the overwrite itself) cannot corrupt what
    ships. This is the core of stored-payload replay."""
    reg = _reg()
    original = _args("original body")
    reg.capture(TOOL, original, {"bob@acme.com"})
    original["text"] = "TAMPERED"
    original["to"] = "attacker@evil.com"
    reg.mark_approved("telegram:7367659986")
    rec = reg.take_for_send(TOOL, {"bob@acme.com"})
    assert rec is not None
    assert rec.args["text"] == "original body"
    assert rec.args["to"] == "bob@acme.com"


def test_body_drift_ships_stored_not_regenerated():
    """The consume returns the captured body regardless of what the re-invoked
    turn would have regenerated — the gate replays rec.args, ignoring the live
    call's body."""
    reg = _reg()
    reg.capture(TOOL, _args("carefully reviewed body"), {"bob@acme.com"})
    reg.mark_approved("telegram:7367659986")
    rec = reg.take_for_send(TOOL, {"bob@acme.com"})
    assert rec is not None
    assert rec.args["text"] == "carefully reviewed body"


# ---------------------------------------------------------------------------
# Withhold cases — nothing consumes without a valid, matching approval
# ---------------------------------------------------------------------------


def test_no_approval_does_not_consume():
    reg = _reg()
    reg.capture(TOOL, _args(), {"bob@acme.com"})
    assert reg.take_for_send(TOOL, {"bob@acme.com"}) is None


def test_recipient_mismatch_re_withholds():
    reg = _reg()
    reg.capture(TOOL, _args(), {"bob@acme.com"})
    reg.mark_approved("telegram:7367659986")
    # approved for bob; a re-invoke to alice must not consume the approval.
    assert reg.take_for_send(TOOL, {"alice@acme.com"}) is None
    # the record is untouched — still available for the correct recipient.
    assert reg.take_for_send(TOOL, {"bob@acme.com"}) is not None


def test_tool_mismatch_re_withholds():
    reg = _reg()
    reg.capture(TOOL, _args(), {"bob@acme.com"})
    reg.mark_approved("telegram:7367659986")
    assert reg.take_for_send("mcp_agentmail_send_draft", {"bob@acme.com"}) is None


def test_extra_recipient_on_reinvoke_re_withholds():
    """Adding a recipient on the second turn changes the set → no match. Combined
    with stored-payload replay, this closes the cc/bcc-injection channel."""
    reg = _reg()
    reg.capture(TOOL, _args(), {"bob@acme.com"})
    reg.mark_approved("telegram:7367659986")
    assert reg.take_for_send(TOOL, {"bob@acme.com", "attacker@evil.com"}) is None


# ---------------------------------------------------------------------------
# Single-outstanding-pending invariant
# ---------------------------------------------------------------------------


def test_new_compose_supersedes_and_resets_approval():
    reg = _reg()
    reg.capture(TOOL, _args("first"), {"bob@acme.com"})
    reg.mark_approved("telegram:7367659986")
    # A new compose (to a different recipient) supersedes the approved record.
    reg.capture(TOOL, _args("second"), {"carol@acme.com"})
    # The old approval is gone; the new record is not approved.
    assert reg.take_for_send(TOOL, {"bob@acme.com"}) is None
    assert reg.take_for_send(TOOL, {"carol@acme.com"}) is None
    # Approving now approves the NEW record only.
    reg.mark_approved("telegram:7367659986")
    rec = reg.take_for_send(TOOL, {"carol@acme.com"})
    assert rec is not None and rec.args["text"] == "second"


def test_mark_approved_on_empty_register_is_noop():
    reg = _reg()
    assert reg.mark_approved("telegram:7367659986") is False


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


def test_expired_record_cannot_be_approved():
    reg = _reg()
    reg.capture(TOOL, _args(), {"bob@acme.com"})
    reg._record.created_at -= reg.ttl_seconds + 1  # backdate past the TTL
    assert reg.mark_approved("telegram:7367659986") is False


def test_expired_record_is_not_consumed_even_if_approved():
    reg = _reg()
    reg.capture(TOOL, _args(), {"bob@acme.com"})
    reg.mark_approved("telegram:7367659986")
    reg._record.created_at -= reg.ttl_seconds + 1
    assert reg.take_for_send(TOOL, {"bob@acme.com"}) is None
    # expiry clears the stale record.
    assert reg.peek() is None


# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------


def test_process_singleton_exists_and_is_a_register():
    assert isinstance(PENDING_SEND, PendingSendRegister)
    PENDING_SEND.clear()
    assert PENDING_SEND.peek() is None
