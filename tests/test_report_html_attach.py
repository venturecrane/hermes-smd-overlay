"""Tests for the trust plugin's post-gate html attach (report email formatting).

Covers the trigger predicate (reports get html, prose replies do not, a
model-authored html body wins) and the ORDERING guarantee that the safety
argument depends on: the attach runs only after every gate has allowed, on both
send paths (the tool path and the out-of-band approved-send path).
"""

from __future__ import annotations

import pytest

from .conftest import load_plugin

REPORT_TEXT = """## Needs you today (1)

1. matter ALPHA-1, records outstanding, due 2026-07-11 (overdue 4 days) [ACK-6WS08D]
   Records were requested 2026-06-20 and have not yet arrived.
"""

PROSE_TEXT = (
    "Hi Scott,\n\nHappy to help. The check-in has moved to Wednesday at the "
    "same time.\n\nThanks,\nOperator\n"
)

SEND_TOOL = "mcp_agentmail_send_message"


@pytest.fixture
def mod():
    return load_plugin("hermes-smd-trust")


def test_report_send_gains_an_html_half(mod) -> None:
    args = {"to": ["scott@smd.services"], "subject": "[Deadlines] 1 need you", "text": REPORT_TEXT}
    mod._attach_html_body(SEND_TOOL, args)
    assert "<h2 " in args["html"]
    assert "<ol " in args["html"]
    # The markdown stays put as the plaintext half of the multipart send.
    assert args["text"] == REPORT_TEXT


def test_prose_reply_is_left_byte_identical(mod) -> None:
    """The whole point of the block-structure trigger: this change must not
    reshape a client-facing reply as a side effect."""
    args = {"to": ["client@example.com"], "subject": "Re: Quick favor", "text": PROSE_TEXT}
    before = dict(args)
    mod._attach_html_body(SEND_TOOL, args)
    assert args == before
    assert "html" not in args


def test_model_authored_html_is_never_clobbered(mod) -> None:
    args = {"to": ["scott@smd.services"], "text": REPORT_TEXT, "html": "<p>mine</p>"}
    mod._attach_html_body(SEND_TOOL, args)
    assert args["html"] == "<p>mine</p>"


def test_non_send_tools_and_empty_bodies_are_untouched(mod) -> None:
    for tool, args in (
        ("workspace_read_file", {"text": REPORT_TEXT}),
        (SEND_TOOL, {"to": ["x@y.z"], "text": "   "}),
        (SEND_TOOL, {"to": ["x@y.z"]}),
        (SEND_TOOL, {"to": ["x@y.z"], "text": None}),
    ):
        mod._attach_html_body(tool, args)
        assert "html" not in args


def test_attach_runs_only_after_the_gates_allow(mod, monkeypatch) -> None:
    """ORDERING GUARANTEE. A blocked send must never be rendered.

    Ordering is the safety argument: html is injected without its own scan
    BECAUSE the gates already scanned the text it is derived from. A render that
    happened on a blocked call would mean the attach sits outside the gate.
    """
    calls: list[str] = []

    def _blocked(*_a, **_k):
        calls.append("gate")
        return {"action": "block", "message": "ceiling refuses this"}

    monkeypatch.setattr(mod.enforce, "evaluate_tool_call", _blocked)
    monkeypatch.setattr(mod, "_attach_html_body", lambda *a, **k: calls.append("attach"))

    args = {"to": ["scott@smd.services"], "text": REPORT_TEXT}
    result = mod.on_pre_tool_call(
        tool_name=SEND_TOOL, args=args, customer_slug="pilot-smokeball", session_id="s1"
    )
    assert result is not None and result["action"] == "block"
    assert calls == ["gate"], "attach ran on a call the gate refused"


def test_tool_path_attaches_when_the_gates_allow(mod, monkeypatch) -> None:
    monkeypatch.setattr(mod.enforce, "evaluate_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(mod.outbound, "check_outbound_draft", lambda **k: None)
    monkeypatch.setattr(mod.outbound, "check_outbound_send", lambda **k: None)

    args = {"to": ["scott@smd.services"], "text": REPORT_TEXT}
    result = mod.on_pre_tool_call(
        tool_name=SEND_TOOL, args=args, customer_slug="pilot-smokeball", session_id="s1"
    )
    assert result is None
    assert "<h2 " in args["html"], "an allowed report send reached the tool without an html half"


def test_out_of_band_approved_send_also_attaches(mod, monkeypatch) -> None:
    """The confirm path stores the payload BEFORE the tool path's attach runs, so
    a withheld-then-approved report would ship markdown-only without its own call."""
    sent: dict = {}

    class _Rec:
        tool_name = SEND_TOOL
        approved = True
        recipients = {"scott@smd.services"}
        approval_source = "telegram"
        args = {"to": ["scott@smd.services"], "text": REPORT_TEXT}

    monkeypatch.setattr(mod.PENDING_SEND, "peek", lambda: _Rec())
    monkeypatch.setattr(mod.enforce, "evaluate_tool_call", lambda *a, **k: None)

    # ss#2258: no key and no inbox to stub — the broker owns both.
    def _capture(*, payload, **_kw):
        sent.update(payload)
        return "msg-1"

    monkeypatch.setattr(mod.outbound_send, "send_message", _capture)
    # No audit emitter left to stub either: the broker writes the row (ss#2258).

    mod._dispatch_approved_send("s1", "pilot-smokeball")
    assert "<h2 " in sent.get("html", ""), "approved report send dispatched without an html half"
    assert sent["text"] == REPORT_TEXT
