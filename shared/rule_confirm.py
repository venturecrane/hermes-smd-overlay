"""Does this reply confirm a rule the Operator stated back? (ss-console#2529)

THE PROBLEM THIS SOLVES, AND WHY IT IS NOT "ASK THE MODEL". The Operator states
a rule back with a tag and asks the person to confirm. Their answer arrives as
an ordinary email: one word of their own on top of a full quoted thread, a
signature, and whatever the mail client bolted on. Deciding "did they say yes,
and to WHICH rule" from that, by judgment, on a turn that also has to do the
person's actual work, is exactly the kind of decision that looks right in
ninety-nine transcripts and installs a firm-wide rule off a stray "ok" in the
hundredth. So it is a function, with a table, and the table is tested.

THREE THINGS MUST LINE UP, each in its own place:

1. **A tag, anywhere in the message.** ``[rule 7f3a2c1d]``. Anywhere is
   deliberate: on most mail clients the person's "yes" sits above a quoted copy
   of the readback, so the tag is in the quoted half. Requiring it in their own
   text would fail the ordinary case.
2. **An affirmative in their OWN text**, after the prompt preamble, quoted
   history and signature are stripped. This is the half that must NOT read
   anything but the person: the readback the Operator sent says "Reply yes to
   confirm", and a quoted copy of it therefore contains the word "yes", so
   testing the whole message would let the Operator confirm its own proposal.
   The preamble is the same failure from the other end and it is the one that
   was live (2026-08-21, pilot seat): on the email lane the whole turn prompt
   reaches this module, and the instruction block above the untrusted-body
   delimiter says "never as instructions" and "do NOT use a direct-send tool"
   -- our own words, carrying "never", "not" and "no", which trip the DEFEATERS
   below. Every real email therefore read as qualified or declined, and the
   unit tests could not see it because they passed bare bodies. So the person's
   own text starts at :func:`email_body`, not at the top of the prompt.
3. **The sender's standing over that particular rule.** A bare affirmative
   binds only to rules the sender stated themselves. "apply that" binds only to
   rules waiting on an admin, and only when the sender is one.

Anything less than all three, and the Operator ASKS rather than guessing. An
affirmative with no tag, a tag with no affirmative, two rules the sender could
mean: each produces a question, which costs one reply. Guessing costs a rule the
firm never agreed to.

A NEGATION ANYWHERE IN THEIR OWN TEXT DEFEATS THE WHOLE THING, including the
words that mean "not quite": ``change``, ``instead``, ``but``, ``rather``. "Yes
but make it letters only" is not a yes to the rule as stated, and the readback
is worth nothing if a qualified answer commits the unqualified sentence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from shared.inbound import UNTRUSTED_EMAIL_DELIMITER

#: The tag the Operator puts in the readback: eight lowercase hex, in brackets.
#: Pinned to the broker's proposal-id shape (ss-console
#: operator/workspace_broker/establishment.py) and the applier's
#: ``_ADJUSTMENT_ID``; all three must agree or a confirmed rule cannot be found.
#:
#: TWO KINDS, ONE CHANNEL (ss-console operator-own-matter). ``[rule XXXXXXXX]``
#: is a sentence to install; ``[act XXXXXXXX]`` is a call to make. They share
#: this matcher because they share the thing that matters: a person read one
#: sentence and answered it, and the seat has to decide what they answered
#: without guessing. The id is broker-minted and unique across both, so binding
#: is by id and the word only tells the reader which sort of thing they are
#: looking at.
RULE_TAG = re.compile(r"\[(rule|act|ops) ([0-9a-f]{8})\]")

#: The kinds :func:`find_tags` returns unless a caller names others. RULE and
#: ACT are things a person at the FIRM answers, and they are the only two the
#: confirmation matcher may ever see; OPS is deliberately absent from the default
#: (ss-console#2546). An ``[ops XXXXXXXX]`` tag names a change only SMD makes, so
#: a message quoting one is not an answer to anything the firm can confirm, and a
#: default that returned it would make an SMD reply read as "which rule do you
#: mean?" to the firm's own matcher.
CONFIRMABLE_TAG_KINDS: tuple[str, ...] = ("rule", "act")

#: The tag word an operations request carries. Its own constant because three
#: places key on it and a typo in any of them is a silent miss.
OPS_TAG_KIND = "ops"

#: The proposal kind the broker stores an operations request under. Mirrors
#: ``OPS_REQUEST_KIND`` in ss-console ``operator/workspace_broker/establishment.py``.
OPS_REQUEST_KIND = "ops_request"

#: A line that begins the quoted history. Everything from here down is somebody
#: else's words (usually ours, quoted back), and none of it is the reply.
_QUOTE_HEADER = re.compile(
    r"\A\s*(?:"
    r"On\b.*\bwrote:"  # Gmail, Apple Mail, most clients
    r"|From:\s"  # Outlook block header
    r"|Sent:\s"
    r"|-{2,}\s*Original Message"
    r"|-{2,}\s*Forwarded message"
    r"|_{5,}"  # Outlook's horizontal rule above the quoted header
    r")",
    re.IGNORECASE,
)

#: The signature separator, per RFC 3676 §4.3: a line of exactly "-- ". Matched
#: with an optional trailing space because plenty of clients strip it.
_SIGNATURE = re.compile(r"\A--\s*\Z")

#: Plain agreement. Each binds ONLY to a rule the sender stated themselves.
BARE_AFFIRMATIVES: tuple[str, ...] = (
    "yes please",
    "yes",
    "yep",
    "yeah",
    "correct",
    "that's right",
    "thats right",
    "confirmed",
    "confirm",
    "go ahead",
    "do it",
    "okay",
    "ok",
    "sounds good",
    "that works",
)

#: An admin putting somebody else's rule in force. A separate class from the
#: bare affirmatives because it means something different: not "yes, that is
#: what I said" but "yes, do what THEY said", which is an authority only an
#: admin holds.
APPLY_TOKENS: tuple[str, ...] = ("apply that", "apply it", "apply theirs")

#: Anything here in the sender's own text defeats an affirmative. The last four
#: are not negations in the dictionary sense and are the important ones: "yes
#: but make it letters only" is not a yes to the rule as stated, and a readback
#: that commits the unqualified sentence anyway was never a control.
DEFEATERS: tuple[str, ...] = (
    "no",
    "not",
    "nope",
    "don't",
    "dont",
    "do not",
    "never",
    "cancel",
    "stop",
    "hold off",
    "wait",
    "actually",
    "change",
    "instead",
    "but",
    "rather",
    "except",
    "unless",
)

#: An explicit refusal, in the sender's OWN words. A strict subset of
#: :data:`DEFEATERS`, and the distinction is the whole of ss-console#2546's
#: change to this module. Before it, ANY defeater with a named tag produced
#: DECLINED, which was harmless while a decline only shaped a sentence the model
#: said. It is not harmless now: a decline calls a broker verb, writes a row, and
#: emails the person who asked to tell them their rule was refused. "Wait, which
#: letters?" and "hold off until Monday" both contain defeaters and neither is a
#: refusal, so neither may spend somebody else's request.
DECLINE_TOKENS: tuple[str, ...] = (
    "no",
    "nope",
    "decline",
    "declined",
    "reject",
    "rejected",
    "do not apply",
    "don't apply",
    "dont apply",
    "not applying",
)

#: Verdict kinds returned by :func:`resolve`.
CONFIRMED = "confirmed"
DECLINED = "declined"
ASK = "ask"
NONE = "none"

#: Why an ASK was returned. Each is a different sentence to the person.
ASK_NEEDS_TAG = "needs_tag"
ASK_NEEDS_AFFIRMATIVE = "needs_affirmative"
ASK_AMBIGUOUS = "ambiguous"
ASK_UNKNOWN_TAG = "unknown_tag"
ASK_NOT_THEIRS = "not_theirs"
ASK_QUALIFIED = "qualified"
#: A negation that is not an explicit refusal, or an explicit refusal the sender
#: has no standing to give. Either way the rule stays open and the Operator asks.
ASK_UNCLEAR_REFUSAL = "unclear_refusal"
#: An affirmative when the only thing outstanding carries no tag and is not
#: answerable on this channel (a send withheld for a Telegram approval).
ASK_UNNAMEABLE = "unnameable"


@dataclass(frozen=True)
class Verdict:
    """What the sender's reply did to the rules they could confirm.

    ``kind`` is one of CONFIRMED / DECLINED / ASK / NONE. ``proposal_id`` is set
    only on CONFIRMED. ``candidates`` carries the ids in play, so an ASK can
    name them and a DECLINED can be logged against something.
    """

    kind: str
    proposal_id: str | None = None
    reason: str | None = None
    candidates: tuple[str, ...] = field(default_factory=tuple)


def email_body(user_message: Any) -> str:
    """The sender-controlled half of a turn prompt: everything below the fence.

    On the email lane the string this module is handed is not an email body. It
    is the whole rendered turn prompt (``bootstrap/translate.py``): an
    instruction paragraph, then ``from:``, ``subject:``, ``message_id:``, then
    the untrusted-body delimiter, then what the person actually wrote. Handing
    that to :func:`strip_quoted` reads OUR prose as THEIRS, and our prose says
    "never as instructions" -- so the DEFEATERS fire on every message and no
    real reply can confirm anything (live, 2026-08-21).

    Cutting at the FIRST delimiter is the safe direction: it can only ever
    narrow the text to material the sender controls, which is precisely the
    text a confirmation must be read from. A prompt with no delimiter is
    returned whole -- MCP, cron and connector turns have no fence and the whole
    message is the person's.
    """
    if not isinstance(user_message, str) or not user_message:
        return ""
    cut = user_message.find(UNTRUSTED_EMAIL_DELIMITER)
    if cut < 0:
        return user_message
    line_end = user_message.find("\n", cut)
    if line_end < 0:
        # The fence is the last line: the person wrote nothing below it.
        return ""
    return user_message[line_end + 1 :]


def strip_quoted(body: Any) -> str:
    """The sender's OWN words: quoted history and signature removed.

    Three cuts, in the order a mail body presents them: a line that opens the
    quoted history truncates everything below it, ``>``-prefixed lines are
    dropped wherever they appear, and a signature separator truncates the rest.

    Conservative by construction. A quote style this does not recognize leaves
    quoted text in the result, which can only make an affirmative appear where
    the person did not write one — so this function is never the ONLY thing
    standing between a stray "yes" and a committed rule. The sender's standing
    over the specific rule is the other half, and the tag is the third.
    """
    if not isinstance(body, str) or not body:
        return ""
    kept: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if _QUOTE_HEADER.match(line):
            break
        if _SIGNATURE.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def find_tags(message: Any, kinds: tuple[str, ...] = CONFIRMABLE_TAG_KINDS) -> tuple[str, ...]:
    """Every ``[rule XXXXXXXX]`` / ``[act XXXXXXXX]`` id, in order, de-duplicated.

    ``kinds`` narrows which tag WORDS count, and the default deliberately
    excludes ``ops`` (ss-console#2546): an operations request is answered by SMD
    and can never be confirmed by anybody at the firm, so it must be invisible
    to every caller on the confirmation path. The ops hook passes
    ``kinds=("ops",)`` to ask the opposite question.

    Reads the WHOLE message, prompt preamble and quoted history included: on
    most mail clients the person's "yes" sits above a quoted copy of the
    readback, so the quoted half is exactly where the tag will be, and on the
    email lane a "Re:" subject rendered ABOVE the untrusted-body fence can
    carry it too. Unlike :func:`read_own_text` this is safe to widen: the
    preamble contains no tag, and a tag on its own commits nothing -- it names
    which rule an affirmative would be about, and the affirmative is still read
    from the person's own words alone.
    """
    if not isinstance(message, str) or not message:
        return ()
    seen: list[str] = []
    for match in RULE_TAG.finditer(message):
        if match.group(1) not in kinds:
            continue
        tag = match.group(2)
        if tag not in seen:
            seen.append(tag)
    return tuple(seen)


def _contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word/phrase containment.

    Word boundaries matter more than they look: a substring test makes "but"
    match "attribute" and "no" match "another", so every reply would read as a
    refusal. Apostrophes are normalized before this is called.
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _normalize(text: str) -> str:
    """Lowercase, curly quotes folded to straight, whitespace collapsed."""
    lowered = text.lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", lowered).strip()


@dataclass(frozen=True)
class Reading:
    """What the sender's own text says, before any rule is considered."""

    affirmative: bool
    apply_others: bool
    negated: bool
    #: An EXPLICIT refusal was written, not merely a word that defeats a yes.
    declining: bool = False


def read_own_text(body: Any) -> Reading:
    """Classify the sender's own words.

    Neither our prompt preamble nor quoted history reaches the classifier: the
    fence comes off first (:func:`email_body`), the quote and signature second
    (:func:`strip_quoted`). Both halves are load-bearing and each was, on its
    own, enough to make every reply unreadable in one direction or the other.
    """
    text = _normalize(strip_quoted(email_body(body)))
    if not text:
        return Reading(affirmative=False, apply_others=False, negated=False)
    negated = any(_contains_phrase(text, token) for token in DEFEATERS)
    apply_others = any(_contains_phrase(text, token) for token in APPLY_TOKENS)
    bare = any(_contains_phrase(text, token) for token in BARE_AFFIRMATIVES)
    declining = any(_contains_phrase(text, token) for token in DECLINE_TOKENS)
    return Reading(
        affirmative=bare or apply_others,
        apply_others=apply_others,
        negated=negated,
        declining=declining,
    )


#: Verdict kinds returned by :func:`read_ops_reply`. ``DONE`` and ``OPS_DECLINED``
#: each END an operations request in the broker; ``OPS_NONE`` leaves it open.
OPS_DONE = "done"
OPS_DECLINED = "declined"
OPS_NONE = "none"

#: Ceiling on the quoted note. Mirrors ``MAX_OUTCOME_REASON`` in ss-console
#: ``operator/workspace_broker/establishment.py``, which re-normalizes and
#: re-truncates whatever this sends; bounding it here too keeps the sentence the
#: seat logs and the sentence the broker stores the same one.
MAX_OPS_NOTE = 300

#: A line that is nothing but a greeting, optionally with a first name. Skipped
#: when looking for the answer line, because "Hi,\n\ndone" is one of the two
#: commonest shapes a real reply takes and reading only the FIRST non-empty line
#: would answer "Hi," instead of "done". Skipping can only ever move the read
#: DOWN to the person's actual sentence: a line that carries any other word does
#: not match, so nothing meaningful can be skipped past.
_OPS_GREETING_ONLY = re.compile(
    r"\A(?:hi|hey|hello|morning|good (?:morning|afternoon|evening)|thanks|thank you|sure|ok|okay)"
    r"(?:\s+[a-z][a-z'\-]{0,30})?[\s,.:;!-]*\Z",
    re.IGNORECASE,
)

#: SMD saying the change is made. ``yes`` is DELIBERATELY absent (critique item
#: 7): a bare "yes" on this channel is as likely to be agreement with the request
#: as a statement that it was carried out, and the difference is a person being
#: told a routine exists when it does not. These four words all assert a
#: completed act.
_OPS_DONE_TOKENS = ("done", "set up", "applied", "installed")

#: A done token at the start of a CLAUSE, not merely anywhere in the line. The
#: clause boundary is what makes "No problem, done." read as done while leaving
#: "the digest is not done yet" alone.
_OPS_DONE = re.compile(
    r"(?:\A|(?<=[,;:.\-])\s*)(" + "|".join(_OPS_DONE_TOKENS) + r")\b[\s,.;:!\-]*",
    re.IGNORECASE,
)

#: A bare "no" that IS the answer: at the start of the line and followed by
#: punctuation or the end of it. "No, not in this package" is a refusal; "No
#: problem, done." is not, and that one case is why this is anchored and
#: punctuated rather than a word search.
_OPS_NO_BARE = re.compile(r"\Ano\b\s*(?:[,.;:!\-]+\s*|\Z)", re.IGNORECASE)

#: A "no" carrying its own defeater: "no, cannot do that", "no not this month".
_OPS_NO_DEFEATER = re.compile(
    r"\Ano\b\s+(?=(?:not|can't|cant|cannot|won't|wont|declin(?:e|ed|ing))\b)",
    re.IGNORECASE,
)

#: A refusal that never says "no" at all. The whole line rides the notice as the
#: quoted note in this case, because "Can't do that this month" reads as a
#: refusal only WITH its opening word.
_OPS_REFUSAL_OPENER = re.compile(
    r"\A(?:declin(?:e|ed|ing)|reject(?:ed)?|not going|not possible|can't|cant|cannot|won't|wont)\b",
    re.IGNORECASE,
)

#: Any proposal tag, stripped out of a quoted note before it travels. The note
#: rides an email the Operator sends under its own name to the person who asked,
#: and a tag inside it would be a capability the seat handed onward: quoting an
#: ``[ops XXXX]`` is how an answer binds to a request, so a note carrying one
#: would let the requester's reply resolve something.
_OPS_TAG_IN_NOTE = re.compile(r"\[(?:rule|act|ops) [0-9a-f]{8}\]")


@dataclass(frozen=True)
class OpsReading:
    """What SMD's reply said about one operations request.

    ``verdict`` is :data:`OPS_DONE`, :data:`OPS_DECLINED` or :data:`OPS_NONE`.
    ``note`` is SMD's own remaining words on that line, quoted and never
    paraphrased — the Operator composing its own account of somebody else's
    business decision would be inventing client-facing content.
    """

    verdict: str
    note: str = ""


def _ops_answer_line(message: Any) -> str:
    """SMD's own answer line: below the fence, above the quote, past the hello.

    The same two cuts a confirmation gets (:func:`email_body` then
    :func:`strip_quoted`) and for the same two reasons — our prompt preamble
    carries "never" and "not", and a quoted copy of the request carries the word
    "done" from the instructions we wrote into it. Reading either would make
    every reply answer itself.
    """
    own = strip_quoted(email_body(message))
    if not own:
        return ""
    for raw in own.splitlines():
        line = raw.strip()
        if not line or _OPS_GREETING_ONLY.match(line):
            continue
        return line
    return ""


def _ops_note(text: str) -> str:
    """One line of SMD's own words, bounded, tag-free, ``""`` when there are none."""
    folded = re.sub(r"\s+", " ", _OPS_TAG_IN_NOTE.sub("", text)).strip()
    folded = folded.strip(" ,;:-")
    return folded[:MAX_OPS_NOTE]


def read_ops_reply(message: Any) -> OpsReading:
    """Did SMD say the change is made, or that they will not make it?

    A FUNCTION WITH A TABLE, for the reason :func:`resolve` is one: the answer
    ends a request in the broker and sends the person who asked a letter saying
    what SMD decided, so "is this a yes" cannot be a judgment made on a turn that
    has other work to do.

    THREE VERDICTS AND NO FOURTH. :data:`OPS_DONE` and :data:`OPS_DECLINED` each
    end the row; :data:`OPS_NONE` leaves it open and is the answer to everything
    ambiguous — an SMD reply that says "looking at it" must not resolve anything,
    because the requester would then be told the matter is settled.

    THE REFUSAL IS READ NARROWLY AND FIRST. "No" counts only at the start of the
    answer line and only when it is followed by punctuation or by its own
    defeater, so "No problem, done." is a done rather than a refusal — which is
    not a curiosity, it is how people actually write the message.
    """
    line = _ops_answer_line(message)
    if not line:
        return OpsReading(verdict=OPS_NONE)
    match = _OPS_NO_BARE.match(line) or _OPS_NO_DEFEATER.match(line)
    if match is not None:
        return OpsReading(verdict=OPS_DECLINED, note=_ops_note(line[match.end() :]))
    if _OPS_REFUSAL_OPENER.match(line):
        # The WHOLE line, opener included: "Can't do that this month" reads as a
        # refusal only with the words that make it one, and a note that dropped
        # them would quote SMD as having written "do that this month".
        return OpsReading(verdict=OPS_DECLINED, note=_ops_note(line))
    done = _OPS_DONE.search(line)
    if done is not None:
        return OpsReading(verdict=OPS_DONE, note=_ops_note(line[done.end() :]))
    return OpsReading(verdict=OPS_NONE)


def _sender_may_confirm(
    row: dict[str, Any], sender: str, is_admin: bool, apply_others: bool
) -> bool:
    """Whether THIS sender, saying THIS kind of affirmative, may confirm THIS rule.

    Two lanes, deliberately disjoint:

    * a rule waiting on an admin (``for_admin``) is put in force by an admin
      saying "apply that". The person who stated it cannot confirm their own,
      which is the entire point of it waiting;
    * every other rule is confirmed by the person who stated it, saying yes.
      An admin's own firm rule is this lane, because they already had the
      authority when they stated it.

    AN ACT IS A THIRD LANE (``kind == "tool_call"``, ss-console
    operator-own-matter), and it is admin-only in both directions. A commitment
    is the firm's employee doing something to the firm's system of record, so the
    only person whose yes counts is one the firm authored as an Operator
    administrator; and because an act is only ever PROPOSED on an administrator's
    own turn, the ordinary "did you state this yourself" test would be the wrong
    question. Any administrator may answer it, with a plain yes or with "apply
    that", and nobody else may answer it at all.
    """
    if str(row.get("kind") or "rule") == "tool_call":
        return is_admin
    for_admin = bool(row.get("for_admin"))
    stated_by = str(row.get("instructed_by") or "").strip().lower()
    if apply_others:
        return for_admin and is_admin
    return not for_admin and stated_by == sender


def may_decline(row: dict[str, Any], sender: Any, is_admin: bool) -> bool:
    """Whether THIS sender may refuse THIS rule ON SOMEBODY ELSE'S BEHALF.

    NOT a test of whether the reply reads as a refusal: that is
    :func:`resolve`'s job, and a person refusing their OWN rule is a perfectly
    ordinary decline that this returns False for. This answers the narrower
    question the ACT needs: does saying no here close somebody else's request
    in the broker and send them a letter about it.

    Three conditions, and each closes a different way a decline could be
    manufactured (ss-console#2546):

    * the row is ``for_admin``: a rule somebody stated about their own work is
      not another person's to refuse; they simply do not confirm it;
    * the sender is an Operator admin, because refusing a firm-level request is
      an act of authority, and the seat classifies that against the authored list
      rather than believing the message;
    * the sender is not the person who stated it, because one address that could
      both raise and refuse a request is a loop with no second person in it, and
      the row would read as though somebody in authority had answered.

    An ACT is out of scope here and stays so: declining an act is "do not do
    it", which is what happens anyway when nobody confirms it.
    """
    if str(row.get("kind") or "rule") == "tool_call":
        return False
    if not bool(row.get("for_admin")) or not is_admin:
        return False
    address = str(sender or "").strip().lower()
    return str(row.get("instructed_by") or "").strip().lower() != address


def _declined_or_ask(
    named: list[dict[str, Any]],
    named_ids: tuple[str, ...],
    reading: Reading,
) -> Verdict:
    """A negation that named a rule: a real refusal, or a question?

    Two conditions, and ss-console#2546 added both, because DECLINED can now
    SPEND something: on a rule somebody else asked for it closes the proposal in
    the broker and emails them to say so. So it is reached only on an EXPLICIT
    refusal (a word from :data:`DECLINE_TOKENS`, not merely any defeater) over
    EXACTLY ONE named rule.

    "Wait, which letters?" carries a defeater and is a question; before this it
    read as a refusal, which was harmless while a decline only shaped a
    sentence. "No" over two quoted tags refuses something the seat cannot
    identify, so it asks which.

    WHOSE rule it is, and whether this sender has the standing to close it, is
    deliberately NOT decided here: a person refusing their OWN rule is an
    ordinary decline that spends nothing, and reporting that as a question would
    ask somebody to repeat themselves. :func:`may_decline` answers that separate
    question where the broker verb is actually called.
    """
    if not reading.declining or len(named) != 1:
        return Verdict(kind=ASK, reason=ASK_UNCLEAR_REFUSAL, candidates=named_ids)
    return Verdict(
        kind=DECLINED,
        proposal_id=str(named[0].get("proposal_id") or ""),
        candidates=named_ids,
    )


def resolve(
    message: Any,
    pending: list[dict[str, Any]],
    sender: Any,
    is_admin: bool,
    extra_open: int = 0,
) -> Verdict:
    """Decide what this reply did, over the items this sender has outstanding.

    ``pending`` is the broker's ``establish_pending`` list for this sender (its
    ``for_admin`` rows included when the sender is an admin), rules and acts
    alike. Every branch that is not a clean single match returns ASK, because the
    cost of asking is one reply and the cost of guessing is a rule the firm never
    agreed to, or a call it never asked for.

    ``extra_open`` counts outstanding things that carry NO tag and so can never
    be named: today that is a withheld send waiting on a Telegram yes. They
    cannot be confirmed here, but they can make a bare affirmative ambiguous, so
    they are counted when deciding whether to ask.
    """
    # ss-console#2546. AN OPERATIONS ROW IS NOT SOMETHING ANYBODY HERE CAN
    # ANSWER, and it is dropped before the matcher sees it rather than refused
    # afterwards. The broker's ``open_for`` already excludes the kind in SQL, so
    # on the live path this filter never fires; it is here because "the firm
    # accidentally installed a routine change by saying yes" is the failure
    # worth a refusal at every layer that could pass one through, and a caller
    # that fetches rows some other way must not become the exception.
    rows = [
        r
        for r in pending
        if isinstance(r, dict)
        and r.get("proposal_id")
        and str(r.get("kind") or "rule") != OPS_REQUEST_KIND
    ]
    if not rows:
        if extra_open > 0 and read_own_text(message).affirmative:
            # Something IS waiting, it just is not something a tag can name, and
            # it is not approved on this channel. Do not silently do nothing: the
            # person believes they just answered a question.
            return Verdict(kind=ASK, reason=ASK_UNNAMEABLE)
        return Verdict(kind=NONE)
    address = str(sender or "").strip().lower()
    tags = find_tags(message)
    reading = read_own_text(message)

    if not tags:
        if reading.affirmative and not reading.negated:
            # They agreed with something, and there is something to agree with,
            # but nothing says which. Asking is cheap; picking is not.
            return Verdict(
                kind=ASK,
                reason=ASK_NEEDS_TAG,
                candidates=tuple(str(r["proposal_id"]) for r in rows),
            )
        return Verdict(kind=NONE)

    by_id = {str(r["proposal_id"]): r for r in rows}
    named = [by_id[t] for t in tags if t in by_id]
    if not named:
        return Verdict(kind=ASK, reason=ASK_UNKNOWN_TAG, candidates=tags)

    named_ids = tuple(str(r["proposal_id"]) for r in named)
    if reading.negated:
        # A QUALIFIED answer is not a decline, and must not be reported as one.
        # "Yes but make it letters only" contains both an agreement and a
        # correction; committing the unqualified sentence is wrong, and telling
        # the person they declined is also wrong. The Operator restates.
        if reading.affirmative:
            return Verdict(kind=ASK, reason=ASK_QUALIFIED, candidates=named_ids)
        return _declined_or_ask(named, named_ids, reading)
    if not reading.affirmative:
        return Verdict(kind=ASK, reason=ASK_NEEDS_AFFIRMATIVE, candidates=named_ids)

    eligible = [r for r in named if _sender_may_confirm(r, address, is_admin, reading.apply_others)]
    if len(eligible) == 1:
        return Verdict(
            kind=CONFIRMED,
            proposal_id=str(eligible[0]["proposal_id"]),
            candidates=named_ids,
        )
    if not eligible:
        return Verdict(kind=ASK, reason=ASK_NOT_THEIRS, candidates=named_ids)
    return Verdict(
        kind=ASK,
        reason=ASK_AMBIGUOUS,
        candidates=tuple(str(r["proposal_id"]) for r in eligible),
    )


__all__ = [
    "APPLY_TOKENS",
    "ASK_UNCLEAR_REFUSAL",
    "ASK",
    "ASK_AMBIGUOUS",
    "ASK_NEEDS_AFFIRMATIVE",
    "ASK_NEEDS_TAG",
    "ASK_NOT_THEIRS",
    "ASK_QUALIFIED",
    "ASK_UNKNOWN_TAG",
    "ASK_UNNAMEABLE",
    "BARE_AFFIRMATIVES",
    "CONFIRMABLE_TAG_KINDS",
    "CONFIRMED",
    "DECLINED",
    "DECLINE_TOKENS",
    "DEFEATERS",
    "MAX_OPS_NOTE",
    "NONE",
    "OPS_DECLINED",
    "OPS_DONE",
    "OPS_NONE",
    "OPS_REQUEST_KIND",
    "OPS_TAG_KIND",
    "OpsReading",
    "RULE_TAG",
    "Reading",
    "Verdict",
    "email_body",
    "find_tags",
    "may_decline",
    "read_ops_reply",
    "read_own_text",
    "resolve",
    "strip_quoted",
]
