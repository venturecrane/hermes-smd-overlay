"""Emails the calendar consumed before any turn: the webhook router leaves them alone.

``pre_gateway_dispatch`` runs every plugin's callback and only then reads the
results (Hermes ``gateway/run_inbound.py``), so a ``skip`` from the calendar
plugin does not stop the webhook router from running on the same email. Left
alone, the router would record a reply origin, index a request card (and later
page SMD that a calendar robot's "Accepted" was never answered) and write a
WEBHOOK_ROUTED row, for an email nobody will ever see.

So the calendar plugin, which registers ahead of the router, marks the message
id here, and the router's hook returns at once for a marked id. Bounded and
one-shot: a mark is taken once, and the oldest marks fall off.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

_MAX = 512
_lock = threading.Lock()
_consumed: OrderedDict[str, None] = OrderedDict()


def mark(message_id: str) -> None:
    if not message_id:
        return
    with _lock:
        _consumed[message_id] = None
        while len(_consumed) > _MAX:
            _consumed.popitem(last=False)


def take(message_id: str) -> bool:
    """True once for a marked id."""
    if not message_id:
        return False
    with _lock:
        return _consumed.pop(message_id, False) is None


def _reset_for_tests() -> None:
    with _lock:
        _consumed.clear()


__all__ = ["mark", "take"]
