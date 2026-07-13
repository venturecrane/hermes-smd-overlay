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


# ===========================================================================
# ADR 0075 — typed outbound roster (CLIENT / VENDOR) enrichment
# ===========================================================================

# Typed outbound roster: the firm's own client (a consumer on gmail) and a
# records vendor. Exact addresses — a whole-@domain grant at a public provider is
# rejected by the validator; the classifier matches these exactly.
TYPED_ROSTER = [("jane@gmail.com", "client"), ("records@radiology.com", "records_vendor")]

BENIGN = "Your intake packet is attached; review it at your convenience."
MONEY = "The settlement offer is $45,000, net to client after liens."


def _setup_typed(monkeypatch, enforce, *, exposure, typed=TYPED_ROSTER, floors=None):
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", lambda slug="": dict(exposure))
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: list(ROSTER))
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: list(typed))
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: dict(floors or {}))
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "quinn")


# ---- _reclassify_send: the recipient axis resolves to the right class ------


def _reclass(monkeypatch, enforce, tool, args, *, session="s1", tainted=False):
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: list(ROSTER))
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: list(TYPED_ROSTER))
    return enforce._reclassify_send(tool, args, enforce.ActionClass.EXTERNAL_SEND, session, tainted)


def test_reclassify_client_recipient(monkeypatch):
    enforce = _load_enforce()
    got = _reclass(monkeypatch, enforce, "mcp_agentmail_send_message", {"to": ["jane@gmail.com"]})
    assert got is enforce.ActionClass.EXTERNAL_SEND_CLIENT


def test_reclassify_vendor_recipient(monkeypatch):
    enforce = _load_enforce()
    got = _reclass(
        monkeypatch, enforce, "mcp_agentmail_send_message", {"to": ["records@radiology.com"]}
    )
    assert got is enforce.ActionClass.EXTERNAL_SEND_VENDOR


def test_reclassify_internal_outranks_typed(monkeypatch):
    enforce = _load_enforce()
    got = _reclass(
        monkeypatch, enforce, "mcp_agentmail_send_message", {"to": ["scott@smd.services"]}
    )
    assert got is enforce.ActionClass.EXTERNAL_SEND_INTERNAL


def test_reclassify_unrostered_is_outside(monkeypatch):
    enforce = _load_enforce()
    got = _reclass(
        monkeypatch, enforce, "mcp_agentmail_send_message", {"to": ["opposing@counsel.com"]}
    )
    assert got is enforce.ActionClass.EXTERNAL_SEND


def test_reclassify_tainted_client_is_outside(monkeypatch):
    enforce = _load_enforce()
    got = _reclass(
        monkeypatch, enforce, "mcp_agentmail_send_message", {"to": ["jane@gmail.com"]}, tainted=True
    )
    assert got is enforce.ActionClass.EXTERNAL_SEND


def test_reclassify_send_draft_resolves_client_from_registry(monkeypatch):
    enforce = _load_enforce()
    record_draft_from_post_tool_call(
        "mcp_agentmail_create_draft", {"to": ["jane@gmail.com"]}, {"id": "d-client"}, "s1"
    )
    got = _reclass(monkeypatch, enforce, "mcp_agentmail_send_draft", {"draft_id": "d-client"})
    assert got is enforce.ActionClass.EXTERNAL_SEND_CLIENT


# ---- evaluate_tool_call: the four-way ceiling behavior --------------------


def test_client_autonomous_benign_body_sends(monkeypatch):
    enforce = _load_enforce()
    _setup_typed(
        monkeypatch,
        enforce,
        exposure={enforce.ActionClass.EXTERNAL_SEND_CLIENT: enforce.Ceiling.AUTONOMOUS},
    )
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["jane@gmail.com"], "text": BENIGN}, "smd", "s1"
    )
    assert result is None  # rostered client, autonomous, benign → sends


def test_client_draft_ceiling_drafts(monkeypatch):
    enforce = _load_enforce()
    _setup_typed(
        monkeypatch,
        enforce,
        exposure={enforce.ActionClass.EXTERNAL_SEND_CLIENT: enforce.Ceiling.DRAFT_FOR_REVIEW},
    )
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["jane@gmail.com"], "text": BENIGN}, "smd", "s1"
    )
    assert result is not None and "draft" in result["message"].lower()


def test_client_unauthored_is_fail_closed(monkeypatch):
    enforce = _load_enforce()
    # external_send authored, external_send_client NOT → the client send fails closed.
    _setup_typed(
        monkeypatch,
        enforce,
        exposure={enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS},
    )
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["jane@gmail.com"], "text": BENIGN}, "smd", "s1"
    )
    assert result is not None and "refused" in result["message"].lower()


def test_vendor_autonomous_sends_and_draft_ceiling_drafts(monkeypatch):
    enforce = _load_enforce()
    _setup_typed(
        monkeypatch,
        enforce,
        exposure={enforce.ActionClass.EXTERNAL_SEND_VENDOR: enforce.Ceiling.AUTONOMOUS},
    )
    ok = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["records@radiology.com"], "text": BENIGN}, "smd", "s1"
    )
    assert ok is None
    _setup_typed(
        monkeypatch,
        enforce,
        exposure={enforce.ActionClass.EXTERNAL_SEND_VENDOR: enforce.Ceiling.DRAFT_FOR_REVIEW},
    )
    drafted = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["records@radiology.com"], "text": BENIGN}, "smd", "s2"
    )
    assert drafted is not None and "draft" in drafted["message"].lower()


