"""Graph transport tests for the reply relay + confirm dispatch (ADR 0078, piece 3).

The seam is provider-neutral above the transport: roster / recipient-lock / floors
/ rate-limit are shared, and only the wire call differs. These pin the msgraph
branch of both out-of-band send paths:

  * the reply relay dispatches a rostered colleague's reply via Graph
    /messages/{id}/reply (keyed on the recorded inbound message id), and REFUSES
    fail-closed (audited REPLY_FAILED) when MSGRAPH_* is unset — never falling
    back to AgentMail;
  * the confirm dispatch sends an approved payload via Graph /sendMail with the
    flat to/cc/subject/body_text shape, and raises MsGraphSendError (→
    CONFIRM_SEND_FAILED) when the creds are absent.
"""

from __future__ import annotations

import json

import pytest

from shared import inbound
from shared.inbound import SESSION_TAINT
from shared.outbound_recipient import DRAFT_RECIPIENTS
from shared.pending_send import PENDING_SEND
from tests.conftest import load_plugin

_MSGRAPH_ROSTERED_YAML = (
    "customer_id: acme\n"
    "vertical: law-firm\n"
    "connectors:\n"
    "  Email:\n"
    "    adapter: msgraph\n"
    "    backend: mcp:msgraph-mail\n"
    "    enabled: true\n"
    "scope:\n"
    "  inbound_allow_from:\n"
    "    - greg@whitfield.example\n"
)


class _FakeD1:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql, *params):
        self.calls.append((sql, params))
        return 1

    def events(self):
        return [(p[2], json.loads(p[-1]) if p[-1] else {}) for _s, p in self.calls]


class _FakeGraphClient:
    def __init__(self, mailbox="op@client.example") -> None:
        self.mailbox = mailbox
        self.replies: list[tuple[str, str]] = []
        self.sends: list[dict] = []

    def reply(self, message_id, comment, *, reply_all=False):
        self.replies.append((message_id, comment))
        return {"status": "replied"}

    def send_mail(self, *, to, subject, body_text, cc=None, save_to_sent_items=True):
        self.sends.append({"to": to, "subject": subject, "body_text": body_text, "cc": cc})
        return {"status": "sent"}


# ---------------------------------------------------------------------------
# Reply relay — msgraph provider dispatch
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_origin():
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    yield
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()


def _reply_mod(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-reply")
    d1 = _FakeD1()
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(_MSGRAPH_ROSTERED_YAML)
    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_API_KEY", None, raising=False)  # msgraph seat: no AgentMail key
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", yaml_path, raising=False)
    return mod, d1


def _record_origin(
    sender="greg@whitfield.example", message_id="graph-mid-1", mailbox="op@client.example"
):
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1",
        inbound.InboundOrigin(sender_address=sender, message_id=message_id, inbox_id=mailbox),
    )


def test_msgraph_reply_dispatches_via_graph_reply(monkeypatch, tmp_path):
    mod, d1 = _reply_mod(monkeypatch, tmp_path)
    fake = _FakeGraphClient()
    monkeypatch.setattr(mod.msgraph_client, "build_client_from_env", lambda **k: fake)
    _record_origin()
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={
            "to": ["greg@whitfield.example"],
            "subject": "Re: matter",
            "body_text": "Thanks, will do.",
        },
        session_id="s1",
    )
    # Replied in-thread on the recorded Graph message id (structural lock).
    assert fake.replies == [("graph-mid-1", "Thanks, will do.")]
    events = d1.events()
    assert any(a == "REPLY_SENT" for a, _ in events)
    _, meta = next((a, m) for a, m in events if a == "REPLY_SENT")
    assert meta["adapter"] == "msgraph"
    assert "Thanks, will do." not in json.dumps(meta)  # body never persisted


def test_msgraph_reply_fails_closed_when_creds_missing(monkeypatch, tmp_path):
    mod, d1 = _reply_mod(monkeypatch, tmp_path)
    # build_client_from_env returns None (MSGRAPH_* unset) → refuse, do NOT fall
    # back to AgentMail.
    monkeypatch.setattr(mod.msgraph_client, "build_client_from_env", lambda **k: None)
    _record_origin()
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"to": ["greg@whitfield.example"], "subject": "Re: matter", "body_text": "Thanks."},
        session_id="s1",
    )
    events = d1.events()
    assert any(a == "REPLY_FAILED" for a, _ in events)
    assert not any(a == "REPLY_SENT" for a, _ in events)


# ---------------------------------------------------------------------------
# Confirm dispatch — msgraph /sendMail transport
# ---------------------------------------------------------------------------

_ALLOWED = "7367659986"
_TO = "client@example.com"
_MSGRAPH_SEND = "mcp_msgraph_mail_send_message"


@pytest.fixture(autouse=True)
def _clean_pending(monkeypatch):
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", _ALLOWED)
    yield
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()


def _arm_msgraph(monkeypatch, trust):
    enforce = trust.enforce
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.CONFIRM},
    )
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: ["scott@smd.services"])
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: [])
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "agent-crane")


def _capture_and_approve_msgraph(trust, body="the reviewed body", session="s1"):
    trust.enforce.evaluate_tool_call(
        _MSGRAPH_SEND,
        {"to": [_TO], "subject": "Report", "body_text": body},
        "scott",
        session_id=session,
    )
    assert PENDING_SEND.peek() is not None
    PENDING_SEND.mark_approved(f"telegram:{_ALLOWED}")


def test_confirm_dispatch_routes_msgraph_send_via_graph(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    _arm_msgraph(monkeypatch, trust)
    fake = _FakeGraphClient()
    monkeypatch.setattr(
        trust.outbound_send.msgraph_client, "build_client_from_env", lambda **k: fake
    )
    _capture_and_approve_msgraph(trust)
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None and "Dispatched" in ctx and _TO in ctx
    # The STORED flat payload was sent via Graph /sendMail (not AgentMail).
    assert len(fake.sends) == 1
    assert fake.sends[0]["to"] == [_TO] and fake.sends[0]["body_text"] == "the reviewed body"
    assert PENDING_SEND.peek() is None  # single-use


def test_confirm_dispatch_msgraph_fails_closed_without_creds(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    _arm_msgraph(monkeypatch, trust)
    monkeypatch.setattr(
        trust.outbound_send.msgraph_client, "build_client_from_env", lambda **k: None
    )
    _capture_and_approve_msgraph(trust)
    ctx = trust._dispatch_approved_send("s1", "scott")
    # Not delivered; the caller reports the failure plainly.
    assert ctx is not None and "not sent" in ctx.lower()


def test_send_via_msgraph_builds_flat_payload(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    fake = _FakeGraphClient()
    monkeypatch.setattr(
        trust.outbound_send.msgraph_client, "build_client_from_env", lambda **k: fake
    )
    trust.outbound_send.send_via_msgraph(
        {
            "to": ["a@x.example"],
            "cc": ["b@x.example"],
            "subject": "S",
            "body_text": "B",
            "extra": "drop",
        }
    )
    assert fake.sends == [
        {"to": ["a@x.example"], "subject": "S", "body_text": "B", "cc": ["b@x.example"]}
    ]


def test_send_via_msgraph_refuses_without_client(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(
        trust.outbound_send.msgraph_client, "build_client_from_env", lambda **k: None
    )
    with pytest.raises(trust.outbound_send.MsGraphSendError):
        trust.outbound_send.send_via_msgraph({"to": ["a@x.example"], "body_text": "B"})
