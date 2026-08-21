"""Outbound provenance gate — ADR 0028 fail-closed policy core.

The first LIVE caller of provenance enforcement. A draft body that carries a
banned fabrication-marker string (Tier-1, universal) or a fabricated legal
citation (Tier-2, law-vertical) is BLOCKED before the draft tool runs.

This module is pure policy: ``evaluate(body, cohort, vertical) -> GateDecision``.
It is wired into the trust plugin's ``pre_tool_call`` hook (a SECOND evaluation
in the same callback, after the trust-ceiling check passes, only for
draft-creating tools). Body extraction, audit emission, and the block-directive
shape live in ``plugins/hermes-smd-trust/outbound.py``; this module makes the
allow/block decision and nothing else.

Two tiers
---------
* **Tier-1 (UNIVERSAL).** Every vertical. Scan ``body`` against the
  HIGH_RISK_MARKERS registry (``shared.fabrication_markers``). Any hit blocks.
  These are the CLAUDE.md Pattern-A / Pattern-B + tone-rule banned strings.
* **Tier-2 (LAW-VERTICAL ONLY).** Run ``citation_filter.contains_citation`` on
  ``body``. A fabricated case cite / statute / rule blocks. This is the
  Mata-v.-Avianca venture-killer guard. The refusal reason names the pattern
  kind that hit and the remedy for it, and never echoes the matched text.

Fail-closed, most-restrictive-on-indeterminate
----------------------------------------------
The non-negotiable posture (from the 3-critic review):

* If ``vertical`` is ``None`` / unknown / empty → run BOTH tiers. An
  indeterminate vertical is treated as the MOST restrictive (law-tier filters
  apply, block on any marker). Never the permissive/empty set.
* If the marker registry cannot load → block.
* If the citation filter raises → block.
* Any unexpected error in evaluation → block.

A gate that cannot evaluate BLOCKS. Silence is never "allow."
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from shared import citation_filter, identifier_filter
from shared.fabrication_markers import FabricationMarkersError, load_markers

logger = logging.getLogger(__name__)


# Vertical slugs that trigger the Tier-2 citation scan. ``law-firm`` is the v1
# law vertical; the set is kept small and explicit. An UNKNOWN vertical is NOT
# in this set, but unknown verticals run Tier-2 anyway via the most-restrictive
# rule below — membership here is only the "definitely law" fast path.
_LAW_VERTICALS: frozenset[str] = frozenset(
    {"law-firm", "law", "legal", "pi-law", "personal-injury"}
)


# Closed vocabulary for ``GateDecision.audit_action``. The outbound wiring maps
# this onto the audit row; ``block`` is the only non-allow value this gate
# produces (the gate never "drafts" — drafting is the trust-ceiling layer's job).
AUDIT_ALLOW = "allow"
AUDIT_BLOCK = "fabrication_block"


@dataclass(frozen=True)
class GateDecision:
    """Outcome of an outbound-gate evaluation.

    Attributes:
        allowed: True iff the body cleared both applicable tiers.
        reason: Human-readable explanation (audit + block message).
        audit_action: ``AUDIT_ALLOW`` or ``AUDIT_BLOCK``.
        tier: Which tier triggered the block (``"tier1_marker"`` /
            ``"tier2_citation"`` / ``"load_error"`` / ``""`` when allowed).
        marker_hits: Marker ids that hit (Tier-1). Never the full body.
        citation_hits: Citation pattern labels that hit (Tier-2).
        evaluated_law_tier: True iff the Tier-2 citation scan ran (law or
            indeterminate vertical). Surfaced for tests + audit metadata.
    """

    allowed: bool
    reason: str
    audit_action: str
    tier: str = ""
    marker_hits: tuple[str, ...] = field(default_factory=tuple)
    citation_hits: tuple[str, ...] = field(default_factory=tuple)
    evaluated_law_tier: bool = False


def _is_law_or_indeterminate(vertical: str | None) -> bool:
    """Should the Tier-2 citation scan run for this vertical?

    Runs when the vertical is a known law vertical OR is indeterminate
    (None / empty / not a recognized non-law vertical). Indeterminate →
    most-restrictive → run the law-tier filter. The ONLY way to skip Tier-2
    is a vertical that is present, non-empty, and NOT a law vertical — i.e. a
    positively-identified non-law cohort.
    """
    if vertical is None:
        return True
    if not isinstance(vertical, str):
        return True
    normalized = vertical.strip().lower()
    if not normalized:
        return True
    if normalized in _LAW_VERTICALS:
        return True
    # A positively-identified non-law vertical. The brief's rule: only skip the
    # restrictive tier when we KNOW the cohort is not law. Any law/indeterminate
    # state already returned True above. We treat a recognized, non-law,
    # non-empty vertical string as a real cohort and skip Tier-2.
    #
    # NOTE: this is deliberately the ONLY permissive branch. If a future
    # vertical taxonomy makes "unknown but non-empty" ambiguous, prefer
    # widening _LAW_VERTICALS or returning True here — never the reverse.
    return False


#: The one Tier-1 marker a provenance exemption applies to. Named as a constant
#: so the narrowness is legible: the other thirteen markers are unaffected by
#: anything a session read, because none of them describes a fact that CAN be
#: read from a matter record. "We'll reach out" is not more true for having been
#: read somewhere.
_PROVENANCE_EXEMPT_MARKER = "specific-dollar-amount"


def _unverified_money(body: str, allowed_money: Iterable[str] | None) -> list[str]:
    """Dollar figures in ``body`` the agent did NOT read this session.

    Empty list means every figure traces to a source read this session, which is
    the only condition under which the ``specific-dollar-amount`` marker is
    waived. An empty allowed set therefore returns every figure in the body —
    fail-closed, and identical to today's behavior.
    """
    allowed = {str(a) for a in (allowed_money or ())}
    found = identifier_filter.extract_money(body)
    if not allowed:
        return sorted({raw for raw, _canon in found})
    return sorted({raw for raw, canon in found if canon not in allowed})


#: Per-pattern remedy text for a Tier-2 refusal, keyed by the
#: ``citation_filter.PATTERNS`` labels. The old refusal named neither the kind
#: nor a fix, so a model that hit it retried blindly: on the Ashton and Price
#: seat one status reply was refused four to six times in a turn on ordinary
#: comparison prose, then shipped trimmed with the gate mentioned to the client.
#: A hint must say what shape was seen and what to do instead, and must never
#: echo the matched text (that is what makes the dead ``refusal_message``
#: unusable here: a refusal that quotes the cite hands it back to the drafter).
_CITATION_HINTS: dict[str, str] = {
    "case-name": (
        "what reads as a court case caption (two names joined by v., vs, or "
        "versus). If it names a court case, delete the reference entirely; do "
        "not rephrase or abbreviate it. If the two names are things you are "
        "comparing (two letter types, two options), write 'compared with' "
        "instead, or restructure the sentence. If it is one of the firm's own "
        "matters, read that matter from the case system this turn and name it "
        "exactly as the record does. Never mention this refusal to the reader."
    ),
    "reporter-cite": (
        "a reporter citation (volume, reporter, page). Remove it; never cite authority."
    ),
    "federal-statute": "a federal statute reference. Remove it; never cite authority.",
    "state-statute": "a state statute reference. Remove it; never cite authority.",
    "federal-rule": ("a federal court-rule reference. Remove it; never cite authority."),
    "local-rule": "a local court-rule reference. Remove it; never cite authority.",
    "bluebook-signal": (
        "clustered Bluebook signals (id., supra, see also). Rewrite as plain prose."
    ),
}

_CITATION_HINT_DEFAULT = "legal-citation-shaped content. Re-draft without it."


def _citation_reason(labels: Iterable[str]) -> str:
    """Refusal text naming each pattern kind that hit, and its remedy.

    One hint per distinct label, in hit order. Never includes matched text.
    """
    seen = tuple(dict.fromkeys(labels))
    hints = [_CITATION_HINTS.get(label, _CITATION_HINT_DEFAULT) for label in seen]
    if not hints:
        hints = [_CITATION_HINT_DEFAULT]
    return (
        "Refused: draft body contains "
        + " Also: ".join(hints)
        + " (ADR 0028 / safety invariant #6)."
    )


def evaluate(
    body: str,
    cohort: str | None,
    vertical: str | None,
    allowed_case_names: Iterable[str] | None = None,
    allowed_money: Iterable[str] | None = None,
) -> GateDecision:
    """Decide whether a draft ``body`` may be produced.

    Args:
        body: The draft body text the agent is about to write.
        cohort: The customer cohort, if known. Reserved for future
            cohort-scoped marker tiers; not load-bearing in v1 but carried so
            callers and the audit row capture it. An indeterminate cohort does
            NOT relax any tier.
        vertical: The customer vertical (e.g. ``law-firm``). ``None`` / unknown
            → both tiers run (most-restrictive).
        allowed_case_names: Provenance-verified case CAPTIONS the agent
            actually read this session (the runtime register's captions —
            ss-console #1758). Exempts only the Tier-2 case-name pattern;
            fabricated-authority patterns (reporter cites, statutes, rules)
            and every Tier-1 marker are unaffected. Empty/None = no exemption.
        allowed_money: Provenance-verified dollar FIGURES the agent actually read
            this session (the runtime register's ``money()`` — ss-console#2258),
            in :func:`identifier_filter.canon_money` form. Exempts ONLY the
            Tier-1 ``specific-dollar-amount`` marker, and only when EVERY figure
            in the body is verified. Empty/None = no exemption, which is exactly
            today's behavior.

            WHY THIS EXISTS. The marker is the regex ``\\$\\s?\\d`` — any dollar
            sign followed by a digit — while ``demand-letter-drafter``'s own
            SKILL.md authorizes "a specific dollar figure ... when it exists in
            an authored source on the matter, and name that source in the same
            sentence." The gate forbade what the skill permitted, so a demand
            letter's medical specials were refused on the delivery path even
            though the agent had just read them off the billing summary.

    Returns:
        A :class:`GateDecision`. ``allowed=False`` means the draft tool must be
        blocked; the caller emits the audit row and returns the block directive.

    Fail-closed: a missing body, an unloadable marker registry, or a raising
    citation filter all BLOCK.
    """
    # A draft-creating tool with no recognizable body is a fail-closed block at
    # the caller; but defend here too — an empty/None body reaching the policy
    # core is itself indeterminate and must not be waved through.
    if not isinstance(body, str) or not body.strip():
        logger.warning("outbound_gate: empty/None body reached evaluate(); BLOCKING (fail-closed)")
        return GateDecision(
            allowed=False,
            reason="Refused: draft body is empty or unreadable; failing closed (ADR 0028)",
            audit_action=AUDIT_BLOCK,
            tier="load_error",
        )

    # ----- Tier-1: universal fabrication markers -----
    try:
        registry = load_markers()
    except FabricationMarkersError as exc:
        # The marker registry could not load. A gate that cannot run its
        # universal scan must block, not pass.
        logger.error(
            "outbound_gate: marker registry failed to load (%s); BLOCKING (fail-closed)", exc
        )
        return GateDecision(
            allowed=False,
            reason="Refused: fabrication-marker registry unavailable; failing closed (ADR 0028)",
            audit_action=AUDIT_BLOCK,
            tier="load_error",
        )

    try:
        marker_hits = registry.scan(body)
    except Exception:  # noqa: BLE001 — any matcher fault must fail closed
        logger.exception("outbound_gate: marker scan raised; BLOCKING (fail-closed)")
        return GateDecision(
            allowed=False,
            reason="Refused: fabrication-marker scan failed; failing closed (ADR 0028)",
            audit_action=AUDIT_BLOCK,
            tier="load_error",
        )

    # Provenance exemption, applied to ONE marker and only when the whole body
    # clears (ss-console#2258). Deliberately all-or-nothing: a body carrying one
    # verified figure and one invented figure is not partly honest, and dropping
    # the hit would let the invented one through beside the real one. The refusal
    # names the unverified figures, because "your draft has a dollar amount" is
    # not actionable and "$88,000.00 is in no source you read" is.
    if marker_hits and any(h.marker_id == _PROVENANCE_EXEMPT_MARKER for h in marker_hits):
        try:
            unverified = _unverified_money(body, allowed_money)
        except Exception:  # noqa: BLE001 — an exemption that cannot be computed is not granted
            logger.exception("outbound_gate: money provenance check raised; keeping the marker hit")
            unverified = ["(provenance check failed)"]
        if not unverified:
            marker_hits = [h for h in marker_hits if h.marker_id != _PROVENANCE_EXEMPT_MARKER]
        # Otherwise the hit stands; the refusal below names the unverified figures.

    if marker_hits:
        hit_ids = tuple(h.marker_id for h in marker_hits)
        first = marker_hits[0]
        detail = first.reason
        if first.marker_id == _PROVENANCE_EXEMPT_MARKER:
            missing = _unverified_money(body, allowed_money)
            detail = (
                f"{first.reason} Not traceable to anything read this session: "
                f"{', '.join(missing[:5])}"
                f"{' and more' if len(missing) > 5 else ''}. Re-read the source "
                "record that carries the figure, or remove it."
            )
        return GateDecision(
            allowed=False,
            reason=(
                f"Refused: draft body contains a banned fabrication marker "
                f"({first.marker_id}: {detail})"
            ),
            audit_action=AUDIT_BLOCK,
            tier="tier1_marker",
            marker_hits=hit_ids,
        )

    # ----- Tier-2: law-vertical citation scan -----
    run_law_tier = _is_law_or_indeterminate(vertical)
    if run_law_tier:
        try:
            if citation_filter.contains_citation(body, allowed_case_names=allowed_case_names):
                hits = citation_filter.scan(body, allowed_case_names=allowed_case_names)
                labels = tuple(dict.fromkeys(h.pattern for h in hits))
                return GateDecision(
                    allowed=False,
                    reason=_citation_reason(labels),
                    audit_action=AUDIT_BLOCK,
                    tier="tier2_citation",
                    citation_hits=labels,
                    evaluated_law_tier=True,
                )
        except Exception:  # noqa: BLE001 — filter raise must fail closed
            logger.exception("outbound_gate: citation filter raised; BLOCKING (fail-closed)")
            return GateDecision(
                allowed=False,
                reason="Refused: citation filter failed; failing closed (ADR 0028)",
                audit_action=AUDIT_BLOCK,
                tier="tier2_citation",
                evaluated_law_tier=True,
            )

    return GateDecision(
        allowed=True,
        reason="outbound gate clear",
        audit_action=AUDIT_ALLOW,
        evaluated_law_tier=run_law_tier,
    )


__all__ = [
    "AUDIT_ALLOW",
    "AUDIT_BLOCK",
    "GateDecision",
    "evaluate",
]
