"""The two attachment verbs, and the plugin that puts them on the seat.

THE DEFECT (live, 2026-09-18). A seat received a vendor invoice with a PDF
attached and replied "Your message arrived without any attachments." The
``message.received`` payload it had carried no ``attachments`` key at all, and
the records tools it could reach all wanted a download URL the mail vendor does
not mint. These tests pin the two halves of the fix and, for each, the direction
that would be a defect if it flipped.
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from shared import agentmail_broker as broker
from shared import attachment_spool as spool
from shared import email_adapter, msgraph_attachments
from tests.conftest import load_plugin

MESSAGE = {
    "message_id": "msg_123",
    "subject": "FW: invoice",
    "attachments": [
        {
            "attachment_id": "att_1",
            "filename": "invoice-1001.pdf",
            "content_type": "application/pdf",
            "size": 1983,
        },
        {
            "attachment_id": "att_2",
            "filename": "../../etc/passwd",
            "content_type": "application/pdf",
            "size": 12,
        },
        {"filename": "no-id.pdf"},
    ],
}

PDF = b"%PDF-1.7 vendor invoice body"

SEAT_INBOX = "pilot@seat.example"

DOWNLOAD_URL = "https://cdn.agentmail.to/attachments/att_1?Expires=1&Signature=x"

ATTACHMENT_RECORD = {
    "attachment_id": "att_1",
    "filename": "invoice-1001.pdf",
    "content_type": "application/pdf",
    "size": len(PDF),
    "download_url": DOWNLOAD_URL,
    "text_url": "https://cdn.agentmail.to/extracted/att_1?Expires=1&Signature=x",
    "expires_at": "2026-09-18T20:46:23.890Z",
}


class _Response(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        super().__init__(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


@pytest.fixture
def seat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A seat with a read credential and an empty spool. Returns the list every
    GET's URL is appended to, so a test can assert what was asked for."""
    monkeypatch.setenv(broker.READ_KEY_ENV, "am_read_key")
    monkeypatch.setenv(spool.SPOOL_DIR_ENV, str(tmp_path / "spool"))
    # The seat's own inbox is cached per process; each test resolves it afresh.
    monkeypatch.setattr(broker, "_own_inbox_cache", None)
    urls: list[str] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> _Response:
        urls.append(request.full_url)
        if "agentmail.to/v0" in request.full_url or "api.agentmail.to" in request.full_url:
            assert request.headers.get("Authorization") == "Bearer am_read_key"
        if "/inboxes?" in request.full_url:
            return _Response(json.dumps({"inboxes": [{"inbox_id": SEAT_INBOX}]}).encode())
        if request.full_url == DOWNLOAD_URL:
            assert request.headers.get("Authorization") is None, "the signed link IS the credential"
            return _Response(PDF, "application/pdf")
        if request.full_url.endswith("/attachments/att_1"):
            return _Response(json.dumps(ATTACHMENT_RECORD).encode())
        return _Response(json.dumps(MESSAGE).encode())

    monkeypatch.setattr(broker.urllib.request, "urlopen", fake_urlopen)
    return urls


# ---------------------------------------------------------------------------
# list_attachments — the fact the event does not carry
# ---------------------------------------------------------------------------


def test_list_attachments_returns_what_the_message_carries(seat: list[str]) -> None:
    found = broker.list_attachments("msg_123")
    assert [a["attachment_id"] for a in found] == ["att_1", "att_2"]
    assert found[0] == {
        "attachment_id": "att_1",
        "filename": "invoice-1001.pdf",
        "content_type": "application/pdf",
        "size": 1983,
    }


def test_an_attachment_with_no_id_is_dropped(seat: list[str]) -> None:
    """It cannot be fetched, so offering it would only invite a failed call."""
    assert all(a["attachment_id"] for a in broker.list_attachments("m"))


def test_a_hostile_filename_is_reduced_before_the_model_sees_it(seat: list[str]) -> None:
    found = broker.list_attachments("m")
    assert found[1]["filename"] == "passwd"


