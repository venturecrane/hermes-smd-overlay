"""The four messages that close the rule-request loop (ss-console#2546).

WHAT WAS BROKEN. ss-console#2529 gave a firm a way to teach the Operator by
talking to it: state a rule, hear it read back, say yes, and it commits. That
works when the person speaking is an Operator admin. When they are not, the
Operator recorded the rule, said an admin could apply it by replying "apply
that", and then nothing happened. No admin was told there was anything to
answer. An admin's "no" did nothing at all. A rule nobody answered was deleted
by the broker's sweep, so the person who asked could not even be told it had
lapsed. Three silences, and every one of them looked from the outside like the
Operator had simply ignored somebody.

WHAT THIS MODULE IS. The four notifications that end those silences, composed
here as FIXED TEMPLATES and sent deterministically by the overlay rather than by
the model:

1. a paralegal's rule reaches the administrators the firm named for request
   traffic, with the tag and the two answers;
2. the person who asked is told when it goes into effect;
3. they are told when an administrator declines it;
4. they are told when nobody answered and it lapsed.

WHY TEMPLATES AND NOT PROSE. Every one of these is a message about the firm's
own governance, sent to a named person, with no human between the decision and
the send. A composed body would put the model in the position of describing an
authority decision it did not make, on a turn that has other work to do. The
bodies here interpolate exactly three kinds of value, all of them authored or
broker-minted: an address, the person's own sentence read back verbatim, and a
proposal tag. Nothing here states a timeline, a promise, or a commitment the
firm has not made.

WHY IT IS INJECTED. ``send`` and ``emit`` are arguments, not imports. The send
has to run through the trust plugin's gate (see :mod:`shared.send_dispatch` for
the layout reason), and every branch here has to be provable in a test without a
broker, a mailbox, or a D1 binding. The rule that makes that safe is the same
one the sweeper follows: this module decides WHAT to say and to WHOM, and it
never decides whether a send is allowed.

WHAT A CALLER OWES. Every function returns a :class:`Notification` saying
whether the message went, and a caller that cannot say "yes" must tell the
person so. An Operator that says an administrator has been asked, when nothing
left the building, is worse than one that says nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: The audit type written when a rule request has actually been emailed to the
#: administrators the firm named. Its own row, rather than a field on somebody
#: else's: this is the moment a request reached a person, and it is the only
#: record that it did.
RULE_REQUEST_NOTIFIED = "RULE_REQUEST_NOTIFIED"

#: Cap on how many administrators one request pages. A firm authoring a long
#: routing list is authoring its own noise, but an unbounded fan-out from a
#: single inbound message is a send amplifier, so it is bounded here as well as
#: by what a person would plausibly author.
MAX_NOTIFIED = 8


@dataclass(frozen=True)
class Notification:
    """What one notification attempt did, and what the Operator may now say.

    ``note`` is written to be injected into the model's context verbatim. On the
    failure path it is an INSTRUCTION to be honest rather than a description of
    the failure, because the failure the loop has to survive is the model
    smoothing over a send that did not happen.
    """

    sent: bool
    note: str
    recipients: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""


#: ``(to, subject, text, session_id, cc) -> object with .sent/.reason``
SendFn = Callable[..., Any]
#: ``(action_type, metadata) -> None``
EmitFn = Callable[..., None]


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _readback(proposal_id: str, text: str) -> str:
    """The canonical block, rendered the same way the broker renders it.

    Deliberately re-rendered rather than passed through from wherever the caller
    happened to have it: the tag and the sentence must arrive together or a
    reply cannot bind an answer to a rule.
    """
    return f"[rule {proposal_id}] {text}"


_ADMIN_SUBJECT = "A rule for the firm to approve [rule {proposal_id}]"

_ADMIN_BODY = """{requester} asked for this to become how the firm's work reads:

{readback}

Reply "apply that" to put it in force, or "no" to decline it. Either answer is
passed back to {requester}.

Nothing is in effect unless an administrator answers. A request nobody answers
lapses on its own, and {requester} is told when it does.
"""

_INSTALLED_SUBJECT = "Your rule is in effect [rule {proposal_id}]"

_INSTALLED_BODY = """{applied_by} applied the rule you asked for:

{readback}

It is in effect from now, and work of that kind is written to it.
"""

_DECLINED_SUBJECT = "Your rule was declined [rule {proposal_id}]"

_DECLINED_BODY = """{declined_by} declined the rule you asked for:

{readback}

Nothing changed. If you think it was misread, say it again and it will be put to
an administrator again.
"""

_LAPSED_SUBJECT = "Your rule lapsed unanswered [rule {proposal_id}]"

_LAPSED_BODY = """No administrator answered the rule you asked for, so it has
lapsed:

{readback}

