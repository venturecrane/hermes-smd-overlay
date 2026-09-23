"""Tests for spooling a whole email as .eml (shared/msgraph_attachments.spool_message)
and the staff-mailbox allowlist it shares with ss-console's msgraph-mail connector.

The load-bearing assertion is the refusal: a staff mailbox the firm did not
author produces NO Graph call. The opener raises on any unscripted call, and
each refusal test scripts none, so a refusal that reached Graph fails loudly.
The authorized cases prove the same opener does see calls when they happen.
"""

from __future__ import annotations

import hashlib

import pytest

from shared import customer_config, msgraph_attachments, staff_mailboxes
from shared.msgraph_attachments import MsGraphAttachmentError
from tests.test_msgraph_attachments import _install, _json, _Resp

EML = (
    b"From: someone@outside.example\r\nTo: manager@firm.example\r\n"
    b"Subject: Re: 09.22 discovery / responses\r\n\r\nThe chart is attached.\r\n"
)
META = {"subject": "Re: 09.22 discovery / responses", "receivedDateTime": "2026-09-22T16:04:00Z"}
STAFF = "manager@firm.example"


@pytest.fixture
def authored(monkeypatch):
    """Author the seat's staff_mailbox_reads block (or none)."""

    def _author(block):
        data = {"customer_id": "x"}
        if block is not None:
            data["staff_mailbox_reads"] = block
        monkeypatch.setattr(
            customer_config.CustomerConfig,
            "from_volume",
            classmethod(lambda cls, path=None: cls(data)),
        )

    return _author


def test_own_mailbox_spools_the_mime_under_a_fileable_name(monkeypatch, tmp_path, authored):
    authored(None)
    opener = _install(monkeypatch, [_json(META), _Resp(200, EML)], spool_dir=tmp_path)
    receipt = msgraph_attachments.spool_message("AAMkMSG")
    assert opener.calls[0].startswith(
        "https://graph.microsoft.com/v1.0/users/operator@example.test/messages/AAMkMSG?"
    )
    assert (
        opener.calls[1]
        == "https://graph.microsoft.com/v1.0/users/operator@example.test/messages/AAMkMSG/$value"
    )
    assert receipt["content_type"] == "message/rfc822"
    assert receipt["sha256"] == hashlib.sha256(EML).hexdigest()
    assert (tmp_path / f"{receipt['spool_token']}.bin").read_bytes() == EML
    # Smokeball truncates at the first period, so the stem carries none.
    assert receipt["filename"] == "2026-09-22 Re 09 22 discovery responses.eml"
    assert "." not in receipt["filename"][: -len(".eml")]


def test_authored_staff_mailbox_reads_under_that_mailbox(monkeypatch, tmp_path, authored):
    authored({"mailboxes": [STAFF.upper()]})
    opener = _install(monkeypatch, [_json(META), _Resp(200, EML)], spool_dir=tmp_path)
    msgraph_attachments.spool_message("AAMkMSG", STAFF)
    assert all(
        c.startswith(f"https://graph.microsoft.com/v1.0/users/{STAFF}/messages/AAMkMSG")
        for c in opener.calls
    )
    assert len(opener.calls) == 2


@pytest.mark.parametrize(
    "block",
    [
        None,
        {"mailboxes": []},
        {"mailboxes": ["other@firm.example"]},
    ],
)
def test_unauthored_staff_mailbox_makes_no_graph_call(monkeypatch, tmp_path, authored, block):
    authored(block)
    opener = _install(monkeypatch, [], spool_dir=tmp_path)
    with pytest.raises(MsGraphAttachmentError, match="not a staff mailbox the firm has authored"):
        msgraph_attachments.spool_message("AAMkMSG", STAFF)
    assert opener.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("block", ["manager@firm.example", {"mailboxes": "manager@firm.example"}])
def test_malformed_block_refuses_rather_than_reading_as_empty(
    monkeypatch, tmp_path, authored, block
):
    authored(block)
    opener = _install(monkeypatch, [], spool_dir=tmp_path)
    with pytest.raises(MsGraphAttachmentError, match="must be a"):
        msgraph_attachments.spool_message("AAMkMSG", STAFF)
    assert opener.calls == []


@pytest.mark.parametrize(
    "mailbox",
    [
        "manager@firm.example/../x@firm.example",
        "manager@firm.example?x=1",
        "manager%40firm.example",
    ],
)
def test_path_changing_mailbox_is_refused(monkeypatch, tmp_path, authored, mailbox):
    authored({"mailboxes": [STAFF]})
    opener = _install(monkeypatch, [], spool_dir=tmp_path)
    with pytest.raises(MsGraphAttachmentError, match="plain email address"):
        msgraph_attachments.spool_message("AAMkMSG", mailbox)
    assert opener.calls == []


def test_own_mailbox_named_explicitly_is_refused(monkeypatch, tmp_path, authored):
    authored({"mailboxes": ["operator@example.test"]})
    opener = _install(monkeypatch, [], spool_dir=tmp_path)
    with pytest.raises(MsGraphAttachmentError, match="own mailbox"):
        msgraph_attachments.spool_message("AAMkMSG", "operator@example.test")
    assert opener.calls == []


def test_empty_body_is_not_spooled(monkeypatch, tmp_path, authored):
    authored(None)
    _install(monkeypatch, [_json(META), _Resp(200, b"")], spool_dir=tmp_path)
    with pytest.raises(MsGraphAttachmentError, match="empty"):
        msgraph_attachments.spool_message("AAMkMSG")


def test_empty_subject_still_names_the_file(monkeypatch, tmp_path, authored):
    authored(None)
    _install(
        monkeypatch,
        [_json({"subject": "...", "receivedDateTime": "2026-09-22T01:00:00Z"}), _Resp(200, EML)],
        spool_dir=tmp_path,
    )
    assert msgraph_attachments.spool_message("AAMkMSG")["filename"] == "2026-09-22 email.eml"


def test_bad_message_id_is_refused_before_a_call(monkeypatch, tmp_path, authored):
    authored(None)
    opener = _install(monkeypatch, [], spool_dir=tmp_path)
    with pytest.raises(MsGraphAttachmentError, match="shape of a Graph id"):
        msgraph_attachments.spool_message("../../users/x")
    assert opener.calls == []


def test_staff_list_normalizes_and_drops_unsafe_entries(authored):
    authored({"mailboxes": [" A@Firm.Example ", "bad/@firm.example", 7, "a@firm.example"]})
    assert staff_mailboxes.authored() == ("a@firm.example",)
