"""Case-caption shapes the Tier-2 citation gate must catch, and the hint it returns.

Two defects, one file.

1. "Smith versus Jones" was caught by nothing. ``CASE_NAME_RE`` folds only
   ``v`` and ``vs``, and ``canonical_caption`` did not know the spelled-out
   word either. A fabricated caption written in longhand walked straight past
   the venture-killer guard.
2. The Tier-2 refusal string was fixed prose that named neither the pattern
   kind nor a remedy. On the Ashton and Price seat the Operator's own status
   reply was refused four to six times per turn on ordinary comparison prose
   ("third-party vs first-party letters"), retried blindly with no signal about
   what to change, and finally told the client about the citation gate.

The ``versus`` pattern is case-SENSITIVE in its parties, unlike ``CASE_NAME_RE``:
"versus" is ordinary English, so it is a caption only between Capitalized names.
"apples versus oranges" must pass; "Palsgraf versus Long Island Railroad" must not.
"""

from shared import provenance
from shared.citation_filter import contains_citation, scan
from shared.identifier_filter import ProvenanceRegister
from shared.outbound_gate import evaluate

# ---- blocked shapes ---------------------------------------------------------

BLOCKED = [
    "SMITH V. JONES held that the rule applies.",
    "see smith v jones for the rule",
    "as held in johnson v. metro transit",
    "see smith vs jones for the controlling rule",
    "Smith vs. Jones",
    "SMITH VS. JONES",
    "In re Marriage of Smith",
    "in re smith",
    "Palsgraf versus Long Island Railroad",
    "Smith versus Jones",
]


def test_case_name_shapes_block() -> None:
    for body in BLOCKED:
        assert contains_citation(body), f"should block: {body!r}"
        labels = {h.pattern for h in scan(body)}
        assert "case-name" in labels, f"should hit case-name: {body!r}"


# ---- allowed prose ----------------------------------------------------------

ALLOWED = [
    "apples versus oranges",
    "version two versus version three",
    "we compared the intake letter with the follow-up letter",
]


def test_ordinary_comparison_prose_passes() -> None:
    for body in ALLOWED:
        assert scan(body) == [], f"should be clean: {body!r} -> {scan(body)}"


# ---- allowlist --------------------------------------------------------------

_VERSUS_BODY = "Per the file, Espinoza versus Kaviani is set for Tuesday."


def test_versus_caption_blocks_without_allowlist() -> None:
    assert contains_citation(_VERSUS_BODY)


def test_versus_caption_passes_when_the_v_form_is_allowlisted() -> None:
    """A caption read as "Espinoza v. Kaviani" also exempts the longhand form."""
    assert not contains_citation(_VERSUS_BODY, allowed_case_names=["ESPINOZA v. Kaviani"])


# ---- gate reason ------------------------------------------------------------


def test_case_name_reason_names_the_kind_and_the_remedy() -> None:
    d = evaluate("As held in Smith v. Jones, the claim is strong.", None, "law-firm")
    assert d.allowed is False
    assert d.tier == "tier2_citation"
    assert d.citation_hits == ("case-name",)
    assert d.reason.startswith("Refused:")
    assert "court case caption" in d.reason
    assert "delete the reference" in d.reason
    assert "compared with" in d.reason


def test_reason_never_echoes_matched_text() -> None:
    d = evaluate("As held in Smith v. Jones, the claim is strong.", None, "law-firm")
    assert "Smith" not in d.reason
    assert "Jones" not in d.reason


def test_multiple_labels_each_get_a_hint_without_echoing_text() -> None:
    d = evaluate("Roe v. Wade, 410 U.S. 113 controls here.", None, "law-firm")
    assert d.allowed is False
    assert "court case caption" in d.reason
    assert "reporter citation" in d.reason
    assert "Roe" not in d.reason
    assert "410" not in d.reason


# ---- provenance -------------------------------------------------------------


def test_versus_caption_registers_in_provenance() -> None:
    reg = ProvenanceRegister()
    provenance._record_captions(
        reg, "Matter 2026-PI-101. Status call on Espinoza versus Kaviani next week."
    )
    assert "espinoza v. kaviani" in reg.captions()
