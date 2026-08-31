"""Identifier-integrity filter — provenance discipline for asserted identifiers.

SOURCE OF TRUTH: ``ss-console/operator/safety-substrate/identifier_filter.py``.
This is a VENDORED copy carried in the overlay so the trust plugin can run the
gate at runtime without a cross-repo import (the overlay cannot runtime-import
ss-console). Keep aligned with ss-console; the shape changes there first.
Alignment is asserted by a CONTRACT test (``tests/test_identifier_filter.py``),
not a byte hash — this is CODE, and formatting/lint deltas between the two repos
would break a byte hash (same rationale as ``shared/inbound.py``).

A fabrication-discipline primitive in the same family as ``citation_filter.py``
(invariant #6) and the spec'd-but-unbuilt fabrication filter (invariant #8,
``docs/specs/operator/fabrication-filter.md``, issue #798). It is NOT invariant
#8 itself — #8 is the broader field-tag + source-existence filter. This module
covers one dimension of fabrication discipline that #798's field-tag approach
does not: **identifiers that appear in the free-text body of a draft.**

The distinction from ``citation_filter``:

- ``citation_filter`` *refuses* citation-shaped strings outright — a law message
  never cites, so any citation is a fabrication.
- ``identifier_filter`` *permits* identifiers but only when **provenance-
  verified**: every identifier-shaped token in a draft body must canonically
  match a token the agent actually **read** from a source this session (the
  provenance register). An A-number, client name, case number, or date the
  agent *composed* but never *read* is the runtime signature of a fabricated or
  garbled identifier — and a garbled USCIS name or a wrong filing date is a
  venture-killer.

Why a register, not a shape blocklist: you cannot tell a *correct* A-number from
a *fabricated* one by shape alone — both match ``A\\d{9}``. The only durable
signal is provenance: did the agent read this exact identifier this session?
This mirrors the citation-filter lesson (a prompt-level "use only real numbers"
instruction is insufficient; a code-level provenance check is the durable
answer).

Posture — REPORT-ONLY by default, and it NEVER hard-blocks
----------------------------------------------------------

Two deliberate choices, both from the plan's design review:

1. **Canonicalized matching, not byte matching.** A competent assistant
   *composes*: it writes "June 8, 2026" where the source said "6/8/26", strips
   an A-number's punctuation, or addresses "Robert Smith" where the matter says
   "Robert J. Smith". Byte-matching would flag exactly these polished, correct
   drafts while a lazy copy-paste passes — inverting the value gradient. So
   dates fold to a canonical ``YYYY-MM-DD``, identifiers strip punctuation, and
   names match on last-name + first-initial before being called "unverified".

2. **The filter reports; the caller sets the posture.** This module returns the
   unverified identifiers; the *caller* decides the action by ``Mode`` and its
   own policy. In ``REPORT`` mode it emits an audit signal only. In ``FLAG``
   mode it routes the draft to human review with the unverified identifiers
   annotated. The module itself never hard-blocks — but the deployed overlay
   caller REFUSES on unverified identifiers (ss #2171, Captain directive
   2026-08-02): the draft-class surface includes structured INTERNAL_WRITEs
   (a ``create_event`` lands on the firm calendar with no reviewer between),
   so annotation alone cannot back the "refuses rather than guesses"
   commitment. The refusal decision, its carve-outs (ambient dates,
   empty-register draft carve, NAME exclusion), and its rollback lever live
   in the overlay caller, not here.

This module is pure (no I/O, no state beyond the register the caller passes).
The overlay wires it onto the live output path (Tier-3 of ``outbound_gate``) and
builds the register by tapping read-class tool results — that is the Wave-2
overlay change; this is the canonical primitive it vendors.

Usage::

    reg = ProvenanceRegister()
    reg.add_read_text(filevine_matter_blob)   # everything the agent read
    result = check(draft_body, reg, mode=Mode.REPORT)
    if result.has_unverified:
        emit_audit(result.audit_metadata())   # shapes only, never values
        if result.mode is Mode.FLAG:
            route_to_review(result.annotations())  # human sees the values
"""

from __future__ import annotations

import datetime
import enum
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Mode + kinds
# ---------------------------------------------------------------------------


