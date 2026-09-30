"""The output checklist scanner (``shared/output_checklist``), pure.

The standard every case here answers: would a good paralegal have sent, filed or
handed this over as is? The wired half, where each surface meets the scanner,
is ``tests/test_output_checklist_wired.py``.
"""

from __future__ import annotations

import pytest

from shared import output_checklist as oc

# The dashes are spelled by code point so this file carries none itself.
_EM = chr(0x2014)
_EN = chr(0x2013)


def _rules(body: str, surface: str = oc.STAFF_SEND) -> list[str]:
    return sorted(v.rule for v in oc.check(body, surface))


# ---------------------------------------------------------------------------
# What a correct staff email looks like passes
# ---------------------------------------------------------------------------


def test_a_digest_with_headings_and_list_markers_passes_on_the_wire():
    """The wire text is render_plain's: headings and bold are gone before a
    reader sees them, and a plain-text list IS its markers."""
    digest = (
        "## Needs you today\n"
        "- **2026-PI-101** Garcia: hearing Oct 7 at 9:30 a.m., Dept 31\n"
        "- 2026-PI-104 Nguyen: lien payoff of $1,250.00 is due\n"
        "\n"
        "## This week\n"
        "1. Call the adjuster\n"
        "2. Send the records request\n"
    )
    assert _rules(digest) == []


def test_the_act_tag_is_a_thing_a_person_replies_to_and_passes():
    assert _rules("Reply yes to delete it. [act 1a2b3c4d]") == []


@pytest.mark.parametrize(
    "body",
    [
        "At 9:00 a.m. in Dept 31.",
        "At 9:00 AM in Dept 31.",
        "From 2:30-4:00 p.m.",
        "From 2:30 to 4 p.m.",
        "Dept 31",
        "2026-PI-101 owes $1,250.00.",
        "Smith Depo. 22:14-23:2 covers it.",
        "See Tr. 5:3 for the answer.",
        "Smith Depo. 22:14-23:20 covers it.",
        "Tr. 105:3-106:12 covers it.",
        "The MEDI-CAL lien is open.",
        "Sections 1" + _EN + "5 apply.",
        "christa <christa@firm.example> asked for it.",
        "PLAINTIFF filed the motion.",
        "Garcia v. SMITH TRUCKING is set for trial.",
        "The SROG-1 responses are due.",
        "Noon works, and so does 12:00 noon.",
    ],
)
def test_ordinary_firm_writing_passes(body: str):
    assert _rules(body) == []


# ---------------------------------------------------------------------------
# What a paralegal would correct fails, one violation per rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Matter a1b2c3d4-1111-2222-3333-444455556666 is late.",
        "Ref 01J9Z8Y7X6W5V4T3S2R1Q0P9N8 is late.",
        "Reply ACK-7Q3M2K to confirm.",
        "Digest " + "ab" * 16 + " sent.",
    ],
)
def test_an_internal_id_fails(body: str):
    assert _rules(body) == ["internal_id"]


@pytest.mark.parametrize(
    ("body", "rule"),
    [
        ("The hearing is at 16:30.", "bare_clock"),
        ("The hearing is at 9:00:00 today.", "bare_clock"),
        ("The hearing is at 2026-10-07T16:30:00Z.", "iso_timestamp"),
        ("The hearing is at 16:30 UTC.", "utc_time"),
        ("The hearing is at 16:30Z.", "utc_time"),
        # Same shape as a page:line range, no cite word: a UTC hearing window.
        ("Hearing runs 16:30-17:00.", "bare_clock"),
    ],
)
def test_a_time_the_firm_would_not_write_fails(body: str, rule: str):
    assert _rules(body) == [rule]


def test_one_iso_stamp_is_one_violation_not_three():
    assert _rules("Set for 2026-10-07T16:30:00.000-07:00 in Dept 31.") == ["iso_timestamp"]


def test_bold_in_prose_fails_but_the_same_bold_in_a_report_passes():
    assert _rules("This is **urgent** for the Garcia file.") == ["emphasis_marks"]
    assert _rules("## Garcia\n- This is **needed** for the file.") == []


@pytest.mark.parametrize(
    ("body", "rule"),
    [
        ("Call the client today!", "exclamation"),
        ("Two items " + _EM + " both today.", "em_dash"),
        ("Two items " + _EN + " both today.", "em_dash"),
        ("Bring the <b>binder</b>.", "html_tag"),
        ("Use `render` for it.", "emphasis_marks"),
        ("| Matter | Date |\n| 101 | Oct 7 |", "pipe_table"),
    ],
)
def test_markup_and_punctuation_a_paralegal_would_remove_fail(body: str, rule: str):
    assert _rules(body) == [rule]


