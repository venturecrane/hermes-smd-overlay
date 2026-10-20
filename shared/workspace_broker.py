"""Synchronous client for the local Workspace capability broker."""

from __future__ import annotations

import json
import os
import socket
from typing import Any

SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"
GRANT_ARG = "_smd_workspace_grant"
DEFAULT_TIMEOUT_SECONDS = 30.0


class BrokerError(RuntimeError):
    """The broker refused or failed a request.

    ``code`` is the reply's ``error`` field: the class name of what the broker
    raised (ss-console ``workspace_broker/request_errors.py``), or
    ``internal_error``; ``invalid_response`` when the reply was JSON but not an
    object. Empty when the refusal happened here, before anything was sent.
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class BrokerNotReached(OSError):
    """``connect()`` failed, so nothing was delivered to the broker.

    An ``OSError`` so every caller that reads any socket fault as "broker
    unavailable" is unchanged. The distinction exists for a SEND, where it
    separates two answers: a request that never reached the broker was certainly
    not sent and may be tried again; one that failed after it was written has an
    unknown outcome and must not be.
    """


class BrokerReplyUnreadable(ValueError):
    """The request was written and the reply was empty or not JSON.

    A ``ValueError`` because that is what ``json.loads`` raised here before this
    class existed, so callers catching it are unchanged. For a send it means the
    outcome is unknown: the broker may have sent and then died.
    """


#: The code of the one :class:`BrokerError` raised here before any socket is
#: opened: no broker path is configured, so nothing can have been sent.
NOT_CONFIGURED = "not_configured"

#: Reply ``error`` codes after which a SEND's outcome is unknown, so the caller
#: must not send it again.
#:
#: A transport error is raised by the broker's transmit verbs both before the
#: vendor is called (no credential, a token that could not be minted) and after
#: (a timeout, a 5xx, a reset), and the reply does not say which, so it counts
#: as unknown: the cost of that is a definite failure reported as "checking",
#: never a duplicate. ``internal_error`` is an exception the broker did not
#: raise on purpose, which can happen after the vendor call too.
#: ``invalid_response`` is a reply that is not an object, and ``""`` is a reply
#: with ``ok: false`` and no code at all: neither says what happened.
#:
#: Every other code is a refusal the broker wrote on purpose before transmitting
#: (``AgentMailRefused`` / ``MsGraphRefused``, ``PermissionError`` and
#: ``ValueError`` from its gates, ``request_too_large``), or ``not_configured``.
UNKNOWN_SEND_OUTCOME_CODES = frozenset(
    {
        "AgentMailTransportError",
        "MsGraphTransportError",
        "internal_error",
        "invalid_response",
        "",
    }
)


def send_outcome_unknown(exc: BrokerError) -> bool:
    """True when a send the broker answered with ``exc`` may still have gone out."""
    return getattr(exc, "code", "") in UNKNOWN_SEND_OUTCOME_CODES


def request(payload: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Send one newline-delimited JSON request over the broker Unix socket.

    Raises :class:`BrokerNotReached` when nothing was delivered, another
    ``OSError`` for a fault after the request may have been written,
    :class:`BrokerReplyUnreadable` for an empty or non-JSON reply, and
    :class:`BrokerError` for a reply that is not ``ok``.
    """
    socket_path = os.environ.get(SOCKET_ENV, "")
    if not socket_path:
        raise BrokerError(f"{SOCKET_ENV} is unset", code=NOT_CONFIGURED)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        try:
            client.connect(socket_path)
        except OSError as exc:
            # EVERY connect() failure, not a chosen few: nothing is written
            # until sendall below, so no fault here can have delivered the
            # request (refused, no socket file, EACCES, a full backlog, a reset
            # in the handshake). AF_UNIX has no EINPROGRESS, so a connect()
            # does not time out halfway through a delivery either.
            raise BrokerNotReached(exc.errno, f"broker not reached: {exc.strerror or exc}") from exc
        client.sendall(encoded)
        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = client.recv(65_536)
            if not chunk:
                break
            response.extend(chunk)
    try:
        data = json.loads(response)
    except ValueError as exc:
        raise BrokerReplyUnreadable("the broker's reply was empty or not JSON") from exc
    if not isinstance(data, dict):
        raise BrokerError("invalid response", code="invalid_response")
    if data.get("ok") is not True:
        code = str(data.get("error") or "")
        raise BrokerError(str(data.get("message") or code or "broker request failed"), code=code)
    return data


def authorize(
    operation: str,
    payload: dict[str, Any],
    *,
    customer_slug: str,
    session_id: str,
    tool_call_id: str,
) -> dict[str, Any]:
    """Mint a short-lived, single-use grant for one exact tool payload."""
    return request(
        {
            "action": "authorize",
            "operation": operation,
            "payload": payload,
            "customer_slug": customer_slug,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
        }
    )


def execute(operation: str, payload: dict[str, Any], grant: str) -> dict[str, Any]:
    """Redeem a grant and execute the corresponding provider operation."""
    return request(
        {
            "action": "execute",
            "operation": operation,
            "payload": payload,
            "grant": grant,
        }
    )
