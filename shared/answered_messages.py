"""Which inbound emails this process has already sent something in answer to.

A turn can answer the email that opened it by more than one path: the reply
channel relaying the model's draft (``hermes-smd-reply``), the seat delivering
an act line, the space gate replying in the thread to ask for a confirmation
(``hermes-smd-space-gate.email_confirm``), a code-sent notice anchored to the
email. Every one of them reaches the broker through
:func:`shared.agentmail_broker._call` with the email's anchor, so the broker
client notes the anchor here and any later question "did this turn already
answer that email" has one place to ask.

The question exists for the end-of-turn text delivery (``hermes-smd-reply``
``text_delivery``): the seat sends a turn's final text to the sender only when
NOTHING answered the email, because a second email in reply to one message is a
defect the person sees.

A transmit whose outcome is unknown (the socket timed out after the broker may
have sent) is noted too. Delivering the text again on a maybe would risk the
duplicate, and the broker's own row says what happened.

Process-wide, bounded, thread-safe. Attribution only: message ids, never bodies.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

_MAX = 1024
_LOCK = threading.Lock()
_ANSWERED: OrderedDict[str, None] = OrderedDict()

#: The anchor keys that name the email a send answers (``shared.send_anchor``).
_ANCHOR_KEYS = ("message_id", "graph_message_id")


def note(message_id: str) -> None:
    """Record that something was sent (or may have been) in answer to ``message_id``."""
    mid = str(message_id or "").strip()
    if not mid:
        return
    with _LOCK:
        _ANSWERED[mid] = None
        _ANSWERED.move_to_end(mid)
        while len(_ANSWERED) > _MAX:
            _ANSWERED.popitem(last=False)


def note_anchor(anchor: Any) -> None:
    """Note the email an anchor names, if it names one. Never raises."""
    if not isinstance(anchor, dict):
        return
    for key in _ANCHOR_KEYS:
        value = anchor.get(key)
        if isinstance(value, str) and value.strip():
            note(value)


def answered(message_id: str) -> bool:
    """Whether anything was sent in answer to ``message_id`` in this process."""
    mid = str(message_id or "").strip()
    if not mid:
        return False
    with _LOCK:
        return mid in _ANSWERED


def _reset_for_tests() -> None:
    with _LOCK:
        _ANSWERED.clear()


__all__ = ["answered", "note", "note_anchor"]
