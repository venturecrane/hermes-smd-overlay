"""Markdown -> HTML for operator report emails.

The report skills author markdown (``## Needs you today (2)``, numbered items,
``**bold**``) per each skill's ``references/output-format.md``. That markdown was
only ever delivered in the AgentMail ``text`` field, so a mail reader showed the
SOURCE: literal hash marks, literal asterisks, a list that is only a list because
the model typed the digits. This module renders that markdown into the ``html``
half of a multipart send so a header is a header.

**Live-proven 2026-07-16**: AgentMail accepts ``html`` beside ``text``; the part
survives SES and Gmail's sanitizer with inline styles intact (verify ledger
vfy_01KXMVRX1C2NAMYKH6JSQWTJEV). Inline styles only — Gmail strips ``<style>``
blocks in several clients. No external assets: no fonts, no images, no CSS URLs.

TWO INVARIANTS GOVERN THIS MODULE. Both are enforced by tests, and the send-path
safety argument rests on them:

1. **Purity.** The transform is presentational and NOTHING else. The rendered
   html carries no token of content the source text did not. This is what makes
   it safe to inject AFTER the trust gates have scanned the text: the fabrication
   scan, the content floor, and the taint gate all evaluated the same content the
   reader ends up seeing. ``html_text_content()`` + the purity test hold the line.

2. **Escape-by-default.** Every span of model-authored text is escaped before any
   markup is emitted. The renderer emits only tags IT chose. A model that writes
   ``<script>`` into a report gets ``&lt;script&gt;`` in the html — raw markup
   never passes through. This is a closed allowlist, not a sanitizer blocklist.

Deliberately NOT here: link rendering (a model-authored href is an exfiltration
surface and reports carry no links), images, tables, and content-aware styling
(e.g. spotting ``[ACK-XXXXXX]`` and boxing it). Generic markdown only — that is
what makes one renderer serve every report skill's output-format at once.
"""

from __future__ import annotations

import html as html_lib
import re

# --- style tokens ----------------------------------------------------------
# Inline-only, system fonts, no external assets. Tuned against the live Gmail
# probe: these survived the sanitizer verbatim.

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,monospace"

_S_WRAP = f"font-family:{_FONT};font-size:15px;line-height:1.55;color:#1a1a1a;max-width:640px;"
_S_H1 = "font-size:19px;font-weight:600;margin:0 0 10px 0;color:#111;"
_S_H2 = "font-size:17px;font-weight:600;margin:22px 0 6px 0;color:#111;"
_S_H3 = "font-size:15px;font-weight:600;margin:18px 0 6px 0;color:#333;"
_S_P = "margin:0 0 12px 0;"
_S_LIST = "margin:0 0 16px 0;padding-left:22px;"
_S_LI = "margin-bottom:12px;"
_S_CONT = "color:#555;margin-top:3px;"
_S_QUOTE = "margin:0 0 14px 0;padding:2px 0 2px 12px;border-left:3px solid #e0e0e0;color:#555;"
_S_HR = "border:0;border-top:1px solid #e3e3e3;margin:20px 0 14px 0;"
_S_CODE = (
    f"background:#f2f2f2;padding:1px 5px;border-radius:3px;font-family:{_MONO};font-size:13px;"
)

_HEADING_STYLES = {1: _S_H1, 2: _S_H2, 3: _S_H3, 4: _S_H3}

# --- block grammar ---------------------------------------------------------

_RE_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_RE_OL_ITEM = re.compile(r"^(\d{1,3})\.\s+(.*)$")
_RE_UL_ITEM = re.compile(r"^[-*]\s+(.*)$")
_RE_QUOTE = re.compile(r"^>\s?(.*)$")
_RE_HR = re.compile(r"^\s*(?:-{3,}|_{3,}|\*{3,})\s*$")
_RE_CONT = re.compile(r"^\s{2,}(\S.*)$")

# Inline: bold and code only. Applied to ALREADY-ESCAPED text — escaping touches
# `&<>` and leaves `*` and backticks alone, so the markers still match.
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_RE_CODE = re.compile(r"`([^`]+)`")

# Any tag the renderer itself emits; used by html_text_content() to invert.
_RE_TAG = re.compile(r"<[^>]+>")


def _inline(raw: str) -> str:
    """Escape a span of model text, then apply the inline markup subset."""
    out = html_lib.escape(raw, quote=False)
    out = _RE_BOLD.sub(r'<strong style="font-weight:600;">\1</strong>', out)
    out = _RE_CODE.sub(rf'<code style="{_S_CODE}">\1</code>', out)
    return out


class _Doc:
    """Accumulates rendered blocks and owns list/item open-close bookkeeping.

    The open state of the current list ITEM is a FLAG, not something inferred
    from the last emitted string (ss#2489). The previous version asked
    ``parts[-1].endswith("</li>")``, which is false both for an item that is
    genuinely open and for a paragraph emitted while a list happened to be
    open — so a document mixing the two closed items that were never opened and
    emitted more ``</li>`` than ``<li>``. A flag cannot disagree with itself.

    ``close_list`` closes the open item first, so no caller has to remember the
    order. Every exit from a list goes through one door.
    """

    def __init__(self) -> None:
        self.parts: list[str] = []
        self._list: str | None = None  # "ol" | "ul" | None
        self._item_open = False

    def close_item(self) -> None:
        if self._item_open:
            self.parts.append("</li>")
            self._item_open = False

    def add_item(self, chunk: str) -> None:
        """Emit one list item, closing the previous one first."""
        self.close_item()
        self.parts.append(chunk)
        self._item_open = True

    def close_list(self) -> None:
        self.close_item()
        if self._list:
            self.parts.append(f"</{self._list}>")
            self._list = None

    def open_list(self, kind: str) -> None:
        if self._list == kind:
            return
        self.close_list()
        self.parts.append(f'<{kind} style="{_S_LIST}">')
        self._list = kind

    def add(self, chunk: str) -> None:
        self.parts.append(chunk)

    def in_list(self) -> bool:
        return self._list is not None

    def item_open(self) -> bool:
        return self._item_open


