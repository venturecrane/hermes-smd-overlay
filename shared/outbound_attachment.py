"""The one outbound attachment a seat may send, and the text the gate reads in it.

WHY THIS EXISTS. A routine's pre_run can now hand its out-of-turn send a
workbook (statute-watch's monthly list, ss-console). Until this module no hop
carried an outbound attachment at all, and every hop that now does must answer
the same two questions the same way: is this a well-formed attachment the seat
is allowed to send, and what does a reader SEE in it. Both answers live here so
the prerendered dispatcher, the trust plugin's sender and the transport cannot
disagree about either.

WHAT IS ALLOWED. Exactly one ``.xlsx`` per message, at most 512 KiB decoded,
named plainly, whose sha256 matches the bytes. The descriptor shape is pinned
across repos (ss-console's skill writes it, its broker maps it to a Graph
``fileAttachment``)::

    {"name": str, "content_type": XLSX_CONTENT_TYPE,
     "content_b64": <standard base64>, "sha256": <hex sha256 of the bytes>}

An attachment is NEVER a payload field and never a tool argument. It travels as
a separate keyword from code the model cannot reach (the prerendered envelope
under the ``.smd`` fence), the same shape as ``CodeFixedRecipients``.

WHAT THE GATE READS. :func:`extract_xlsx_text` renders the workbook as the
lines a reader sees: one line per row, cells joined ``" | "`` so a (file
number, date) pair stays on one line for the identifier gate's PAIR check. A
date-formatted numeric cell is a serial number on disk (``46460``) and a date
on screen, so it is rendered as the ISO date it displays; without that the
identifier gate would read a number where the reader reads a deadline.

FAIL CLOSED. Anything this module cannot account for refuses: a part outside
the closed list a plain data workbook needs (macros, embedded objects, external
links, drawings, hyperlink relationships), a formula (its displayed value is
not on disk to scan), a DOCTYPE, an oversized or over-populated archive.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import zipfile
from datetime import datetime, timedelta
from typing import Any
from xml.etree import ElementTree

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_ATTACHMENTS = 1
MAX_ATTACHMENT_BYTES = 512 * 1024
NAME_RE = re.compile(r"^[\w .()-]{1,120}\.xlsx$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Archive bounds: a 512 KiB zip can still inflate enormously.
_MAX_MEMBERS = 64
_MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024

#: The parts a plain data workbook is made of. Anything else refuses.
_ALLOWED_PARTS = re.compile(
    r"^(?:\[Content_Types\]\.xml"
    r"|_rels/\.rels"
    r"|docProps/(?:app|core)\.xml"
    r"|xl/workbook\.xml"
    r"|xl/_rels/workbook\.xml\.rels"
    r"|xl/styles\.xml"
    r"|xl/theme/theme\d+\.xml"
    r"|xl/sharedStrings\.xml"
    r"|xl/calcChain\.xml"
    r"|xl/worksheets/sheet\d+\.xml)$"
)

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

#: Built-in number formats that display a date (ECMA-376 18.8.30).
_BUILTIN_DATE_FORMATS = frozenset(range(14, 23))

#: docProps fields a person can see in File > Properties.
_CORE_TEXT_TAGS = frozenset(
    {"title", "subject", "creator", "keywords", "description", "lastModifiedBy", "category"}
)


class AttachmentError(ValueError):
    """The attachment is malformed, outside the allowed shape, or unreadable."""


def validate(obj: Any) -> tuple[dict[str, str], bytes]:
    """The canonical descriptor and its decoded bytes, or :class:`AttachmentError`.

    The returned descriptor is a fresh dict of exactly the four pinned keys, so
    nothing else a caller put on the object travels further.
    """
    if not isinstance(obj, dict):
        raise AttachmentError("attachment is not an object")
    name = obj.get("name")
    content_type = obj.get("content_type")
    content_b64 = obj.get("content_b64")
    sha256 = obj.get("sha256")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise AttachmentError("attachment name is not a plain .xlsx file name")
    if content_type != XLSX_CONTENT_TYPE:
        raise AttachmentError("attachment content type is not a workbook")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise AttachmentError("attachment sha256 is not a hex digest")
    if not isinstance(content_b64, str) or not content_b64:
        raise AttachmentError("attachment has no content")
    # Cheap length bound before decoding: base64 is 4 chars per 3 bytes.
    if len(content_b64) > (MAX_ATTACHMENT_BYTES + 2) // 3 * 4 + 4:
        raise AttachmentError("attachment is larger than the allowed size")
    try:
        data = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("attachment content is not valid base64") from exc
    if not data:
        raise AttachmentError("attachment has no content")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError("attachment is larger than the allowed size")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise AttachmentError("attachment sha256 does not match its content")
    descriptor = {
        "name": name,
        "content_type": content_type,
        "content_b64": content_b64,
        "sha256": sha256,
    }
    return descriptor, data


def sanitize(value: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Split an envelope's ``attachments`` value into (valid, reasons-stripped).

    Never raises. ``None``/absent is no attachments and nothing stripped. More
    than :data:`MAX_ATTACHMENTS` keeps none: picking one of several would be a
    guess about which the author meant.
    """
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["attachments is not a list"]
    if len(value) > MAX_ATTACHMENTS:
        return [], [f"{len(value)} attachments (at most {MAX_ATTACHMENTS})"] * len(value)
    kept: list[dict[str, str]] = []
    stripped: list[str] = []
    for item in value:
        try:
            descriptor, _ = validate(item)
        except AttachmentError as exc:
            stripped.append(str(exc))
            continue
        kept.append(descriptor)
    return kept, stripped


