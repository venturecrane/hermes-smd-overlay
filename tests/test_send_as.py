"""Sending as a staff member, on that staff member's emailed approval (ss ADR 0089).

WHAT THIS FILE TRIES TO BREAK. The Operator may now prepare a letter that goes
out FROM a named staff member. The invariants:

* a ``from``-bearing send is never sent on the turn. It is PROPOSED: the gates
  run on the composing session, the broker stores the row and emails the draft
  to its one approver, and the tool call is blocked with a sentence naming them;
* the proposal is allowed on a tainted turn (a draft fires nothing), while an
  ordinary send on the same turn is still refused exactly as before;
* the fabrication, identifier and matter gates run at propose, and a failure
  proposes nothing;
* ``from`` must be on the authored roster and must be the person who asked
  (unless an Operator administrator asked); ``bcc``, ``reply_to`` and ``html``
  never reach the broker; the ceiling must be an authored ``confirm``;
* it never enters ``PENDING_SEND`` and never reaches the content floor;
* without ``from`` nothing changes;
* the approver's answer is read from their OWN text only, tag required, and a
  commitment act's lane is untouched.

The broker is mocked at the socket boundary (``msgraph_broker._verdict``) or at
the client function, never by reaching into the plugins.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from shared import act_broker, customer_config, matter_gate, msgraph_broker, rule_confirm
from shared.action_classes import ActionClass

# ``customer_config.CustomerConfig`` is patched by the autouse fixture below;
# this handle is the real class, for the accessor tests.
from shared.customer_config import CustomerConfig as _RealConfig
from shared.inbound import (
    SESSION_INBOUND_ORIGIN,
    SESSION_TAINT,
    TRUST_CLASS_UNKNOWN_EXTERNAL,
    InboundOrigin,
)
from shared.pending_acts import PENDING_ACTS
from shared.pending_send import PENDING_SEND
from tests.conftest import load_plugin

STAFF = "sarah@firm.example"
STAFF_NAME = "Sarah Reyes"
OTHER_STAFF = "dana@firm.example"
ADMIN = "christa@firm.example"
ADJUSTER = "adjuster@insurer.example"
SESSION = "sess-sendas"
ACT = "7f3a2c1d"
TAG = f"[draft {ACT}]"
GRAPH_ID = "AAMkAGraphId=="
INTERNET_ID = "<CAF00d@mail.example>"

BODY = (
    "Dear adjuster,\n\nPlease send the claim file and the signed authorization "
    "our attorney requested, along with any fee schedule that applies.\n\nThank you."
)


class _FakeConfig:
    def __init__(self) -> None:
        self.staff = [
            {"address": STAFF, "name": STAFF_NAME},
            {"address": OTHER_STAFF, "name": "Dana Park"},
        ]
        self.admins = [ADMIN]
        self.connectors: dict = {}
        self.inbound_roster = ["@firm.example"]
        self.outbound_roster: list = []
        self.rule_requests_to: list = []

    @property
    def staff_send_as(self) -> list[dict[str, str]]:
        return list(self.staff)

    def staff_send_as_entry(self, address: object) -> dict | None:
        if not isinstance(address, str):
            return None
        wanted = address.strip().lower()
        return next((e for e in self.staff if e["address"] == wanted), None)

    def sender_is_admin(self, sender: object) -> bool:
        return isinstance(sender, str) and sender.strip().lower() in self.admins

    def sender_on_roster(self, sender: object) -> bool:
        return isinstance(sender, str) and sender.strip().lower().endswith("@firm.example")

    def authored_person_name(self, address: object) -> str | None:
        return None

    @property
    def raw(self) -> dict:
        return {}


class _FakeCustomerConfig:
    instance = _FakeConfig()

    @classmethod
    def from_volume(cls, path=None):  # noqa: ANN001 — mirrors the real signature
        return cls.instance


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    PENDING_ACTS.clear()
    PENDING_SEND.clear()
    SESSION_TAINT._tainted.clear()
    SESSION_INBOUND_ORIGIN._origins.clear()
    _FakeCustomerConfig.instance = _FakeConfig()
    monkeypatch.setattr(customer_config, "CustomerConfig", _FakeCustomerConfig)
    yield
    PENDING_ACTS.clear()
    PENDING_SEND.clear()
    SESSION_TAINT._tainted.clear()
    SESSION_INBOUND_ORIGIN._origins.clear()


@pytest.fixture
def gate(monkeypatch):
    """The trust gate on a seat authoring ``external_send_as_staff: confirm``.

    ``external_send`` is authored ``autonomous`` so the no-``from`` comparisons
    below have something to compare against: an untainted plain send would go
    out, a tainted one is refused by the taint gate.
    """
    trust = load_plugin("hermes-smd-trust")
    enforce = trust.enforce
    exposure = {
        ActionClass.EXTERNAL_SEND_AS_STAFF: enforce.Ceiling.CONFIRM,
        ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS,
    }
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", lambda slug="": dict(exposure))
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: ["@firm.example"])
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: [])
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "operator")
    calls: list[dict] = []

    def fake_verdict(payload: dict, **_: object) -> dict:
        calls.append(payload)
        if payload.get("action") == msgraph_broker.ACTION_SEND_AS_PROPOSE:
            return {
                "ok": True,
                "tag": TAG,
                "act_id": ACT,
                "digest": "d" * 64,
                "expires_at": "2026-09-22T12:00:00Z",
                "notified": True,
            }
        return {"ok": False, "reason": "unexpected verb"}

    monkeypatch.setattr(msgraph_broker, "_verdict", fake_verdict)
    return trust, enforce, calls, exposure


def _email_turn(sender: str = STAFF, session: str = SESSION) -> None:
    SESSION_INBOUND_ORIGIN.record(
        session,
        InboundOrigin(
            sender_address=sender,
            message_id=GRAPH_ID,
            inbox_id="ops@firm.example",
            internet_message_id=INTERNET_ID,
            conversation_id="conv-1",
        ),
    )


def _send_args(**extra: object) -> dict:
    args: dict = {
        "to": [ADJUSTER],
        "subject": "Records request, claim 55",
        "text": BODY,
        "from": STAFF,
    }
    args.update(extra)
    return args


def _proposals(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c.get("action") == msgraph_broker.ACTION_SEND_AS_PROPOSE]


def _evaluate(enforce, args: dict, session: str = SESSION):
    return enforce.evaluate_tool_call("smd_send_message", args, "acme", session_id=session)


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------


def test_a_from_bearing_send_is_proposed_to_the_approver_and_blocked(gate):
    _trust, enforce, calls, _ = gate
    _email_turn()
    result = _evaluate(enforce, _send_args())
    assert result is not None and result["action"] == "block"
    assert STAFF_NAME in result["message"] and TAG in result["message"]
    assert "not sent" in result["message"]
    (proposal,) = _proposals(calls)
    assert proposal["instructed_by"] == STAFF
    assert proposal["session_id"] == SESSION
    assert proposal["payload"] == {
        "from": STAFF,
        "to": [ADJUSTER],
        "cc": [],
        "subject": "Records request, claim 55",
        "body_text": BODY,
    }
    assert proposal["gate_pass"]["fabrication"] is True
    assert proposal["gate_pass"]["matter"] is True
    assert proposal["gate_pass"]["identifier"] is True
    assert proposal["tainted"] is False


def test_a_tainted_turn_proposes_rather_than_refuses(gate):
    """The ordinary case: the Operator read outside material first."""
    _trust, enforce, calls, _ = gate
    _email_turn()
    SESSION_TAINT.mark(SESSION, TRUST_CLASS_UNKNOWN_EXTERNAL)
    result = _evaluate(enforce, _send_args())
    assert TAG in result["message"]
    (proposal,) = _proposals(calls)
    assert proposal["tainted"] is True
    assert "inbound:unknown_external" in proposal["sources"]


def test_the_same_tainted_turn_still_refuses_an_ordinary_send(gate):
    _trust, enforce, calls, _ = gate
    _email_turn()
    SESSION_TAINT.mark(SESSION, TRUST_CLASS_UNKNOWN_EXTERNAL)
    args = _send_args()
    args.pop("from")
    result = _evaluate(enforce, args)
    assert result is not None and "tainted turn" in result["message"]
    assert _proposals(calls) == []


PLAIN_BODY = "Hello,\n\nThank you for your note. We will be in touch.\n\nBest regards."


def test_without_from_the_send_is_unchanged(gate):
    """Untainted, autonomous, clean body: allowed exactly as before."""
    _trust, enforce, calls, _ = gate
    _email_turn()
    args = _send_args(text=PLAIN_BODY)
    args.pop("from")
    snapshot = dict(args)
    assert _evaluate(enforce, args) is None
    assert args == snapshot
    assert calls == []


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_from_is_an_ordinary_send(gate, blank):
    _trust, enforce, calls, _ = gate
    _email_turn()
    assert _evaluate(enforce, _send_args(text=PLAIN_BODY, **{"from": blank})) is None
    assert calls == []


def test_without_from_the_same_body_still_meets_the_content_floor(gate):
    """The contrast that makes the next test mean something: the floor is live
    on this seat, and only the from path is exempt from it."""
    _trust, enforce, calls, _ = gate
    _email_turn()
    args = _send_args()
    args.pop("from")
    result = _evaluate(enforce, args)
    assert result is not None and "content-sensitivity floor" in result["message"]
    assert calls == []


def test_content_floor_words_are_proposed_not_drafted(gate):
    """ "attorney", "signed authorization", "fee" trip the content floor on an
    autonomous send. The approval is the human review that floor forces, so
    the proposal is not downgraded."""
    _trust, enforce, calls, _ = gate
    _email_turn()
    result = _evaluate(enforce, _send_args())
    assert "content-sensitivity" not in result["message"]
    assert len(_proposals(calls)) == 1


def test_bcc_reply_to_and_html_never_reach_the_broker(gate):
    _trust, enforce, calls, _ = gate
    _email_turn()
    _evaluate(
        enforce,
        _send_args(
            bcc=["spy@evil.example"],
            reply_to="spy@evil.example",
            html="<p>other words</p>",
        ),
    )
    (proposal,) = _proposals(calls)
    flat = json.dumps(proposal)
    assert "spy@evil.example" not in flat
    assert "other words" not in flat
    assert set(proposal["payload"]) == {"from", "to", "cc", "subject", "body_text"}


def test_a_from_bearing_send_never_enters_pending_send(gate):
    _trust, enforce, _calls, _ = gate
    _email_turn()
    _evaluate(enforce, _send_args())
    assert PENDING_SEND.peek() is None


def test_from_not_on_the_roster_is_refused_and_proposes_nothing(gate):
    _trust, enforce, calls, _ = gate
    _email_turn(sender="stranger@firm.example")
    result = _evaluate(enforce, _send_args(**{"from": "stranger@firm.example"}))
    assert "not someone this engagement authorizes" in result["message"]
    assert _proposals(calls) == []


def test_a_colleague_cannot_ask_to_send_as_someone_else(gate):
    _trust, enforce, calls, _ = gate
    _email_turn(sender=OTHER_STAFF)
    result = _evaluate(enforce, _send_args())
    assert "only sarah@firm.example can ask" in result["message"]
    assert _proposals(calls) == []


def test_an_administrator_may_ask_on_a_staff_members_behalf(gate):
    _trust, enforce, calls, _ = gate
    _email_turn(sender=ADMIN)
    result = _evaluate(enforce, _send_args())
    assert TAG in result["message"]
    (proposal,) = _proposals(calls)
    assert proposal["instructed_by"] == ADMIN
    assert proposal["payload"]["from"] == STAFF


def test_a_turn_with_no_email_origin_proposes_nothing(gate):
    _trust, enforce, calls, _ = gate
    result = _evaluate(enforce, _send_args())
    assert "not opened by a verified email" in result["message"]
    assert _proposals(calls) == []


@pytest.mark.parametrize(
    "exposure_value",
    [None, "autonomous", "draft_for_review", "refused"],
)
def test_anything_but_an_authored_confirm_refuses(gate, monkeypatch, exposure_value):
    _trust, enforce, calls, exposure = gate
    if exposure_value is None:
        exposure.pop(ActionClass.EXTERNAL_SEND_AS_STAFF)
    else:
        exposure[ActionClass.EXTERNAL_SEND_AS_STAFF] = enforce.Ceiling(exposure_value)
    _email_turn()
    result = _evaluate(enforce, _send_args())
    assert result is not None and result["message"].startswith("Refused:")
    assert "external_send_as_staff refused" in result["message"]
    assert _proposals(calls) == []


def test_a_failing_fabrication_gate_proposes_nothing(gate):
    _trust, enforce, calls, _ = gate
    _email_turn()
    result = _evaluate(enforce, _send_args(text="Our new portal is coming soon."))
    assert result is not None and result["action"] == "block"
    assert _proposals(calls) == []


def test_a_failing_matter_gate_proposes_nothing(gate, monkeypatch):
    _trust, enforce, calls, _ = gate
    _email_turn()
    monkeypatch.setattr(matter_gate, "multi_matter_session", lambda session: ("m-1", "m-2"))
    monkeypatch.setattr(matter_gate, "multi_matter_mode", lambda: "block")
    result = _evaluate(enforce, _send_args())
    assert "matter mixing" in result["message"]
    assert _proposals(calls) == []


def test_a_broker_refusal_is_relayed_and_nothing_is_claimed(gate, monkeypatch):
    _trust, enforce, _calls, _ = gate
    _email_turn()
    monkeypatch.setattr(
        msgraph_broker,
        "_verdict",
        lambda payload, **_: {"ok": False, "reason": "staff member is not on the roster"},
    )
    result = _evaluate(enforce, _send_args())
    assert "staff member is not on the roster" in result["message"]
    assert "Held for approval" not in result["message"]


def test_an_unreachable_broker_proposes_nothing(gate, monkeypatch):
    _trust, enforce, _calls, _ = gate
    _email_turn()

    def boom(payload, **_):
        raise msgraph_broker.MsGraphBrokerUnavailable("down")

    monkeypatch.setattr(msgraph_broker, "_verdict", boom)
    result = _evaluate(enforce, _send_args())
    assert "could not record the draft" in result["message"]


def test_the_hook_blocks_before_any_later_gate_or_the_tool(gate):
    """Through ``on_pre_tool_call``: the block is final, the html attach and the
    workspace grant never run on a from-bearing send."""
    trust, _enforce, calls, _ = gate
    _email_turn()
    args = _send_args()
    result = trust.on_pre_tool_call(
        tool_name="smd_send_message", args=args, session_id=SESSION, tool_call_id="c1"
    )
    assert result is not None and TAG in result["message"]
    assert "html" not in args
    assert len(_proposals(calls)) == 1


def test_the_send_handler_never_transmits_a_from(gate, monkeypatch):
    trust, _enforce, _calls, _ = gate
    monkeypatch.setattr(
        trust.outbound_send,
        "send_via_msgraph",
        lambda *a, **k: pytest.fail("a from-bearing send reached a transport"),
    )
    monkeypatch.setattr(
        trust.outbound_send,
        "send_message",
        lambda *a, **k: pytest.fail("a from-bearing send reached a transport"),
    )
    out = trust._smd_send_message(_send_args())
    assert out.startswith("Not sent")


def test_the_tool_schema_advertises_an_optional_from():
    trust = load_plugin("hermes-smd-trust")
    schema = trust._SEND_TOOL_SCHEMA
    assert "from" in schema["properties"]
    assert "from" not in schema["required"]


# ---------------------------------------------------------------------------
# The broker client (framing at the real socket boundary)
# ---------------------------------------------------------------------------


def _serve_once(path: str, answer: dict, seen: list) -> threading.Thread:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)

    def run() -> None:
        conn, _ = server.accept()
        with conn:
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = conn.recv(65_536)
                if not chunk:
                    break
                raw += chunk
            seen.append(json.loads(raw))
            conn.sendall(json.dumps(answer).encode() + b"\n")
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_the_three_verbs_frame_the_contract_over_the_socket(monkeypatch):
    workdir = tempfile.mkdtemp(prefix="sa", dir="/tmp")
    path = os.path.join(workdir, "b.sock")
    monkeypatch.setenv(msgraph_broker.SOCKET_ENV, path)
    try:
        seen: list = []
        t = _serve_once(path, {"status": "DISPATCHED", "reason": ""}, seen)
        out = msgraph_broker.send_as_decide(
            tag_or_act_id=ACT,
            decision="send",
            decided_by=STAFF,
            internet_message_id=INTERNET_ID,
            instruction=None,
            graph_message_id=GRAPH_ID,
        )
        t.join(2)
        assert out["status"] == "DISPATCHED"
        assert seen[0] == {
            "action": "send_as_decide",
            "tag_or_act_id": ACT,
            "decision": "send",
            "decided_by": STAFF,
            "internet_message_id": INTERNET_ID,
            "instruction": None,
            "graph_message_id": GRAPH_ID,
        }
        os.unlink(path)

        seen.clear()
        t = _serve_once(path, {"matched": True, "tag": TAG}, seen)
        out = msgraph_broker.send_as_match_reply(
            conversation_id="conv-1", internet_message_id=INTERNET_ID, from_addr=ADJUSTER
        )
        t.join(2)
        assert out == {"matched": True, "tag": TAG}
        assert seen[0] == {
            "action": "send_as_match_reply",
            "conversation_id": "conv-1",
            "internet_message_id": INTERNET_ID,
            "from": ADJUSTER,
        }
    finally:
        if os.path.exists(path):
            os.unlink(path)
        os.rmdir(workdir)


def test_an_unknown_decision_never_reaches_the_socket(monkeypatch):
    monkeypatch.setattr(
        msgraph_broker, "_verdict", lambda *a, **k: pytest.fail("reached the socket")
    )
    with pytest.raises(ValueError):
        msgraph_broker.send_as_decide(
            tag_or_act_id=ACT, decision="yes", decided_by=STAFF, internet_message_id=""
        )


def test_no_socket_is_unavailable_not_refused(monkeypatch):
    monkeypatch.delenv(msgraph_broker.SOCKET_ENV, raising=False)
    with pytest.raises(msgraph_broker.MsGraphBrokerUnavailable):
        msgraph_broker.send_as_match_reply(
            conversation_id="c", internet_message_id="", from_addr=ADJUSTER
        )


# ---------------------------------------------------------------------------
# The approver's answer: grammar
# ---------------------------------------------------------------------------

APPROVAL_EMAIL_QUOTE = f"""On Mon, 21 Sep 2026 at 09:12, Operator <ops@firm.example> wrote:
> Reply "{TAG} send" to send it, "{TAG} change: ..." to revise it,
> or "{TAG} cancel" to drop it.
"""


@pytest.mark.parametrize(
    ("message", "decision", "instruction"),
    [
        (f"{TAG} send\n\n{APPROVAL_EMAIL_QUOTE}", "send", ""),
        (f"{TAG} cancel\n", "cancel", ""),
        (f"Hi\n{TAG} change: add the date of loss\n", "change", "add the date of loss"),
        (f"[draft {ACT.upper()}] Send", "send", ""),
    ],
)
def test_the_three_answers_parse(message, decision, instruction):
    command = rule_confirm.read_send_as_command(message)
    assert command is not None
    assert command.draft_id == ACT
    assert command.decision == decision
    assert command.instruction == instruction


def test_a_quoted_tag_is_not_an_answer():
    """The approval email carries all three forms verbatim; its quoted copy must
    never approve itself."""
    assert rule_confirm.read_send_as_command(f"thanks!\n\n{APPROVAL_EMAIL_QUOTE}") is None
    assert rule_confirm.read_send_as_command(f"> {TAG} send") is None


@pytest.mark.parametrize("message", ["send", "send it", "yes please send"])
def test_plain_words_with_no_draft_in_the_turn_are_not_a_send_as_answer(message):
    # Plain words are an answer (2026-09-22), but only when the turn names a
    # draft. With no tag anywhere this is not the send-as lane at all, and
    # claiming it would swallow the bare "yes" the rule and act lanes live on.
    assert rule_confirm.read_send_as_command(message) is None


def test_two_drafts_in_the_thread_ask_which():
    other = "[draft 1a2b3c4d]"
    command = rule_confirm.read_send_as_command(f"send it\n\n{APPROVAL_EMAIL_QUOTE}\n{other}")
    assert command is not None and command.decision == rule_confirm.SEND_AS_AMBIGUOUS


def test_a_tag_after_the_verb_is_not_an_answer():
    # "send [draft X]" is prose about a draft, not the command form, and it is
    # not one of the bare phrases either.
    assert rule_confirm.read_send_as_command(f"send {TAG}") is None


@pytest.mark.parametrize(
    "words", ["send", "send it", "approved", "ok", "go ahead", "Looks good.", "YES"]
)
def test_plain_words_on_the_approval_thread_send(words):
    # What a staff member actually types. The words are their own; the draft
    # comes from the tag the approval email put in the subject and the quote.
    command = rule_confirm.read_send_as_command(f"{words}\n\n{APPROVAL_EMAIL_QUOTE}")
    assert command is not None
    assert (command.draft_id, command.decision) == (ACT, rule_confirm.SEND_AS_SEND)


@pytest.mark.parametrize("words", ["cancel", "don't send", "hold off"])
def test_plain_words_can_also_stop_it(words):
    command = rule_confirm.read_send_as_command(f"{words}\n\n{APPROVAL_EMAIL_QUOTE}")
    assert command is not None
    assert (command.draft_id, command.decision) == (ACT, rule_confirm.SEND_AS_CANCEL)


def test_a_plain_change_carries_the_instruction():
    command = rule_confirm.read_send_as_command(
        f"change: add the date of loss\n\n{APPROVAL_EMAIL_QUOTE}"
    )
    assert command is not None
    assert (command.draft_id, command.decision) == (ACT, rule_confirm.SEND_AS_CHANGE)
    assert command.instruction == "add the date of loss"


def test_a_qualified_yes_is_not_a_send():
    # THE case the whole-text rule exists for: a reply that agrees and then asks
    # for an edit must not send the unedited letter.
    assert (
        rule_confirm.read_send_as_command(f"ok but move the date\n\n{APPROVAL_EMAIL_QUOTE}") is None
    )


def test_two_different_answers_are_ambiguous():
    command = rule_confirm.read_send_as_command(f"{TAG} send\n{TAG} cancel")
    assert command is not None and command.decision == rule_confirm.SEND_AS_AMBIGUOUS


def test_the_rule_and_act_matcher_never_sees_a_draft_tag():
    """The word is the separation: find_tags reads rule/act only, so a draft tag
    names nothing to the rule/act matcher, and an [act X] line is not a draft
    command."""
    assert rule_confirm.find_tags(f"yes\n\n> {TAG}") == ()
    assert rule_confirm.read_send_as_command(f"[act {ACT}] send") is None


# ---------------------------------------------------------------------------
# The approver's answer: the establishment lane
# ---------------------------------------------------------------------------


@pytest.fixture
def establishment(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-establishment")
    state: dict = {"pending": [], "requests": [], "decisions": [], "answer": {}}

    def fake_broker_request(payload):
        state["requests"].append(payload)
        if payload.get("action") == mod.TOOL_PENDING:
            return {"ok": True, "pending": list(state["pending"])}
        return {"ok": True}

    def fake_decide(**kwargs):
        state["decisions"].append(kwargs)
        return dict(state["answer"])

    monkeypatch.setattr(mod, "_broker_request", fake_broker_request)
    monkeypatch.setattr(msgraph_broker, "send_as_decide", fake_decide)
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", str(tmp_path / "possession.db"))
    monkeypatch.setattr(mod, "CustomerConfig", _FakeCustomerConfig)
    mod._ADMIN_STASH.clear()
    mod._CONFIRMED_STASH.clear()
    mod._READBACK_OWED.clear()
    return mod, state


def _turn(mod, sender: str, message: str, session: str = SESSION) -> str:
    out = mod.on_pre_llm_call(session_id=session, sender_id=sender, user_message=message)
    return (out or {}).get("context", "")


def test_send_carries_the_verified_sender_and_message_ids(establishment):
    mod, state = establishment
    state["answer"] = {"status": "DISPATCHED", "reason": ""}
    _email_turn()
    context = _turn(mod, STAFF, f"{TAG} send\n\n{APPROVAL_EMAIL_QUOTE}")
    (decision,) = state["decisions"]
    assert decision == {
        "tag_or_act_id": ACT,
        "decision": "send",
        "decided_by": STAFF,
        "internet_message_id": INTERNET_ID,
        "instruction": None,
        "graph_message_id": GRAPH_ID,
    }
    assert "has been sent from their address" in context
    assert "Do not send it again" in context


def test_change_injects_the_approvers_words_and_asks_for_a_redraft(establishment):
    mod, state = establishment
    state["answer"] = {"status": "REVISED", "reason": "", "instruction": None}
    _email_turn()
    context = _turn(mod, STAFF, f"{TAG} change: add the date of loss, 3 March\n")
    (decision,) = state["decisions"]
    assert decision["decision"] == "change"
    assert decision["instruction"] == "add the date of loss, 3 March"
    assert "add the date of loss, 3 March" in context
    assert f"from set to {STAFF}" in context
    assert "Nothing was sent" in context


def test_a_revision_carries_the_approved_text_and_the_approvers_words_as_provenance(
    establishment, monkeypatch
):
    # smd-staging, 2026-09-22: the first draft passed the identifier gate, and its
    # revision was refused for the claim number the approver was reading in the
    # email he answered, because the gate verifies against THIS session's reads
    # and a reply is a later session. The approved payload and the approver's own
    # instruction are seeded so the redraft can be composed at all.
    mod, state = establishment
    from shared import provenance

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(provenance, "record_read", lambda sid, text: seen.append((sid, text)))
    state["answer"] = {
        "status": "REVISED",
        "reason": "",
        "instruction": None,
        "prior_subject": "Records request: claim TC-1001",
        "prior_text": "Please send the complete claim file for claim TC-1001.",
    }
    _email_turn()
    _turn(mod, STAFF, f"{TAG} change: add that the date of loss was 1 September 2026\n")
    assert len(seen) == 1
    session, text = seen[0]
    assert session == SESSION
    assert "TC-1001" in text  # the value the gate refused
    assert "1 September 2026" in text  # the value the approver supplied
    assert "Records request" in text


def test_a_send_seeds_no_provenance(establishment, monkeypatch):
    # The falsifier: seeding is scoped to a revision. A send composes nothing, so
    # widening the gate there would buy nothing and hide a fabrication.
    mod, state = establishment
    from shared import provenance

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(provenance, "record_read", lambda sid, text: seen.append((sid, text)))
    state["answer"] = {"status": "DISPATCHED", "reason": "", "prior_text": "anything"}
    _email_turn()
    _turn(mod, STAFF, f"{TAG} send\n\n{APPROVAL_EMAIL_QUOTE}")
    assert seen == []


def test_change_with_nothing_to_change_asks_and_decides_nothing(establishment):
    mod, state = establishment
    _email_turn()
    context = _turn(mod, STAFF, f"{TAG} change\n")
    assert state["decisions"] == []
    assert "without saying what to change" in context


def test_cancel_is_carried_and_acknowledged(establishment):
    mod, state = establishment
    state["answer"] = {"status": "CANCELLED", "reason": ""}
    _email_turn(sender=ADMIN)
    context = _turn(mod, ADMIN, f"{TAG} cancel")
    assert state["decisions"][0]["decided_by"] == ADMIN
    assert "cancelled" in context and "Nothing was sent" in context


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        ("EXPIRED", "had expired"),
        ("SUPERSEDED", "replaced by a newer draft (9abc0123)"),
        ("REFUSED", "only the named approver may send"),
        ("FAILED", "the send failed and nothing went out"),
        ("SOMETHING_NEW", "was refused and nothing was sent"),
    ],
)
def test_every_terminal_status_says_plainly_what_happened(establishment, status, needle):
    mod, state = establishment
    state["answer"] = {
        "status": status,
        "reason": "only the named approver may send",
        "replaced_by": "9abc0123",
    }
    _email_turn()
    context = _turn(mod, STAFF, f"{TAG} send")
    assert needle in context


def test_an_unreachable_broker_is_reported_never_claimed_as_sent(establishment, monkeypatch):
    mod, _state = establishment

    def boom(**_):
        raise msgraph_broker.MsGraphBrokerUnavailable("down")

    monkeypatch.setattr(msgraph_broker, "send_as_decide", boom)
    _email_turn()
    context = _turn(mod, STAFF, f"{TAG} send")
    assert "could not reach the broker" in context
    assert "has been sent" not in context


def test_an_answer_not_on_a_verified_email_decides_nothing(establishment):
    mod, state = establishment
    context = _turn(mod, STAFF, f"{TAG} send")  # no inbound origin recorded
    assert state["decisions"] == []
    assert "did not arrive as a verified email" in context


def test_a_plain_yes_on_the_approval_thread_decides_the_draft(establishment):
    # Reversed 2026-09-22: a staff member should not have to type an
    # eight-character code to send their own letter. The affirmative is still
    # read from their own words only; the quoted half supplies the draft id.
    mod, state = establishment
    state["answer"] = {"status": "DISPATCHED", "reason": ""}
    _email_turn()
    _turn(mod, STAFF, f"yes\n\n{APPROVAL_EMAIL_QUOTE}")
    (decision,) = state["decisions"]
    assert (decision["tag_or_act_id"], decision["decision"]) == (ACT, "send")


def test_a_quoted_approval_email_alone_decides_nothing(establishment):
    # The self-approval guard, unchanged: our own email quoted back carries all
    # three forms verbatim, and with no words of the person's own it approves
    # nothing.
    mod, state = establishment
    _email_turn()
    _turn(mod, STAFF, f"thanks!\n\n{APPROVAL_EMAIL_QUOTE}")
    assert state["decisions"] == []


def _commitment_row() -> dict:
    return {
        "proposal_id": ACT,
        "kind": act_broker.KIND_TOOL_CALL,
        "tool": "mcp_smokeball_create_matter",
        "for_admin": True,
        "instructed_by": ADMIN,
        "readback": f"[act {ACT}] Create Smokeball matter",
        "payload": {
            "description": "Operator Library",
            "matter_type_id": "type-1",
            "client_contact_id": "contact-1",
        },
    }


@pytest.mark.parametrize("line", [f"[act {ACT}] cancel", f"[act {ACT}] send"])
def test_an_act_tag_never_reaches_the_send_as_verb(establishment, line):
    """ "[act X] cancel" / "[act X] send" on a COMMITMENT act stay on the act lane
    exactly as before; only [draft X] reaches send_as_decide."""
    mod, state = establishment
    state["pending"] = [_commitment_row()]
    _email_turn(sender=ADMIN)
    _turn(mod, ADMIN, line)
    assert state["decisions"] == []


def test_the_admin_act_lane_still_confirms_on_yes_create_it(establishment):
    mod, state = establishment
    state["pending"] = [_commitment_row()]
    PENDING_ACTS.note_proposed(
        SESSION, ACT, "mcp_smokeball_create_matter", f"[act {ACT}] Create Smokeball matter"
    )
    _email_turn(sender=ADMIN)
    context = _turn(mod, ADMIN, f"yes, create it\n\n> [act {ACT}] Create Smokeball matter")
    assert f"The administrator confirmed [act {ACT}]" in context
    assert state["decisions"] == []


# ---------------------------------------------------------------------------
# Reply tracking (webhook router)
# ---------------------------------------------------------------------------


@pytest.fixture
def router(monkeypatch):
    mod = load_plugin("hermes-smd-webhook-router")
    monkeypatch.setattr(mod, "CustomerConfig", _FakeCustomerConfig)

    class _SyncThread:
        def __init__(self, target, **_):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(mod.threading, "Thread", _SyncThread)
    return mod


def _outside_origin(conversation_id: str = "conv-1") -> InboundOrigin:
    return InboundOrigin(
        sender_address=ADJUSTER,
        message_id=GRAPH_ID,
        internet_message_id=INTERNET_ID,
        conversation_id=conversation_id,
    )


def test_an_inbound_on_a_conversation_asks_the_broker_to_match(router, monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        msgraph_broker,
        "send_as_match_reply",
        lambda **kw: seen.append(kw) or {"matched": True, "tag": TAG},
    )
    router._match_send_as_reply(_outside_origin())
    assert seen == [
        {"conversation_id": "conv-1", "internet_message_id": INTERNET_ID, "from_addr": ADJUSTER}
    ]


def test_a_broker_fault_never_reaches_ingest(router, monkeypatch):
    def boom(**_):
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(msgraph_broker, "send_as_match_reply", boom)
    router._match_send_as_reply(_outside_origin())  # must not raise


def test_a_seat_with_no_roster_pays_no_round_trip(router, monkeypatch):
    _FakeCustomerConfig.instance.staff = []
    monkeypatch.setattr(
        msgraph_broker, "send_as_match_reply", lambda **_: pytest.fail("round trip made")
    )
    router._match_send_as_reply(_outside_origin())
    router._match_send_as_reply(_outside_origin(conversation_id=""))


def test_the_origin_carries_the_graph_ids_from_the_dto():
    mod = load_plugin("hermes-smd-webhook-router")
    from shared import inbound_message

    dto = inbound_message.normalize_inbound(
        "msgraph",
        {
            "provider": "msgraph",
            "mailbox": "ops@firm.example",
            "message_id": GRAPH_ID,
            "thread_ref": "conv-9",
            "from_addr": ADJUSTER,
            "provider_refs": {
                "graph_message_id": GRAPH_ID,
                "conversation_id": "conv-9",
                "internet_message_id": INTERNET_ID,
            },
        },
    )
    origin = mod._origin_from_dto(dto, content="x")
    assert origin.internet_message_id == INTERNET_ID
    assert origin.conversation_id == "conv-9"


def test_graph_normalization_adds_the_internet_id_only_when_present():
    from shared import msgraph_client

    with_id = msgraph_client.normalize_message(
        {"id": "g1", "conversationId": "c1", "internetMessageId": INTERNET_ID}, mailbox="m"
    )
    assert with_id["provider_refs"]["internet_message_id"] == INTERNET_ID
    without = msgraph_client.normalize_message({"id": "g1", "conversationId": "c1"}, mailbox="m")
    assert without["provider_refs"] == {"graph_message_id": "g1", "conversation_id": "c1"}


# ---------------------------------------------------------------------------
# Config: the accessor and the on-box validator
# ---------------------------------------------------------------------------


def test_the_accessor_normalizes_and_drops_malformed_entries():
    cfg = _RealConfig(
        {
            "scope": {
                "staff_send_as": [
                    {"address": " Sarah@Firm.example ", "name": " Sarah Reyes "},
                    {"address": "sarah@firm.example", "name": "Duplicate"},
                    {"address": "@firm.example", "name": "Domain"},
                    {"address": "nobody@firm.example"},
                    "not-a-mapping",
                ]
            }
        }
    )
    assert cfg.staff_send_as == [{"address": STAFF, "name": STAFF_NAME}]
    assert cfg.staff_send_as_entry("SARAH@firm.example") == {"address": STAFF, "name": STAFF_NAME}
    assert cfg.staff_send_as_entry("nobody@firm.example") is None
    assert _RealConfig({"scope": {"staff_send_as": "x"}}).staff_send_as == []
    assert _RealConfig({}).staff_send_as == []


def test_the_ceiling_accessor_reads_the_authored_value_raw():
    cfg = _RealConfig(
        {
            "personas": [
                {
                    "slug": "operator",
                    "entitlements": {"exposure": {"external_send_as_staff": "confirm"}},
                },
                {"slug": "other"},
            ]
        }
    )
    assert cfg.external_send_as_staff_ceiling("operator") == "confirm"
    assert cfg.external_send_as_staff_ceiling("other") is None
    assert cfg.external_send_as_staff_ceiling("missing") is None


def _fixture_yaml(name: str) -> str:
    manifest = json.loads(
        (Path(__file__).parent / "contract" / "validator_parity_fixtures.json").read_text()
    )
    return next(f for f in manifest["fixtures"] if f["name"] == name)["yaml"]


def _validate_yaml(tmp_path: Path, text: str) -> list[str]:
    from bootstrap.validate import validate_customer_yaml

    path = tmp_path / "customer.yaml"
    path.write_text(text)
    return validate_customer_yaml(path)


_ROSTER = "  staff_send_as:\n    - address: paralegal@firm.com\n      name: Pat Lee\n"


def test_the_validator_accepts_the_authored_roster_and_confirm(tmp_path):
    assert _validate_yaml(tmp_path, _fixture_yaml("staff_send_as_accepted")) == []


@pytest.mark.parametrize(
    ("fixture", "needle"),
    [
        ("external_send_as_staff_autonomous_rejected", "one posture, 'confirm'"),
        ("external_send_as_staff_in_ceiling_rejected", "has no exposure_ceiling"),
        ("staff_send_as_domain_grant_rejected", "exact person address"),
        ("staff_send_as_unreachable_rejected", "not covered by scope.inbound_allow_from"),
    ],
)
def test_the_validator_rejects_each_contract_case(tmp_path, fixture, needle):
    errors = _validate_yaml(tmp_path, _fixture_yaml(fixture))
    assert any(needle in e for e in errors), errors


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        (
            "external_send_as_staff: confirm",
            "external_send_as_staff: draft_for_review",
            "one posture, 'confirm'",
        ),
        (_ROSTER, "  staff_send_as:\n    - address: paralegal@firm.com\n", "staff_send_as[0].name"),
        (
            _ROSTER,
            _ROSTER + "    - address: Paralegal@Firm.com\n      name: Pat Lee\n",
            "duplicate staff_send_as address",
        ),
    ],
)
def test_the_validator_rejects_the_other_malformed_shapes(tmp_path, old, new, needle):
    base = _fixture_yaml("staff_send_as_accepted")
    assert base.count(old) == 1
    errors = _validate_yaml(tmp_path, base.replace(old, new))
    assert any(needle in e for e in errors), errors


def test_an_exact_admin_address_makes_a_staff_member_reachable(tmp_path):
    base = _fixture_yaml("staff_send_as_unreachable_rejected")
    assert _validate_yaml(tmp_path, base) != []
    fixed = base.replace("scope:\n", "scope:\n  admins:\n    - paralegal@firm.com\n", 1)
    assert not any("not covered" in e for e in _validate_yaml(tmp_path, fixed))


def test_translation_carries_the_new_exposure_key():
    from bootstrap import translate

    block = translate._entitlements_block({"exposure": {"external_send_as_staff": "confirm"}})
    assert block == {"exposure": {"external_send_as_staff": "confirm"}}
