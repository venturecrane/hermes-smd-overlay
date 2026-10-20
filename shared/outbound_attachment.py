"""The outbound attachments a seat may send, and the text the gate reads in them.

WHY THIS EXISTS. A routine's pre_run can hand its out-of-turn send a workbook
(statute-watch's monthly list, ss-console), and SMD can stage one send carrying
PDFs it authored (:mod:`shared.staged_send`). Every hop that carries an outbound
attachment must answer the same two questions the same way: is this a
well-formed attachment the seat is allowed to send, and what does a reader SEE
in it. Both answers live here so the prerendered dispatcher, the trust plugin's
sender and the transport cannot disagree about either.

WHAT IS ALLOWED. At most :data:`MAX_ATTACHMENTS` per message and
:data:`MAX_TOTAL_ATTACHMENT_BYTES` decoded between them, each named plainly and
carrying a sha256 that matches its bytes, of two kinds:

* ``.xlsx``: at most one per message, at most 512 KiB decoded (the original
  workbook rule, unchanged);
* ``.pdf``: at most :data:`MAX_PDF_BYTES` decoded each.

Which transport carries which is the transport's decision, not this module's:
the Graph verb still carries exactly one workbook (``outbound_send``), the
AgentMail verb carries the full set. The descriptor shape is pinned across
repos (ss-console's broker re-validates it and maps it to the vendor's)::

    {"name": str, "content_type": XLSX_CONTENT_TYPE | PDF_CONTENT_TYPE,
     "content_b64": <standard base64>, "sha256": <hex sha256 of the bytes>}

An attachment is NEVER a payload field and never a tool argument. It travels as
a separate keyword from code the model cannot reach (the prerendered envelope
under the ``.smd`` fence), the same shape as ``CodeFixedRecipients``.

WHAT THE GATE READS. :func:`extract_text` dispatches on the content type.
:func:`extract_xlsx_text` renders the workbook as the lines a reader sees: one
line per row, cells joined ``" | "`` so a (file number, date) pair stays on one
line for the identifier gate's PAIR check. A date-formatted numeric cell is a
serial number on disk (``46460``) and a date on screen, so it is rendered as the
ISO date it displays; without that the identifier gate would read a number where
the reader reads a deadline. :func:`extract_pdf_text` renders a PDF as its
document properties, each page's text, and the text of its links, annotations,
outline and form fields.

FAIL CLOSED. Anything this module cannot account for refuses. A workbook: a part
outside the closed list a plain data workbook needs (macros, embedded objects,
external links, drawings, hyperlink relationships), a formula (its displayed
value is not on disk to scan), a DOCTYPE, an oversized or over-populated
archive. A PDF: one that does not parse, is encrypted, carries JavaScript, an
additional-action trigger, any action other than an in-document jump or a web
link, an embedded or referenced file, rich media, an XFA form, more pages or
objects than a document needs, or no readable page text at all (an image-only
PDF cannot be read by the gate, so it does not go). A runtime with no PDF reader
refuses every PDF.

THE RESIDUAL, stated rather than hidden: text drawn INSIDE an image on a page
that also has real text is not read. The gate reads what a text layer says.
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
PDF_CONTENT_TYPE = "application/pdf"
#: Attachments one message may carry, every kind together.
MAX_ATTACHMENTS = 3
#: Workbooks one message may carry (the original rule).
MAX_XLSX_ATTACHMENTS = 1
#: The decoded bound on a workbook. The name predates PDFs and keeps its meaning.
MAX_ATTACHMENT_BYTES = 512 * 1024
#: The decoded bound on one PDF.
MAX_PDF_BYTES = 2 * 1024 * 1024
#: The decoded bound on every attachment of one message together. Base64 adds a
#: third on the wire, and AgentMail bounds a whole send request at 6 MB.
MAX_TOTAL_ATTACHMENT_BYTES = 3 * 1024 * 1024
NAME_RE = re.compile(r"^[\w .()-]{1,120}\.xlsx$")
PDF_NAME_RE = re.compile(r"^[\w .()-]{1,120}\.pdf$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Per content type: (name pattern, decoded bound, what the name must be).
_KINDS: dict[str, tuple[re.Pattern[str], int, str]] = {
    XLSX_CONTENT_TYPE: (NAME_RE, MAX_ATTACHMENT_BYTES, ".xlsx"),
    PDF_CONTENT_TYPE: (PDF_NAME_RE, MAX_PDF_BYTES, ".pdf"),
}

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
    nothing else a caller put on the object travels further. This checks ONE
    attachment; the per-message limits are :func:`validate_all`'s.
    """
    if not isinstance(obj, dict):
        raise AttachmentError("attachment is not an object")
    name = obj.get("name")
    content_type = obj.get("content_type")
    content_b64 = obj.get("content_b64")
    sha256 = obj.get("sha256")
    kind = _KINDS.get(content_type) if isinstance(content_type, str) else None
    if kind is None:
        raise AttachmentError("attachment content type is not a workbook or a PDF")
    name_re, bound, suffix = kind
    if not isinstance(name, str) or not name_re.fullmatch(name):
        raise AttachmentError(f"attachment name is not a plain {suffix} file name")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise AttachmentError("attachment sha256 is not a hex digest")
    if not isinstance(content_b64, str) or not content_b64:
        raise AttachmentError("attachment has no content")
    # Cheap length bound before decoding: base64 is 4 chars per 3 bytes.
    if len(content_b64) > (bound + 2) // 3 * 4 + 4:
        raise AttachmentError("attachment is larger than the allowed size")
    try:
        data = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("attachment content is not valid base64") from exc
    if not data:
        raise AttachmentError("attachment has no content")
    if len(data) > bound:
        raise AttachmentError("attachment is larger than the allowed size")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise AttachmentError("attachment sha256 does not match its content")
    if content_type == PDF_CONTENT_TYPE and not data.startswith(b"%PDF-"):
        raise AttachmentError("attachment is not a PDF file")
    descriptor = {
        "name": name,
        "content_type": content_type,
        "content_b64": content_b64,
        "sha256": sha256,
    }
    return descriptor, data


