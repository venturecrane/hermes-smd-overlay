"""The casework replay register: task writes the model triggers but never names.

THE PROBLEM (ss-console ``docs/specs/operator/case-manager-deadline-work.md``,
Jobs 1 and 1a). The Operator closes and reassigns firm tasks in Smokeball, and
no gated code path writes to Smokeball: every ``mcp_smokeball_update_task`` is a
tool call inside a woken agent turn. So the model is the one making the call,
and a model that makes a call can also choose its arguments. That is how a
wrong task gets closed: one transposed id in a batch of twelve.

THE ANSWER is the stored-payload replay the commitment round trip already uses
(``act_broker.CALL_PAYLOAD_ACTS`` / :mod:`shared.pending_acts`): code writes a
QUEUE of the exact writes a person approved (or the firm's level permits), and
the trust gate OVERWRITES the model's arguments with the queue head. The model's
only job is to make the call; what executes is what the queue holds.

* **Loaded by code only.** ``casework_finish`` loads the closes a pre_run wrote
  into a tamper-fenced envelope; ``reply_verdicts`` loads the writes a verified
  reply approved, from the raise rows the broker stamped. Neither takes an id
  from the model.
* **Session-keyed.** A queue belongs to the turn that loaded it. A session with
  no queue is untouched: every other routine's ``update_task`` behaves exactly
  as before.
* **Nothing beyond the queue.** In a session that loaded a queue, an
  ``update_task`` call with the queue empty is REFUSED. The turn cannot add a
  write the queue did not name.
* **The ceiling still decides.** The replacement happens BEFORE the exposure
  check, so a refused ceiling refuses the replayed write too, and that write is
  recorded ``write_failed``, never ``completed``.
* **Every outcome is a row.** The post-tool hook records ``completed`` (with the
  tool call id, which is the audit row's join key) or ``write_failed`` through
  the broker's ``casework_event_append`` verb. A write whose outcome is unknown
  (the pre-hook allowed it and no post-tool call ever came) is recorded
  ``write_failed`` when the next write starts: an unknown is never a success.

Exception-safe: a failed row write is logged, never raised into a hook.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from shared.pending_acts import tool_call_failed

logger = logging.getLogger(__name__)

#: The one tool the register replays.
UPDATE_TASK_TOOL = "mcp_smokeball_update_task"

COMPLETED = "completed"
WRITE_FAILED = "write_failed"

_SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"
_TIMEOUT_SECONDS = 10
#: A queue outlives the turn that loaded it by at most this long.
_TTL_SECONDS = 1800.0
_MAX_SESSIONS = 64
#: Bound on one queue: the proposal cap is 30 lines, plus the closes.
MAX_QUEUE = 60

#: What the turn is told when it calls update_task with nothing queued. No gate
#: or rule is named: corrective action only.
BEYOND_QUEUE = (
    "there is no task update waiting in this conversation. Every approved change "
    "has been made; do not update any other task. Carry on with the next step."
)


@dataclass(frozen=True)
class TaskWrite:
    """One queued Smokeball task write, and the ledger row it answers to.

    Exactly one of ``is_completed`` (a close) and ``assignee_ids`` (a
    reassignment) is set. ``staff_id`` is the owner the full-replace PUT
    requires; on a close it is also the completed-by staff member."""

    task_id: str
    staff_id: str
    item_key: str
    matter_id: str | None
    skill: str
    is_completed: bool | None = None
    assignee_ids: tuple[str, ...] | None = None
    n: int | None = None
    line: str = ""

    def tool_arguments(self) -> dict[str, Any]:
        args: dict[str, Any] = {"task_id": self.task_id, "staff_id": self.staff_id}
        if self.is_completed is not None:
            args["is_completed"] = self.is_completed
        if self.assignee_ids is not None:
            args["assignee_ids"] = list(self.assignee_ids)
        return args


def valid_write(write: TaskWrite) -> bool:
    """Well-formed: ids present, and exactly one kind of change."""
    if not (write.task_id and write.staff_id and write.item_key and write.skill):
        return False
    closes = write.is_completed is True
    reassigns = bool(write.assignee_ids) and all(write.assignee_ids or ())
    return closes != reassigns


@dataclass
class Outcome:
    write: TaskWrite
    status: str
    reason: str = ""
    audit_ref: str = ""


@dataclass
class _Queue:
    pending: list[TaskWrite]
    loaded_at: float
    in_flight: TaskWrite | None = None
    outcomes: list[Outcome] = field(default_factory=list)


@dataclass(frozen=True)
class Replay:
    """What the gate does with one call: nothing (``act`` and ``refusal`` both
    None), replay ``act`` over the live arguments, or refuse."""

    act: TaskWrite | None = None
    refusal: str | None = None


Writer = Callable[[dict[str, Any]], Any]


def broker_append(event: dict[str, Any]) -> dict[str, Any]:
    """One casework row through the broker's validated verb."""
    socket_path = os.environ.get(_SOCKET_ENV, "")
    if not socket_path:
        raise RuntimeError(f"{_SOCKET_ENV} is unset; cannot reach the broker")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_TIMEOUT_SECONDS)
        sock.connect(socket_path)
        payload = {"action": "casework_event_append", "event": event}
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        raw = b""
        while not raw.endswith(b"\n"):
            chunk = sock.recv(65_536)
            if not chunk:
                break
            raw += chunk
    return json.loads(raw.decode("utf-8"))