Nothing changed. Say it again if you still want it, and it will be put to an
administrator again.
"""

_NOTIFIED_NOTE = (
    "The rule was recorded and this seat has ALREADY emailed it to {names} for "
    'them to answer with "apply that" or "no". Tell the person that, naming who '
    "was asked. Do not say it is in effect, do not offer to send anything "
    "yourself, and do not call a send tool: the message is already gone."
)

_NOT_NOTIFIED_NOTE = (
    "The rule was recorded, but this seat COULD NOT tell an administrator about "
    "it: {reason}. Say exactly that to the person, in your own words: their rule "
    "is written down, nobody has been asked yet, and they should forward it to "
    "an administrator themselves. Do not say an administrator was asked."
)

_NO_ROUTING_NOTE = (
    "The rule was recorded, but this engagement names nobody to receive rule "
    "requests, so no administrator has been told. Say that plainly: the rule is "
    "written down and is waiting for an administrator to apply it. Do not claim "
    "anyone was asked."
)


def notify_admins(
    *,
    proposal_id: str,
    text: str,
    requester: str,
    rule_requests_to: list[str],
    send: SendFn,
    emit: EmitFn | None = None,
    session_id: str = "",
) -> Notification:
    """Put one paralegal's rule in front of the administrators the firm named.

    THE TAG GOES IN THE SUBJECT as well as the body. Reply chains vary wildly in
    what they quote, and the answer has to carry the tag or the seat cannot bind
    it to a rule; a "Re:" subject survives clients that strip the quoted body
    entirely.

    THE REQUESTER IS COPIED, on purpose. They asked for something and they can
    see that it was asked, by name, without waiting for an outcome. It also
    means the thread they answer on is the thread they started.

    Administrators the firm did NOT name receive nothing, here or anywhere: this
    sends to ``rule_requests_to`` and never to ``admins``. The two lists are
    separate precisely so a partner is not paged for every request.
    """
    recipients = [a for a in (_clean(x) for x in rule_requests_to or []) if a][:MAX_NOTIFIED]
    proposal_id = _clean(proposal_id)
    requester = _clean(requester)
    if not recipients:
        return Notification(sent=False, note=_NO_ROUTING_NOTE, reason="no routing authored")
    readback = _readback(proposal_id, _clean(text))
    result = send(
        to=recipients,
        cc=[requester] if requester else [],
        subject=_ADMIN_SUBJECT.format(proposal_id=proposal_id),
        text=_ADMIN_BODY.format(requester=requester, readback=readback),
        session_id=session_id,
    )
    if not getattr(result, "sent", False):
        reason = _clean(getattr(result, "reason", "")) or "the send was refused"
        logger.info("rule_dispatch: rule %s NOT notified (%s)", proposal_id, reason)
        return Notification(
            sent=False,
            note=_NOT_NOTIFIED_NOTE.format(reason=reason),
            recipients=tuple(recipients),
            reason=reason,
        )
    if emit is not None:
        _record(
            emit,
            proposal_id=proposal_id,
            requester=requester,
            recipients=recipients,
            message_id=_clean(getattr(result, "message_id", "")),
            session_id=session_id,
        )
    return Notification(
        sent=True,
        note=_NOTIFIED_NOTE.format(names=", ".join(recipients)),
        recipients=tuple(recipients),
    )


def _record(
    emit: EmitFn,
    *,
    proposal_id: str,
    requester: str,
    recipients: list[str],
    message_id: str,
    session_id: str,
) -> None:
    """Write the RULE_REQUEST_NOTIFIED row. Best-effort, never fatal.

    Ids and addresses only. The rule's TEXT is deliberately absent, for the same
    reason it is absent from RULE_PROPOSED: a proposal is a sentence somebody
    typed in an email, and the ledger keeps who and which, not what.

    It is a row of its own rather than a field on the send's own
    CONFIRM_SEND_DISPATCHED row, because the two answer different questions. The
    send row says a message left; this says a specific rule reached the people
    authorized to answer it, which is the fact anybody auditing this loop is
    actually looking for.
    """
    try:
        emit(
            action_type=RULE_REQUEST_NOTIFIED,
            metadata={
                "proposal_id": proposal_id,
                "instructed_by": requester,
                "notified_to": list(recipients),
                "notified_count": len(recipients),
                "sent_message_id": message_id,
            },
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 — the send already happened
        logger.warning("rule_dispatch: RULE_REQUEST_NOTIFIED emission failed (%s)", exc)


def notify_outcome(
    *,
    kind: str,
    proposal_id: str,
    text: str,
    requester: str,
    send: SendFn,
    by: str = "",
    session_id: str = "",
) -> Notification:
    """Tell the person who asked how their rule ended.

    ``kind`` is ``installed``, ``declined`` or ``lapsed``. Three sentences
    rather than one parameterised sentence, because the three are different news
    and a person reading them needs to know which happened without parsing a
    clause: one is a change to how their work is written, one is an answer from
    a named colleague, and one is nobody having answered at all.

    The caller marks the broker AFTER this returns sent, never before. A mark
    written first would trade a duplicate note for a silence, and silence is the
    failure this whole module exists to end.
    """
    proposal_id = _clean(proposal_id)
    requester = _clean(requester)
    if not requester:
        return Notification(sent=False, note="", reason="no requester to tell")
    readback = _readback(proposal_id, _clean(text))
    by = _clean(by) or "An administrator"
    if kind == "installed":
        subject = _INSTALLED_SUBJECT.format(proposal_id=proposal_id)
        body = _INSTALLED_BODY.format(applied_by=by, readback=readback)
    elif kind == "declined":
        subject = _DECLINED_SUBJECT.format(proposal_id=proposal_id)
        body = _DECLINED_BODY.format(declined_by=by, readback=readback)
    elif kind == "lapsed":
        subject = _LAPSED_SUBJECT.format(proposal_id=proposal_id)
        body = _LAPSED_BODY.format(readback=readback)
    else:  # pragma: no cover — a caller bug, not a runtime state
        raise ValueError(f"unknown outcome kind: {kind!r}")
    result = send(
        to=[requester],
        cc=[],
        subject=subject,
        text=body,
        session_id=session_id,
    )
    sent = bool(getattr(result, "sent", False))
    reason = _clean(getattr(result, "reason", ""))
    if not sent:
        logger.info(
            "rule_dispatch: %s outcome for rule %s NOT delivered (%s)", kind, proposal_id, reason
        )
    return Notification(
        sent=sent,
        note="",
        recipients=(requester,),
        reason=reason,
    )


__all__ = [
    "MAX_NOTIFIED",
    "RULE_REQUEST_NOTIFIED",
    "EmitFn",
    "Notification",
    "SendFn",
    "notify_admins",
    "notify_outcome",
]
