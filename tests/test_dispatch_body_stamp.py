"""The CONFIRM-row body stamp (WS-RENDER cross-workstream contract).

``_dispatch_internal_message`` stamps ``rendered_body_sha256`` — the canonical
hash of the text the gate ALLOWED, computed BEFORE ``_attach_html_body``
mutates the payload (html attach + plain down-render) — plus the caller's
``routing_leg`` / ``body_variant``. The console's send verifier joins that
stamp against the pre_run's EMITTED_WAKE stamp; a post-mutation hash would
compare the down-rendered plain text against the authored body and grade
every conformant send BODY_DIVERGED.

It ALSO stamps ``plain_body_sha256`` — the canonical hash of the text/plain the
channel actually stores, taken AFTER the attach — because the read-back a
console reconciler performs returns the down-render, not the authored markdown.
One hash cannot answer both questions, which is why there are two.

Also pins the plain down-render wiring itself and the mirror artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from shared import prerendered_dispatch, report_render
from tests.conftest import load_plugin

REPORT_BODY = (
    "## Needs you today (1)\n"
    "\n"
    "1. matter 2026-PI-101, **task-deadline** 2026-08-29 (overdue by 2 days)\n"
)


def _trust():
    return load_plugin("hermes-smd-trust")


def _arm(monkeypatch, trust, captured):
    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", lambda *a, **k: None)
    # The outbound scans are wired on this path (the review fix below tests
    # that); stubbed to allow here so the stamp tests exercise the stamp.
    monkeypatch.setattr(trust.outbound, "check_outbound_draft", lambda **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_send", lambda **k: None)
    monkeypatch.setattr(trust, "get_secret", lambda k: "pilot-smokeball")
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: "agentmail")

    def fake_send(*, payload, session_id="", matter_ref=None, audit_extra=None, **_):
        captured.append({"payload": dict(payload), "audit_extra": dict(audit_extra or {})})
        return "msg-1"

    monkeypatch.setattr(trust.outbound_send, "send_message", fake_send)


def test_outbound_scans_are_wired_on_the_out_of_turn_path(monkeypatch):
    """'Through the full gate' as a control, not a sentence (review fix): the
    fabrication/identifier scans (outbound.check_*) fire on this path and a
    block from either refuses the dispatch before any transport call."""
    trust = _trust()
    captured: list[dict] = []
    _arm(monkeypatch, trust, captured)
    monkeypatch.setattr(
        trust.outbound,
        "check_outbound_send",
        lambda **k: {"action": "block", "message": "Refused: unverified identifier"},
    )
    result = trust._dispatch_internal_message(
        to=["ops@firm.example"], subject="s", text=REPORT_BODY, session_id="s1"
    )
    assert not result.sent
    assert "unverified identifier" in result.reason
    assert captured == []  # nothing reached the transport


def test_a_raising_outbound_scan_fails_toward_not_sending(monkeypatch):
    trust = _trust()
    captured: list[dict] = []
    _arm(monkeypatch, trust, captured)

    def boom(**_k):
        raise RuntimeError("scanner down")

    monkeypatch.setattr(trust.outbound, "check_outbound_send", boom)
    result = trust._dispatch_internal_message(
        to=["ops@firm.example"], subject="s", text="plain body", session_id="s1"
    )
    assert not result.sent
    assert captured == []


def test_rendered_body_sha256_is_the_pre_mutation_canonical_hash(monkeypatch):
    trust = _trust()
    captured: list[dict] = []
    _arm(monkeypatch, trust, captured)
    result = trust._dispatch_internal_message(
        to=["ops@firm.example"],
        subject="[Deadlines] 1 need you, 2026-08-31",
        text=REPORT_BODY,
        session_id="s1",
        audit_extra={"routing_leg": "central", "body_variant": "full"},
    )
    assert result.sent
    [call] = captured
    expected = prerendered_dispatch.canonical_body_sha256(REPORT_BODY)
    assert call["audit_extra"]["rendered_body_sha256"] == expected
    assert call["audit_extra"]["routing_leg"] == "central"
    assert call["audit_extra"]["body_variant"] == "full"
    # The payload WAS mutated after the stamp: html attached, text
    # down-rendered to plain — and the stamp still names the authored bytes.
    assert call["payload"]["html"].startswith("<div")
    assert "## " not in call["payload"]["text"]
    assert "**" not in call["payload"]["text"]
    assert prerendered_dispatch.canonical_body_sha256(call["payload"]["text"]) != expected


def test_stamp_present_without_caller_extra_too(monkeypatch):
    trust = _trust()
    captured: list[dict] = []
    _arm(monkeypatch, trust, captured)
    trust._dispatch_internal_message(
        to=["ops@firm.example"], subject="s", text="plain prose body", session_id="s1"
    )
    [call] = captured
    assert call["audit_extra"][
        "rendered_body_sha256"
    ] == prerendered_dispatch.canonical_body_sha256("plain prose body")
    # Prose gains no html and stays byte-identical.
    assert "html" not in call["payload"]
    assert call["payload"]["text"] == "plain prose body"


# ---------------------------------------------------------------------------
# plain_body_sha256 — the SECOND stamp
#
# rendered_body_sha256 answers "is this the body pre_run authored". It cannot
# answer "is this the body the channel delivered", because the channel does not
# store that body: _attach_html_body replaces text with render_plain(text)
# before the payload leaves this process, and AgentMail returns THAT on a
# read-back. Grading the stored body against the rendered hash therefore HOLDs
# every conformant templated send. These tests pin the second hash, and — the
# load-bearing half — pin that it is ABSENT rather than duplicated when no
# down-render happened.
# ---------------------------------------------------------------------------


def test_plain_stamp_differs_from_rendered_when_markers_subtract(monkeypatch):
    """The whole reason the second stamp exists. REPORT_BODY carries `##` and
    `**`; render_plain subtracts them, so the two hashes MUST diverge. If they
    ever match here the down-render silently stopped happening and the console
    would be grading a body nobody transmitted."""
    trust = _trust()
    captured: list[dict] = []
    _arm(monkeypatch, trust, captured)
    result = trust._dispatch_internal_message(
        to=["ops@firm.example"],
        subject="[Deadlines] 1 need you, 2026-08-31",
        text=REPORT_BODY,
        session_id="s1",
        audit_extra={"routing_leg": "central", "body_variant": "full"},
    )
    assert result.sent
    [call] = captured
    rendered = call["audit_extra"]["rendered_body_sha256"]
    plain = call["audit_extra"]["plain_body_sha256"]
    assert rendered == prerendered_dispatch.canonical_body_sha256(REPORT_BODY)
    assert plain != rendered, "markers subtracted but the two stamps agree"
    # And the plain stamp names the bytes actually on the payload, not a
    # recomputation from the source that happens to look right.
    assert plain == prerendered_dispatch.canonical_body_sha256(call["payload"]["text"])


def test_plain_stamp_is_the_canonical_hash_of_the_render_plain_output(monkeypatch):
    """Hand-computed against the renderer and the canon function independently,
    so a bug in the stamp site cannot hide behind the stamp site's own math."""
    trust = _trust()
    captured: list[dict] = []
    _arm(monkeypatch, trust, captured)
    trust._dispatch_internal_message(
        to=["ops@firm.example"], subject="s", text=REPORT_BODY, session_id="s1"
    )
    [call] = captured
    expected = prerendered_dispatch.canonical_body_sha256(report_render.render_plain(REPORT_BODY))
    assert call["audit_extra"]["plain_body_sha256"] == expected


