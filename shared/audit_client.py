"""Audit-log transport selection: direct D1 file, or the append-only broker.

This is the single selection point for how an ``audit_log`` row reaches
storage. Every audit writer in the overlay (``emit.AuditLogWriter``,
``shared.broker_audit``, the webhook-router, the outbound trust gate) builds
its client through :func:`audit_client_from_env` and then calls
``client.execute(INSERT_SQL, *params)``.

Two transports:

* **Direct** (default — ``SMD_AUDIT_BROKER_SOCKET`` unset): a
  :class:`~shared.d1_client.D1Client` on ``SMD_D1_AUDIT_BINDING``. This is
  the legacy / local-dev / test path and is byte-for-byte the prior
  behavior.
* **Broker** (``SMD_AUDIT_BROKER_SOCKET`` set): a :class:`BrokerAuditClient`
  that ships the row over a Unix socket to the capability broker, which holds
  the *only* RW handle on the ledger file. The agent uid cannot open the
  ledger for write (OP-P1-4), so this is the path that makes the audit log
  tamper-resistant. The broker re-derives ``id``/``ts`` server-side, so the
  agent cannot backdate or collide rows.

:class:`BrokerAuditClient` exposes ``.execute(sql, *params)`` so it is a
drop-in for ``D1Client`` at every call site — no audit writer changes shape.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.audit_contract import COLUMNS
from shared.audit_failure_counter import record_audit_write_failure
from shared.gateway_identity import _env_says_gateway

logger = logging.getLogger(__name__)

SOCKET_ENV = "SMD_AUDIT_BROKER_SOCKET"
_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_HERMES_HOME = "/opt/data"

#: Gap-buffer bounds (SMD-OPERATOR-9). A broker respawn takes ~2s; a starved
#: boot can take tens of seconds. Past these the rows are counted as lost.
_BUFFER_MAX_ROWS = 2000
_RETRY_DEADLINE_SECONDS = 120.0
_RETRY_MAX_BACKOFF_SECONDS = 30.0


class AuditWriteError(RuntimeError):
    """An ``audit_log`` row could not be persisted (direct or broker path).

    Canonical definition lives here in ``shared/`` so the broker client can
    raise it without importing the plugin layer. ``hermes-smd-audit/emit.py``
    re-exports it for backward compatibility with existing importers.

    Constructing one TALLIES a lost row (ss-console #2498). The counting lives
    in the constructor, not at the raise sites, because this class is the one
    definition of "a row could not be persisted" and every writer on the
    Machine — the audit plugin's hooks, the trust and reply gates, the webhook
    router, the config applier — funnels its failure through it before some
    caller swallows it. Counting at the raise sites would have to be re-added
    by every future writer, and the failure this closes is precisely that a
    swallowed write left no trace anywhere.

    The tally is best-effort and never raises, so a broken counter cannot turn
    a degraded audit write into a crashed hook. Off-Machine it is a silent
    no-op (see :mod:`shared.audit_failure_counter`), so raising this in a unit
    test writes nothing.

    One count = one raise, NOT one permanently-lost row. One retry path exists:
    the gateway's in-memory gap buffer (:class:`_GapBuffer`), for rows the
    broker never received because it was not listening. It does not raise
    this class for a row it goes on to persist, so a flushed row is never
    counted; a row it gives up on is tallied once, including one held when
    the gateway is killed (counted at the next boot).
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        record_audit_write_failure(str(args[0]) if args else "audit write failed")


class _BrokerUnreachable(Exception):
    """The broker could not be reached at ``connect()``: nothing was sent.

    Distinct from :class:`AuditWriteError` on purpose. Nothing left the
    process, so nothing can have been half-written and the row is safe to hold
    and send again. Constructing this tallies nothing; a caller that may not
    hold the row converts it to :class:`AuditWriteError`, which does.
    """


#: connect() errors that prove the request never reached the broker: no
#: listener on the socket (respawn gap), no socket file, a full accept backlog
#: (EAGAIN while a starved broker comes up), or a reset during the handshake.
_UNREACHABLE_AT_CONNECT = (
    ConnectionRefusedError,
    FileNotFoundError,
    BlockingIOError,
    ConnectionResetError,
)


def _process_is_gateway() -> bool:
    """True only when ``SMD_GATEWAY_PID`` names THIS process.

    Closed by default: unset, unparseable, or a different pid all answer False.
    The broker is down when this is asked, so the environment is the only
    witness available. One rule, one copy: this is ``shared.gateway_identity``'s
    own environment check.
    """
    return _env_says_gateway()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _with_buffered_at(row: dict[str, Any], at: str) -> dict[str, Any]:
    """Copy of ``row`` whose metadata records when the event really happened.

    The broker stamps ``ts`` when the row ARRIVES, which after a gap is late.
    ``buffered_at`` is when the gateway emitted it. Metadata is a JSON object
    string or None on every emitter (``shared.audit_contract._dumps``); anything
    else is kept verbatim under ``original`` rather than discarded.
    """
    meta = row.get("metadata")
    parsed: Any
    if meta is None:
        parsed = {}
    elif isinstance(meta, str):
        try:
            parsed = json.loads(meta)
        except ValueError:
            parsed = None
    else:
        parsed = None
    if not isinstance(parsed, dict):
        parsed = {"original": meta}
    parsed["buffered_at"] = at
    out = dict(row)
    out["metadata"] = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return out


_HELD_RELPATH = Path(".smd") / "audit_gap_held"


def _held_path(hermes_home: str | None = None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    return Path(home) / _HELD_RELPATH


def _record_held(count: int, hermes_home: str | None = None) -> None:
    """Persist how many rows this gateway is holding. Never raises.

    A hard kill (SIGKILL, OOM, a Fly stop that outlasts its grace) runs no
    ``atexit``, so without this the rows a dead gateway held would vanish with
    no count, and the tally is the only signal the console alerts on. The next
    gateway's registration converts whatever is left here into tallied losses
    (:func:`count_rows_held_by_a_dead_gateway`). Same directory, same owner and
    same trust as the tally itself; no dir means off-Machine, so a no-op.
    """
    path = _held_path(hermes_home)
    if not path.parent.is_dir():
        return
    try:
        if count <= 0:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(str(count), encoding="ascii")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("audit gap buffer: cannot record held count (%s): %s", path, exc)


def count_rows_held_by_a_dead_gateway(hermes_home: str | None = None) -> int:
    """Tally the rows a previous gateway died holding, then clear the record.

    Called once at audit-plugin registration, before this gateway holds
    anything. Returns the number tallied. Never raises.
    """
    path = _held_path(hermes_home)
    try:
        raw = path.read_text(encoding="ascii")
    except OSError:
        return 0
    try:
        held = max(0, min(int(raw.strip()), _BUFFER_MAX_ROWS))
    except ValueError:
        held = 0
    for _ in range(held):
        record_audit_write_failure("audit row held across a broker gap when the gateway died")
    try:
        path.unlink()
    except OSError:
        logger.warning("audit gap buffer: could not clear held record %s", path)
    return held


class _GapBuffer:
    """Rows the gateway emitted while the broker was not listening.

    WHY (SMD-OPERATOR-9, SS-WEB-6..9). The broker is respawned 2s after it
    exits (ss-console ``operator/templates/entrypoint.sh``), and on a starved
    1-vCPU seat a respawn or a slow start can run longer. Every observational
    row the gateway wrote in that window was dropped and tallied: 90 refusals
    since July, alerts about 3 minutes after each release.

    WHY MEMORY AND NOT DISK. A file the gateway drains through ``audit_append``
    would be writable by every agent-uid child (execute_code, terminal, cron),
    and the broker accepts whatever the gateway's pid sends. A spool would let
    a tool call mint audit rows. Only the COUNT of held rows is written down
    (:func:`_record_held`), so a hard kill is still counted.

    NO I/O UNDER THE LOCK. The lock guards the queue and nothing else. Once
    any row is held, every later observational row is queued behind it and
    only the retry thread sends, so a broker that accepts and never answers
    costs the retry thread its socket timeouts, never a tool call. The retry
    deadline is wall-clock from the start of the gap, so a silent broker is
    abandoned (and counted) on time.

    ORDER. Held rows reach the ledger in the order they were emitted. Rows from
    writers that do not hold (the cost breaker, the gates) can land between
    them, and the broker's ``ts`` is arrival time: the emit time of a held row
    is ``metadata.buffered_at``.

    WHAT STILL COUNTS AS LOST. A row arriving at a full buffer, a gap that
    outlives the deadline, rows held at interpreter exit, rows held when the
    gateway is killed (counted at the next boot), and an ambiguous send (the
    row may have landed; sending it again could duplicate it, so it is tallied
    once and dropped, the same stance as ``shared.routine_change_spool``). A
    row that is delivered is never counted.
    """

    def __init__(
        self,
        *,
        max_rows: int = _BUFFER_MAX_ROWS,
        deadline_seconds: float = _RETRY_DEADLINE_SECONDS,
        max_backoff_seconds: float = _RETRY_MAX_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        hermes_home: str | None = None,
    ) -> None:
        self._max_rows = max_rows
        self._deadline = deadline_seconds
        self._max_backoff = max_backoff_seconds
        self._clock = clock
        self._sleep = sleep
        self._hermes_home = hermes_home
        self._reset()

    def _reset(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[dict[str, Any]] = deque()
        self._gap_started: float | None = None
        self._retrier: threading.Thread | None = None

    def reset_in_forked_child(self) -> None:
        """A forked child is not the gateway: drop the parent's rows uncounted
        (the parent still holds and will deliver them) and the parent's lock,
        which may have been held by a thread that does not exist here."""
        self._reset()

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)

    def holding(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def hold(self, payload: dict[str, Any], send: Callable[[dict[str, Any]], Any]) -> None:
        with self._lock:
            if len(self._pending) >= self._max_rows:
                record_audit_write_failure("audit gap buffer full; row dropped")
                return
            self._pending.append(
                {**payload, "row": _with_buffered_at(payload["row"], _utc_now_iso())}
            )
            if self._gap_started is None:
                self._gap_started = self._clock()
            _record_held(len(self._pending), self._hermes_home)
            if self._retrier is None or not self._retrier.is_alive():
                self._retrier = threading.Thread(
                    target=self._retry_loop, args=(send,), name="smd-audit-gap-retry", daemon=True
                )
                self._retrier.start()

    def _retry_loop(self, send: Callable[[dict[str, Any]], Any]) -> None:
        backoff = 1.0
        while True:
            with self._lock:
                if not self._pending:
                    self._gap_started = None
                    self._retrier = None
                    _record_held(0, self._hermes_home)
                    return
                if self._gap_started is not None and (
                    self._clock() - self._gap_started >= self._deadline
                ):
                    self._abandon_locked(f"audit broker unreachable for over {self._deadline:.0f}s")
                    self._retrier = None
                    return
                head = self._pending[0]
            try:
                send(head)
            except _BrokerUnreachable:
                self._sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            except AuditWriteError:
                pass  # ambiguous or refused: tallied by the constructor, never resent
            except Exception as exc:  # noqa: BLE001 — the retry thread must not die
                record_audit_write_failure(f"audit gap flush failed: {type(exc).__name__}")
            with self._lock:
                # Only this thread removes from the head, so it is still `head`.
                if self._pending and self._pending[0] is head:
                    self._pending.popleft()
                _record_held(len(self._pending), self._hermes_home)
            backoff = 1.0

    def _abandon_locked(self, reason: str) -> None:
        while self._pending:
            self._pending.popleft()
            record_audit_write_failure(reason)
        self._gap_started = None
        _record_held(0, self._hermes_home)

    def drain_at_exit(self) -> None:
        if not self._lock.acquire(timeout=1.0):
            return  # cannot happen (no I/O under the lock); the held record still counts them
        try:
            if self._pending:
                self._abandon_locked("audit rows still buffered at interpreter exit")
        finally:
            self._lock.release()


_GAP = _GapBuffer()
atexit.register(_GAP.drain_at_exit)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_GAP.reset_in_forked_child)


class BrokerAuditClient:
    """Append-only audit writer that speaks to the capability broker.

    Drop-in for :class:`~shared.d1_client.D1Client` at the
    ``.execute(sql, *params)`` seam. ``sql`` is accepted for signature
    compatibility; only the canonical audit ``INSERT`` is supported. The
    12 positional params are the :data:`~shared.audit_contract.COLUMNS`
    tuple ``(id, ts, action_type, ...)``; the broker stamps ``id``/``ts``
    server-side, so those two leading values are dropped before sending.

    ``buffer_on_unreachable`` (default False) lets the GATEWAY hold rows across
    a broker gap instead of losing them (see :class:`_GapBuffer`). A held row
    returns 1 without having reached the broker yet. Only the audit plugin's
    observational hooks opt in. Callers whose rule is "a transition that cannot
    be recorded did not happen" (the cost breaker, outbound and reply gates)
    keep the default: for them, no exception still means the broker took it.
    """

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        buffer_on_unreachable: bool = False,
        gap_buffer: _GapBuffer | None = None,
    ) -> None:
        self._socket_path = socket_path or os.environ.get(SOCKET_ENV, "")
        self._timeout = timeout
        self._buffer_on_unreachable = buffer_on_unreachable
        self._gap = gap_buffer if gap_buffer is not None else _GAP
        if not self._socket_path:
            raise AuditWriteError(f"{SOCKET_ENV} is unset; cannot reach the audit broker")

    def execute(self, sql: str, *params: Any) -> int:
        """Ship one audit row to the broker. Returns 1 (sent, or held).

        Raises:
            AuditWriteError: the broker refused the append, or was unreachable
                and this row could not be held.
        """
        if len(params) != len(COLUMNS):
            raise AuditWriteError(
                f"audit broker: expected {len(COLUMNS)} params for {COLUMNS!r}, got {len(params)}"
            )
        # COLUMNS == (id, ts, action_type, ...). Drop id/ts — the broker
        # re-derives them so the agent cannot backdate or collide.
        row = dict(zip(COLUMNS[2:], params[2:], strict=True))
        payload = {"action": "audit_append", "row": row}
        if not self._buffer_on_unreachable or not _process_is_gateway():
            self._request(payload)
            return 1
        if self._gap.holding():
            # Queue behind the held rows: order is kept and the hook does no I/O.
            self._gap.hold(payload, self._send)
            return 1
        try:
            self._send(payload)
        except _BrokerUnreachable:
            self._gap.hold(payload, self._send)
        return 1

    def execute_suppressed_webhook(self, sql: str, *params: Any) -> int:
        """Ship one WEBHOOK_SUPPRESSED row via the uid-gated broker verb.

        Same ``(sql, *params)`` seam as :meth:`execute`, but routes through
        ``webhook_suppressed_append`` instead of the generic ``audit_append``.
        Rationale: the generic verb is PID-gated to the gateway process, and the
        webhook gate runs as the agent uid on a NON-gateway PID (same shape as
        the cron ``pre_run`` children behind ``suppressed_wake_append``). The
        broker locks the row's ``action_type`` to ``WEBHOOK_SUPPRESSED`` on this
        verb, so it cannot forge any other audit row.
        """
        if len(params) != len(COLUMNS):
            raise AuditWriteError(
                f"audit broker: expected {len(COLUMNS)} params for {COLUMNS!r}, got {len(params)}"
            )
        row = dict(zip(COLUMNS[2:], params[2:], strict=True))
        self._request({"action": "webhook_suppressed_append", "row": row})
        return 1

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._send(payload)
        except _BrokerUnreachable as exc:
            raise AuditWriteError(f"audit broker socket error: {exc}") from exc

    def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One request. :class:`_BrokerUnreachable` when nothing was delivered."""
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:
            raise AuditWriteError(f"audit broker socket error: {exc}") from exc
        with client:
            try:
                client.settimeout(self._timeout)
                client.connect(self._socket_path)
            except _UNREACHABLE_AT_CONNECT as exc:
                raise _BrokerUnreachable(str(exc)) from exc
            except OSError as exc:
                raise AuditWriteError(f"audit broker socket error: {exc}") from exc
            try:
                client.sendall(encoded)
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    chunk = client.recv(65_536)
                    if not chunk:
                        break
                    buf.extend(chunk)
            except OSError as exc:
                raise AuditWriteError(f"audit broker socket error: {exc}") from exc
        try:
            data = json.loads(buf)
        except ValueError as exc:
            raise AuditWriteError("audit broker returned invalid JSON") from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            message = (data.get("message") or data.get("error")) if isinstance(data, dict) else None
            raise AuditWriteError(f"audit broker refused append: {message or 'unknown error'}")
        return data


def audit_client_from_env(
    customer_slug: str | None = None, *, buffer_on_unreachable: bool = False
) -> Any:
    """Return the audit-log transport for this Machine.

    Broker mode when ``SMD_AUDIT_BROKER_SOCKET`` is set; otherwise a direct
    :class:`~shared.d1_client.D1Client` on ``SMD_D1_AUDIT_BINDING``.
    ``buffer_on_unreachable`` applies to broker mode only (see
    :class:`BrokerAuditClient`).
    """
    if os.environ.get(SOCKET_ENV):
        return BrokerAuditClient(buffer_on_unreachable=buffer_on_unreachable)
    # Direct mode — import lazily so the broker path carries no D1 dependency.
    from shared.d1_env import d1_client_from_env

    return d1_client_from_env(customer_slug, binding_name="SMD_D1_AUDIT_BINDING")


__all__ = [
    "SOCKET_ENV",
    "AuditWriteError",
    "BrokerAuditClient",
    "audit_client_from_env",
    "count_rows_held_by_a_dead_gateway",
]
