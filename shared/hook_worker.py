"""Run a hook's slow end-of-turn work off the hook, in the turn's own context.

WHY (hermes-captain-mark, 2026-10-19). Pinned Hermes runs a bounded hook
callback (``post_llm_call``, ``on_session_end`` and the others in
``_HOOK_TIMEOUT_BOUNDED_HOOKS``) on a worker and keys its "running" slot on
``(hook_name, id(callback))`` for the WHOLE PROCESS, not per session
(``hermes_cli/plugins_dispatch.py`` ``_run_hook_callback_bounded``). A second
invocation while the first is in flight is skipped with one WARNING and nothing
else. A callback that sends an email from inside the hook (a broker round trip,
seconds) therefore silently drops the next session's turn that ends meanwhile:
two routines due the same minute, two emails answered at once.

So a callback that does slow work hands it here (:meth:`HookWorker.submit`: a
queue put plus ``contextvars.copy_context()``, microseconds) and returns. One
worker thread per :class:`HookWorker` runs the submitted calls in FIFO order,
each inside the context copied at submit time, which is what Hermes' own worker
would have run it in. FIFO matters: a turn's ``post_llm_call`` work is submitted
before its ``on_session_end`` work, in the same thread, so it runs first.

THE WORKER IS STARTED OUTSIDE ANY HOOK (overlay CI flake, 2026-10-19). The
first version started its thread lazily inside ``submit`` and let it exit after
2 s idle, so on a quiet seat nearly every turn's hook started a thread.
``Thread.start()`` waits for the new thread to be scheduled, and under the GIL
the new thread then ran the submitted delivery until its first blocking call
before ``submit`` returned (500 of 500 trials, cold; 0 of 500 with the worker
already running). The hook was "still running" for the worker's CPU time, not
for a queue put, which is exactly the window Hermes' skip rule punishes. So the
thread starts when the :class:`HookWorker` is built (plugin load), stays up,
and ``submit`` only starts one if it died (logged).

The thread is non-daemon, so a process exiting with work queued finishes it
first; it exits once the main thread has finished and its queue stays empty
for :data:`POLL_SECONDS`, so it never holds an exiting process open.

:data:`INLINE` (tests only) runs a submitted call synchronously on the caller's
thread, so a test that drives a hook can assert right after it. The concurrency
falsifiers turn it off.
"""

from __future__ import annotations

import contextvars
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: How often an idle worker checks whether the process is shutting down.
POLL_SECONDS = 0.5

#: Tests only: run submitted work on the caller's thread (see module doc).
INLINE = False


class HookWorker:
    """A FIFO of end-of-turn calls, each run in the context it was submitted in.

    Build one at module level (plugin load), never inside a hook: building it
    starts its thread."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._queue: queue.Queue[tuple[Callable[..., Any], tuple, dict, contextvars.Context]] = (
            queue.Queue()
        )
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ensure_thread()

    def _ensure_thread(self) -> bool:
        """Start the worker thread if it is not running. True when it was
        already running (the hot path, no thread start)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._thread = threading.Thread(target=self._drain, name=self.name, daemon=False)
            self._thread.start()
            return False

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """Queue ``fn(*args, **kwargs)`` to run in the current context. True when
        queued (or, under :data:`INLINE`, run). A queue put on the hot path;
        starts a thread only when the worker died. Never raises."""
        try:
            ctx = contextvars.copy_context()
            if INLINE:
                self._run(fn, args, kwargs, ctx)
                return True
            self._queue.put((fn, args, kwargs, ctx))
            thread = self._thread
            if thread is None or not thread.is_alive():
                if not self._ensure_thread():
                    logger.error("%s: worker was not running; restarted from a hook", self.name)
            return True
        except Exception:  # noqa: BLE001 - never raises into a hook
            logger.error("%s: could not hand off %s", self.name, fn, exc_info=True)
            return False

    def _run(self, fn: Callable[..., Any], args: tuple, kwargs: dict, ctx: Any) -> None:
        try:
            ctx.run(fn, *args, **kwargs)
        except Exception:  # noqa: BLE001 - one bad call never stops the worker
            logger.error("%s: %s failed", self.name, getattr(fn, "__name__", fn), exc_info=True)

    def _drain(self) -> None:
        while True:
            try:
                fn, args, kwargs, ctx = self._queue.get(timeout=POLL_SECONDS)
            except queue.Empty:
                if threading.main_thread().is_alive():
                    continue
                with self._lock:
                    if self._queue.empty():
                        if self._thread is threading.current_thread():
                            self._thread = None
                        return
                continue
            try:
                self._run(fn, args, kwargs, ctx)
            finally:
                self._queue.task_done()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until every submitted call has run. True when it did within
        ``timeout``."""
        done = threading.Event()

        def _join() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=_join, daemon=True).start()
        return done.wait(timeout)
