"""Plain-word replies to a deadline digest: "got it on 1" acks item 1.

THE CHANGE. A digest used to ask the reader to type an ``ACK-XXXXXX`` code per
item. Staff do not do that, and a great case manager would never ask them to.
The digest now numbers its items, and the reader answers in words. What this
module guarantees is that the words resolve to ledger rows DETERMINISTICALLY:

* the digest a reply answers is found by THREAD, never by what the reply says.
  The broker stamped each raise row with the ``thread_ref`` of the message that
  carried it (``shared/digest_reply_ref.py`` is the send-side half), and the
  reply's verified origin carries the same thread id;
* the numbers are read by :func:`parse_reply_items`, a pure function over the
  reader's OWN words (``InboundOrigin.reply_text``, quoted history removed by
  the provider). A reply of "thanks" above a quoted list parses to nothing;
* each number maps to the raise rows the send stamped with it, in code. A group
  number (a per-matter band) maps to every row in the group.

THE MODEL SUPPLIES NOTHING. The tool schema is an empty object. The session,
the sender, the thread, the text, the numbers and the rows all come from the
verified inbound origin and the ledger. The 2026-07-31 incident is why: keying
acks on model-composed identity silenced nothing (86 fired events, 83 distinct
keys). The model's whole job is to call the tool when a rostered person replies
to a deadline email, and to send back ``confirmation_text`` verbatim.

ALL OR NOTHING. A reply naming a number the digest does not carry writes
nothing and asks. So does a reply the parser cannot read, and a thread holding
more than one digest. Ambiguity is asked, never guessed: a wrong ack silences a
real deadline, and a question costs one more email.

WHAT AN ACK DOES is unchanged: it quiets an item until the snooze lapses; only
completion in Smokeball closes it. The acked row goes through the broker's
existing ``escalation_event_append`` verb exactly as the legacy ``ack_token``
path writes it (which stays, so codes already in inboxes keep working), with
``acked_by`` from the same verified-sender resolution (ss#2152).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from shared import escalation_ledger, inbound
from shared.digest_reply_ref import RAISE_EVENTS, valid_digest_number, valid_snooze_days

logger = logging.getLogger(__name__)

STATUS_ACKED = "acked"
NO_VERIFIED_REPLY = "no_verified_reply"
AUTO_REPLY = "auto_reply"
NOT_ROSTERED = "not_rostered"
NOT_A_DIGEST_REPLY = "not_a_digest_reply"
AMBIGUOUS_THREAD = "ambiguous_thread"
NOTHING_PARSED = "nothing_parsed"
UNKNOWN_NUMBERS = "unknown_numbers"
NOT_RECORDED = "not_recorded"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Record a person's plain-word reply to a deadline email (for example 'got it "
    "on 1', '1 and 3', 'all'). Takes NO arguments: the reply, its sender, the "
    "email it answers and the numbered items are all read from the verified "
    "inbound message and the ledger, and the numbers are matched to items in "
    "code. Call it once when a rostered person replies to a [Deadlines] email "
    "without an ACK- code, then send the returned confirmation_text back to them "
    "verbatim (send nothing when it is empty). Nothing is recorded unless every "
    "number in the reply is on that email's list."
)

# ---------------------------------------------------------------------------
# Parsing the reader's words (pure)
# ---------------------------------------------------------------------------

# A number is a standalone 1..999 token. Digits joined to other digits by
# - . / : are a phone number, a date, a time or a decimal, never an item.
_NUMBER = re.compile(r"(?<![\w])(?<!\d[-./:])([1-9]\d{0,2})(?![\w])(?![-./:]\d)")
_ALL = re.compile(r"\b(all(?:\s+of\s+them)?|every\s+one|everything)\b")
# Words that, within the few words before a number in the same clause, mean
# the reader is NOT confirming it ("got 1, not 2", "still working on 3").
_NEGATORS = frozenset(
    {
        "not",
        "no",
        "except",
        "excluding",
        "but",
        "without",
        "still",
        "yet",
        "haven't",
        "havent",
        "hasn't",
        "hasnt",
        "didn't",
        "didnt",
        "don't",
        "dont",
        "isn't",
        "isnt",
        "can't",
        "cant",
        "cannot",
        "won't",
        "wont",
        "waiting",
        "pending",
    }
)
_NEGATION_WINDOW = 3
# A greeting, a thank-you or a closing is not "all of them": "Hi all",
# "thanks all", "that's all", "1 is all I have".
_ALL_GREETERS = frozenset(
    {
        "hi",
        "hey",
        "hello",
        "thanks",
        "thank",
        "you",
        "morning",
        "afternoon",
        "evening",
        "at",
        "that's",
        "thats",
        "is",
        "was",
    }
)
# Where the reader's words end: the standard signature delimiter, a phone
# client's footer, or a line that is only a sign-off. A signature's phone
# number or suite number must never read as an item.
_SIGNOFF = re.compile(
    r"^\s*(?:--\s*|sent from my\b.*|(?:thanks|thank you|best|regards|best regards|"
    r"kind regards|cheers|sincerely|warmly|respectfully|talk soon)[\s,.!]*)$",
    re.IGNORECASE,
)
# A clause ends at punctuation, but a period inside a number ("1.5") does not.
_CLAUSE = re.compile(r"(?:[,;!?\n]|\.(?!\d))+")
_WORD = re.compile(r"[a-z']+|\d+")
# The digest's item line ("1. matter 2026-PI-101, ..."), quoted or not.
_DIGEST_ITEM_LINE = re.compile(r"^[\s>]*\d{1,3}\.\s+matter\b", re.MULTILINE)


def _own_words(text: str) -> str:
    """The reply above its signature."""
    kept: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if _SIGNOFF.match(line):
            break
        kept.append(line)
    return "\n".join(kept)


def parse_reply_items(text: object) -> dict[str, Any]:
    """The item numbers a reader's reply confirms: ``{"all": bool, "numbers": [...]}``.

    Pure and literal. A number counts only when it appears as a standalone
    1..999 token in the reader's own words, above any signature, and no
    negating word ("not", "except", "still"...) sits in the few words before it
    in the same clause. "all" / "all of them" / "every one" / "everything"
    select every item, unless it is a greeting ("Hi all") or the reply also
    excludes a number ("all except 2"), which is ambiguous and reads as nothing.
    Anything else is ignored.

    A line shaped like the digest's own item line (``N. matter ...``) means the
    quoted list leaked into the text (a quote marker the provider or
    :mod:`shared.reply_text` did not recognize). Then the reader's words cannot
    be told from the list, and the whole reply reads as nothing: asking costs
    one email, acking every quoted number silences real deadlines."""
    if not isinstance(text, str) or not text.strip():
        return {"all": False, "numbers": []}
    words = _own_words(text).lower()
    if _DIGEST_ITEM_LINE.search(words):
        return {"all": False, "numbers": []}
    numbers: set[int] = set()
    negated = False
    for clause in _CLAUSE.split(words):
        for match in _NUMBER.finditer(clause):
            before = _WORD.findall(clause[: match.start()])[-_NEGATION_WINDOW:]
            if any(word in _NEGATORS for word in before):
                negated = True
                continue
            numbers.add(int(match.group(1)))
    select_all = False
    for match in _ALL.finditer(words):
        before = _WORD.findall(words[: match.start()])[-1:]
        if before and before[0] in _ALL_GREETERS | _NEGATORS:
            continue
        select_all = True
    if select_all and negated:
        # "all except 2": the reader named an exception the parser will not
        # model. Ask rather than silence the one item they held back.
        return {"all": False, "numbers": []}
    return {"all": select_all, "numbers": sorted(numbers)}


# ---------------------------------------------------------------------------
# Rendering the reply (code, never the model)
# ---------------------------------------------------------------------------


def _join(numbers: list[int], conjunction: str = "and") -> str:
    parts = [str(n) for n in numbers]
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} {conjunction} {parts[-1]}"


def _span(numbers: list[int]) -> str:
    if len(numbers) == 1:
        return f"The only number was {numbers[0]}."
    if numbers == list(range(numbers[0], numbers[-1] + 1)):
        return f"The numbers were {numbers[0]} to {numbers[-1]}."
    return f"The numbers were {_join(numbers)}."


def render_confirmation(
    status: str,
    *,
    acked: list[int] | None = None,
    still_open: list[int] | None = None,
    failed: list[int] | None = None,
    unknown: list[int] | None = None,
    valid: list[int] | None = None,
    all_selected: bool = False,
    snooze_days: int | None = None,
) -> str:
    """The sentence the seat sends back. Empty means send nothing: an auto-reply,
    an unverified sender and a sender off the roster get no answer at all.
    ``snooze_days`` comes off the raise rows (the skill's own interval); without
    one the sentence says "for now" rather than invent a number."""
    acked = acked or []
    still_open = still_open or []
    if status == STATUS_ACKED:
        if valid_snooze_days(snooze_days):
            period = f"for {snooze_days} day" + ("" if snooze_days == 1 else "s")
        else:
            period = "for now"
        if all_selected and len(acked) > 1 and not failed:
            text = f"Got it: all {len(acked)} are quiet {period}."
        else:
            verb = "is" if len(acked) == 1 else "are"
            text = f"Got it: {_join(acked)} {verb} quiet {period}."
        if still_open:
            text += f" Still open: {_join(still_open)}."
        if failed:
            pronoun = "it" if len(failed) == 1 else "them"
            text += f" I couldn't record {_join(failed)} just now; please send {pronoun} again."
        return text
    if status == NOTHING_PARSED:
        return "Which numbers do you have? Reply with the numbers from the list, or say all."
    if status == UNKNOWN_NUMBERS:
        return f"I don't see {_join(unknown or [], 'or')} on that list. {_span(valid or [])}"
    if status == NOT_A_DIGEST_REPLY:
        return "I couldn't tell which list you're answering. Reply directly to the deadline email."
    if status == AMBIGUOUS_THREAD:
        return (
            "I couldn't tell which list you're answering. Reply directly to the most "
            "recent deadline email."
        )
    if status == NOT_RECORDED:
        return (
            "I couldn't record that just now, so nothing was marked. Please send the numbers again."
        )
    return ""


def _result(status: str, *, acked=None, still_open=None, **render: Any) -> str:
    acked = sorted(acked or [])
    still_open = sorted(still_open or [])
    text = render_confirmation(status, acked=acked, still_open=still_open, **render)
    return json.dumps(
        {
            "status": status,
            "acked": acked,
            "still_open": still_open,
            "confirmation_text": text,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# The digest a thread carries (ledger read)
# ---------------------------------------------------------------------------


def _digest_rows(events: list[dict], thread_ref: str) -> dict[int, dict[str, dict]]:
    """``{n: {item_key: raise_row}}`` for the numbered raises on this thread."""
    rows: dict[int, dict[str, dict]] = {}
    for event in events:
        if event.get("event") not in RAISE_EVENTS:
            continue
        if event.get("thread_ref") != thread_ref:
            continue
        number = event.get("n")
        if not valid_digest_number(number):
            continue
        if escalation_ledger.is_pre_identity_epoch(event):
            continue
        key = str(event.get("item_key") or "")
        if key:
            rows.setdefault(number, {})[key] = event
    return rows


def _dispatch_refs(events: list[dict], thread_ref: str) -> set[object]:
    return {
        event.get("dispatch_ref")
        for event in events
        if event.get("event") in RAISE_EVENTS
        and event.get("thread_ref") == thread_ref
        and valid_digest_number(event.get("n"))
    }


def _one_snooze(digest: dict[int, dict[str, dict]], numbers: list[int]) -> int | None:
    """The ack snooze every acked row agrees on, or ``None``. Rows that carry
    none, or disagree, get "for now": the sentence never states a period one of
    the acked items does not have."""
    values = {row.get("snooze_days") for n in numbers for row in digest[n].values()}
    if len(values) != 1:
        return None
    value = values.pop()
    return value if valid_snooze_days(value) else None


def _quiet(state: escalation_ledger.ItemState | None) -> bool:
    return state is not None and (state.acked or state.resolved or state.handed_off)


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def escalation_reply_ack(
    *,
    session_id: str,
    load_config: Callable[[], Any],
    verified_acker: Callable[[str], dict[str, str] | None],
    broker_request: Callable[[dict], dict],
    ledger_path: str,
) -> str:
    """Resolve this turn's verified reply to its digest and ack what it names.

    Every input is injected by the plugin; nothing comes from the model.
    Returns ``{status, acked, still_open, confirmation_text}`` as JSON."""
    origin = inbound.SESSION_INBOUND_ORIGIN.get(session_id) if session_id else None
    if origin is None or not getattr(origin, "sender_address", ""):
        return _result(NO_VERIFIED_REPLY)
    if getattr(origin, "auto_submitted", False) is True:
        return _result(AUTO_REPLY)
    try:
        rostered = bool(load_config().sender_on_roster(origin.sender_address))
    except Exception:  # noqa: BLE001 — an unreadable roster authorizes nobody
        logger.warning("hermes-smd-escalation: roster unreadable for a digest reply")
        rostered = False
    if not rostered:
        return _result(NOT_ROSTERED)

    thread_ref = getattr(origin, "conversation_id", "") or ""
    events = escalation_ledger.read_ledger(ledger_path) if thread_ref else []
    digest = _digest_rows(events, thread_ref) if thread_ref else {}
    if not digest:
        return _result(NOT_A_DIGEST_REPLY)
    if len(_dispatch_refs(events, thread_ref)) > 1:
        return _result(AMBIGUOUS_THREAD)

    states = escalation_ledger.derive_state(events)
    valid = sorted(digest)
    open_now = [n for n in valid if not all(_quiet(states.get(k)) for k in digest[n])]
    parsed = parse_reply_items(getattr(origin, "reply_text", ""))
    selected = valid if parsed["all"] else parsed["numbers"]
    if not selected:
        return _result(NOTHING_PARSED, still_open=open_now)
    unknown = [n for n in selected if n not in digest]
    if unknown:
        # All or nothing: one number off the list writes no row at all.
        return _result(UNKNOWN_NUMBERS, still_open=open_now, unknown=unknown, valid=valid)

    acker = verified_acker(session_id)
    acked: list[int] = []
    failed: list[int] = []
    for number in selected:
        ok = True
        for key, row in digest[number].items():
            event = {
                "v": escalation_ledger.SCHEMA_VERSION,
                "ts": None,  # the broker stamps ts/id; nobody here can backdate
                "skill": str(row.get("skill") or ""),
                "matter_id": row.get("matter_id"),
                "item_key": key,
                "event": "acked",
                "attempt": int(row.get("attempt") or 0),
                "token": row.get("token"),
                "session_id": session_id,
            }
            if acker is not None:
                event["acked_by"] = acker
            try:
                response = broker_request({"action": "escalation_event_append", "event": event})
            except Exception as exc:  # noqa: BLE001 — one failed row must not lose the rest
                logger.warning("hermes-smd-escalation: reply ack append failed (%s)", exc)
                response = None
            if not (isinstance(response, dict) and response.get("ok")):
                logger.warning("hermes-smd-escalation: reply ack refused (%s)", response)
                ok = False
        (acked if ok else failed).append(number)
    if not acked:
        return _result(NOT_RECORDED, still_open=open_now)
    still_open = sorted({n for n in open_now if n not in acked} | set(failed))
    return _result(
        STATUS_ACKED,
        acked=acked,
        still_open=still_open,
        failed=failed,
        all_selected=parsed["all"],
        snooze_days=_one_snooze(digest, acked),
    )


__all__ = [
    "DESCRIPTION",
    "SCHEMA",
    "escalation_reply_ack",
    "parse_reply_items",
    "render_confirmation",
]