# ---------------------------------------------------------------------------
# What a reader sees
# ---------------------------------------------------------------------------


def _parse(raw: bytes) -> ElementTree.Element:
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise AttachmentError("workbook part declares a DOCTYPE")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise AttachmentError("workbook part is not well-formed XML") from exc


def _text_of(element: ElementTree.Element) -> str:
    """All ``<t>`` text under ``element`` (a shared string or an inline string),
    skipping phonetic runs (``<rPh>``), which Excel does not display."""
    parts: list[str] = []
    for child in element:
        if child.tag == f"{_NS}t":
            parts.append(child.text or "")
        elif child.tag == f"{_NS}r":
            parts.extend(t.text or "" for t in child.iter(f"{_NS}t"))
    return "".join(parts)


def _strip_format_literals(code: str) -> str:
    """A format code with its quoted literals, escapes and [bracketed] sections
    removed, leaving only the tokens that format the number."""
    code = re.sub(r'"[^"]*"', "", code)
    code = re.sub(r"\\.", "", code)
    code = re.sub(r"\[[^\]]*\]", "", code)
    return code.lower()


def _is_date_format_code(code: str) -> bool:
    tokens = _strip_format_literals(code)
    if "d" in tokens or "y" in tokens:
        return True
    # "m" alone is month; beside h or s it is minutes (a time format).
    return "m" in tokens and "h" not in tokens and "s" not in tokens


def _date_styles(styles_raw: bytes | None) -> frozenset[int]:
    """Indices into ``cellXfs`` whose number format displays a date."""
    if styles_raw is None:
        return frozenset()
    root = _parse(styles_raw)
    custom_dates: set[int] = set()
    num_fmts = root.find(f"{_NS}numFmts")
    if num_fmts is not None:
        for fmt in num_fmts.findall(f"{_NS}numFmt"):
            try:
                fmt_id = int(fmt.get("numFmtId", ""))
            except ValueError:
                continue
            if _is_date_format_code(fmt.get("formatCode", "")):
                custom_dates.add(fmt_id)
    dates: set[int] = set()
    cell_xfs = root.find(f"{_NS}cellXfs")
    if cell_xfs is not None:
        for index, xf in enumerate(cell_xfs.findall(f"{_NS}xf")):
            try:
                fmt_id = int(xf.get("numFmtId", "0"))
            except ValueError:
                continue
            if fmt_id in _BUILTIN_DATE_FORMATS or fmt_id in custom_dates:
                dates.add(index)
    return frozenset(dates)


def _serial_to_iso(value: str, epoch: datetime) -> str:
    """An Excel date serial as the ISO date it displays; raw text if not a number."""
    try:
        serial = float(value)
    except ValueError:
        return value
    if serial < 0 or serial > 2_958_465:  # 9999-12-31
        return value
    return (epoch + timedelta(days=serial)).date().isoformat()


def _number_text(value: str) -> str:
    """A numeric cell as displayed in General format: integral floats lose ``.0``."""
    try:
        number = float(value)
    except ValueError:
        return value
    if number.is_integer() and "e" not in value.lower() and abs(number) < 1e15:
        return str(int(number))
    return value


