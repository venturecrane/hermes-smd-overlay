"""The outbound PDF attachment: what may go, what the gate reads in it, and the
AgentMail transport that now carries it.

A PDF is a program as much as a document: it can run JavaScript, act on open,
launch a file, carry other files inside it. The reader here must refuse every
one of those shapes and read the text a person sees in what remains, because
that text is what the identifier gate scans. The falsifier pair at the bottom
is the control that the gate really reads it: the SAME PDF refuses when its
date was never read and passes once it was.
"""

from __future__ import annotations

import base64
import hashlib
import io

import pytest

from shared import outbound_attachment, provenance
from tests.conftest import load_plugin
from tests.pdf_samples import make_pdf, pdf_descriptor
from tests.test_attachment_scan import descriptor as xlsx_descriptor
from tests.test_attachment_scan import gate  # noqa: F401 - the shared gate fixture

FILE_NUMBER = "2026-PI-106"
STATUTE_DATE = "2027-03-14"


# ---------------------------------------------------------------------------
# What a reader sees
# ---------------------------------------------------------------------------


def test_the_page_text_title_and_link_are_read():
    data = make_pdf(
        ["Six weeks. Starts Monday, October 26.", "Interested? Tell Tim."],
        info_title="Brokery pilot",
        page_extra="/Annots [20 0 R]",
        extra_objects={
            20: "<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] "
            "/A << /S /URI /URI (https://thebrokery.example/pilot) >> >>"
        },
    )
    lines = outbound_attachment.extract_pdf_text(data).splitlines()
    assert lines[0] == "Brokery pilot"
    assert "Six weeks. Starts Monday, October 26." in lines
    assert "Interested? Tell Tim." in lines
    assert "https://thebrokery.example/pilot" in lines


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"catalog_extra": "/OpenAction [10 0 R /Fit]"}, id="open-at-a-page"),
        pytest.param(
            {
                "catalog_extra": "/OpenAction 20 0 R",
                "extra_objects": {20: "<< /S /GoTo /D [10 0 R /Fit] >>"},
            },
            id="open-with-goto",
        ),
        pytest.param(
            {
                "page_extra": "/Annots [20 0 R]",
                "extra_objects": {
                    20: "<< /Type /Annot /Subtype /Link /Rect [0 0 1 1] /BS << /W 0 /S /S >> "
                    "/A << /S /URI /URI (https://example.com) >> >>"
                },
            },
            id="border-style-is-not-an-action",
        ),
        pytest.param(
            {"page_extra": "/Group << /Type /Group /S /Transparency /CS /DeviceRGB >>"},
            id="transparency-group-is-not-an-action",
        ),
    ],
)
def test_a_plain_document_shape_passes(kwargs):
    assert "October 26" in outbound_attachment.extract_pdf_text(make_pdf(**kwargs))


def _encrypted() -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(make_pdf())).pages:
        writer.add_page(page)
    writer.encrypt(user_password="", owner_password="owner", algorithm="RC4-40")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


