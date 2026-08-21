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
2. **An affirmative in their OWN text**, after quoted history and signature are
   stripped. This is the half that must NOT read the quote: the readback the
   Operator sent says "Reply yes to confirm", and a quoted copy of it therefore
   contains the word "yes". Testing the whole message would let the Operator
   confirm its own proposal.
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

#: The tag the Operator puts in the readback: eight lowercase hex, in brackets.
#: Pinned to the broker's proposal-id shape (ss-console
#: operator/workspace_broker/establishment.py) and the applier's
#: ``_ADJUSTMENT_ID``; all three must agree or a confirmed rule cannot be found.
RULE_TAG = re.compile(r"\[rule ([0-9a-f]{8})\]")

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


def find_tags(message: Any) -> tuple[str, ...]:
    """Every ``[rule XXXXXXXX]`` id in the message, in order, de-duplicated.

    Reads the WHOLE message, quoted history included: on most mail clients the
    person's "yes" sits above a quoted copy of the readback, so that is exactly
    where the tag will be.
    """
    if not isinstance(message, str) or not message:
        return ()
    seen: list[str] = []
    for match in RULE_TAG.finditer(message):
        tag = match.group(1)
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


def read_own_text(body: Any) -> Reading:
    """Classify the sender's own words. Quoted history never reaches here."""
    text = _normalize(strip_quoted(body))
    if not text:
        return Reading(affirmative=False, apply_others=False, negated=False)
    negated = any(_contains_phrase(text, token) for token in DEFEATERS)
    apply_others = any(_contains_phrase(text, token) for token in APPLY_TOKENS)
    bare = any(_contains_phrase(text, token) for token in BARE_AFFIRMATIVES)
    return Reading(affirmative=bare or apply_others, apply_others=apply_others, negated=negated)


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
    """
    for_admin = bool(row.get("for_admin"))
    stated_by = str(row.get("instructed_by") or "").strip().lower()
    if apply_others:
        return for_admin and is_admin
    return not for_admin and stated_by == sender


def resolve(
    message: Any,
    pending: list[dict[str, Any]],
    sender: Any,
    is_admin: bool,
) -> Verdict:
    """Decide what this reply did, over the rules this sender has outstanding.

    ``pending`` is the broker's ``establish_pending`` list for this sender (its
    ``for_admin`` rows included when the sender is an admin). Every branch that
    is not a clean single match returns ASK, because the cost of asking is one
    reply and the cost of guessing is a rule the firm never agreed to.
    """
    rows = [r for r in pending if isinstance(r, dict) and r.get("proposal_id")]
    if not rows:
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
        return Verdict(kind=DECLINED, candidates=named_ids)
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
    "ASK",
    "ASK_AMBIGUOUS",
    "ASK_NEEDS_AFFIRMATIVE",
    "ASK_NEEDS_TAG",
    "ASK_NOT_THEIRS",
    "ASK_QUALIFIED",
    "ASK_UNKNOWN_TAG",
    "BARE_AFFIRMATIVES",
    "CONFIRMED",
    "DECLINED",
    "DEFEATERS",
    "NONE",
    "RULE_TAG",
    "Reading",
    "Verdict",
    "find_tags",
    "read_own_text",
    "resolve",
    "strip_quoted",
]
