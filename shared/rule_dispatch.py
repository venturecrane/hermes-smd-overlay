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

THE OPERATIONS HALF (ss-console#2546, the second wave). A routine, a schedule, a
channel, a memory setting, an autonomy level or an on/off is SMD's to change
rather than the firm's, so it never reaches an administrator here at all — it
reaches SMD's desk, and the silence to end is the one AFTER that: the desk
answered and the person who asked never heard. :func:`notify_ops_outcome` is the
three sentences that close it, and they are deliberately NOT the four above.
"Your rule is in effect" says an administrator of the FIRM applied something,
which is a different fact about a different authority; sending it about a routine
change would tell somebody their own colleagues decided a thing SMD decided.

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

#: SMD's operations desk, quoted in the lapsed notice so a person whose request
#: nobody answered has somewhere to go that is not this seat. Mirrors
#: ``shared.operations_request.SMD_OPERATIONS_DESK``; the two are the same desk
#: and are written out here rather than imported so this module keeps its
#: "templates and nothing else" shape.
OPS_DESK = "team@smd.services"


def _ops_readback(proposal_id: str, text: str) -> str:
    """The operations readback, rendered the way the broker renders it.

    Its own function beside :func:`_readback` rather than a parameter on it,
    because the tag WORD is the whole difference between "a sentence the firm
    agreed to" and "a change SMD made", and a caller passing the wrong one would
    tell somebody their rule was set up.
    """
    return f"[ops {proposal_id}] {text}"


_OPS_DONE_SUBJECT = "SMD set this up [ops {proposal_id}]"

_OPS_DONE_BODY = """SMD has made the change you asked for:

{readback}
{note}
Nothing else is needed from you. If it is not behaving the way you expected, say
so and the Operator will pass that on the same way.
"""

_OPS_DECLINED_SUBJECT = "SMD declined this request [ops {proposal_id}]"

_OPS_DECLINED_BODY = """SMD is not making the change you asked for:

{readback}
{note}
Nothing changed. If the request was misread, say it again with what you meant
and it will be put to SMD again.
"""

_OPS_LAPSED_SUBJECT = "Your request to SMD lapsed unanswered [ops {proposal_id}]"

_OPS_LAPSED_BODY = (
    """Nobody at SMD answered the change you asked for within a
week, so the request has lapsed:

{readback}

Nothing changed. Ask for it again and it will be put to SMD again, or write to
"""
    + OPS_DESK
    + """ directly.
"""
)

#: How SMD's own words are quoted, and they ARE quoted rather than paraphrased.
#: An Operator composing its own account of somebody else's business decision
#: would be inventing client-facing content; a quotation is a report.
_OPS_REASON_LINE = '\nSMD wrote: "{reason}"\n'
_OPS_DONE_NOTE_LINE = '\nSMD\'s note: "{reason}"\n'


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


def notify_ops_outcome(
    *,
    kind: str,
    proposal_id: str,
    text: str,
    requester: str,
    send: SendFn,
    by: str = "",
    reason: str = "",
    session_id: str = "",
) -> Notification:
    """Tell the person who asked how SMD answered their operations request.

    THE HALF ss-console#2546 WAS MISSING. Before this the Operator told somebody
    their request had been passed on, which was true, and that was the last they
    heard of it: SMD's answer reached the desk's own mailbox and stopped there.
    From where the person sat, a request that was granted and a request that was
    ignored produced exactly the same silence.

    THREE SENTENCES, NOT ONE PARAMETERISED SENTENCE, for the reason
    :func:`notify_outcome` gives: the three are different news and the reader
    must know which happened without parsing a clause. They are also DELIBERATELY
    not that function's four — "your rule is in effect" says an administrator of
    the FIRM applied something, which is a different fact about a different
    authority, and sending it about a routine change would misattribute the
    decision to the reader's own colleagues.

    ``by`` IS NOT RENDERED, and its absence is the point rather than an omission.
    Who at SMD answered is on the broker row and in the ledger; putting a named
    individual in front of the firm would make the answer read as one person's
    opinion instead of the firm's, and it is a person's address, which the
    requester has no reason to hold.

    ``reason`` is SMD's OWN WORDS, quoted. It is optional on a ``done`` (where it
    reads as a note) and usual on a ``declined`` (where it is the answer). Its
    absence renders as nothing at all rather than as an empty quotation.
    """
    proposal_id = _clean(proposal_id)
    requester = _clean(requester)
    if not requester:
        return Notification(sent=False, note="", reason="no requester to tell")
    readback = _ops_readback(proposal_id, _clean(text))
    quoted = _clean(reason)
    if kind == "done":
        subject = _OPS_DONE_SUBJECT.format(proposal_id=proposal_id)
        body = _OPS_DONE_BODY.format(
            readback=readback,
            note=_OPS_DONE_NOTE_LINE.format(reason=quoted) if quoted else "",
        )
    elif kind == "declined":
        subject = _OPS_DECLINED_SUBJECT.format(proposal_id=proposal_id)
        body = _OPS_DECLINED_BODY.format(
            readback=readback,
            note=_OPS_REASON_LINE.format(reason=quoted) if quoted else "",
        )
    elif kind == "lapsed":
        subject = _OPS_LAPSED_SUBJECT.format(proposal_id=proposal_id)
        body = _OPS_LAPSED_BODY.format(readback=readback)
    else:  # pragma: no cover - a caller bug, not a runtime state
        raise ValueError(f"unknown operations outcome kind: {kind!r}")
    result = send(
        to=[requester],
        cc=[],
        subject=subject,
        text=body,
        session_id=session_id,
    )
    sent = bool(getattr(result, "sent", False))
    send_reason = _clean(getattr(result, "reason", ""))
    if not sent:
        logger.info(
            "rule_dispatch: %s outcome for operations request %s NOT delivered (%s)",
            kind,
            proposal_id,
            send_reason,
        )
    return Notification(
        sent=sent,
        note="",
        recipients=(requester,),
        reason=send_reason,
    )


__all__ = [
    "MAX_NOTIFIED",
    "OPS_DESK",
    "RULE_REQUEST_NOTIFIED",
    "EmitFn",
    "Notification",
    "SendFn",
    "notify_admins",
    "notify_ops_outcome",
    "notify_outcome",
]
