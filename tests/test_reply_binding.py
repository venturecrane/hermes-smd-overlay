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
    inbound.SESSION_INBOUND_ORIGIN.note_turn_prompt(WAKE, "graph-in-1")
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
    inbound.SESSION_INBOUND_ORIGIN.note_turn_prompt("mail-1", "graph-in-9")
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


@pytest.mark.parametrize(
    "args",
    [
        {"internet_message_id": "<req@firm.example>"},
        {"graph_message_id": GRAPH_ID},
        {"job_id": OTHER_JOB},
    ],
)
def test_a_wake_bind_by_email_id_is_refused_before_the_broker(lane, args) -> None:
    """A live demand job: the broker's 'already had its bound reply' reached the model
    first, so it never learned to bind by job id. FALSIFIER: drop the
    reply_bind branch and the guard lets the broker be asked."""
    mod, _d1, _broker, _ = lane
    _wake(mod)
    verdict = mod.on_pre_tool_call(tool_name="reply_bind", args=args, session_id=WAKE)
    assert verdict is not None and verdict["action"] == "block"
    assert (
        verdict["message"] == f"this is demand job {JOB}'s wake; call reply_bind with job_id={JOB}"
    )


def test_a_wake_bind_by_its_own_job_id_passes_the_guard(lane) -> None:
    mod, _d1, _broker, _ = lane
    _wake(mod)
    assert (
        mod.on_pre_tool_call(tool_name="reply_bind", args={"job_id": JOB}, session_id=WAKE) is None
    )


# -- a drafting job's wake: the demand wake's rules, its own kind


def _drafting_wake(mod, session: str = WAKE, job: str = JOB) -> None:
    mod.on_pre_llm_call(
        session_id=session,
        sender_id="webhook:handoff",
        user_message=(
            f"Run the document-drafter skill's DELIVER mode for drafting job {job}.\n"
            "Kind: drafting.\nDocument class: memo."
        ),
    )


def _verdict(kind: str, job: str = JOB) -> str:
    """A broker 'bound' answer for ``kind``, as handle_tool would return it."""
    return json.dumps(
        {
            "bound": True,
            "binding": {"kind": kind, "job_id": job},
            "sender": ADMIN,
            "graph_message_id": GRAPH_ID,
        }
    )


def test_a_drafting_wake_replies_through_a_drafting_binding(lane) -> None:
    """FALSIFIER: hard-code kind=demand_job in _request_from_args and the broker
    looks the drafting job up on the demand ledger."""
    mod, d1, broker, _ = lane
    _drafting_wake(mod)
    assert _bind(mod, {"job_id": JOB})["bound"] is True
    assert broker.binds == [{"kind": "drafting_job", "job_id": JOB}]
    _draft(mod, [ADMIN])
    assert broker.bound_sends == [
        {"binding": {"kind": "drafting_job", "job_id": JOB}, "session_id": WAKE}
    ]
    sent = [m for a, m in d1.events() if a == "REPLY_SENT"]
    assert len(sent) == 1 and sent[0]["in_reply_to"] == GRAPH_ID


def test_a_demand_wake_still_binds_a_demand_job(lane) -> None:
    mod, _d1, broker, _ = lane
    _drafting_wake(mod, session="other-wake", job=OTHER_JOB)
    _wake(mod)
    assert _bind(mod, {"job_id": JOB})["bound"] is True
    assert broker.binds == [{"kind": "demand_job", "job_id": JOB}]


def test_a_drafting_wake_cannot_answer_another_job(lane) -> None:
    mod, _d1, _broker, _ = lane
    _drafting_wake(mod)
    out = _bind(mod, {"job_id": OTHER_JOB})
    assert out["bound"] is False
    assert out["reason"] == f"this wake is for drafting job {JOB}; it cannot answer another job"


def test_a_demand_binding_is_refused_in_a_drafting_wake(lane) -> None:
    """Cross-kind: the right id, the wrong ledger. FALSIFIER: drop the kind
    comparison in record_from_result and the demand binding is taken."""
    mod, _d1, _broker, _ = lane
    _drafting_wake(mod)
    out = json.loads(mod.binding.record_from_result(WAKE, _verdict("demand_job")))
    assert out["bound"] is False
    assert out["reason"] == f"this wake is for drafting job {JOB}; it cannot answer another job"
    assert mod.binding.SESSION_BINDINGS.get(WAKE) is None