_JS = "<< /S /JavaScript /JS (app.alert(1)) >>"


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: make_pdf(catalog_extra="/OpenAction 20 0 R", extra_objects={20: _JS}),
            id="javascript-on-open",
        ),
        pytest.param(
            lambda: make_pdf(
                catalog_extra="/Names << /JavaScript 20 0 R >>",
                extra_objects={20: "<< /Names [(a) 21 0 R] >>", 21: _JS},
            ),
            id="document-javascript",
        ),
        pytest.param(
            lambda: make_pdf(page_extra="/AA << /O 20 0 R >>", extra_objects={20: _JS}),
            id="page-open-trigger",
        ),
        pytest.param(
            lambda: make_pdf(
                page_extra="/Annots [20 0 R]",
                extra_objects={
                    20: "<< /Type /Annot /Subtype /Link /Rect [0 0 1 1] "
                    "/A << /S /Launch /F (calc.exe) >> >>"
                },
            ),
            id="launch-link",
        ),
        pytest.param(
            lambda: make_pdf(
                page_extra="/Annots [20 0 R]",
                extra_objects={
                    20: "<< /Type /Annot /Subtype /Link /Rect [0 0 1 1] "
                    "/A << /S /GoToR /F (other.pdf) /D [0 /Fit] >> >>"
                },
            ),
            id="remote-goto",
        ),
        pytest.param(
            lambda: make_pdf(
                page_extra="/Annots [20 0 R]",
                extra_objects={
                    20: "<< /Type /Annot /Subtype /Widget /Rect [0 0 1 1] "
                    "/A << /S /SubmitForm /F (https://example.com) >> >>"
                },
            ),
            id="submit-form",
        ),
        pytest.param(
            lambda: make_pdf(
                page_extra="/Annots [20 0 R]",
                extra_objects={
                    20: "<< /Type /Annot /Subtype /Link /Rect [0 0 1 1] "
                    "/A << /S /URI /URI (https://example.com) /Next 21 0 R >> >>",
                    21: _JS,
                },
            ),
            id="javascript-chained-after-a-link",
        ),
        pytest.param(
            lambda: make_pdf(
                catalog_extra="/Names << /EmbeddedFiles 20 0 R >>",
                extra_objects={20: "<< /Names [] >>"},
            ),
            id="embedded-files",
        ),
        pytest.param(
            lambda: make_pdf(
                page_extra="/Annots [20 0 R]",
                extra_objects={
                    20: "<< /Type /Annot /Subtype /FileAttachment /Rect [0 0 1 1] /FS 21 0 R >>",
                    21: "<< /Type /Filespec /F (a.exe) >>",
                },
            ),
            id="file-attachment",
        ),
        pytest.param(
            lambda: make_pdf(
                page_extra="/Annots [20 0 R]",
                extra_objects={20: "<< /Type /Annot /Subtype /RichMedia /Rect [0 0 1 1] >>"},
            ),
            id="rich-media",
        ),
        pytest.param(
            lambda: make_pdf(
                catalog_extra="/AcroForm << /Fields [] /XFA 20 0 R >>",
                extra_objects={20: "<< /Length 0 >>\nstream\n\nendstream"},
            ),
            id="xfa-form",
        ),
        pytest.param(lambda: make_pdf(text=False), id="no-readable-text"),
        pytest.param(lambda: make_pdf(pages=51), id="too-many-pages"),
        pytest.param(_encrypted, id="encrypted"),
        pytest.param(lambda: b"%PDF-1.4\nnot really a pdf at all\n", id="unparseable"),
        pytest.param(lambda: b"PK\x03\x04 a zip, not a pdf", id="not-a-pdf"),
    ],
)
def test_anything_active_or_unreadable_refuses(build):
    with pytest.raises(outbound_attachment.AttachmentError):
        outbound_attachment.extract_pdf_text(build())


def test_a_runtime_without_a_pdf_reader_refuses_every_pdf(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_pypdf(name, *args, **kwargs):
        if name == "pypdf" or name.startswith("pypdf."):
            raise ImportError("no pypdf here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)
    with pytest.raises(outbound_attachment.AttachmentError, match="cannot read a PDF"):
        outbound_attachment.extract_pdf_text(make_pdf())


def test_extract_text_dispatches_on_the_content_type():
    pdf, pdf_bytes = outbound_attachment.validate(pdf_descriptor())
    assert "October 26" in outbound_attachment.extract_text(pdf, pdf_bytes)
    xlsx, xlsx_bytes = outbound_attachment.validate(xlsx_descriptor())
    assert outbound_attachment.extract_text(xlsx, xlsx_bytes) == (
        outbound_attachment.extract_xlsx_text(xlsx_bytes)
    )


# ---------------------------------------------------------------------------
# Validation: one attachment, and the set one message carries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: {**d, "name": "flyer.pdf.exe"}, id="exe-name"),
        pytest.param(lambda d: {**d, "name": "flyer.xlsx"}, id="workbook-name-on-a-pdf"),
        pytest.param(lambda d: {**d, "name": "../flyer.pdf"}, id="path-name"),
        pytest.param(lambda d: {**d, "sha256": "0" * 64}, id="sha-mismatch"),
    ],
)
def test_a_malformed_pdf_descriptor_is_rejected(mutate):
    with pytest.raises(outbound_attachment.AttachmentError):
        outbound_attachment.validate(mutate(pdf_descriptor()))


def test_bytes_that_are_not_a_pdf_are_rejected_at_validation():
    with pytest.raises(outbound_attachment.AttachmentError, match="not a PDF"):
        outbound_attachment.validate(pdf_descriptor(b"GIF89a not a pdf"))


def test_the_pdf_size_bound_is_exact():
    at_bound = b"%PDF-" + b"\0" * (outbound_attachment.MAX_PDF_BYTES - 5)
    outbound_attachment.validate(pdf_descriptor(at_bound))
    with pytest.raises(outbound_attachment.AttachmentError, match="larger"):
        outbound_attachment.validate(pdf_descriptor(at_bound + b"\0"))


def _pdfs(n: int, size: int | None = None) -> list[dict]:
    out = []
    for i in range(n):
        data = make_pdf([f"Flyer {i}"]) if size is None else b"%PDF-" + bytes([i]) * (size - 5)
        out.append(pdf_descriptor(data, name=f"Flyer {i}.pdf"))
    return out