class Mode(str, enum.Enum):
    """How the caller acts on unverified identifiers.

    ``REPORT`` — emit an audit signal, pass the draft. The default until the
    false-positive rate is measured on real traffic.
    ``FLAG`` — route the draft to human review with the unverified identifiers
    annotated. Still not a block.
    """

    REPORT = "report"
    FLAG = "flag"


class IdKind(str, enum.Enum):
    """Closed set of identifier shapes this filter recognizes.

    Money is deliberately excluded — a specific dollar amount is the content
    floor's domain (ADR 0031), which routes any money to draft regardless of
    provenance. This filter is about *identity* tokens.
    """

    A_NUMBER = "a_number"  # USCIS alien registration number
    RECEIPT_NUMBER = "receipt_number"  # USCIS receipt (e.g. EAC2190012345)
    SSN = "ssn"
    CASE_NUMBER = "case_number"  # docket / case number
    DATE = "date"
    NAME = "name"  # recipient name in a greeting slot
    PAIR = "pair"  # a (case number, date) asserted together on one line


# ---------------------------------------------------------------------------
# Extraction patterns
# ---------------------------------------------------------------------------

# A-number: "A123456789", "A 123 456 789", "A-12345678", "A#123-456-789".
# 8 or 9 digits, optional "#" and separators (the "A-number" / "A#" notation).
_A_NUMBER_RE = re.compile(r"\bA#?[-\s]?(?:\d[-\s]?){8,9}\b")
# USCIS receipt: three letters + 10 digits (EAC/WAC/LIN/SRC/MSC/IOE...).
_RECEIPT_RE = re.compile(r"\b[A-Z]{3}\d{10}\b")
# SSN.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Case / docket numbers: federal-style "1:24-cv-01234", or "No. 24-12345", or a
# practice-management MATTER number ("2026-PI-101", "PI-2026-0001").
#
# The matter-number alternation was added 2026-07-31. Before it, this pattern
# could not see a matter number at all: probed live, "2026-PI-107" -> no hit,
# "PI-2026-0001" -> no hit. Every IDENTIFIER_UNVERIFIED row on the pilot seat
# showed only date shapes, which read as "no identifier problems found" when the
# truth was "this filter is blind to the identifiers this firm uses." A gate that
# cannot see a class of value is not reporting on it, and silence from it meant
# nothing.
#
# Still REPORT-ONLY, deliberately. See the posture note in the overlay's
# plugins/hermes-smd-trust/outbound.py: enforcement flips only after the
# false-positive rate is measured on real traffic, and that discipline is not
# overridden here. What changes is that the signal now exists to measure.
_CASE_RE = re.compile(
    r"\b(?:\d{1,2}:\d{2}-[a-z]{2}-\d{3,6}"
    r"|No\.?\s?\d{2,4}-\d{2,6}"
    r"|\d{4}-[A-Z]{2}-\d{3,4}"
    r"|[A-Z]{2}-\d{4}-\d{4})\b",
    re.IGNORECASE,
)

# Dates — numeric, ISO, and month-name forms.
_DATE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(r"\b\d{1,2}-\d{1,2}-\d{2,4}\b"),
    # ISO date, and the date half of an ISO *datetime*. The trailing \b this
    # replaced could not match "2026-08-12T09:00:00Z": between the final "2" and
    # the "T" there is no word boundary, both being word chars. Smokeball events
    # carry ISO datetimes (create_event start_time/end_time), so a digest that
    # correctly read a hearing and wrote its date was flagged unverified — a
    # false positive at daily volume, and one that would have been measured as
    # the model's fabrication rate. The negative lookahead also declines
    # "2026-08-12-99", which the old \b form wrongly matched as a date.
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?![\d-])"),
    # Month-name forms accept an ordinal day suffix ("August 5th, 2026",
    # "5th August 2026"). Without it, the evasive rewrite a refusal induces —
    # the model rephrasing a blocked "August 5, 2026" as "August 5th, 2026" —
    # would be invisible to both blocking and reporting (#2171). The suffix is
    # accepted on BOTH the scan side and the seeding side (add_read_text uses
    # these same patterns), so a correctly-read ordinal date also verifies.
    re.compile(
        r"\b(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|"
        r"Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June"
        r"|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun"
        r"|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{4}\b",
        re.IGNORECASE,
    ),
)

