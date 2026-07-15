"""Escalation-ledger tools — the agent's mediated door to the escalation state.

WP-A (ss #1894) shipped the escalation ledger with an append procedure that ran
through ``execute_code``: an LLM-turn python snippet opening the broker socket.
The WP-D live proof (ss #1915) found that path dead on the pilot seat — the
``code_execution`` action class has no authored exposure, so the trust layer
refuses the snippet (fail-closed, ADR 0056, correctly) and the agent can never
write ``fired``/``chased``/``acked``/``handed_off``. Tokens suppress nothing,
attempts never count.

These two tools close the gap without widening any entitlement:

* ``escalation_append`` — one validated event through the broker's uid-gated
  ``escalation_event_append`` verb (the broker still owns ALL validation: schema,
  event vocabulary, and the acked-must-reference-a-prior-raise rule). The tool is
  a socket courier, not a second validator.
* ``escalation_state`` — folds the read-only ledger twin
  (``/opt/data/audit/escalation-ledger.jsonl``; the hermes uid is in
  ``audit-readers``) into per-item state + ACK tokens via the vendored
  ``shared/escalation_ledger`` module (byte-identical twin of
  ``operator/workspace_broker/escalation_ledger.py``).

Both tools are mapped in ``shared/action_classes.py`` (``internal_write`` /
``read``) — an unmapped tool is REFUSED by design, which is exactly how the
execute_code gap surfaced.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import asdict
from typing import Any

from shared import escalation_ledger
from shared.tool_registration import register_wrapped_tool

logger = logging.getLogger(__name__)

_SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"
_LEDGER_PATH_ENV = "SMD_ESCALATION_LEDGER_PATH"
_TIMEOUT_SECONDS = 10

STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}

_APPEND_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {
            "type": "string",
            "description": "The skill writing the event (e.g. deadline-miss-escalator).",
        },
        "matter_id": {
            **NULLABLE_STRING,
            "description": "Smokeball matter id; null for seat-level sentinel items.",
        },
        "item_key": {
            "type": "string",
            "description": (
                "Stable per-item key from item_key(matter_id, task_id, label, "
                "authored_date) — the same tuple the pre_run gate computes."
            ),
        },
        "event": {
            "type": "string",
            "enum": ["fired", "chased", "acked", "handed_off", "resolved"],
            "description": "The ledger event kind.",
        },
        "attempt": {
            "type": "integer",
            "minimum": 0,
            "description": "The attempt number this raise carries (0 for non-raises).",
        },
        "token": {
            **NULLABLE_STRING,
            "description": (
                "ACK-XXXXXX token for fired/chased raises and for the acked event "
                "that references them; omit for items with no stable identity."
            ),
        },
    },
    "required": ["skill", "item_key", "event", "attempt"],
    "additionalProperties": False,
}

_STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {
            **NULLABLE_STRING,
            "description": "Optional filter: only events written by this skill.",
        },
    },
    "required": [],
    "additionalProperties": False,
}


def _broker_request(payload: dict[str, Any]) -> dict[str, Any]:
    socket_path = os.environ.get(_SOCKET_ENV, "")
    if not socket_path:
        raise RuntimeError(f"{_SOCKET_ENV} is unset; cannot reach the broker")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_TIMEOUT_SECONDS)
        sock.connect(socket_path)
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        raw = b""
        while not raw.endswith(b"\n"):
            chunk = sock.recv(65_536)
            if not chunk:
                break
            raw += chunk
    return json.loads(raw.decode("utf-8"))


def _escalation_append(args: dict[str, Any], **_: Any) -> str:
    event = {
        "v": escalation_ledger.SCHEMA_VERSION,
        "ts": None,  # broker stamps ts/id server-side; the agent cannot backdate
        "skill": str(args["skill"]),
        "matter_id": None if args.get("matter_id") is None else str(args["matter_id"]),
        "item_key": str(args["item_key"]),
        "event": str(args["event"]),
        "attempt": int(args["attempt"]),
        "token": None if args.get("token") is None else str(args["token"]),
    }
    response = _broker_request({"action": "escalation_event_append", "event": event})
    # The broker's verdict (ok/id or a validation error) goes back verbatim —
    # a rejected acked-with-no-prior-raise must stay visible to the turn.
    return json.dumps(response, ensure_ascii=False)


def _state_to_jsonable(state: Any) -> dict[str, Any]:
    row = asdict(state)
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            row[key] = value.isoformat()
    return row


def _escalation_state(args: dict[str, Any], **_: Any) -> str:
    path = os.environ.get(_LEDGER_PATH_ENV) or escalation_ledger.DEFAULT_LEDGER_PATH
    events = escalation_ledger.read_ledger(path)
    skill = args.get("skill")
    if skill:
        events = [e for e in events if e.get("skill") == skill]
    states = escalation_ledger.derive_state(events)
    items = {}
    for key, state in states.items():
        row = _state_to_jsonable(state)
        row["token"] = row.get("token") or escalation_ledger.token_for(key)
        items[key] = row
    return json.dumps(
        {"event_count": len(events), "item_count": len(items), "items": items},
        ensure_ascii=False,
    )


TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "escalation_append": (
        "Append one escalation-ledger event (fired/chased/acked/handed_off/resolved) "
        "through the validated broker seam. The broker stamps ts/id and rejects an "
        "acked event whose token has no prior raise.",
        _APPEND_SCHEMA,
        _escalation_append,
    ),
    "escalation_state": (
        "Read the escalation ledger and return per-item state (attempts, last raise, "
        "acked/handed_off/resolved, ACK token), optionally filtered by skill.",
        _STATE_SCHEMA,
        _escalation_state,
    ),
}


def register(ctx: Any) -> None:
    """Register the escalation-ledger tools. Both require the broker socket env
    (the state read wants the same Machine layout even though it reads a file)."""
    for name, (description, schema, handler) in TOOLS.items():
        register_wrapped_tool(
            ctx,
            name=name,
            toolset="escalation",
            schema=schema,
            handler=handler,
            requires_env=[_SOCKET_ENV],
            description=description,
            emoji="",
        )
    logger.info("hermes-smd-escalation registered %d ledger tools", len(TOOLS))