def test_three_pdfs_and_a_workbook_mix_validate_as_a_set():
    assert len(outbound_attachment.validate_all(_pdfs(3))) == 3
    assert len(outbound_attachment.validate_all([*_pdfs(2), xlsx_descriptor()])) == 3


@pytest.mark.parametrize(
    "build, match",
    [
        pytest.param(lambda: _pdfs(4), "at most 3", id="four"),
        pytest.param(
            lambda: [xlsx_descriptor(), xlsx_descriptor(name="b.xlsx")],
            "workbooks",
            id="two-workbooks",
        ),
        pytest.param(lambda: _pdfs(2, size=1_600_000), "together", id="over-the-total"),
        pytest.param(lambda: [pdf_descriptor(), pdf_descriptor()], "share a name", id="same-name"),
        pytest.param(lambda: [], "non-empty", id="empty"),
        pytest.param(lambda: "x", "non-empty", id="not-a-list"),
    ],
)
def test_a_set_outside_the_limits_is_refused_whole(build, match):
    with pytest.raises(outbound_attachment.AttachmentError, match=match):
        outbound_attachment.validate_all(build())


def test_sanitize_keeps_none_of_a_set_that_breaks_a_limit():
    kept, stripped = outbound_attachment.sanitize(_pdfs(2, size=1_600_000))
    assert kept == [] and len(stripped) == 2
    kept, stripped = outbound_attachment.sanitize(_pdfs(2))
    assert len(kept) == 2 and stripped == []


# ---------------------------------------------------------------------------
# The gate reads the PDF (the falsifier pair)
# ---------------------------------------------------------------------------


def _dated_pdf_text() -> str:
    data = make_pdf([f"Statute date {STATUTE_DATE} for file {FILE_NUMBER}"])
    return outbound_attachment.extract_pdf_text(data)


def test_falsifier_an_unread_date_in_a_pdf_refuses(gate):  # noqa: F811
    provenance.record_records("sess-pdf", [{"matterNumber": FILE_NUMBER, "dates": ["2027-01-02"]}])
    block = gate.check_outbound_attachment(_dated_pdf_text(), session_id="sess-pdf")
    assert block is not None and block["action"] == "block"


def test_falsifier_the_same_pdf_passes_once_its_date_was_read(gate):  # noqa: F811
    provenance.record_records("sess-pdf", [{"matterNumber": FILE_NUMBER, "dates": [STATUTE_DATE]}])
    assert gate.check_outbound_attachment(_dated_pdf_text(), session_id="sess-pdf") is None


# ---------------------------------------------------------------------------
# The transport: AgentMail carries the set; Graph still carries one workbook
# ---------------------------------------------------------------------------


def _outbound_send():
    return load_plugin("hermes-smd-trust").outbound_send


def _capture(captured: list):
    def send(body, **kwargs):
        captured.append({"body": body, **kwargs})
        return "am-msg-1"

    return send


def test_agentmail_forwards_every_validated_attachment_as_the_pinned_descriptor():
    mod = _outbound_send()
    captured: list = []
    attachments = [
        pdf_descriptor(make_pdf(["A"]), "A.pdf"),
        pdf_descriptor(make_pdf(["B"]), "B.pdf"),
    ]
    message_id = mod.send_message(
        payload={"to": "tim@brokerage.example", "subject": "Flyers", "text": "Both attached."},
        sender=_capture(captured),
        attachments=[{**a, "extra": "dropped"} for a in attachments],
    )
    assert message_id == "am-msg-1"
    [call] = captured
    assert call["body"]["attachments"] == attachments


def test_agentmail_without_attachments_sends_exactly_as_before():
    """The falsifier for every other AgentMail send: no attachments key at all."""
    mod = _outbound_send()
    captured: list = []
    mod.send_message(payload={"to": "a@b.com", "text": "hi"}, sender=_capture(captured))
    assert captured[0]["body"] == {"to": "a@b.com", "text": "hi"}


@pytest.mark.parametrize(
    "attachments",
    [
        pytest.param([{**pdf_descriptor(), "sha256": "0" * 64}], id="sha-mismatch"),
        pytest.param(_pdfs(4), id="too-many"),
        pytest.param(_pdfs(2, size=1_600_000), id="over-the-total"),
    ],
)
def test_agentmail_refuses_a_bad_attachment_loudly_never_drops_it(attachments):
    mod = _outbound_send()
    captured: list = []
    with pytest.raises(mod.AgentMailAttachmentError):
        mod.send_message(
            payload={"to": "a@b.com", "text": "attached"},
            sender=_capture(captured),
            attachments=attachments,
        )
    assert captured == []
    assert issubclass(mod.AgentMailAttachmentError, mod.AttachmentSendError)


