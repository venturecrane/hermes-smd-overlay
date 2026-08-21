"""Did this reply confirm a rule? (shared/rule_confirm, ss-console#2529)

THE TEST THIS FILE IS BUILT AROUND is the second one:
``test_a_quoted_readback_cannot_confirm_itself``. The block the Operator sends
says "Reply yes to confirm", so a mail client that quotes it back has put the
word "yes" and the rule's tag into the reply. Testing the whole message for an
affirmative therefore lets the Operator confirm its own proposal, on any thread,
without the person typing anything. Every other property here is a fence around
a different way of arriving at the same place: an "ok" on an unrelated thread, a
tag with no answer, two rules and one yes, somebody else's rule.

The costs are asymmetric and the code is built on that. Asking again costs one
reply. Guessing costs a standing rule the firm never agreed to, applied to every
future document of its kind, discovered whenever somebody reads a letter and
wonders why it sounds like that.
"""

from __future__ import annotations

import pytest

from shared import rule_confirm as rc

ADMIN = "christa@firm.com"
PARALEGAL = "sarah@firm.com"
RULE_A = "7f3a2c1d"
RULE_B = "0b91ee42"


def _row(proposal_id=RULE_A, *, instructed_by=ADMIN, for_admin=False, scope="firm_adjust"):
    return {
        "proposal_id": proposal_id,
        "scope": scope,
        "subject": {"output_class": "outbound", "property": "voice"},
        "text": "In client letters, be more formal and shorter.",
        "readback": f"[rule {proposal_id}] In client letters, be more formal and shorter.",
        "instructed_by": instructed_by,
        "for_admin": for_admin,
    }


READBACK = _row()["readback"]

QUOTED_REPLY = f"""yes

On Thu, 21 Aug 2026 at 18:04, Operator <ops@firm.com> wrote:
> {READBACK}
>
> Reply yes to confirm.
"""


# ---------------------------------------------------------------------------
# Stripping the quote
# ---------------------------------------------------------------------------


def test_strip_quoted_keeps_only_the_senders_own_words():
    assert rc.strip_quoted(QUOTED_REPLY) == "yes"


@pytest.mark.parametrize(
    "header",
    [
        "On Thu, 21 Aug 2026 at 18:04, Operator <ops@firm.com> wrote:",
        "From: Operator <ops@firm.com>",
        "Sent: Thursday, August 21, 2026 6:04 PM",
        "-----Original Message-----",
        "---------- Forwarded message ---------",
        "________________________________",
    ],
)
def test_every_quote_header_truncates(header):
    body = f"yes\n\n{header}\nBelow this is not theirs: no, cancel that.\n"
    assert rc.strip_quoted(body) == "yes"


def test_the_signature_is_not_the_reply():
    body = "yes\n\n-- \nSarah, who never says no to anything\n"
    assert rc.strip_quoted(body) == "yes"


def test_a_quoted_readback_cannot_confirm_itself():
    """THE test. The readback the Operator sends contains the word "yes" and
    the rule's tag, so a client that quotes it back has supplied both halves of
    a confirmation the person never gave."""
    only_the_quote = (
        f"On Thu, 21 Aug 2026, Operator wrote:\n> {READBACK}\n> Reply yes to confirm.\n"
    )
    assert rc.strip_quoted(only_the_quote) == ""
    assert rc.read_own_text(only_the_quote).affirmative is False
    verdict = rc.resolve(only_the_quote, [_row()], ADMIN, is_admin=True)
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_NEEDS_AFFIRMATIVE


# ---------------------------------------------------------------------------
# Reading the sender's own words
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", list(rc.BARE_AFFIRMATIVES))
def test_every_declared_affirmative_reads_as_one(word):
    assert rc.read_own_text(word).affirmative is True


@pytest.mark.parametrize("word", list(rc.APPLY_TOKENS))
def test_apply_tokens_are_their_own_class(word):
    reading = rc.read_own_text(word)
    assert reading.affirmative is True
    assert reading.apply_others is True


@pytest.mark.parametrize(
    "body",
    ["no", "no thanks", "not yet", "don't", "cancel that", "hold off", "actually, wait"],
)
def test_negations_read_as_negations(body):
    assert rc.read_own_text(body).negated is True


@pytest.mark.parametrize("body", ["yes but make it letters only", "yes, change it to memos"])
def test_a_qualified_yes_is_both(body):
    reading = rc.read_own_text(body)
    assert reading.affirmative is True
    assert reading.negated is True


@pytest.mark.parametrize("body", ["Please attribute this to the partner.", "another matter"])
def test_word_boundaries_hold(body):
    """A substring test makes "but" match "attribute" and "no" match
    "another", so every reply would read as a refusal."""
    assert rc.read_own_text(body).negated is False


def test_curly_apostrophes_do_not_hide_an_answer():
    assert rc.read_own_text("that’s right").affirmative is True


# ---------------------------------------------------------------------------
# Finding the tag
# ---------------------------------------------------------------------------


def test_the_tag_is_found_in_the_quoted_half():
    """Deliberately the whole message: on most clients the person's yes sits
    above a quoted copy of the readback, so that is where the tag will be."""
    assert rc.find_tags(QUOTED_REPLY) == (RULE_A,)


