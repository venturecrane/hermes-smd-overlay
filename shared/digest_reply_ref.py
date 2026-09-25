"""The per-digest map a plain-word reply is resolved against (overlay half).

A deadline digest used to ask the reader to type an ``ACK-XXXXXX`` code per
item. The replacement asks for the item NUMBERS ("got it on 1"), which only
works if the seat can later answer "which ledger rows was item 1 in THAT
message?" deterministically. This module is the send-side half of that map:

* every dispatch of a pre-rendered envelope gets a fresh ``dispatch_ref``
  (uuid4 hex). It rides ``audit_extra`` onto the broker's CONFIRM row, and it
  is stamped on each raise append written after the send, so the broker can
  join the raise to the confirm row and stamp the message's ``thread_ref``
  (the AgentMail thread id / Graph conversation id). The overlay NEVER sets
  ``thread_ref`` itself: the broker is the only party that saw the send.
* each raise append carries the envelope's ``n``: the 1-based number printed
  beside the item in the body. Several appends share one ``n`` when the body
  numbered a per-matter group; answering that number quiets every row in it.
* each raise append also carries the envelope's ``snooze_days`` when authored:
  how long an ack quiets the item (the skill's own re-fire interval), so the
  confirmation can say "quiet for 7 days" from data, not from a constant.

The reply side (``plugins/hermes-smd-escalation/reply_items.py``) reads the
raise rows back by ``thread_ref`` and maps the reader's numbers to them in
code. The model supplies none of it.

Mirrored by ss-console: the pre_run writes ``n`` on each envelope append
(``operator/skills/deadline-miss-escalator``), and the broker allowlists
``dispatch_ref`` on ``audit_extra`` and completes the join
(``operator/workspace_broker/digest_ref.py``).
"""

from __future__ import annotations

import uuid

#: The raise events a digest number may ride. ``handed_off`` is a release, not a
#: raise, and the broker refuses ``n`` on anything that is not a raise.
RAISE_EVENTS = frozenset({"fired", "chased"})

#: Digest numbers are printed as 1..999; the reply parser reads the same range.
MAX_DIGEST_NUMBER = 999
#: An ack snooze is 1..365 days.
MAX_SNOOZE_DAYS = 365


def mint_dispatch_ref() -> str:
    """A fresh, unguessable id for one dispatch (uuid4 hex)."""
    return uuid.uuid4().hex


def valid_digest_number(value: object) -> bool:
    """True iff ``value`` is an int in 1..999 (``bool`` is not a number here)."""
    return (
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_DIGEST_NUMBER
    )


def valid_snooze_days(value: object) -> bool:
    """True iff ``value`` is an int in 1..365 (``bool`` is not a number here)."""
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_SNOOZE_DAYS


def append_digest_fields_ok(append: dict) -> bool:
    """An envelope append's ``n`` and ``snooze_days`` are well-formed: each is
    absent or null (an envelope written before numbering, or a row the body
    printed no number for) or in range. Anything else is malformed, and a
    malformed append refuses the whole envelope, as every other malformed
    field does."""
    number = append.get("n")
    snooze = append.get("snooze_days")
    return (number is None or valid_digest_number(number)) and (
        snooze is None or valid_snooze_days(snooze)
    )


def stamp_raise(event: dict, append: dict, dispatch_ref: str) -> dict:
    """Stamp the digest fields onto one ledger event bound for the broker.

    Only raises carry them. ``n`` and ``snooze_days`` are copied only when the
    envelope authored valid ones; ``thread_ref`` is never set here (the broker
    stamps it)."""
    if event.get("event") not in RAISE_EVENTS or not dispatch_ref:
        return event
    event["dispatch_ref"] = dispatch_ref
    number = append.get("n")
    if valid_digest_number(number):
        event["n"] = number
    snooze = append.get("snooze_days")
    if valid_snooze_days(snooze):
        event["snooze_days"] = snooze
    return event


__all__ = [
    "MAX_DIGEST_NUMBER",
    "MAX_SNOOZE_DAYS",
    "RAISE_EVENTS",
    "append_digest_fields_ok",
    "mint_dispatch_ref",
    "stamp_raise",
    "valid_digest_number",
    "valid_snooze_days",
]
