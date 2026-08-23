"""Somebody asked for a routine change. Pass it to SMD, and say so (ss#2546).

THE THIRD CATEGORY. ADR 0085's amendment of 2026-08-22 names three kinds of
change and who decides each. A person decides their own preferences. Any
Operator admin decides the firm's standards. OPERATIONS are the third: routines,
schedules, channels, memory, autonomy, and turning things on or off. Those are
made by SMD on request, and the firm's admins keep pause and off in the portal.

WHY IT NEEDED CODE AT ALL. Before this, "start sending me a digest every Monday"
got a sentence and nothing behind it. The Operator said, more or less, that
somebody would look at it, and nobody ever did, because there was no path from
that sentence to anyone at SMD. A polite acknowledgement that reaches nobody is
the failure mode this venture's doctrine calls built-but-not-wired, and it is
worse here than a plain refusal would have been: the person believes the request
is in hand.

WHAT THIS MODULE HOLDS. The fixed sentence the Operator must use, and the body
of the message that actually reaches SMD. Both are templates, and the message
carries the requester's VERBATIM address and the id of the message they wrote
it in, so the desk can read the original rather than the Operator's summary of
it.

WHAT IT DOES NOT HOLD. The send. That goes through the same gate a model's own
send goes through (:mod:`shared.send_dispatch`), so a tainted turn refuses it
exactly as it refuses any other send, and the Operator then says it could not
pass the request on rather than saying it did.
"""

from __future__ import annotations

from typing import Any

#: SMD's operations desk. Ours, not the client's, which is why it is a constant
#: here rather than an authored per-seat value: every seat's operations requests
#: reach the same desk, and a seat that could re-point it could route the firm's
#: request about its own Operator somewhere else. It still has to be authored on
#: each seat's ``scope.outbound_roster`` to be sendable at all, which is where a
#: seat says the address is reachable; this says who it is.
SMD_OPERATIONS_DESK = "team@smd.services"

#: The longest summary that rides in one request. A request is a sentence or
#: two; anything longer is the person writing SMD a letter, which they can do
#: directly and which should not travel as a tool argument.
MAX_SUMMARY_CHARS = 2000

#: The subject SMD sees, and the tag is IN IT rather than only in the body
#: (ss-console#2546). Reply chains vary wildly in what they quote, and SMD's
#: answer has to carry the tag or the seat cannot bind it to the request it
#: answers; a "Re:" subject survives clients that strip the quoted body entirely.
SUBJECT = "Operations request from {sender} [ops {proposal_id}]"

BODY = """{sender} asked the {slug} Operator for an operations change.

What they asked for, as the Operator understood it:

{summary}

Their own message is {message_ref} in the Operator's mailbox. Read that rather
than this summary before acting.

Routines, schedules, channels, memory, autonomy and on/off are SMD changes
(ADR 0085). The Operator has told them the request was passed on and has
promised nothing about when or whether it happens.

TO ANSWER, reply to this email keeping [ops {proposal_id}] in the subject:

  done                     -- you have made the change
  done, <what you did>     -- the same, with a line they will see
  no, <why not>            -- you are not making it, and your words are quoted

{requester} is told your answer once, automatically, and nothing else is sent.
An answer that is neither of those leaves the request open, and you are asked
once for a plain "done" or "no". Nobody answering within seven days lapses it,
and {requester} is told that too.
"""

#: What the Operator says to the person. FIXED, and the send-time gate refuses a
#: reply that promises a routine change without this tool having run, so the
#: sentence and the act cannot come apart. It promises exactly one thing that
#: has already happened (the request was passed on) and nothing about the
#: future, because nothing about the future has been decided.
FIXED_REPLY = (
    "Routines, schedules, channels, memory, autonomy and turning things on or "
    "off are changes SMD makes rather than ones I can make myself. I have "
    "passed your request to them with your own message attached. Say this to "
    "the person in your own words, and do NOT say when it will happen, whether "
    "it will happen, or that you have started doing it."
)

#: What the Operator says when the request could NOT be passed on. The point of
#: having a second sentence at all: the first one is only true if the send went.
REFUSED_REPLY = (
    "Routines, schedules, channels, memory, autonomy and turning things on or "
    "off are changes SMD makes rather than ones I can make myself, and this "
    "seat COULD NOT pass the request on: {reason}. Tell the person exactly "
    "that, and ask them to send it to SMD themselves. Do not say it was passed "
    "on and do not say it will happen."
)


def summarize(value: Any) -> str:
    """The request as the Operator understood it, bounded and whitespace-folded.

    Not sanitized beyond bounds and line folding: the desk reads the person's
    own message anyway (its id is in the body), so this is a subject line for a
    human, not a record anything acts on.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return ""
    folded = " ".join(text.split())
    return folded[:MAX_SUMMARY_CHARS]


def build(
    *,
    sender: str,
    summary: str,
    proposal_id: str,
    message_id: str = "",
    customer_slug: str = "",
) -> dict:
    """The message to SMD: recipient, subject, body.

    ``proposal_id`` is the broker-minted eight-hex id of the row this request was
    recorded under, and it is REQUIRED rather than optional (ss-console#2546): it
    is the whole of how SMD's answer finds its way back to the person who asked,
    so a message built without one would be the old silence in a new envelope. It
    is rendered into the subject as well as the body.

    ``message_id`` is the id of the email the person wrote the request in. It is
    quoted verbatim, and when it is missing the body says so rather than
    omitting the line: a desk that cannot find the original needs to know that
    is why, not to wonder whether it looked properly.
    """
    sender = sender.strip() if isinstance(sender, str) else ""
    ref = message_id.strip() if isinstance(message_id, str) else ""
    tag = proposal_id.strip().lower() if isinstance(proposal_id, str) else ""
    requester = sender or "The person who asked"
    return {
        "to": [SMD_OPERATIONS_DESK],
        "subject": SUBJECT.format(sender=sender or "an unattributed sender", proposal_id=tag),
        "text": BODY.format(
            sender=sender or "An unattributed sender",
            slug=(customer_slug or "").strip() or "client",
            summary=summary or "(the Operator recorded no summary)",
            message_ref=f"message {ref}" if ref else "not identified by the seat",
            proposal_id=tag,
            requester=requester,
        ),
    }


__all__ = [
    "BODY",
    "FIXED_REPLY",
    "MAX_SUMMARY_CHARS",
    "REFUSED_REPLY",
    "SMD_OPERATIONS_DESK",
    "SUBJECT",
    "build",
    "summarize",
]