# Greeting name slot only. We do NOT scan prose for names (far too noisy), and
# we do NOT check the sign-off: that is the *sender's* own name (the firm /
# attorney), authored by the firm, not a provenance-checkable recipient
# identifier — checking it only manufactures false positives. The threat model
# is a garbled *recipient* (a client name / addressee), which lives in the
# greeting.
_GREETING_RE = re.compile(
    r"(?:^|\n)\s*(?:Dear|Hi|Hello|Greetings)\s+([A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){0,2})\s*[,:]",
)

# Bound the (case x date) cross-product on a pathological line. A real digest
# row carries one matter and one or two dates; anything past this is noise, and
# an unbounded product on a paragraph-length "line" would be a cheap DoS on the
# gate itself.
_MAX_PAIRS_PER_LINE = 8

# Bound the pair register the same way the session registers are bounded
# elsewhere. A seeded record contributes a handful of pairs; a runaway seeder
# must not grow this without limit on a long-lived Machine.
_MAX_REGISTERED_PAIRS = 4096

# Bound the register-anchored known-number alternation (ss#2458). Registered
# numbers come from code-resolved records (``add_record``), so the cap is a
# scan-cost guard, not a precision knob; numbers past it simply do not scan —
# the narrow direction (an unscanned number feeds no pair check, and the
# punctuated shapes ``_CASE_RE`` sees are unaffected).
_MAX_KNOWN_NUMBER_TOKENS = 64

_DATE_STRPTIME_FORMATS: tuple[str, ...] = (
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%Y-%m-%d",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
)


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


#: A dollar figure: ``$`` then digits, with optional thousands separators and an
#: optional two-decimal tail. Deliberately WIDER than the Tier-1 marker it
#: exempts (``\$\s?\d``), on both axes:
#:
#: * it captures the WHOLE figure, where the marker only ever glimpses the first
#:   digit, and the exemption has to compare the whole thing;
#: * it allows ANY run of whitespace after the ``$``, where the marker allows at
#:   most one space. Real billing summaries column-align their figures
#:   (``$  4,820.00``), and a register that missed those would leave the
#:   exemption unreachable on exactly the documents it exists for. Caught by
#:   test_reading_a_billing_summary_registers_its_figures against a fixture
#:   copied from a real seeded matter.
#:
#: Wider is the safe direction here: a figure this misses is simply not
#: exempted, and stays blocked.
MONEY_RE = re.compile(r"\$\s*\d[\d,]*(?:\.\d{1,2})?")


def canon_money(raw: str) -> str:
    """Fold a dollar figure to a comparable canonical form.

    ``$41,515.00``, ``$ 41515``, and ``$41,515`` are the same amount and must
    compare equal — a draft that writes a figure without the cents it was read
    with has not fabricated anything. Returns ``""`` for anything unparseable,
    and an empty canonical never matches, so a parse failure withholds the
    exemption rather than granting it.
    """
    digits = raw.replace("$", "").replace(",", "").replace(" ", "").strip()
    if not digits:
        return ""
    try:
        value = float(digits)
    except ValueError:
        return ""
    # Normalize the cents tail so 41515, 41515.0 and 41515.00 collapse together,
    # while 41515.50 stays distinct from 41515.05.
    return f"{value:.2f}".rstrip("0").rstrip(".")


def extract_money(text: str) -> list[tuple[str, str]]:
    """Every dollar figure in ``text`` as ``(raw, canonical)``.

    Unparseable figures are dropped rather than registered: an amount nobody can
    canonicalize cannot be matched later either, so keeping it would only make
    the register look fuller than it is.
    """
    out: list[tuple[str, str]] = []
    for match in MONEY_RE.finditer(text or ""):
        canon = canon_money(match.group(0))
        if canon:
            out.append((match.group(0), canon))
    return out


def _canon_digits(raw: str, prefix: str = "") -> str:
    """Strip every non-alphanumeric char, upper-case, optional prefix."""
    core = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    return f"{prefix}{core}" if prefix else core


def _canon_a_number(raw: str) -> str:
    """``A 123 456 789`` / ``A-123456789`` -> ``A123456789``."""
    digits = re.sub(r"\D", "", raw)
    return f"A{digits}"


