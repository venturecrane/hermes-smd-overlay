"""Graph transport tests for the reply relay + confirm dispatch (ADR 0078, piece 3).

The seam is provider-neutral above the transport: roster / recipient-lock / floors
/ rate-limit are shared, and only the wire call differs. These pin the msgraph
branch of both out-of-band paths, plus the shared send tool:

  * the reply relay answers in-thread on the recorded inbound message id, and
    REFUSES fail-closed (audited REPLY_FAILED) when there is no transmit path —
    never falling back to AgentMail;
  * the confirm dispatch sends an approved payload with the flat
    to/cc/subject/body_text shape, and raises MsGraphSendError (→
    CONFIRM_SEND_FAILED) when it cannot;
  * `smd_send_message` routes by the adapter the SEAT authors, so the one tool
    serves both channels without either seat reaching the other's transport.

ss#2258: all three go through the workspace broker now. What used to be asserted
against a fake Graph client is asserted against a fake broker, and the difference
is the point — the fake cannot name a mailbox, cannot name a reply's recipient,
and holds no credential, because none of those are the agent's to decide any more.

The limit this file cannot test, stated so nobody reads its green as more than it
is: the agent still holds MSGRAPH_* for reads, so a path that ignores these
functions entirely can still reach Graph. Only a second, read-only app
registration closes that, and it lives in the customer's tenant.
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


class _FakeGraphBroker:
    """Stands in for the broker's msgraph verbs (ss#2258).

    Note what it CANNOT express, because that is the change these tests exist to
    pin: no mailbox, no recipient for a reply, no credential. The broker takes
    the first two from the seat's own customer.yaml and holds the third, so the
    agent-side surface has nothing left to get wrong.
    """

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.sends: list[dict] = []

    def send_reply(self, message_id, comment):
        self.replies.append((message_id, comment))
        return ""

    def send_message(self, payload):
        self.sends.append(dict(payload))
        return ""


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
    fake = _FakeGraphBroker()
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", fake.send_reply)
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


def test_msgraph_reply_fails_closed_when_broker_unreachable(monkeypatch, tmp_path):
    mod, d1 = _reply_mod(monkeypatch, tmp_path)

    # No broker transmit path → refuse, do NOT fall back to AgentMail and do NOT
    # fall back to a direct Graph call. "Unavailable" must never become "allowed".
    def _unavailable(*_a, **_k):
        raise mod.msgraph_broker.MsGraphBrokerUnavailable("no socket")

    monkeypatch.setattr(mod.msgraph_broker, "send_reply", _unavailable)
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


def test_confirm_dispatch_routes_msgraph_send_via_broker(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    _arm_msgraph(monkeypatch, trust)
    fake = _FakeGraphBroker()
    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", fake.send_message)
    _capture_and_approve_msgraph(trust)
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None and "Dispatched" in ctx and _TO in ctx
    # The STORED flat payload went to the Graph broker verb, not the AgentMail one.
    assert len(fake.sends) == 1
    assert fake.sends[0]["to"] == [_TO] and fake.sends[0]["body_text"] == "the reviewed body"
    assert PENDING_SEND.peek() is None  # single-use


def test_confirm_dispatch_msgraph_never_falls_back_to_agentmail(monkeypatch):
    """A Graph seat whose broker refuses must NOT reach the AgentMail verb.

    The falsifier this exists for: route by the wrong signal — a tool name, a
    default — and an msgraph seat quietly transmits through the other channel,
    from an inbox it does not own, past a fence built for a different roster.
    """
    trust = load_plugin("hermes-smd-trust")
    _arm_msgraph(monkeypatch, trust)
    agentmail_calls: list[dict] = []

    def _refuse(_payload):
        raise trust.outbound_send.msgraph_broker.BrokerError("recipient not authored")

    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", _refuse)
    monkeypatch.setattr(
        trust.outbound_send,
        "send_message",
        lambda **kw: agentmail_calls.append(kw) or "am-1",
    )
    _capture_and_approve_msgraph(trust)
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None and "not sent" in ctx.lower()
    assert agentmail_calls == []


def test_confirm_dispatch_msgraph_fails_closed_without_broker(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    _arm_msgraph(monkeypatch, trust)

    def _unavailable(_payload):
        raise trust.outbound_send.msgraph_broker.MsGraphBrokerUnavailable("no socket")

    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", _unavailable)
    _capture_and_approve_msgraph(trust)
    ctx = trust._dispatch_approved_send("s1", "scott")
    # Not delivered; the caller reports the failure plainly.
    assert ctx is not None and "not sent" in ctx.lower()


def test_send_via_msgraph_forwards_a_closed_allowlist(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    fake = _FakeGraphBroker()
    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", fake.send_message)
    trust.outbound_send.send_via_msgraph(
        {
            "to": ["a@x.example"],
            "cc": ["b@x.example"],
            "bcc": ["c@x.example"],
            "subject": "S",
            "body_text": "B",
            "extra": "drop",
            "grant": "drop",
        }
    )
    assert fake.sends == [
        {
            "to": ["a@x.example"],
            "cc": ["b@x.example"],
            "bcc": ["c@x.example"],
            "subject": "S",
            "body_text": "B",
        }
    ]


def test_send_via_msgraph_refuses_without_a_recipient(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    fake = _FakeGraphBroker()
    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", fake.send_message)
    with pytest.raises(trust.outbound_send.MsGraphSendError):
        trust.outbound_send.send_via_msgraph({"body_text": "B"})
    assert fake.sends == []


# ---------------------------------------------------------------------------
# smd_send_message — ONE tool, routed by what the SEAT authors
# ---------------------------------------------------------------------------
#
# These are the tests for the branch that replaced tool-name routing. With the
# msgraph connector's own send tool off the menu, `smd_send_message` is the only
# send an msgraph seat has, and if it routed by tool name it would take the
# AgentMail path on every one of them.

_AGENTMAIL_SEAT_YAML = (
    "customer_id: acme\nvertical: law-firm\nconnectors:\n  Email:\n"
    "    adapter: agentmail\n    backend: mcp:agentmail\n    enabled: true\n"
)


def _seat_yaml(monkeypatch, tmp_path, body: str):
    path = tmp_path / "customer.yaml"
    path.write_text(body)
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(path))
    return path


def _both_transports(monkeypatch, trust):
    """Stub BOTH transports so a mis-route is visible as the wrong list filling."""
    graph: list[dict] = []
    agentmail: list[dict] = []
    monkeypatch.setattr(
        trust.outbound_send.msgraph_broker,
        "send_message",
        lambda payload: graph.append(dict(payload)) or "",
    )
    monkeypatch.setattr(
        trust.outbound_send,
        "send_message",
        lambda **kw: agentmail.append(kw) or "am-1",
    )
    return graph, agentmail


def test_send_tool_routes_by_authored_adapter_msgraph(monkeypatch, tmp_path):
    trust = load_plugin("hermes-smd-trust")
    _seat_yaml(monkeypatch, tmp_path, _MSGRAPH_ROSTERED_YAML)
    graph, agentmail = _both_transports(monkeypatch, trust)
    trust._smd_send_message(to=[_TO], subject="S", text="B")
    assert len(graph) == 1 and agentmail == []
    # `text` reaches the Graph verb under its own name; the broker accepts either
    # spelling, so nothing is silently dropped on the way.
    assert graph[0]["text"] == "B"


def test_send_tool_routes_by_authored_adapter_agentmail(monkeypatch, tmp_path):
    trust = load_plugin("hermes-smd-trust")
    _seat_yaml(monkeypatch, tmp_path, _AGENTMAIL_SEAT_YAML)
    graph, agentmail = _both_transports(monkeypatch, trust)
    trust._smd_send_message(to=[_TO], subject="S", text="B")
    assert len(agentmail) == 1 and graph == []


def test_confirm_dispatch_routes_by_adapter_when_the_tool_is_the_shared_one(monkeypatch, tmp_path):
    """The withheld-then-approved path on an msgraph seat, via `smd_send_message`.

    This is the exact combination the old tool-name check got wrong: the pending
    record carries the shared tool name, so `rec.tool_name == _MSGRAPH_SEND_TOOL`
    is False, and without the adapter read the approved send would have gone out
    through AgentMail — a different inbox, a different fence.
    """
    trust = load_plugin("hermes-smd-trust")
    _arm_msgraph(monkeypatch, trust)
    _seat_yaml(monkeypatch, tmp_path, _MSGRAPH_ROSTERED_YAML)
    graph, agentmail = _both_transports(monkeypatch, trust)
    trust.enforce.evaluate_tool_call(
        "smd_send_message",
        {"to": [_TO], "subject": "Report", "text": "the reviewed body"},
        "scott",
        session_id="s1",
    )
    assert PENDING_SEND.peek() is not None
    PENDING_SEND.mark_approved(f"telegram:{_ALLOWED}")
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None and "Dispatched" in ctx
    assert len(graph) == 1 and agentmail == []


def test_send_via_msgraph_surfaces_a_broker_refusal(monkeypatch):
    trust = load_plugin("hermes-smd-trust")

    def _refuse(_payload):
        raise trust.outbound_send.msgraph_broker.BrokerError("not on the authored surface")

    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", _refuse)
    with pytest.raises(trust.outbound_send.MsGraphSendError) as excinfo:
        trust.outbound_send.send_via_msgraph({"to": ["a@x.example"], "body_text": "B"})
    # The broker's REASON reaches the operator, not a generic delivery failure.
    assert "not on the authored surface" in str(excinfo.value)
