"""Is THIS process the seat's gateway? The gate behind the non-gateway tool wall.

WHY IT EXISTS (pilot-smokeball, 2026-09-21 / found 2026-09-26). On a seat whose
audit ledger sits behind the capability broker, the generic ``audit_append`` verb
is gated to the gateway PID (ss-console ``workspace_broker/verbs.py``). A second
Hermes runtime started on the Machine (``hermes -p operator ...`` over ssh) loads
every plugin, runs real tools against the firm's systems, and has every one of its
audit rows refused. The decision is enforced; the record is not. The seat's own
log shows it: plugin load at 21:09:22, a tool call at 21:09:49, "audit broker
refused append: request did not originate from the gateway process" at 21:09:50.

So a process that is not the gateway runs no tools. It can still reach a model,
and those rows are refused and counted in ``audit_write_failures``; what it cannot
do is act on the firm's records with nothing written down.

WHO DECIDES. The broker, from the socket's peer credentials (``health`` reports
``caller_is_gateway``). That is ground truth rather than a claim the process makes
about itself. Only when the broker cannot answer (unreachable, or an image whose
broker predates the field) does the environment decide, and it decides CLOSED:
``SMD_GATEWAY_PID`` unset or different from ``os.getpid()`` blocks. Unset matters:
the broker socket path is a fly.toml ``[env]`` var, visible to a bare ``fly ssh``
shell, while ``SMD_GATEWAY_PID`` is exported only by the entrypoint.

The wall is off entirely when ``SMD_AUDIT_BROKER_SOCKET`` is unset (direct-D1
transport, local dev, tests): there is no PID gate to fall foul of there.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from typing import Any

logger = logging.getLogger(__name__)

SOCKET_ENV = "SMD_AUDIT_BROKER_SOCKET"
GATEWAY_PID_ENV = "SMD_GATEWAY_PID"
_TIMEOUT_SECONDS = 3.0

BLOCK_MESSAGE = (
    "This Operator process is not the seat's gateway, so the audit ledger cannot "
    "record what it does; no tools run here. Use a cron one-shot, which runs inside "
    "the gateway. Do not retry; end the turn."
)

#: pid -> definitive broker answer. Keyed by pid so a forked child re-asks.
_ANSWERS: dict[int, bool] = {}
_MISMATCH_ALARM_INTERVAL_S = 900.0
_last_alarm: float | None = None


def _ask_broker(socket_path: str) -> bool | None:
    """The broker's ``caller_is_gateway`` for this connection, or None if it
    cannot say. Deliberately NOT ``BrokerAuditClient._request``: that raises
    ``AuditWriteError``, whose constructor tallies a lost audit row, and a health
    probe that fails has lost nothing."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(_TIMEOUT_SECONDS)
            client.connect(socket_path)
            client.sendall(b'{"action":"health"}\n')
            buf = bytearray()
            while not buf.endswith(b"\n"):
                chunk = client.recv(65_536)
                if not chunk:
                    break
                buf.extend(chunk)
        data: Any = json.loads(buf)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    answer = data.get("caller_is_gateway")
    return answer if isinstance(answer, bool) else None


def _env_says_gateway() -> bool:
    raw = os.environ.get(GATEWAY_PID_ENV, "").strip()
    try:
        return int(raw) == os.getpid()
    except ValueError:
        return False


def _alarm_gateway_misidentified() -> None:
    """The broker says no while the environment says this IS the gateway. Every
    audit row this process writes is being refused and every tool is walled, so
    somebody must hear about it. Rate-limited; never raises."""
    global _last_alarm
    now = time.monotonic()
    if _last_alarm is not None and now - _last_alarm < _MISMATCH_ALARM_INTERVAL_S:
        return
    _last_alarm = now
    logger.error(
        "gateway_identity: the broker does not recognise pid %s as the gateway although "
        "%s names it; every tool is refused and every audit row is lost until this is fixed",
        os.getpid(),
        GATEWAY_PID_ENV,
    )
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("component", "gateway-identity")
            sentry_sdk.capture_message(
                "gateway misidentified by the audit broker; tools refused", level="error"
            )
    except Exception:  # noqa: BLE001 — observability must never break the wall
        logger.debug("gateway_identity: sentry note failed", exc_info=True)


def is_gateway() -> bool:
    """True when this process may run tools on a broker-audited seat."""
    socket_path = os.environ.get(SOCKET_ENV, "")
    if not socket_path:
        return True
    pid = os.getpid()
    cached = _ANSWERS.get(pid)
    if cached is not None:
        return cached
    answer = _ask_broker(socket_path)
    if answer is None:
        # The broker could not say. Do not cache: the next call asks again.
        return _env_says_gateway()
    _ANSWERS[pid] = answer
    if not answer and _env_says_gateway():
        _alarm_gateway_misidentified()
    return answer


def wall_block() -> dict[str, str] | None:
    """The ``pre_tool_call`` block directive for a non-gateway process, else None."""
    if is_gateway():
        return None
    return {"action": "block", "message": BLOCK_MESSAGE}


def _reset_for_tests() -> None:
    global _last_alarm
    _ANSWERS.clear()
    _last_alarm = None


__all__ = ["BLOCK_MESSAGE", "is_gateway", "wall_block"]
