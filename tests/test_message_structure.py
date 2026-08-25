"""The message structure floor (ss#2090 refiled).

Every rule here carries a falsifier, per this suite's own convention
(``test_body_key_parity.py::test_the_parity_check_catches_a_desynced_list``): a
check that cannot fail has measured nothing.

The load-bearing fixture is ``EIGHT_TWENTY_FIVE_BODY`` — the literal message the
pilot seat sent to scott@smd.services at 2026-08-25T14:01:09Z, recovered verbatim
from the AgentMail sent folder. It is here rather than paraphrased because its
exact shape is the argument: one ``---`` and nothing else, which is precisely
what ``looks_like_report`` accepts and what a reader cannot use.
"""

from __future__ import annotations

from shared import message_structure as ms
from shared import report_render

# The real thing, trimmed in the middle only (the elided rows are byte-identical
# in shape to the ones kept). Every structural property is preserved: no heading,
# no list marker, one horizontal rule.
EIGHT_TWENTY_FIVE_BODY = """Matter 2026-PI-101 has a task deadline authored for 2026-08-26, one day out, with no prior Operator raise on record. It needs attention today.

NEEDS YOU TODAY

2026-PI-101 | task-deadline | due 2026-08-26 | 1 day out
No Operator raise on record. Confirm this deadline is handled or reassign it.
ACK: ACK-YED4HY

To acknowledge this item, reply with ESCALATION_ACKNOWLEDGED. Your reply acks the items quoted in this message only; items not quoted remain open.

---

UNDER ACTIVE ESCALATION ELSEWHERE (no action required from you)

These items are being tracked on the active escalation chain. Last raise dates are from the Operator ledger.

2026-PI-106 | task-deadline | authored 2026-07-08 | 48 days overdue | last raised 2026-08-24
2026-PI-106 | court-date | authored 2026-08-26 | 1 day out | last raised 2026-08-24
2026-PI-102 | task-deadline | authored 2026-07-14 | 42 days overdue | last raised 2026-08-24
PI-2026-0001 | task-deadline | authored 2026-06-30 | 56 days overdue | last raised 2026-08-23
2026-PI-105 | task-deadline | authored 2026-07-20 | 36 days overdue | last raised 2026-08-24
2026-PI-101 | task-deadline | authored 2026-07-08 | 48 days overdue | last raised 2026-08-24
2026-PI-101 | task-deadline | authored 2026-08-12 | 13 days overdue | last raised 2026-08-24

Action needed: confirm or reassign the 2026-PI-101 task due 2026-08-26 (ACK-YED4HY), then reply to acknowledge.
"""

#: The smallest LEGAL digest. Every band but the first is omitted when empty
#: (output-format rule 9), so a quiet day is one heading and one item. If the
#: floor rejected this it would refuse correct output on the commonest day.
QUIET_DAY_BODY = """## Needs you today (1)

1. matter 2026-PI-101, task-deadline 2026-08-26 (due in 1 day) [ACK-YED4HY]
   An unverified response is treated as no response.

Reply with the ACK code above to acknowledge.
"""

CONFORMING_DIGEST = """## Needs you today (2)

1. matter 2026-PI-101, task-deadline 2026-08-26 (due in 1 day) [ACK-AAAAAA]
   Disbursement blocked until the lien payoff is confirmed.
2. matter 2026-PI-104, filing-deadline 2026-08-27 (due in 2 days) [ACK-BBBBBB]
   An unverified response is treated as no response.

## Under active escalation elsewhere (3 across 2 matters)

- matter 2026-PI-105: 2 item(s) under active escalation (last raised 2026-08-24).
- matter 2026-PI-106: 1 item(s) under active escalation (last raised 2026-08-24).

Reply with the ACK code(s) above to acknowledge.
"""


# --- the incident ------------------------------------------------------------


def test_the_2026_08_25_body_is_rejected():
    """The message that started this. Two rules catch it independently."""
    violations = ms.check(EIGHT_TWENTY_FIVE_BODY, ms.BANDED_DIGEST)
    rules = {v.rule for v in violations}
    assert "no_heading" in rules
    assert "no_list_items" in rules


def test_the_old_check_would_have_passed_that_same_body():
    """The falsifier for the whole module.

    ``looks_like_report`` is what the send path had on 2026-08-25, and it says
    yes. If this ever starts returning False the incident fixture has been
    edited into something the old code would also have caught, and the test
    above stops proving anything.
    """
    assert report_render.looks_like_report(EIGHT_TWENTY_FIVE_BODY) is True


# --- rule (a): a heading ------------------------------------------------------


def test_missing_heading_is_named():
    body = "- one\n- two\n"
    assert "no_heading" in {v.rule for v in ms.check(body, ms.BANDED_DIGEST)}


