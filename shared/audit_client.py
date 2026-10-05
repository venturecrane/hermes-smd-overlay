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
import os
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from shared.audit_contract import COLUMNS
from shared.audit_failure_counter import record_audit_write_failure

SOCKET_ENV = "SMD_AUDIT_BROKER_SOCKET"
_DEFAULT_TIMEOUT_SECONDS = 5.0
GATEWAY_PID_ENV = "SMD_GATEWAY_PID"

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
    counted; a row it gives up on is tallied exactly once.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        record_audit_write_failure(str(args[0]) if args else "audit write failed")


class _BrokerUnreachable(Exception):
    """The broker socket refused or did not exist at ``connect()``.

    Distinct from :class:`AuditWriteError` on purpose: nothing was sent, so
    nothing can have been half-written, and the row is safe to hold and send
    again. Constructing this tallies nothing. A caller that is not allowed to
    hold the row converts it to :class:`AuditWriteError`, which does.
    """


def _process_is_gateway() -> bool:
    """True only when ``SMD_GATEWAY_PID`` names THIS process.

    Closed by default, like :mod:`shared.gateway_identity`: unset, unparseable,
    or a different pid all answer False. The broker is down when this is asked,
    so the environment is the only witness available.
    """
    raw = os.environ.get(GATEWAY_PID_ENV, "")
    try:
        return int(raw) == os.getpid()
    except ValueError:
        return False


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
    a tool call mint audit rows, or delete real ones uncounted. This buffer is
    reachable only from inside the gateway process.

    ORDER. A pending row always goes out before a newer one, so the ledger's
    order is the order things happened; ``buffered_at`` carries the real time.

    WHAT STILL COUNTS AS LOST. Overflow (oldest dropped), a gap longer than the
    retry deadline, interpreter exit with rows pending, and an ambiguous send
    during a flush (the row may have landed; sending it again could duplicate
    it, so it is tallied once and dropped, the same stance as
    ``shared.routine_change_spool``). A row that is flushed is never counted.
    """

    def __init__(
        self,
        *,
        max_rows: int = _BUFFER_MAX_ROWS,
        deadline_seconds: float = _RETRY_DEADLINE_SECONDS,
        max_backoff_seconds: float = _RETRY_MAX_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.lock = threading.Lock()
        self._pending: deque[dict[str, Any]] = deque()
        self._max_rows = max_rows
        self._deadline = deadline_seconds
        self._max_backoff = max_backoff_seconds
        self._clock = clock
        self._sleep = sleep
        self._gap_started: float | None = None
        self._retrier: threading.Thread | None = None

    def __len__(self) -> int:
        return len(self._pending)

    def add_locked(self, payload: dict[str, Any]) -> None:
        if len(self._pending) >= self._max_rows:
            self._pending.popleft()
            record_audit_write_failure("audit gap buffer full; oldest buffered row dropped")
        row = _with_buffered_at(payload["row"], _utc_now_iso())
        self._pending.append({**payload, "row": row})
        if self._gap_started is None:
            self._gap_started = self._clock()

    def flush_locked(self, send: Callable[[dict[str, Any]], Any]) -> bool:
        """Send pending rows oldest first. True when the buffer is empty."""
        while self._pending:
            try:
                send(self._pending[0])
            except _BrokerUnreachable:
                return False
            except AuditWriteError:
                pass  # ambiguous or refused: tallied by the constructor, not resent
            self._pending.popleft()
        self._gap_started = None
        return True

    def expire_locked(self) -> bool:
        """Tally and drop everything if the gap outlived the deadline."""
        if self._gap_started is None or self._clock() - self._gap_started < self._deadline:
            return False
        self.abandon_locked(f"audit broker unreachable for over {self._deadline:.0f}s")
        return True

    def abandon_locked(self, reason: str) -> None:
        while self._pending:
            self._pending.popleft()
            record_audit_write_failure(reason)
        self._gap_started = None

    def ensure_retrier_locked(self, send: Callable[[dict[str, Any]], Any]) -> None:
        if self._retrier is not None and self._retrier.is_alive():
            return
        self._retrier = threading.Thread(
            target=self._retry_loop, args=(send,), name="smd-audit-gap-retry", daemon=True
        )
        self._retrier.start()

    def _retry_loop(self, send: Callable[[dict[str, Any]], Any]) -> None:
        backoff = 1.0
        while True:
            self._sleep(backoff)
            with self.lock:
                if self.flush_locked(send) or self.expire_locked():
                    self._retrier = None
                    return
            backoff = min(backoff * 2, self._max_backoff)

    def drain_at_exit(self) -> None:
        if self.lock.acquire(timeout=1.0):
            try:
                if self._pending:
                    self.abandon_locked("audit rows still buffered at interpreter exit")
            finally:
                self.lock.release()


_GAP = _GapBuffer()
atexit.register(_GAP.drain_at_exit)


class BrokerAuditClient:
    """Append-only audit writer that speaks to the capability broker.

    Drop-in for :class:`~shared.d1_client.D1Client` at the
    ``.execute(sql, *params)`` seam. ``sql`` is accepted for signature
    compatibility; only the canonical audit ``INSERT`` is supported. The
    12 positional params are the :data:`~shared.audit_contract.COLUMNS`
    tuple ``(id, ts, action_type, ...)``; the broker stamps ``id``/``ts``
    server-side, so those two leading values are dropped before sending.

    ``buffer_on_unreachable`` (default False) lets the GATEWAY hold rows across
    a broker gap instead of losing them (see :class:`_GapBuffer`). Only the
    audit plugin's observational hooks opt in. Callers whose rule is "a
    transition that cannot be recorded did not happen" (the cost breaker,
    outbound and reply gates) keep the default and still fail on the spot.
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
        """Ship one audit row to the broker. Returns 1 (rows written or held).

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
        if not self._buffer_on_unreachable:
            self._request(payload)
            return 1
        with self._gap.lock:
            if len(self._gap) and not self._gap.flush_locked(self._send):
                self._gap.add_locked(payload)
                return 1
            try:
                self._send(payload)
            except _BrokerUnreachable as exc:
                if not _process_is_gateway():
                    raise AuditWriteError(f"audit broker socket error: {exc}") from exc
                self._gap.add_locked(payload)
                self._gap.ensure_retrier_locked(self._send)
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
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self._timeout)
            try:
                client.connect(self._socket_path)
            except (ConnectionRefusedError, FileNotFoundError) as exc:
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


__all__ = ["SOCKET_ENV", "AuditWriteError", "BrokerAuditClient", "audit_client_from_env"]
