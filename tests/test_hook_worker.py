"""``shared.hook_worker``: the hook's side is a queue put, nothing more.

FALSIFIER for the 2026-10-19 overlay CI flake: the first worker started its
thread inside ``submit`` (and exited after 2 s idle, so nearly every turn on a
quiet seat started one). ``Thread.start()`` waits for the new thread to be
scheduled, and the new thread then ran the delivery until its first blocking
call before ``submit`` returned, so the hook stayed "running" for the worker's
CPU time. Here a fresh worker's first ``submit`` must not create a thread at
all: on the pre-fix worker it does, the patched constructor raises, and the
hand-off fails.
"""

from __future__ import annotations

import threading

import pytest

from shared import hook_worker


@pytest.fixture
def real_worker(monkeypatch):
    monkeypatch.setattr(hook_worker, "INLINE", False)


def test_FALSIFIER_submit_starts_no_thread(real_worker, monkeypatch):
    worker = hook_worker.HookWorker("t-hot-path")
    ran = threading.Event()

    def no_thread(*_a, **_k):
        raise AssertionError("submit created a thread on the hook's path")

    monkeypatch.setattr(hook_worker.threading, "Thread", no_thread)
    assert worker.submit(ran.set) is True
    assert ran.wait(5)


def test_the_worker_runs_calls_in_order_in_their_context(real_worker):
    import contextvars

    var = contextvars.ContextVar("v", default="")
    worker = hook_worker.HookWorker("t-order")
    seen: list[str] = []
    for value in ("a", "b", "c"):
        token = var.set(value)
        try:
            worker.submit(lambda: seen.append(var.get()))
        finally:
            var.reset(token)
    assert worker.wait_idle(5)
    assert seen == ["a", "b", "c"]


def test_a_dead_worker_is_restarted_and_logged(real_worker, caplog):
    worker = hook_worker.HookWorker("t-restart")
    worker._thread = None  # as if it had died
    ran = threading.Event()
    with caplog.at_level("ERROR", logger="shared.hook_worker"):
        assert worker.submit(ran.set)
    assert ran.wait(5)
    assert any("restarted from a hook" in r.getMessage() for r in caplog.records)


def test_a_failing_call_does_not_stop_the_worker(real_worker):
    worker = hook_worker.HookWorker("t-survive")
    ran = threading.Event()

    def boom():
        raise RuntimeError("x")

    worker.submit(boom)
    worker.submit(ran.set)
    assert ran.wait(5)