def test_the_fragment_is_quoted_in_the_detail_and_the_remedy_follows_the_reason():
    (violation,) = oc.check("The hearing is at 16:30.", oc.STAFF_SEND)
    assert "'16:30'" in violation.detail
    reason, _, remedy = violation.detail.partition(". ")
    assert "16:30" in reason and "a.m." in remedy


# ---------------------------------------------------------------------------
# Report-only rules
# ---------------------------------------------------------------------------


def test_twenty_one_lines_is_a_report_only_violation():
    body = "\n".join(f"Line {i} of the update." for i in range(21))
    assert _rules(body) == ["max_lines"]
    assert oc.refusing(oc.check(body, oc.STAFF_SEND)) == []
    assert "max_lines" in oc.REPORT_ONLY_RULES


def test_twenty_lines_is_within_the_ceiling():
    body = "\n\n".join(f"Line {i} of the update." for i in range(20))
    assert _rules(body) == []


def test_a_caption_excuses_only_the_capitals_inside_it():
    assert _rules("URGENT re Garcia v. Smith Trucking deadline") == ["caps_emphasis"]
    assert _rules("Garcia v. SMITH TRUCKING deadline moved.") == []


def test_capitals_touching_a_hyphen_are_a_spelling():
    assert _rules("The MEDI-CAL lien and the SROG-1 set are open.") == []


def test_urgent_in_capitals_is_reported_and_plaintiff_is_not():
    assert _rules("URGENT: the Garcia file needs you.") == ["caps_emphasis"]
    assert oc.refusing(oc.check("URGENT: the Garcia file needs you.", oc.STAFF_SEND)) == []
    assert _rules("PLAINTIFF: the Garcia file needs you.") == []


# ---------------------------------------------------------------------------
# The file-note surface
# ---------------------------------------------------------------------------


def test_a_memo_with_a_quote_and_bold_is_not_refused_here():
    """The connector normalizes headings, emphasis and quotes after the hook;
    refusing them here would refuse every log-memo skill on day one."""
    assert _rules("> **What:** the motion was served.\n## Next\nNothing to do.", oc.MEMO) == []


def test_a_memo_with_a_table_fails_and_is_told_to_file_a_document():
    (violation,) = oc.check("| Exhibit | Page |\n| 1 | 4 |", oc.MEMO)
    assert violation.rule == "pipe_table"
    assert "render_docx_draft" in violation.detail


def test_a_memo_carries_the_time_rules():
    assert _rules("Hearing moved to 16:30.", oc.MEMO) == ["bare_clock"]


def test_a_sixteen_line_memo_is_reported_with_the_document_remedy():
    body = "\n".join(f"Line {i}." for i in range(16))
    (violation,) = oc.check(body, oc.MEMO)
    assert violation.rule == "max_lines"
    assert "render_docx_draft" in violation.detail


# ---------------------------------------------------------------------------
# The document surface and the external reply surface
# ---------------------------------------------------------------------------


def test_a_docx_caption_table_and_a_deposition_cite_pass():
    markdown = (
        "| SUPERIOR COURT OF CALIFORNIA | Case No. 24STCV01234 |\n"
        "|---|---|\n"
        "# SEPARATE STATEMENT\n"
        "**Fact 1.** Smith testified at 22:14-23:2!\n"
    )
    assert _rules(markdown, oc.DOCX) == []


def test_a_docx_with_an_internal_id_fails():
    markdown = "| Caption | x |\nMatter a1b2c3d4-1111-2222-3333-444455556666\n"
    assert _rules(markdown, oc.DOCX) == ["internal_id"]


def test_an_external_reply_checks_ids_and_stamps_only():
    assert _rules("Thanks, we will help! See you at 16:30.", oc.EXTERNAL_REPLY) == []
    assert _rules("Stamp 2026-10-07T16:30:00Z.", oc.EXTERNAL_REPLY) == ["iso_timestamp"]


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def test_rule_names_carry_no_fragment():
    violations = oc.check("Matter a1b2c3d4-1111-2222-3333-444455556666 at 16:30!", oc.STAFF_SEND)
    names = oc.rule_names(violations)
    assert names == "bare_clock,exclamation,internal_id"
    assert "16:30" not in names and "a1b2" not in names


def test_an_unknown_surface_checks_nothing():
    assert oc.check("At 16:30!", "no-such-surface") == []


def test_the_counter_is_per_session_and_per_message():
    counter = oc.RefusalCounter()
    key = oc.fingerprint("The hearing is at 16:30.")
    assert [counter.bump("s1", key) for _ in range(3)] == [1, 2, 3]
    assert counter.bump("s2", key) == 1
    assert counter.bump("s1", oc.fingerprint("Another message at 17:00.")) == 1


