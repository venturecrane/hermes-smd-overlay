"""Authored shape is checked, every time (ss ADR 0083 §3, criterion 7).

ADR 0083 draws the line these tests sit on: *"format is a separate axis from
voice, and it is binary where voice is probabilistic. A model writes IN a
register and one grades whether it sounds right; typography either complies or
does not."*

So format gets a real check and voice keeps the read-gate. Promising to enforce
how something SOUNDS is a promise the substrate cannot keep; promising a
required closing line is present is one it can keep every time. *"Once authored,
a format is binding and deterministic, every time. Bindingness does not vary by
document importance."*

THE RULE SET IS NOT HYPOTHETICAL. It is the four rules from the 2026-07-31 live
probe, and the one the Operator broke is `single_closing_line`: it closed
correctly AND carried a second closer earlier in the text. An ends-with check
would have passed that output. That is why "exactly one" is a separate rule from
"ends with", and why this file tests it directly.
"""

from __future__ import annotations

from shared import format_check

FOUR_RULES = {
    "opening_line_prefix": "Bottom line:",
    "closing_line_prefix": "Next:",
    "single_closing_line": True,
    "forbid_bullets": True,
    "forbid_substrings": ["utilize"],
}

COMPLIANT = """Bottom line: the seat rebuilt cleanly.

We rebuilt twice and found a permissions defect, which is now fixed. Nothing
touched production and no customer data was involved.

Next: confirm the fix holds through the next rebuild.
"""


def test_a_compliant_output_passes():
    assert format_check.check(COMPLIANT, FOUR_RULES) == []


def test_no_assertions_means_nothing_is_checked():
    """An authored prose spec with no machine rules blocks nothing. Format is
    empty until the customer authors it, and empty means persona judgment."""
    assert format_check.check("anything at all", {}) == []
    assert format_check.check("anything at all", None) == []


def test_the_live_failure_is_caught():
    """THE REGRESSION. The real output ended correctly and carried a second
    closer earlier — an ends-with check passes it; this must not."""
    body = COMPLIANT.replace("We rebuilt twice", "Next: something premature.\n\nWe rebuilt twice")
    rules = [v.rule for v in format_check.check(body, FOUR_RULES)]
    assert "single_closing_line" in rules


def test_a_missing_opening_is_caught_and_names_what_it_found():
    v = format_check.check("We rebuilt twice.\n\nNext: go.", FOUR_RULES)
    assert [x.rule for x in v] == ["opening_line_prefix"]
    assert "Bottom line:" in v[0].detail


def test_a_missing_closing_is_caught():
    v = format_check.check("Bottom line: done.\n\nAnd that is all.", FOUR_RULES)
    assert [x.rule for x in v] == ["closing_line_prefix"]


def test_bullets_are_caught_in_every_common_form():
    for line in ("- one", "* one", "1. one", "2) one", "• one"):
        body = f"Bottom line: x.\n\n{line}\n\nNext: y."
        rules = [v.rule for v in format_check.check(body, FOUR_RULES)]
        assert "forbid_bullets" in rules, line


def test_a_forbidden_word_is_caught_case_insensitively():
    body = "Bottom line: we will Utilize the tool.\n\nNext: go."
    v = format_check.check(body, FOUR_RULES)
    assert [x.rule for x in v] == ["forbid_substrings"]
    assert "utilize" in v[0].detail


def test_a_length_ceiling_is_enforced():
    v = format_check.check("x" * 50, {"max_chars": 10})
    assert [x.rule for x in v] == ["max_chars"]


def test_every_violation_is_reported_at_once():
    """A writer told one broken rule at a time experiences the checker moving
    the goalposts. Report the whole set, once."""
    body = "We will utilize this.\n\n- a bullet\n\nAnd no closer."
    rules = {v.rule for v in format_check.check(body, FOUR_RULES)}
    assert rules == {
        "opening_line_prefix",
        "closing_line_prefix",
        "forbid_bullets",
        "forbid_substrings",
    }


def test_an_unknown_assertion_is_ignored_not_fatal():
    """An older seat against a newer authoring surface must check LESS, never
    block everything it cannot parse. Refusal belongs at write time, where a
    person is present to be told."""
    assert format_check.check(COMPLIANT, {"invented_rule": "nope"}) == []


def test_blank_lines_are_layout_not_content():
    """Leading and trailing blank lines must not make an opening rule fail."""
    assert format_check.check("\n\n" + COMPLIANT + "\n\n", FOUR_RULES) == []


def test_the_audit_string_carries_rule_names_only():
    """The audit row is durable and read by people who were not in the session.
    The fragment that helps the model fix its draft must not persist there."""
    body = "We will utilize this.\n\nAnd no closer."
    violations = format_check.check(body, FOUR_RULES)
    names = format_check.rule_names(violations)
    assert "utilize" not in names
    assert "We will" not in names
    assert "forbid_substrings" in names


def test_the_model_facing_string_does_carry_the_fragment():
    """Safe here and nowhere else: the model already holds the text it wrote,
    and cannot fix a line it was never shown."""
    v = format_check.check("Wrong opener.\n\nNext: go.", FOUR_RULES)
    assert "Wrong opener" in format_check.describe(v)
