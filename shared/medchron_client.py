"""Overlay client for the chronology-package job seam (routine 11, ss#2614).

Twin of :class:`shared.job_ledger_client.BrokerJobClient`: a thin transport
over the capability broker's Unix socket for the five ``medchron_*`` verbs
the broker exposes (ss-console ``operator/workspace_broker/medchron_verbs.py``).
The broker gates each verb by peer identity (submit: the gateway PID or root;
status and allowance: gateway, agent uid, or root; list: agent uid or root;
record: root only), so an ``execute_code`` child reaching the socket directly
gets a refusal, and the agent can never write a transition itself.

Nothing here carries document content: envelopes in, counts and states out.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any

SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"
_DEFAULT_TIMEOUT_SECONDS = 5.0


class MedchronBrokerError(RuntimeError):
    """A medchron request failed (broker unreachable, refused, or malformed)."""


class MedchronBrokerClient:
    def __init__(
        self, *, socket_path: str | None = None, timeout: float = _DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self._socket_path = socket_path or os.environ.get(SOCKET_ENV, "")
        self._timeout = timeout
        if not self._socket_path:
            raise MedchronBrokerError(f"{SOCKET_ENV} is unset; cannot reach the broker")

    def submit(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Returns the broker's answer: ``accepted`` with a ``job_id`` and the
        allowance remainder, or ``accepted: False`` with a prose ``reason``."""
        return self._request({"action": "medchron_job_submit", "envelope": envelope})

    def status(self, job_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": "medchron_job_status"}
        if job_id:
            payload["job_id"] = job_id
        return self._request(payload)

    def allowance(self) -> dict[str, Any]:
        return self._request({"action": "medchron_allowance"})

    def list_all(self) -> list[dict[str, Any]]:
        return list(self._request({"action": "medchron_job_list"}).get("jobs") or [])

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
            raise MedchronBrokerError(f"broker socket error: {exc}") from exc
        try:
            resp = json.loads(bytes(buf).decode("utf-8") or "{}")
        except ValueError as exc:
            raise MedchronBrokerError("broker returned malformed JSON") from exc
        if not isinstance(resp, dict) or resp.get("ok") is not True:
            err = resp.get("error") if isinstance(resp, dict) else "unknown"
            msg = resp.get("message") if isinstance(resp, dict) else ""
            raise MedchronBrokerError(f"broker refused: {err}: {msg}")
        return resp