def test_a_full_counter_evicts_only_its_oldest_slot():
    """Clearing the whole table under load would reset every session's
    near-exhausted count and reopen the loop."""
    counter = oc.RefusalCounter(max_entries=3)
    counter.bump("s1", "a")
    counter.bump("s2", "b")
    counter.bump("s2", "b")
    counter.bump("s3", "c")
    counter.bump("s3", "c")
    assert counter.bump("s4", "d") == 1
    assert counter.count("s1", "a") == 0
    assert counter.count("s2", "b") == 2
    assert counter.count("s3", "c") == 2


def test_the_known_caps_list_holds_no_word_shorter_than_the_floor():
    assert all(len(word) >= 4 and word.isupper() for word in oc.KNOWN_CAPS)


# ---------------------------------------------------------------------------
# Machine-read marker lines on a file note (MEMO_MACHINE_LINES, debt)
# ---------------------------------------------------------------------------

_G = "a1b2c3d4-1111-2222-3333-444455556666"
_G2 = "b1b2c3d4-1111-2222-3333-444455556666"


@pytest.mark.parametrize(
    "line",
    [
        f"fileId {_G} recorded",
        f"fileId {_G} recorded.",
        f"op-mmou:{_G}:638609288928990639",
        f"Package job: job-42; covered document ids: {_G}, {_G2}",
        "Package job: job-42; covered document ids: covered set unrecorded",
    ],
)
def test_a_whole_marker_line_passes_on_a_memo(line):
    body = f"[Operator] Service confirmation as of Oct 7\nService confirmed.\n{line}"
    assert _rules(body, oc.MEMO) == []


def test_the_same_id_inside_prose_on_a_memo_fails():
    assert _rules(f"Service confirmed, fileId {_G} recorded.", oc.MEMO) == ["internal_id"]
    assert _rules(f"Note: op-mmou:{_G}:638609288928990639", oc.MEMO) == ["internal_id"]


def test_a_marker_line_on_a_staff_send_fails():
    assert _rules(f"fileId {_G} recorded", oc.STAFF_SEND) == ["internal_id"]
    assert _rules(f"op-mmou:{_G}:638609288928990639", oc.EXTERNAL_REPLY) == ["internal_id"]


def test_a_facts_digest_line_passes_on_a_memo_and_fails_elsewhere():
    body = "[Operator] Deadlines as of Oct 7\nAnswer due Oct 9.\nfacts 0123456789ab"
    assert _rules(body, oc.MEMO) == []
    assert _rules("facts 0123456789ab", oc.STAFF_SEND) == ["internal_id"]


def test_a_facts_digest_inside_prose_on_a_memo_fails():
    assert _rules("facts 0123456789ab and more", oc.MEMO) == ["internal_id"]


# ---------------------------------------------------------------------------
# Short entry ids a routine cites after a naming word
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", [oc.MEMO, oc.STAFF_SEND])
def test_a_calendar_entry_cited_by_short_id_fails_with_the_entry_remedy(surface):
    violations = oc.check("Oct 6 at 9:30 a.m., Dept 3 (event cef69a47)", surface)
    assert [v.rule for v in violations] == ["internal_id"]
    assert "the internal id 'cef69a47'" in violations[0].detail
    assert "'event cef69a47'" not in violations[0].detail
    assert "calendar entry by its subject and date" in violations[0].detail
    assert "never by id" in violations[0].detail


@pytest.mark.parametrize(
    "body",
    [
        "It was reported in task 98570c78.",
        "Saved as file 3061ca2e.",
        "Task 223145b9 is open.",
        "See document: a1b2c3d4e5f6.",
        "Memo #0a1b2c3d was updated.",
    ],
)
def test_a_short_entry_id_after_a_naming_word_fails(body: str):
    assert _rules(body) == ["internal_id"]
    assert _rules(body, oc.MEMO) == ["internal_id"]


@pytest.mark.parametrize(
    "body",
    [
        "Case No. 25STCV31844 is set for trial.",
        "Bates 000123 through 000200.",
        "Hold the deadbeef release.",
        "File 20250101 is the firm's number.",
        "matter 2026-PI-101 is open.",
        "Job 4 of 5 is done.",
        "Task: call the client at 10",
        "See doc 2.",
        "Use id 12.",
        "The task list is short.",
    ],
)
def test_a_number_or_word_that_is_not_an_entry_id_passes(body: str):
    assert _rules(body) == []


def test_a_matter_guid_after_the_word_matter_is_one_hit_with_the_matter_remedy():
    violations = oc.check(f"Matter {_G} is late.", oc.STAFF_SEND)
    assert [v.rule for v in violations] == ["internal_id"]
    assert "firm's own matter number" in violations[0].detail
