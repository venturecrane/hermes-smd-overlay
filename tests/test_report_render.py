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


@pytest.mark.parametrize("source", [REPORT, DIGEST], ids=["report", "digest"])
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