def _canon_date(raw: str) -> str | None:
    """Fold a recognized date to ``YYYY-MM-DD``; ``None`` if unparseable.

    Two-digit years resolve via ``%y`` (00-68 -> 20xx). US month/day order is
    assumed (the venture is US) — a documented limitation, not a guarantee.
    """
    s = re.sub(r"\s+", " ", raw.strip()).rstrip(".")
    s = s.replace(".", "")  # "Jun." -> "Jun"
    # Strip ordinal day suffixes ("5th" -> "5") BEFORE strptime: the extraction
    # patterns accept them (#2171), strptime does not — and stripping here is
    # what makes a read "August 5, 2026" verify a written "August 5th, 2026"
    # (both fold to 2026-08-05).
    s = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", s, flags=re.IGNORECASE)
    for fmt in _DATE_STRPTIME_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _canon_name(raw: str) -> str:
    """Canonical name key: ``last|first-initial``, lower-cased.

    "Robert J. Smith" -> "smith|r"; "Robert Smith" -> "smith|r". This makes a
    composed "Robert Smith" match a source "Robert J. Smith". It does NOT
    normalize nicknames ("Bob" -> "Robert") — that is out of scope for v1, and
    in REPORT mode a nickname mismatch is a signal, not a block.
    """
    tokens = [t for t in re.split(r"\s+", raw.strip()) if t and t not in {".", ","}]
    if not tokens:
        return ""
    first = tokens[0].strip(".,'-").lower()
    last = tokens[-1].strip(".,'-").lower()
    initial = first[:1] if first else ""
    return f"{last}|{initial}"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentifierHit:
    """One identifier found in a body. ``canonical`` is the provenance key.

    ``raw`` is retained for the human-facing review annotation (FLAG mode). It
    is never written to an audit row — :meth:`IdentifierResult.audit_metadata`
    emits redacted shapes only.
    """

    kind: IdKind
    raw: str
    canonical: str
    span: tuple[int, int]


def _extract(text: str, include_names: bool = True) -> list[IdentifierHit]:
    """Return every identifier-shaped token in ``text`` (verified-state unset).

    Used on the draft body (to check). The structured-shape kinds (dates,
    A-numbers, receipts, SSNs, case numbers) are also extracted from read
    tool-results to populate the register — the symmetry is the point: a
    structured identifier in the body whose canonical form was never extracted
    from any read is unverified.

    ``include_names`` is False when populating the register from read text:
    party/recipient names cannot be reliably scanned from free read-text (that
    noise is the whole reason body name-checking is slot-scoped). Names enter the
    register through structured metadata via :meth:`ProvenanceRegister.add_name`,
    not by scanning blobs.
    """
    hits: list[IdentifierHit] = []
    if not isinstance(text, str) or not text:
        return hits

    def _add(kind: IdKind, m: re.Match[str], canonical: str) -> None:
        if canonical:
            hits.append(IdentifierHit(kind, m.group(0), canonical, m.span()))

    for m in _A_NUMBER_RE.finditer(text):
        _add(IdKind.A_NUMBER, m, _canon_a_number(m.group(0)))
    for m in _RECEIPT_RE.finditer(text):
        _add(IdKind.RECEIPT_NUMBER, m, _canon_digits(m.group(0)))
    for m in _SSN_RE.finditer(text):
        _add(IdKind.SSN, m, _canon_digits(m.group(0)))
    for m in _CASE_RE.finditer(text):
        _add(IdKind.CASE_NUMBER, m, _canon_digits(m.group(0)))
    for pat in _DATE_RES:
        for m in pat.finditer(text):
            canon = _canon_date(m.group(0))
            if canon:
                _add(IdKind.DATE, m, canon)
    if include_names:
        for m in _GREETING_RE.finditer(text):
            name = m.group(1)
            # span of the captured name, not the whole greeting.
            start = m.start(1)
            hits.append(
                IdentifierHit(IdKind.NAME, name, _canon_name(name), (start, start + len(name)))
            )
    return hits


def pair_key(case_canonical: str, date_canonical: str) -> str:
    """The register key for one (case number, date) assertion."""
    return f"{case_canonical}|{date_canonical}"


