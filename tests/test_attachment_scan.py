"""The outbound workbook attachment: its shape, what the gate reads in it, and
the falsifier that proves a date-formatted cell is read as a date.

statute-watch v2 (ss-console) attaches one ``.xlsx`` to its monthly send. The
gate must read that workbook as a person sees it: a date cell is a serial
number on disk (``46460``) and a deadline on screen. If extraction rendered the
serial, the identifier gate would scan a number and a fabricated statute date
would ship. The falsifier pair below is the control: the SAME real openpyxl
workbook refuses when its date was never read and passes once it was.
"""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from shared import outbound_attachment, provenance
from tests.conftest import load_plugin

_FIXTURE = Path(__file__).parent / "fixtures" / "attachments" / "statute_watch_sample.xlsx.b64"
FILE_NUMBER = "2026-PI-106"  # mirrors make_statute_watch_sample.py
STATUTE_DATE = "2027-03-14"


def _sample_bytes() -> bytes:
    return base64.b64decode(_FIXTURE.read_text(encoding="ascii").strip())


def descriptor(data: bytes | None = None, name: str = "Statute watch - October 2026.xlsx") -> dict:
    data = _sample_bytes() if data is None else data
    return {
        "name": name,
        "content_type": outbound_attachment.XLSX_CONTENT_TYPE,
        "content_b64": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _workbook(
    sheet_xml: str,
    *,
    styles: str = "",
    shared: str = "",
    extra: dict | None = None,
    date1904: bool = False,
) -> bytes:
    """A minimal hand-built workbook for the shapes openpyxl never writes
    (shared strings, the 1904 epoch, a formula, a forbidden part)."""
    parts = {
        "[Content_Types].xml": "<Types/>",
        "xl/workbook.xml": (
            f'<workbook xmlns="{_MAIN}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            + ('<workbookPr date1904="1"/>' if date1904 else "<workbookPr/>")
            + '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_MAIN}"><sheetData>{sheet_xml}</sheetData></worksheet>',
    }
    if styles:
        parts["xl/styles.xml"] = f'<styleSheet xmlns="{_MAIN}">{styles}</styleSheet>'
    if shared:
        parts["xl/sharedStrings.xml"] = f'<sst xmlns="{_MAIN}">{shared}</sst>'
    parts.update(extra or {})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, body in parts.items():
            archive.writestr(name, body)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Extraction: what a reader sees
# ---------------------------------------------------------------------------


def test_the_real_workbook_reads_as_rows_with_its_date_as_a_date():
    text = outbound_attachment.extract_xlsx_text(_sample_bytes())
    lines = text.splitlines()
    # The date cell is stored as a serial; the reader (and the gate) see the date,
    # on the same line as the file number, so the PAIR check sees them together.
    assert f"12 | {STATUTE_DATE} | {FILE_NUMBER} | Doe, Jane | Attorney A" in lines
    assert "Days left | Statute date | File number | Client | Attorney" in lines
    assert "Statute watch" in lines and "Since last month" in lines
    assert f"New | {FILE_NUMBER} | Doe, Jane" in lines
    assert "46460" not in text


def test_shared_strings_builtin_date_format_and_numbers():
    styles = '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>'
    shared = "<si><t>Hello</t></si><si><r><t>Rich </t></r><r><t>text</t></r></si>"
    sheet = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" s="1"><v>46460</v></c><c r="B2"><v>203353</v></c>'
        '<c r="C2" t="b"><v>1</v></c><c r="D2"><v>2.5</v></c></row>'
    )
    text = outbound_attachment.extract_xlsx_text(_workbook(sheet, styles=styles, shared=shared))
    assert text.splitlines() == [
        "Data",
        "Hello | Rich text",
        f"{STATUTE_DATE} | 203353 | TRUE | 2.5",
    ]


def test_the_1904_date_system_is_honoured():
    styles = '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>'
    sheet = '<row r="1"><c r="A1" s="1"><v>0</v></c></row>'
    text = outbound_attachment.extract_xlsx_text(_workbook(sheet, styles=styles, date1904=True))
    assert "1904-01-01" in text


def test_a_custom_time_format_is_not_read_as_a_date():
    styles = (
        '<numFmts><numFmt numFmtId="170" formatCode="h:mm"/>'
        '<numFmt numFmtId="171" formatCode="[$-409]d-mmm-yy;@"/></numFmts>'
        '<cellXfs><xf numFmtId="0"/><xf numFmtId="170"/><xf numFmtId="171"/></cellXfs>'
    )
    sheet = '<row r="1"><c r="A1" s="1"><v>0.5</v></c><c r="B1" s="2"><v>46460</v></c></row>'
    text = outbound_attachment.extract_xlsx_text(_workbook(sheet, styles=styles))
    assert text.splitlines()[-1] == f"0.5 | {STATUTE_DATE}"


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: _workbook('<row r="1"><c r="A1"><f>DATE(2027,3,14)</f></c></row>'), id="formula"
        ),
        pytest.param(
            lambda: _workbook('<row r="1"/>', extra={"xl/vbaProject.bin": "x"}), id="macro-part"
        ),
        pytest.param(
            lambda: _workbook('<row r="1"/>', extra={"xl/externalLinks/externalLink1.xml": "<x/>"}),
            id="external-link",
        ),
        pytest.param(
            lambda: _workbook('<row r="1"/>', shared='<!DOCTYPE x [<!ENTITY a "b">]><si/>'),
            id="doctype",
        ),
        pytest.param(lambda: b"not a zip", id="not-a-zip"),
    ],
)
def test_anything_unaccounted_for_refuses(build):
    with pytest.raises(outbound_attachment.AttachmentError):
        outbound_attachment.extract_xlsx_text(build())


