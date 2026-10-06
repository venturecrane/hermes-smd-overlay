"""The verified reply binding (hermes-smd-reply/binding.py), overlay side.

A turn no email opened binds to ONE earlier email through the broker, then its
create_draft is relayed like any reply, but transmitted through
``msgraph_reply_bound``. Each test pins a guard and fails without it:

* the agent names an email, never a person; the bound sender is the broker's;
* a draft to anyone but the bound sender is held (recipient lock);
* without a binding, a non-inbound turn still sends nothing;
* an email-opened turn cannot bind (its replies are that email's);
* one binding per turn;
* a bound reply goes through the bound verb, never the ordinary reply verb;
* the floors still run (a fabricated reply is held, not sent);
* a rate-held bound reply is never queued for release (that path would skip
  the broker's once-only claim).
"""

from __future__ import annotations

import json

import pytest

from shared import inbound
from shared.action_classes import TOOL_ACTION_CLASS_MAP, ActionClass
from shared.inbound import SESSION_TAINT
from tests.conftest import load_plugin

ADMIN = "admin@firm.example"
OTHER = "someone@firm.example"
GRAPH_ID = "AAMkSOURCEMESSAGE0001="
JOB = "01J0000000000000000000000Z"

_SEAT_YAML = (
    "customer_id: acme\n"
    "vertical: law-firm\n"
    "connectors:\n"
    "  Email:\n"
    "    adapter: {adapter}\n"
    "    enabled: true\n"
    "scope:\n"
    "  inbound_allow_from:\n"
    "    - '@firm.example'\n"
    "  admins:\n"
    f"    - {ADMIN}\n"
)


class _FakeD1:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql, *params):
        self.calls.append((sql, params))
        return 1

    def events(self) -> list[tuple[str, dict]]:
        return [(p[2], json.loads(p[-1]) if p[-1] else {}) for _s, p in self.calls]


class _FakeBroker:
    def __init__(self, sender: str = ADMIN, bound: bool = True) -> None:
        self.sender, self.bound = sender, bound
        self.binds: list[dict] = []
        self.bound_sends: list[dict] = []
        self.plain_sends: list[dict] = []

    def bind_reply(self, req):
        self.binds.append(req)
        if not self.bound:
            return {
                "ok": True,
                "bound": False,
                "reason": "that email has already been answered from this mailbox",
            }
        return {"ok": True, "bound": True, "sender": self.sender}

    def send_bound_reply(self, req, comment, *, html="", session_id="", matter_ref=None):
        self.bound_sends.append({"binding": req, "comment": comment, "session_id": session_id})
        return "<sent@firm.example>"

    def send_reply(self, message_id, comment, **kw):
        self.plain_sends.append({"message_id": message_id})
        return "<plain@firm.example>"


@pytest.fixture(autouse=True)
def _clean():
    mod = load_plugin("hermes-smd-reply")
    for reg in (
        inbound.SESSION_INBOUND_ORIGIN._origins,
        inbound.SESSION_INBOUND_ORIGIN._by_address,
        inbound.SESSION_INBOUND_ORIGIN._by_message,
        SESSION_TAINT._tainted,
    ):
        reg.clear()
    mod.binding.SESSION_BINDINGS._reset_for_tests()
    yield
    mod.binding.SESSION_BINDINGS._reset_for_tests()


@pytest.fixture
def lane(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-reply")
    d1, broker = _FakeD1(), _FakeBroker()
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(_SEAT_YAML.format(adapter="msgraph"))
    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod, "_REPLIED", mod.relay.RepliedOnce(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", yaml_path, raising=False)
    monkeypatch.setattr(
        mod, "_HELD_STORE", mod.held_store.HeldReplyStore(str(tmp_path / "held.db")), raising=False
    )
    monkeypatch.setattr(mod.msgraph_broker, "bind_reply", broker.bind_reply)
    monkeypatch.setattr(mod.msgraph_broker, "send_bound_reply", broker.send_bound_reply)
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", broker.send_reply)
    return mod, d1, broker, yaml_path


def _bind(mod, args: dict, session: str = "wake-1") -> str:
    """The tool call and both hooks, in the pinned Hermes order."""
    result = mod.binding.handle_tool(args)
    replaced = mod.on_transform_tool_result(
        tool_name="reply_bind", result=result, session_id=session, tool_call_id="b1"
    )
    final = replaced if isinstance(replaced, str) else result
    mod.on_post_tool_call(
        tool_name="reply_bind", result=result, session_id=session, tool_call_id="b1"
    )
    return final


def _draft(
    mod, to, session: str = "wake-1", call: str = "c1", body: str = "Filed both to the matter."
) -> None:
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"to": to, "subject": "Re: demand prep", "body_text": body},
        session_id=session,
        tool_call_id=call,
    )


def test_a_bound_turn_replies_through_the_bound_verb(lane) -> None:
    mod, d1, broker, _ = lane
    out = json.loads(_bind(mod, {"job_id": JOB}))
    assert out["bound"] is True and out["sender"] == ADMIN
    assert broker.binds == [{"kind": "demand_job", "job_id": JOB}]
    _draft(mod, [ADMIN])
    assert broker.bound_sends == [
        {
            "binding": {"kind": "demand_job", "job_id": JOB},
            "comment": broker.bound_sends[0]["comment"],
            "session_id": "wake-1",
        }
    ]
    assert broker.plain_sends == []
    assert [a for a, _m in d1.events()].count("REPLY_SENT") == 1


