"""Tests for plugins/hermes-smd-corrections (ss-console #2091, ADR 0083 §4).

Four properties are load-bearing enough that a regression would be silent and
would reach a running seat:

 1. THE TOOL IS MAPPED. An unmapped tool is REFUSED by design, which is exactly
    how the ``execute_code`` gap this plugin closes stayed invisible until a live
    probe (ss #1915). ``correction_capture`` must classify INTERNAL_WRITE — the
    class every seat already authors at ``draft_for_review`` or better.
 2. THE AGENT NEVER SETS STATUS. The broker stamps ``proposed`` as a constant;
    nothing the caller sends may carry a status, so the marshalled payload must
    not contain one even if the model puts one in its args.
 3. TAINT REFUSES, AND FAILS CLOSED. A correction stated on a turn that read
    outside content is not the customer's. An unresolvable taint state refuses
    too — the cost of declining is a person restating a preference; the cost of
    accepting is a stranger's words in a reviewer's queue under the customer's
    name.
 4. THE NUDGE MATCHES THE REFUSAL. ``record_peer_preference`` shipped registered
    and unprompted and the lane had zero rows fleet-wide (overlay #170). The
    nudge exists for that reason, and it must never advertise capture on a turn
    ``pre_tool_call`` would refuse.

The broker is faked. Its validation is tested where it lives (console side,
``operator/workspace_broker/corrections.py``); duplicating it here would assert a
second, drifting copy of rules this plugin deliberately does not own.
"""

from __future__ import annotations

import json

import pytest

from shared.action_classes import ActionClass, classify_tool
from shared.inbound import TRUST_CLASS_INTERNAL
from tests.conftest import load_plugin


@pytest.fixture
def corrections(monkeypatch):
    plugin = load_plugin("hermes-smd-corrections")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "id": "cor-1", "status": "proposed"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    return plugin, requests


def _untainted(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin.SESSION_TAINT, "trust_class", lambda _sid: TRUST_CLASS_INTERNAL, raising=False
    )


def _tainted(plugin, monkeypatch, value="unknown_external"):
    monkeypatch.setattr(plugin.SESSION_TAINT, "trust_class", lambda _sid: value, raising=False)


# ---------------------------------------------------------------------------
# 1. The tool is mapped
# ---------------------------------------------------------------------------


def test_capture_tool_is_mapped_internal_write(corrections):
    """Unmapped => REFUSED by design. INTERNAL_WRITE is the class every seat
    already authors, so capture needs no entitlement widening."""
    plugin, _ = corrections
    assert classify_tool(plugin.TOOL_NAME).action_class is ActionClass.INTERNAL_WRITE


def test_registers_tool_and_both_hooks(corrections):
    plugin, _ = corrections
    registered_tools: list[dict] = []
    registered_hooks: list[str] = []

    class Ctx:
        def register_tool(self, **kwargs):
            registered_tools.append(kwargs)

        def register_hook(self, name, _cb):
            registered_hooks.append(name)

    plugin.register(Ctx())
    assert [t["name"] for t in registered_tools] == [plugin.TOOL_NAME]
    # The wrapped function shape — a bare JSON-schema advertises empty
    # parameters and the model cannot pass a single argument.
    assert "parameters" in registered_tools[0]["schema"]
    assert sorted(registered_hooks) == ["pre_llm_call", "pre_tool_call"]


# ---------------------------------------------------------------------------
# 2. The agent never sets status
# ---------------------------------------------------------------------------


def test_marshalled_payload_carries_no_status(corrections):
    """`status` is a broker-side constant. A validated-but-caller-supplied status
    is one typo away from a caller-supplied `approved`; a constant cannot be."""
    plugin, requests = corrections
    plugin._capture(
        {
            "output_class": "staff",
            "spec_property": "format",
            "statement": "Could this be a table instead of text?",
            "status": "approved",  # the model tries; nothing reads it
        }
    )
    assert requests[0]["action"] == "correction_propose"
    assert "status" not in requests[0]["proposal"]
    assert set(requests[0]["proposal"]) == {
        "output_class",
        "spec_property",
        "statement",
        "stated_by",
        "source_ref",
    }


def test_broker_verdict_is_returned_verbatim(corrections, monkeypatch):
    """A refusal must stay visible to the turn rather than be swallowed into a
    cheerful acknowledgement."""
    plugin, _ = corrections
    monkeypatch.setattr(
        plugin,
        "_broker_request",
        lambda _p: {"error": "CorrectionValidationError", "message": "statement must not be empty"},
    )
    out = json.loads(plugin._capture({"output_class": "staff", "spec_property": "voice"}))
    assert out["error"] == "CorrectionValidationError"


# ---------------------------------------------------------------------------
# 3. Taint refuses, and fails closed
# ---------------------------------------------------------------------------


def test_untainted_turn_is_allowed(corrections, monkeypatch):
    plugin, _ = corrections
    _untainted(plugin, monkeypatch)
    assert plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s1") is None


def test_tainted_turn_is_refused(corrections, monkeypatch):
    plugin, _ = corrections
    _tainted(plugin, monkeypatch)
    block = plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s1")
    assert block is not None and block["action"] == "block"
    assert "outside the firm" in block["message"]


def test_unresolvable_taint_refuses(corrections, monkeypatch):
    """Fail-closed: a capture we cannot certify came from a trusted turn is one
    we decline."""
    plugin, _ = corrections

    def boom(_sid):
        raise RuntimeError("register unreadable")

    monkeypatch.setattr(plugin.SESSION_TAINT, "trust_class", boom, raising=False)
    block = plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s1")
    assert block is not None and block["action"] == "block"


def test_other_tools_are_untouched(corrections, monkeypatch):
    """The hook fires for every tool call on the seat; it must be inert for all
    but its own."""
    plugin, _ = corrections
    _tainted(plugin, monkeypatch)
    assert plugin.on_pre_tool_call(tool_name="write_file", session_id="s1") is None


# ---------------------------------------------------------------------------
# 4. The nudge matches the refusal
# ---------------------------------------------------------------------------


def test_nudge_on_a_sender_attributed_untainted_turn(corrections, monkeypatch):
    plugin, _ = corrections
    _untainted(plugin, monkeypatch)
    out = plugin.on_pre_llm_call(session_id="s1", sender_id="someone@example.com")
    assert out is not None and plugin.TOOL_NAME in out["context"]


def test_no_nudge_without_a_human(corrections, monkeypatch):
    """A cron turn has nobody to state a correction; the line would be pure
    context cost on every scheduled run."""
    plugin, _ = corrections
    _untainted(plugin, monkeypatch)
    assert plugin.on_pre_llm_call(session_id="s1", sender_id="") is None


def test_no_nudge_where_capture_would_be_refused(corrections, monkeypatch):
    """The nudge must never advertise something pre_tool_call would refuse."""
    plugin, _ = corrections
    _tainted(plugin, monkeypatch)
    assert plugin.on_pre_llm_call(session_id="s1", sender_id="someone@example.com") is None