def test_the_message_id_cannot_contribute_a_path_segment(seat: list[str]) -> None:
    """The message id reaches this module from a webhook payload or from the
    model. A message id of ``../../inboxes/other`` must read as a message id,
    not as a walk up the vendor's API."""
    broker.list_attachments("../../other")
    assert seat[-1] == (
        f"https://api.agentmail.to/v0/inboxes/{urllib.parse.quote(SEAT_INBOX, safe='')}"
        "/messages/..%2F..%2Fother"
    )


def test_the_inbox_is_the_seats_own_never_the_models(seat: list[str]) -> None:
    """2026-09-18: the first live run named the SENDER's inbox, the vendor
    answered 404, and the turn reported an unreadable file to that sender. The
    inbox is resolved from this seat's own key, and no argument can name one."""
    broker.list_attachments("msg_123")
    assert seat[0].startswith("https://api.agentmail.to/v0/inboxes?")
    assert urllib.parse.quote(SEAT_INBOX, safe="") in seat[-1]
    assert "message_id" in broker.list_attachments.__code__.co_varnames
    assert "inbox_id" not in broker.list_attachments.__code__.co_varnames


def test_a_key_covering_no_single_inbox_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero or several inboxes is a configuration fault, never a guess."""
    monkeypatch.setenv(broker.READ_KEY_ENV, "am_read_key")
    monkeypatch.setenv(spool.SPOOL_DIR_ENV, str(tmp_path / "spool"))
    broker._own_inbox_cache = None

    def fake_urlopen(request: Any, timeout: float | None = None) -> _Response:
        return _Response(json.dumps({"inboxes": [{"inbox_id": "a"}, {"inbox_id": "b"}]}).encode())

    monkeypatch.setattr(broker.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(broker.AgentMailReadError) as exc:
        broker.list_attachments("msg_123")
    assert "exactly one" in str(exc.value)


@pytest.mark.parametrize("args", ["", None])
def test_a_missing_id_refuses_before_any_call(seat: list[str], args: Any) -> None:
    with pytest.raises(broker.AgentMailReadError):
        broker.list_attachments(args)
    assert seat == []


def test_no_credential_means_a_named_refusal_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(broker.READ_KEY_ENV, raising=False)
    with pytest.raises(broker.AgentMailReadError) as exc:
        broker.list_attachments("m")
    assert broker.READ_KEY_ENV in str(exc.value)


# ---------------------------------------------------------------------------
# spool_attachment — bytes to the volume, a token to the model
# ---------------------------------------------------------------------------


def test_spool_attachment_returns_a_receipt_and_never_the_bytes(seat: list[str]) -> None:
    receipt = broker.spool_attachment("msg_123", "att_1")
    assert set(receipt) == {"spool_token", "filename", "content_type", "size", "sha256"}
    assert receipt["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert receipt["filename"] == "invoice-1001.pdf"
    assert receipt["content_type"] == "application/pdf"
    assert "%PDF" not in json.dumps(receipt)
    assert spool.read(receipt["spool_token"], expected_sha256=receipt["sha256"]) == PDF


def test_the_spooled_name_comes_from_the_token_not_the_vendor(
    seat: list[str], tmp_path: Path
) -> None:
    receipt = broker.spool_attachment("m", "att_1")
    written = sorted(p.name for p in (tmp_path / "spool").iterdir())
    assert written == [f"{receipt['spool_token']}.bin", f"{receipt['spool_token']}.json"]


def test_an_oversized_attachment_is_refused_and_nothing_is_spooled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(broker.READ_KEY_ENV, "am_read_key")
    monkeypatch.setenv(spool.SPOOL_DIR_ENV, str(tmp_path / "spool"))
    big = b"x" * (spool.MAX_SPOOL_BYTES + 1)
    monkeypatch.setattr(broker, "_own_inbox_cache", None)

    def fake_urlopen(request: Any, timeout: float | None = None) -> _Response:
        if "/inboxes?" in request.full_url:
            return _Response(json.dumps({"inboxes": [{"inbox_id": SEAT_INBOX}]}).encode())
        if request.full_url == DOWNLOAD_URL:
            return _Response(big, "application/pdf")
        if request.full_url.endswith("/attachments/att_1"):
            return _Response(json.dumps({**ATTACHMENT_RECORD, "size": len(big)}).encode())
        return _Response(json.dumps(MESSAGE).encode())

    monkeypatch.setattr(broker.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(broker.AgentMailReadError, match="limit"):
        broker.spool_attachment("m", "att_1")
    assert not (tmp_path / "spool").exists() or list((tmp_path / "spool").iterdir()) == []


def test_a_vendor_error_never_echoes_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(broker.READ_KEY_ENV, "am_secret_value")
    monkeypatch.setenv(spool.SPOOL_DIR_ENV, str(tmp_path / "spool"))

    def boom(request: Any, timeout: float | None = None) -> None:
        raise OSError("connection reset by am_secret_value")

    monkeypatch.setattr(broker.urllib.request, "urlopen", boom)
    with pytest.raises(broker.AgentMailReadError) as exc:
        broker.list_attachments("m")
    assert "am_secret_value" not in str(exc.value)


# ---------------------------------------------------------------------------
# The plugin surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def mail_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    """A seat that authors an enabled Email connector on AgentMail.

    The plugin's load gate asks the CAPABILITY question (does this seat have a
    mailbox?) rather than the vendor one (is AGENTMAIL_API_KEY set?), because
    the vendor question is what kept these tools off every Microsoft 365 seat.
    So a test that wants the tools has to give the seat a mailbox.
    """
    monkeypatch.setattr(email_adapter, "email_connector_enabled", lambda *_, **__: True)
    monkeypatch.setattr(
        email_adapter, "email_adapter", lambda *_, **__: email_adapter.ADAPTER_AGENTMAIL
    )


def test_the_plugin_registers_both_tools_on_a_seat_with_mail(
    fake_ctx: Any, mail_seat: None
) -> None:
    plugin = load_plugin("hermes-smd-mail-attachments")
    plugin.register(fake_ctx)
    assert set(fake_ctx.tools) == {
        "mail_list_attachments",
        "mail_spool_attachment",
        "mail_spool_message",
    }
    for entry in fake_ctx.tools.values():
        # NO requires_env, deliberately: a failing requires_env check drops the
        # tool from the resolved surface SILENTLY, leaving a turn with no way to
        # look and no way to know it could not. The handlers raise a named
        # reason instead.
        assert not entry.get("requires_env")
        # tool_registration's function shape: parameters must be nested, or the
        # model is advertised a tool it cannot pass an argument to.
        assert "parameters" in entry["schema"]
        assert entry["schema"]["parameters"]["type"] == "object"
        assert entry["schema"]["description"]


def test_a_seat_with_no_mail_gets_no_attachment_tools(fake_ctx: Any, monkeypatch) -> None:
    """The gate did not disappear when it moved off the vendor key."""
    monkeypatch.setattr(email_adapter, "email_connector_enabled", lambda *_, **__: False)
    plugin = load_plugin("hermes-smd-mail-attachments")
    plugin.register(fake_ctx)
    assert fake_ctx.tools == {}


def test_a_msgraph_seat_dispatches_to_the_graph_backend(fake_ctx: Any, monkeypatch) -> None:
    """The two tool NAMES are the same on both channels; the backend is not.

    ``vendor-invoice-intake`` and ``discovery-served-watch`` hardcode these
    names, so a seat's transport must not reach the skills.
    """
    monkeypatch.setattr(email_adapter, "email_connector_enabled", lambda *_, **__: True)
    monkeypatch.setattr(
        email_adapter, "email_adapter", lambda *_, **__: email_adapter.ADAPTER_MSGRAPH
    )
    seen: list[str] = []
    monkeypatch.setattr(msgraph_attachments, "list_attachments", lambda mid: seen.append(mid) or [])
    plugin = load_plugin("hermes-smd-mail-attachments")
    plugin.register(fake_ctx)
    json.loads(fake_ctx.tools["mail_list_attachments"]["handler"]({"message_id": "graph-id"}))
    assert seen == ["graph-id"], "a msgraph seat must not be served by the AgentMail branch"


def test_an_unreadable_seat_config_raises_rather_than_defaulting(
    fake_ctx: Any, monkeypatch
) -> None:
    """Unknown transport is a refusal, never a silent fall back to agentmail.

    A Graph seat quietly dispatched to AgentMail reports that its own mailbox
    has no attachments, which is the exact dead end these tools exist to close.
    """
    monkeypatch.setattr(email_adapter, "email_connector_enabled", lambda *_, **__: True)

    def boom(*_: Any, **__: Any) -> str:
        raise email_adapter.EmailAdapterUnreadable("customer.yaml is unreadable")

    monkeypatch.setattr(email_adapter, "email_adapter", boom)
    plugin = load_plugin("hermes-smd-mail-attachments")
    plugin.register(fake_ctx)
    with pytest.raises(email_adapter.EmailAdapterUnreadable):
        fake_ctx.tools["mail_list_attachments"]["handler"]({"message_id": "m"})


def test_the_spool_handler_returns_the_receipt_as_json(
    seat: list[str], fake_ctx: Any, mail_seat: None
) -> None:
    """End to end through the registered handler, which is the shape the model
    actually calls."""
    plugin = load_plugin("hermes-smd-mail-attachments")
    plugin.register(fake_ctx)
    handler = fake_ctx.tools["mail_spool_attachment"]["handler"]
    receipt = json.loads(handler({"message_id": "m", "attachment_id": "att_1"}))
    assert spool.TOKEN_RE.match(receipt["spool_token"])
    listed = json.loads(fake_ctx.tools["mail_list_attachments"]["handler"]({"message_id": "m"}))
    assert listed["count"] == 2


# ---------------------------------------------------------------------------
# The 2026-09-18 live defect: the record was spooled in place of the document
# ---------------------------------------------------------------------------


def test_the_bytes_come_from_the_link_not_the_record(seat: list[str]) -> None:
    """The vendor answers the attachment endpoint with a RECORD carrying a
    signed link. The first live run wrote that record to the spool -- 1187
    bytes of JSON where a 1983-byte PDF belonged -- and every later step called
    the invoice unreadable."""
    receipt = broker.spool_attachment("msg_123", "att_1")
    blob = (spool.spool_dir() / f"{receipt['spool_token']}.bin").read_bytes()
    assert blob == PDF
    assert blob.startswith(b"%PDF")
    assert receipt["size"] == len(PDF)
    assert DOWNLOAD_URL in seat


def test_a_length_that_disagrees_with_the_vendor_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheapest proof that what landed is not the document."""
    monkeypatch.setenv(broker.READ_KEY_ENV, "am_read_key")
    monkeypatch.setenv(spool.SPOOL_DIR_ENV, str(tmp_path / "spool"))
    monkeypatch.setattr(broker, "_own_inbox_cache", None)

    def fake_urlopen(request: Any, timeout: float | None = None) -> _Response:
        if "/inboxes?" in request.full_url:
            return _Response(json.dumps({"inboxes": [{"inbox_id": SEAT_INBOX}]}).encode())
        if request.full_url == DOWNLOAD_URL:
            return _Response(b"short", "application/pdf")
        return _Response(json.dumps(ATTACHMENT_RECORD).encode())

    monkeypatch.setattr(broker.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(broker.AgentMailReadError, match="bytes and"):
        broker.spool_attachment("msg_123", "att_1")
    assert not (tmp_path / "spool").exists() or list((tmp_path / "spool").iterdir()) == []


def test_a_link_off_the_vendors_hosts_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link arrives inside vendor-authored JSON, so its host is checked."""
    monkeypatch.setenv(broker.READ_KEY_ENV, "am_read_key")
    monkeypatch.setenv(spool.SPOOL_DIR_ENV, str(tmp_path / "spool"))
    monkeypatch.setattr(broker, "_own_inbox_cache", None)

    def fake_urlopen(request: Any, timeout: float | None = None) -> _Response:
        if "/inboxes?" in request.full_url:
            return _Response(json.dumps({"inboxes": [{"inbox_id": SEAT_INBOX}]}).encode())
        return _Response(
            json.dumps({**ATTACHMENT_RECORD, "download_url": "https://evil.example/x"}).encode()
        )

    monkeypatch.setattr(broker.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(broker.AgentMailReadError, match="not one the vendor serves"):
        broker.spool_attachment("msg_123", "att_1")