def _known_number_re(tokens: Iterable[str]) -> re.Pattern[str] | None:
    """A bounded alternation matching the REGISTERED matter numbers literally
    (escaped, word-anchored, longest alternative first — alternation is
    first-match-wins, the matter_gate lesson of 2026-08-11). ``None`` when
    nothing is registered: an empty register scans nothing, and a gate that
    cannot see must not claim to have seen."""
    cleaned = sorted({t for t in tokens if t}, key=lambda t: (-len(t), t))
    cleaned = cleaned[:_MAX_KNOWN_NUMBER_TOKENS]
    if not cleaned:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in cleaned) + r")\b")


def _extract_pairs(
    text: str, known_number_re: re.Pattern[str] | None = None
) -> list[IdentifierHit]:
    """Return the (case number, date) co-occurrences asserted **per line**.

    ``known_number_re`` (ss#2458) merges per-line literal matches of the
    REGISTER'S OWN matter numbers with the ``_CASE_RE`` matches, so a line
    pairing a seeded bare-digit number with a date is judged against the
    seeded ``(number, date)`` associations exactly like a shaped number.
    ``_CASE_RE`` itself is untouched.

    Why this exists: atom-level provenance cannot see a *mispairing*. On
    2026-08-01 the Operator wrote "matter 2026-PI-105, deposition of plaintiff
    Alvarez, August 6, 2026" when the deposition event carried
    ``matterNumber=2026-PI-101``. Both "2026-PI-105" and "2026-08-06" had been
    legitimately read that session — one from the Okafor trial tasks, the other
    from the Alvarez event — so every atom verified and the line passed clean.
    What was never read is the two of them *together*.

    Line-scoped because a line is the unit of assertion in the artifacts this
    guards: one digest row, one ledger row, one escalation item. Two identifiers
    on the same line are being claimed about each other; two identifiers three
    paragraphs apart are not.
    """
    hits: list[IdentifierHit] = []
    if not isinstance(text, str) or not text:
        return hits

    offset = 0
    for line in text.splitlines(keepends=True):
        cases = [(m, _canon_digits(m.group(0))) for m in _CASE_RE.finditer(line)]
        if known_number_re is not None:
            seen_spans = {m.span() for m, _ in cases}
            for m in known_number_re.finditer(line):
                if m.span() not in seen_spans:
                    cases.append((m, _canon_digits(m.group(0))))
        if cases:
            dates: list[tuple[re.Match[str], str]] = []
            for pat in _DATE_RES:
                for m in pat.finditer(line):
                    canon = _canon_date(m.group(0))
                    if canon:
                        dates.append((m, canon))
            emitted = 0
            for cm, ccanon in cases:
                for dm, dcanon in dates:
                    if emitted >= _MAX_PAIRS_PER_LINE:
                        break
                    start = offset + min(cm.start(), dm.start())
                    end = offset + max(cm.end(), dm.end())
                    hits.append(
                        IdentifierHit(
                            IdKind.PAIR,
                            f"{cm.group(0)} ↔ {dm.group(0)}",
                            pair_key(ccanon, dcanon),
                            (start, end),
                        )
                    )
                    emitted += 1
        offset += len(line)
    return hits


# ---------------------------------------------------------------------------
# Provenance register
# ---------------------------------------------------------------------------


