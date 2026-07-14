"""Proactive outbound relay (ADR 0075 / #1868).

The inbound reply relay delivers the agent's governed draft back to a verified
inbound sender. This suite covers the PROACTIVE mirror: a scheduled/cron chase
draft addressed to the firm's own rostered CLIENT / RECORDS VENDOR
(``scope.outbound_roster``) is delivered — sending the exact draft the model
created — but ONLY when the same authorization the gate's ``send_message`` would
require holds (typed-roster class + that class's authored ceiling is
``autonomous`` + untainted turn + content/fabrication floors re-pass). Every other
case holds the draft (the day-one posture).
"""

from __future__ import annotations

import json

import pytest

from shared import inbound
from shared.recipient_classifier import (
    ACTION_CLASS_EXTERNAL_SEND_CLIENT,
    ACTION_CLASS_EXTERNAL_SEND_VENDOR,
)
from tests.conftest import load_plugin


class _FakeD1Client:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql: str, *params):
        self.calls.append((sql, params))
        return 1

    def events(self) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        for _sql, params in self.calls:
            out.append((params[2], json.loads(params[-1]) if params[-1] else {}))
        return out


_CLIENT = "client@ashtonprice-client.example"
_VENDOR = "records@valleyimaging.example"


def _yaml(*, client_ceiling: str = "autonomous", vendor_ceiling: str | None = None) -> str:
    exposure = f"        external_send_client: {client_ceiling}\n"
    if vendor_ceiling is not None:
        exposure += f"        external_send_vendor: {vendor_ceiling}\n"
    return (
        "customer_id: acme\n"
        "vertical: law-firm\n"
        "personas:\n"
        "  - slug: quinn\n"
        "    entitlements:\n"
        "      exposure:\n"
        f"{exposure}"
        "scope:\n"
        "  inbound_allow_from:\n"
        "    - attorney@ashtonprice.example\n"
        "  outbound_roster:\n"
        f"    - address: {_CLIENT}\n"
        "      class: client\n"
        f"    - address: {_VENDOR}\n"
        "      class: records_vendor\n"
    )


@pytest.fixture(autouse=True)
def _clear_state():
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    inbound.SESSION_TAINT._tainted.clear()
    yield
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    inbound.SESSION_TAINT._tainted.clear()


@pytest.fixture
def relay_mod(monkeypatch, tmp_path):
    """Plugin in the infra-ready state, active persona = quinn, send_draft stubbed."""
    mod = load_plugin("hermes-smd-reply")
    fake_d1 = _FakeD1Client()
    sent: list[dict] = []

    def _fake_send_draft(*, api_key, inbox_id, draft_id, **_kw):
        sent.append({"api_key": api_key, "inbox_id": inbox_id, "draft_id": draft_id})
        return "msg_proactive_1"

    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(_yaml())

    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", fake_d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", yaml_path, raising=False)
    monkeypatch.setattr(mod.relay, "send_draft", _fake_send_draft)
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "quinn")
    return mod, fake_d1, sent, yaml_path


# A floor-clean chase body: no money figures, no contract/legal trigger words —
# the content floor must pass it. (The real verification-request template carries
# words like "sign"/"verification" that trip the contract category and would hold
# the send; that template-tuning constraint is Risk 4 in the ADR, out of scope
# for the relay mechanism under test here.)
def _draft(
    to,
    text="Just following up on the item we sent over last week. Please let us know where things stand when you have a chance. Thanks.",
    subject="Following up",
):
    return {"to": to, "subject": subject, "text": text, "html": ""}


def _result(inbox_id="inbox_op", draft_id="dr_1") -> str:
    return json.dumps({"draftId": draft_id, "inboxId": inbox_id, "to": ["x"]})


def _fire(mod, args, *, session="cron1", result=None):
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=args,
        session_id=session,
        result=result if result is not None else _result(),
    )


# ---------------------------------------------------------------------------
# The graduated send
# ---------------------------------------------------------------------------


