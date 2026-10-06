"""The verified reply binding (hermes-smd-reply/binding.py), overlay side.

A job's completion wake or a scheduled turn binds to ONE earlier email through
the broker; its create_draft is then relayed like any reply, but transmitted
through ``msgraph_reply_bound``. Each test pins a guard and fails without it.
"""

from __future__ import annotations

import json

import pytest

from shared import inbound
from shared.action_classes import TOOL_ACTION_CLASS_MAP, ActionClass
from shared.inbound import SESSION_TAINT, TRUST_CLASS_UNKNOWN_EXTERNAL
from tests.conftest import load_plugin

ADMIN = "admin@firm.example"
OTHER = "someone@firm.example"
GRAPH_ID = "AAMkSOURCEMESSAGE0001="
JOB = "01J0000000000000000000000Z"
OTHER_JOB = "01J0000000000000000000000Y"
WAKE = "wake-1"
CRON = "cron_abc123def456_20261006_120000"

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
    # Release ENABLED, so the test that a bound rate-hold is never queued can
    # fail: with release off nothing is ever queued and that test proves nothing.
    "send_policy:\n"
    "  held_release:\n"
    "    enabled: true\n"
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
    def __init__(self) -> None:
        self.bound = True
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
        return {"ok": True, "bound": True, "sender": ADMIN, "graph_message_id": GRAPH_ID}

    def send_bound_reply(self, req, comment, *, html="", session_id="", matter_ref=None):
        self.bound_sends.append({"binding": req, "session_id": session_id})
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
    mod.binding.TURN_SOURCES._reset_for_tests()
    yield
    mod.binding.SESSION_BINDINGS._reset_for_tests()
    mod.binding.TURN_SOURCES._reset_for_tests()


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


def _wake(mod, session: str = WAKE, job: str = JOB, sender: str = "webhook:handoff") -> None:
    mod.on_pre_llm_call(
        session_id=session,
        sender_id=sender,
        user_message=(
            f"Run the demand-letter-drafter skill's DELIVER mode for demand job {job}.\n"
            "Kind: demand."
        ),
    )


def _bind(mod, args: dict, session: str = WAKE) -> dict:
    """The tool call and both hooks, in the pinned Hermes order."""
    result = mod.binding.handle_tool(args)
    replaced = mod.on_transform_tool_result(
        tool_name="reply_bind", result=result, session_id=session, tool_call_id="b1"
    )
    mod.on_post_tool_call(
        tool_name="reply_bind", result=result, session_id=session, tool_call_id="b1"
    )
    return json.loads(replaced if isinstance(replaced, str) else result)


def _draft(
    mod, to, session: str = WAKE, call: str = "c1", body: str = "Filed both to the matter."
) -> None:
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"to": to, "subject": "Re: demand prep", "body_text": body},
        session_id=session,
        tool_call_id=call,
    )


def _held(d1) -> list[dict]:
    return [m for a, m in d1.events() if a == "REPLY_HELD"]


# -- the happy paths


def test_a_demand_wake_replies_through_the_bound_verb(lane) -> None:
    mod, d1, broker, _ = lane
    _wake(mod)
    assert _bind(mod, {"job_id": JOB})["bound"] is True
    _draft(mod, [ADMIN])
    assert broker.bound_sends == [
        {"binding": {"kind": "demand_job", "job_id": JOB}, "session_id": WAKE}
    ]
    assert broker.plain_sends == []
    sent = [m for a, m in d1.events() if a == "REPLY_SENT"]
    assert len(sent) == 1 and sent[0]["in_reply_to"] == GRAPH_ID


def test_a_scheduled_turn_binds_a_message(lane) -> None:
    mod, _d1, broker, _ = lane
    assert _bind(mod, {"graph_message_id": GRAPH_ID}, session=CRON)["bound"] is True
    _draft(mod, [ADMIN], session=CRON)
    assert broker.bound_sends[0]["binding"] == {"kind": "message", "graph_message_id": GRAPH_ID}


# -- which turns may bind


def test_a_turn_that_is_neither_wake_nor_schedule_cannot_bind(lane) -> None:
    mod, _d1, broker, _ = lane
    out = _bind(mod, {"graph_message_id": GRAPH_ID}, session="some-other-turn")
    assert out["bound"] is False and "neither" in out["reason"]
    _draft(mod, [ADMIN], session="some-other-turn")
    assert broker.bound_sends == [] and broker.plain_sends == []


def test_a_wake_from_another_route_is_not_a_handoff(lane) -> None:
    mod, _d1, _broker, _ = lane
    _wake(mod, sender="webhook:agentmail")
    assert _bind(mod, {"job_id": JOB})["bound"] is False


