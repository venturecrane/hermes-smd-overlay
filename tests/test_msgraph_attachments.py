"""Tests for the Graph attachment path (shared/msgraph_attachments.py).

Each test here is written so that removing the line it defends makes it fail.
Two in particular are the point of the file:

* ``test_list_drops_content_bytes_graph_sent_anyway`` feeds a response that
  CARRIES ``contentBytes``. The ``$select`` is an optimisation; the allowlist
  construction is the control. If the handler ever passes Graph's object
  through, a scanned client letter reaches the model's context and this fails.
* ``test_entry_with_unreadable_type_is_refused`` feeds an entry with no
  ``@odata.type``. Whether Graph annotates a derived type under a narrowing
  ``$select`` is Graph's property, not ours, and defaulting to accept would
  spool a MIME blob as if it were the letter.
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error

import pytest

from shared import attachment_spool, msgraph_attachments, msgraph_client
from shared.msgraph_attachments import MsGraphAttachmentError

PDF = b"%PDF-1.4\n" + b"x" * 400


class _Resp:
    """urlopen response whose ``read`` accepts the optional size the raw path passes."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body
        self.read_sizes: list[int | None] = []

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int | None = None) -> bytes:
        self.read_sizes.append(size)
        return self._body if size is None else self._body[:size]


class _Opener:
    """Injectable urlopen: answers the token POST, then plays a queued script."""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[str] = []

    def __call__(self, req, timeout=None):  # noqa: ANN001
        url = req.full_url
        if "login.microsoftonline.com" in url:
            return _Resp(200, b'{"access_token":"T","expires_in":3600}')
        self.calls.append(url)
        if not self._script:
            raise AssertionError(f"unscripted Graph call: {url}")
        nxt = self._script.pop(0)
        if isinstance(nxt, urllib.error.HTTPError):
            raise nxt
        return nxt


def _install(monkeypatch, script: list[object], spool_dir=None) -> _Opener:
    opener = _Opener(script)
    client = msgraph_client.MsGraphClient(
        tenant_id="t",
        client_id="c",
        client_secret="s",
        mailbox="operator@example.test",
        opener=opener,
    )
    monkeypatch.setattr(msgraph_client, "build_client_from_env", lambda **_: client)
    if spool_dir is not None:
        monkeypatch.setenv(attachment_spool.SPOOL_DIR_ENV, str(spool_dir))
    return opener


def _json(payload: object) -> _Resp:
    return _Resp(200, json.dumps(payload).encode())


def _file_entry(**over) -> dict:
    entry = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": "AAMkAGUz",
        "name": "letter.pdf",
        "contentType": "application/pdf",
        "size": len(PDF),
        "isInline": False,
    }
    entry.update(over)
    return entry


# ---- list ------------------------------------------------------------------


def test_list_sends_the_narrowing_select(monkeypatch):
    opener = _install(monkeypatch, [_json({"value": [_file_entry()]})])
    msgraph_attachments.list_attachments("AAMkMSG")
    assert "%24select=id%2Cname%2CcontentType%2Csize%2CisInline" in opener.calls[0]


def test_list_drops_content_bytes_graph_sent_anyway(monkeypatch):
    """The allowlist, not the $select, is what keeps a client document out of the turn."""
    entry = _file_entry(contentBytes="JVBERi0xLjQK" * 500)
    _install(monkeypatch, [_json({"value": [entry]})])
    found = msgraph_attachments.list_attachments("AAMkMSG")
    assert found[0]["filename"] == "letter.pdf"
    assert "contentBytes" not in json.dumps(found)


def test_entry_with_unreadable_type_is_refused(monkeypatch):
    entry = _file_entry()
    del entry["@odata.type"]
    _install(monkeypatch, [_json({"value": [entry]})])
    found = msgraph_attachments.list_attachments("AAMkMSG")
    assert "could not be read" in found[0]["refused"]


@pytest.mark.parametrize(
    "odata_type,fragment",
    [
        ("#microsoft.graph.itemAttachment", "embedded Outlook item"),
        ("#microsoft.graph.referenceAttachment", "link to cloud storage"),
    ],
)
def test_non_file_types_are_refused_by_name(monkeypatch, odata_type, fragment):
    _install(monkeypatch, [_json({"value": [_file_entry(**{"@odata.type": odata_type})]})])
    found = msgraph_attachments.list_attachments("AAMkMSG")
    assert fragment in found[0]["refused"]


