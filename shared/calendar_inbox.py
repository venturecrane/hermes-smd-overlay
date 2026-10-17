"""Emails the calendar consumed before any turn: the webhook router leaves them alone.

``pre_gateway_dispatch`` runs every plugin's callback and only then reads the
results (Hermes ``gateway/run_inbound.py``), so a ``skip`` from the calendar
plugin does not stop the webhook router from running on the same email. Left
alone, the router would record a reply origin, index a request card (and later
page SMD that a calendar robot's "Accepted" was never answered) and write a
WEBHOOK_ROUTED row, for an email nobody will ever see.

So the calendar plugin, which registers ahead of the router, marks the message
id here, and the router's hook returns at once for a marked id.

FAIL OPEN, NARROWLY. A mark is taken once, names one vendor message id, and
expires after :data:`TTL_SECONDS`: the router runs in the same dispatch,
milliseconds later, so a mark that outlives that is a mark nothing should ever
honor. Only the calendar plugin marks, and it is registered only on a seat that
authors the calendar, so on every other seat this register stays empty and
:func:`take` answers False for every email.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

_MAX = 512
TTL_SECONDS = 60.0
_lock = threading.Lock()
_consumed: OrderedDict[str, float] = OrderedDict()


def mark(message_id: str, *, now: float | None = None) -> None:
    if not message_id:
        return
    with _lock:
        _consumed[message_id] = time.monotonic() if now is None else now
        while len(_consumed) > _MAX:
            _consumed.popitem(last=False)


def take(message_id: str, *, now: float | None = None) -> bool:
    """True once for an id marked within :data:`TTL_SECONDS`."""
    if not message_id:
        return False
    with _lock:
        marked = _consumed.pop(message_id, None)
    if marked is None:
        return False
    return ((time.monotonic() if now is None else now) - marked) <= TTL_SECONDS


def _reset_for_tests() -> None:
    with _lock:
        _consumed.clear()


__all__ = ["TTL_SECONDS", "mark", "take"]