def test_a_drafting_binding_is_refused_in_a_demand_wake(lane) -> None:
    mod, _d1, _broker, _ = lane
    _wake(mod)
    out = json.loads(mod.binding.record_from_result(WAKE, _verdict("drafting_job")))
    assert out["bound"] is False
    assert out["reason"] == f"this wake is for demand job {JOB}; it cannot answer another job"
    assert mod.binding.SESSION_BINDINGS.get(WAKE) is None


def test_a_scheduled_turn_cannot_bind_a_drafting_job(lane) -> None:
    mod, _d1, _broker, _ = lane
    out = json.loads(mod.binding.record_from_result(CRON, _verdict("drafting_job")))
    assert out["bound"] is False
    assert out["reason"] == "a drafting job's reply binds only from that job's own completion wake"


def test_a_drafting_wake_refuses_a_binding_by_email_id(lane) -> None:
    mod, _d1, _broker, _ = lane
    _drafting_wake(mod)
    out = _bind(mod, {"internet_message_id": "<req@firm.example>"})
    assert out["bound"] is False
    assert out["reason"] == (
        f"this is drafting job {JOB}'s wake; bind with job_id={JOB}, never the email's id"
    )


@pytest.mark.parametrize(
    "tool",
    [
        "smd_send_message",
        "casework_brief",
        "mcp_msgraph_mail_send_message",
        "mcp_agentmail_send_message",
    ],
)
def test_a_drafting_wake_may_call_no_send_tool(lane, tool) -> None:
    """FALSIFIER: match only "demand job" in the wake regex and a drafting wake
    is no wake at all, so every send passes (2026-10-06's failure)."""
    mod, _d1, _broker, _ = lane
    _drafting_wake(mod)
    verdict = mod.on_pre_tool_call(tool_name=tool, args={}, session_id=WAKE)
    assert verdict is not None and verdict["action"] == "block"
    assert f"drafting job {JOB}'s completion wake" in verdict["message"]


@pytest.mark.parametrize(
    "args",
    [
        {"internet_message_id": "<req@firm.example>"},
        {"graph_message_id": GRAPH_ID},
        {"job_id": OTHER_JOB},
    ],
)
def test_a_drafting_wake_bind_by_email_id_is_refused_before_the_broker(lane, args) -> None:
    mod, _d1, _broker, _ = lane
    _drafting_wake(mod)
    verdict = mod.on_pre_tool_call(tool_name="reply_bind", args=args, session_id=WAKE)
    assert verdict is not None and verdict["action"] == "block"
    assert verdict["message"] == (
        f"this is drafting job {JOB}'s wake; call reply_bind with job_id={JOB}"
    )


def test_the_bound_reply_path_stays_open_in_a_drafting_wake(lane) -> None:
    mod, _d1, _broker, _ = lane
    _drafting_wake(mod)
    for tool in ("reply_bind", "mcp_msgraph_mail_create_draft", "drafting_job_status"):
        assert mod.on_pre_tool_call(tool_name=tool, args={}, session_id=WAKE) is None, tool
    assert (
        mod.on_pre_tool_call(tool_name="reply_bind", args={"job_id": JOB}, session_id=WAKE) is None
    )


# -- a chronology job's wake (2026-10-07): a held chronology's wake was not
# recognised, and the Operator emailed the requester AND the matter's attorney


def _chronology_wake(mod, session: str = WAKE, job: str = JOB) -> None:
    # The runner's own first line (ss-console operator/runners/medchron/medchron/daemon.py).
    mod.on_pre_llm_call(
        session_id=session,
        sender_id="webhook:handoff",
        user_message=(
            "Run the medical-chronology-maintainer skill's DELIVER mode for chronology job "
            f"{job}.\nRequester: admin@firm.example."
        ),
    )


@pytest.mark.parametrize(
    "tool",
    [
        "smd_send_message",
        "casework_brief",
        "mcp_msgraph_mail_send_message",
        "mcp_agentmail_send_message",
    ],
)
def test_a_chronology_wake_may_call_no_send_tool(lane, tool) -> None:
    """FALSIFIER: drop "chronology" from the wake regex and this is 2026-10-07:
    the wake is no wake, and a new email to the attorney passes."""
    mod, _d1, _broker, _ = lane
    _chronology_wake(mod)
    verdict = mod.on_pre_tool_call(tool_name=tool, args={}, session_id=WAKE)
    assert verdict is not None and verdict["action"] == "block"
    assert f"chronology job {JOB}'s completion wake" in verdict["message"]


