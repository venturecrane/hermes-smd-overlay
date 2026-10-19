"""Two rostered emails whose turns end together both get their reply.

Same defect class as the 2026-10-19 hermes-captain-mark routine drop
(``tests/test_space_delivery_concurrency.py``): pinned Hermes keys a bounded
hook callback's "running" slot on ``(hook_name, id(callback))`` for the whole
process, so while the reply plugin's ``post_llm_call`` was still sending one
turn's text reply through the broker, the next turn's invocation of the SAME
callback was skipped and that person got silence.

Falsifiers:

* two concurrent email turns that both end in text: both replies land and
  Hermes skips nothing (fails on the pre-fix plugin, which sent from inside
  ``post_llm_call``: one reply, one skip);
* a turn whose ``post_llm_call`` was skipped is ERROR + paged at
  ``on_session_end`` (fails on the pre-fix plugin: silence);
* the worker sends with the turn's state as the hook saw it.
"""

from __future__ import annotations

import logging
import threading

import pytest

from shared import answered_messages, hook_worker, tool_gate
from shared.pending_acts import PENDING_ACTS
from tests.test_reply import _clear_origin, _record_origin, relay_mod  # noqa: F401
from tests.test_space_delivery_concurrency import _PinnedHermesHooks

ANA, BEN = "ana@example.com", "ben@example.com"
TEXT = "Thanks, Friday at 7 works. I have put it on the calendar."

_TWO_ROSTERED = (
    "customer_id: acme\n"
    "vertical: law-firm\n"
    "connectors:\n"
    "  Email:\n"
    "    adapter: agentmail\n"
    "    enabled: true\n"
    "scope:\n"
    "  inbound_allow_from:\n"
    f"    - {ANA}\n"
    f"    - {BEN}\n"
)


@pytest.fixture
def seat(relay_mod, monkeypatch):  # noqa: F811
    mod, d1, sent = relay_mod
    monkeypatch.setattr(hook_worker, "INLINE", False)  # the real worker, as on a seat
    mod._YAML_PATH.write_text(_TWO_ROSTERED)
    monkeypatch.setattr(mod, "_TURN_SENDS", mod.text_delivery.TurnSends(), raising=False)
    monkeypatch.setattr(mod, "_TEXT_DELIVERED", mod.text_delivery.DeliveredOnce(), raising=False)
    monkeypatch.setattr(mod, "_TEXT_SEEN", mod.text_delivery.DeliveredOnce(), raising=False)
    pages: list[str] = []
    monkeypatch.setattr(mod, "_page_text_hold", pages.append)
    tool_gate.set_gate(lambda **_kw: None)
    answered_messages._reset_for_tests()
    PENDING_ACTS.clear()
    bus = _PinnedHermesHooks()
    bus.register_hook("post_llm_call", mod.on_post_llm_call)
    bus.register_hook("on_session_end", mod.on_turn_end)
    yield {"mod": mod, "bus": bus, "sent": sent, "pages": pages, "mp": monkeypatch}
    _drain(mod)
    tool_gate.set_gate(None)
    answered_messages._reset_for_tests()
    PENDING_ACTS.clear()


def _drain(mod) -> bool:
    """Wait for the plugin's end-of-turn worker. Tolerates a plugin without one
    (the pre-fix plugin sent inline), so the falsifiers fail on what they test."""
    worker = getattr(mod, "_END_OF_TURN", None)
    return True if worker is None else worker.wait_idle(10)


def _email_turn_starts(session: str, sender: str, message_id: str) -> None:
    _record_origin(sender=sender, message_id=message_id, session=session)


def test_FALSIFIER_two_text_replies_ending_together_both_land(seat):
    mod, bus = seat["mod"], seat["bus"]
    send_started = threading.Event()
    turns_done = threading.Event()
    sent: list[str] = []

    def slow_broker(*, message_id, text, html, **_kw):
        # The broker reply verb is a network round trip: the first reply is
        # still in flight while the second email's turn ends.
        send_started.set()
        turns_done.wait(5)
        sent.append(message_id)
        return f"out-{message_id}"

    seat["mp"].setattr(mod.relay, "send_reply", slow_broker)
    _email_turn_starts("s-ana", ANA, "msg-ana")
    _email_turn_starts("s-ben", BEN, "msg-ben")

    first = threading.Thread(target=bus.turn, args=("s-ana", TEXT))
    first.start()
    assert send_started.wait(5), "the first reply never reached the broker"
    second = threading.Thread(target=bus.turn, args=("s-ben", TEXT))
    second.start()
    second.join(5)
    turns_done.set()
    first.join(5)
    assert _drain(mod)

    assert bus.skipped == [], f"Hermes skipped a reply callback: {bus.skipped}"
    assert sorted(sent) == ["msg-ana", "msg-ben"]
    assert seat["pages"] == []


def test_FALSIFIER_a_skipped_text_reply_is_loud(seat, caplog):
    mod, bus = seat["mod"], seat["bus"]
    _email_turn_starts("s-ana", ANA, "msg-ana")
    (post_cb,) = bus.hooks["post_llm_call"]
    bus.running.add(("post_llm_call", id(post_cb)))  # another session's reply in flight
    with caplog.at_level(logging.ERROR):
        bus.turn("s-ana", TEXT)
        assert _drain(mod)
    assert bus.skipped == [("post_llm_call", "s-ana")]
    assert seat["sent"] == []
    assert seat["pages"] == ["no_response_captured"]
    assert any(
        r.levelno == logging.ERROR
        and "msg-ana from a rostered sender got NO reply" in r.getMessage()
        for r in caplog.records
    )


def test_a_replied_turn_is_not_paged_at_its_end(seat):
    mod, bus = seat["mod"], seat["bus"]
    _email_turn_starts("s-ana", ANA, "msg-ana")
    bus.turn("s-ana", TEXT)
    assert _drain(mod)
    assert [s["message_id"] for s in seat["sent"]] == ["msg-ana"]
    assert seat["pages"] == []


def test_a_stranger_or_a_non_email_turn_is_never_paged(seat):
    mod, bus = seat["mod"], seat["bus"]
    _email_turn_starts("s-x", "stranger@example.org", "msg-x")
    (post_cb,) = bus.hooks["post_llm_call"]
    bus.running.add(("post_llm_call", id(post_cb)))
    bus.turn("s-x", TEXT)  # not rostered: quarantined mail, silence is designed
    bus.turn("cron_job1_20261019_150000", TEXT)  # no email opened the turn
    assert _drain(mod)
    assert seat["pages"] == [] and seat["sent"] == []


def test_the_worker_acts_on_the_turn_the_hook_saw(seat):
    """The next turn of the session starts before the worker runs: the reply
    still answers the email that opened the turn that ended."""
    mod = seat["mod"]
    release = threading.Event()
    seat["mp"].setattr(hook_worker, "INLINE", False)
    mod._END_OF_TURN.submit(release.wait, 5)  # the worker is busy
    _email_turn_starts("s-ana", ANA, "msg-ana")
    mod.on_post_llm_call(session_id="s-ana", assistant_response=TEXT)
    mod.on_pre_llm_call(session_id="s-ana", user_message="(next turn)")
    mod._TURN_SENDS.note("s-ana")  # the NEXT turn drafts something
    release.set()
    assert _drain(mod)
    assert [s["message_id"] for s in seat["sent"]] == ["msg-ana"]
