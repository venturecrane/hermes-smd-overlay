"""Content-sensitivity floor — money / contract / scope / legal → draft.

A second, content-derived floor on top of the ADR 0025 trust ceiling. Where the
ceiling decides exposure by *action class* (and a vertical pack can pin a class
to draft), this module decides by the *content of a specific outbound message*:
even when ``external_send`` is configured ``autonomous``, a message that touches
**money, contracts, scope, or legal commitments** is forced down to
``draft_for_review`` so a human reviews it before it leaves.

Provenance
----------
Customer-zero onboarding interview, 2026-05-31 (Captain decision), recorded in
``ai-employee/customers/smd/onboarding-interview-2026-05-31.md`` and
``docs/adr/0031-content-sensitivity-send-floor.md``:

    "Crane send from AgentMail | Autonomous — *except* the content floor below.
     even under autonomous send, anything touching money, contracts, scope, or
     legal commitments drops to draft-for-review."

This is NOT the ADR 0028 outbound *fabrication* gate (banned marker strings /
fabricated citations — ``shared.outbound_gate``). That gate asks "did the agent
invent something it must not say." This floor asks "is this the *kind* of thing
a human must sign off on before it autonomously leaves." Different axis, both
fail-toward-safe.

Posture: fail toward draft
--------------------------
Draft is recoverable; an autonomous send of a contract or a wire instruction is
not. So this classifier is deliberately *broad* — a false positive costs a human
glance at a draft; a false negative could send a commitment Scott never made.
The trust layer treats an indeterminate / unreadable body on an autonomous send
as sensitive (route to draft), never as clear.

Pure module: ``classify(text) -> ContentFloorResult``. No I/O, no state. The
trust plugin's ``enforce.evaluate_tool_call`` calls it after the ceiling has
resolved an ``EXTERNAL_SEND`` to *send*, and downgrades to draft on a hit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache


class ContentFloorCategory(str):
    """String-enum-ish category labels (kept as plain strings for audit JSON)."""


# Category → list of case-insensitive patterns. Each pattern is matched with
# word boundaries where it is a word/phrase, or as an explicit regex for the
# structured cases (currency amounts). Curated to the four Captain-named
# classes; broad on purpose (see "fail toward draft" above).
#
# Adding a category or pattern is a one-line edit here + a test row in
# tests/test_content_floor.py. There is no external JSON to keep in sync — this
# floor is overlay-owned (unlike the cross-repo fabrication-marker registry).
_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    # MONEY — payments, amounts, billing, banking.
    "money": (
        r"\$\s?\d",  # $5, $ 1,200
        r"\b\d+(?:,\d{3})*(?:\.\d{2})?\s?(?:usd|dollars?)\b",
        r"\binvoice\b",
        r"\bpayment\b",
        r"\bpay\b",
        r"\bdeposit\b",
        r"\brefund\b",
        r"\bwire\b",
        r"\bwiring\b",
        r"\bach\b",
        r"\bbank\b",
        r"\brouting number\b",
        r"\baccount number\b",
        r"\bcredit card\b",
        r"\bcharge\b",
        r"\bbilling\b",
        r"\bprice\b",
        r"\bpricing\b",
        r"\bquote\b",
        r"\bfee\b",
        r"\bretainer\b",
        r"\bcost\b",
    ),
    # CONTRACT — agreements, signatures, binding terms.
    "contract": (
        r"\bcontract\b",
        r"\bagreement\b",
        r"\bsign(?:ed|ature|ing)?\b",
        r"\bcountersign\b",
        r"\bexecute(?:d)? (?:the )?(?:contract|agreement|sow)\b",
        r"\bterms (?:and|&) conditions\b",
        r"\bnda\b",
        r"\bnon-?disclosure\b",
        r"\bmsa\b",
        r"\bproposal\b",
        r"\bengagement letter\b",
        r"\bpurchase order\b",
        r"\bamend(?:ment)?\b",
    ),
    # SCOPE — deliverables, timelines, commitments about the work.
    "scope": (
        r"\bscope\b",
        r"\bstatement of work\b",
        r"\bsow\b",
        r"\bdeliverable[s]?\b",
        r"\bmilestone[s]?\b",
        r"\bdeadline[s]?\b",
        r"\btimeline\b",
        r"\bwe(?:'| wi)ll deliver\b",
        r"\bwe (?:can )?commit to\b",
        r"\bguarantee[ds]?\b",
        r"\bwarrant(?:y|ies)?\b",
    ),
    # LEGAL — commitments with legal weight, liability, counsel.
    "legal": (
        r"\blegal\b",
        r"\bliabilit(?:y|ies)\b",
        r"\bindemnif(?:y|ication|ies)\b",
        r"\battorney\b",
        r"\bcounsel\b",
        r"\blawsuit\b",
        r"\blitigation\b",
        r"\bsettlement\b",
        r"\bbinding\b",
        r"\bwaiver\b",
        r"\bcompliance obligation\b",
        r"\bcease and desist\b",
    ),
}


@dataclass(frozen=True)
class ContentFloorResult:
    """Outcome of a content-sensitivity scan.

    Attributes:
        sensitive: True iff at least one category matched (route to draft).
        categories: Sorted tuple of category labels that hit (audit metadata).
        hits: Sorted tuple of the literal matched substrings, lower-cased and
            de-duplicated. NEVER the full body — only the trigger fragments, so
            an audit row can explain the downgrade without persisting content.
    """

    sensitive: bool
    categories: tuple[str, ...] = field(default_factory=tuple)
    hits: tuple[str, ...] = field(default_factory=tuple)


@lru_cache(maxsize=1)
def _compiled() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Compile (category, pattern) pairs once. Immutable for process life."""
    out: list[tuple[str, re.Pattern[str]]] = []
    for category, patterns in _CATEGORY_PATTERNS.items():
        for pat in patterns:
            out.append((category, re.compile(pat, re.IGNORECASE)))
    return tuple(out)


def classify(text: str | None) -> ContentFloorResult:
    """Classify outbound text against the content-sensitivity floor.

    Args:
        text: The combined outbound content to scan (typically subject + body).
            ``None`` / empty / non-string is treated as INDETERMINATE and
            returns ``sensitive=True`` — the caller cannot certify an
            uninspectable autonomous send is non-sensitive, so it fails toward
            draft (see module docstring).

    Returns:
        A :class:`ContentFloorResult`. ``sensitive=True`` means the trust layer
        must downgrade an otherwise-autonomous ``EXTERNAL_SEND`` to draft.
    """
    if not isinstance(text, str) or not text.strip():
        return ContentFloorResult(
            sensitive=True,
            categories=("indeterminate",),
            hits=(),
        )

    categories: set[str] = set()
    hits: set[str] = set()
    for category, pattern in _compiled():
        for m in pattern.finditer(text):
            matched = m.group(0).strip().lower()
            if matched:
                categories.add(category)
                hits.add(matched)

    if not categories:
        return ContentFloorResult(sensitive=False)
    return ContentFloorResult(
        sensitive=True,
        categories=tuple(sorted(categories)),
        hits=tuple(sorted(hits)),
    )


__all__ = ["ContentFloorCategory", "ContentFloorResult", "classify"]
