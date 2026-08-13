"""The ``specific-dollar-amount`` provenance exemption (ss-console#2258).

WHY THIS EXISTS. ``demand-letter-drafter``'s SKILL.md authorizes "a specific
dollar figure ... when it exists in an authored source on the matter, and name
that source in the same sentence." The Tier-1 marker is the regex ``\\$\\s?\\d``
— any dollar sign followed by a digit — so the gate forbade exactly what the
skill permitted. Rehearsing card 18 on the pilot, the delivery of a demand
letter was refused twice on medical specials the agent had just read off the
billing summary on the matter.

THREE PROPERTIES, and the third is the one that decides whether this is a
narrowing or a hole:

 1. A figure READ this session passes.
 2. A figure the agent INVENTED still blocks, and the refusal names it.
 3. **The other thirteen markers still fire, on every path.** A change that
    quietly widened past its one marker would be indistinguishable from the
    registry-emptying design this replaced, and that design would have stripped
    every prose-bearing write on a law seat.

ALL-OR-NOTHING BY DESIGN. A body with one verified figure and one invented one
is not partly honest; waiving the marker there would let the invented figure
ride out beside the real one.
"""

from __future__ import annotations

import pytest

from shared import identifier_filter
from shared.identifier_filter import ProvenanceRegister, canon_money, extract_money
from shared.outbound_gate import evaluate

_LAW = "law-firm"


def _reg(*read_blobs: str) -> ProvenanceRegister:
    reg = ProvenanceRegister()
    for blob in read_blobs:
        reg.add_read_text(blob)
    return reg


# --------------------------------------------------------------------------- #
# Canonicalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "canon"),
    [
        ("$41,515.00", "41515"),
        ("$41515", "41515"),
        ("$41,515", "41515"),
        ("$ 41,515.00", "41515"),
        ("$41,515.50", "41515.5"),
        ("$0.99", "0.99"),
        ("$1,000,000.00", "1000000"),
    ],
)
def test_the_same_amount_written_differently_compares_equal(raw: str, canon: str) -> None:
    """A draft that writes a figure without the cents it was read with has not
    fabricated anything. If these did not fold together, the exemption would be
    unreachable in practice."""
    assert canon_money(raw) == canon


def test_amounts_that_differ_stay_distinct() -> None:
    """The fold must not be so aggressive that it verifies a figure nobody read.
    41,515.50 and 41,515.05 are different amounts."""
    assert canon_money("$41,515.50") != canon_money("$41,515.05")
    assert canon_money("$4,151.50") != canon_money("$41,515.00")


@pytest.mark.parametrize("junk", ["$", "$ ", "", "no money here"])
def test_unparseable_input_yields_no_canonical(junk: str) -> None:
    """An empty canonical never matches an allowed set, so a parse failure
    WITHHOLDS the exemption rather than granting it."""
    assert canon_money(junk) == ""


def test_the_extractor_sees_more_than_the_marker_does() -> None:
    """The marker regex matches ``$`` plus ONE digit. The exemption has to see
    the whole figure, or it would compare "$4" against the register."""
    found = dict(extract_money("Billed $41,515.00 and $1,000,000.00 in limits."))
    assert set(found) == {"$41,515.00", "$1,000,000.00"}


# --------------------------------------------------------------------------- #
# The register
# --------------------------------------------------------------------------- #


def test_reading_a_billing_summary_registers_its_figures() -> None:
    reg = _reg("Valley Medical Center ER (2026-01-14)  $  4,820.00\nTOTAL  $41,515.00\n")
    assert canon_money("$4,820.00") in reg.money()
    assert canon_money("$41,515") in reg.money()


def test_money_is_invisible_to_the_identifier_gate() -> None:
    """THE boundary. ``IdKind`` drives the A1 gate, which REFUSES what it cannot
    verify. Putting money there would start blocking every dollar figure not
    read this session, on every draft, on every path — a large tightening nobody
    asked for. This mirrors ``captions()`` instead: registered for one exemption,
    never consulted by ``verifies``.

    FALSIFIER: add a MONEY member to IdKind and wire it into ``_extract``, and
    this fails.
    """
    assert not any("money" in k.value.lower() for k in identifier_filter.IdKind)
    reg = _reg("The bill was $4,820.00")
    # The register learned the figure...
    assert reg.money()
    # ...and the A1 surface is unchanged: no identifier hit was created for it.
    hits = identifier_filter._extract("The bill was $4,820.00", include_names=False)
    assert not hits


def test_the_money_set_is_bounded() -> None:
    """A pathological read blob must not grow a session register without limit.
    Adds past the cap are ignored, which leaves later figures UNVERIFIED — the
    narrow direction."""
    reg = ProvenanceRegister()
    reg.add_read_text(" ".join(f"${n}.00" for n in range(1, 700)))
    assert len(reg.money()) <= ProvenanceRegister._MAX_MONEY


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

_READ = "Billing summary: ER $4,820.00, surgery $22,415.00, TOTAL $41,515.00"


