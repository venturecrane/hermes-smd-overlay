"""Hand-built PDFs for the outbound PDF tests.

Built byte by byte rather than by a writer library so each test controls the
exact objects a file carries: the passing flyer shape, and every active-content
shape the reader must refuse. The cross-reference table is computed, so pypdf
reads each file the way it reads a real one rather than repairing it.
"""

from __future__ import annotations

import base64
import hashlib


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(
    lines: list[str] | tuple[str, ...] = ("Six weeks. Starts Monday, October 26.",),
    *,
    pages: int = 1,
    catalog_extra: str = "",
    page_extra: str = "",
    extra_objects: dict[int, str] | None = None,
    info_title: str | None = "Pilot flyer",
    text: bool = True,
) -> bytes:
    """A small valid PDF: ``pages`` pages, each drawing ``lines`` in Helvetica.

    ``catalog_extra`` / ``page_extra`` are raw dictionary entries added to the
    catalog and to every page; ``extra_objects`` are more numbered objects (from
    20 up) those entries may reference. ``text=False`` draws nothing.
    """
    objects: dict[int, str] = {}
    page_ids = [10 + 2 * i for i in range(pages)]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = f"<< /Type /Catalog /Pages 2 0 R {catalog_extra} >>"
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>"
    objects[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    if info_title is not None:
        objects[4] = f"<< /Title ({_escape(info_title)}) >>"
    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for line in lines:
        ops.append(f"({_escape(line)}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops) if text else ""
    for pid in page_ids:
        objects[pid] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {pid + 1} 0 R {page_extra} >>"
        )
        objects[pid + 1] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    objects.update(extra_objects or {})
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{objects[num]}\nendobj\n".encode("latin-1")
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, size):
        if num in offsets:
            out += f"{offsets[num]:010d} 00000 n \n".encode()
        else:
            out += b"0000000000 65535 f \n"
    info = " /Info 4 0 R" if info_title is not None else ""
    out += f"trailer\n<< /Size {size} /Root 1 0 R{info} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def pdf_descriptor(data: bytes | None = None, name: str = "Pilot flyer.pdf") -> dict:
    data = make_pdf() if data is None else data
    return {
        "name": name,
        "content_type": "application/pdf",
        "content_b64": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
