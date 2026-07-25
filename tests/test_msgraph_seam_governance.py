"""Governance coverage for the msgraph email tools (ADR 0078 slice 4, piece 2).

Two dangerous silent regressions this pins:

  * Recipient classification — the msgraph send takes FLAT ``to`` args (D4), so an
    INTERNAL (rostered) recipient must classify INTERNAL, not degrade to
    OUTSIDE/draft (which reads as "the operator stopped sending").
  * Content-gate body coverage — the msgraph send/reply body rides ``body_text``.
    Before this slice ``_SEND_SCAN_KEYS`` omitted it, so the fabrication/citation
    gate found no body, scanned "", and silently ALLOWED. The gate must SEE the
    body_text (a policy-tripping body is blocked) AND fail CLOSED when a
    body-required send tool carries no locatable body.
"""

from __future__ import annotations

from shared.outbound_recipient import CLASSIFIED_SEND_TOOLS, DRAFT_RECORD_TOOLS, send_recipients
from tests.conftest import load_plugin

ROSTER = ["@ashtonandprice.com", "scott@smd.services"]

_MSGRAPH_SEND = "mcp_msgraph_mail_send_message"
_MSGRAPH_DRAFT = "mcp_msgraph_mail_create_draft"


def _load_enforce():
    return load_plugin("hermes-smd-trust").enforce


def _load_outbound():
    return load_plugin("hermes-smd-trust").outbound


# ---------------------------------------------------------------------------
# Recipient extraction + INTERNAL classification (flat args)
# ---------------------------------------------------------------------------


def test_msgraph_send_is_a_classified_direct_to_send_tool():
    assert _MSGRAPH_SEND in CLASSIFIED_SEND_TOOLS
    assert _MSGRAPH_DRAFT in DRAFT_RECORD_TOOLS


def test_send_recipients_extracts_flat_to_for_msgraph():
    # The flat ``to`` must resolve (not degrade to an empty set → OUTSIDE).
    assert send_recipients(_MSGRAPH_SEND, {"to": ["Scott@smd.services"]}, "") == {
        "scott@smd.services"
    }
    assert send_recipients(_MSGRAPH_SEND, {"to": "greg@ashtonandprice.com"}, "") == {
        "greg@ashtonandprice.com"
    }


def _setup(monkeypatch, enforce, *, exposure):
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", lambda slug="": dict(exposure))
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: list(ROSTER))
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: [])
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "quinn")


def _exposure(enforce):
    return {
        enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW,
        enforce.ActionClass.EXTERNAL_SEND_INTERNAL: enforce.Ceiling.AUTONOMOUS,
    }


def test_msgraph_send_to_roster_classifies_internal_and_is_allowed(monkeypatch):
    enforce = _load_enforce()
    _setup(monkeypatch, enforce, exposure=_exposure(enforce))
    # Rostered recipient via the msgraph send → external_send_internal autonomous →
    # ALLOWED (None). If reclassification failed, it would land on external_send
    # (draft_for_review) and block — the regression this guards.
    result = enforce.evaluate_tool_call(
        _MSGRAPH_SEND,
        {"to": ["scott@smd.services"], "body_text": "quick internal note"},
        "smd",
        session_id="s1",
    )
    assert result is None


def test_msgraph_send_to_outside_drafts(monkeypatch):
    enforce = _load_enforce()
    _setup(monkeypatch, enforce, exposure=_exposure(enforce))
    result = enforce.evaluate_tool_call(
        _MSGRAPH_SEND,
        {"to": ["stranger@example.com"], "body_text": "hello"},
        "smd",
        session_id="s1",
    )
    assert result is not None and result["action"] == "block"
    assert "draft" in result["message"].lower()


# ---------------------------------------------------------------------------
# Content-gate body_text coverage (the fabrication/citation gate)
# ---------------------------------------------------------------------------


def test_fabrication_in_body_text_is_caught_for_msgraph_send():
    outbound = _load_outbound()
    # A Pattern-A fabrication marker hidden in body_text (not in text/html) must be
    # caught now that _SEND_SCAN_KEYS includes body_text.
    block = outbound.check_outbound_send(
        tool_name=_MSGRAPH_SEND,
        args={"to": ["c@x.example"], "body_text": "We'll reach out to schedule kickoff."},
        session_id="s1",
    )
    assert block is not None and block["action"] == "block"


def test_clean_body_text_allows_msgraph_send():
    outbound = _load_outbound()
    block = outbound.check_outbound_send(
        tool_name=_MSGRAPH_SEND,
        args={"to": ["c@x.example"], "body_text": "Thanks, I will take a look and follow up."},
        session_id="s1",
    )
    assert block is None


def test_body_required_msgraph_send_with_no_body_fails_closed():
    outbound = _load_outbound()
    # A send-class tool that always authors a body, carrying NO locatable body key,
    # must BLOCK — not silently allow (the body-omission bypass class).
    block = outbound.check_outbound_send(
        tool_name=_MSGRAPH_SEND,
        args={"to": ["c@x.example"]},
        session_id="s1",
    )
    assert block is not None and block["action"] == "block"


def test_content_floor_scans_body_text_for_msgraph_send():
    # enforce._extract_send_body must locate body_text so the ADR-0031 content
    # floor (money/contract/legal → draft) can inspect an msgraph send body.
    enforce = _load_enforce()
    body = enforce._extract_send_body(
        {"to": ["c@x.example"], "body_text": "the settlement is $5,000"}
    )
    assert body is not None and "5,000" in body