def test_a_chronology_wake_replies_through_a_chronology_binding(lane) -> None:
    mod, d1, broker, _ = lane
    _chronology_wake(mod)
    assert _bind(mod, {"job_id": JOB})["bound"] is True
    assert broker.binds == [{"kind": "medchron_job", "job_id": JOB}]
    _draft(mod, [ADMIN])
    assert broker.bound_sends == [
        {"binding": {"kind": "medchron_job", "job_id": JOB}, "session_id": WAKE}
    ]
    sent = [m for a, m in d1.events() if a == "REPLY_SENT"]
    assert len(sent) == 1 and sent[0]["in_reply_to"] == GRAPH_ID


def test_a_chronology_wake_refuses_a_binding_by_email_id(lane) -> None:
    mod, _d1, _broker, _ = lane
    _chronology_wake(mod)
    verdict = mod.on_pre_tool_call(
        tool_name="reply_bind", args={"internet_message_id": "<req@firm.example>"}, session_id=WAKE
    )
    assert verdict is not None and verdict["action"] == "block"
    assert verdict["message"] == (
        f"this is chronology job {JOB}'s wake; call reply_bind with job_id={JOB}"
    )


def test_a_demand_binding_is_refused_in_a_chronology_wake(lane) -> None:
    mod, _d1, _broker, _ = lane
    _chronology_wake(mod)
    out = json.loads(mod.binding.record_from_result(WAKE, _verdict("demand_job")))
    assert out["bound"] is False
    assert out["reason"] == f"this wake is for chronology job {JOB}; it cannot answer another job"


# -- a litigation status job's wake. A REQUEST-trigger wake is fenced exactly
# like a drafting wake. A SCHEDULED-trigger wake has no request email: its one
# channel is the job's binding in the broker's new_message mode (the broker
# sets the recipient and the subject); every send tool stays refused.

SUBJECT = "Litigation status list, October 7, 2026"


def _litigation_wake(
    mod,
    trigger: str = "request",
    session: str = WAKE,
    job: str = JOB,
    sender: str = "webhook:handoff",
) -> None:
    # The lane's own wake (ss-console litigation_lane.py, the frozen interface).
    mod.on_pre_llm_call(
        session_id=session,
        sender_id=sender,
        user_message=(
            f"Run the litigation-status skill's DELIVER mode for litigation job {job}.\n"
            "Kind: litigation.\n"
            f"Trigger: {trigger}.\n"
            "Outcome: delivered.\n"
            "Matters: 40; re-read this run: 6; new flags: 2.\n"
            "Folder id: f-1.\n"
            "Files: workbook (9000 bytes).\n"
            f"Requested by: {ADMIN}."
        ),
    )


def _new_message_broker(mod, monkeypatch, broker) -> None:
    """The broker's answer for a scheduled litigation job (ss-console
    reply_binding.py): a new message, no email id."""

    def bind_reply(req):
        broker.binds.append(req)
        return {
            "ok": True,
            "bound": True,
            "mode": "new_message",
            "subject": SUBJECT,
            "sender": ADMIN,
            "graph_message_id": "",
        }

    monkeypatch.setattr(mod.msgraph_broker, "bind_reply", bind_reply)


def _new_message_verdict(kind: str = "litigation_job", job: str = JOB) -> str:
    return json.dumps(
        {
            "bound": True,
            "mode": "new_message",
            "binding": {"kind": kind, "job_id": job},
            "sender": ADMIN,
            "graph_message_id": "",
        }
    )


SEND_TOOLS = [
    "smd_send_message",
    "casework_brief",
    "mcp_msgraph_mail_send_message",
    "mcp_agentmail_send_message",
]


