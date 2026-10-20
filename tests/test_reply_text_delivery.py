"""A rostered sender's email answered only in text gets that text as its reply.

Live failure this pins (hermes-captain-mark, 2026-10-19 03:12Z, session
20261019_031221_502671f3, skill inbox-triage): a rostered partner replied to the
agent's hello, the model answered with a 474-character TEXT response and zero
tool calls, the webhook platform logged the text and sent nothing, and the
partner got silence. The same seat answered another rostered sender correctly
minutes earlier only because that turn called ``create_draft``.

FALSIFIER: on the parent commit the reply plugin's ``post_llm_call`` delivers an
owed act line and nothing else, so ``test_a_text_only_turn_from_a_rostered_
sender_is_delivered_once`` sends nothing (``sent == []``) and fails, as does
every other test here that expects a send. The plugin also has no
``on_post_llm_call``, so the hook-level tests fail with AttributeError.
"""

from __future__ import annotations

import pytest

from shared import answered_messages, inbound, tool_gate
from shared.pending_acts import PENDING_ACTS
from tests.conftest import load_plugin
from tests.test_couples_email import (  # noqa: F401 - fixture reuse
    P1,
    P2,
    _inbound,
    _turn_space,
    lane,
)
from tests.test_reply import _draft, _record_origin, relay_mod  # noqa: F401 - fixture reuse

SESSION = "s1"
SENDER = "greg@whitfield.example"
TEXT = (
    "Sure! To set this up properly, I need a few details:\n\n"
    "1. Which day of the week works best?\n"
    "2. What time should the reminder arrive?\n\n"
    "Once I have those I will set it up."
)


@pytest.fixture
def gate_calls():
    """The trust gate as published by hermes-smd-trust: here, one that allows
    and records what it was asked about."""
    calls: list[dict] = []

    def _gate(**kwargs):
        calls.append(kwargs)
        return None

    tool_gate.set_gate(_gate)
    yield calls
    tool_gate.set_gate(None)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    mod = load_plugin("hermes-smd-reply")
    monkeypatch.setattr(mod, "_TURN_SENDS", mod.text_delivery.TurnSends(), raising=False)
    monkeypatch.setattr(mod, "_TEXT_DELIVERED", mod.text_delivery.DeliveredOnce(), raising=False)
    answered_messages._reset_for_tests()
    PENDING_ACTS.clear()
    yield
    answered_messages._reset_for_tests()
    PENDING_ACTS.clear()


def _end_turn(mod, text=TEXT, session=SESSION):
    mod.on_post_llm_call(session_id=session, assistant_response=text, user_message="(prompt)")


def _rows(d1, action):
    return [m for a, m in d1.events() if a == action]


# --------------------------------------------------------------------- (a)


def test_registers_post_llm_call_as_the_text_delivery(fake_ctx, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(tmp_path / "absent.yaml"))
    mod = load_plugin("hermes-smd-reply")
    mod.register(fake_ctx)
    assert mod.on_post_llm_call in fake_ctx.registered["post_llm_call"]


def test_a_text_only_turn_from_a_rostered_sender_is_delivered_once(relay_mod, gate_calls):  # noqa: F811
    mod, d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    _end_turn(mod)
    assert len(sent) == 1
    # Threaded to the sender's own message: the broker reply verb names no one.
    assert sent[0]["message_id"] == "msg_in"
    # The model's words, with the one punctuation fix no model is left to make.
    assert sent[0]["text"] == TEXT.replace("Sure!", "Sure.")
    # It passed the trust gate as the draft it stands in for, to the sender only.
    assert len(gate_calls) == 1
    assert gate_calls[0]["tool_name"] == "mcp_agentmail_create_draft"
    assert gate_calls[0]["args"]["to"] == [SENDER]
    assert gate_calls[0]["session_id"] == SESSION
    row = _rows(d1, "REPLY_SENT")[-1]
    assert row["recipient"] == SENDER and row["in_reply_to"] == "msg_in"
    assert row["seat_delivered_text"] is True
    # Never twice: a second end-of-turn for the same email sends nothing more.
    _end_turn(mod)
    assert mod._deliver_text_reply(SESSION, TEXT) == "answered"
    assert len(sent) == 1


def test_the_session_end_hook_never_sends_the_text(relay_mod, gate_calls):  # noqa: F811
    """on_session_end carries no final text; only post_llm_call delivers it."""
    mod, _d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    mod.on_turn_end(session_id=SESSION)
    assert sent == []


# --------------------------------------------------------------------- (b)


def test_a_turn_that_drafted_its_reply_sends_no_second_email(relay_mod, gate_calls):  # noqa: F811
    mod, _d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    mod.on_pre_llm_call(session_id=SESSION, user_message="(prompt)")
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft([SENDER]),
        session_id=SESSION,
        tool_call_id="c1",
    )
    assert len(sent) == 1
    _end_turn(mod, text="I've replied to Greg with the intake confirmation.")
    assert len(sent) == 1
    assert gate_calls == []


