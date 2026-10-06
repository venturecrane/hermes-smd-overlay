"""Overlay client for the B1 durable-job control plane (ADR 0051).

Twin of :class:`shared.audit_client.BrokerAuditClient`: a thin transport over
the capability broker's Unix socket. The broker (``workspace_broker/server.py``)
holds the only read-write handle on the job ledger and gates every ``job_*``
verb on ``peer_pid == gateway_pid`` — so only the gateway process (which hosts
the durable worker thread) can drive the ledger. An ``execute_code`` child gets
a different peer PID and is refused, so the agent cannot claim leases, raise
budgets, flip job status, or mark a side-effecting step done by reaching the
socket directly.

Lease timing is stamped server-side by the broker (``now_and_lease_cutoff``),
so this client never sends a clock value — a worker cannot lie its lease alive.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable
from typing import Any

from shared.audit_client import _UNREACHABLE_AT_CONNECT

SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"
_DEFAULT_TIMEOUT_SECONDS = 5.0
#: How long one request keeps re-trying a connect() the broker refused. The
#: broker respawns in about 2s; this rides that out with room to spare. It is
#: safe for EVERY verb, writes included, because a refused connect proves the
#: request never left this process: nothing can have been half-applied.
_CONNECT_RETRY_SECONDS = 10.0
_CONNECT_RETRY_INTERVAL_SECONDS = 0.5


class JobLedgerError(RuntimeError):
    """A job-ledger request failed (broker unreachable, refused, or malformed)."""


class BrokerJobClient:
    """Speaks the ``job_*`` verbs to the capability broker over its Unix socket.

    Every method maps to one broker verb and returns the unwrapped result.
    Raises :class:`JobLedgerError` on transport failure or a broker refusal.
    """

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        connect_retry_seconds: float = _CONNECT_RETRY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._socket_path = socket_path or os.environ.get(SOCKET_ENV, "")
        self._timeout = timeout
        self._connect_retry_seconds = connect_retry_seconds
        self._sleep = sleep
        self._clock = clock
        if not self._socket_path:
            raise JobLedgerError(f"{SOCKET_ENV} is unset; cannot reach the job broker")

    # -- intake / read -----------------------------------------------------
    def create(self, row: dict[str, Any]) -> str:
        """Create a queued job from caller-supplied create columns. Returns the
        broker-stamped job id."""
        return str(self._request({"action": "job_create", "row": row})["id"])

    def read(self, job_id: str) -> dict[str, Any] | None:
        return self._request({"action": "job_read", "job_id": job_id}).get("job")

    def list_claimable(self) -> list[dict[str, Any]]:
        return list(self._request({"action": "job_list_claimable"}).get("jobs") or [])

    def list_all(self) -> list[dict[str, Any]]:
        """Every job row, newest-created first (terminal + live). Backs the
        observability seam (the ``jobs`` runtime-read kind); unlike
        ``list_claimable`` it applies no lease/terminal filter."""
        return list(self._request({"action": "job_list"}).get("jobs") or [])

    # -- lease / fencing ---------------------------------------------------
    def claim(self, job_id: str, worker_id: str) -> int | None:
        """Atomically claim the job. Returns the new ``lease_epoch`` the worker
        must carry on every privileged write, or ``None`` if not claimable."""
        resp = self._request({"action": "job_claim", "job_id": job_id, "worker_id": worker_id})
        epoch = resp.get("lease_epoch")
        return int(epoch) if epoch is not None else None

    def heartbeat(self, job_id: str, lease_epoch: int) -> bool:
        return bool(
            self._request(
                {"action": "job_heartbeat", "job_id": job_id, "lease_epoch": lease_epoch}
            )["result"]
        )

    def record(self, job_id: str, lease_epoch: int, fields: dict[str, Any]) -> bool:
        """Epoch-fenced progress write. Returns False if the caller's epoch is
        stale (its write was a no-op) — the worker must then stop."""
        return bool(
            self._request(
                {
                    "action": "job_record",
                    "job_id": job_id,
                    "lease_epoch": lease_epoch,
                    "fields": fields,
                }
            )["result"]
        )

    def cancel(self, job_id: str) -> bool:
        return bool(self._request({"action": "job_cancel", "job_id": job_id})["result"])

    # -- idempotency (record-key-before-effect) ----------------------------
    def idem_begin(self, job_id: str, step_key: str, lease_epoch: int) -> str:
        """Returns 'proceed' | 'skip' | 'review' (see JobLedgerWriter)."""
        return str(
            self._request(
                {
                    "action": "job_idem_begin",
                    "job_id": job_id,
                    "step_key": step_key,
                    "lease_epoch": lease_epoch,
                }
            )["decision"]
        )

    def idem_complete(self, job_id: str, step_key: str, lease_epoch: int) -> bool:
        return bool(
            self._request(
                {
                    "action": "job_idem_complete",
                    "job_id": job_id,
                    "step_key": step_key,
                    "lease_epoch": lease_epoch,
                }
            )["result"]
        )

    # -- transport ---------------------------------------------------------
    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One request, riding out a broker respawn.

        A job-ledger write is synchronous and its answer matters (the epoch
        fence), so it cannot be held in memory the way the gateway's audit rows
        are (overlay#416). What it can do is wait: a connect() the broker
        refused sent nothing, so re-trying it for a few seconds is exactly as
        safe as the first try. Without this, a ``record`` that lands in the 2s
        respawn gap after a paid segment raised, and that segment's spend and
        rotated session tip never reached the ledger.
        """
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        deadline = self._clock() + self._connect_retry_seconds
        while True:
            try:
                buf = self._send_once(encoded)
                break
            except _UNREACHABLE_AT_CONNECT as exc:
                if self._clock() >= deadline:
                    raise JobLedgerError(f"job broker socket error: {exc}") from exc
                self._sleep(_CONNECT_RETRY_INTERVAL_SECONDS)
            except OSError as exc:
                raise JobLedgerError(f"job broker socket error: {exc}") from exc
        try:
            data = json.loads(buf)
        except ValueError as exc:
            raise JobLedgerError("job broker returned invalid JSON") from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            message = (data.get("message") or data.get("error")) if isinstance(data, dict) else None
            raise JobLedgerError(f"job broker refused request: {message or 'unknown error'}")
        return data

    def _send_once(self, encoded: bytes) -> bytearray:
        """Connect, send, read one reply. A connect-time refusal propagates as
        itself (one of ``_UNREACHABLE_AT_CONNECT``) so ``_request`` can retry
        it; any OSError after connect becomes :class:`JobLedgerError` at once
        and is NOT retried, because by then the broker may already have applied
        the request."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self._timeout)
            client.connect(self._socket_path)
            try:
                client.sendall(encoded)
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    chunk = client.recv(65_536)
                    if not chunk:
                        break
                    buf.extend(chunk)
            except OSError as exc:
                # Past connect(), even a reset is not retry-safe: raise now.
                raise JobLedgerError(f"job broker socket error: {exc}") from exc
        return buf


__all__ = ["SOCKET_ENV", "JobLedgerError", "BrokerJobClient"]