def _flush_paragraph(doc: _Doc, buf: list[str]) -> None:
    """Emit the buffered paragraph, ending any open list first.

    ss#2489 — a paragraph ENDS a list. ``<p>`` is not valid inside ``<ul>``/
    ``<ol>``, and a renderer that leaves it there produces a list that swallows
    every following block. Observed live on hermes-ashton-price 2026-08-20: an
    introduction opened a bullet list at its first section, and the remaining
    three sections plus a second bullet group all rendered INSIDE that one list.

    Closing here rather than at each call site is deliberate. The three block
    handlers that already call ``close_list`` had to remember to; a caller that
    forgot produced exactly this bug. Now the invariant lives with the emitter.
    """
    if not buf:
        return
    doc.close_list()
    doc.add(f'<p style="{_S_P}">{_inline(" ".join(buf))}</p>')
    buf.clear()


def _render_line(doc: _Doc, line: str, para: list[str]) -> None:
    """Render one line into ``doc``. ``para`` is the open paragraph buffer."""
    if _RE_HR.match(line):
        _flush_paragraph(doc, para)
        doc.close_list()
        doc.add(f'<hr style="{_S_HR}">')
        return

    heading = _RE_HEADING.match(line)
    if heading:
        _flush_paragraph(doc, para)
        doc.close_list()
        level = len(heading.group(1))
        tag = f"h{min(level, 3)}"
        doc.add(f'<{tag} style="{_HEADING_STYLES[level]}">{_inline(heading.group(2))}</{tag}>')
        return

    ordered = _RE_OL_ITEM.match(line)
    if ordered:
        _flush_paragraph(doc, para)
        doc.open_list("ol")
        doc.add_item(f'<li style="{_S_LI}"><div>{_inline(ordered.group(2))}</div>')
        return

    bullet = _RE_UL_ITEM.match(line)
    if bullet:
        _flush_paragraph(doc, para)
        doc.open_list("ul")
        doc.add_item(f'<li style="{_S_LI}"><div>{_inline(bullet.group(1))}</div>')
        return

    quote = _RE_QUOTE.match(line)
    if quote:
        _flush_paragraph(doc, para)
        doc.close_list()
        doc.add(f'<blockquote style="{_S_QUOTE}">{_inline(quote.group(1))}</blockquote>')
        return

    # An indented line directly under a list item is that item's detail line —
    # the shape every report skill uses for its "why this is consequential" line.
    # An indented line is a continuation only while an ITEM is open. A blank
    # line closes the item, so an indented line after one is a new block, not a
    # detail line — emitting it as a detail div would put a bare <div> directly
    # inside the <ul>, which is the same invalid-nesting class as the <p> above.
    cont = _RE_CONT.match(line)
    if cont and doc.item_open() and not para:
        doc.add(f'<div style="{_S_CONT}">{_inline(cont.group(1))}</div>')
        return

    para.append(line.strip())


def render_markdown(text: str) -> str:
    """Render the report-markdown subset to an inline-styled html fragment.

    Escape-by-default: every span of ``text`` is escaped before markup is
    emitted, so model-authored markup never reaches the reader as markup.
    """
    doc = _Doc()
    para: list[str] = []
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        if not raw_line.strip():
            # A blank line ends the current item but NOT the list: blank lines
            # between items are ordinary markdown and must not split one list
            # into several.
            _flush_paragraph(doc, para)
            doc.close_item()
            continue
        _render_line(doc, raw_line, para)
    _flush_paragraph(doc, para)
    doc.close_list()
    return f'<div style="{_S_WRAP}">' + "".join(doc.parts) + "</div>"


def looks_like_report(text: str) -> bool:
    """True when ``text`` carries markdown BLOCK structure — a heading, a list
    item, a rule, or a quote.

    The send path renders only what this returns True for. That is deliberate
    and conservative: the report skills all emit block structure per their
    ``output-format.md``, while a prose reply (the email-reply skill answering a
    person) emits none. So the reports gain an html part and every other send
    stays byte-identical to today — this change cannot reshape a client-facing
    reply as a side effect.

    Inline ``**bold**`` alone does NOT qualify. Prose can carry emphasis; only
    block structure means "this was composed as a report".

    A report that somehow carries no block marker simply gets no html part and
    renders exactly as it does today. The degradation is the status quo.
    """
    for line in text.replace("\r\n", "\n").split("\n"):
        if (
            _RE_HEADING.match(line)
            or _RE_OL_ITEM.match(line)
            or _RE_UL_ITEM.match(line)
            or _RE_HR.match(line)
            or _RE_QUOTE.match(line)
        ):
            return True
    return False


def html_text_content(rendered: str) -> str:
    """The reader-visible text of ``rendered``, tags removed and entities undone.

    The inverse used by the purity test: this must equal the source text once
    both sides are whitespace- and marker-normalized. If it ever does not, the
    renderer is inventing or dropping content and the send-path safety argument
    (inject after the scan, because html adds nothing the scan did not see) no
    longer holds.
    """
    return html_lib.unescape(_RE_TAG.sub(" ", rendered))
