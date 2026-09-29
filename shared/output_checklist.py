"""Would a good paralegal have sent, filed or handed this over as is? (ss-console, Option B)

THE STANDARD. On 2026-09-29 a Smokeball file note on the pilot showed a 9:30 a.m.
hearing as 4:30 PM, because Smokeball returns UTC and only one skill converted it
(``vfy_01M3PWQQPSVGDFY33XQ52RSYFB``). Sixty-six commits on the case-manager skills
had not stopped it, because every fix moved one transform out of the model for one
skill and nothing read the finished note against a standard. The Captain approved
a sixteen-item output checklist that day. This module is the part of it a machine
can decide: items 1 (local time, never UTC or an ISO stamp), 2 (no internal ids),
3 (plain sentences: no markup, no em dash, no exclamation mark, no capitals for
emphasis), 7 (a staff email fits a phone screen) and 12 (a file note is short and
carries no table). The rest of the checklist is a read-through by a person.

WHAT IT IS. A pure scanner in the shape of :mod:`shared.format_check`:
``check(body, surface) -> list[Violation]``, ``describe`` for the model, and
``rule_names`` for the audit row. It decides nothing about disposition; the hooks
in ``plugins/hermes-smd-trust/outbound.py`` and ``plugins/hermes-smd-reply`` do.

SURFACES. Each output kind gets the rules that are true of it:

* ``STAFF_SEND``: every rule, plus a 20-line ceiling. Scanned on :func:`wire_text`,
  the bytes a reader's mail client shows, so a digest whose headings and list
  markers are rendered away on the wire is not refused for carrying them.
* ``EVERY_OUTPUT``: every rule, no line ceiling. A send to anyone who is not staff.
* ``MEMO``: ids, times, tables, html, em dash, exclamation, capitals and a 15-line
  ceiling, scanned RAW. It does not refuse headings, emphasis or blockquotes: the
  connector normalizes those deterministically after this hook runs, and refusing
  them here would refuse every log-memo skill on the day this deploys while
  leaving the normalizer as dead code.
* ``DOCX``: ids and ISO/UTC stamps only. Markdown is the document's input, so its
  markup is not a defect, and separate statements and motion packages carry
  deposition cites like ``22:14-23:2``, so there is no clock rule.
* ``EXTERNAL_REPLY``: ids and ISO/UTC stamps only, for a relayed reply to someone
  outside the firm. A client's own "please help!" quoted back must not hold the
  reply, and no read-through of client replies exists yet.

REPORT-ONLY RULES. ``caps_emphasis`` and ``max_lines`` have an unmeasured false
positive rate (an acronym the allowlist does not know; a long list someone asked
for). They write an audit row and never refuse until the pilot rate is read, the
same measure-before-flip doctrine ``outbound.py`` records for the identifier gate.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass

from shared import report_render
from shared.rule_confirm import RULE_TAG

# ---------------------------------------------------------------------------
# Surfaces and rule sets
# ---------------------------------------------------------------------------

STAFF_SEND = "staff_send"
EVERY_OUTPUT = "every_output"
MEMO = "memo"
DOCX = "docx"
EXTERNAL_REPLY = "external_reply"

_IDS_AND_STAMPS: tuple[str, ...] = ("internal_id", "iso_timestamp", "utc_time")

_EVERY: tuple[str, ...] = (
    "internal_id",
    "iso_timestamp",
    "utc_time",
    "bare_clock",
    "pipe_table",
    "heading_marks",
    "emphasis_marks",
    "html_tag",
    "em_dash",
    "exclamation",
    "caps_emphasis",
)

_MEMO: tuple[str, ...] = (
    "internal_id",
    "iso_timestamp",
    "utc_time",
    "bare_clock",
    "pipe_table",
    "html_tag",
    "em_dash",
    "exclamation",
    "caps_emphasis",
)

#: surface -> (rules, line ceiling or None, scan the wire text rather than raw)
_SURFACES: dict[str, tuple[tuple[str, ...], int | None, bool]] = {
    STAFF_SEND: (_EVERY, 20, True),
    EVERY_OUTPUT: (_EVERY, None, True),
    MEMO: (_MEMO, 15, False),
    DOCX: (_IDS_AND_STAMPS, None, False),
    EXTERNAL_REPLY: (_IDS_AND_STAMPS, None, True),
}

SURFACES: frozenset[str] = frozenset(_SURFACES)

#: Rules that write an audit row and proceed, never refuse, until the pilot's
#: false-positive rate for each has been read.
REPORT_ONLY_RULES: frozenset[str] = frozenset({"caps_emphasis", "max_lines"})

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# A matter id as the connector emits it; the same pattern as
# ``shared/matter_gate.py`` ``_MATTER_ID_RE``.
_GUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
# A digest or token: 32 to 64 hex characters in one run.
_HEX = re.compile(r"\b[0-9a-f]{32,64}\b", re.IGNORECASE)
# A ULID (``shared/ids.py``). The first character is 0-7, so no English word fits.
_ULID = re.compile(r"\b[0-7][0-9A-HJKMNP-TV-Z]{25}\b")
# The retired escalation ack code (``shared/escalation_ledger.py`` ``token_for``).
_ACK = re.compile(r"\bACK-[0-9A-Z]{6}\b")

_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
_UTC = re.compile(
    r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:Z|UTC|GMT|\+00:?00)(?![A-Za-z0-9])|\b(?:UTC|GMT)\b"
)
_CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")
_MERIDIEM = r"(?:a\.?\s?m\b\.?|p\.?\s?m\b\.?|noon\b|midnight\b)"
_MERIDIEM_AFTER = re.compile(r"\s*" + _MERIDIEM, re.IGNORECASE)
_MERIDIEM_ANY = re.compile(r"\d\s*" + _MERIDIEM + r"|\bnoon\b|\bmidnight\b", re.IGNORECASE)
# "2:30-4:00 p.m." / "2:30 to 4 p.m.": the left clock takes the right one's meridiem.
_RANGE_TO_MERIDIEM = re.compile(
    r"\s*(?:-|\u2013|to)\s*\d{1,2}(?::\d{2})?\s*" + _MERIDIEM, re.IGNORECASE
)
# A deposition page:line cite: "Smith Depo. 22:14", "Tr. 5:3", "at p. 12:4".
_CITE_BEFORE = re.compile(r"(?:Depo|Dep\.|Tr\.|Transcript|at p\.|pp\.)")
_CITE_WINDOW = 12
_CITE_RANGE_AFTER = re.compile(r"-\d+:\d+")
_CITE_RANGE_BEFORE = re.compile(r"\d+:\d+-$")

_TABLE_ROW = re.compile(r"^\s*\|")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_EMPHASIS = re.compile(r"\*\*|`|(?<![\w_])__(?=\S)|(?<![\w*])\*(?=[^\s*])[^*\n]*?[^\s*]\*(?![\w*])")
_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>\n]*)?/?>")
_EXCLAMATION = re.compile(r"!(?!=)")
_CAPS = re.compile(r"\b[A-Z]{4,}\b")
_CAPTION = re.compile(r"\sv\.\s|\svs\.\s")

#: Upper-case words a firm writes in capitals because that is their spelling, not
#: because they are shouting.
#:
#: THE DECISION PROCEDURE, stated so nobody has to reverse-engineer it: a run of
#: four or more capitals is emphasis UNLESS (a) it is in this list, (b) it touches
#: a digit or a hyphen (a matter number like ``2026-PI-101``, a discovery set like
#: ``SROG-1``, ``Medi-Cal`` spelled in capitals), or (c) it sits on a caption line
#: (one carrying `` v. `` or `` vs. ``). There is no dictionary and no inference:
#: an acronym this list does not know is a reported row, and the count of those
#: rows is exactly the measured false-positive rate the report-only posture is
#: waiting for. Three-letter words are never checked, so MSC, FSC, RFP, MRI and
#: CCP need no entry.
KNOWN_CAPS: frozenset[str] = frozenset(
    {
        "PLAINTIFF",
        "PLAINTIFFS",
        "DEFENDANT",
        "DEFENDANTS",
        "EXHIBIT",
        "DRAFT",
        "FINAL",
        "ESTATE",
        "DHCS",
        "IOLTA",
        "HIPAA",
        "ERISA",
        "FRCP",
        "LASC",
        "USDC",
        "CACI",
        "JAMS",
        "SROG",
        "FROG",
        "PLLC",
        "GEICO",
        "USAA",
        "CSAA",
        "OSHA",
        "CDCR",
        "SNAP",
        "SSDI",
        "LAPD",
        "LASD",
        "USPS",
        "NHTSA",
        "COVID",
    }
)

_FRAGMENT_LEN = 40


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One broken rule, with the model-facing detail that says how to fix it."""

    rule: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.rule}: {self.detail}"