# ---------------------------------------------------------------------------
# Validation: the pinned descriptor
# ---------------------------------------------------------------------------


def test_a_valid_descriptor_round_trips_as_exactly_the_four_keys():
    raw = {**descriptor(), "extra": "dropped"}
    clean, data = outbound_attachment.validate(raw)
    assert set(clean) == {"name", "content_type", "content_b64", "sha256"}
    assert data == _sample_bytes()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: {**d, "name": "../evil.xlsx"}, id="path-name"),
        pytest.param(lambda d: {**d, "name": "list.xlsm"}, id="macro-extension"),
        pytest.param(
            lambda d: {**d, "content_type": "application/octet-stream"}, id="content-type"
        ),
        pytest.param(lambda d: {**d, "content_b64": d["content_b64"][:-4] + "!!!!"}, id="bad-b64"),
        pytest.param(lambda d: {**d, "sha256": "0" * 64}, id="sha-mismatch"),
        pytest.param(lambda d: {**d, "sha256": "XYZ"}, id="sha-shape"),
        pytest.param(lambda d: "not a dict", id="not-object"),
    ],
)
def test_a_malformed_descriptor_is_rejected(mutate):
    with pytest.raises(outbound_attachment.AttachmentError):
        outbound_attachment.validate(mutate(descriptor()))


def test_the_512_kib_bound_is_exact():
    at_bound = b"\0" * outbound_attachment.MAX_ATTACHMENT_BYTES
    outbound_attachment.validate(descriptor(at_bound))
    with pytest.raises(outbound_attachment.AttachmentError, match="larger"):
        outbound_attachment.validate(descriptor(at_bound + b"\0"))


def test_sanitize_strips_bad_entries_and_refuses_more_than_one():
    good = descriptor()
    kept, stripped = outbound_attachment.sanitize([good])
    assert kept == [outbound_attachment.validate(good)[0]] and stripped == []
    kept, stripped = outbound_attachment.sanitize([{**good, "sha256": "0" * 64}])
    assert kept == [] and len(stripped) == 1
    kept, stripped = outbound_attachment.sanitize([good, good])
    assert kept == [] and len(stripped) == 2
    assert outbound_attachment.sanitize(None) == ([], [])
    assert outbound_attachment.sanitize("x")[0] == []


# ---------------------------------------------------------------------------
# The gate: check_outbound_attachment (the falsifier pair)
# ---------------------------------------------------------------------------


class _FakeD1:
    def __init__(self) -> None:
        self.calls: list = []

    def query(self, *args, **kwargs):  # noqa: D401 — duck-typed D1 client
        self.calls.append(args)
        return {"success": True}

    execute = query


@pytest.fixture
def gate(monkeypatch, tmp_path):
    trust = load_plugin("hermes-smd-trust")
    ob = trust.outbound
    ob._AUDIT_CLIENT = _FakeD1()
    ob._AUDIT_CUSTOMER_SLUG = "acme"
    ob._AUDIT_WIRED = True
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("SMD_IDENTIFIER_GATE_MODE", raising=False)
    provenance._reset_for_tests()
    yield ob
    provenance._reset_for_tests()


def _sample_text() -> str:
    return outbound_attachment.extract_xlsx_text(_sample_bytes())


def test_falsifier_an_unseeded_date_in_a_date_cell_refuses(gate):
    # The file number was read, with a DIFFERENT date: the register is not
    # empty, and the workbook's statute date is on no record.
    provenance.record_records("sess-att", [{"matterNumber": FILE_NUMBER, "dates": ["2027-01-02"]}])
    block = gate.check_outbound_attachment(_sample_text(), session_id="sess-att")
    assert block is not None and block["action"] == "block"


def test_falsifier_the_same_workbook_passes_once_its_date_was_read(gate):
    provenance.record_records("sess-att", [{"matterNumber": FILE_NUMBER, "dates": [STATUTE_DATE]}])
    assert gate.check_outbound_attachment(_sample_text(), session_id="sess-att") is None