def test_a_turn_whose_draft_was_held_does_not_also_send_its_text(relay_mod, gate_calls):  # noqa: F811
    """The draft's hold is told to the model in the turn; the text is not a
    second try around it."""
    mod, d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    mod.on_pre_llm_call(session_id=SESSION, user_message="(prompt)")
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["someone-else@example.org"]),
        session_id=SESSION,
        tool_call_id="c1",
    )
    assert sent == [] and _rows(d1, "REPLY_HELD")[-1]["reason"] == "recipient_mismatch"
    _end_turn(mod)
    assert sent == []


def test_an_email_answered_by_another_path_is_not_answered_again(relay_mod, gate_calls):  # noqa: F811
    """The space gate's confirmation reply, a code-sent notice: anything the
    broker transmitted anchored to this email."""
    mod, _d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    answered_messages.note_anchor({"kind": "agentmail_message", "message_id": "msg_in"})
    _end_turn(mod)
    assert sent == []


def test_a_send_attempt_in_the_turn_suppresses_the_text(relay_mod, gate_calls):  # noqa: F811
    mod, _d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    mod.on_pre_llm_call(session_id=SESSION, user_message="(prompt)")
    mod.on_pre_tool_call(tool_name="smd_send_message", args={"to": [SENDER]}, session_id=SESSION)
    _end_turn(mod)
    assert sent == []


def test_the_turn_send_mark_is_per_turn(relay_mod, gate_calls):  # noqa: F811
    """A draft in an earlier turn of the session does not silence a later one."""
    mod, _d1, sent = relay_mod
    mod._TURN_SENDS.note(SESSION)
    mod.on_pre_llm_call(session_id=SESSION, user_message="(prompt)")
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    _end_turn(mod)
    assert len(sent) == 1


def test_a_turn_with_an_owed_act_line_sends_the_line_not_the_text(relay_mod, gate_calls):  # noqa: F811
    mod, _d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    line = '[act 527116d6] Delete 1 event. Reply "yes, delete them" to proceed.'
    assert PENDING_ACTS.note_proposed(SESSION, "527116d6", "mcp_smokeball_delete_events", line)
    _end_turn(mod, text=f"I need your confirmation first.\n\n{line}")
    assert len(sent) == 1
    assert sent[0]["text"].startswith(mod.act_delivery.PREAMBLE)


# --------------------------------------------------------------------- (c)


@pytest.mark.parametrize("text", ["", "   \n", "[SILENT]", "[silent]", None, "Done.\n\n[SILENT]"])
def test_silence_or_nothing_sends_nothing(relay_mod, gate_calls, text):  # noqa: F811
    mod, d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    _end_turn(mod, text=text)
    assert sent == []
    assert gate_calls == []
    assert _rows(d1, "REPLY_HELD") == []


# --------------------------------------------------------------------- (d)


def test_a_non_rostered_sender_gets_nothing_and_no_rows(relay_mod, gate_calls):  # noqa: F811
    mod, d1, sent = relay_mod
    _record_origin(sender="stranger@example.org", message_id="msg_in", session=SESSION)
    assert mod._deliver_text_reply(SESSION, TEXT) == "not_rostered"
    _end_turn(mod)
    assert sent == []
    assert gate_calls == []
    assert d1.events() == []


def test_an_unauthenticated_sender_records_no_origin_and_gets_nothing(relay_mod, gate_calls):  # noqa: F811
    """The router records NO origin for an unauthenticated From (sender_auth),
    which is the quarantine an address on the roster cannot talk its way past."""
    mod, _d1, sent = relay_mod
    inbound.SESSION_INBOUND_ORIGIN.note_turn_prompt(SESSION, "msg_in")
    _end_turn(mod)
    assert sent == []


def test_a_turn_no_email_opened_gets_nothing(relay_mod, gate_calls):  # noqa: F811
    """A cron, a wake, Telegram: the session's sticky origin is an earlier
    email, and this turn's prompt named none."""
    mod, _d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    inbound.SESSION_INBOUND_ORIGIN.note_turn_prompt(SESSION, "")
    _end_turn(mod)
    assert sent == []


def test_a_graph_seat_is_not_changed(relay_mod, gate_calls):  # noqa: F811
    mod, _d1, sent = relay_mod
    mod._YAML_PATH.write_text(
        "customer_id: acme\nvertical: law-firm\n"
        "connectors:\n  Email:\n    adapter: msgraph\n    enabled: true\n"
        f"scope:\n  inbound_allow_from:\n    - {SENDER}\n"
    )
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    assert mod._deliver_text_reply(SESSION, TEXT) == "adapter"
    assert sent == []


# --------------------------------------------------------------------- (e)


