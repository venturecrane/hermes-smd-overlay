"""The CONFIRM-row body stamp (WS-RENDER cross-workstream contract).

``_dispatch_internal_message`` stamps ``rendered_body_sha256`` — the canonical
hash of the text the gate ALLOWED, computed BEFORE ``_attach_html_body``
mutates the payload (html attach + plain down-render) — plus the caller's
``routing_leg`` / ``body_variant``. The console's send verifier joins that
stamp against the pre_run's EMITTED_WAKE stamp; a post-mutation hash would
compare the down-rendered plain text against the authored body and grade
every conformant send BODY_DIVERGED.

Also pins the plain down-render wiring itself and the mirror artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from shared import prerendered_dispatch
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
    monkeypatch.setattr(trust, "get_secret", lambda k: "pilot-smokeball")
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: "agentmail")

    def fake_send(*, payload, session_id="", matter_ref=None, audit_extra=None, **_):
        captured.append({"payload": dict(payload), "audit_extra": dict(audit_extra or {})})
        return "msg-1"

    monkeypatch.setattr(trust.outbound_send, "send_message", fake_send)


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


def test_canon_vectors_mirror_is_wellformed_and_arbitrated():
    fixture = _REPO / "tests" / "fixtures" / "body-canon-vectors.json"
    vectors = json.loads(fixture.read_text(encoding="utf-8"))["vectors"]
    assert len(vectors) >= 8
    for vector in vectors:
        assert prerendered_dispatch.canonical_body_sha256(vector["input"]) == vector["sha256"]