def test_a_demand_wake_cannot_answer_another_job(lane) -> None:
    mod, _d1, _broker, _ = lane
    _wake(mod, job=JOB)
    out = _bind(mod, {"job_id": OTHER_JOB})
    assert out["bound"] is False and "cannot answer another job" in out["reason"]


def test_a_scheduled_turn_cannot_bind_a_demand_job(lane) -> None:
    mod, _d1, _broker, _ = lane
    assert _bind(mod, {"job_id": JOB}, session=CRON)["bound"] is False


def test_an_email_opened_turn_cannot_bind(lane) -> None:
    mod, _d1, _broker, _ = lane
    _wake(mod)
    inbound.SESSION_INBOUND_ORIGIN.record(
        WAKE, inbound.InboundOrigin(sender_address=OTHER, message_id="graph-in-1", inbox_id="op@x")
    )
    out = _bind(mod, {"job_id": JOB})
    assert out["bound"] is False and "an email opened this turn" in out["reason"]
    assert mod.binding.SESSION_BINDINGS.get(WAKE) is None


def test_one_binding_per_turn(lane) -> None:
    mod, _d1, _broker, _ = lane
    _bind(mod, {"graph_message_id": GRAPH_ID}, session=CRON)
    out = _bind(mod, {"internet_message_id": "<x@y.example>"}, session=CRON)
    assert out["bound"] is False and "already bound" in out["reason"]


def test_a_held_reply_waiting_for_that_email_blocks_the_binding(lane) -> None:
    """The release would answer it a second time. FALSIFIER: drop held_pending."""
    mod, _d1, broker, _ = lane
    mod._HELD_STORE.enqueue(
        sender=ADMIN,
        sender_class="internal",
        adapter="msgraph",
        inbox_id="op@x",
        message_id=GRAPH_ID,
        send_text="held",
        send_html="",
        body_digest="d",
        hold_reason="rate_limited",
    )
    out = _bind(mod, {"graph_message_id": GRAPH_ID}, session=CRON)
    assert out["bound"] is False and "held reply" in out["reason"]
    _draft(mod, [ADMIN], session=CRON)
    assert broker.bound_sends == []


def test_a_refused_binding_binds_nothing(lane) -> None:
    mod, _d1, broker, _ = lane
    broker.bound = False
    _wake(mod)
    out = _bind(mod, {"job_id": JOB})
    assert out["bound"] is False and "already been answered" in out["reason"]
    _draft(mod, [ADMIN])
    assert broker.bound_sends == []


def test_without_a_binding_a_wake_turn_sends_nothing(lane) -> None:
    mod, _d1, broker, _ = lane
    _wake(mod)
    _draft(mod, [ADMIN])
    assert broker.bound_sends == [] and broker.plain_sends == []


# -- the relay's guards still hold


def test_a_draft_to_anyone_else_is_held(lane) -> None:
    mod, d1, broker, _ = lane
    _wake(mod)
    _bind(mod, {"job_id": JOB})
    _draft(mod, [OTHER])
    assert broker.bound_sends == []
    assert any(m["reason"] == "recipient_mismatch" for m in _held(d1))


def test_the_fabrication_floor_still_holds_a_bound_reply(lane) -> None:
    mod, d1, broker, _ = lane
    _wake(mod)
    _bind(mod, {"job_id": JOB})
    _draft(
        mod,
        [ADMIN],
        body="Per Smith v. Jones, 123 F.3d 456 (9th Cir. 1999), the lien is waived.",
    )
    assert broker.bound_sends == [] and _held(d1)


def _spy_floor(mod, monkeypatch) -> list[bool]:
    seen: list[bool] = []
    real = mod.relay.gate_body

    def spy(*a, **k):
        seen.append(k["internal_recipient"])
        return real(*a, **k)

    monkeypatch.setattr(mod.relay, "gate_body", spy)
    return seen


def test_a_tainted_bound_turn_gets_the_content_floor(lane, monkeypatch) -> None:
    """FALSIFIER: drop the taint branch and the floor is skipped for a colleague."""
    mod, _d1, _broker, _ = lane
    seen = _spy_floor(mod, monkeypatch)
    _wake(mod)
    _bind(mod, {"job_id": JOB})
    SESSION_TAINT.mark(WAKE, TRUST_CLASS_UNKNOWN_EXTERNAL)
    _draft(mod, [ADMIN])
    assert seen == [False]


def test_an_untainted_bound_turn_keeps_the_colleague_carve_out(lane, monkeypatch) -> None:
    mod, _d1, _broker, _ = lane
    seen = _spy_floor(mod, monkeypatch)
    _wake(mod)
    _bind(mod, {"job_id": JOB})
    _draft(mod, [ADMIN])
    assert seen == [True]


