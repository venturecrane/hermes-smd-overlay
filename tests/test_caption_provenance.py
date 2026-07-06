"""Caption provenance allowlist for the Tier-2 citation gate (ss #1758).

92 case-name refusals in one rehearsal day were the matter's OWN caption in
memos about that matter — the case-name pattern cannot distinguish fabricated
case law from the case being worked. The fix: captions harvested from
READ-tool results enter the session's provenance register; the gate exempts
only bare case-name hits that match a registered caption. Fabricated-authority
patterns (reporter cites, statutes, rules) and Tier-1 markers never relax.
"""

from shared import provenance
from shared.identifier_filter import ProvenanceRegister
from shared.outbound_gate import evaluate

CAPTION_READ = (
    "Matter 2026-PI-101. Discovery capture on Alvarez v. Draper: "
    "RFP Set One served by mail June 20, 2026."
)


def _register_with_captions(text: str) -> ProvenanceRegister:
    reg = ProvenanceRegister()
    provenance._record_captions(reg, text)
    return reg


# ---- register harvesting ----------------------------------------------------


def test_record_read_harvests_captions() -> None:
    reg = _register_with_captions(CAPTION_READ)
    assert "alvarez v. draper" in reg.captions()


def test_greedy_spillover_still_yields_core_caption() -> None:
    reg = _register_with_captions("Discovery capture on Alvarez v. Draper is our matter")
    assert "alvarez v. draper" in reg.captions()


def test_in_re_registers_whole_form() -> None:
    reg = _register_with_captions("Petition filed In re Ramirez this week.")
    assert any(c.startswith("in re") and "ramirez" in c for c in reg.captions())


def test_caption_cap_ignores_new_never_widens() -> None:
    reg = ProvenanceRegister()
    for i in range(600):
        reg.add_caption(f"party{i} v. other{i}")
    assert len(reg.captions()) <= 512


# ---- gate behavior ----------------------------------------------------------


def _allowed(body: str, captions: frozenset[str]) -> bool:
    return evaluate(body, None, "law-firm", allowed_case_names=captions).allowed


def test_unread_caption_blocks() -> None:
    d = evaluate("Compare Mata v. Avianca on this point.", None, "law-firm")
    assert not d.allowed
    assert d.tier == "tier2_citation"


def test_read_caption_passes_in_memo_body() -> None:
    captions = _register_with_captions(CAPTION_READ).captions()
    assert _allowed(
        "Verification chase run on Alvarez v. Draper. No signed document found.",
        captions,
    )


def test_unread_case_law_blocks_even_with_registered_caption() -> None:
    captions = _register_with_captions(CAPTION_READ).captions()
    assert not _allowed(
        "On Alvarez v. Draper, the rule from Mata v. Avianca applies.",
        captions,
    )


def test_registered_caption_as_fabricated_authority_blocks() -> None:
    captions = _register_with_captions(CAPTION_READ).captions()
    assert not _allowed(
        "Alvarez v. Draper, 123 Cal. App. 5th 456 controls here.",
        captions,
    )


def test_statute_blocks_regardless_of_captions() -> None:
    captions = _register_with_captions(CAPTION_READ).captions()
    assert not _allowed("Responses run under 42 U.S.C. § 1983.", captions)


def test_tier1_markers_unaffected() -> None:
    captions = _register_with_captions(CAPTION_READ).captions()
    d = evaluate(
        "Alvarez v. Draper update — verification outstanding.",
        None,
        "law-firm",
        allowed_case_names=captions,
    )
    assert not d.allowed  # em dash is a Tier-1 marker; captions never relax it


def test_empty_register_means_no_exemption() -> None:
    assert not _allowed("Alvarez v. Draper memo.", frozenset())


def test_end_to_end_record_read_to_register_for() -> None:
    session = "test-caption-session-1758"
    provenance.record_read(session, CAPTION_READ)
    captions = provenance.register_for(session).captions()
    assert "alvarez v. draper" in captions
    assert _allowed("Deadline proposed on Alvarez v. Draper, confirm before relying.", captions)