def test_tags_are_deduplicated_in_order():
    body = f"[rule {RULE_B}] and [rule {RULE_A}] and [rule {RULE_B}] again"
    assert rc.find_tags(body) == (RULE_B, RULE_A)


@pytest.mark.parametrize(
    "bad", ["[rule 7F3A2C1D]", "[rule 7f3a]", "[rule zzzzzzzz]", "rule 7f3a2c1d"]
)
def test_a_tag_that_is_not_the_shape_is_not_a_tag(bad):
    assert rc.find_tags(bad) == ()


# ---------------------------------------------------------------------------
# The whole decision
# ---------------------------------------------------------------------------


def test_a_bare_yes_on_a_quoted_thread_confirms_the_senders_own_rule():
    verdict = rc.resolve(QUOTED_REPLY, [_row()], ADMIN, is_admin=True)
    assert verdict.kind == rc.CONFIRMED
    assert verdict.proposal_id == RULE_A


def test_an_affirmative_with_no_tag_asks_rather_than_picking():
    """This kills the "ok" on an unrelated thread. The person agreed with
    something; nothing says it was a rule."""
    verdict = rc.resolve("ok, thanks", [_row(), _row(RULE_B)], ADMIN, is_admin=True)
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_NEEDS_TAG
    assert set(verdict.candidates) == {RULE_A, RULE_B}


def test_a_tag_with_no_affirmative_asks():
    verdict = rc.resolve(f"about [rule {RULE_A}]", [_row()], ADMIN, is_admin=True)
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_NEEDS_AFFIRMATIVE


def test_two_named_rules_and_one_yes_asks():
    body = f"yes [rule {RULE_A}] [rule {RULE_B}]"
    verdict = rc.resolve(body, [_row(), _row(RULE_B)], ADMIN, is_admin=True)
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_AMBIGUOUS
    assert set(verdict.candidates) == {RULE_A, RULE_B}


def test_a_plain_no_declines():
    verdict = rc.resolve(f"no, leave it [rule {RULE_A}]", [_row()], ADMIN, is_admin=True)
    assert verdict.kind == rc.DECLINED
    assert verdict.candidates == (RULE_A,)


def test_a_qualified_yes_is_neither_committed_nor_reported_as_a_refusal():
    """ "Yes but make it letters only" agrees with something other than the
    sentence as stated. Committing it is wrong; telling them they declined is
    also wrong."""
    body = f"yes but make it letters only [rule {RULE_A}]"
    verdict = rc.resolve(body, [_row()], ADMIN, is_admin=True)
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_QUALIFIED


def test_a_tag_nobody_holds_asks_and_commits_nothing():
    verdict = rc.resolve("yes [rule deadbeef]", [_row()], ADMIN, is_admin=True)
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_UNKNOWN_TAG


def test_nothing_pending_is_nothing_to_do():
    assert rc.resolve("yes", [], ADMIN, is_admin=True).kind == rc.NONE


def test_a_reply_that_is_not_an_answer_at_all_does_nothing():
    verdict = rc.resolve("Can you pull the Ashton file?", [_row()], ADMIN, is_admin=True)
    assert verdict.kind == rc.NONE


# ---------------------------------------------------------------------------
# Standing: whose rule is it to confirm
# ---------------------------------------------------------------------------


def test_a_bare_yes_does_not_release_a_rule_waiting_on_an_admin():
    """The paralegal who stated it cannot confirm their own — that it waits is
    the entire point of it waiting."""
    row = _row(instructed_by=PARALEGAL, for_admin=True)
    verdict = rc.resolve(f"yes [rule {RULE_A}]", [row], PARALEGAL, is_admin=False)
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_NOT_THEIRS


def test_an_admin_releases_it_by_applying_it():
    row = _row(instructed_by=PARALEGAL, for_admin=True)
    verdict = rc.resolve(f"apply that [rule {RULE_A}]", [row], ADMIN, is_admin=True)
    assert verdict.kind == rc.CONFIRMED
    assert verdict.proposal_id == RULE_A


def test_apply_that_from_a_non_admin_releases_nothing():
    row = _row(instructed_by=PARALEGAL, for_admin=True)
    verdict = rc.resolve(f"apply that [rule {RULE_A}]", [row], PARALEGAL, is_admin=False)
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_NOT_THEIRS


def test_apply_that_does_not_reach_a_rule_that_is_not_waiting():
    """The two lanes are disjoint. An admin saying "apply that" over their OWN
    outstanding rule has named the wrong verb for it, and the Operator asks
    rather than treating the two as interchangeable."""
    verdict = rc.resolve(f"apply that [rule {RULE_A}]", [_row()], ADMIN, is_admin=True)
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_NOT_THEIRS


def test_one_person_cannot_confirm_anothers_rule_with_a_bare_yes():
    row = _row(instructed_by=ADMIN, for_admin=False)
    verdict = rc.resolve(f"yes [rule {RULE_A}]", [row], PARALEGAL, is_admin=False)
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_NOT_THEIRS


def test_addresses_compare_case_insensitively():
    row = _row(instructed_by="Christa@Firm.com")
    verdict = rc.resolve(f"yes [rule {RULE_A}]", [row], ADMIN, is_admin=True)
    assert verdict.kind == rc.CONFIRMED
