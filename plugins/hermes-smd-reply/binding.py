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
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shared import inbound, msgraph_broker
from shared.cron_attribution import parse_cron_session

TOOL_NAME = "reply_bind"
#: The inbox marker a bound origin carries. Never a real inbox id; the relay
#: keys every bound-only branch on it.
BOUND_INBOX = "verified-binding"


#: The sender id the gateway gives a ``/webhooks/handoff`` turn (the route, as
#: ``webhook:agentmail`` is for mail): a job's completion wake.
HANDOFF_SENDER = "webhook:handoff"
#: A job runner's wake names its job in its first line: "... for demand job
#: <ULID>." (demand-letter-drafter), "... for drafting job <ULID>."
#: (document-drafter) or "... for chronology job <ULID>."
#: (medical-chronology-maintainer). The earliest mention decides the kind.
#: The chronology wake was missing until 2026-10-07: a held chronology's wake
#: was unfenced, and the Operator wrote a NEW email to the requester and the
#: matter's attorney, who was not on the request.
_WAKE_JOB = re.compile(r"\b(demand|drafting|chronology) job ([0-9A-HJKMNP-TV-Z]{26})\b")
#: The wake word -> the broker's binding kind (ss-console reply_binding.KINDS).
_JOB_KIND = {"demand": "demand_job", "drafting": "drafting_job", "chronology": "medchron_job"}
JOB_KINDS = frozenset(_JOB_KIND.values())
_LABEL = {kind: f"{word} job" for word, kind in _JOB_KIND.items()}


def _label(kind: str) -> str:
    """ "demand job" / "drafting job" / "chronology job", for the refusal sentences."""
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


class TurnSources:
    """session id -> the handoff wake that opened it, with the job it names: the
    kind (``demand_job`` / ``drafting_job``) and id, or ("", ""). Recorded at
    ``pre_llm_call`` from the gateway's own sender id, never from anything the
    model writes. Cron turns need no record: the scheduler stamps them in the
    session id itself."""

    def __init__(self, max_sessions: int = 256) -> None:
        self._max = max_sessions
        self._handoff: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._lock = threading.Lock()

    def note(self, session_id: str, sender_id: Any, user_message: Any) -> None:
        if not session_id or sender_id != HANDOFF_SENDER:
            return
        found = _WAKE_JOB.search(user_message) if isinstance(user_message, str) else None
        with self._lock:
            if session_id in self._handoff:
                return
            self._handoff[session_id] = (
                (_JOB_KIND[found.group(1)], found.group(2)) if found else ("", "")
            )
            while len(self._handoff) > self._max:
                self._handoff.popitem(last=False)

    def source(self, session_id: str) -> tuple[str, str] | None:
        """("handoff", pinned job id or ""), ("cron", ""), or None."""
        with self._lock:
            if session_id in self._handoff:
                return ("handoff", self._handoff[session_id][1])
        if parse_cron_session(session_id):
            return ("cron", "")
        return None

    def kind_of_job(self, job_id: str) -> str:
        """The kind a noted wake gave ``job_id``, or ""."""
        with self._lock:
            for kind, jid in self._handoff.values():
                if jid and jid == job_id:
                    return kind
        return ""

    def job_kind(self, session_id: str) -> str:
        """The pinned job's binding kind in a job's wake, or ""."""
        with self._lock:
            have = self._handoff.get(session_id)
        return have[0] if have else ""

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._handoff.clear()


TURN_SOURCES = TurnSources()


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
    return json.dumps(
        {
            "bound": True,
            "binding": req,
            "sender": verdict.get("sender"),
            "graph_message_id": verdict.get("graph_message_id"),
            "message": (
                "Bound. Draft ONE reply with create_draft addressed to this sender only; it is "
                "sent in that email's thread once it passes the reply checks. Nothing else in "
                "this turn can be replied to."
            ),
        }
    )


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
    if not isinstance(req, dict) or not sender or "@" not in sender or not graph_id:
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
                "A demand, drafting or chronology job's id, from its completion wake: binds to "
                "the email that requested it."
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
    "in that email's thread, once. Pass exactly one of job_id, graph_message_id, internet_message_id."
)

#: Tools a demand or drafting job's completion wake may never call: every
#: send, and the casework brief (a message to staff by another name). The bound reply
#: (create_draft after reply_bind) is the wake's ONLY channel (2026-10-06: a
#: refused bind was followed by an email to the responsible attorney).
WAKE_FORBIDDEN_TOOLS = frozenset({"smd_send_message", "casework_brief"})


def wake_send_refusal(session_id: str, tool_name: str, args: Any = None) -> str | None:
    """The refusal for ``tool_name`` in a demand, drafting or chronology job's wake, or None."""
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
    "WAKE_FORBIDDEN_TOOLS",
    "wake_send_refusal",
    "BOUND_INBOX",
    "HANDOFF_SENDER",
    "TURN_SOURCES",
    "SESSION_BINDINGS",
    "TOOL_DESCRIPTION",
    "TOOL_NAME",
    "TOOL_SCHEMA",
    "Binding",
    "handle_tool",
    "record_from_result",
]
