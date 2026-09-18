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
from pathlib import Path
from typing import Any

import pytest

from shared import agentmail_broker as broker
from shared import attachment_spool as spool
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
    urls: list[str] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> _Response:
        urls.append(request.full_url)
        assert request.headers.get("Authorization") == "Bearer am_read_key"
        if request.full_url.endswith("/attachments/att_1"):
            return _Response(PDF, "application/pdf")
        return _Response(json.dumps(MESSAGE).encode())

    monkeypatch.setattr(broker.urllib.request, "urlopen", fake_urlopen)
    return urls


# ---------------------------------------------------------------------------
# list_attachments — the fact the event does not carry
# ---------------------------------------------------------------------------


def test_list_attachments_returns_what_the_message_carries(seat: list[str]) -> None:
    found = broker.list_attachments("inbox@seat.example", "msg_123")
    assert [a["attachment_id"] for a in found] == ["att_1", "att_2"]
    assert found[0] == {
        "attachment_id": "att_1",
        "filename": "invoice-1001.pdf",
        "content_type": "application/pdf",
        "size": 1983,
    }


def test_an_attachment_with_no_id_is_dropped(seat: list[str]) -> None:
    """It cannot be fetched, so offering it would only invite a failed call."""
    assert all(a["attachment_id"] for a in broker.list_attachments("i", "m"))


def test_a_hostile_filename_is_reduced_before_the_model_sees_it(seat: list[str]) -> None:
    found = broker.list_attachments("i", "m")
    assert found[1]["filename"] == "passwd"


def test_the_ids_cannot_contribute_a_path_segment(seat: list[str]) -> None:
    """Both ids reach this module from a webhook payload or from the model. A
    message id of ``../../inboxes/other`` must read as a message id, not as a
    walk up the vendor's API."""
    broker.list_attachments("inbox@seat.example", "../../other")
    assert seat[-1] == (
        "https://api.agentmail.to/v0/inboxes/inbox%40seat.example/messages/..%2F..%2Fother"
    )


@pytest.mark.parametrize("args", [("", "m"), ("i", ""), (None, "m")])
def test_a_missing_id_refuses_before_any_call(seat: list[str], args: tuple[Any, Any]) -> None:
    with pytest.raises(broker.AgentMailReadError):
        broker.list_attachments(*args)
    assert seat == []


def test_no_credential_means_a_named_refusal_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(broker.READ_KEY_ENV, raising=False)
    with pytest.raises(broker.AgentMailReadError) as exc:
        broker.list_attachments("i", "m")
    assert broker.READ_KEY_ENV in str(exc.value)


# ---------------------------------------------------------------------------
# spool_attachment — bytes to the volume, a token to the model
# ---------------------------------------------------------------------------


def test_spool_attachment_returns_a_receipt_and_never_the_bytes(seat: list[str]) -> None:
    receipt = broker.spool_attachment("inbox@seat.example", "msg_123", "att_1")
    assert set(receipt) == {"spool_token", "filename", "content_type", "size", "sha256"}
    assert receipt["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert receipt["filename"] == "invoice-1001.pdf"
    assert receipt["content_type"] == "application/pdf"
    assert "%PDF" not in json.dumps(receipt)
    assert spool.read(receipt["spool_token"], expected_sha256=receipt["sha256"]) == PDF


def test_the_spooled_name_comes_from_the_token_not_the_vendor(
    seat: list[str], tmp_path: Path
) -> None:
    receipt = broker.spool_attachment("i", "m", "att_1")
    written = sorted(p.name for p in (tmp_path / "spool").iterdir())
    assert written == [f"{receipt['spool_token']}.bin", f"{receipt['spool_token']}.json"]


def test_an_oversized_attachment_is_refused_and_nothing_is_spooled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(broker.READ_KEY_ENV, "am_read_key")
    monkeypatch.setenv(spool.SPOOL_DIR_ENV, str(tmp_path / "spool"))
    big = b"x" * (spool.MAX_SPOOL_BYTES + 1)

    def fake_urlopen(request: Any, timeout: float | None = None) -> _Response:
        if request.full_url.endswith("/attachments/att_1"):
            return _Response(big, "application/pdf")
        return _Response(json.dumps(MESSAGE).encode())

    monkeypatch.setattr(broker.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(broker.AgentMailReadError, match="limit"):
        broker.spool_attachment("i", "m", "att_1")
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
        broker.list_attachments("i", "m")
    assert "am_secret_value" not in str(exc.value)


# ---------------------------------------------------------------------------
# The plugin surface
# ---------------------------------------------------------------------------


def test_the_plugin_registers_both_tools_with_the_read_credential(fake_ctx: Any) -> None:
    plugin = load_plugin("hermes-smd-mail-attachments")
    plugin.register(fake_ctx)
    assert set(fake_ctx.tools) == {"mail_list_attachments", "mail_spool_attachment"}
    for entry in fake_ctx.tools.values():
        assert entry["requires_env"] == [broker.READ_KEY_ENV]
        # tool_registration's function shape: parameters must be nested, or the
        # model is advertised a tool it cannot pass an argument to.
        assert "parameters" in entry["schema"]
        assert entry["schema"]["parameters"]["type"] == "object"
        assert entry["schema"]["description"]


def test_the_spool_handler_returns_the_receipt_as_json(seat: list[str], fake_ctx: Any) -> None:
    """End to end through the registered handler, which is the shape the model
    actually calls."""
    plugin = load_plugin("hermes-smd-mail-attachments")
    plugin.register(fake_ctx)
    handler = fake_ctx.tools["mail_spool_attachment"]["handler"]
    receipt = json.loads(handler({"inbox_id": "i", "message_id": "m", "attachment_id": "att_1"}))
    assert spool.TOKEN_RE.match(receipt["spool_token"])
    listed = json.loads(
        fake_ctx.tools["mail_list_attachments"]["handler"]({"inbox_id": "i", "message_id": "m"})
    )
    assert listed["count"] == 2
