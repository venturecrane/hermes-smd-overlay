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

2. **Report-only first; flag-to-review, never block.** This module returns the
   unverified identifiers; the *caller* decides the action by ``Mode``. In
   ``REPORT`` mode (the default until the false-positive rate is measured on
   real traffic) it emits an audit signal and passes the draft. In ``FLAG`` mode
   it routes the draft to human review with the unverified identifiers
   annotated. It is never a hard block — a mismatched identifier is precisely
   what a human reviewer should *see*, not something to hide behind a refusal.
   Under a draft-for-review posture the draft already reaches a human; the
   gate's job there is to annotate, not to stop.

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
# Case / docket numbers: federal-style "1:24-cv-01234", or "No. 24-12345".
_CASE_RE = re.compile(
    r"\b(?:\d{1,2}:\d{2}-[a-z]{2}-\d{3,6}|No\.?\s?\d{2,4}-\d{2,6})\b",
    re.IGNORECASE,
)

# Dates — numeric, ISO, and month-name forms.
_DATE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(r"\b\d{1,2}-\d{1,2}-\d{2,4}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(
        r"\b(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|"
        r"Nov|Dec)\.?\s+\d{1,2},?\s+\d{4}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|"
        r"Sept|Oct|Nov|Dec)\.?\s+\d{4}\b",
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

    def __init__(self) -> None:
        self._canon: set[str] = set()
        self._names: set[str] = set()
        self._captions: set[str] = set()

    def add_read_text(self, text: str) -> None:
        """Register the structured-shape identifiers found in a blob the agent
        read (dates, A-numbers, receipts, SSNs, case numbers). Names are NOT
        scanned from read text — register them via :meth:`add_name` from
        structured matter/contact metadata."""
        for hit in _extract(text, include_names=False):
            self.add(hit.kind, hit.canonical)

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

    def verifies(self, hit: IdentifierHit) -> bool:
        if hit.kind is IdKind.NAME:
            return hit.canonical in self._names
        return hit.canonical in self._canon

    def __bool__(self) -> bool:
        return bool(self._canon or self._names or self._captions)


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
        return [
            f"unverified {h.kind.value}: {h.raw!r} — not found in anything read this session"
            for h in self.unverified
        ]

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