class ProvenanceRegister:
    """Canonical identifier tokens the agent actually read this session.

    The caller populates it from read-class tool results AND from durable
    session memory + matter metadata (seeding from memory, not only this-session
    reads, keeps the first draft of a session from being blanket-flagged — a
    design-review correction). This module only consumes the register.
    """

    # Bound the caption set so a pathological read blob cannot grow a session
    # register without limit. Adds past the cap are IGNORED (the narrow
    # direction: an unregistered caption stays blocked, never the reverse).
    _MAX_CAPTIONS = 512
    #: Same bound and same narrow direction for money figures. A matter record
    #: with more than 512 distinct dollar amounts exists; a draft that needs all
    #: of them does not, and an unregistered figure stays blocked.
    _MAX_MONEY = 512

    def __init__(self) -> None:
        self._canon: set[str] = set()
        self._names: set[str] = set()
        self._pairs: set[str] = set()
        self._captions: set[str] = set()
        self._money: set[str] = set()
        # Matter numbers seeded via add_record, in canonical digit form —
        # the register-anchored extraction pass (ss#2458). Populated ONLY by
        # add_record (never by bare add() or add_read_text), because only the
        # record seam knows a token IS a matter number rather than a token
        # that merely matched a shape.
        self._matter_numbers: set[str] = set()

    def add_read_text(self, text: str) -> None:
        """Register the structured-shape identifiers found in a blob the agent
        read (dates, A-numbers, receipts, SSNs, case numbers). Names are NOT
        scanned from read text — register them via :meth:`add_name` from
        structured matter/contact metadata.

        **Deliberately registers no pairs.** A tool result is a *collection* of
        records: pairing every case number in the blob with every date in the
        blob would register the cross-product and verify exactly the mispairings
        this is meant to catch. Pairs come from :meth:`add_record`, one record
        at a time, where the association is a fact rather than an inference.

        **Money is registered here too, into its own set** (ss-console#2258).
        It is NOT an ``IdKind`` and never reaches :meth:`verifies` — see
        :meth:`money` for why that boundary is the whole point.
        """
        for hit in _extract(text, include_names=False):
            self.add(hit.kind, hit.canonical)
        for _raw, canon in extract_money(text or ""):
            self.add_money(canon)

    def add_record(self, case_number: str | None, dates: Iterable[str]) -> None:
        """Register one record's identifiers **and the associations within it**.

        This is the structured seam: the caller holds a single record (a task, an
        event) whose matter binding was resolved in code, so "this date belongs
        to this matter" is known rather than guessed. Callers pass raw values;
        canonicalization happens here so a seeder cannot register a key shaped
        differently from the one :func:`check` will look up.
        """
        case_canon = _canon_digits(case_number) if case_number else ""
        if case_canon:
            self._canon.add(case_canon)
            if len(self._matter_numbers) < _MAX_KNOWN_NUMBER_TOKENS:
                self._matter_numbers.add(case_canon)
        for raw in dates:
            if not raw:
                continue
            # Route through _extract rather than _canon_date so the seeder and
            # check() canonicalize identically BY CONSTRUCTION. _canon_date alone
            # parses only date-shaped strings, and a record's date field is
            # routinely an ISO *datetime* ("2026-08-06T10:00:00Z") — seeding that
            # directly registered nothing, so every pair on that record silently
            # failed to verify. A key the checker will never look up is worse
            # than no key: it reads as a mispairing.
            for hit in _extract(str(raw), include_names=False):
                if hit.kind is not IdKind.DATE:
                    continue
                self._canon.add(hit.canonical)
                if case_canon and len(self._pairs) < _MAX_REGISTERED_PAIRS:
                    self._pairs.add(pair_key(case_canon, hit.canonical))

    def add_pair(self, case_canonical: str, date_canonical: str) -> None:
        """Register one already-canonical association directly."""
        if case_canonical and date_canonical and len(self._pairs) < _MAX_REGISTERED_PAIRS:
            self._pairs.add(pair_key(case_canonical, date_canonical))

    def matter_numbers(self) -> frozenset[str]:
        """The matter numbers seeded via :meth:`add_record`, canonical digit
        forms. Feeds the register-anchored extraction pass in :func:`check`
        (ss#2458): a firm whose matter numbers are bare digit runs ("201537",
        "4853") is invisible to ``_CASE_RE`` — no shape can see a bare number
        at acceptable precision (dates, amounts, zips and page counts all
        collide) — so the scan is anchored to MEMBERSHIP in this set instead,
        extending no pattern anywhere."""
        return frozenset(self._matter_numbers)

    @property
    def has_pairs(self) -> bool:
        """True once anything has seeded an association.

        :func:`check` consults this before reporting any pair: a register with no
        associations cannot judge one, and a gate that cannot see must not claim
        to have seen. Reporting pairs from an unseeded register would flag every
        line carrying a matter and a date — which is every line of a deadline
        digest, and marking everything is how a reader learns to ignore the mark.
        """
        return bool(self._pairs)

    def add(self, kind: IdKind, canonical: str) -> None:
        """Register one canonical identifier directly (e.g. from structured
        matter metadata where the field type is already known)."""
        if not canonical:
            return
        if kind is IdKind.NAME:
            self._names.add(canonical)
        else:
            self._canon.add(canonical)

    def add_name(self, name: str) -> None:
        """Register a known recipient/party name (canonicalized)."""
        canon = _canon_name(name)
        if canon:
            self._names.add(canon)

    def add_caption(self, canonical_caption: str) -> None:
        """Register a case-caption string the agent READ this session
        (already canonicalized by ``citation_filter.canonical_caption``).
        Feeds the tier-2 citation gate's provenance allowlist (ss-console
        #1758): repeating a caption you read is quoting the record, not
        fabricating authority."""
        if not canonical_caption or len(self._captions) >= self._MAX_CAPTIONS:
            return
        self._captions.add(canonical_caption)

    def captions(self) -> frozenset[str]:
        """The session's provenance-verified case captions (canonical forms)."""
        return frozenset(self._captions)

    def add_money(self, canonical_amount: str) -> None:
        """Register a dollar figure the agent READ this session, canonicalized
        by :func:`canon_money`.

        Feeds the Tier-1 ``specific-dollar-amount`` exemption (ss-console#2258),
        and NOTHING ELSE. See :meth:`money` for why that boundary matters.
        """
        if not canonical_amount or len(self._money) >= self._MAX_MONEY:
            return
        self._money.add(canonical_amount)

    def money(self) -> frozenset[str]:
        """The session's provenance-verified dollar figures (canonical forms).

        DELIBERATELY NOT AN ``IdKind``, and this is the load-bearing part. The
        kinds in :class:`IdKind` drive the A1 identifier gate, which REFUSES a
        hit it cannot verify. Adding money there would start blocking every
        dollar figure not read this session, on every draft, on every path — a
        large tightening nobody asked for and one that would land as a fleet of
        new refusals.

        This mirrors :meth:`captions` instead: registered and exposed purely so
        one Tier-1 marker can be exempted, invisible to :meth:`verifies`. The
        only behavior change is that a figure the agent READ stops being treated
        as fabricated.
        """
        return frozenset(self._money)

    def verifies(self, hit: IdentifierHit) -> bool:
        if hit.kind is IdKind.NAME:
            return hit.canonical in self._names
        if hit.kind is IdKind.PAIR:
            return hit.canonical in self._pairs
        return hit.canonical in self._canon

    def __bool__(self) -> bool:
        return bool(self._canon or self._names or self._pairs or self._captions)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentifierResult:
    """Outcome of :func:`check`. ``unverified`` is empty iff the body is clean."""

    mode: Mode
    unverified: tuple[IdentifierHit, ...] = field(default_factory=tuple)
    register_was_empty: bool = False

    @property
    def has_unverified(self) -> bool:
        return len(self.unverified) > 0

    def __len__(self) -> int:
        return len(self.unverified)

    def annotations(self) -> list[str]:
        """Human-facing review notes (FLAG mode). Includes the raw value so the
        reviewer can judge it — this is surfaced to the firm's reviewer, not an
        audit log."""
        notes: list[str] = []
        for h in self.unverified:
            if h.kind is IdKind.PAIR:
                notes.append(
                    f"unverified pair: {h.raw} — both values were read this session, "
                    "but never together on one record"
                )
            else:
                notes.append(
                    f"unverified {h.kind.value}: {h.raw!r} — not found in anything read this session"
                )
        return notes

    def audit_metadata(self) -> dict:
        """Audit-row metadata: KINDS and REDACTED shapes only, never the raw
        identifier value (an A-number / SSN must not land in a log row)."""
        by_kind: dict[str, int] = {}
        for h in self.unverified:
            by_kind[h.kind.value] = by_kind.get(h.kind.value, 0) + 1
        return {
            "gate_tier": "tier3_identifier",
            "mode": self.mode.value,
            "register_was_empty": self.register_was_empty,
            "unverified_counts": by_kind,
            "shapes": sorted({_redact(h) for h in self.unverified}),
        }