def test_file_attachment_is_not_refused(monkeypatch):
    """The falsifier for the three tests above: a real file must come back clean."""
    _install(monkeypatch, [_json({"value": [_file_entry()]})])
    assert msgraph_attachments.list_attachments("AAMkMSG")[0]["refused"] == ""


def test_inline_is_reported_not_filtered(monkeypatch):
    _install(monkeypatch, [_json({"value": [_file_entry(isInline=True)]})])
    found = msgraph_attachments.list_attachments("AAMkMSG")
    assert found[0]["is_inline"] is True and found[0]["refused"] == ""


def test_empty_list_is_empty_not_an_error(monkeypatch):
    _install(monkeypatch, [_json({"value": []})])
    assert msgraph_attachments.list_attachments("AAMkMSG") == []


# ---- ids -------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../users/someone@else.test/messages",
        "AAMk?$select=body",
        "AAMk/../../users",
        "",
        "AAMk\x00",
    ],
)
def test_a_bad_id_never_becomes_a_url(monkeypatch, bad):
    opener = _install(monkeypatch, [])
    with pytest.raises(MsGraphAttachmentError):
        msgraph_attachments.list_attachments(bad)
    assert opener.calls == [], "a refused id must not reach Graph"


# ---- spool -----------------------------------------------------------------


def test_spool_writes_the_bytes_and_returns_a_receipt(monkeypatch, tmp_path):
    _install(monkeypatch, [_json(_file_entry()), _Resp(200, PDF)], spool_dir=tmp_path)
    receipt = msgraph_attachments.spool_attachment("AAMkMSG", "AAMkATT")
    assert receipt["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert receipt["filename"] == "letter.pdf"
    assert (tmp_path / f"{receipt['spool_token']}.bin").read_bytes() == PDF


def test_spool_refuses_a_non_file_before_fetching_bytes(monkeypatch, tmp_path):
    opener = _install(
        monkeypatch,
        [_json(_file_entry(**{"@odata.type": "#microsoft.graph.itemAttachment"}))],
        spool_dir=tmp_path,
    )
    with pytest.raises(MsGraphAttachmentError, match="embedded Outlook item"):
        msgraph_attachments.spool_attachment("AAMkMSG", "AAMkATT")
    assert len(opener.calls) == 1, "the type refusal must cost one metadata read, not a download"
    assert list(tmp_path.iterdir()) == []


def test_spool_refuses_when_the_length_disagrees_with_graph(monkeypatch, tmp_path):
    """A short read and a complete small file are identical from the bytes alone."""
    _install(monkeypatch, [_json(_file_entry()), _Resp(200, PDF[:100])], spool_dir=tmp_path)
    with pytest.raises(MsGraphAttachmentError, match=f"{len(PDF)} bytes and 100 arrived"):
        msgraph_attachments.spool_attachment("AAMkMSG", "AAMkATT")
    assert list(tmp_path.iterdir()) == [], "a truncated document must not reach the spool"


def test_spool_refuses_an_oversize_attachment_before_fetching(monkeypatch, tmp_path):
    over = attachment_spool.MAX_SPOOL_BYTES + 1
    opener = _install(monkeypatch, [_json(_file_entry(size=over))], spool_dir=tmp_path)
    with pytest.raises(MsGraphAttachmentError, match="over the"):
        msgraph_attachments.spool_attachment("AAMkMSG", "AAMkATT")
    assert len(opener.calls) == 1


def test_spool_reads_at_most_the_ceiling_plus_one(monkeypatch, tmp_path):
    body = _Resp(200, PDF)
    _install(monkeypatch, [_json(_file_entry()), body], spool_dir=tmp_path)
    msgraph_attachments.spool_attachment("AAMkMSG", "AAMkATT")
    assert body.read_sizes == [attachment_spool.MAX_SPOOL_BYTES + 1], (
        "the bytes path must bound its read so an oversized attachment "
        "never lands in the machine's memory whole"
    )


# ---- credential ------------------------------------------------------------


def test_no_credential_raises_rather_than_reporting_no_attachments(monkeypatch):
    monkeypatch.setattr(msgraph_client, "build_client_from_env", lambda **_: None)
    with pytest.raises(MsGraphAttachmentError, match="NOT checked"):
        msgraph_attachments.list_attachments("AAMkMSG")


def test_a_graph_error_names_itself(monkeypatch):
    err = urllib.error.HTTPError("https://graph", 404, "nf", {}, io.BytesIO(b"{}"))
    _install(monkeypatch, [err])
    with pytest.raises(MsGraphAttachmentError, match="could not be listed"):
        msgraph_attachments.list_attachments("AAMkMSG")