def test_an_empty_register_refuses_on_the_send_gate(gate):
    """No empty-register carve: an attachment nothing was read for cannot be verified."""
    assert gate.check_outbound_attachment(_sample_text(), session_id="sess-empty") is not None


def test_a_crashing_identifier_scan_refuses_the_attachment(gate, monkeypatch):
    provenance.record_records("sess-att", [{"matterNumber": FILE_NUMBER, "dates": [STATUTE_DATE]}])

    def boom(*_a, **_k):
        raise RuntimeError("scanner down")

    monkeypatch.setattr(gate.identifier_filter, "check", boom)
    block = gate.check_outbound_attachment(_sample_text(), session_id="sess-att")
    assert block is not None and block["action"] == "block"


def test_a_fabricated_citation_in_a_cell_refuses(gate):
    provenance.record_records("sess-att", [{"matterNumber": FILE_NUMBER, "dates": [STATUTE_DATE]}])
    text = _sample_text() + "\nSee Smith v. Jones, 123 F.3d 456 (9th Cir. 1999)"
    assert gate.check_outbound_attachment(text, session_id="sess-att") is not None


# ---------------------------------------------------------------------------
# The sender: _dispatch_internal_message with an attachment
# ---------------------------------------------------------------------------


def _arm_sender(monkeypatch, trust, captured, adapter="msgraph"):
    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_draft", lambda **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_send", lambda **k: None)
    monkeypatch.setattr(trust, "get_secret", lambda k: "pilot-smokeball")
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: adapter)

    def fake_broker_send(body, **kwargs):
        captured.append({"body": body, **kwargs})
        return "<sent@firm.example>"

    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", fake_broker_send)