def _redact(hit: IdentifierHit) -> str:
    """A loggable shape that reveals the kind and length but not the value."""
    if hit.kind is IdKind.NAME:
        return "name:<redacted>"
    masked = re.sub(r"[0-9A-Za-z]", "#", hit.raw)
    return f"{hit.kind.value}:{masked.strip()}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check(body: str, register: ProvenanceRegister, mode: Mode = Mode.REPORT) -> IdentifierResult:
    """Return the identifiers in ``body`` that the register cannot verify.

    Never raises on normal input and never blocks — the ``mode`` tells the
    caller how to act (REPORT: signal + pass; FLAG: route to review annotated).
    An empty register is recorded (``register_was_empty``) so the caller can
    treat "nothing was read" distinctly from "everything verified" — but even
    then this returns hits to *report/flag*, not to block, per the report-first
    posture.
    """
    hits = _extract(body)
    # Register-anchored matter numbers (ss#2458): scan the body for the numbers
    # the register itself holds (seeded one record at a time by add_record) and
    # emit CASE_NUMBER hits for exact matches. Verified by construction — they
    # are in the register — so they can never flag; their purpose is to feed
    # the pair check below, so a line pairing a seeded BARE-DIGIT number with a
    # date is judged against the seeded associations. No shape pattern could do
    # this: bare digit runs collide with dates, amounts, zips and page counts
    # at any useful recall, and _MAX_CITED-style caps mean the false hits would
    # crowd out real ones. DOCUMENTED RESIDUAL BLINDNESS: a *fabricated*
    # bare-digit number that was never read stays invisible to this filter —
    # the gate's claim shrinks to what it measures (see the enable-gate
    # checklist's per-firm evidence slot).
    known_re = _known_number_re(register.matter_numbers())
    if known_re is not None and isinstance(body, str) and body:
        for m in known_re.finditer(body):
            canonical = _canon_digits(m.group(0))
            if canonical:
                hits.append(IdentifierHit(IdKind.CASE_NUMBER, m.group(0), canonical, m.span()))
    # Pairs only when the register carries associations to judge them against.
    # An unseeded register cannot distinguish a mispairing from a correct one, so
    # it reports neither — see ProvenanceRegister.has_pairs.
    if register.has_pairs:
        hits.extend(_extract_pairs(body, known_number_re=known_re))
    unverified = tuple(h for h in hits if not register.verifies(h))
    return IdentifierResult(
        mode=mode,
        unverified=unverified,
        register_was_empty=not bool(register),
    )