def test_a_figure_read_this_session_passes() -> None:
    """Property 1, and the whole point. This is the delivery that was refused
    twice on the pilot."""
    reg = _reg(_READ)
    body = "Medical specials total $41,515.00 per the billing summary on the matter."
    decision = evaluate(body, None, _LAW, allowed_money=reg.money())
    assert decision.allowed, decision.reason


def test_an_invented_figure_still_blocks_and_is_named() -> None:
    """Property 2. "Your draft has a dollar amount" is not actionable;
    "$88,000.00 is in no source you read" is."""
    reg = _reg(_READ)
    body = "We value this claim at $88,000.00."
    decision = evaluate(body, None, _LAW, allowed_money=reg.money())
    assert not decision.allowed
    assert "specific-dollar-amount" in decision.marker_hits
    assert "$88,000.00" in decision.reason
    assert "read this session" in decision.reason


def test_one_invented_figure_poisons_a_body_full_of_verified_ones() -> None:
    """All-or-nothing. A body with one real figure and one invented figure is
    not partly honest, and waiving the marker would let the invented one ride
    out beside the real one."""
    reg = _reg(_READ)
    body = "Specials are $41,515.00 and we demand $88,000.00 to resolve."
    decision = evaluate(body, None, _LAW, allowed_money=reg.money())
    assert not decision.allowed
    assert "$88,000.00" in decision.reason
    assert "$41,515.00" not in decision.reason, "the verified figure must not be blamed"


def test_an_empty_register_changes_nothing() -> None:
    """Fail-closed default, identical to the behavior before this existed. A
    session that read nothing gets no exemption."""
    body = "Medical specials total $41,515.00."
    for allowed in (None, frozenset(), ProvenanceRegister().money()):
        decision = evaluate(body, None, _LAW, allowed_money=allowed)
        assert not decision.allowed, f"{allowed!r} must not exempt anything"


def test_a_caller_that_never_passes_money_is_unaffected() -> None:
    """The parameter is optional, and every existing caller that omits it keeps
    the old behavior exactly."""
    assert not evaluate("Total $41,515.00", None, _LAW).allowed


# --------------------------------------------------------------------------- #
# Property 3: the narrowing did not become a hole
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        ("We will reach out to schedule kickoff.", "well-reach-out"),
        ("Coming soon to your portal.", "coming-soon"),
        ("Work begins within two weeks of signing.", "work-begins-within"),
        ("Replies within 1 business day.", "replies-within-n-business-day"),
        ("A stabilization period follows handoff.", "stabilization-period"),
        ("Results are guaranteed.", "guarantee"),
        ("We will have it by next week.", "by-next-week"),
        ("Ready by end of month.", "by-end-of"),
        ("We shadow and observe.", "we-shadow-and-observe"),
        ("We redesign together.", "we-redesign-together"),
        ("Training and handoff follow.", "training-and-handoff"),
    ],
)
def test_every_other_marker_still_fires_with_a_full_money_register(body: str, marker: str) -> None:
    """PROPERTY 3, the one that decides whether this is a narrowing or a hole.

    A session that read every dollar figure in the record must not thereby get
    a pass on "we'll reach out" or "guaranteed". None of these markers describes
    a fact that CAN be read from a matter record — reading a promise somewhere
    does not make it true — so provenance is irrelevant to all of them and the
    exemption must not touch them.

    EVERY BODY CARRIES A VERIFIED FIGURE, and that is not decoration. Without
    one, ``specific-dollar-amount`` never hits, the exemption branch never runs,
    and this test passes no matter how far the exemption widens — it would be a
    check that cannot fail. Mutation-tested: widening the exemption to drop ALL
    marker hits fails all eleven of these with the figure present, and none of
    them without it.
    """
    reg = _reg(_READ)
    decision = evaluate(f"Specials are $41,515.00. {body}", None, _LAW, allowed_money=reg.money())
    assert not decision.allowed, f"{marker} stopped firing"
    assert marker in decision.marker_hits
    assert "specific-dollar-amount" not in decision.marker_hits, "the figure WAS verified"


def test_a_verified_figure_does_not_rescue_a_body_with_another_marker() -> None:
    """The exemption removes ONE marker's hit, not the block. A body whose money
    is fully verified and which also promises a callback is still refused."""
    reg = _reg(_READ)
    body = "Specials are $41,515.00 and we will reach out to schedule."
    decision = evaluate(body, None, _LAW, allowed_money=reg.money())
    assert not decision.allowed
    assert "well-reach-out" in decision.marker_hits
    assert "specific-dollar-amount" not in decision.marker_hits


def test_the_citation_tier_is_untouched() -> None:
    """Tier-2 is the Mata guard and has nothing to do with money."""
    reg = _reg(_READ)
    body = "Specials are $41,515.00. See Gonzalez v. Ramirez, 44 Cal.App.5th 112 (2020)."
    decision = evaluate(body, None, _LAW, allowed_money=reg.money())
    assert not decision.allowed
    assert decision.tier == "tier2_citation"