def test_a_passing_attachment_reaches_the_broker_as_a_separate_key(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    captured: list[dict] = []
    _arm_sender(monkeypatch, trust, captured)
    monkeypatch.setattr(trust.outbound, "check_outbound_attachment", lambda text, **k: None)
    att = descriptor()
    result = trust._dispatch_internal_message(
        to=["staff@firm.example"],
        subject="Statute watch",
        text="The full list is attached.",
        session_id="s1",
        audit_extra={"body_variant": "full", "attachment_sha256": "f" * 64},
        attachments=[att],
    )
    assert result.sent and not result.attachment_refused
    [call] = captured
    assert call["body"]["attachments"] == [outbound_attachment.validate(att)[0]]
    # Stamped by the sender from the validated bytes; a caller's value is replaced.
    assert call["audit_extra"]["attachment_sha256"] == att["sha256"]


def test_the_scan_reads_the_extracted_workbook_text(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    captured: list[dict] = []
    _arm_sender(monkeypatch, trust, captured)
    seen: list[str] = []
    monkeypatch.setattr(
        trust.outbound,
        "check_outbound_attachment",
        lambda text, **k: seen.append(text) or None,
    )
    trust._dispatch_internal_message(
        to=["staff@firm.example"],
        subject="s",
        text="body",
        session_id="s1",
        attachments=[descriptor()],
    )
    assert seen == [_sample_text()]


def test_a_refused_attachment_refuses_the_send_and_says_why(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    captured: list[dict] = []
    _arm_sender(monkeypatch, trust, captured)
    monkeypatch.setattr(
        trust.outbound,
        "check_outbound_attachment",
        lambda text, **k: {"action": "block", "message": "unverified date"},
    )
    result = trust._dispatch_internal_message(
        to=["staff@firm.example"],
        subject="s",
        text="body",
        session_id="s1",
        attachments=[descriptor()],
    )
    assert not result.sent and result.attachment_refused
    assert captured == []


def test_a_raising_attachment_scan_fails_closed(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    captured: list[dict] = []
    _arm_sender(monkeypatch, trust, captured)

    def boom(text, **_k):
        raise RuntimeError("scan down")

    monkeypatch.setattr(trust.outbound, "check_outbound_attachment", boom)
    result = trust._dispatch_internal_message(
        to=["staff@firm.example"],
        subject="s",
        text="body",
        session_id="s1",
        attachments=[descriptor()],
    )
    assert not result.sent and result.attachment_refused and captured == []


def test_a_body_refusal_is_not_an_attachment_refusal(monkeypatch):
    """Rung 2 exists for the attachment alone. A refused BODY must reach the
    skeleton, not a resend of the same body without its workbook."""
    trust = load_plugin("hermes-smd-trust")
    captured: list[dict] = []
    _arm_sender(monkeypatch, trust, captured)
    monkeypatch.setattr(
        trust.outbound, "check_outbound_send", lambda **k: {"action": "block", "message": "no"}
    )
    scanned: list[str] = []
    monkeypatch.setattr(
        trust.outbound, "check_outbound_attachment", lambda text, **k: scanned.append(text)
    )
    result = trust._dispatch_internal_message(
        to=["staff@firm.example"],
        subject="s",
        text="body",
        session_id="s1",
        attachments=[descriptor()],
    )
    assert not result.sent and not result.attachment_refused
    assert scanned == []  # the attachment is scanned only after the body passed


def test_an_invalid_attachment_handed_to_the_sender_refuses(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    captured: list[dict] = []
    _arm_sender(monkeypatch, trust, captured)
    result = trust._dispatch_internal_message(
        to=["staff@firm.example"],
        subject="s",
        text="body",
        session_id="s1",
        attachments=[{**descriptor(), "sha256": "0" * 64}],
    )
    assert not result.sent and result.attachment_refused and captured == []


def test_agentmail_seat_refuses_an_attachment_as_an_attachment_refusal(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    captured: list[dict] = []
    _arm_sender(monkeypatch, trust, captured, adapter="agentmail")
    monkeypatch.setattr(trust.outbound, "check_outbound_attachment", lambda text, **k: None)
    am_calls: list = []
    monkeypatch.setattr(
        trust.outbound_send.agentmail_broker,
        "send_message",
        lambda body, **k: am_calls.append(body) or "am-1",
    )
    result = trust._dispatch_internal_message(
        to=["staff@firm.example"],
        subject="s",
        text="body",
        session_id="s1",
        attachments=[descriptor()],
    )
    assert not result.sent and result.attachment_refused
    assert am_calls == [] and captured == []


def test_no_attachment_keeps_the_transport_call_unchanged(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    captured: list[dict] = []
    _arm_sender(monkeypatch, trust, captured)
    result = trust._dispatch_internal_message(
        to=["staff@firm.example"], subject="s", text="body", session_id="s1"
    )
    assert result.sent
    [call] = captured
    assert "attachments" not in call["body"]
    assert "attachment_sha256" not in call["audit_extra"]


# ---------------------------------------------------------------------------
# End to end: envelope -> prerendered ladder -> trust sender -> REAL attachment
# scan -> broker double. The falsifier pair again, through every hop.
# ---------------------------------------------------------------------------


def _wire_end_to_end(monkeypatch, tmp_path, gate):
    from shared import prerendered_dispatch, send_dispatch
    from tests import test_prerendered_dispatch as tpd

    trust = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(trust.outbound, "_AUDIT_CLIENT", gate._AUDIT_CLIENT)
    monkeypatch.setattr(trust.outbound, "_AUDIT_WIRED", True)
    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_draft", lambda **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_send", lambda **k: None)
    monkeypatch.setattr(trust, "get_secret", lambda k: "pilot-smokeball")
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: "msgraph")
    sent: list[dict] = []
    monkeypatch.setattr(
        trust.outbound_send.msgraph_broker,
        "send_message",
        lambda body, **kwargs: sent.append({"body": body, **kwargs}) or "<m@firm.example>",
    )
    send_dispatch.set_sender(trust._dispatch_internal_message)
    tpd._routine(monkeypatch)
    tpd._appends_recorder(monkeypatch)
    entry = tpd._attachment_entry(full_body="The full list is attached.\n")
    tpd._write_envelope(tmp_path, dispatches=[entry])
    session = trust._resolved_session({"session_id": tpd.SESSION})
    return prerendered_dispatch, tpd.SESSION, session, sent, entry


def test_end_to_end_a_read_date_ships_the_workbook(gate, monkeypatch, tmp_path):
    dispatcher, cron_session, session, sent, _entry = _wire_end_to_end(monkeypatch, tmp_path, gate)
    provenance.record_records(session, [{"matterNumber": FILE_NUMBER, "dates": [STATUTE_DATE]}])
    note = dispatcher.dispatch_prerendered(cron_session)
    [call] = sent
    assert call["body"]["attachments"][0]["sha256"] == descriptor()["sha256"]
    assert call["audit_extra"]["attachment_sha256"] == descriptor()["sha256"]
    assert call["audit_extra"]["body_variant"] == "full"
    assert "without its attachment" not in note


def test_end_to_end_an_unread_date_sends_the_body_without_the_workbook(gate, monkeypatch, tmp_path):
    dispatcher, cron_session, session, sent, entry = _wire_end_to_end(monkeypatch, tmp_path, gate)
    provenance.record_records(session, [{"matterNumber": FILE_NUMBER, "dates": ["2027-01-02"]}])
    note = dispatcher.dispatch_prerendered(cron_session)
    [call] = sent  # rung 1 never reached the broker; rung 2 did
    assert "attachments" not in call["body"]
    assert "attachment_sha256" not in call["audit_extra"]
    assert call["audit_extra"]["body_variant"] == "full_no_attachment"
    assert "could not be attached" in call["body"]["text"]
    assert "without its attachment" in note
