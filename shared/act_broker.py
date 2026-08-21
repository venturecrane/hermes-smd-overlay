"""The wire to the broker's act verbs (ss-console operator-own-matter).

Separate from :mod:`shared.pending_acts` on purpose: that module is a register
with no I/O, which is what makes the approval invariants testable without a
socket. This one is the two calls that cross the process boundary, plus the
closed vocabulary of what may be sent.

WHY THE VOCABULARY IS CLOSED. ``act_propose`` writes a row that an administrator
will be asked to approve with one word. The model's arguments never reach it:
the trust gate reads the authored block out of the root-owned customer.yaml and
sends exactly the keys named here, and the broker re-reads the same block and
refuses anything that does not match. Two independent readings of one authored
file, which is the property that makes "yes" mean the matter the firm authored
rather than the matter the model asked for.

TWO KEY SETS, AND THE DIFFERENCE MATTERS. The PAYLOAD is the whole authored
block, names included, because that is the thing the two readings compare and the
thing the committed row records. The ARGUMENTS are the subset the vendor's API
actually accepts. The authored names exist so the broker can render a read-back a
person can judge ("client: Ashton and Price") rather than a UUID; sending them on
to the connector would be sending it fields it does not have.

WHY NOT ``shared.workspace_broker.request``. That helper raises on ``ok != True``,
and a broker refusal here has to reach the caller as the sentence it is, so the
seat can tell the administrator what was refused instead of turning it into an
exception string. Same posture, and the same framing, as the establishment
plugin's own ``_broker_request``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

logger = logging.getLogger(__name__)

SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"
TIMEOUT_SECONDS = 15.0

ACTION_PROPOSE = "act_propose"
ACTION_COMMIT = "act_commit"

#: The broker's ``kind`` for an act row. Rule rows carry ``"rule"``; the two
#: share one store and one confirmation channel, so the kind is how a consumer
#: tells a sentence to install from a call to make.
KIND_TOOL_CALL = "tool_call"

#: The only payload keys any act may carry, per tool: the authored block, names
#: included. Mirrors the broker's own ``ACT_TOOLS``. A key absent from the
#: authored block is simply omitted; anything not listed here is never sent,
#: whoever authored it.
ACT_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "mcp_smokeball_create_matter": (
        "description",
        "matter_type_id",
        "client_contact_id",
        "number",
        "client_contact_name",
        "matter_type_name",
    ),
}

#: The subset of the payload the TOOL is called with. The authored names are for
#: the read-back the administrator judges; the connector takes ids.
ACT_ARG_KEYS: dict[str, frozenset[str]] = {
    "mcp_smokeball_create_matter": frozenset(
        {"description", "matter_type_id", "client_contact_id", "number"}
    ),
}

#: Keys without which the act cannot be proposed at all. ``number`` is optional
#: at the vendor, so an authored block that omits it is still complete. The names
#: are optional too: without them the read-back falls back to ids, which is
#: unreadable but honest.
ACT_REQUIRED_KEYS: dict[str, frozenset[str]] = {
    "mcp_smokeball_create_matter": frozenset(
        {"description", "matter_type_id", "client_contact_id"}
    ),
}


def tool_arguments(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The payload projected onto the keys the tool itself accepts.

    An unknown tool projects to nothing rather than passing the payload through:
    a commitment whose argument shape this module does not know is one it must
    not be replaying.
    """
    keys = ACT_ARG_KEYS.get(tool_name)
    if not keys:
        return {}
    return {key: value for key, value in payload.items() if key in keys}


def is_act_tool(tool_name: Any) -> bool:
    """True for a COMMITMENT tool this channel knows how to propose."""
    return isinstance(tool_name, str) and tool_name in ACT_PAYLOAD_KEYS


def verdict(payload: dict[str, Any]) -> dict[str, Any]:
    """One request over the broker's unix socket, verdict returned verbatim.

    Raises only on a transport fault (no socket configured, a connect or decode
    failure). A refusal is a value, not an exception.
    """
    socket_path = os.environ.get(SOCKET_ENV, "")
    if not socket_path:
        raise RuntimeError(f"{SOCKET_ENV} is unset; cannot reach the broker")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(TIMEOUT_SECONDS)
        sock.connect(socket_path)
        sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        raw = b""
        while not raw.endswith(b"\n"):
            chunk = sock.recv(65_536)
            if not chunk:
                break
            raw += chunk
    decoded = json.loads(raw.decode("utf-8"))
    return decoded if isinstance(decoded, dict) else {"ok": False, "refused": "malformed verdict"}


def propose(
    *,
    tool: str,
    payload: dict[str, Any],
    instructed_by: str,
    source_ref: str,
) -> dict[str, Any]:
    """Ask the broker to mint a proposal for one authored act.

    ``payload`` is the authored block whole, names included. The hook resolves
    nothing and looks nothing up: the broker renders the read-back from the same
    authored names, so what the administrator reads and what the seat sent came
    from one file read twice, with no third version composed in between.
    """
    return verdict(
        {
            "action": ACTION_PROPOSE,
            "tool": tool,
            "payload": payload,
            "instructed_by": instructed_by,
            "source_ref": source_ref,
        }
    )


def commit(
    *,
    proposal_id: str,
    tool: str,
    payload: dict[str, Any],
    confirmed_by: str,
    confirmed_message_id: str,
    ok: bool,
    ref: str,
) -> dict[str, Any]:
    """Close the broker row against what the vendor actually recorded."""
    return verdict(
        {
            "action": ACTION_COMMIT,
            "proposal_id": proposal_id,
            "tool": tool,
            "payload": payload,
            "confirmed_by": confirmed_by,
            "confirmed_message_id": confirmed_message_id,
            "outcome": {"ok": bool(ok), "ref": ref},
        }
    )


__all__ = [
    "ACTION_COMMIT",
    "ACTION_PROPOSE",
    "ACT_ARG_KEYS",
    "ACT_PAYLOAD_KEYS",
    "ACT_REQUIRED_KEYS",
    "KIND_TOOL_CALL",
    "SOCKET_ENV",
    "commit",
    "is_act_tool",
    "propose",
    "tool_arguments",
    "verdict",
]
