"""Tests for shared.report_render — markdown -> html for operator report emails.

The two invariants the send-path safety argument rests on get the most weight
here: PURITY (the html carries no content the source text did not, which is why
it is safe to inject after the trust gates have scanned the text) and
ESCAPE-BY-DEFAULT (model-authored markup never reaches the reader as markup).
"""

from __future__ import annotations

import re

import pytest

from shared.report_render import html_text_content, render_markdown

# A real report shape: the 2026-07-15 "[Deadlines] 2 need you" body, with the
# matter content replaced by synthetic stand-ins. Exercises every block the
# report skills actually emit — h2, lead-in paragraph, ordered items with an
# indented detail line, a zero-count section, an hr, and a bold footer.
REPORT = """## Needs you today (2)

Ranked by what the record says, most consequential first.

1. matter ALPHA-1, records outstanding, due 2026-07-11 (overdue 4 days) [ACK-6WS08D]
   Records were requested 2026-06-20 via the vendor and have not yet arrived.

2. matter BRAVO-2, offer expiration confirm, due 2026-07-25 (10 days out) [ACK-FH0M72]
   Attorney must decide: accept, counter, or let expire. Operator does not decide this.

## Admin confirms (0 across 0 matters)

No routine confirmations this run.

---

Reply with the **ACK code** to acknowledge. This is an internal alert.
"""

# The digest shape: h1, bullet bands, a blockquote training note.
DIGEST = """# Needs a person today - 2026-07-15 - 3 items across 2 open matters

## Deadlines near (1)

- matter ALPHA-1 - response due 2026-07-27 (12 days) - owns: deadline-and-sol-tracker

## Notes for a paralegal (training)

> An unverified response is treated as no response. Bring the attorney in
> before the window closes.
"""


# ss#2489 — the shape that broke live. An introduction the A&P Operator sent on
# 2026-08-20: a bullet list, then PARAGRAPHS, then a second bullet list. Neither
# REPORT nor DIGEST above has a paragraph after a list item, which is exactly
# why the bug reached a paying client's inbox with nine green tests behind it.
MIXED = """I'm Operator, the AI Case Coordinator. Here's what I can see right now.

CONNECTIONS (observed this turn)

- Smokeball: authenticated, production, US region.
- Email: connected.

MATTERS 577 open matters as of this read.

VOICE The firm has not established a staff voice yet.

HOW I WORK

- Messages to colleagues: I send on my own.
- Messages outside the firm: a person reviews the draft.

What can I help with?
"""


def _normalize(text: str) -> list[str]:
    """Reader-visible tokens, with markdown MARKERS dropped.

    Markers are presentation, not content: `##`, `**`, `` ` ``, `>`, the `---`
    rule, and a list item's literal `1.` all become structure in the html and
    carry no text of their own. Everything else must survive the round trip.
    """
    stripped = re.sub(r"^\s*(?:-{3,}|_{3,}|\*{3,})\s*$", " ", text, flags=re.M)
    stripped = re.sub(r"^\s*\d{1,3}\.\s|^\s*[-*]\s", " ", stripped, flags=re.M)
    stripped = re.sub(r"[*`#>]", " ", stripped)
    return stripped.split()


@pytest.mark.parametrize("source", [REPORT, DIGEST, MIXED], ids=["report", "digest", "mixed"])
def test_purity_html_carries_exactly_the_source_text(source: str) -> None:
    """PURITY INVARIANT. The rendered html's reader-visible text equals the
    source text, token for token.

    This is the load-bearing test. The send path injects html AFTER the
    fabrication scan, the content floor, and the taint gate have evaluated the
    text — safe ONLY because the html adds no content those gates did not see.
    If this fails, that argument is void: stop and re-derive the injection point,
    do not relax the test.
    """
    assert _normalize(html_text_content(render_markdown(source))) == _normalize(source)


def test_model_authored_markup_is_escaped_not_passed_through() -> None:
    """ESCAPE-BY-DEFAULT. The renderer emits only tags it chose."""
    hostile = "## Header\n\nA <script>alert(1)</script> and <b>raw</b> & an ampersand.\n"
    out = render_markdown(hostile)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<b>raw</b>" not in out
    assert "&lt;b&gt;raw&lt;/b&gt;" in out
    # The text still reads correctly to a human once unescaped.
    assert "<script>alert(1)</script>" in html_text_content(out)


