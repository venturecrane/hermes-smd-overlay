"""Email delivery for a space-bound cron run while Telegram is unbound (ADR 0091).

A couples seat must work fully over email before anyone binds Telegram. A
routine whose space has no Telegram chat yet is materialized with Hermes
``deliver: local`` (``shared.cron_space``), and its final response comes here:

* ``person:<id>``: one email to that person's ``people[].email``;
* ``shared``: ONE email to every person.

WHO IS DECIDED BY CODE, TWICE. :func:`shared.cron_space.email_run` derives the
recipients from the job's managed name and the seat's ``people:``; the model's
text is the body and nothing else. The send rides the broker's ``space`` lane,
which admits exactly ``people[].email`` (ss-console ``space_people.py``), so even
a caller here that named someone else would be refused by the process holding
the key. Everything else about the send (ceilings, taint, content floor,
fabrication scan, the audit row) is the seat's ordinary gated path,
``shared.send_dispatch``.

THE HOOKS ONLY HAND OFF; A WORKER SENDS (hermes-captain-mark, 2026-10-19).
Hermes runs a bounded hook callback on a worker thread and keys "is it still
running" on ``(hook_name, id(callback))`` for the WHOLE PROCESS, not per
session (``hermes_cli/plugins_dispatch.py`` ``_run_hook_callback_bounded`` at
the pinned ref): a second invocation while the first is in flight is skipped
with one WARNING ("skipped after previous timeout or while still running") and
nothing else. When this module sent from inside ``post_llm_call``, two routines
due the same minute (one per partner) raced: p2's callback was still talking to
the broker when p1's turn ended, so p1's callback was skipped, p1's message was
dropped, and the cron job reported success. So the hook callbacks now only
record (``note_response`` / ``note_turn_end``: a queue put, microseconds, never
"still running" long enough to collide), and a worker thread does the slow part
in the turn's own context (``contextvars``), exactly as Hermes would have.

A ROUTINE THAT OWES AN EMAIL AND SENDS NONE IS LOUD. ``on_session_end`` fires
after ``post_llm_call`` in the same turn (``agent/turn_finalizer.py``). If a run
that owes an email reaches its end with no response recorded (its
``post_llm_call`` was skipped, or the run produced nothing), or its send was
refused, that is logged at ERROR and paged (``space_routine_not_delivered``):
the run is over, so nobody else would learn a person heard nothing.

Never raises: the run's output is still saved locally by Hermes.
"""

from __future__ import annotations

import contextvars
import logging
import queue
import re
import threading
from collections import OrderedDict
from typing import Any

from shared import cron_space, send_anchor, send_dispatch, silence
from shared import space as _space

logger = logging.getLogger(__name__)

#: Fixed frame used only when the response has no usable first line.
FALLBACK_SUBJECT = "A note from your assistant"
_SUBJECT_MAX = 78

_lock = threading.Lock()
_delivered: OrderedDict[str, None] = OrderedDict()
#: Sessions whose final response reached this module (delivered or not owed).
_responded: OrderedDict[str, None] = OrderedDict()
_MAX_REMEMBERED = 256

#: One silence rule for every final-response consumer (``shared.silence``).
_is_silent = silence.is_silent

#: The paging message for a routine that owed an email and sent none.
PAGE_MESSAGE = "space_routine_not_delivered"

#: Seconds the worker waits on an empty queue before it exits. A new event
#: starts a new worker, so an idle seat holds no thread; the worker is
#: non-daemon, so a process exiting with a delivery queued finishes it first.
_IDLE_EXIT_SECONDS = 2.0

_events: queue.Queue[tuple[str, str, Any, contextvars.Context]] = queue.Queue()
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def subject_for(text: str) -> str:
    """The first non-empty line, stripped of markdown markers, cut to 78 chars."""
    for line in text.splitlines():
        cleaned = re.sub(r"^[#>*\-\s]+", "", line).replace("**", "").strip()
        if cleaned:
            return cleaned if len(cleaned) <= _SUBJECT_MAX else cleaned[: _SUBJECT_MAX - 3] + "..."
    return FALLBACK_SUBJECT


def _remember(store: OrderedDict[str, None], session_id: str) -> bool:
    """Record ``session_id`` in ``store``; True the first time. Caller holds ``_lock``."""
    if session_id in store:
        return False
    store[session_id] = None
    while len(store) > _MAX_REMEMBERED:
        store.popitem(last=False)
    return True


def _claim(session_id: str) -> bool:
    """True the first time a session is delivered in this process."""
    with _lock:
        return _remember(_delivered, session_id)


def _page(reason: str, space_key: str) -> None:
    """A person was owed a routine's email and got none: page. Reason code and
    space key only (never the text, never an address). Never raises."""
    try:
        import os

        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("reason", reason)
            scope.set_tag("space", space_key)
            scope.set_tag(
                "customer",
                os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG") or "",
            )
            sentry_sdk.capture_message(PAGE_MESSAGE, level="error")
    except Exception as exc:  # noqa: BLE001 - monitoring must never break delivery
        logger.debug("space_delivery: page skipped (%s)", exc)