def test_client_autonomous_money_body_is_content_floored(monkeypatch):
    enforce = _load_enforce()
    _setup_typed(
        monkeypatch,
        enforce,
        exposure={enforce.ActionClass.EXTERNAL_SEND_CLIENT: enforce.Ceiling.AUTONOMOUS},
    )
    # A settlement dollar figure to a client must draft even under autonomous —
    # the content floor applies to the client class (unlike external_send_internal).
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["jane@gmail.com"], "text": MONEY}, "smd", "s1"
    )
    assert result is not None and "draft" in result["message"].lower()


def test_client_send_on_tainted_turn_is_refused(monkeypatch):
    enforce = _load_enforce()
    _setup_typed(
        monkeypatch,
        enforce,
        exposure={enforce.ActionClass.EXTERNAL_SEND_CLIENT: enforce.Ceiling.AUTONOMOUS},
    )
    enforce.SESSION_TAINT.mark("s-taint", "unknown_external")
    try:
        result = enforce.evaluate_tool_call(
            "mcp_agentmail_send_message",
            {"to": ["jane@gmail.com"], "text": BENIGN},
            "smd",
            "s-taint",
        )
    finally:
        enforce.SESSION_TAINT._tainted.clear()
    assert result is not None and "refused" in result["message"].lower()


def test_client_vendor_mix_routes_outside_no_ceiling_shopping(monkeypatch):
    enforce = _load_enforce()
    # client + vendor both autonomous, but a MIXED send aggregates to OUTSIDE and is
    # governed by external_send (here draft) — a mixed send cannot ceiling-shop.
    _setup_typed(
        monkeypatch,
        enforce,
        exposure={
            enforce.ActionClass.EXTERNAL_SEND_CLIENT: enforce.Ceiling.AUTONOMOUS,
            enforce.ActionClass.EXTERNAL_SEND_VENDOR: enforce.Ceiling.AUTONOMOUS,
            enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW,
        },
    )
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": ["jane@gmail.com", "records@radiology.com"], "text": BENIGN},
        "smd",
        "s1",
    )
    assert result is not None and "draft" in result["message"].lower()


def test_inert_when_no_typed_roster_and_no_new_keys(monkeypatch):
    enforce = _load_enforce()
    # Old-style config: only external_send authored, NO typed roster. A send to what
    # WOULD be a client address stays governed by external_send — byte-identical to
    # pre-ADR-0075 behavior (the classifier finds no typed match → OUTSIDE).
    _setup_typed(
        monkeypatch,
        enforce,
        exposure={enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW},
        typed=[],
    )
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message", {"to": ["jane@gmail.com"], "text": BENIGN}, "smd", "s1"
    )
    assert result is not None and "draft" in result["message"].lower()


# ---- typed-classifier unit tests (adversarial parity with the 3-class set) --


def test_typed_classifier_public_domain_exact_vs_grant():
    from shared.recipient_classifier import RecipientClass, classify_recipients_typed

    typed = [("jane@gmail.com", "client")]
    # EXACT gmail address is a valid client match.
    assert classify_recipients_typed(["jane@gmail.com"], [], typed) is RecipientClass.CLIENT
    # A DIFFERENT gmail address is NOT the client (exact match only, no domain widening).
    assert classify_recipients_typed(["bob@gmail.com"], [], typed) is RecipientClass.OUTSIDE


def test_typed_classifier_display_name_and_plus_tag_and_homoglyph():
    from shared.recipient_classifier import RecipientClass, classify_recipients_typed

    typed = [("jane@gmail.com", "client")]
    # Display-name form is UNKNOWN (not parsed), a hard error for the caller.
    assert classify_recipients_typed(["Jane <jane@gmail.com>"], [], typed) is RecipientClass.UNKNOWN
    # Plus-tag is not widened to the bare client address.
    assert classify_recipients_typed(["jane+x@gmail.com"], [], typed) is RecipientClass.OUTSIDE
    # Homoglyph domain never matches the ASCII roster (U+0430 Cyrillic 'а').
    assert classify_recipients_typed(["jane@gmаil.com"], [], typed) is not RecipientClass.CLIENT


def test_typed_classifier_empty_typed_roster_is_outside():
    from shared.recipient_classifier import RecipientClass, classify_recipients_typed

    assert classify_recipients_typed(["jane@gmail.com"], [], []) is RecipientClass.OUTSIDE


def test_typed_classifier_multiclass_address_is_outside_never_guesses():
    from shared.recipient_classifier import RecipientClass, classify_recipients_typed

    # One address typed as BOTH classes (validators forbid this, but the classifier
    # never guesses which wins) → OUTSIDE.
    typed = [("x@firm-vendor.com", "client"), ("x@firm-vendor.com", "records_vendor")]
    assert classify_recipients_typed(["x@firm-vendor.com"], [], typed) is RecipientClass.OUTSIDE


def test_typed_classifier_unknown_recipient_is_hard_error_class():
    from shared.recipient_classifier import (
        RecipientClass,
        UnclassifiedRecipientError,
        classify_recipients_typed,
        send_action_class,
    )

    typed = [("jane@gmail.com", "client")]
    assert classify_recipients_typed(["garbage"], [], typed) is RecipientClass.UNKNOWN
    with pytest.raises(UnclassifiedRecipientError):
        send_action_class(RecipientClass.UNKNOWN)
