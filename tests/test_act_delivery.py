"""The seat delivers an act line the model proposed and did not send.

Live failure this pins (pilot-smokeball 2026-09-25, session
20260925_214749_dbc67acb): the gate withheld a delete and minted
``[act 527116d6]``; the model ended the turn with the line as final text and
sent nothing, so the administrator was never asked.

FALSIFIER: on the parent commit the reply plugin registers no end-of-turn hook
and has no ``on_turn_end``, so every delivery test below fails (AttributeError
or nothing sent).
"""

from __future__ import annotations

import pytest

from shared import inbound
from shared.pending_acts import PENDING_ACTS, ConfirmedAct
from tests.conftest import load_plugin
from tests.test_reply import _draft, _record_origin, relay_mod  # noqa: F401 - fixture reuse

SESSION = "s1"
PROPOSAL = "527116d6"
TOOL = "mcp_smokeball_delete_events"
LINE = (
    f"[act {PROPOSAL}] Delete 1 Smokeball calendar event: 1 on matter 2026-PI-102 "
    '(2026-11-03 "[SMD-PROBE] delete-act probe 2 EDITED"). Reply "yes, delete them" to proceed.'
)


@pytest.fixture(autouse=True)
def _clean(relay_mod):  # noqa: F811 - pytest fixture injection
    mod, _d1, _sent = relay_mod
    PENDING_ACTS.clear()
    mod._ACT_BODIES.clear()
    yield
    PENDING_ACTS.clear()
    mod._ACT_BODIES.clear()


def _propose():
    _record_origin(sender="greg@whitfield.example", message_id="msg_in", session=SESSION)
    assert PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, LINE)


def test_registers_the_end_of_turn_hooks(fake_ctx, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(tmp_path / "absent.yaml"))
    mod = load_plugin("hermes-smd-reply")
    mod.register(fake_ctx)
    assert "post_llm_call" in fake_ctx.registered
    assert "on_session_end" in fake_ctx.registered


def test_a_turn_that_sent_nothing_gets_the_line_delivered_by_the_seat(relay_mod):  # noqa: F811
    mod, d1, sent = relay_mod
    _propose()
    mod.on_turn_end(session_id=SESSION)
    assert len(sent) == 1
    assert sent[0]["message_id"] == "msg_in"  # threaded to the admin's own message
    assert LINE in sent[0]["text"]  # byte for byte
    assert PENDING_ACTS.proposed(SESSION) == []  # no longer owed
    rows = [m for a, m in d1.events() if a == "REPLY_SENT"]
    assert rows and rows[-1]["seat_delivered_act"] == PROPOSAL


def test_both_end_of_turn_hooks_deliver_it_once(relay_mod):  # noqa: F811
    mod, _d1, sent = relay_mod
    _propose()
    mod.on_turn_end(session_id=SESSION)  # post_llm_call
    mod.on_turn_end(session_id=SESSION)  # on_session_end
    assert len(sent) == 1


def test_a_reply_that_carried_the_line_is_not_repeated(relay_mod):  # noqa: F811
    mod, _d1, sent = relay_mod
    _propose()
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["greg@whitfield.example"], text=f"Holding this for you.\n\n{LINE}"),
        session_id=SESSION,
    )
    assert len(sent) == 1
    mod.on_turn_end(session_id=SESSION)
    assert len(sent) == 1


def test_a_reply_with_an_edited_line_does_not_count(relay_mod):  # noqa: F811
    """The live model restored neutralized brackets: a reply carrying an EDITED
    line is not the line the administrator must answer."""
    mod, _d1, sent = relay_mod
    _propose()
    edited = LINE.replace("[SMD-PROBE]", "(SMD-PROBE)")
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["greg@whitfield.example"], text=edited),
        session_id=SESSION,
    )
    mod.on_turn_end(session_id=SESSION)
    assert len(sent) == 2 and LINE in sent[1]["text"]


def test_a_failed_delivery_is_loud(relay_mod, monkeypatch):  # noqa: F811
    mod, d1, _sent = relay_mod
    _propose()

    def _boom(**_kw):
        raise mod.relay.RelaySendError("broker transmit unavailable")

    monkeypatch.setattr(mod.relay, "send_reply", _boom)
    assert mod._deliver_owed_act(SESSION) == "failed"
    failed = [m for a, m in d1.events() if a == "REPLY_FAILED"]
    assert failed and failed[-1]["reason"].startswith("act_line_undelivered")
    assert PENDING_ACTS.proposed(SESSION) == [LINE]  # still owed, still visible


def test_nothing_is_sent_when_no_act_is_owed(relay_mod):  # noqa: F811
    mod, _d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example", message_id="msg_in", session=SESSION)
    mod.on_turn_end(session_id=SESSION)
    assert sent == []


def test_a_confirmed_act_is_not_asked_again(relay_mod):  # noqa: F811
    mod, _d1, sent = relay_mod
    _propose()
    PENDING_ACTS.mark_confirmed(
        SESSION,
        ConfirmedAct(
            proposal_id=PROPOSAL,
            tool=TOOL,
            payload={"events": []},
            instructed_by="greg@whitfield.example",
            confirmed_by="greg@whitfield.example",
            confirmed_message_id="msg_yes",
            confirmed_at=1.0,
        ),
    )
    mod.on_turn_end(session_id=SESSION)
    assert sent == []


def test_no_recorded_origin_pages_instead_of_guessing(relay_mod):  # noqa: F811
    mod, d1, sent = relay_mod
    assert PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, LINE)
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    assert mod._deliver_owed_act(SESSION) == "failed"
    assert sent == []
    assert any(a == "REPLY_FAILED" for a, _ in d1.events())
