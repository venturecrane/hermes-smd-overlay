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
    """The broker refused or failed a request."""


def request(payload: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Send one newline-delimited JSON request over the broker Unix socket."""
    socket_path = os.environ.get(SOCKET_ENV, "")
    if not socket_path:
        raise BrokerError(f"{SOCKET_ENV} is unset")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall(encoded)
        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = client.recv(65_536)
            if not chunk:
                break
            response.extend(chunk)
    data = json.loads(response)
    if not isinstance(data, dict) or data.get("ok") is not True:
        message = data.get("message") if isinstance(data, dict) else "invalid response"
        raise BrokerError(str(message or data.get("error") or "broker request failed"))
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