def test_both_stamps_present_on_each_body_variant(monkeypatch):
    """Full and skeleton reach the channel through this one function, so the
    pair rides both. The variants differ only in the caller's body_variant tag;
    nothing in the stamp path branches on it, and this pins that."""
    trust = _trust()
    for variant in ("full", "skeleton"):
        captured: list[dict] = []
        _arm(monkeypatch, trust, captured)
        trust._dispatch_internal_message(
            to=["ops@firm.example"],
            subject="s",
            text=REPORT_BODY,
            session_id="s1",
            audit_extra={"routing_leg": "central", "body_variant": variant},
        )
        [call] = captured
        extra = call["audit_extra"]
        assert extra["body_variant"] == variant
        assert extra["rendered_body_sha256"]
        assert extra["plain_body_sha256"]
        assert extra["plain_body_sha256"] != extra["rendered_body_sha256"]


def test_plain_stamp_is_omitted_when_no_plain_part_is_attached(monkeypatch):
    """Never stamp a lie. A prose reply gets no html half and no down-render —
    text reaches the channel as the gate allowed it — so a plain stamp would be
    a duplicate of the rendered one wearing a different name, i.e. a second
    observation that never happened."""
    trust = _trust()
    captured: list[dict] = []
    _arm(monkeypatch, trust, captured)
    trust._dispatch_internal_message(
        to=["client@example.com"],
        subject="Re: Quick favor",
        text="Hi Scott,\n\nMoved to Wednesday.\n\nThanks,\nOperator\n",
        session_id="s1",
    )
    [call] = captured
    assert "html" not in call["payload"]
    assert "rendered_body_sha256" in call["audit_extra"]
    assert "plain_body_sha256" not in call["audit_extra"]


