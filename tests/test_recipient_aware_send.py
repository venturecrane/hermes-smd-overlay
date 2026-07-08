"""Recipient-aware proactive send — overlay runtime tests.

The fix for the "nothing ever sends" root: a proactive send to a rostered
internal recipient is governed by ``external_send_internal`` (autonomous-capable),
while an outside send stays ``external_send`` (gated). An unresolved recipient is
forced OUTSIDE (draft), never INTERNAL. ``send_draft`` resolves its recipient from
a draft recorded at ``create_draft`` time via the per-session registry.
"""

from __future__ import annotations

import pytest

from shared.outbound_recipient import (
    DRAFT_RECIPIENTS,
    DraftRecipientRegistry,
    extract_to_recipients,
    record_draft_from_post_tool_call,
    send_recipients,
)
from tests.conftest import load_plugin

ROSTER = ["@ashtonandprice.com", "scott@smd.services"]


def _load_enforce():
    return load_plugin("hermes-smd-trust").enforce


@pytest.fixture(autouse=True)
def _clear_registry():
    DRAFT_RECIPIENTS._by_key.clear()
    yield
    DRAFT_RECIPIENTS._by_key.clear()


# ---------------------------------------------------------------------------
# outbound_recipient unit tests
# ---------------------------------------------------------------------------


def test_extract_to_recipients_list_and_single_and_displayname():
    assert extract_to_recipients({"to": ["A@B.com", "c@d.com"]}) == {"a@b.com", "c@d.com"}
    assert extract_to_recipients({"to": "scott@smd.services"}) == {"scott@smd.services"}
    # parseaddr takes the bracketed routable address (defeats display-name spoof).
    assert extract_to_recipients({"to": "Scott <scott@smd.services>"}) == {"scott@smd.services"}
    # A malformed display-name-with-@ ("addr <addr>") is rejected by parseaddr →
    # empty → dropped. Fail-closed and safe: the spoof display text never becomes
    # the routed recipient, and an incomplete recipient set routes the send OUTSIDE.
    assert extract_to_recipients({"to": "scott@smd.services <evil@x.com>"}) == set()
    assert extract_to_recipients({}) == set()


def test_registry_record_and_lookup():
    reg = DraftRecipientRegistry()
    reg.record("s1", "d1", {"scott@smd.services"})
    assert reg.lookup("s1", "d1") == {"scott@smd.services"}
    assert reg.lookup("s1", "unknown") is None
    assert reg.lookup("other-session", "d1") is None  # session-scoped


def test_registry_eviction_is_bounded():
    reg = DraftRecipientRegistry(max_entries=2)
    reg.record("s", "d1", {"a@b.com"})
    reg.record("s", "d2", {"a@b.com"})
    reg.record("s", "d3", {"a@b.com"})  # evicts d1
    assert reg.lookup("s", "d1") is None
    assert reg.lookup("s", "d3") == {"a@b.com"}


def test_send_recipients_direct_to():
    args = {"to": ["scott@smd.services"]}
    assert send_recipients("mcp_agentmail_send_message", args, "s1") == {"scott@smd.services"}


def test_send_recipients_send_draft_resolves_from_registry():
    record_draft_from_post_tool_call(
        "mcp_agentmail_create_draft",
        {"to": ["scott@smd.services"]},
        {"id": "draft-123"},
        "s1",
    )
    got = send_recipients("mcp_agentmail_send_draft", {"draft_id": "draft-123"}, "s1")
    assert got == {"scott@smd.services"}


def test_send_recipients_unrecorded_draft_is_none():
    assert send_recipients("mcp_agentmail_send_draft", {"draft_id": "nope"}, "s1") is None


