"""The seat delivers an act line the model did not.

THE LIVE FAILURE (pilot-smokeball, 2026-09-25, session
20260925_214749_dbc67acb). An administrator emailed a request; the trust gate
withheld the call and minted ``[act 527116d6]``; the model was told to put the
line in its reply and instead ended the turn with the line as its final TEXT.
On an email seat a turn's final text reaches nobody, so the administrator was
never asked and the act sat unanswerable. An instruction had already failed on
the surface it exists to protect, and repeating it more firmly is not a control.

WHAT THIS IS. At the end of every turn (``post_llm_call`` for a completed turn,
``on_session_end`` for any turn, so an interrupted turn is covered too), the
reply plugin asks one question: did this session propose an act whose line has
NOT gone out in a reply this plugin actually transmitted? If so, the seat sends
the line itself, threaded to the inbound message that opened the turn, through
the same broker reply verb the relay uses (roster-checked and audited by the
broker). If that send fails, it is loud: ``REPLY_FAILED`` on the ledger and an
error-level Sentry event, never silence.

WHY "TRANSMITTED", NOT "PASSED THE READBACK GATE". The establishment plugin
marks a line delivered when a send-shaped tool call's ARGUMENTS carry it, at
``pre_tool_call``. That is before the trust gate and before this plugin's own
floors; a draft blocked after that point, or held here, carried the line and
still sent nothing. So the evidence this module trusts is the body of a reply
the relay handed to the transport (or durably queued for release), recorded by
:meth:`TransmittedBodies.note`.

WHY THE TEXT IS NOT RE-GATED. Every floor this plugin re-applies (fabrication,
content, matter identity) exists to catch what the MODEL composed. The line is
rendered by the workspace broker from the stored act payload; the sentence
above it is fixed here. Nothing in the body is model-authored, and the
recipient is structural (the thread's original sender, whom the trust gate
already required to be an authored administrator before it would propose).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

#: The fixed sentence the seat puts above the broker's line. No commitment, no
#: date, no name: it says only that the Operator is waiting on this answer.
PREAMBLE = (
    "The Operator is holding the action below until an administrator confirms it. "
    "Nothing has been done yet. To proceed, reply to this email with the words "
    "the line asks for; to stop, reply no."
)

_MAX_SESSIONS = 128
_MAX_BODIES = 8


class TransmittedBodies:
    """Per-session bodies of replies the relay actually transmitted or queued."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bodies: OrderedDict[str, list[str]] = OrderedDict()
        self._delivered: OrderedDict[str, set[str]] = OrderedDict()

    def note(self, session_id: str, *parts: str) -> None:
        if not session_id:
            return
        body = "\n".join(p for p in parts if isinstance(p, str) and p)
        if not body:
            return
        with self._lock:
            bucket = self._bodies.setdefault(session_id, [])
            bucket.append(body)
            del bucket[:-_MAX_BODIES]
            self._bodies.move_to_end(session_id)
            while len(self._bodies) > _MAX_SESSIONS:
                self._bodies.popitem(last=False)

    def carried(self, session_id: str, line: str) -> bool:
        with self._lock:
            if line in self._delivered.get(session_id, set()):
                return True
            return any(line in body for body in self._bodies.get(session_id, []))

    def claim(self, session_id: str, line: str) -> bool:
        """Record that the seat is delivering ``line`` itself. False if already
        claimed, so two end-of-turn hooks never send it twice."""
        with self._lock:
            done = self._delivered.setdefault(session_id, set())
            if line in done:
                return False
            done.add(line)
            self._delivered.move_to_end(session_id)
            while len(self._delivered) > _MAX_SESSIONS:
                self._delivered.popitem(last=False)
            return True

    def release(self, session_id: str, line: str) -> None:
        with self._lock:
            self._delivered.get(session_id, set()).discard(line)

    def clear(self) -> None:
        with self._lock:
            self._bodies.clear()
            self._delivered.clear()


def owed_line(pending: Any) -> str | None:
    """The act line a session proposed and still owes, or None.

    Only a record THIS session proposed carries its line; an act adopted from a
    later answer has none, and a confirmed or in-flight act is past asking."""
    if pending is None:
        return None
    line = getattr(pending, "readback", "") or ""
    if (
        not line
        or getattr(pending, "confirmed", None) is not None
        or getattr(pending, "in_flight", False)
    ):
        return None
    return line


def body_for(line: str) -> str:
    return f"{PREAMBLE}\n\n{line}\n"


def deliver_if_owed(
    session_id: str,
    *,
    pending: Any,
    origin: Any,
    ledger: TransmittedBodies,
    send: Callable[[str, str], str],
    on_sent: Callable[[str, str], None],
    on_failed: Callable[[str, str], None],
) -> str:
    """Deliver the owed line if no transmitted reply carried it.

    Returns what happened: ``none`` (nothing owed), ``carried`` (a reply
    already carried it), ``sent``, or ``failed``. ``send(message_id, body)``
    returns the sent message id or raises; the two callbacks write the ledger
    row (and, on failure, the page)."""
    line = owed_line(pending)
    if not line:
        return "none"
    if ledger.carried(session_id, line):
        return "carried"
    if not ledger.claim(session_id, line):
        return "carried"
    message_id = str(getattr(origin, "message_id", "") or "") if origin is not None else ""
    if not message_id:
        ledger.release(session_id, line)
        on_failed(line, "no recorded inbound message to thread the act line to")
        return "failed"
    try:
        sent_id = send(message_id, body_for(line))
    except Exception as exc:  # noqa: BLE001 - every failure is reported, none is swallowed
        ledger.release(session_id, line)
        on_failed(line, f"{exc.__class__.__name__}: {exc}")
        return "failed"
    on_sent(line, str(sent_id or ""))
    return "sent"


__all__ = [
    "PREAMBLE",
    "TransmittedBodies",
    "body_for",
    "deliver_if_owed",
    "owed_line",
]