def test_sends_the_exact_draft_to_rostered_client_when_autonomous(relay_mod) -> None:
    mod, d1, sent, _ = relay_mod
    _fire(mod, _draft([_CLIENT]), result=_result(inbox_id="inbox_op", draft_id="dr_42"))
    assert len(sent) == 1
    # Delivered the EXACT draft the model created (send-draft, not re-composed).
    assert sent[0]["inbox_id"] == "inbox_op"
    assert sent[0]["draft_id"] == "dr_42"
    events = d1.events()
    a, meta = next((a, m) for a, m in events if a == "PROACTIVE_SENT")
    assert meta["action_class"] == ACTION_CLASS_EXTERNAL_SEND_CLIENT
    assert meta["recipients"] == [_CLIENT]
    assert meta["sent_message_id"] == "msg_proactive_1"
    assert "body_digest" in meta and "text" not in meta  # never the body


def test_sends_to_rostered_vendor_when_vendor_class_autonomous(relay_mod, monkeypatch) -> None:
    mod, d1, sent, yaml_path = relay_mod
    yaml_path.write_text(_yaml(client_ceiling="draft_for_review", vendor_ceiling="autonomous"))
    _fire(mod, _draft([_VENDOR]))
    assert len(sent) == 1
    a, meta = next((a, m) for a, m in d1.events() if a == "PROACTIVE_SENT")
    assert meta["action_class"] == ACTION_CLASS_EXTERNAL_SEND_VENDOR


# ---------------------------------------------------------------------------
# Holds — fail-closed
# ---------------------------------------------------------------------------


def test_holds_when_ceiling_is_draft_for_review(relay_mod) -> None:
    mod, d1, sent, yaml_path = relay_mod
    yaml_path.write_text(_yaml(client_ceiling="draft_for_review"))
    _fire(mod, _draft([_CLIENT]))
    assert sent == []
    a, meta = next((a, m) for a, m in d1.events() if a == "PROACTIVE_HELD")
    assert meta["reason"] == "ceiling_not_autonomous"
    assert meta["ceiling"] == "draft_for_review"


def test_holds_when_class_unauthored(relay_mod) -> None:
    mod, d1, sent, yaml_path = relay_mod
    # Persona authors NO external_send_client at all -> fail-closed hold.
    yaml_path.write_text(
        "customer_id: acme\nvertical: law-firm\n"
        "personas:\n  - slug: quinn\n    entitlements:\n      exposure:\n"
        "        external_send_internal: autonomous\n"
        "scope:\n  inbound_allow_from: []\n"
        f"  outbound_roster:\n    - address: {_CLIENT}\n      class: client\n"
    )
    _fire(mod, _draft([_CLIENT]))
    assert sent == []
    a, meta = next((a, m) for a, m in d1.events() if a == "PROACTIVE_HELD")
    assert meta["reason"] == "ceiling_not_autonomous"
    assert meta["ceiling"] == ""


def test_holds_on_tainted_turn_even_when_autonomous(relay_mod) -> None:
    mod, d1, sent, _ = relay_mod
    inbound.SESSION_TAINT.mark("cron1", inbound.TRUST_CLASS_UNKNOWN_EXTERNAL)
    _fire(mod, _draft([_CLIENT]), session="cron1")
    assert sent == []
    a, meta = next((a, m) for a, m in d1.events() if a == "PROACTIVE_HELD")
    assert meta["reason"] == "tainted_turn"
    assert meta["action_class"] == ACTION_CLASS_EXTERNAL_SEND_CLIENT


def test_holds_when_body_trips_content_floor(relay_mod) -> None:
    mod, d1, sent, _ = relay_mod
    _fire(mod, _draft([_CLIENT], text="The settlement offer is $50,000 net to you."))
    assert sent == []
    reasons = [m["reason"] for a, m in d1.events() if a == "PROACTIVE_HELD"]
    assert reasons and reasons[0] != "autonomous"


def test_holds_when_draft_ids_unresolved(relay_mod) -> None:
    mod, d1, sent, _ = relay_mod
    _fire(mod, _draft([_CLIENT]), result=json.dumps({"unexpected": "shape"}))
    assert sent == []
    a, meta = next((a, m) for a, m in d1.events() if a == "PROACTIVE_HELD")
    assert meta["reason"] == "unresolved_draft_ids"


# ---------------------------------------------------------------------------
# Not a proactive-relay event — silent (no send, no audit noise)
# ---------------------------------------------------------------------------


def test_unrostered_outside_recipient_is_silent(relay_mod) -> None:
    mod, d1, sent, _ = relay_mod
    _fire(mod, _draft(["opposing.counsel@other-firm.example"]))
    assert sent == []
    assert d1.events() == []  # not a typed-roster send -> no PROACTIVE_* row


