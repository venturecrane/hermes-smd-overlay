"""The seat's trust gate, callable for a tool call no model made.

``hermes-smd-trust``'s ``pre_tool_call`` is where every governed tool call is
decided: the operator-pause wall, the non-gateway wall, the trust ceiling, the
outbound draft gate (banned fabrication markers, fabricated citations, the A1
identifier gate) and the output checklist. Hermes runs it for the model's own
calls only.

The reply channel's end-of-turn text delivery (``hermes-smd-reply``
``text_delivery``) sends a turn's final text as the reply the model did not
draft, and it must pass exactly the gate a drafted reply would have. Rather than
re-implement that gate in a second place (where it would drift), the trust
plugin publishes its own hook here at register time, the way it publishes its
internal sender through :mod:`shared.send_dispatch`, and the reply channel asks
it about the draft call it is standing in for.

FAIL CLOSED. With no gate published, or a gate that raises, the answer is a
block: a reply the trust gate could not look at does not go out.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: ``(**kwargs) -> {"action": "block", "message": ...} | None``, the
#: ``pre_tool_call`` contract.
Gate = Callable[..., Any]

_LOCK = threading.Lock()
_GATE: Gate | None = None

NOT_WIRED = "no trust gate is wired on this seat"


def set_gate(gate: Gate | None) -> None:
    """Publish the seat's trust ``pre_tool_call``. Last writer wins (re-register)."""
    global _GATE
    with _LOCK:
        _GATE = gate


def evaluate(
    tool_name: str, args: dict[str, Any], *, session_id: str, tool_call_id: str
) -> dict[str, Any] | None:
    """The trust gate's verdict on a call: a block directive, or ``None`` to allow.

    ``args`` may be mutated by the gate exactly as it mutates a model call's
    args (it strips forged control keys and attaches a report's html half), so
    the caller sends what the gate returns it, not a copy taken before."""
    with _LOCK:
        gate = _GATE
    if gate is None:
        return {"action": "block", "message": NOT_WIRED}
    try:
        verdict = gate(
            tool_name=tool_name,
            args=args,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    except Exception as exc:  # noqa: BLE001 - a gate that raised did not allow
        logger.warning("tool_gate: the trust gate raised (%s); blocking", type(exc).__name__)
        return {"action": "block", "message": f"the trust gate raised {type(exc).__name__}"}
    if isinstance(verdict, dict) and verdict.get("action") == "block":
        return verdict
    return None


__all__ = ["NOT_WIRED", "Gate", "evaluate", "set_gate"]
