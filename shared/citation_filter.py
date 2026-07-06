"""Citation detector — PI-vertical safety invariant #6.

VENDORED from ``ss-console/operator/safety-substrate/citation_filter.py``,
which is the source-of-truth primitive. This is a pure-python copy carried in
the overlay so the outbound gate (ADR 0028) can run the law-vertical Tier-2
citation scan inside the trust plugin without a cross-repo runtime dependency.
Keep this aligned with ss-console; changes land there first.

Detects patterns in agent output that look like legal citations (case names
with reporter cites, statute references, court rule references). The
substrate's policy is REFUSE on any positive detection in any law-vertical
skill's output, regardless of how the agent reached it.

Why this exists at the substrate level, not as prompt guidance: Mata v.
Avianca (S.D.N.Y. 2023) plus 200+ documented sanctions through mid-2025
demonstrate that prompt-level "don't cite cases" instructions are not
sufficient. Agents drift, especially under compaction. A code-level filter
that scans output regardless of prompt is the only durable answer.

The filter is conservative: false positives are acceptable (refuses ambiguous
text, agent re-drafts without the suspect string); false negatives are not
(a real citation slipping through is the venture-killer).

Usage:
    from safety_substrate.citation_filter import contains_citation, scan
    if contains_citation(agent_output):
        raise CitationRefused(scan(agent_output))
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

# ---------- Case-name patterns ----------
# "Smith v. Jones", "Smith v Jones" (no period), "Smith vs. Jones", "In re Smith",
# "United States v. Smith". Tolerates middle initials and "Inc."/"LLC" suffixes.
_PARTY = r"[A-Z][A-Za-z'.\-]{1,40}(?:\s+[A-Z][A-Za-z'.\-]{1,40}){0,4}"
# IGNORECASE so all-caps ("SMITH V. JONES") and lowercase ("smith v jones")
# party names are caught — fabricated cites without a reporter cite otherwise
# slipped through the case-name gap (issue #1128). False positives are
# acceptable here; a false negative is the venture-killer.
CASE_NAME_RE = re.compile(
    rf"\b(?:In re\s+{_PARTY}|{_PARTY}\s+v(?:s)?\.?\s+{_PARTY})\b",
    re.IGNORECASE,
)

# ---------- Reporter cite patterns (volume + reporter + page) ----------
# Federal: U.S. Reports, Supreme Court Reporter, Federal Reporter (1d, 2d, 3d, 4th),
# Federal Supplement (1st, 2d, 3d), Federal Appendix, Federal Rules Decisions.
# State: Common reporter abbreviations. List is not exhaustive but covers the
# fabrications models actually produce.
_REPORTER_ABBREVS = [
    # Federal
    r"U\.\s?S\.",  # U.S.
    r"S\.\s?Ct\.",
    r"L\.\s?Ed\.(?:\s?2d)?",
    r"F\.\s?(?:Supp\.?\s?(?:2d|3d)?|App'?x|R\.D\.|[1-4]?(?:st|nd|rd|th|d))?",  # F., F.2d, F.3d, F. Supp., F. Supp. 2d, F. App'x, F.R.D.
    # State (common). California's modern series are ordinal (Cal.4th,
    # Cal.App.5th, Cal.Rptr.3d) — "[2-5]d" alone missed them (found via
    # #1758's allowlist tests: "123 Cal. App. 5th 456" scanned clean).
    r"Cal\.\s?(?:App\.?\s?)?(?:[2-5]\s?(?:d|th)\.?)?",
    r"Cal\.\s?Rptr\.(?:\s?[2-3]d)?",
    r"N\.\s?Y\.(?:\s?[2-3]d)?",
    r"Ill\.(?:\s?(?:App\.?\s?)?[2-3]d)?",
    r"Tex\.(?:\s?(?:App\.?\s?)?)?",
    r"Fla\.(?:\s?(?:App\.?\s?)?)?",
    r"Pa\.(?:\s?(?:Super\.?\s?)?)?",
    r"Ariz\.(?:\s?(?:App\.?\s?)?)?",
    # Regional reporters
    r"A\.\s?(?:[2-3]d)?",  # A., A.2d, A.3d
    r"N\.\s?E\.(?:\s?[2-3]d)?",
    r"N\.\s?W\.(?:\s?[2-3]d)?",
    r"S\.\s?E\.(?:\s?[2-3]d)?",
    r"S\.\s?W\.(?:\s?[2-3]d)?",
    r"P\.(?:\s?[2-3]d)?",
    r"So\.(?:\s?[2-3]d)?",
]
_REPORTER_GROUP = "(?:" + "|".join(_REPORTER_ABBREVS) + ")"
REPORTER_CITE_RE = re.compile(rf"\b\d{{1,4}}\s+{_REPORTER_GROUP}\s+\d{{1,5}}\b")

# ---------- Statute reference patterns ----------
# 42 U.S.C. § 1983, 18 U.S.C. §§ 1961-1968, A.R.S. § 12-501, Cal. Civ. Code § 1638, etc.
# § is optional (§{0,2}) so "42 U.S.C. 1983" — common in casual model output
# with no section symbol — is still caught, matching STATE_STATUTE_RE's
# already-optional handling (issue #1128).
STATUTE_RE = re.compile(
    r"\b\d{1,3}\s+(?:U\.\s?S\.\s?C\.|C\.\s?F\.\s?R\.)\s?§{0,2}\s?\d+",
    re.IGNORECASE,
)
STATE_STATUTE_RE = re.compile(
    r"\b(?:A\.\s?R\.\s?S\.|Cal\.\s?(?:Civ|Penal|Veh|Gov't|Bus\.?\s?&\s?Prof\.?)?\.?\s?(?:Code)?|N\.\s?Y\.\s?(?:C\.\s?P\.\s?L\.\s?R\.|Penal\s?Law|Gen\.?\s?Bus\.?\s?Law)|Tex\.\s?(?:Civ\.?\s?Prac\.?\s?&\s?Rem\.?|Penal|Bus\.?\s?&\s?Com\.?)?\s?Code|Fla\.\s?Stat\.|Ill\.\s?Comp\.?\s?Stat\.|N\.\s?J\.\s?Stat\.\s?Ann\.|Ohio\s?Rev\.?\s?Code)\s?§{0,2}\s?\d+",
    re.IGNORECASE,
)

# ---------- Court rule patterns ----------
RULE_RE = re.compile(
    r"\b(?:Fed\.?\s?R\.?\s?(?:Civ\.?|Crim\.?|App\.?|Evid\.?|Bankr\.?)\s?P\.?|FRCP|FRCrP|FRAP|FRE)\s?\d+(?:\([a-z0-9]+\))*",
    re.IGNORECASE,
)
LOCAL_RULE_RE = re.compile(
    r"\bL\.?\s?R\.?\s?Civ\.?\s?P\.?\s?\d+",
    re.IGNORECASE,
)

# ---------- Bluebook signals (alone, weak; in combination, strong) ----------
BLUEBOOK_SIGNALS_RE = re.compile(
    r"\b(?:id\.|supra(?:\s+note\s+\d+)?|infra|cf\.|accord|see also|Restatement\s+\(\w+\)\s+of)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Hit:
    pattern: str
    match: str
    span: tuple[int, int]


PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("case-name", CASE_NAME_RE),
    ("reporter-cite", REPORTER_CITE_RE),
    ("federal-statute", STATUTE_RE),
    ("state-statute", STATE_STATUTE_RE),
    ("federal-rule", RULE_RE),
    ("local-rule", LOCAL_RULE_RE),
    ("bluebook-signal", BLUEBOOK_SIGNALS_RE),
]


def _normalize_unicode(text: str) -> str:
    """Strip invisible characters and fold Unicode confusables to ASCII.

    Closes the zero-width / confusable evasion of the citation scan: a cite like
    "Roe v.<ZWSP>Wade, 410 U.<ZWSP>S.<ZWSP>113" (zero-width spaces) or a
    one-dot-leader "U<U+2024>S<U+2024>" renders identically to a human but
    evades the ASCII-only regex. We remove zero-width/format chars and NFKC-fold
    compatibility confusables (one-dot leader U+2024 -> '.', fullwidth forms) to
    their ASCII equivalents before the whitespace-collapse pass runs.
    """
    # Remove invisible / zero-width format characters (category Cf: ZWSP, ZWNJ,
    # ZWJ, word joiner, BOM, soft hyphen, etc.).
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    # NFKC folds compatibility confusables (one-dot leader, fullwidth) to ASCII.
    return unicodedata.normalize("NFKC", text)


def _normalize_encoding_bypass(text: str) -> str:
    """Collapse adversarial whitespace inserted to defeat the citation regex.

    Examples this catches:
      "Roe v . Wade , 410 U . S . 113" -> "Roe v. Wade, 410 U.S. 113"
      "Roe v.<ZWSP>Wade, 410 U.<ZWSP>S.<ZWSP>113" -> "Roe v.Wade, 410 U.S. 113"
      "S m i t h" stays "S m i t h" (each char is a single letter, not a token)
    The heuristic: normalize away invisible/confusable characters, then collapse
    whitespace that sits between two single-letter tokens (typical abbreviation
    evasion) or between a token and punctuation. Real prose with multi-letter
    words is preserved.
    """
    # Strip zero-width / confusable evasion before the ASCII whitespace pass.
    text = _normalize_unicode(text)
    # Collapse whitespace adjacent to punctuation: "v . Wade" -> "v. Wade"
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    # Collapse "U . S . " -> "U.S. " — single letters glued by dots and spaces.
    text = re.sub(r"\b([A-Z])\s*\.\s*([A-Z])\s*\.\s*", r"\1.\2. ", text)
    # Collapse "v . " -> "v. " for case-name separators (and similar).
    text = re.sub(r"\b(v|vs)\s*\.\s*", r"\1. ", text, flags=re.IGNORECASE)
    return text


def canonical_caption(text: str) -> str:
    """Canonical form of a case caption for allowlist comparison.

    Applies the same anti-evasion normalization the scanner uses, then folds
    case, collapses whitespace, and normalizes the party separator so
    "ALVAREZ V DRAPER", "Alvarez vs. Draper", and "Alvarez v. Draper" all
    compare equal. Used by :func:`scan`'s ``allowed_case_names`` and by
    callers building a provenance register of captions actually read.
    """
    s = _normalize_encoding_bypass(text or "")
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return re.sub(r"\bv(?:s)?\.?\s", "v. ", s)


def _canonical_allowlist(
    allowed_case_names: Iterable[str] | None,
) -> tuple[re.Pattern[str], ...]:
    """Compile boundary-bounded containment patterns for the allowlist.

    The case-name regex greedily swallows adjacent prose into its parties
    ("Discovery capture on Alvarez v. Draper is our matter" is ONE hit), so
    the exemption tests containment at word boundaries rather than equality:
    the registered caption must appear whole inside the canonical hit, with
    no name-character touching either edge ("Alvarez v. Drapers" never
    matches a registered "Alvarez v. Draper"). Prose spillover beyond the
    caption's own tokens is tolerated; a caption used as fabricated AUTHORITY
    still dies on the reporter-cite/statute/rule patterns, which are never
    allowlisted.

    Fail-closed: any error yields an EMPTY tuple (everything stays blocked),
    never a broadened one.
    """
    if not allowed_case_names:
        return ()
    try:
        # Boundary classes guard NAME CONTINUATION only (word chars,
        # apostrophe, hyphen). Sentence punctuation is deliberately NOT
        # excluded: the case-name regex swallows a trailing period into the
        # party ("...Alvarez v. Draper. No signed..."), and a caption ending
        # a sentence is the commonest legitimate form.
        return tuple(
            re.compile(rf"(?<![\w'\-]){re.escape(canonical_caption(s))}(?![\w'\-])")
            for s in allowed_case_names
            if s
        )
    except Exception:  # noqa: BLE001 — allowlist failure must narrow, not widen
        return ()


def scan(text: str, allowed_case_names: Iterable[str] | None = None) -> list[Hit]:
    """Return every citation-shaped hit in `text`. Empty list = clean.

    Scans both the raw text and a whitespace-normalized version to catch
    adversarial encoding (extra spaces inserted to bypass the regex).

    ``allowed_case_names`` is a provenance allowlist of case CAPTIONS the
    caller can attest the agent actually read this session (e.g. the matter's
    own caption harvested from read-tool results — issue #1758). A
    ``case-name`` hit whose matched text canonicalizes to an allowlisted
    caption is dropped: repeating a caption you read is quoting the record,
    not fabricating authority. The exemption is deliberately narrow —
    reporter cites, statutes, and rules are NEVER allowlisted, so an
    allowlisted caption followed by a reporter cite ("Alvarez v. Draper, 123
    Cal.App.5th 456") still blocks on the reporter-cite pattern.
    """
    allow = _canonical_allowlist(allowed_case_names)
    seen_matches: set[tuple[str, str]] = set()
    hits: list[Hit] = []
    for source_text in (text, _normalize_encoding_bypass(text)):
        for label, pat in PATTERNS:
            for m in pat.finditer(source_text):
                if label == "case-name" and allow:
                    canon_hit = canonical_caption(m.group(0))
                    if any(p.search(canon_hit) for p in allow):
                        continue
                key = (label, m.group(0))
                if key in seen_matches:
                    continue
                seen_matches.add(key)
                hits.append(Hit(pattern=label, match=m.group(0), span=m.span()))
    return hits


def contains_citation(text: str, allowed_case_names: Iterable[str] | None = None) -> bool:
    """Fast yes/no check. True if any strong pattern matches.

    `bluebook-signal` alone is NOT enough (too many false positives — "id." is
    a common Spanish abbreviation, "cf." appears in academic writing). Only
    returns True if a stronger pattern fires, OR a bluebook signal co-occurs
    with another bluebook signal in close proximity (Bluebook prose typically
    has multiple signals per paragraph).

    ``allowed_case_names`` — see :func:`scan`.
    """
    strong_labels = {
        "case-name",
        "reporter-cite",
        "federal-statute",
        "state-statute",
        "federal-rule",
        "local-rule",
    }
    hits = scan(text, allowed_case_names=allowed_case_names)
    if any(h.pattern in strong_labels for h in hits):
        return True
    bluebook_hits = [h for h in hits if h.pattern == "bluebook-signal"]
    if len(bluebook_hits) >= 2:
        spans = sorted(h.span for h in bluebook_hits)
        for a, b in zip(spans, spans[1:], strict=False):
            if b[0] - a[1] < 200:
                return True
    return False


def refusal_message(hits: Iterable[Hit]) -> str:
    """Generate a user-facing refusal explaining what was detected."""
    if not isinstance(hits, list):
        hits = list(hits)
    if not hits:
        return ""
    by_pat: dict[str, list[str]] = {}
    for h in hits:
        by_pat.setdefault(h.pattern, []).append(h.match)
    pieces = [
        f"{pat}: {', '.join(repr(m) for m in matches[:3])}" for pat, matches in by_pat.items()
    ]
    return (
        "REFUSED: output contains content matching legal-citation patterns. "
        "Per safety invariant #6 (PI-vertical citation refusal), the agent does "
        "not produce, repeat, or reformulate legal citations. Detected: "
        + "; ".join(pieces)
        + ". Re-draft without citation content; defer citation work to human research."
    )
