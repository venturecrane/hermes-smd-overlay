"""The verified reply binding, overlay side: a turn no inbound email opened may
answer ONE earlier email, once, to the person who sent it.

WHY. The relay is keyed on the inbound that opened the turn
(``SESSION_INBOUND_ORIGIN``). A job's completion turn (a ``/webhooks/handoff``
wake) and a one-shot cron turn have none, so before this the chronology
runner's delivery reached the matter and never the requester's thread: every
operator chronology thread was the request and "queued", and nothing after.

THE SHAPE. The agent calls ``reply_bind`` naming WHICH email (a demand or drafting
job id, or the email's Graph or RFC 5322 id). It never names a person. The broker
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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shared import inbound, msgraph_broker
from shared.turn_sources import (
    HANDOFF_SENDER,
    JOB_KIND,
    JOB_KINDS,
    TURN_SOURCES,
    WAKE_JOB,
    WAKE_TRIGGER,
    TurnSources,
)

TOOL_NAME = "reply_bind"
#: The inbox marker a bound origin carries. Never a real inbox id; the relay
#: keys every bound-only branch on it.
BOUND_INBOX = "verified-binding"


#: The wake vocabulary lives in ``shared.turn_sources`` (moved there so the
#: trust plugin's send paths can read a wake's job as their participant-fence
#: anchor). The chronology wake was missing until 2026-10-07: a held
#: chronology's wake was unfenced, and the Operator wrote a NEW email to the
#: requester and the matter's attorney, who was not on the request.
_WAKE_JOB = WAKE_JOB
_JOB_KIND = JOB_KIND
_WAKE_TRIGGER = WAKE_TRIGGER
_LABEL = {kind: f"{word} job" for word, kind in _JOB_KIND.items()}

#: A SCHEDULED litigation job has no request email: the calendar ran it. The
#: broker binds it as ONE new message (``mode: new_message``), and it alone
#: sets the recipient (the firm's authored scheduled recipient) and the fixed
#: subject. The overlay takes such a binding only in that job's own scheduled
#: wake, and never takes a reply-to-an-email binding there.
MODE_REPLY = "reply"
MODE_NEW_MESSAGE = "new_message"
#: The in-process once-only key of a new-message binding (there is no email id
#: to key on; an empty key would collide across every scheduled run). It names
#: the wake's own lane ("litigation-scheduled:<job>",
#: "negotiation-scheduled:<notice>"), and it is what the REPLY_SENT audit row
#: records as in_reply_to: a negotiation send must never read as a litigation
#: one (2026-10-10, the first live negotiation notice did).
_WORD_OF_KIND = {kind: word for word, kind in _JOB_KIND.items()}


def _new_message_key(kind: str, job_id: str) -> str:
    return f"{_WORD_OF_KIND[kind]}-scheduled:{job_id}"


def _label(kind: str) -> str:
    """ "demand job" / "drafting job" / "chronology job" / "litigation job"."""
    return _LABEL.get(kind, "demand job")


@dataclass(frozen=True)
class Binding:
    sender: str
    #: The exact dict the broker verified, replayed verbatim at send.
    request: tuple[tuple[str, str], ...]
    #: The Graph id of the bound email: the reply's in_reply_to, the in-turn
    #: once-only key, and what the held-reply store is checked against.
    graph_message_id: str

    def as_request(self) -> dict[str, str]:
        return dict(self.request)

    def origin(self) -> inbound.InboundOrigin:
        """The bound email as the relay's recipient-lock anchor."""
        return inbound.InboundOrigin(
            sender_address=self.sender, message_id=self.graph_message_id, inbox_id=BOUND_INBOX
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
    """The broker binding for the tool's arguments, or a refusal sentence.

    A ``job_id`` binds as the kind a gateway-delivered wake named it: a drafting
    job when a drafting wake named that id, a demand job otherwise. Looked up
    by the id, not the session, because a tool handler has no reliable session
    id (overlay#141); ``record_from_result``, which does, refuses any kind or id
    the session's own wake does not name."""
    given = {
        k: str(args.get(k) or "").strip()
        for k in ("job_id", "graph_message_id", "internet_message_id")
        if str(args.get(k) or "").strip()
    }
    if len(given) != 1:
        return "name exactly one of job_id, graph_message_id or internet_message_id"
    ((field, value),) = given.items()
    if field == "job_id":
        kind = TURN_SOURCES.kind_of_job(value) or "demand_job"
        return {"kind": kind, "job_id": value}
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
    mode = str(verdict.get("mode") or MODE_REPLY)
    out: dict[str, Any] = {
        "bound": True,
        "binding": req,
        "mode": mode,
        "sender": verdict.get("sender"),
        "graph_message_id": verdict.get("graph_message_id"),
        "message": (
            "Bound. Draft ONE reply with create_draft addressed to this sender only; it is "
            "sent in that email's thread once it passes the reply checks. Nothing else in "
            "this turn can be replied to."
        ),
    }
    if mode == MODE_NEW_MESSAGE:
        out["subject"] = verdict.get("subject")
        out["message"] = (
            "Bound to ONE new message (no email asked for this run). Draft it with create_draft "
            "addressed to this sender only; the subject is set for you. It is sent once it "
            "passes the reply checks. Nothing else in this turn can be sent."
        )
    return json.dumps(out)


def _refused(reason: str) -> str:
    return json.dumps({"bound": False, "reason": reason})


def record_from_result(
    session_id: str, result: Any, held_pending: Callable[[str], bool] | None = None
) -> str | None:
    """Record a successful ``reply_bind`` result for the session.

    Returns a replacement tool result when the binding is NOT taken, so the
    agent reads why; ``None`` when the result stands as is. Not taken when:
    an email opened this turn (its replies are that email's); the turn is not a
    job's completion wake or a scheduled turn; a job is not the one (of the
    kind) the wake names; a held reply to that email is waiting for release; or
    the turn already holds a different binding.
    """
    try:
        data = json.loads(result) if isinstance(result, str) else None
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("bound") is not True:
        return None
    req, sender = data.get("binding"), str(data.get("sender") or "").strip().lower()
    graph_id = str(data.get("graph_message_id") or "").strip()
    mode = str(data.get("mode") or MODE_REPLY)
    if mode not in (MODE_REPLY, MODE_NEW_MESSAGE):
        return _refused("the broker's answer was incomplete; nothing was bound")
    if not isinstance(req, dict) or not sender or "@" not in sender:
        return _refused("the broker's answer was incomplete; nothing was bound")
    if mode == MODE_REPLY and not graph_id:
        return _refused("the broker's answer was incomplete; nothing was bound")
    if not session_id:
        return _refused("this turn has no session to bind; the reply cannot be sent")
    if inbound.SESSION_INBOUND_ORIGIN.get(session_id) is not None:
        return _refused("an email opened this turn; its replies go to that email, not a bound one")
    source = TURN_SOURCES.source(session_id)
    if source is None:
        return _refused(
            "only a job's completion wake or a scheduled turn may bind a reply; this turn is neither"
        )
    wake_kind = TURN_SOURCES.job_kind(session_id) if source[0] == "handoff" else ""
    if source[0] == "handoff" and source[1] and req.get("kind") not in JOB_KINDS:
        # A job's wake answers through the job's own binding only: the request
        # email already had its acknowledgment (2026-10-06 practice job).
        return _refused(
            f"this is {_label(wake_kind)} {source[1]}'s wake; bind with job_id={source[1]}, "
            "never the email's id"
        )
    if req.get("kind") in JOB_KINDS:
        if source[0] != "handoff" or not source[1]:
            return _refused(
                f"a {_label(str(req.get('kind')))}'s reply binds only from that job's own "
                "completion wake"
            )
        if req.get("job_id") != source[1] or req.get("kind") != wake_kind:
            return _refused(
                f"this wake is for {_label(wake_kind)} {source[1]}; it cannot answer another job"
            )
    # A new message is a scheduled run's ONE channel (a litigation list, or a
    # negotiation notice: one per new offer), and that wake has no other: the
    # broker's mode must agree with the wake's own trigger. (Only a litigation
    # or negotiation wake is ever marked scheduled, and the checks above
    # already pinned the binding to that wake's own kind and job.)
    scheduled = source[0] == "handoff" and TURN_SOURCES.scheduled(session_id)
    if mode == MODE_NEW_MESSAGE:
        if not scheduled:
            return _refused(
                "only a scheduled run's own wake may bind a new message; nothing was bound"
            )
        graph_id = _new_message_key(wake_kind, source[1])
    elif scheduled:
        return _refused(
            f"this is {_label(wake_kind)} {source[1]}'s scheduled wake: no email asked for this run, "
            "so it answers no email; nothing was bound"
        )
    if held_pending is not None and held_pending(graph_id):
        return _refused(
            "a held reply to that email is waiting for release; binding another would answer it twice"
        )
    request = tuple(sorted((str(k), str(v)) for k, v in req.items()))
    wanted = Binding(sender=sender, request=request, graph_message_id=graph_id)
    have = SESSION_BINDINGS.record(session_id, wanted)
    if have != wanted:
        return _refused("this turn is already bound to another email; one per turn")
    return None


TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "string",
            "description": (
                "A demand, drafting, chronology or litigation job's id, from its completion "
                "wake: binds to the email that requested it."
            ),
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
    "in that email's thread, once. Pass exactly one of job_id, graph_message_id, internet_message_id. "
    "A scheduled litigation job's wake binds by its job_id too: the broker answers with a NEW "
    "message to the firm's scheduled recipient instead of a thread."
)