def test_the_banned_marker_gate_still_holds_the_text(relay_mod, gate_calls):  # noqa: F811
    mod, d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    outcome = mod._deliver_text_reply(
        SESSION, "The client portal is coming soon; tell the team to hold questions."
    )
    assert outcome == "held"
    assert sent == []
    held = _rows(d1, "REPLY_HELD")[-1]
    assert held["reason"].startswith("fabrication")
    assert held["seat_delivered_text"] is True


def test_a_trust_gate_refusal_sends_nothing(relay_mod):  # noqa: F811
    mod, d1, sent = relay_mod
    tool_gate.set_gate(lambda **_kw: {"action": "block", "message": "Refused: paused"})
    try:
        _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
        assert mod._deliver_text_reply(SESSION, TEXT) == "blocked"
    finally:
        tool_gate.set_gate(None)
    assert sent == []
    held = _rows(d1, "REPLY_HELD")[-1]
    assert held["reason"] == "trust_gate_refused" and "Refused" not in str(held)


def test_no_trust_gate_wired_fails_closed(relay_mod):  # noqa: F811
    mod, _d1, sent = relay_mod
    tool_gate.set_gate(None)
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    assert mod._deliver_text_reply(SESSION, TEXT) == "blocked"
    assert sent == []


def test_the_trust_plugin_publishes_its_gate(fake_ctx, monkeypatch) -> None:
    trust = load_plugin("hermes-smd-trust")
    tool_gate.set_gate(None)
    try:
        trust.register(fake_ctx)
        with tool_gate._LOCK:
            assert tool_gate._GATE is trust.on_pre_tool_call
    finally:
        tool_gate.set_gate(None)


# ------------------------------------------------------------ couples seat


def test_a_couples_person_space_text_reply_goes_to_that_partner_only(lane, gate_calls):  # noqa: F811
    mod, _d1, agentmail, _y, mp = lane
    mp.setattr(mod, "_DECIDED", mod.relay.DecidedOnce(), raising=False)
    _turn_space(mp, "person:p1")
    _inbound(sender=P1, message_id="am-in-1", session="s1")
    mod.on_post_llm_call(session_id="s1", assistant_response=TEXT, user_message="(prompt)")
    assert len(agentmail) == 1
    assert agentmail[0]["message_id"] == "am-in-1"
    assert agentmail[0].get("shared", False) is False
    assert gate_calls[0]["args"]["to"] == [P1]


def test_a_couples_joint_thread_text_reply_is_shared(lane, gate_calls):  # noqa: F811
    """A joint thread is answered the way a drafted reply on it would be."""
    mod, _d1, agentmail, _y, mp = lane
    mp.setattr(mod, "_DECIDED", mod.relay.DecidedOnce(), raising=False)
    _turn_space(mp, "shared")
    _inbound(sender=P1, message_id="am-in-9", session="s9")
    mod.on_post_llm_call(session_id="s9", assistant_response=TEXT, user_message="(prompt)")
    assert len(agentmail) == 1
    assert agentmail[0]["shared"] is True and agentmail[0]["message_id"] == "am-in-9"


def test_the_scrub_changes_punctuation_only():
    from tests.conftest import load_plugin as _lp

    td = _lp("hermes-smd-reply").text_delivery
    assert (
        td.scrub("Sure! Friday works \u2014 see you then!") == "Sure. Friday works, see you then."
    )
    assert td.scrub("if a != b") == "if a != b"


def test_a_style_rule_the_scrub_cannot_fix_still_holds(relay_mod, gate_calls):  # noqa: F811
    mod, d1, sent = relay_mod
    _record_origin(sender=SENDER, message_id="msg_in", session=SESSION)
    assert mod._deliver_text_reply(SESSION, "Booked for 2026-10-23T19:00:00Z.") == "held"
    assert sent == []
    assert _rows(d1, "REPLY_HELD")[-1]["reason"] == "output_checklist"


# --------------------------------------------------------- broker ledger


def test_the_broker_client_notes_the_email_a_transmit_answered(monkeypatch):
    from shared import agentmail_broker

    monkeypatch.setenv(agentmail_broker.SOCKET_ENV, "/tmp/none.sock")
    monkeypatch.setattr(agentmail_broker, "request", lambda *_a, **_k: {"message_id": "out-1"})
    agentmail_broker.send_reply(
        "am-in-5",
        text="ok",
        anchor={"kind": "agentmail_message", "message_id": "am-in-5"},
    )
    assert answered_messages.answered("am-in-5")


def test_a_broker_refusal_answers_nothing(monkeypatch):
    from shared import agentmail_broker

    def _refuse(*_a, **_k):
        raise agentmail_broker.BrokerError("refused", code="AgentMailRefused")

    monkeypatch.setenv(agentmail_broker.SOCKET_ENV, "/tmp/none.sock")
    monkeypatch.setattr(agentmail_broker, "request", _refuse)
    with pytest.raises(agentmail_broker.BrokerError):
        agentmail_broker.send_reply(
            "am-in-6",
            text="ok",
            anchor={"kind": "agentmail_message", "message_id": "am-in-6"},
        )
    assert not answered_messages.answered("am-in-6")