def test_heading_present_clears_that_rule():
    assert "no_heading" not in {v.rule for v in ms.check(CONFORMING_DIGEST, ms.BANDED_DIGEST)}


# --- rule (b): list items, and only where the family is a list ----------------


def test_banded_digest_without_list_items_is_named():
    body = "## Needs you today\n\nSomething happened and here is a sentence about it.\n"
    assert "no_list_items" in {v.rule for v in ms.check(body, ms.BANDED_DIGEST)}


def test_a_decision_card_is_not_required_to_carry_list_items():
    """Its fields are bold PARAGRAPH lines, not a list.

    Requiring a list here would refuse the shape the skill is supposed to emit,
    which is the failure mode of a floor that has drifted into asserting format.
    """
    body = "# Verification — J. Okafor — matter 2026-PI-101 — 2026-08-25\n\n**Decision:** staged for the attorney.\n"
    rules = {v.rule for v in ms.check(body, ms.DECISION_CARD)}
    assert "no_list_items" not in rules
    assert rules == set()


# --- rule (c): the wall -------------------------------------------------------


def test_a_single_long_paragraph_is_a_wall_even_with_a_heading_and_a_list():
    """The rule my own first draft lacked.

    "at least one heading and one list item" accepts this body, and this body is
    the 2026-08-25 failure with one line added.
    """
    body = "## Needs you today\n\n1. matter 2026-PI-101\n\n" + ("word " * 900)
    rules = {v.rule for v in ms.check(body, ms.BANDED_DIGEST)}
    assert rules == {"paragraph_wall"}


def test_the_wall_rule_can_pass():
    """Falsifier for rule (c): the same shape under the limit is clean."""
    body = "## Needs you today\n\n1. matter 2026-PI-101\n\n" + ("word " * 20)
    assert "paragraph_wall" not in {v.rule for v in ms.check(body, ms.BANDED_DIGEST)}


def test_blocks_break_a_paragraph_so_many_short_lines_are_not_a_wall():
    """Density is measured per PARAGRAPH, not per message.

    A long digest of many short list items is legible and must pass, or the
    floor would punish exactly the shape it is asking for.
    """
    body = "## Needs you today (40)\n\n" + "".join(
        f"- matter 2026-PI-1{i:02d}: one item under active escalation.\n" for i in range(40)
    )
    assert ms.check(body, ms.BANDED_DIGEST) == []


# --- legitimate output must survive -------------------------------------------


def test_a_conforming_digest_passes():
    assert ms.check(CONFORMING_DIGEST, ms.BANDED_DIGEST) == []


def test_the_quiet_day_digest_passes():
    """One heading, one item. The commonest legal digest there is."""
    assert ms.check(QUIET_DAY_BODY, ms.BANDED_DIGEST) == []


# --- binding ------------------------------------------------------------------


def test_an_unmapped_skill_binds_nothing():
    """No family means no rule. Inventing one would be the imposed default."""
    assert ms.family_for_skill("inbox-triage") is None
    assert ms.check(EIGHT_TWENTY_FIVE_BODY, ms.family_for_skill("inbox-triage")) == []


def test_the_two_digest_skills_share_one_family():
    assert ms.family_for_skill("deadline-miss-escalator") == ms.BANDED_DIGEST
    assert ms.family_for_skill("daily-needs-you-digest") == ms.BANDED_DIGEST


def test_skills_that_never_send_are_absent_from_the_map():
    for skill in ("inbox-triage", "connector-auth-check", "workspace"):
        assert ms.family_for_skill(skill) is None, skill


def test_an_empty_or_missing_body_binds_nothing():
    """Uninspectable bodies are the content floor's problem, not this one."""
    assert ms.check(None, ms.BANDED_DIGEST) == []
    assert ms.check("   ", ms.BANDED_DIGEST) == []


# --- the audit field ----------------------------------------------------------


def test_rule_names_is_a_comma_joined_string_not_a_list():
    """Matches ``format_check.rule_names``'s shape.

    The audit row's ``rules`` field is a string at the violation-shaped call site
    and a list at the broken-control one. This is a violation.
    """
    names = ms.rule_names(ms.check(EIGHT_TWENTY_FIVE_BODY, ms.BANDED_DIGEST))
    assert isinstance(names, str)
    assert "no_heading" in names and "no_list_items" in names


def test_describe_names_every_broken_rule_without_quoting_the_body():
    """The operator message helps; the audit detail must not carry the text."""
    described = ms.describe(ms.check(EIGHT_TWENTY_FIVE_BODY, ms.BANDED_DIGEST))
    assert described
    assert "2026-PI-101" not in described