def _cell_text(
    cell: ElementTree.Element,
    shared: list[str],
    date_styles: frozenset[int],
    epoch: datetime,
) -> str:
    if cell.find(f"{_NS}f") is not None:
        raise AttachmentError("workbook carries a formula")
    kind = cell.get("t", "n")
    value_el = cell.find(f"{_NS}v")
    value = value_el.text if value_el is not None and value_el.text is not None else ""
    if kind == "inlineStr":
        inline = cell.find(f"{_NS}is")
        return _text_of(inline) if inline is not None else ""
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError) as exc:
            raise AttachmentError("workbook cell names a missing shared string") from exc
    if kind == "b":
        return "TRUE" if value.strip() == "1" else "FALSE"
    if kind == "d":
        # ISO 8601 stored as text; the date part is what a date format shows.
        return value[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", value) else value
    if kind in ("str", "e"):
        return value
    if not value:
        return ""
    try:
        style = int(cell.get("s", "0"))
    except ValueError:
        style = 0
    if style in date_styles:
        return _serial_to_iso(value, epoch)
    return _number_text(value)


def _sheet_targets(archive: zipfile.ZipFile, names: set[str]) -> list[tuple[str, str]]:
    """(sheet name, part path) in workbook order; sheets the workbook does not
    list are still read (a part nobody lists can still be opened by hand)."""
    ordered: list[tuple[str, str]] = []
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        rels_root = _parse(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {}
        for rel in rels_root.iter(f"{_PKG_REL_NS}Relationship"):
            if rel.get("TargetMode", "").lower() == "external":
                raise AttachmentError("workbook links to an external target")
            target = (rel.get("Target") or "").lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            targets[rel.get("Id")] = target
        wb_root = _parse(archive.read("xl/workbook.xml"))
        sheets = wb_root.find(f"{_NS}sheets")
        if sheets is not None:
            for sheet in sheets.findall(f"{_NS}sheet"):
                target = targets.get(sheet.get(f"{_REL_NS}id"))
                if target in names:
                    ordered.append((sheet.get("name", ""), target))
    listed = {path for _, path in ordered}
    for path in sorted(n for n in names if n.startswith("xl/worksheets/") and n not in listed):
        ordered.append(("", path))
    return ordered


def _workbook_epoch_and_names(
    archive: zipfile.ZipFile, names: set[str]
) -> tuple[datetime, list[str]]:
    """The date-system epoch, and any defined-name text (visible in Name Manager)."""
    epoch = datetime(1899, 12, 30)
    defined: list[str] = []
    if "xl/workbook.xml" not in names:
        return epoch, defined
    root = _parse(archive.read("xl/workbook.xml"))
    pr = root.find(f"{_NS}workbookPr")
    if pr is not None and pr.get("date1904", "").lower() in ("1", "true"):
        epoch = datetime(1904, 1, 1)
    container = root.find(f"{_NS}definedNames")
    if container is not None:
        for name in container.findall(f"{_NS}definedName"):
            defined.append(" | ".join(x for x in (name.get("name", ""), name.text or "") if x))
    return epoch, defined


def extract_xlsx_text(data: bytes) -> str:
    """The text a reader sees in workbook ``data``, one line per row.

    Raises :class:`AttachmentError` on anything it cannot account for (see the
    module docstring). Lines, in order: document properties, defined names,
    then per sheet its name, every non-empty row (cells joined ``" | "``), and
    its header/footer text.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise AttachmentError("attachment is not a workbook archive") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > _MAX_MEMBERS:
            raise AttachmentError("workbook has too many parts")
        if sum(info.file_size for info in infos) > _MAX_UNCOMPRESSED_BYTES:
            raise AttachmentError("workbook inflates beyond the allowed size")
        names = {info.filename for info in infos}
        for name in names:
            if not _ALLOWED_PARTS.fullmatch(name):
                raise AttachmentError("workbook carries a part a plain data workbook does not")
        lines: list[str] = []
        if "docProps/core.xml" in names:
            for element in _parse(archive.read("docProps/core.xml")):
                local = element.tag.rsplit("}", 1)[-1]
                if local in _CORE_TEXT_TAGS and (element.text or "").strip():
                    lines.append(element.text.strip())
        epoch, defined = _workbook_epoch_and_names(archive, names)
        lines.extend(defined)
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = _parse(archive.read("xl/sharedStrings.xml"))
            shared = [_text_of(si) for si in root.findall(f"{_NS}si")]
        date_styles = _date_styles(
            archive.read("xl/styles.xml") if "xl/styles.xml" in names else None
        )
        for sheet_name, path in _sheet_targets(archive, names):
            if sheet_name.strip():
                lines.append(sheet_name.strip())
            root = _parse(archive.read(path))
            sheet_data = root.find(f"{_NS}sheetData")
            if sheet_data is not None:
                for row in sheet_data.findall(f"{_NS}row"):
                    cells = [
                        _cell_text(cell, shared, date_styles, epoch)
                        for cell in row.findall(f"{_NS}c")
                    ]
                    cells = [c.strip() for c in cells if c and c.strip()]
                    if cells:
                        lines.append(" | ".join(cells))
            header_footer = root.find(f"{_NS}headerFooter")
            if header_footer is not None:
                for part in header_footer:
                    if (part.text or "").strip():
                        lines.append(part.text.strip())
        return "\n".join(lines)


__all__ = [
    "MAX_ATTACHMENTS",
    "MAX_ATTACHMENT_BYTES",
    "NAME_RE",
    "XLSX_CONTENT_TYPE",
    "AttachmentError",
    "extract_xlsx_text",
    "sanitize",
    "validate",
]