#: Tools a demand or drafting job's completion wake may never call: every
#: send, and the casework brief (a message to staff by another name). The bound reply
#: (create_draft after reply_bind) is the wake's ONLY channel (2026-10-06: a
#: refused bind was followed by an email to the responsible attorney).
WAKE_FORBIDDEN_TOOLS = frozenset({"smd_send_message", "casework_brief"})


def wake_send_refusal(session_id: str, tool_name: str, args: Any = None) -> str | None:
    """The refusal for ``tool_name`` in a demand, drafting, chronology or
    litigation job's wake, or None."""
    source = TURN_SOURCES.source(session_id) if session_id else None
    if source is None or source[0] != "handoff" or not source[1]:
        return None
    label = _label(TURN_SOURCES.job_kind(session_id))
    if tool_name == TOOL_NAME:
        # Refused BEFORE the broker is asked (a live demand job, 2026-10-06): a bind by
        # the email's id reached the broker, which refused it as already
        # answered, and the model read that as "the reply was sent". The only
        # binding this wake is owed is the job's own.
        a = args if isinstance(args, dict) else {}
        by_email = any(
            str(a.get(k) or "").strip() for k in ("graph_message_id", "internet_message_id")
        )
        other_job = str(a.get("job_id") or "").strip() not in ("", source[1])
        if by_email or other_job:
            return f"this is {label} {source[1]}'s wake; call reply_bind with job_id={source[1]}"
        return None
    from shared.action_classes import ActionClass, classify_tool

    try:
        cls = classify_tool(tool_name).action_class
    except Exception:  # noqa: BLE001 - an unclassifiable tool is not allowed here
        cls = None
    sends = cls is not None and cls.value.startswith("external_send")
    if tool_name in WAKE_FORBIDDEN_TOOLS or sends or cls is ActionClass.REFUSED:
        return (
            f"{tool_name} refused: this is {label} {source[1]}'s completion wake, and its only "
            "channel is the bound reply to the requester (reply_bind with job_id, then create_draft). "
            "If the bind was refused, send nothing to anyone and end the turn stating the refusal."
        )
    return None


__all__ = [
    "JOB_KINDS",
    "MODE_NEW_MESSAGE",
    "MODE_REPLY",
    "WAKE_FORBIDDEN_TOOLS",
    "wake_send_refusal",
    "BOUND_INBOX",
    "HANDOFF_SENDER",
    "TURN_SOURCES",
    "TurnSources",
    "SESSION_BINDINGS",
    "TOOL_DESCRIPTION",
    "TOOL_NAME",
    "TOOL_SCHEMA",
    "Binding",
    "handle_tool",
    "record_from_result",
]
