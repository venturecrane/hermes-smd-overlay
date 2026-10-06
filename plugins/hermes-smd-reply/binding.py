"""The verified reply binding, overlay side: a turn no inbound email opened may
answer ONE earlier email, once, to the person who sent it.

WHY. The relay is keyed on the inbound that opened the turn
(``SESSION_INBOUND_ORIGIN``). A job's completion turn (a ``/webhooks/handoff``
wake) and a one-shot cron turn have none, so before this the chronology
runner's delivery reached the matter and never the requester's thread: every
operator chronology thread was the request and "queued", and nothing after.

THE SHAPE. The agent calls ``reply_bind`` naming WHICH email (a demand job id,
or the email's Graph or RFC 5322 id). It never names a person. The broker
(ss-console ``workspace_broker/reply_binding.py``) resolves the email in the
operator mailbox, refuses a draft, checks the sender against the live reply
roster and that the email has not been answered, and answers with the sender.
This module records that binding for the session; the relay then treats the
bound sender exactly as it treats a verified inbound sender (recipient lock,
fabrication and content floors, output checklist, matter gate, rate limit) and
transmits through ``msgraph_reply_bound``, where the broker verifies AGAIN,
takes a durable once-only claim and replies on the Graph id it resolved. So:

* the recipient is the broker's, never the model's;
* the roster is re-read at bind time, at relay time and at send time;
* once-only survives a restart (the broker's claim; ``RepliedOnce`` is only
  the in-turn guard);
* a turn an email opened never binds: its replies belong to that email.

One binding per session, first wins, exactly like the inbound origin.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from shared import inbound, msgraph_broker

TOOL_NAME = "reply_bind"
#: The inbox marker a bound origin carries. Never a real inbox id; the relay
#: keys every bound-only branch on it.
BOUND_INBOX = "verified-binding"


@dataclass(frozen=True)
class Binding:
    sender: str
    #: The exact dict the broker verified, replayed verbatim at send.
    request: tuple[tuple[str, str], ...]
    key: str

    def as_request(self) -> dict[str, str]:
        return dict(self.request)

    def origin(self) -> inbound.InboundOrigin:
        """The bound email as the relay's recipient-lock anchor."""
        return inbound.InboundOrigin(
            sender_address=self.sender, message_id=self.key, inbox_id=BOUND_INBOX
        )


class SessionBindings:
    """session id -> Binding. Bounded, thread-safe, first binding wins."""

    def __init__(self, max_sessions: int = 256) -> None:
        self._max = max_sessions
        self._by_session: OrderedDict[str, Binding] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, session_id: str, binding: Binding) -> Binding:
        """Record unless one is set; returns the binding the session HAS."""
        with self._lock:
            have = self._by_session.get(session_id)
            if have is not None:
                return have
            self._by_session[session_id] = binding
            while len(self._by_session) > self._max:
                self._by_session.popitem(last=False)
            return binding

    def get(self, session_id: str) -> Binding | None:
        if not session_id:
            return None
        with self._lock:
            return self._by_session.get(session_id)

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._by_session.clear()


SESSION_BINDINGS = SessionBindings()


def _request_from_args(args: dict[str, Any]) -> dict[str, str] | str:
    """The broker binding for the tool's arguments, or a refusal sentence."""
    given = {
        k: str(args.get(k) or "").strip()
        for k in ("job_id", "graph_message_id", "internet_message_id")
        if str(args.get(k) or "").strip()
    }
    if len(given) != 1:
        return "name exactly one of job_id, graph_message_id or internet_message_id"
    ((field, value),) = given.items()
    if field == "job_id":
        return {"kind": "demand_job", "job_id": value}
    return {"kind": "message", field: value}


def handle_tool(args: dict[str, Any], **_: Any) -> str:
    """``reply_bind``: ask the broker; return its verdict as the tool result."""
    req = _request_from_args(args if isinstance(args, dict) else {})
    if isinstance(req, str):
        return json.dumps({"bound": False, "reason": req})
    try:
        verdict = msgraph_broker.bind_reply(req)
    except (msgraph_broker.MsGraphBrokerUnavailable, msgraph_broker.BrokerError) as exc:
        return json.dumps({"bound": False, "reason": f"the binding could not be checked: {exc}"})
    if verdict.get("bound") is not True:
        return json.dumps({"bound": False, "reason": str(verdict.get("reason") or "refused")})
    return json.dumps(
        {
            "bound": True,
            "binding": req,
            "sender": verdict.get("sender"),
            "message": (
                "Bound. Draft ONE reply with create_draft addressed to this sender only; it is "
                "sent in that email's thread once it passes the reply checks. Nothing else in "
                "this turn can be replied to."
            ),
        }
    )


def record_from_result(session_id: str, result: Any) -> str | None:
    """Record a successful ``reply_bind`` result for the session.

    Returns a replacement tool result when the binding is NOT taken (an email
    opened this turn, or the session already holds a different binding), so
    the agent reads why; ``None`` when the result stands as is.
    """
    try:
        data = json.loads(result) if isinstance(result, str) else None
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("bound") is not True:
        return None
    req, sender = data.get("binding"), str(data.get("sender") or "").strip().lower()
    if not isinstance(req, dict) or not sender or "@" not in sender:
        return None
    if not session_id:
        return json.dumps(
            {"bound": False, "reason": "this turn has no session to bind; the reply cannot be sent"}
        )
    if inbound.SESSION_INBOUND_ORIGIN.get(session_id) is not None:
        return json.dumps(
            {
                "bound": False,
                "reason": "an email opened this turn; its replies go to that email, not a bound one",
            }
        )
    request = tuple(sorted((str(k), str(v)) for k, v in req.items()))
    wanted = Binding(
        sender=sender, request=request, key="bound:" + "|".join(f"{k}={v}" for k, v in request)
    )
    have = SESSION_BINDINGS.record(session_id, wanted)
    if have != wanted:
        return json.dumps(
            {"bound": False, "reason": "this turn is already bound to another email; one per turn"}
        )
    return None


TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "string",
            "description": "A demand job's id, from its completion wake: binds to the email that requested it.",
        },
        "graph_message_id": {
            "type": "string",
            "description": "The Graph id of the email to answer, as this turn was handed it.",
        },
        "internet_message_id": {
            "type": "string",
            "description": "The email's RFC 5322 Message-ID (<...@...>), when that is what you hold.",
        },
    },
    "required": [],
    "additionalProperties": False,
}

TOOL_DESCRIPTION = (
    "Bind this turn's reply to ONE earlier email, for a turn no email opened (a job's completion "
    "wake, a scheduled one-off). Name the email, never a person: the broker finds it in the "
    "operator mailbox, checks its sender may be replied to and that it has had no reply, and "
    "answers with that sender. Then draft one reply with create_draft to that sender; it goes out "
    "in that email's thread, once. Pass exactly one of job_id, graph_message_id, internet_message_id."
)

__all__ = [
    "BOUND_INBOX",
    "SESSION_BINDINGS",
    "TOOL_DESCRIPTION",
    "TOOL_NAME",
    "TOOL_SCHEMA",
    "Binding",
    "handle_tool",
    "record_from_result",
]