def test_a_graph_id_binds_as_a_message(lane) -> None:
    mod, _d1, broker, _ = lane
    _bind(mod, {"graph_message_id": GRAPH_ID})
    assert broker.binds == [{"kind": "message", "graph_message_id": GRAPH_ID}]


def test_without_a_binding_a_wake_turn_sends_nothing(lane) -> None:
    """FALSIFIER for the relay's fail-closed default: no origin, no binding."""
    mod, _d1, broker, _ = lane
    _draft(mod, [ADMIN])
    assert broker.bound_sends == [] and broker.plain_sends == []


def test_a_draft_to_anyone_else_is_held(lane) -> None:
    """The recipient lock holds the bound sender, not the draft's choice."""
    mod, d1, broker, _ = lane
    _bind(mod, {"job_id": JOB})
    _draft(mod, [OTHER])
    assert broker.bound_sends == []
    assert any(a == "REPLY_HELD" and m["reason"] == "recipient_mismatch" for a, m in d1.events())


def test_a_refused_binding_binds_nothing(lane) -> None:
    mod, _d1, broker, _ = lane
    broker.bound = False
    out = json.loads(_bind(mod, {"job_id": JOB}))
    assert out["bound"] is False and "already been answered" in out["reason"]
    _draft(mod, [ADMIN])
    assert broker.bound_sends == []


def test_an_email_opened_turn_cannot_bind(lane) -> None:
    mod, _d1, broker, _ = lane
    inbound.SESSION_INBOUND_ORIGIN.record(
        "wake-1",
        inbound.InboundOrigin(sender_address=OTHER, message_id="graph-in-1", inbox_id="op@x"),
    )
    out = json.loads(_bind(mod, {"job_id": JOB}))
    assert out["bound"] is False and "an email opened this turn" in out["reason"]
    assert mod.binding.SESSION_BINDINGS.get("wake-1") is None


def test_one_binding_per_turn(lane) -> None:
    mod, _d1, _broker, _ = lane
    _bind(mod, {"job_id": JOB})
    out = json.loads(_bind(mod, {"graph_message_id": GRAPH_ID}))
    assert out["bound"] is False and "already bound" in out["reason"]
    assert mod.binding.SESSION_BINDINGS.get("wake-1").as_request() == {
        "kind": "demand_job",
        "job_id": JOB,
    }


def test_the_tool_takes_exactly_one_email(lane) -> None:
    mod, _d1, broker, _ = lane
    assert json.loads(mod.binding.handle_tool({}))["bound"] is False
    assert (
        json.loads(mod.binding.handle_tool({"job_id": JOB, "graph_message_id": GRAPH_ID}))["bound"]
        is False
    )
    assert broker.binds == []


def test_the_schema_offers_no_recipient(lane) -> None:
    props = load_plugin("hermes-smd-reply").binding.TOOL_SCHEMA["properties"]
    assert set(props) == {"job_id", "graph_message_id", "internet_message_id"}


def test_the_fabrication_floor_still_holds_a_bound_reply(lane) -> None:
    mod, d1, broker, _ = lane
    _bind(mod, {"job_id": JOB})
    _draft(
        mod, [ADMIN], body="Per Smith v. Jones, 123 F.3d 456 (9th Cir. 1999), the lien is waived."
    )
    assert broker.bound_sends == []
    assert any(a == "REPLY_HELD" for a, _m in d1.events())


def test_a_rate_held_bound_reply_is_never_queued_for_release(lane, monkeypatch) -> None:
    mod, d1, broker, _ = lane
    monkeypatch.setattr(
        mod._LIMITER,
        "check",
        lambda *a, **k: mod.relay.RateDecision(False, "rate_limited"),
        raising=True,
    )
    _bind(mod, {"job_id": JOB})
    _draft(mod, [ADMIN])
    assert broker.bound_sends == []
    assert mod._HELD_STORE.pending_count() == 0
    held = [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert held and held[0].get("held_for_release") is False


def test_another_transport_holds_a_bound_reply(lane) -> None:
    mod, d1, broker, yaml_path = lane
    yaml_path.write_text(_SEAT_YAML.format(adapter="agentmail"))
    _bind(mod, {"job_id": JOB})
    _draft(mod, [ADMIN])
    assert broker.bound_sends == []
    assert any(
        a == "REPLY_HELD" and m["reason"] == "bound_reply_unsupported" for a, m in d1.events()
    )


def test_reply_bind_is_classified(lane) -> None:
    assert TOOL_ACTION_CLASS_MAP["reply_bind"] is ActionClass.INTERNAL_WRITE


def test_the_plugin_registers_the_tool(tmp_path, monkeypatch) -> None:
    class Ctx:
        def __init__(self) -> None:
            self.tools: dict = {}
            self.hooks: list = []

        def register_tool(self, name, **kw):
            self.tools[name] = kw

        def register_hook(self, name, fn):
            self.hooks.append(name)

    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(tmp_path / "c.yaml"))
    mod = load_plugin("hermes-smd-reply")
    monkeypatch.setattr(mod, "_start_held_release", lambda: None)
    ctx = Ctx()
    mod.register(ctx)
    assert "reply_bind" in ctx.tools
    assert ctx.tools["reply_bind"]["schema"]["parameters"]["additionalProperties"] is False