def _check_set(validated: list[tuple[dict[str, str], bytes]]) -> None:
    """The per-message limits over already-validated attachments."""
    if len(validated) > MAX_ATTACHMENTS:
        raise AttachmentError(f"{len(validated)} attachments (at most {MAX_ATTACHMENTS})")
    workbooks = sum(1 for d, _ in validated if d["content_type"] == XLSX_CONTENT_TYPE)
    if workbooks > MAX_XLSX_ATTACHMENTS:
        raise AttachmentError(f"{workbooks} workbooks (at most {MAX_XLSX_ATTACHMENTS})")
    if sum(len(data) for _, data in validated) > MAX_TOTAL_ATTACHMENT_BYTES:
        raise AttachmentError("the attachments together are larger than the allowed size")
    names = [d["name"].lower() for d, _ in validated]
    if len(set(names)) != len(names):
        raise AttachmentError("two attachments share a name")


def validate_all(value: Any) -> list[tuple[dict[str, str], bytes]]:
    """Every attachment of one message, validated, or :class:`AttachmentError`.

    A non-empty list of at most :data:`MAX_ATTACHMENTS`, each passing
    :func:`validate`, at most one workbook, distinct names, and at most
    :data:`MAX_TOTAL_ATTACHMENT_BYTES` between them. All or nothing: a message
    never goes with some of the files its author attached.
    """
    if not isinstance(value, list) or not value:
        raise AttachmentError("attachments is not a non-empty list")
    if len(value) > MAX_ATTACHMENTS:
        raise AttachmentError(f"{len(value)} attachments (at most {MAX_ATTACHMENTS})")
    validated = [validate(item) for item in value]
    _check_set(validated)
    return validated