def unverified_identifiers(body: str, register: ProvenanceRegister) -> list[IdentifierHit]:
    """Convenience: just the unverified hits (mode-agnostic)."""
    return list(check(body, register).unverified)


# ---------------------------------------------------------------------------
# Substrate-runner self-check (boot smoke; full coverage in tests/)
# ---------------------------------------------------------------------------


def _self_check() -> tuple[bool, str]:
    reg = ProvenanceRegister()
    reg.add_read_text("A# 123-456-789, hearing on 6/8/26.")
    reg.add_name("Robert J. Smith")  # names come from structured metadata

    # Composed/normalized but correct -> verified (no false flag).
    clean = check(
        "Dear Robert Smith,\n\nYour hearing is set for June 8, 2026 (A123456789).",
        reg,
    )
    if clean.has_unverified:
        return (False, f"FAIL: composed-but-correct draft falsely flagged: {clean.annotations()}")

    # Fabricated A-number not in any read -> flagged.
    bad = check("Your A-number is A999999999.", reg)
    if not bad.has_unverified or bad.unverified[0].kind is not IdKind.A_NUMBER:
        return (False, "FAIL: fabricated A-number not flagged")

    return (True, "PASS: identifier filter verifies composed identifiers and flags fabricated ones")


def run() -> tuple[bool, str]:
    """Substrate-runner shape — boot smoke check. Full coverage in
    ``tests/test_identifier_filter.py``."""
    try:
        return _self_check()
    except Exception as e:  # noqa: BLE001
        return (False, f"FAIL: identifier filter self-check raised {type(e).__name__}: {e}")


def refusal_message(result: IdentifierResult) -> str:
    """Human-facing review note (FLAG mode). Not a refusal — an annotation.

    Named ``*_message`` for parity with ``citation_filter.refusal_message``, but
    this gate flags to review, it does not refuse.
    """
    if not result.has_unverified:
        return ""
    notes = "; ".join(result.annotations())
    return (
        "REVIEW: this draft contains identifiers not traceable to anything read "
        f"this session ({notes}). Verify each against the source before it goes "
        "out. (identifier-integrity gate; fabrication discipline.)"
    )


__all__ = [
    "IdKind",
    "IdentifierHit",
    "IdentifierResult",
    "Mode",
    "ProvenanceRegister",
    "check",
    "refusal_message",
    "run",
    "unverified_identifiers",
]