def test_injected_extra_outside_recipient_is_held_silent(relay_mod) -> None:
    mod, d1, sent, _ = relay_mod
    # client + an injected outside cc -> aggregates to OUTSIDE -> not sent.
    _fire(mod, _draft([_CLIENT, "attacker@evil.example"]))
    assert sent == []
    # OUTSIDE aggregate carries no action_class -> silent (no audit noise).
    assert all(a not in ("PROACTIVE_SENT",) for a, _ in d1.events())
    assert sent == []


def test_internal_recipient_draft_is_silent(relay_mod) -> None:
    mod, d1, sent, _ = relay_mod
    _fire(mod, _draft(["attorney@ashtonprice.example"]))
    assert sent == []
    assert d1.events() == []


# ---------------------------------------------------------------------------
# The two relay paths do not cross
# ---------------------------------------------------------------------------


def test_inbound_origin_uses_reply_path_not_proactive(relay_mod, monkeypatch) -> None:
    mod, d1, sent, _ = relay_mod
    replied: list[dict] = []
    monkeypatch.setattr(
        mod.relay,
        "send_reply",
        lambda *, api_key, inbox_id, message_id, text, html, **_k: (
            replied.append({"inbox_id": inbox_id, "message_id": message_id}) or "r1"
        ),
    )
    # A verified inbound from the attorney (on the inbound roster) opens the turn.
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s_in",
        inbound.InboundOrigin(
            sender_address="attorney@ashtonprice.example", message_id="m_in", inbox_id="inbox_in"
        ),
    )
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["attorney@ashtonprice.example"]),
        session_id="s_in",
        result=_result(),
    )
    # The reply relay handled it; the proactive path never fired (no send_draft).
    assert len(replied) == 1
    assert sent == []
    assert any(a == "REPLY_SENT" for a, _ in d1.events())
    assert all(not a.startswith("PROACTIVE_") for a, _ in d1.events())


# ---------------------------------------------------------------------------
# Parity with the gate + parser robustness
# ---------------------------------------------------------------------------


def test_autonomous_string_matches_gate_ceiling_enum() -> None:
    relay = load_plugin("hermes-smd-reply").relay
    gate = load_plugin("hermes-smd-trust").enforce
    assert relay.CEILING_AUTONOMOUS == gate.Ceiling.AUTONOMOUS.value


def test_disposition_action_class_matches_gate_reclassify() -> None:
    """The relay's class->action mapping is the gate's, via the shared classifier."""
    relay = load_plugin("hermes-smd-reply").relay
    typed = [(_CLIENT, "client"), (_VENDOR, "records_vendor")]
    d_client = relay.proactive_disposition(
        recipients={_CLIENT},
        internal_roster=[],
        typed_roster=typed,
        persona_exposure={ACTION_CLASS_EXTERNAL_SEND_CLIENT: "autonomous"},
        tainted=False,
    )
    assert d_client.send and d_client.action_class == ACTION_CLASS_EXTERNAL_SEND_CLIENT
    d_vendor = relay.proactive_disposition(
        recipients={_VENDOR},
        internal_roster=[],
        typed_roster=typed,
        persona_exposure={ACTION_CLASS_EXTERNAL_SEND_VENDOR: "autonomous"},
        tainted=False,
    )
    assert d_vendor.send and d_vendor.action_class == ACTION_CLASS_EXTERNAL_SEND_VENDOR


@pytest.mark.parametrize(
    "result,expect",
    [
        (json.dumps({"draftId": "d1", "inboxId": "i1"}), ("i1", "d1")),
        (json.dumps({"draft_id": "d2", "inbox_id": "i2"}), ("i2", "d2")),
        (json.dumps({"id": "d3", "inboxId": "i3"}), ("i3", "d3")),
        (json.dumps({"draft": {"draftId": "d4", "inboxId": "i4"}}), ("i4", "d4")),
        ({"draftId": "d5", "inboxId": "i5"}, ("i5", "d5")),  # already-parsed dict
        (json.dumps({"draftId": "d6"}), ("", "")),  # inbox missing -> fail closed
        ("not json", ("", "")),
        (None, ("", "")),
        (json.dumps([1, 2, 3]), ("", "")),
    ],
)
def test_parse_created_draft_tolerant(result, expect) -> None:
    relay = load_plugin("hermes-smd-reply").relay
    assert relay.parse_created_draft(result) == expect
