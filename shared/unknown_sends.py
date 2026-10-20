"""The structural half of "do not send it again" after an unknown outcome.

A send whose answer was lost (the trust plugin's ``outbound_send.SendOutcomeUnknown``)
may already be out. Telling the model not to resend is an instruction, and an
instruction is not a control. So every such send is remembered here by the
digest of its wire body, and the same body is refused, without a broker call,
for :data:`HOLD_S` afterwards: through the send tool, an approved replay, or any
out-of-turn sender, because all of them reach the broker through
``outbound_send``. A reworded message is not caught; the instruction still
covers that.

THE HOLD IS BOUNDED IN TIME on purpose. A routine can legitimately send the
same text again ("nothing due today"), and a permanent memory would silence it
after one lost answer. An hour covers the retries that actually happen (the
model's, within its turn; a caller's, within its backoff) and is long past the
console reconciler's first look.

AND EVERY UNKNOWN OUTCOME PAGES, here, in the process that knows it happened.
The broker's intent row is what the console reconciler grades, but a broker
that answered after this seat stopped waiting, or failed before writing that
row, leaves nothing to grade, and the agent has just told a person "SMD is
checking".

Per process and in memory, like ``shared.answered_messages``: a restart
forgets it, which costs the backstop for at most one hour, never a send.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

HOLD_S = 3600.0
_MAX = 256
_LOCK = threading.Lock()
_HELD: OrderedDict[str, float] = OrderedDict()


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def hold(body: dict[str, Any], *, now: float | None = None) -> None:
    """Remember that this exact wire body's outcome is unknown."""
    key = _digest(body)
    with _LOCK:
        _HELD[key] = time.monotonic() if now is None else now
        _HELD.move_to_end(key)
        while len(_HELD) > _MAX:
            _HELD.popitem(last=False)


def held(body: dict[str, Any], *, now: float | None = None) -> bool:
    """True when this exact wire body's outcome went unknown within the hold."""
    key = _digest(body)
    now = time.monotonic() if now is None else now
    with _LOCK:
        stamp = _HELD.get(key)
        if stamp is None:
            return False
        if now - stamp >= HOLD_S:
            _HELD.pop(key, None)
            return False
        return True


def page(channel: str) -> None:
    """Page SMD that a send's outcome is unknown. The channel only, never an
    address or a body. Never raises."""
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("reason", "send_outcome_unknown")
            scope.set_tag("channel", channel)
            scope.set_tag(
                "customer",
                os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG") or "",
            )
            sentry_sdk.capture_message("send_outcome_unknown", level="error")
    except Exception as exc:  # noqa: BLE001 - monitoring must never break a send path
        logger.debug("unknown_sends: page skipped (%s)", exc)


def _reset_for_tests() -> None:
    with _LOCK:
        _HELD.clear()


__all__ = ["HOLD_S", "hold", "held", "page"]
