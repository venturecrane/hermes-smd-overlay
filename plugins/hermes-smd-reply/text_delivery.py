"""The seat delivers a reply the model wrote as text and never drafted.

THE LIVE FAILURE (hermes-captain-mark, 2026-10-19 03:12Z, session
20261019_031221_502671f3). A partner on the seat's roster replied to the
agent's hello. The webhook turn ran its skill, and the model answered with a
474-character TEXT response ("Sure! To set this up properly, I need a few
details: ...") and zero tool calls. On the webhook platform a turn's final text
is logged (``[webhook] Response for webhook:agentmail:msg_...``) and goes to
nobody, so the partner got silence. Minutes earlier the same seat answered
another rostered sender correctly, because in that turn the model happened to
call ``create_draft`` and this plugin relayed the draft. Whether a colleague got
an answer depended on whether the model remembered which tool sends.

That is the same shape as the act-line failure ``act_delivery`` closes, and the
same answer: a prompt that says "reply by drafting" is a request, not a control.
The skills say it too (belt and braces); this module is the structure.

WHAT THIS IS. At ``post_llm_call`` (one per completed turn, carrying the final
response), the reply plugin asks: did an email from a rostered sender open this
turn, does the turn end with something to say, and did nothing answer that
email? If so, the final text becomes the reply, through the very path a drafted
reply takes: the plugin builds the ``create_draft`` call the model did not make
(addressed to the verified sender only), puts it through the seat's trust gate
(``shared.tool_gate``, the trust plugin's own ``pre_tool_call``), then through
the relay's single decision (``_relay_draft``: roster, recipient lock, shared
reply on a couples seat, the fabrication / banned-marker gate, the content
floor, the output checklist, the matter gate, the rate limit, the broker reply
verb, the audit row). The text it is handed is already the voice-transformed
response (Hermes runs ``transform_llm_output`` before ``post_llm_call``); the
one change made to it is :func:`scrub`'s punctuation fix, because no model is
left to redraft it. A hold or refusal is paged (``seat_text_reply_held``): the
turn is over, so nobody else would learn the person heard nothing.

WHEN IT SENDS NOTHING. Every one of these is a test:

* no email opened this turn (a cron, a wake, Telegram, the MCP channel);
* the sender is not on the roster. A non-rostered sender is exactly the mail
  the router quarantines, and an unauthenticated From records no origin at all;
* the response is empty, or the skill's authored silence (``shared.silence``);
* the turn already answered the email: the relay committed a reply for it, the
  act delivery sent the seat's line, the broker transmitted anything anchored
  to it (``shared.answered_messages``, which covers the space gate's own
  confirmation reply), or the turn so much as attempted a draft or a send
  (``TurnSends``). An attempt that was held has its own recovery, told to the
  model in the same turn; sending the turn's text on top of it would answer one
  email twice;
* the seat's Email adapter is not one this delivery is turned on for
  (:data:`ADAPTERS`). Graph seats (the A&P firm's) are not changed by this
  module: nothing turns on for a client without its ask.

At most once per inbound message, whatever fires twice.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Any

#: The Email adapters whose inbound turns get their final text delivered. AgentMail
#: seats only: every hosted and couples seat, Brokery and the SMD pilot. A Graph
#: seat is a client firm's mailbox, and widening what it sends is that firm's ask.
ADAPTERS: frozenset[str] = frozenset({"agentmail"})

#: The draft tool the delivery stands in for, under its live Hermes runtime name.
DRAFT_TOOL = "mcp_agentmail_create_draft"

#: Prefix of the synthetic call id, so its hold notice and trust decision are
#: never mistaken for a model call's.
CALL_ID_PREFIX = "seat-text-reply:"

_MAX_SESSIONS = 512


class TurnSends:
    """Sessions whose CURRENT turn attempted a draft or a send. Reset per turn."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, None] = OrderedDict()

    def note(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions[session_id] = None
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > _MAX_SESSIONS:
                self._sessions.popitem(last=False)

    def attempted(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


class DeliveredOnce:
    """Inbound message ids whose text this module has already put on the relay."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: OrderedDict[str, None] = OrderedDict()

    def claim(self, message_id: str) -> bool:
        """True the first time; False for every later call with the same id."""
        with self._lock:
            if message_id in self._ids:
                return False
            self._ids[message_id] = None
            while len(self._ids) > _MAX_SESSIONS:
                self._ids.popitem(last=False)
            return True

    def clear(self) -> None:
        with self._lock:
            self._ids.clear()


_EXCLAMATION = re.compile(r"!(?!=)")
_EM_DASH = re.compile(r"\s*—\s*")


def scrub(text: str) -> str:
    """Fix the two punctuation marks the output checklist refuses and a model
    corrects on redraft: an exclamation mark becomes a period, an em dash a
    comma. No word is added, removed or reordered.

    WHY HERE AND NOT FOR A DRAFT. A drafted reply the checklist holds is told to
    the model in the same turn (``transform_tool_result``), and the model fixes
    it. This text is delivered after the turn ended: there is no model left to
    tell, so a hold on "Sure!" would be the very silence this module exists to
    end (the live 03:12Z text opened with it). Every other checklist rule, and
    every safety floor, still runs on the result and still holds it."""
    if not isinstance(text, str):
        return ""
    return _EM_DASH.sub(", ", _EXCLAMATION.sub(".", text))


def draft_args(sender: str, text: str) -> dict[str, Any]:
    """The ``create_draft`` arguments the model would have written.

    Addressed to the verified sender alone; the relay's lock (and, on a shared
    couples thread, its shared lock) decides from there exactly as for a model
    draft. No subject: a reply threads under the original's."""
    return {"to": [sender], "subject": "", "text": text, "html": ""}


__all__ = [
    "ADAPTERS",
    "CALL_ID_PREFIX",
    "DRAFT_TOOL",
    "DeliveredOnce",
    "TurnSends",
    "draft_args",
    "scrub",
]