def outcome_event(
    write: TaskWrite, status: str, session_id: str, *, audit_ref: str = "", reason: str = ""
) -> dict[str, Any]:
    """The ``completed`` / ``write_failed`` row for one write. The broker stamps
    ts and id; ``audit_ref`` is the update_task call's tool_call_id, the key its
    TOOL_CALL_COMPLETED audit row carries."""
    event: dict[str, Any] = {
        "ts": None,
        "skill": write.skill,
        "matter_id": write.matter_id,
        "item_key": write.item_key,
        "event": status,
        "session_id": session_id,
    }
    if audit_ref:
        event["audit_ref"] = audit_ref
    if reason:
        event["reason"] = reason[:300]
    return event


def _result_reports_error(result: Any) -> bool:
    """A connector result that carries an ``error`` is a failed write even when
    the tool call itself reported success."""
    payload: Any = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return False
    return isinstance(payload, dict) and bool(payload.get("error"))


class CaseworkActs:
    """Per-session queues of task writes (see the module docstring)."""

    def __init__(self, writer: Writer | None = None) -> None:
        self._lock = threading.Lock()
        self._queues: OrderedDict[str, _Queue] = OrderedDict()
        self._writer = writer

    def set_writer(self, writer: Writer | None) -> None:
        self._writer = writer

    # ---- loading -----------------------------------------------------------

    def load(self, session_id: str, writes: list[TaskWrite]) -> int:
        """Queue ``writes`` for this session, after anything already queued.
        Returns how many are now pending. Malformed writes are dropped, loudly."""
        if not session_id:
            return 0
        good = [w for w in writes if valid_write(w)]
        if len(good) != len(writes):
            logger.warning("casework_acts: dropped %d malformed write(s)", len(writes) - len(good))
        with self._lock:
            queue = self._fresh(session_id)
            if queue is None:
                queue = _Queue(pending=[], loaded_at=time.monotonic())
                self._queues[session_id] = queue
            queue.pending.extend(good[: max(0, MAX_QUEUE - len(queue.pending))])
            queue.loaded_at = time.monotonic()
            self._queues.move_to_end(session_id)
            while len(self._queues) > _MAX_SESSIONS:
                self._queues.popitem(last=False)
            return len(queue.pending)

    def _fresh(self, session_id: str) -> _Queue | None:
        queue = self._queues.get(session_id)
        if queue is not None and time.monotonic() - queue.loaded_at > _TTL_SECONDS:
            self._queues.pop(session_id, None)
            return None
        return queue

    def active(self, session_id: str) -> bool:
        with self._lock:
            return bool(session_id) and self._fresh(session_id) is not None

    def pending(self, session_id: str) -> int:
        """Writes not yet finished, counting one in flight."""
        with self._lock:
            queue = self._fresh(session_id) if session_id else None
            if queue is None:
                return 0
            return len(queue.pending) + (1 if queue.in_flight is not None else 0)

    def outcomes(self, session_id: str) -> list[Outcome]:
        with self._lock:
            queue = self._fresh(session_id) if session_id else None
            return list(queue.outcomes) if queue is not None else []

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._queues.pop(session_id, None)

    # ---- the gate ----------------------------------------------------------

    def replay(self, session_id: str, tool_name: str, args: Any) -> Replay:
        """Called by the trust gate BEFORE the ceiling decides. For a session
        holding a queue, the live ``args`` are overwritten in place with the
        queue head; with the queue empty, the call is refused."""
        if tool_name != UPDATE_TASK_TOOL or not session_id:
            return Replay()
        stale: TaskWrite | None = None
        with self._lock:
            queue = self._fresh(session_id)
            if queue is None:
                return Replay()
            if queue.in_flight is not None:
                # The last write's outcome never arrived: never call it a success.
                stale = queue.in_flight
                queue.in_flight = None
            if not queue.pending:
                act = None
            else:
                act = queue.pending.pop(0)
                queue.in_flight = act
        if stale is not None:
            self._record(session_id, stale, WRITE_FAILED, reason="no outcome was reported")
        if act is None:
            return Replay(refusal=BEYOND_QUEUE)
        if isinstance(args, dict):
            args.clear()
            args.update(copy.deepcopy(act.tool_arguments()))
        return Replay(act=act)

    def blocked(self, session_id: str, reason: str) -> None:
        """The gate refused the replayed write: it did not happen."""
        with self._lock:
            queue = self._fresh(session_id)
            act = queue.in_flight if queue is not None else None
            if queue is not None:
                queue.in_flight = None
        if act is not None:
            self._record(session_id, act, WRITE_FAILED, reason=reason)

    def on_post_tool(self, tool_name: str, session_id: str, kwargs: dict[str, Any]) -> None:
        """Record the outcome of the write that just ran. Never raises."""
        try:
            if tool_name != UPDATE_TASK_TOOL or not session_id:
                return
            with self._lock:
                queue = self._fresh(session_id)
                act = queue.in_flight if queue is not None else None
                if queue is not None:
                    queue.in_flight = None
            if act is None:
                return
            failed = tool_call_failed(kwargs.get("status"), kwargs.get("error_type"))
            if failed or _result_reports_error(kwargs.get("result")):
                self._record(session_id, act, WRITE_FAILED, reason="the task update failed")
            else:
                self._record(
                    session_id, act, COMPLETED, audit_ref=str(kwargs.get("tool_call_id") or "")
                )
        except Exception:  # noqa: BLE001 — a hook must never raise
            logger.warning("casework_acts: post-tool outcome not recorded", exc_info=True)

    def _record(
        self, session_id: str, act: TaskWrite, status: str, *, audit_ref: str = "", reason: str = ""
    ) -> None:
        with self._lock:
            queue = self._queues.get(session_id)
            if queue is not None:
                queue.outcomes.append(Outcome(act, status, reason, audit_ref))
        event = outcome_event(act, status, session_id, audit_ref=audit_ref, reason=reason)
        writer = self._writer or broker_append
        try:
            response = writer(event)
        except Exception as exc:  # noqa: BLE001 — one lost row must not break the turn
            logger.warning(
                "casework_acts: %s row for %s not written (%s)", status, act.item_key, exc
            )
            return
        if not (isinstance(response, dict) and response.get("ok")):
            logger.warning(
                "casework_acts: %s row for %s refused (%s)", status, act.item_key, response
            )


#: The process singleton the trust gate and the escalation tools share.
CASEWORK_ACTS = CaseworkActs()


__all__ = [
    "BEYOND_QUEUE",
    "CASEWORK_ACTS",
    "COMPLETED",
    "CaseworkActs",
    "MAX_QUEUE",
    "Outcome",
    "Replay",
    "TaskWrite",
    "UPDATE_TASK_TOOL",
    "WRITE_FAILED",
    "broker_append",
    "outcome_event",
    "valid_write",
]