def test_a_litigation_request_wake_replies_through_a_litigation_binding(lane) -> None:
    """FALSIFIER: drop "litigation" from _JOB_KIND and the bind goes to the
    demand ledger, which has no such job."""
    mod, d1, broker, _ = lane
    _litigation_wake(mod)
    assert _bind(mod, {"job_id": JOB})["bound"] is True
    assert broker.binds == [{"kind": "litigation_job", "job_id": JOB}]
    _draft(mod, [ADMIN])
    assert broker.bound_sends == [
        {"binding": {"kind": "litigation_job", "job_id": JOB}, "session_id": WAKE}
    ]
    sent = [m for a, m in d1.events() if a == "REPLY_SENT"]
    assert len(sent) == 1 and sent[0]["in_reply_to"] == GRAPH_ID


@pytest.mark.parametrize("trigger", ["request", "scheduled"])
@pytest.mark.parametrize("tool", SEND_TOOLS)
def test_a_litigation_wake_may_call_no_send_tool(lane, trigger, tool) -> None:
    """FALSIFIER: drop "litigation" from the wake regex and the wake is no wake:
    every send passes. A scheduled wake is no exception."""
    mod, _d1, _broker, _ = lane
    _litigation_wake(mod, trigger=trigger)
    verdict = mod.on_pre_tool_call(
        tool_name=tool, args={"to": [ADMIN], "subject": SUBJECT}, session_id=WAKE
    )
    assert verdict is not None and verdict["action"] == "block"
    assert f"litigation job {JOB}'s completion wake" in verdict["message"]


def test_a_demand_binding_is_refused_in_a_litigation_wake(lane) -> None:
    mod, _d1, _broker, _ = lane
    _litigation_wake(mod)
    out = json.loads(mod.binding.record_from_result(WAKE, _verdict("demand_job")))
    assert out["bound"] is False
    assert out["reason"] == f"this wake is for litigation job {JOB}; it cannot answer another job"


def test_a_scheduled_wake_sends_one_new_message_through_the_binding(lane, monkeypatch) -> None:
    """The scheduled run's status email: bound by job id, the broker answers
    new_message with no email id, and the draft goes out through
    msgraph_reply_bound. FALSIFIER: require a graph id for every binding and
    the scheduled run can never deliver."""
    mod, d1, broker, _ = lane
    _new_message_broker(mod, monkeypatch, broker)
    _litigation_wake(mod, trigger="scheduled")
    out = _bind(mod, {"job_id": JOB})
    assert out["bound"] is True and out["mode"] == "new_message" and out["subject"] == SUBJECT
    assert broker.binds == [{"kind": "litigation_job", "job_id": JOB}]
    _draft(mod, [ADMIN], body="40 matters; 6 re-read this run; 2 new flags.")
    assert broker.bound_sends == [
        {"binding": {"kind": "litigation_job", "job_id": JOB}, "session_id": WAKE}
    ]
    assert len([m for a, m in d1.events() if a == "REPLY_SENT"]) == 1


def test_a_scheduled_wake_sends_only_once(lane, monkeypatch) -> None:
    mod, d1, broker, _ = lane
    _new_message_broker(mod, monkeypatch, broker)
    _litigation_wake(mod, trigger="scheduled")
    assert _bind(mod, {"job_id": JOB})["bound"] is True
    _draft(mod, [ADMIN], call="c1")
    _draft(mod, [ADMIN], call="c2")
    assert len(broker.bound_sends) == 1
    assert len([m for a, m in d1.events() if a == "REPLY_SENT"]) == 1


def test_two_scheduled_runs_each_send(lane, monkeypatch) -> None:
    """The once-only key is per job: FALSIFIER: key the new message on its
    empty email id and the second day's run is held as a duplicate."""
    mod, _d1, broker, _ = lane
    _new_message_broker(mod, monkeypatch, broker)
    _litigation_wake(mod, trigger="scheduled", session="day-1", job=JOB)
    _litigation_wake(mod, trigger="scheduled", session="day-2", job=OTHER_JOB)
    assert _bind(mod, {"job_id": JOB}, session="day-1")["bound"] is True
    _draft(mod, [ADMIN], session="day-1", call="c1")
    assert _bind(mod, {"job_id": OTHER_JOB}, session="day-2")["bound"] is True
    _draft(mod, [ADMIN], session="day-2", call="c2")
    assert len(broker.bound_sends) == 2


