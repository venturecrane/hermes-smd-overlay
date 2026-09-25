"""The reader's own words in a reply: the text above the quoted history.

AgentMail hands the seat ``extracted_text`` (the provider already removed the
quote). Graph can: ``uniqueBody`` is exactly that. But asking the delta poll for
``uniqueBody`` is unproven on a live tenant, and a delta poll that fails stops a
firm's whole inbound, so the Graph side derives the same thing here, from the
body it already fetched, with a pure and deterministic cut.

The cut keeps every line ABOVE the first line that opens quoted history:

* a line starting with ``>`` (plain-text quoting);
* ``-----Original Message-----`` (classic Outlook);
* a long underscore rule (``________________________________``, Outlook's
  HTML reply separator after it is reduced to text);
* ``On <date>, <person> wrote:`` (Gmail, Apple Mail), including the shape where
  the sender's address wraps the ``wrote:`` onto the next line;
* a ``From:`` header line followed within a few lines by ``Sent:`` / ``Date:`` /
  ``To:`` / ``Subject:`` (Outlook's quoted header block). A lone ``From:`` line
  is not enough: the reader might write one.

Missing a quote marker is NOT a safe failure on its own: the quoted numbered
list would then read as the reader's words and name every item. So the digest
parser carries a second guard of its own (it skips any line shaped like a
digest item line, ``N. matter ...``), and this cut is the first. Cutting too
early would lose the reader's words, so the markers are the specific,
well-known ones and nothing looser.
"""

from __future__ import annotations

import re

_QUOTE_LINE = re.compile(r"^\s*>")
_ORIGINAL_MESSAGE = re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.IGNORECASE)
_RULE = re.compile(r"^\s*_{10,}\s*$")
_ON_WROTE_START = re.compile(r"^\s*on\s.+", re.IGNORECASE)
_WROTE_END = re.compile(r"wrote:\s*$", re.IGNORECASE)
_FROM_HEADER = re.compile(r"^\s*\**from:\**\s*\S", re.IGNORECASE)
_HEADER_FOLLOW = re.compile(r"^\s*\**(sent|date|to|subject|cc):\**\s*", re.IGNORECASE)
_HEADER_LOOKAHEAD = 4


def _opens_quote(lines: list[str], index: int) -> bool:
    line = lines[index]
    if _QUOTE_LINE.match(line) or _ORIGINAL_MESSAGE.match(line) or _RULE.match(line):
        return True
    if _ON_WROTE_START.match(line):
        if _WROTE_END.search(line):
            return True
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if _WROTE_END.search(following):
            return True
    if _FROM_HEADER.match(line):
        window = lines[index + 1 : index + 1 + _HEADER_LOOKAHEAD]
        if any(_HEADER_FOLLOW.match(candidate) for candidate in window):
            return True
    return False


def strip_quoted_reply(text: object) -> str:
    """``text`` above its quoted history, stripped. ``""`` for a non-string."""
    if not isinstance(text, str):
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    for index in range(len(lines)):
        if _opens_quote(lines, index):
            return "\n".join(lines[:index]).strip()
    return text.strip()


__all__ = ["strip_quoted_reply"]
