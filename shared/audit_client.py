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

import json
import os
import socket
from typing import Any

from shared.audit_contract import COLUMNS
from shared.audit_failure_counter import record_audit_write_failure

SOCKET_ENV = "SMD_AUDIT_BROKER_SOCKET"
_DEFAULT_TIMEOUT_SECONDS = 5.0


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

    One count = one raise, NOT one permanently-lost row. Nothing retries an
    audit write today (deliberately: a retry queue changes the best-effort
    posture and is out of scope per #2498's non-goals). If a retry path is ever
    added, it must not re-count a row it goes on to persist.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        record_audit_write_failure(str(args[0]) if args else "audit write failed")


class BrokerAuditClient:
    """Append-only audit writer that speaks to the capability broker.

    Drop-in for :class:`~shared.d1_client.D1Client` at the
    ``.execute(sql, *params)`` seam. ``sql`` is accepted for signature
    compatibility; only the canonical audit ``INSERT`` is supported. The
    12 positional params are the :data:`~shared.audit_contract.COLUMNS`
    tuple ``(id, ts, action_type, ...)``; the broker stamps ``id``/``ts``
    server-side, so those two leading values are dropped before sending.
    """

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._socket_path = socket_path or os.environ.get(SOCKET_ENV, "")
        self._timeout = timeout
        if not self._socket_path:
            raise AuditWriteError(f"{SOCKET_ENV} is unset; cannot reach the audit broker")

    def execute(self, sql: str, *params: Any) -> int:
        """Ship one audit row to the broker. Returns 1 (rows written).

        Raises:
            AuditWriteError: the broker refused the append or was unreachable.
        """
        if len(params) != len(COLUMNS):
            raise AuditWriteError(
                f"audit broker: expected {len(COLUMNS)} params for {COLUMNS!r}, got {len(params)}"
            )
        # COLUMNS == (id, ts, action_type, ...). Drop id/ts — the broker
        # re-derives them so the agent cannot backdate or collide.
        row = dict(zip(COLUMNS[2:], params[2:], strict=True))
        self._request({"action": "audit_append", "row": row})
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
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self._timeout)
                client.connect(self._socket_path)
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


def audit_client_from_env(customer_slug: str | None = None) -> Any:
    """Return the audit-log transport for this Machine.

    Broker mode when ``SMD_AUDIT_BROKER_SOCKET`` is set; otherwise a direct
    :class:`~shared.d1_client.D1Client` on ``SMD_D1_AUDIT_BINDING``.
    """
    if os.environ.get(SOCKET_ENV):
        return BrokerAuditClient()
    # Direct mode — import lazily so the broker path carries no D1 dependency.
    from shared.d1_env import d1_client_from_env

    return d1_client_from_env(customer_slug, binding_name="SMD_D1_AUDIT_BINDING")


__all__ = ["SOCKET_ENV", "AuditWriteError", "BrokerAuditClient", "audit_client_from_env"]