def test_a_scheduled_wake_draft_to_anyone_else_is_held(lane, monkeypatch) -> None:
    mod, d1, broker, _ = lane
    _new_message_broker(mod, monkeypatch, broker)
    _litigation_wake(mod, trigger="scheduled")
    assert _bind(mod, {"job_id": JOB})["bound"] is True
    _draft(mod, [OTHER])
    assert broker.bound_sends == []
    assert [m for a, m in d1.events() if a == "REPLY_SENT"] == []


def test_a_new_message_binding_is_refused_in_a_request_wake(lane) -> None:
    """The broker's mode must agree with the wake's trigger. FALSIFIER: drop
    the scheduled check and a request wake could mail out instead of replying."""
    mod, _d1, _broker, _ = lane
    _litigation_wake(mod, trigger="request")
    out = json.loads(mod.binding.record_from_result(WAKE, _new_message_verdict()))
    assert out["bound"] is False and "scheduled litigation job" in out["reason"]
    assert mod.binding.SESSION_BINDINGS.get(WAKE) is None


@pytest.mark.parametrize("kind", ["demand_job", "drafting_job", "medchron_job"])
def test_a_new_message_binding_is_refused_for_any_other_job(lane, kind) -> None:
    mod, _d1, _broker, _ = lane
    mod.on_pre_llm_call(
        session_id=WAKE,
        sender_id="webhook:handoff",
        user_message=(
            f"Run the document-drafter skill's DELIVER mode for drafting job {JOB}.\n"
            "Trigger: scheduled."
        ),
    )
    out = json.loads(mod.binding.record_from_result(WAKE, _new_message_verdict(kind)))
    assert out["bound"] is False
    assert mod.binding.SESSION_BINDINGS.get(WAKE) is None


def test_a_new_message_binding_is_refused_on_a_cron_turn(lane) -> None:
    mod, _d1, _broker, _ = lane
    out = json.loads(mod.binding.record_from_result(CRON, _new_message_verdict()))
    assert out["bound"] is False
    assert mod.binding.SESSION_BINDINGS.get(CRON) is None


def test_a_reply_binding_is_refused_in_a_scheduled_wake(lane) -> None:
    """No email asked for a scheduled run, so it answers none. FALSIFIER: drop
    the elif and a scheduled wake could answer whatever email the broker named."""
    mod, _d1, _broker, _ = lane
    _litigation_wake(mod, trigger="scheduled")
    out = json.loads(mod.binding.record_from_result(WAKE, _verdict("litigation_job")))
    assert out["bound"] is False and "scheduled wake" in out["reason"]
    assert mod.binding.SESSION_BINDINGS.get(WAKE) is None


def test_a_scheduled_wake_refuses_a_bind_by_email_id(lane) -> None:
    mod, _d1, _broker, _ = lane
    _litigation_wake(mod, trigger="scheduled")
    verdict = mod.on_pre_tool_call(
        tool_name="reply_bind", args={"internet_message_id": "<r@firm.example>"}, session_id=WAKE
    )
    assert verdict is not None and verdict["action"] == "block"


@pytest.mark.parametrize("trigger", ["request", "scheduled"])
def test_an_unknown_mode_binds_nothing(lane, trigger) -> None:
    """FALSIFIER: drop the mode check and a request wake takes a binding whose
    mode it does not understand as an ordinary reply."""
    mod, _d1, _broker, _ = lane
    _litigation_wake(mod, trigger=trigger)
    verdict = json.loads(_verdict("litigation_job"))
    verdict["mode"] = "forward"
    out = mod.binding.record_from_result(WAKE, json.dumps(verdict))
    assert out is not None and json.loads(out)["bound"] is False
    assert mod.binding.SESSION_BINDINGS.get(WAKE) is None


def test_the_scheduled_trigger_is_read_only_from_the_handoff_route(lane) -> None:
    mod, _d1, _broker, _ = lane
    _litigation_wake(mod, trigger="scheduled", sender="webhook:agentmail")
    assert mod.binding.TURN_SOURCES.scheduled(WAKE) is False


def test_the_scheduled_trigger_is_litigation_only(lane) -> None:
    mod, _d1, _broker, _ = lane
    mod.on_pre_llm_call(
        session_id=WAKE,
        sender_id="webhook:handoff",
        user_message=f"Run the DELIVER mode for drafting job {JOB}.\nTrigger: scheduled.",
    )
    assert mod.binding.TURN_SOURCES.scheduled(WAKE) is False
