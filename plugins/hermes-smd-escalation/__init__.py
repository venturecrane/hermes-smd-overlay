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

# The tool derives item_key + token from the IDENTITY COMPONENTS — the agent
# never hashes. The first live probe (ss #1915 WP-D) proved why: with item_key
# as an opaque string parameter, the model wrote a human-readable colon-joined
# composite; the broker accepted it, and the pre_run gate's sha256 join could
# never match it — the suppression state silently forked. The tuple here MUST
# be the exact tuple the skill's pre_run computes (see each skill's SKILL.md
# for its authored_date convention — the chase uses None, identity rides on
# the stable task source_id).
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
        "source_id": {
            **NULLABLE_STRING,
            "description": (
                "The item's STABLE Smokeball task/event id (the anti-collision "
                "identity field). null only for items with no stable id — those "
                "get no per-item token (blanket-ack-only group)."
            ),
        },
        "label": {
            "type": "string",
            "description": (
                "The item's fixed label, exactly as the skill's pre_run computes "
                "it (e.g. 'client-verification', or the deadline label)."
            ),
        },
        "authored_date": {
            **NULLABLE_STRING,
            "description": (
                "The authored date component of the identity tuple (YYYY-MM-DD), "
                "or null when the skill's identity convention omits it (the "
                "verification chase uses null — a re-dated tracking task must "
                "not change identity)."
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
        "ack_token": {
            **NULLABLE_STRING,
            "description": (
                "For acked events ONLY: the ACK-XXXXXX code quoted in the reply. "
                "The tool resolves it to its item against the ledger's prior "
                "raises — pass this INSTEAD of the identity components."
            ),
        },
    },
    "required": ["skill", "event", "attempt"],
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


def _resolve_token_identity(ack_token: str) -> tuple[str, str | None]:
    """Resolve an ACK token to (item_key, matter_id) via the ledger's prior
    raises. Raises ValueError when no prior raise carries the token — the same
    verdict the broker would return, surfaced before a malformed event ships."""
    path = os.environ.get(_LEDGER_PATH_ENV) or escalation_ledger.DEFAULT_LEDGER_PATH
    for event in reversed(escalation_ledger.read_ledger(path)):
        if event.get("token") == ack_token and event.get("event") in ("fired", "chased"):
            return str(event.get("item_key")), event.get("matter_id")
    raise ValueError(
        f"no prior raise carries token {ack_token!r}; an alarm that never rang cannot be acked"
    )


def _escalation_append(args: dict[str, Any], **_: Any) -> str:
    kind = str(args["event"])
    ack_token = args.get("ack_token")
    if ack_token:
        if kind != "acked":
            raise ValueError("ack_token is only valid for acked events")
        key, matter_id = _resolve_token_identity(str(ack_token))
        token: str | None = str(ack_token)
    else:
        # Derived, never model-authored: the sha256 identity key and its ACK
        # token come from the same vendored helpers the pre_run gates use, so
        # the join can never fork on a hand-typed key (the first live probe's
        # failure mode: a colon-joined composite the sha256 join never matched).
        label = args.get("label")
        if not label:
            raise ValueError("label is required (with matter_id/source_id) unless acking by token")
        matter_id = None if args.get("matter_id") is None else str(args["matter_id"])
        source_id = None if args.get("source_id") is None else str(args["source_id"])
        authored_date = (
            None if args.get("authored_date") in (None, "") else str(args["authored_date"])
        )
        key = escalation_ledger.item_key(matter_id or "", source_id, str(label), authored_date)
        token = escalation_ledger.token_for(key) if source_id is not None else None
    event = {
        "v": escalation_ledger.SCHEMA_VERSION,
        "ts": None,  # broker stamps ts/id server-side; the agent cannot backdate
        "skill": str(args["skill"]),
        "matter_id": matter_id,
        "item_key": key,
        "event": kind,
        "attempt": int(args["attempt"]),
        "token": token,
    }
    response = _broker_request({"action": "escalation_event_append", "event": event})
    # The broker's verdict (ok/id or a validation error) goes back verbatim —
    # a rejected acked-with-no-prior-raise must stay visible to the turn. Echo
    # the derived identity so the turn can quote the token in the alert body.
    if isinstance(response, dict):
        response = {**response, "item_key": key, "token": token}
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
