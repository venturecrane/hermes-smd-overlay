"""Paired decision and execution audit rows for mediated connector calls."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from shared.audit_client import audit_client_from_env
from shared.audit_contract import INSERT_SQL, agent_event_params


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def write_decision(
    *,
    operation: str,
    payload_digest: str,
    session_id: str,
    tool_call_id: str,
) -> None:
    """Persist the allowed trust decision before the grant can be redeemed."""
    client = audit_client_from_env()
    params = agent_event_params(
        action_type="BROKER_DECISION_ALLOWED",
        metadata={
            "operation": operation,
            "payload_digest": payload_digest,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
        },
    )
    client.execute(INSERT_SQL, *params)


def write_execution(*, operation: str, receipt: dict[str, Any]) -> None:
    """Persist digest-only evidence from the broker-signed execution receipt."""
    client = audit_client_from_env()
    params = agent_event_params(
        action_type="BROKER_EXECUTED",
        metadata={
            "operation": operation,
            "payload_digest": receipt.get("payload_digest"),
            "receipt_digest": _digest(receipt),
            "executed_at": receipt.get("executed_at"),
            "duration_ms": receipt.get("duration_ms"),
        },
    )
    client.execute(INSERT_SQL, *params)
