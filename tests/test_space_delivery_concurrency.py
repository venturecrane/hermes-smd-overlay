"""Two routines due the same minute both reach their person (ss-console #3264).

THE LIVE FAILURE (hermes-captain-mark, 2026-10-19 15:51:29). Two one-shot
routines, ``couples-settings@person:p1`` and ``...@person:p2``, ran at once.
p2's email went out; p1's run logged only

    Hook 'post_llm_call' callback on_post_llm_call skipped after previous
    timeout or while still running

and then "completed successfully". Hermes keys a bounded hook callback's
"running" slot on ``(hook_name, id(callback))`` for the whole process
(``hermes_cli/plugins_dispatch.py`` ``_run_hook_callback_bounded`` at the
pinned ref 345cd2b0), so while p2's delivery callback was still talking to the
broker, p1's invocation of the SAME callback was skipped and its message lost.

:class:`_PinnedHermesHooks` reproduces exactly that skip rule. The falsifiers:

* both concurrent routines are delivered, and Hermes skips nothing (fails on
  the pre-fix plugin, which sent from inside ``post_llm_call``: one email);
* a run that owed an email and whose response never reached delivery is
  logged at ERROR and paged (fails if ``settle`` is not wired to
  ``on_session_end``, or does not page);
* a refused send is ERROR + paged, not a WARNING;
* the worker sends in the turn's own context, as Hermes' worker would.
"""

from __future__ import annotations

import contextvars
import logging
import threading

import pytest
import yaml

from shared import cron_attribution, hook_worker, send_dispatch, space, space_delivery
from tests.conftest import load_plugin
from tests.test_couples_email import CHECKIN, P1, P2, _couple, _materialize

SKILL_TEXT = "Your settings\n\nHere is how your assistant is set up for you."


class _PinnedHermesHooks:
    """Pinned Hermes' bounded hook dispatch, reduced to its skip rule.

    ``_run_hook_callback_bounded``: under a lock, if ``(hook_name, id(cb))`` is
    already running (for ANY session) the call is skipped and logged; else the
    slot is taken, the callback runs in a copy of the caller's context, and the
    slot is released when it returns. Hermes runs it on a worker thread and
    waits; running it on the caller thread is the same for the skip rule."""

    def __init__(self) -> None:
        self.hooks: dict[str, list] = {}
        self.running: set[tuple[str, int]] = set()
        self.skipped: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def register_hook(self, name: str, cb) -> None:
        self.hooks.setdefault(name, []).append(cb)

    def invoke(self, name: str, **kwargs) -> None:
        for cb in self.hooks.get(name, []):
            key = (name, id(cb))
            with self._lock:
                if key in self.running:
                    self.skipped.append((name, kwargs.get("session_id")))
                    continue
                self.running.add(key)
            try:
                contextvars.copy_context().run(cb, **kwargs)
            finally:
                with self._lock:
                    self.running.discard(key)

    def turn(self, session_id: str, text: str) -> None:
        """One cron turn's tail, in pinned order (``agent/turn_finalizer.py``)."""
        self.invoke("post_llm_call", session_id=session_id, assistant_response=text)
        self.invoke(
            "on_session_end", session_id=session_id, completed=True, failed=False, interrupted=False
        )


@pytest.fixture
def seat(monkeypatch, tmp_path):
    monkeypatch.setattr(hook_worker, "INLINE", False)  # the real worker, as on a seat
    cust = _couple()
    sessions = _materialize(
        tmp_path,
        cust,
        [{**CHECKIN, "space": "person:p1"}, {**CHECKIN, "space": "person:p2"}],
    )
    path = tmp_path / "customer.yaml"
    path.write_text(yaml.safe_dump(cust))
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    space._reset_cache_for_tests()
    cron_attribution._CACHE.clear()
    space_delivery._reset_for_tests()
    pages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        space_delivery, "_page", lambda reason, key: pages.append((reason, key)), raising=True
    )
    bus = _PinnedHermesHooks()
    load_plugin("hermes-smd-space-delivery").register(bus)
    yield {
        "bus": bus,
        "p1": sessions["op-managed:couple:couples-checkin@person:p1"],
        "p2": sessions["op-managed:couple:couples-checkin@person:p2"],
        "pages": pages,
    }
    send_dispatch.set_sender(None)
    space_delivery._reset_for_tests()
    space._reset_cache_for_tests()
    cron_attribution._CACHE.clear()