def test_headings_lists_and_detail_lines_render_as_structure() -> None:
    out = render_markdown(REPORT)
    assert "<h2 " in out and ">Needs you today (2)</h2>" in out
    assert "<ol " in out and out.count("<li ") == 2
    # The indented "why it is consequential" line is the item's detail div, not
    # a stray paragraph that floats out of the list.
    assert "Records were requested 2026-06-20" in out
    assert out.index("Records were requested") < out.index("</li>")
    assert "<hr " in out
    assert "<strong " in out and ">ACK code</strong>" in out


def test_h1_bullets_and_blockquote_render() -> None:
    out = render_markdown(DIGEST)
    assert "<h1 " in out
    assert "<ul " in out and "<li " in out
    assert "<blockquote " in out


def test_inline_code_renders() -> None:
    out = render_markdown("Reply with `ESCALATION_ACKNOWLEDGED` to ack.\n")
    assert "<code " in out and ">ESCALATION_ACKNOWLEDGED</code>" in out


def test_no_external_assets_or_style_blocks() -> None:
    """Inline styles only, no network fetch. Gmail strips <style> in several
    clients, and any external URL in a report body is an exfiltration surface —
    the renderer emits no link, image, or remote asset by construction."""
    for source in (REPORT, DIGEST):
        out = render_markdown(source)
        assert "<style" not in out
        assert "<img" not in out
        assert "<a " not in out
        assert "http://" not in out and "https://" not in out
        assert "url(" not in out


def test_a_model_authored_link_is_inert_text_not_an_anchor() -> None:
    """Reports carry no links. A markdown link stays visible as its literal
    text — never promoted to a clickable anchor the reader could be walked into."""
    out = render_markdown("See [the portal](https://evil.example.com/x) for detail.\n")
    assert "<a " not in out
    assert "href" not in out


def test_empty_and_whitespace_input_do_not_crash() -> None:
    for source in ("", "\n", "   \n\n  \n"):
        out = render_markdown(source)
        assert out.startswith("<div ") and out.endswith("</div>")
        assert html_text_content(out).strip() == ""


# ---------------------------------------------------------------------------
# ss#2489 — list nesting. A paragraph ENDS a list.
#
# The live failure: an Operator introduction opened a bullet list at its first
# section, and every following paragraph plus a second bullet group rendered
# INSIDE that one <ul>. Nine tests passed over it because no corpus here mixed
# a list with a following paragraph.
# ---------------------------------------------------------------------------


def _inside_first_list(html: str) -> str:
    """The span between the first list open and its matching close."""
    start = min(
        (i for i in (html.find("<ul"), html.find("<ol")) if i != -1),
        default=-1,
    )
    if start == -1:
        return ""
    end = min(
        (i for i in (html.find("</ul>"), html.find("</ol>")) if i != -1),
        default=len(html),
    )
    return html[start:end]


def test_a_paragraph_after_a_list_item_closes_the_list() -> None:
    html = render_markdown(MIXED)
    assert "<p " not in _inside_first_list(html)


def test_list_tags_are_balanced() -> None:
    """<li> without a matching </li> is what the previous sniff-based close
    produced: it read the last emitted string, which is </p> after a paragraph,
    and closed an item that was never open."""
    html = render_markdown(MIXED)
    assert html.count("<li") == html.count("</li>")
    assert html.count("<ul") == html.count("</ul>")
    assert html.count("<ol") == html.count("</ol>")


def test_a_second_bullet_group_opens_its_own_list() -> None:
    """Two bullet groups separated by paragraphs are two lists, not one list
    with prose wedged into it."""
    html = render_markdown(MIXED)
    assert html.count("<ul") == 2


def test_a_blank_line_between_items_keeps_one_list() -> None:
    """The falsifier for the three tests above: a fix that closed the list too
    eagerly would split this into two lists, and every report skill's output
    would grow spurious list breaks."""
    html = render_markdown("- one\n\n- two\n\n- three\n")
    assert html.count("<ul") == 1
    assert html.count("<li") == 3


def test_an_ordered_list_followed_by_prose_closes_too() -> None:
    """The report skills emit ordered lists; the bug is not bullet-specific."""
    html = render_markdown("1. first\n2. second\n\nClosing thought.\n")
    assert "<p " not in _inside_first_list(html)
    assert html.count("<ol") == html.count("</ol>") == 1


def test_a_detail_line_still_attaches_to_its_item() -> None:
    """The indented detail line under a list item is the shape every report
    skill uses, and tightening the continuation rule must not break it."""
    html = render_markdown("1. matter ALPHA-1, records outstanding\n   Requested 2026-06-20.\n")
    assert "Requested 2026-06-20." in html_text_content(html)
    assert html.count("<li") == html.count("</li>") == 1