def deliver(
    session_id: Any,
    response: Any,
    *,
    config: _space.SpaceConfig | None = None,
    hermes_home: str | None = None,
) -> send_dispatch.DispatchResult | None:
    """Email this cron run's final response when it owes one. ``None`` when it
    owes none (not a space-mode seat, not an email-delivered run, silence, or
    already delivered). Never raises. Synchronous: the hooks reach it through
    the worker (:func:`note_response`), never directly."""
    try:
        sid = str(session_id or "")
        text = response if isinstance(response, str) else ""
        if sid:
            with _lock:
                _remember(_responded, sid)
        cfg = config if config is not None else _space.space_config()
        if cfg is None or not sid:
            return None
        run = cron_space.email_run(sid, cfg, hermes_home=hermes_home)
        if run is None or not text.strip() or _is_silent(text):
            return None
        if not _claim(sid):
            return None
        result = send_dispatch.dispatch(
            to=list(run.recipients),
            subject=subject_for(text),
            text=text,
            # A routine answers no email; the lane is what admits the people,
            # named here and nowhere else.
            anchor=None,
            lane=send_anchor.LANE_SPACE,
            session_id=sid,
            templated=False,
            audit_extra={"skill_name": run.skill},
        )
        if result.sent:
            logger.info("space_delivery: %s routine delivered by email", run.space_key)
        else:
            logger.error(
                "space_delivery: %s routine NOT delivered by email: %s",
                run.space_key,
                result.reason,
            )
            _page("send_refused", run.space_key)
        return result
    except Exception:  # noqa: BLE001 - delivery never raises into a hook
        logger.error("space_delivery: delivery failed", exc_info=True)
        _page("delivery_error", "")
        return None


def settle(
    session_id: Any,
    *,
    completed: Any = None,
    failed: Any = None,
    interrupted: Any = None,
    config: _space.SpaceConfig | None = None,
    hermes_home: str | None = None,
) -> bool:
    """At a turn's end: True (and logged ERROR + paged) when the run owed an
    email and no final response ever reached :func:`deliver`. Never raises."""
    try:
        sid = str(session_id or "")
        if not sid:
            return False
        with _lock:
            if sid in _responded:
                return False
        cfg = config if config is not None else _space.space_config()
        if cfg is None:
            return False
        run = cron_space.email_run(sid, cfg, hermes_home=hermes_home)
        if run is None:
            return False
        logger.error(
            "space_delivery: %s routine NOT delivered: no final response reached delivery "
            "(session=%s completed=%s failed=%s interrupted=%s). If Hermes logged "
            "\"Hook 'post_llm_call' ... skipped\" for this run, the response was dropped "
            "by the hook dispatcher.",
            run.space_key,
            sid,
            completed,
            failed,
            interrupted,
        )
        _page("no_response_captured", run.space_key)
        return True
    except Exception:  # noqa: BLE001 - never raises into a hook
        logger.error("space_delivery: settle failed", exc_info=True)
        return False


# --------------------------------------------------------------- hand-off


def note_response(session_id: Any, response: Any) -> None:
    """``post_llm_call``: hand the turn's final response to the worker. A queue
    put; returns at once so the callback is never "still running" when the
    next routine's turn ends. Never raises."""
    _enqueue("response", session_id, response)


def note_turn_end(session_id: Any, **status: Any) -> None:
    """``on_session_end``: hand the turn's end to the worker, which checks the
    run's owed email was attempted. Never raises."""
    _enqueue(
        "end",
        session_id,
        {k: status.get(k) for k in ("completed", "failed", "interrupted")},
    )


def _enqueue(kind: str, session_id: Any, payload: Any) -> None:
    global _worker
    try:
        sid = str(session_id or "")
        if not sid:
            return
        _events.put((kind, sid, payload, contextvars.copy_context()))
        with _worker_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(
                    target=_drain_loop, name="smd-space-delivery", daemon=False
                )
                _worker.start()
    except Exception:  # noqa: BLE001 - never raises into a hook
        logger.error(
            "space_delivery: could not hand off %s for %s", kind, session_id, exc_info=True
        )


def _handle(kind: str, sid: str, payload: Any) -> None:
    if kind == "response":
        deliver(sid, payload)
    elif kind == "end":
        settle(sid, **(payload or {}))


def _drain_loop() -> None:
    global _worker
    while True:
        try:
            kind, sid, payload, ctx = _events.get(timeout=_IDLE_EXIT_SECONDS)
        except queue.Empty:
            with _worker_lock:
                if _events.empty():
                    if _worker is threading.current_thread():
                        _worker = None
                    return
            continue
        try:
            # The turn's own context: anything the send path reads from the
            # session (gateway session vars) sees what the hook would have.
            ctx.run(_handle, kind, sid, payload)
        except Exception:  # noqa: BLE001 - one bad event never stops the worker
            logger.error("space_delivery: worker failed on %s for %s", kind, sid, exc_info=True)
        finally:
            _events.task_done()


def wait_idle(timeout: float = 10.0) -> bool:
    """Block until every handed-off event is processed (tests, shutdown).
    True when the queue drained within ``timeout``."""
    done = threading.Event()

    def _join() -> None:
        _events.join()
        done.set()

    threading.Thread(target=_join, daemon=True).start()
    return done.wait(timeout)


def _reset_for_tests() -> None:
    wait_idle(5.0)
    with _lock:
        _delivered.clear()
        _responded.clear()
