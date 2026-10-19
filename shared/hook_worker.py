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

The thread is non-daemon (a process exiting with work queued finishes it
first) and exits after :data:`IDLE_EXIT_SECONDS` with nothing queued; the next
submit starts a new one, so an idle seat holds no thread.

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

IDLE_EXIT_SECONDS = 2.0

#: Tests only: run submitted work on the caller's thread (see module doc).
INLINE = False


class HookWorker:
    """A FIFO of end-of-turn calls, each run in the context it was submitted in."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._queue: queue.Queue[tuple[Callable[..., Any], tuple, dict, contextvars.Context]] = (
            queue.Queue()
        )
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """Queue ``fn(*args, **kwargs)`` to run in the current context. True when
        queued (or, under :data:`INLINE`, run). Never raises."""
        try:
            ctx = contextvars.copy_context()
            if INLINE:
                self._run(fn, args, kwargs, ctx)
                return True
            self._queue.put((fn, args, kwargs, ctx))
            with self._lock:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(
                        target=self._drain, name=self.name, daemon=False
                    )
                    self._thread.start()
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
                fn, args, kwargs, ctx = self._queue.get(timeout=IDLE_EXIT_SECONDS)
            except queue.Empty:
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