def test_FALSIFIER_two_routines_due_the_same_minute_both_deliver(seat):
    bus = seat["bus"]
    sent: list[list[str]] = []
    send_started = threading.Event()
    turns_done = threading.Event()

    def slow_broker(**kw):
        # The broker call is slow (it was, live): the first send is still in
        # flight while the second routine's turn ends.
        send_started.set()
        turns_done.wait(5)
        sent.append(list(kw["to"]))
        return send_dispatch.DispatchResult(sent=True, message_id="m", recipients=tuple(kw["to"]))

    send_dispatch.set_sender(slow_broker)

    first = threading.Thread(target=bus.turn, args=(seat["p2"], SKILL_TEXT))
    first.start()
    assert send_started.wait(5), "the first routine never reached the broker"
    second = threading.Thread(target=bus.turn, args=(seat["p1"], SKILL_TEXT))
    second.start()
    second.join(5)
    turns_done.set()
    first.join(5)
    assert space_delivery.wait_idle()

    assert bus.skipped == [], f"Hermes skipped a delivery callback: {bus.skipped}"
    assert sorted(sent) == [[P1], [P2]]
    assert seat["pages"] == []


def test_FALSIFIER_a_skipped_delivery_is_loud(seat, caplog):
    """The live shape: post_llm_call never reached the plugin for p1."""
    bus = seat["bus"]
    sent: list[list[str]] = []
    send_dispatch.set_sender(
        lambda **kw: (
            sent.append(kw["to"])
            or send_dispatch.DispatchResult(sent=True, recipients=tuple(kw["to"]))
        )
    )
    (post_cb,) = bus.hooks["post_llm_call"]
    bus.running.add(("post_llm_call", id(post_cb)))  # another session's call in flight
    with caplog.at_level(logging.ERROR, logger="shared.space_delivery"):
        bus.turn(seat["p1"], SKILL_TEXT)
        assert space_delivery.wait_idle()
    assert bus.skipped == [("post_llm_call", seat["p1"])]
    assert sent == []
    assert seat["pages"] == [("no_response_captured", "person:p1")]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("person:p1 routine NOT delivered" in r.getMessage() for r in errors)


def test_a_delivered_or_silent_run_is_not_paged(seat):
    bus = seat["bus"]
    send_dispatch.set_sender(
        lambda **kw: send_dispatch.DispatchResult(sent=True, recipients=tuple(kw["to"]))
    )
    bus.turn(seat["p1"], SKILL_TEXT)
    bus.turn(seat["p2"], "[SILENT]")
    bus.turn("webhook-session-1", "a reply to an email")  # not a routine at all
    assert space_delivery.wait_idle()
    assert seat["pages"] == []


def test_FALSIFIER_a_refused_send_is_an_error_and_paged(seat, caplog):
    send_dispatch.set_sender(
        lambda **kw: send_dispatch.DispatchResult(sent=False, reason="ceiling")
    )
    with caplog.at_level(logging.ERROR, logger="shared.space_delivery"):
        seat["bus"].turn(seat["p1"], SKILL_TEXT)
        assert space_delivery.wait_idle()
    assert seat["pages"] == [("send_refused", "person:p1")]
    assert any(
        r.levelno == logging.ERROR and "NOT delivered by email: ceiling" in r.getMessage()
        for r in caplog.records
    )


_TURN = contextvars.ContextVar("turn", default="")


def test_the_worker_sends_in_the_turns_own_context(seat):
    seen: list[str] = []
    send_dispatch.set_sender(
        lambda **kw: (
            seen.append(_TURN.get())
            or send_dispatch.DispatchResult(sent=True, recipients=tuple(kw["to"]))
        )
    )
    token = _TURN.set("turn-p1")
    try:
        seat["bus"].turn(seat["p1"], SKILL_TEXT)
    finally:
        _TURN.reset(token)
    assert space_delivery.wait_idle()
    assert seen == ["turn-p1"]


def test_the_page_names_reason_and_space_only(monkeypatch):
    sentry_sdk = pytest.importorskip("sentry_sdk")
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sentry_sdk, "capture_message", lambda msg, level=None: captured.append((msg, level))
    )
    space_delivery._page("no_response_captured", "person:p1")
    assert captured == [(space_delivery.PAGE_MESSAGE, "error")]