def wire_text(body: str) -> str:
    """The text a reader's mail client shows for ``body``.

    Mirrors ``_attach_html_body`` in ``plugins/hermes-smd-trust/__init__.py``:
    a send that looks like a report goes out with its text part down-rendered by
    ``render_plain``, anything else goes out exactly as written. Scanning this,
    not the source, is what lets a digest keep its headings and list markers.
    """
    if not isinstance(body, str):
        return ""
    if report_render.looks_like_report(body):
        return report_render.render_plain(body)
    return body


def fingerprint(text: str) -> str:
    """``sha256`` of the wire text: the key the refusal counter uses."""
    return hashlib.sha256(wire_text(text).encode("utf-8")).hexdigest()


def _blank(text: str, start: int, end: int) -> str:
    """``text`` with ``[start, end)`` replaced by spaces, so offsets still line up."""
    return text[:start] + " " * (end - start) + text[end:]


def _fragment(value: str) -> str:
    flat = " ".join(value.split())
    if len(flat) > _FRAGMENT_LEN:
        flat = flat[: _FRAGMENT_LEN - 3] + "..."
    return flat


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _line_text(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start : end if end != -1 else len(text)]


def _first(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    return pattern.search(text)


def _mask_all(pattern: re.Pattern[str], text: str) -> tuple[str, re.Match[str] | None]:
    """Blank every match of ``pattern`` and return the first, for the detail."""
    first = None
    for match in pattern.finditer(text):
        if first is None:
            first = match
        text = _blank(text, match.start(), match.end())
    return text, first


def _is_cite(text: str, match: re.Match[str]) -> bool:
    """True when this H:MM is a deposition page:line cite rather than a clock."""
    if match.group(3) is not None:
        return False  # H:MM:SS is never a cite
    before = text[max(0, match.start() - _CITE_WINDOW) : match.start()]
    if _CITE_BEFORE.search(before):
        return True
    line = _line_text(text, match.start())
    if _MERIDIEM_ANY.search(line):
        return False
    if _CITE_RANGE_AFTER.match(text, match.end()):
        return True
    line_start = text.rfind("\n", 0, match.start()) + 1
    return bool(_CITE_RANGE_BEFORE.search(text[line_start : match.start()]))


def _bare_clock(text: str) -> re.Match[str] | None:
    for match in _CLOCK.finditer(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            continue
        if _MERIDIEM_AFTER.match(text, match.end()):
            continue
        if _RANGE_TO_MERIDIEM.match(text, match.end()):
            continue
        if _is_cite(text, match):
            continue
        return match
    return None


def _em_dash(text: str) -> tuple[int, str] | None:
    for i, ch in enumerate(text):
        if ch == "\u2014":
            return i, ch
        if ch == "\u2013":
            left = text[:i].rstrip(" ")
            right = text[i + 1 :].lstrip(" ")
            if left[-1:].isdigit() and right[:1].isdigit():
                continue  # a numeric range such as "sections 1 to 5" written with an en dash
            return i, ch
    return None


def _caps(text: str) -> re.Match[str] | None:
    for match in _CAPS.finditer(text):
        word = match.group(0)
        if word in KNOWN_CAPS:
            continue
        before = text[match.start() - 1 : match.start()]
        after = text[match.end() : match.end() + 1]
        if before.isdigit() or after.isdigit() or before == "-" or after == "-":
            continue
        if _CAPTION.search(_line_text(text, match.start())):
            continue
        return match
    return None


def _at(text: str, pos: int, what: str) -> str:
    return f"line {_line_of(text, pos)} carries {what}"


def check(body: str, surface: str) -> list[Violation]:
    """Every checklist rule ``body`` breaks on ``surface``. Empty means it complies.

    Returns every violation, one per rule, each quoting the first offending
    fragment, for the reason :func:`shared.format_check.check` gives: a writer
    told about one rule at a time reads the checker as moving the goalposts.
    An unknown surface checks nothing.
    """
    spec = _SURFACES.get(surface)
    if spec is None or not isinstance(body, str) or not body.strip():
        return []
    rules, ceiling, on_wire = spec
    text = wire_text(body) if on_wire else body
    # The act/rule/ops tag is a thing a person replies to by design
    # (``shared/rule_confirm.py``); it is blanked before any rule reads the text.
    text, _ = _mask_all(RULE_TAG, text)

    found: list[Violation] = []
    local_time = (
        "Write the firm's local time the way the firm writes it, converted from the "
        "calendar entry's own time zone, for example Oct 7 at 9:30 a.m."
    )

    # Stamps first, and masked, so one stamp is one violation, not three.
    text, iso = _mask_all(_ISO, text)
    if iso and "iso_timestamp" in rules:
        found.append(
            Violation(
                "iso_timestamp",
                f"{_at(text, iso.start(), 'the machine timestamp ' + repr(iso.group(0)))}. "
                + local_time,
            )
        )
    text, utc = _mask_all(_UTC, text)
    if utc and "utc_time" in rules:
        found.append(
            Violation(
                "utc_time",
                f"{_at(text, utc.start(), 'a UTC time ' + repr(_fragment(utc.group(0))))}. "
                + local_time,
            )
        )
    if "bare_clock" in rules:
        clock = _bare_clock(text)
        if clock:
            found.append(
                Violation(
                    "bare_clock",
                    f"{_at(text, clock.start(), 'the clock time ' + repr(clock.group(0)))} "
                    "with no a.m. or p.m. " + local_time,
                )
            )

    if "internal_id" in rules:
        hit = None
        for pattern in (_GUID, _HEX, _ULID, _ACK):
            text, first = _mask_all(pattern, text)
            if first and (hit is None or first.start() < hit[0]):
                hit = (first.start(), first.group(0))
        if hit:
            found.append(
                Violation(
                    "internal_id",
                    f"{_at(text, hit[0], 'the internal id ' + repr(_fragment(hit[1])))}. "
                    "Name the matter by the firm's own matter number and the client's name.",
                )
            )

    lines = text.split("\n")
    if "pipe_table" in rules:
        for number, line in enumerate(lines, start=1):
            if _TABLE_ROW.match(line) or line.count("|") >= 2:
                remedy = (
                    "A table belongs in a Word document: call render_docx_draft and name "
                    "the file in the note."
                    if surface == MEMO
                    else "Write each row as its own sentence or list line."
                )
                found.append(
                    Violation(
                        "pipe_table",
                        f"line {number} is a table row {_fragment(line)!r}. {remedy}",
                    )
                )
                break
    if "heading_marks" in rules:
        for number, line in enumerate(lines, start=1):
            if _HEADING.match(line):
                found.append(
                    Violation(
                        "heading_marks",
                        f"line {number} begins with pound signs {_fragment(line)!r}. "
                        "Write the heading as a plain sentence.",
                    )
                )
                break
    if "emphasis_marks" in rules:
        hit_em = _first(_EMPHASIS, text)
        if hit_em:
            found.append(
                Violation(
                    "emphasis_marks",
                    f"{_at(text, hit_em.start(), 'markup ' + repr(_fragment(hit_em.group(0))))}. "
                    "Delete the asterisks, underscores or backticks and keep the words.",
                )
            )
    if "html_tag" in rules:
        tag = _first(_HTML_TAG, text)
        if tag:
            found.append(
                Violation(
                    "html_tag",
                    f"{_at(text, tag.start(), 'the tag ' + repr(_fragment(tag.group(0))))}. "
                    "Delete the tag and keep the plain words.",
                )
            )
    if "em_dash" in rules:
        dash = _em_dash(text)
        if dash:
            pos = dash[0]
            around = text[max(0, pos - 15) : pos + 15]
            found.append(
                Violation(
                    "em_dash",
                    f"{_at(text, pos, 'a dash in ' + repr(_fragment(around)))}. "
                    "Use a comma or a period there.",
                )
            )
    if "exclamation" in rules:
        bang = _first(_EXCLAMATION, text)
        if bang:
            around = text[max(0, bang.start() - 25) : bang.end()]
            found.append(
                Violation(
                    "exclamation",
                    f"{_at(text, bang.start(), 'an exclamation mark in ' + repr(_fragment(around)))}. "
                    "End the sentence with a period.",
                )
            )
    if "caps_emphasis" in rules:
        caps = _caps(text)
        if caps:
            found.append(
                Violation(
                    "caps_emphasis",
                    f"{_at(text, caps.start(), 'the capitals ' + repr(caps.group(0)))}. "
                    "Write the word in ordinary case.",
                )
            )
    if ceiling is not None:
        count = sum(1 for line in lines if line.strip())
        if count > ceiling:
            remedy = (
                "File the detail as a Word document with render_docx_draft and point "
                "the note at it."
                if surface == MEMO
                else "Cut it to what the reader must know or act on."
            )
            found.append(Violation("max_lines", f"it is {count} lines (limit {ceiling}). {remedy}"))
    return found


def refusing(violations: list[Violation]) -> list[Violation]:
    """The violations that refuse: everything except the report-only rules."""
    return [v for v in violations if v.rule not in REPORT_ONLY_RULES]


def describe(violations: list[Violation]) -> str:
    """Full detail for the refusal handed to the MODEL, fragments included.

    Safe here for the reason ``format_check.describe`` gives: the model already
    holds the text it composed. Never written to an audit row.
    """
    return " ".join(f"({i}) {v.detail}" for i, v in enumerate(violations, start=1))


def rule_names(violations: list[Violation]) -> str:
    """Rule names ONLY, comma-joined and sorted, for the audit row."""
    return ",".join(sorted({v.rule for v in violations}))


# ---------------------------------------------------------------------------
# The three-strike hold
# ---------------------------------------------------------------------------

#: The refusal on which the message is withheld rather than refused again.
HOLD_AFTER = 3


class RefusalCounter:
    """Checklist refusals per (session, message), bounded and thread-safe.

    Keyed on the message's :func:`fingerprint`, never on the tool, so a cron turn
    that recomposes the same body three times is stopped (the 2026-08-19 loop)
    while an unrelated message in the same session is still scanned on its own.
    """

    def __init__(self, max_entries: int = 512) -> None:
        self._max = max(1, int(max_entries))
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def bump(self, session_id: str, key: str) -> int:
        """Record one refusal and return how many this message has had."""
        slot = (session_id or "", key)
        with self._lock:
            if slot not in self._counts and len(self._counts) >= self._max:
                self._counts.clear()
            count = self._counts.get(slot, 0) + 1
            self._counts[slot] = count
            return count

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


__all__ = [
    "DOCX",
    "EVERY_OUTPUT",
    "EXTERNAL_REPLY",
    "HOLD_AFTER",
    "KNOWN_CAPS",
    "MEMO",
    "REPORT_ONLY_RULES",
    "STAFF_SEND",
    "SURFACES",
    "RefusalCounter",
    "Violation",
    "check",
    "describe",
    "fingerprint",
    "refusing",
    "rule_names",
    "wire_text",
]