def _rate_hold_everything(mod, monkeypatch) -> None:
    monkeypatch.setattr(
        mod._LIMITER, "check", lambda *a, **k: mod.relay.RateDecision(False, "rate_limited")
    )


def test_a_rate_held_bound_reply_is_never_queued_for_release(lane, monkeypatch) -> None:
    """Release is enabled in the fixture, so an ordinary rate-hold WOULD queue
    (the control below); a bound one must not. FALSIFIER: drop the BOUND_INBOX
    branch in _enqueue_hold."""
    mod, d1, broker, _ = lane
    _rate_hold_everything(mod, monkeypatch)
    _wake(mod)
    _bind(mod, {"job_id": JOB})
    _draft(mod, [ADMIN])
    assert broker.bound_sends == []
    assert mod._HELD_STORE.pending_count() == 0
    assert _held(d1)[0]["held_for_release"] is False


def test_an_ordinary_rate_hold_does_queue_under_this_fixture(lane, monkeypatch) -> None:
    mod, _d1, _broker, _ = lane
    _rate_hold_everything(mod, monkeypatch)
    inbound.SESSION_INBOUND_ORIGIN.record(
        "mail-1",
        inbound.InboundOrigin(sender_address=ADMIN, message_id="graph-in-9", inbox_id="op@x"),
    )
    _draft(mod, [ADMIN], session="mail-1")
    assert mod._HELD_STORE.pending_count() == 1


def test_another_transport_holds_a_bound_reply(lane) -> None:
    mod, d1, broker, yaml_path = lane
    yaml_path.write_text(_SEAT_YAML.format(adapter="agentmail"))
    _wake(mod)
    _bind(mod, {"job_id": JOB})
    _draft(mod, [ADMIN])
    assert broker.bound_sends == []
    assert any(m["reason"] == "bound_reply_unsupported" for m in _held(d1))


# -- the surface


def test_the_tool_takes_exactly_one_email(lane) -> None:
    mod, _d1, broker, _ = lane
    assert json.loads(mod.binding.handle_tool({}))["bound"] is False
    both = {"job_id": JOB, "graph_message_id": GRAPH_ID}
    assert json.loads(mod.binding.handle_tool(both))["bound"] is False
    assert broker.binds == []


def test_the_schema_offers_no_recipient(lane) -> None:
    props = load_plugin("hermes-smd-reply").binding.TOOL_SCHEMA["properties"]
    assert set(props) == {"job_id", "graph_message_id", "internet_message_id"}


def test_reply_bind_is_classified(lane) -> None:
    assert TOOL_ACTION_CLASS_MAP["reply_bind"] is ActionClass.INTERNAL_WRITE


def test_the_plugin_registers_the_tool_and_the_wake_hook(tmp_path, monkeypatch) -> None:
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
    assert "reply_bind" in ctx.tools and "pre_llm_call" in ctx.hooks
    assert ctx.tools["reply_bind"]["schema"]["parameters"]["additionalProperties"] is False


# -- a demand job's wake has one channel (2026-10-06 practice job)


def test_a_demand_wake_refuses_a_binding_by_email_id(lane) -> None:
    """FALSIFIER: drop the handoff+job branch and the email-id bind is taken."""
    mod, _d1, _broker, _ = lane
    _wake(mod)
    out = _bind(mod, {"internet_message_id": "<req@firm.example>"})
    assert out["bound"] is False and f"bind with job_id={JOB}" in out["reason"]
    out = _bind(mod, {"graph_message_id": GRAPH_ID})
    assert out["bound"] is False


@pytest.mark.parametrize(
    "tool",
    [
        "smd_send_message",
        "casework_brief",
        "mcp_msgraph_mail_send_message",
        "mcp_agentmail_send_message",
    ],
)
def test_a_demand_wake_may_call_no_send_tool(lane, tool) -> None:
    mod, _d1, _broker, _ = lane
    _wake(mod)
    verdict = mod.on_pre_tool_call(tool_name=tool, args={}, session_id=WAKE)
    assert verdict is not None and verdict["action"] == "block"


def test_the_bound_reply_path_stays_open_in_a_wake(lane) -> None:
    mod, _d1, _broker, _ = lane
    _wake(mod)
    for tool in ("reply_bind", "mcp_msgraph_mail_create_draft", "demand_job_status"):
        assert mod.on_pre_tool_call(tool_name=tool, args={}, session_id=WAKE) is None, tool


def test_other_turns_are_untouched_by_the_wake_guard(lane) -> None:
    mod, _d1, _broker, _ = lane
    assert mod.on_pre_tool_call(tool_name="smd_send_message", args={}, session_id="mail-1") is None
    assert mod.on_pre_tool_call(tool_name="smd_send_message", args={}, session_id=CRON) is None
