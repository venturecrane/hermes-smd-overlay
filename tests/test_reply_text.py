"""The reader's own words above a reply's quoted history (``shared.reply_text``).

The Graph mail path derives ``reply_text`` with this cut instead of asking the
delta poll for ``uniqueBody``. Pinned with the body shapes Outlook and Gmail
actually produce once ``msgraph_client.html_to_text`` has reduced them.
"""

from __future__ import annotations

import pytest

from shared import msgraph_client
from shared.reply_text import strip_quoted_reply

DIGEST = (
    "1. matter 2026-PI-101, due 2026-09-20\n"
    "2. matter 2026-PI-102, due 2026-09-21\n"
    "3. matter 2026-PI-103, due 2026-09-22"
)


def test_outlook_header_block_html():
    body = msgraph_client.html_to_text(
        "<div>got it on 1</div>"
        '<div id="divRplyFwdMsg"><b>From:</b> Operator &lt;ops@firm.example&gt;<br>'
        "<b>Sent:</b> Thursday, September 25, 2026 7:00 AM<br>"
        "<b>To:</b> Dana &lt;dana@firm.example&gt;<br>"
        "<b>Subject:</b> [Deadlines] 3 need you</div>"
        "<div>1. matter 2026-PI-101</div>"
    )
    assert strip_quoted_reply(body) == "got it on 1"


def test_outlook_plain_text_header_block():
    body = (
        "Done with 2.\n\n"
        "From: Operator <ops@firm.example>\n"
        "Sent: Thursday, September 25, 2026 7:00 AM\n"
        "To: Dana <dana@firm.example>\n"
        "Subject: [Deadlines] 3 need you\n\n" + DIGEST
    )
    assert strip_quoted_reply(body) == "Done with 2."


def test_outlook_underscore_rule():
    body = "1 and 3\n________________________________\nFrom: Operator\nSent: Thu\n" + DIGEST
    assert strip_quoted_reply(body) == "1 and 3"


def test_classic_original_message_separator():
    body = "all\n\n-----Original Message-----\nFrom: Operator\n" + DIGEST
    assert strip_quoted_reply(body) == "all"


def test_gmail_on_wrote_one_line():
    body = (
        "got 3\n\nOn Thu, Sep 25, 2026 at 7:00 AM Operator <ops@firm.example> wrote:\n"
        "> " + DIGEST.replace("\n", "\n> ")
    )
    assert strip_quoted_reply(body) == "got 3"


def test_gmail_on_wrote_wrapped_onto_two_lines():
    body = (
        "got 3\n\nOn Thu, Sep 25, 2026 at 7:00 AM Operator <\nops@firm.example> wrote:\n" + DIGEST
    )
    assert strip_quoted_reply(body) == "got 3"


def test_gmail_html_blockquote():
    body = msgraph_client.html_to_text(
        '<div dir="ltr">thanks</div><br><div class="gmail_quote">'
        '<div dir="ltr" class="gmail_attr">On Thu, Sep 25, 2026 at 7:00 AM Operator '
        "&lt;ops@firm.example&gt; wrote:<br></div>"
        '<blockquote class="gmail_quote"><div>1. matter 2026-PI-101</div></blockquote></div>'
    )
    assert strip_quoted_reply(body) == "thanks"


def test_angle_bracket_quoting():
    assert strip_quoted_reply("1\n> 1. matter A\n> 2. matter B") == "1"


def test_no_quote_keeps_everything():
    assert strip_quoted_reply("  got it on 1\nDana  ") == "got it on 1\nDana"


@pytest.mark.parametrize(
    "text",
    [
        "From: the front desk, got 1",  # a lone From: line is the reader's own
        "On it, 2 is done",  # "On ..." without "wrote:"
        "1 and 2 -- all good",
    ],
)
def test_near_misses_are_not_cut(text):
    assert strip_quoted_reply(text) == text


def test_non_string_is_empty():
    assert strip_quoted_reply(None) == ""
    assert strip_quoted_reply(["1"]) == ""


def test_quote_at_the_top_leaves_nothing():
    assert strip_quoted_reply("> 1. matter A\nsomething below") == ""