def sanitize(value: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Split an envelope's ``attachments`` value into (valid, reasons-stripped).

    Never raises. ``None``/absent is no attachments and nothing stripped. More
    than :data:`MAX_ATTACHMENTS`, or a set that breaks a per-message limit,
    keeps none: picking some of several would be a guess about which the author
    meant.
    """
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["attachments is not a list"]
    if len(value) > MAX_ATTACHMENTS:
        return [], [f"{len(value)} attachments (at most {MAX_ATTACHMENTS})"] * len(value)
    kept: list[tuple[dict[str, str], bytes]] = []
    stripped: list[str] = []
    for item in value:
        try:
            kept.append(validate(item))
        except AttachmentError as exc:
            stripped.append(str(exc))
    try:
        _check_set(kept)
    except AttachmentError as exc:
        return [], stripped + [str(exc)] * len(kept)
    return [d for d, _ in kept], stripped


def extract_text(descriptor: dict[str, str], data: bytes) -> str:
    """The text a reader sees in a validated attachment, by its content type."""
    content_type = descriptor.get("content_type")
    if content_type == XLSX_CONTENT_TYPE:
        return extract_xlsx_text(data)
    if content_type == PDF_CONTENT_TYPE:
        return extract_pdf_text(data)
    raise AttachmentError("attachment content type is not a workbook or a PDF")


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


# ---------------------------------------------------------------------------
# What a reader sees in a PDF
# ---------------------------------------------------------------------------

#: A flyer, a letter, a form: anything SMD stages fits well inside these.
_MAX_PDF_PAGES = 50
_MAX_PDF_OBJECTS = 50_000

#: Dictionary keys whose presence means the document can act, not just be read.
_FORBIDDEN_PDF_KEYS = frozenset(
    {
        "/JS",
        "/JavaScript",
        "/AA",  # additional actions: triggers on open, close, focus, page view
        "/EmbeddedFile",
        "/EmbeddedFiles",
        "/RichMedia",
        "/RichMediaContent",
        "/XFA",
    }
)
#: The only action types a sent PDF may carry: a jump inside itself and a link.
_ALLOWED_PDF_ACTIONS = frozenset({"/GoTo", "/URI"})
#: ``/Type`` and ``/Subtype`` values that are files, media or scripts.
_FORBIDDEN_PDF_TYPES = frozenset(
    {
        "/Filespec",
        "/EmbeddedFile",
        "/FileAttachment",
        "/Sound",
        "/Movie",
        "/Screen",
        "/RichMedia",
        "/3D",
    }
)
_DOCINFO_KEYS = ("/Title", "/Subject", "/Author", "/Keywords")


def _pdf_reader(data: bytes) -> Any:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # a runtime without the reader sends no PDF
        raise AttachmentError("this seat cannot read a PDF") from exc
    if not data.startswith(b"%PDF-"):
        raise AttachmentError("attachment is not a PDF file")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        encrypted = reader.is_encrypted
    except Exception as exc:  # noqa: BLE001 - any parse failure is a refusal
        raise AttachmentError("the PDF could not be read") from exc
    if encrypted:
        raise AttachmentError("the PDF is encrypted")
    return reader


#: Where an action lives: an annotation's or outline item's ``/A``, the
#: catalog's ``/OpenAction``, an action's chained ``/Next``. A ``/S`` anywhere
#: else (a border style, a transparency group) is not an action.
_ACTION_KEYS = ("/A", "/OpenAction", "/Next")


def _check_action(value: Any) -> None:
    """Refuse an action that is neither a jump inside the document nor a link.

    An ``/OpenAction`` may be a bare destination array, which only opens the
    document at a page. A chain (``/Next``) may be one action or an array."""
    from pypdf.generic import ArrayObject, DictionaryObject

    value = value.get_object() if hasattr(value, "get_object") else value
    if isinstance(value, ArrayObject):
        for item in value:
            item = item.get_object() if hasattr(item, "get_object") else item
            if isinstance(item, DictionaryObject):
                _check_action(item)
        return
    if not isinstance(value, DictionaryObject):
        return
    # A destination array names a page dictionary, which is not an action.
    if "/S" not in value and value.get("/Type") != "/Action":
        return
    if value.get("/S") not in _ALLOWED_PDF_ACTIONS:
        raise AttachmentError("the PDF carries an action other than a jump or a link")


def _pdf_refuse_active_content(reader: Any) -> None:
    """Walk every object reachable from the trailer; refuse anything active.

    Reachable is the right set: an object nothing references is never rendered
    or run. Object streams are read through pypdf, so a dictionary compressed
    into one is checked like any other.
    """
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

    trailer = reader.trailer
    if "/Encrypt" in trailer:
        raise AttachmentError("the PDF is encrypted")
    stack: list[Any] = [trailer]
    seen: set[tuple[int, int]] = set()
    visited = 0
    while stack:
        node = stack.pop()
        if isinstance(node, IndirectObject):
            ref = (node.idnum, node.generation)
            if ref in seen:
                continue
            seen.add(ref)
            node = node.get_object()
        visited += 1
        if visited > _MAX_PDF_OBJECTS:
            raise AttachmentError("the PDF has more objects than a document needs")
        if isinstance(node, DictionaryObject):
            keys = set(node.keys())
            if keys & _FORBIDDEN_PDF_KEYS:
                raise AttachmentError("the PDF carries scripts, triggers or embedded files")
            for key in ("/Type", "/Subtype"):
                value = node.get(key)
                if isinstance(value, NameObject) and value in _FORBIDDEN_PDF_TYPES:
                    raise AttachmentError("the PDF carries a file, media or 3D content")
            if node.get("/Type") == "/Action":
                _check_action(node)
            for key in _ACTION_KEYS:
                if key in node:
                    _check_action(node[key])
            # /Parent points back up the page tree; following it only revisits.
            stack.extend(v for k, v in node.items() if k != "/Parent")
        elif isinstance(node, ArrayObject):
            stack.extend(node)


def _pdf_string(value: Any) -> str:
    try:
        text = value.get_object() if hasattr(value, "get_object") else value
    except Exception:  # noqa: BLE001
        return ""
    return str(text).strip() if isinstance(text, str) else ""


def _outline_titles(outline: Any) -> list[str]:
    titles: list[str] = []
    for item in outline or []:
        if isinstance(item, list):
            titles.extend(_outline_titles(item))
        else:
            title = getattr(item, "title", None)
            if isinstance(title, str) and title.strip():
                titles.append(title.strip())
    return titles


def extract_pdf_text(data: bytes) -> str:
    """The text a reader sees in PDF ``data``.

    Raises :class:`AttachmentError` on anything it cannot account for (see the
    module docstring). Lines, in order: the document properties a viewer shows,
    the outline, then per page its text, its annotations' text and its links.
    Form field values close the list.
    """
    reader = _pdf_reader(data)
    try:
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise AttachmentError("the PDF's pages could not be read") from exc
    if not pages:
        raise AttachmentError("the PDF has no pages")
    if len(pages) > _MAX_PDF_PAGES:
        raise AttachmentError("the PDF has more pages than the allowed number")
    try:
        _pdf_refuse_active_content(reader)
        lines: list[str] = []
        info = reader.trailer.get("/Info")
        info = info.get_object() if info is not None else None
        if info is not None and hasattr(info, "get"):
            for key in _DOCINFO_KEYS:
                value = _pdf_string(info.get(key))
                if value:
                    lines.append(value)
        lines.extend(_outline_titles(reader.outline))
        page_text = 0
        for page in pages:
            text = page.extract_text() or ""
            page_text += len(text.strip())
            lines.extend(line.strip() for line in text.splitlines() if line.strip())
            for annot in page.get("/Annots") or []:
                annot = annot.get_object()
                for key in ("/Contents", "/T", "/TU"):
                    value = _pdf_string(annot.get(key))
                    if value:
                        lines.append(value)
                action = annot.get("/A")
                action = action.get_object() if action is not None else None
                if action is not None and hasattr(action, "get"):
                    uri = _pdf_string(action.get("/URI"))
                    if uri:
                        lines.append(uri)
        for field in (reader.get_fields() or {}).values():
            value = _pdf_string(field.get("/V")) if hasattr(field, "get") else ""
            if value:
                lines.append(value)
    except AttachmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - "could not look" must not send
        raise AttachmentError("the PDF's text could not be read") from exc
    if not page_text:
        raise AttachmentError("the PDF has no readable text, so it cannot be checked")
    return "\n".join(lines)


__all__ = [
    "MAX_ATTACHMENTS",
    "MAX_ATTACHMENT_BYTES",
    "MAX_PDF_BYTES",
    "MAX_TOTAL_ATTACHMENT_BYTES",
    "MAX_XLSX_ATTACHMENTS",
    "NAME_RE",
    "PDF_CONTENT_TYPE",
    "PDF_NAME_RE",
    "XLSX_CONTENT_TYPE",
    "AttachmentError",
    "extract_pdf_text",
    "extract_text",
    "extract_xlsx_text",
    "sanitize",
    "validate",
    "validate_all",
]
