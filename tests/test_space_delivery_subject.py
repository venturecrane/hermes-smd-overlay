"""A routine's email subject: a short title line, or the fixed subject, never a cut sentence.

2026-10-19 19:17Z, captain-mark: a one-sentence routine reply went out with the
subject "The sample invite is in your inbox for October 30 and you can decline
it wh..." because the subject was the first line cut to 78 characters.
"""

from __future__ import annotations

from shared import space_delivery as sd


def test_a_short_title_line_becomes_the_subject_and_leaves_the_body() -> None:
    text = "What I can do for you two\n\nHi Laurie,\n\nHere's a quick guide.\n\nCaptain Mark"
    subject, body = sd.split_title(text)
    assert subject == "What I can do for you two"
    assert body.startswith("Hi Laurie,")
    assert "What I can do for you two" not in body


def test_FALSIFIER_a_one_sentence_reply_is_never_cut_into_the_subject() -> None:
    text = "The sample invite is in your inbox for October 30 and you can decline it whenever you like."
    subject, body = sd.split_title(text)
    assert subject == sd.FALLBACK_SUBJECT
    assert body == text
    assert "..." not in subject


def test_a_short_first_line_that_is_a_sentence_is_not_a_title() -> None:
    text = "Done, it is on the calendar.\n\nKids, every other week."
    assert sd.split_title(text) == (sd.FALLBACK_SUBJECT, text)


def test_a_greeting_is_not_a_title() -> None:
    text = "Hi Scott,\n\nIt is on the calendar now."
    assert sd.split_title(text) == (sd.FALLBACK_SUBJECT, text)


def test_a_title_with_nothing_after_it_is_the_body() -> None:
    text = "Kids is on the calendar"
    assert sd.split_title(text) == (sd.FALLBACK_SUBJECT, text)


def test_a_title_not_followed_by_a_blank_line_is_not_split() -> None:
    text = "Kids is on the calendar\nAll day Friday through Sunday."
    assert sd.split_title(text) == (sd.FALLBACK_SUBJECT, text)


def test_markdown_markers_on_the_title_are_removed() -> None:
    subject, _ = sd.split_title("## **Kids is on the calendar**\n\nAll day.")
    assert subject == "Kids is on the calendar"


def test_empty_text_gets_the_fixed_subject() -> None:
    assert sd.split_title("") == (sd.FALLBACK_SUBJECT, "")
    assert sd.subject_for("   \n") == sd.FALLBACK_SUBJECT
