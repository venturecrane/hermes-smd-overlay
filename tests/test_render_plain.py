"""render_plain (WS-RENDER): the text/plain half of a report send.

Same grammar as the html renderer (block_kind — one parser, no drift), and
the same purity contract: strictly marker-subtractive. The round-trip test
here is the plain twin of the html purity test — if render_plain ever adds a
token the source did not carry, the inject-after-the-gates safety argument
collapses and its call site must move.
"""

from __future__ import annotations

import re

from shared import report_render

REPORT = (
    "## Needs you today (2)\n"
    "\n"
    "1. matter 2026-PI-101, **task-deadline** 2026-08-29 (overdue by 2 days) [ACK-AAAAAA]\n"
    "   the task is marked CRITICAL in Smokeball\n"
    "- matter 2026-PI-102: 1 routine confirmation. [ACK-BBBBBB]\n"
    "\n"
    "---\n"
    "\n"
    "> quoted line\n"
    "\n"
    "Reply with the `ACK` code(s) above to acknowledge.\n"
)


def _tokens(text: str) -> list[str]:
    """Marker-normalized token stream: what a reader reads, order preserved."""
    stripped = re.sub(r"[#*`>\-]|(?<!\S)\d{1,3}\.(?=\s)", " ", text)
    return stripped.split()


def test_plain_render_is_marker_subtractive_token_for_token():
    rendered = report_render.render_plain(REPORT)
    assert _tokens(rendered) == _tokens(REPORT)


def test_headings_lose_hashes_and_keep_their_text():
    rendered = report_render.render_plain(REPORT)
    assert "## " not in rendered
    assert "Needs you today (2)" in rendered


def test_list_markers_survive_verbatim():
    rendered = report_render.render_plain(REPORT)
    assert "1. matter 2026-PI-101" in rendered
    assert "- matter 2026-PI-102: 1 routine confirmation. [ACK-BBBBBB]" in rendered


def test_inline_bold_and_code_lose_their_markers():
    rendered = report_render.render_plain(REPORT)
    assert "**" not in rendered
    assert "`" not in rendered
    assert "task-deadline" in rendered
    assert "ACK code(s)" in rendered


def test_continuation_lines_keep_their_indent():
    rendered = report_render.render_plain(REPORT)
    assert "\n   the task is marked CRITICAL in Smokeball\n" in rendered


def test_rule_becomes_a_blank_line_and_quote_indents():
    rendered = report_render.render_plain(REPORT)
    assert "---" not in rendered
    assert "\n  quoted line\n" in rendered


def test_prose_without_markers_is_untouched():
    prose = "Hi Ana, following up on the verification: it is still open.\n"
    assert report_render.render_plain(prose) == prose
