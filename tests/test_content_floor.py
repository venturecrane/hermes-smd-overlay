"""Tests for the content-sensitivity floor (ADR 0031).

The floor forces money / contract / scope / legal outbound content to draft even
under an autonomous external_send ceiling. Posture is fail-toward-draft: an
empty / unreadable body is treated as sensitive.
"""

import pytest

from shared.content_floor import classify

# ---------------------------------------------------------------------------
# Clean content passes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Got it, that works on my end. Talk soon.",
        "Thanks for the update, I'll take a look tomorrow.",
        "Sounds good. See you at the meeting.",
        "Here are the notes from our call.",
    ],
)
def test_clean_text_is_not_sensitive(text) -> None:
    result = classify(text)
    assert result.sensitive is False
    assert result.categories == ()


# ---------------------------------------------------------------------------
# Each category trips the floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,category",
    [
        ("Please remit payment of $500 by Friday.", "money"),
        ("Can you send the invoice for last month?", "money"),
        ("Wire the deposit to the account below.", "money"),
        ("Attached is the contract, please sign and return.", "contract"),
        ("We can execute the agreement once terms are final.", "contract"),
        ("Here is the statement of work and the deliverables.", "scope"),
        ("We will deliver the milestones by the deadline.", "scope"),
        ("Our attorney advised on the liability waiver.", "legal"),
        ("This settlement is binding on both parties.", "legal"),
    ],
)
def test_sensitive_categories_trip_floor(text, category) -> None:
    result = classify(text)
    assert result.sensitive is True
    assert category in result.categories
    assert result.hits  # at least one trigger fragment, never the full body


# ---------------------------------------------------------------------------
# Disclaimer carve-out (2026-06-14) — standard not-legal-advice / attorney-
# client boilerplate must NOT trip the legal category. Regression for the
# reply-channel test where ``REPLY_HELD reason=content_sensitive``
# fired on a benign "this is not legal advice" disclaimer.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # The exact draft phrasing that blocked the relay on 2026-06-14.
        "Nothing in this note should be read as legal advice.",
        "This is not legal advice.",
        "This email does not constitute legal advice.",
        "This is not intended as legal advice and no attorney-client relationship is created.",
        "We cannot provide legal advice in this format.",
        "Please note: this should not be construed as legal advice.",
        "No attorney-client relationship is formed by this message.",
        "This does not create an attorney-client relationship.",
        "We are not your attorneys.",
    ],
)
def test_not_legal_advice_disclaimers_pass(text) -> None:
    result = classify(text)
    assert result.sensitive is False, f"disclaimer wrongly flagged: {result.categories}"
    assert result.categories == ()


@pytest.mark.parametrize(
    "text,category",
    [
        # A disclaimer does NOT launder genuinely sensitive content elsewhere
        # in the same message — the carve-out is clause-local.
        (
            "This is not legal advice, but please wire $5,000 to the account below.",
            "money",
        ),
        (
            "Nothing here is legal advice; the contract is attached for signature.",
            "contract",
        ),
    ],
)
def test_disclaimer_does_not_launder_other_sensitive_content(text, category) -> None:
    result = classify(text)
    assert result.sensitive is True
    assert category in result.categories


@pytest.mark.parametrize(
    "text",
    [
        # Genuine legal weight with no disclaimer negation must still trip,
        # including "legal advice" actually being GIVEN.
        "Here is my legal advice: sign immediately.",
        "We will pursue legal action if this is not resolved.",
        "This raises a legal issue we need to discuss.",
    ],
)
def test_genuine_legal_content_still_trips(text) -> None:
    result = classify(text)
    assert result.sensitive is True
    assert "legal" in result.categories


# ---------------------------------------------------------------------------
# Fail toward draft on indeterminate input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "", "   ", "\n\t"])
def test_empty_or_none_is_sensitive(bad) -> None:
    result = classify(bad)
    assert result.sensitive is True
    assert "indeterminate" in result.categories


def test_hits_are_lowercased_and_deduped() -> None:
    result = classify("INVOICE invoice Invoice")
    assert result.sensitive is True
    assert result.hits.count("invoice") == 1


def test_multiple_categories_reported_sorted() -> None:
    result = classify("Sign the contract and wire the $1,000 payment.")
    assert result.sensitive is True
    assert "money" in result.categories
    assert "contract" in result.categories
    assert list(result.categories) == sorted(result.categories)