def test_agentmail_refuses_files_on_an_invitation():
    from shared.send_dispatch import CALENDAR_ARG

    mod = _outbound_send()
    captured: list = []
    with pytest.raises(mod.AgentMailAttachmentError, match="invitation"):
        mod.send_message(
            payload={
                "to": "a@b.com",
                "text": "hi",
                CALENDAR_ARG: {"method": "REQUEST", "ics": "x"},
            },
            sender=_capture(captured),
            attachments=[pdf_descriptor()],
        )
    assert captured == []


def test_a_broker_refusal_of_the_attachment_is_an_attachment_refusal():
    from shared import agentmail_broker

    mod = _outbound_send()

    def refuse(_body, **_kwargs):
        raise agentmail_broker.BrokerError(
            "attachment refused: the PDF is not one", code="AgentMailRefused"
        )

    with pytest.raises(mod.AgentMailAttachmentError):
        mod.send_message(
            payload={"to": "a@b.com", "text": "attached"},
            sender=refuse,
            attachments=[pdf_descriptor()],
        )


def test_graph_refuses_a_pdf_before_the_broker_is_asked():
    mod = _outbound_send()
    captured: list = []
    with pytest.raises(mod.MsGraphAttachmentError, match="only a workbook"):
        mod.send_via_msgraph(
            {"to": "a@b.com", "text": "attached"},
            sender=_capture(captured),
            attachments=[pdf_descriptor()],
        )
    assert captured == []


# ---------------------------------------------------------------------------
# The sender: two PDFs through _dispatch_internal_message on an AgentMail seat
# ---------------------------------------------------------------------------


def _arm(monkeypatch, trust, adapter):
    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_draft", lambda **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_send", lambda **k: None)
    monkeypatch.setattr(trust, "get_secret", lambda k: "brokery")
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: adapter)
    am: list = []
    graph: list = []
    monkeypatch.setattr(trust.outbound_send.agentmail_broker, "send_message", _capture(am))
    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", _capture(graph))
    return am, graph


def test_two_pdfs_reach_the_agentmail_broker_scanned_and_stamped(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    am, graph = _arm(monkeypatch, trust, "agentmail")
    scanned: list[str] = []
    monkeypatch.setattr(
        trust.outbound, "check_outbound_attachment", lambda text, **k: scanned.append(text)
    )
    a = pdf_descriptor(make_pdf(["Starts Monday, October 26."]), "Flyer Oct 26.pdf")
    b = pdf_descriptor(make_pdf(["Starts Monday, November 2."]), "Flyer Nov 2.pdf")
    result = trust._dispatch_internal_message(
        to=["tim@brokerage.example"],
        subject="Flyers",
        text="Both attached.",
        session_id="s1",
        attachments=[a, b],
    )
    assert result.sent and not result.attachment_refused
    [call] = am
    assert [x["sha256"] for x in call["body"]["attachments"]] == [a["sha256"], b["sha256"]]
    assert call["audit_extra"]["attachment_sha256"] == f"{a['sha256']},{b['sha256']}"
    assert any("October 26" in t for t in scanned) and any("November 2" in t for t in scanned)
    assert graph == []


def test_one_refused_pdf_refuses_the_whole_send(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    am, _graph = _arm(monkeypatch, trust, "agentmail")
    monkeypatch.setattr(
        trust.outbound,
        "check_outbound_attachment",
        lambda text, **k: {"action": "block", "message": "no"} if "November" in text else None,
    )
    result = trust._dispatch_internal_message(
        to=["tim@brokerage.example"],
        subject="Flyers",
        text="Both attached.",
        session_id="s1",
        attachments=[
            pdf_descriptor(make_pdf(["October 26"]), "A.pdf"),
            pdf_descriptor(make_pdf(["November 2"]), "B.pdf"),
        ],
    )
    assert not result.sent and result.attachment_refused and am == []


def test_a_pdf_on_a_graph_seat_is_an_attachment_refusal(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    am, graph = _arm(monkeypatch, trust, "msgraph")
    monkeypatch.setattr(trust.outbound, "check_outbound_attachment", lambda text, **k: None)
    result = trust._dispatch_internal_message(
        to=["staff@firm.example"],
        subject="s",
        text="b",
        session_id="s1",
        attachments=[pdf_descriptor()],
    )
    assert not result.sent and result.attachment_refused
    assert am == [] and graph == []


def test_the_descriptor_bytes_round_trip():
    data = make_pdf()
    d = pdf_descriptor(data)
    assert base64.b64decode(d["content_b64"]) == data
    assert d["sha256"] == hashlib.sha256(data).hexdigest()