def test_plain_stamp_is_omitted_when_the_composer_supplied_its_own_html(monkeypatch):
    """The other no-down-render path. _attach_html_body returns early to leave
    a model-authored html body alone, and leaves ``text`` alone with it.

    The html arrives the one way it can on this path: the gate rewrites the
    payload it allows (``evaluate_tool_call`` consumes approvals and stores its
    own copy), which the dispatch site already documents and reads back.
    """
    trust = _trust()
    captured: list[dict] = []
    _arm(monkeypatch, trust, captured)

    def _gate_supplies_html(_tool, args, *_a, **_k):
        args["html"] = "<p>mine</p>"
        return None

    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", _gate_supplies_html)
    trust._dispatch_internal_message(
        to=["ops@firm.example"], subject="s", text=REPORT_BODY, session_id="s1"
    )
    [call] = captured
    assert call["payload"]["html"] == "<p>mine</p>"
    assert call["payload"]["text"] == REPORT_BODY  # untouched, so no plain part
    assert "plain_body_sha256" not in call["audit_extra"]


def test_attach_reports_whether_it_attached(monkeypatch):
    """The boolean the stamp site trusts. Pinned directly so the stamp tests
    above cannot both pass by agreeing on the same wrong answer."""
    trust = _trust()
    report = {"to": ["x@y.z"], "text": REPORT_BODY}
    assert trust._attach_html_body("mcp_agentmail_send_message", report) is True
    assert report["text"] == report_render.render_plain(REPORT_BODY)
    for tool, args in (
        ("workspace_read_file", {"text": REPORT_BODY}),
        ("mcp_agentmail_send_message", {"text": "prose reply, no blocks"}),
        ("mcp_agentmail_send_message", {"text": "   "}),
        ("mcp_agentmail_send_message", {}),
        ("mcp_agentmail_send_message", {"text": REPORT_BODY, "html": "<p>mine</p>"}),
    ):
        assert trust._attach_html_body(tool, args) is False


# ---------------------------------------------------------------------------
# Mirrored artifacts (the same-change discipline)
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[1]


def test_send_render_mirror_declares_the_flipped_modes():
    text = (_REPO / "tests" / "contract" / "send-render.yaml").read_text(encoding="utf-8")
    # Parsed lightly (no yaml dep guaranteed in the bare suite): the two flips
    # this pair PR carries must be present in the mirror.
    assert "deadline-miss-escalator:" in text
    assert "render: templated" in text
    assert "client-verification-tracker:" in text
    assert "render: slot-templated" in text
    # The stamp schema is the cross-repo contract; the second hash must be
    # declared there or the console side has no authority to expect it.
    assert "plain_body_sha256:" in text
    assert "rendered_body_sha256:" in text


def test_canon_vectors_mirror_is_wellformed_and_arbitrated():
    fixture = _REPO / "tests" / "fixtures" / "body-canon-vectors.json"
    vectors = json.loads(fixture.read_text(encoding="utf-8"))["vectors"]
    assert len(vectors) >= 8
    for vector in vectors:
        assert prerendered_dispatch.canonical_body_sha256(vector["input"]) == vector["sha256"]