def test_record_extracts_draft_id_from_nested_and_json_results():
    record_draft_from_post_tool_call(
        "mcp_agentmail_create_draft", {"to": ["a@b.com"]}, {"draft": {"id": "nested-1"}}, "s"
    )
    assert send_recipients("mcp_agentmail_send_draft", {"draft_id": "nested-1"}, "s") == {"a@b.com"}
    record_draft_from_post_tool_call(
        "mcp_agentmail_create_draft", {"to": ["a@b.com"]}, '{"id": "json-1"}', "s"
    )
    assert send_recipients("mcp_agentmail_send_draft", {"draft_id": "json-1"}, "s") == {"a@b.com"}


# ---------------------------------------------------------------------------
# evaluate_tool_call end-to-end reclassification
# ---------------------------------------------------------------------------


def _setup(monkeypatch, enforce, *, exposure):
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", lambda slug="": dict(exposure))
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: list(ROSTER))
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "quinn")


def _exposure(enforce):
    return {
        enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW,
        enforce.ActionClass.EXTERNAL_SEND_INTERNAL: enforce.Ceiling.AUTONOMOUS,
    }


def test_send_message_to_roster_is_autonomous(monkeypatch):
    enforce = _load_enforce()
    _setup(monkeypatch, enforce, exposure=_exposure(enforce))
    # Internal (rostered) send → external_send_internal autonomous → ALLOWED (None).
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["scott@smd.services"]}, "smd", session_id="s1"
    )
    assert result is None


def test_send_message_to_outside_drafts(monkeypatch):
    enforce = _load_enforce()
    _setup(monkeypatch, enforce, exposure=_exposure(enforce))
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["client@example.com"]}, "smd", session_id="s1"
    )
    assert result is not None and result["action"] == "block"
    assert "draft" in result["message"].lower()


def test_send_draft_to_roster_after_create_is_autonomous(monkeypatch):
    enforce = _load_enforce()
    _setup(monkeypatch, enforce, exposure=_exposure(enforce))
    # Simulate the real flow: create_draft (recorded at post_tool_call) then send_draft.
    record_draft_from_post_tool_call(
        "mcp_agentmail_create_draft", {"to": ["scott@smd.services"]}, {"id": "d-live"}, "s1"
    )
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_draft", {"draft_id": "d-live"}, "smd", session_id="s1"
    )
    assert result is None  # the 2026-07-08 attorney alert now SENDS instead of drafting


def test_unresolved_send_draft_drafts_never_autonomous(monkeypatch):
    enforce = _load_enforce()
    _setup(monkeypatch, enforce, exposure=_exposure(enforce))
    # No create_draft recorded → recipient unresolved → OUTSIDE (draft), never internal.
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_draft", {"draft_id": "never-seen"}, "smd", session_id="s1"
    )
    assert result is not None and result["action"] == "block"
    assert "draft" in result["message"].lower()


def test_internal_send_unauthored_is_fail_closed(monkeypatch):
    enforce = _load_enforce()
    # Only external_send authored; external_send_internal unauthored → refused.
    _setup(
        monkeypatch,
        enforce,
        exposure={enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW},
    )
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["scott@smd.services"]}, "smd", session_id="s1"
    )
    assert result is not None and result["action"] == "block"
    assert "refused" in result["message"].lower()


def test_outside_send_still_content_floored_internal_is_not(monkeypatch):
    enforce = _load_enforce()
    # Both send classes autonomous; law floor pins ONLY the outside class.
    exposure = {
        enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS,
        enforce.ActionClass.EXTERNAL_SEND_INTERNAL: enforce.Ceiling.AUTONOMOUS,
    }
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", lambda slug="": dict(exposure))
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: list(ROSTER))
    monkeypatch.setattr(
        enforce,
        "_resolve_vertical_floors",
        lambda: {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW},
    )
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "quinn")
    # Outside send: law floor draws autonomous → draft.
    outside = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["client@example.com"]}, "smd", session_id="s1"
    )
    assert outside is not None and "draft" in outside["message"].lower()
    # Internal send: floor does not touch it → autonomous send.
    internal = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["scott@smd.services"]}, "smd", session_id="s2"
    )
    assert internal is None
