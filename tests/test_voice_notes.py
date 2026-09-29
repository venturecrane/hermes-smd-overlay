"""``voice_note_transcribe``: a rostered person's voice memo, as their words.

Each test pins one rule and the direction that would be a defect if it
flipped: the roster is checked before any byte is read; a non-audio attachment
is skipped, not transcribed; a missing speech provider is a named refusal, not a
silent empty; the tool exists only where the seat has a mailbox.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from shared import agentmail_broker as broker
from shared import attachment_spool as spool
from shared import email_adapter
from shared.customer_config import CustomerConfig
from tests.conftest import load_plugin

MESSAGE_ID = "msg_voice_1"
ROSTERED = "tim@thebrokery.com"
STRANGER = "someone@elsewhere.example"

ATTACHMENTS = [
    {
        "attachment_id": "att_audio",
        "filename": "Open house.m4a",
        "content_type": "audio/x-m4a",
        "size": 40_000,
    },
    {
        "attachment_id": "att_pdf",
        "filename": "flyer.pdf",
        "content_type": "application/pdf",
        "size": 9_000,
    },
]


@pytest.fixture()
def mail_seat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(email_adapter, "email_connector_enabled", lambda *_, **__: True)
    monkeypatch.setattr(
        email_adapter, "email_adapter", lambda *_, **__: email_adapter.ADAPTER_AGENTMAIL
    )
    monkeypatch.setenv(spool.SPOOL_DIR_ENV, str(tmp_path / "spool"))
    cfg = CustomerConfig(
        {"customer_id": "scott", "scope": {"inbound_allow_from": [ROSTERED, "@firm.example"]}}
    )
    monkeypatch.setattr(CustomerConfig, "from_volume", classmethod(lambda cls, path=None: cfg))


def _wire_vendor(monkeypatch: pytest.MonkeyPatch, *, sender: str, spooled: list[str]) -> None:
    monkeypatch.setattr(broker, "message_sender", lambda mid: sender)
    monkeypatch.setattr(broker, "list_attachments", lambda mid: list(ATTACHMENTS))

    def fake_spool(mid: str, aid: str) -> dict[str, Any]:
        spooled.append(aid)
        return spool.write(
            b"RIFF fake audio", filename="Open house.m4a", content_type="audio/x-m4a"
        )

    monkeypatch.setattr(broker, "spool_attachment", fake_spool)


def test_a_rostered_senders_audio_is_transcribed_and_the_pdf_is_skipped(
    monkeypatch: pytest.MonkeyPatch, mail_seat: None
) -> None:
    plugin = load_plugin("hermes-smd-voice-notes")
    spooled: list[str] = []
    # message_sender already reduces "Tim Broker <Tim@TheBrokery.com>" to the bare
    # lowercase address (pinned below); the roster check sees only that.
    _wire_vendor(monkeypatch, sender=ROSTERED, spooled=spooled)
    seen_paths: list[str] = []

    def fake_transcribe(path: str) -> dict[str, Any]:
        seen_paths.append(path)
        return {
            "success": True,
            "transcript": " Tom and Maria, downsizing from Maple. ",
            "provider": "groq",
        }

    monkeypatch.setattr(plugin, "_transcribe", fake_transcribe)
    out = plugin.transcribe_message(MESSAGE_ID)
    assert out["sender"] == ROSTERED
    assert spooled == ["att_audio"], "only the audio attachment is spooled"
    assert out["skipped_non_audio"] == 1
    assert len(out["transcripts"]) == 1
    entry = out["transcripts"][0]
    assert entry["transcript"] == "Tom and Maria, downsizing from Maple."
    assert entry["provider"] == "groq"
    assert entry["filename"] == "Open house.m4a"
    assert seen_paths and Path(seen_paths[0]).exists(), "the transcriber got a real spooled path"


def test_a_stranger_is_refused_before_any_byte_is_read(
    monkeypatch: pytest.MonkeyPatch, mail_seat: None
) -> None:
    plugin = load_plugin("hermes-smd-voice-notes")
    spooled: list[str] = []
    _wire_vendor(monkeypatch, sender=STRANGER, spooled=spooled)
    listed: list[str] = []
    monkeypatch.setattr(broker, "list_attachments", lambda mid: listed.append(mid) or [])
    with pytest.raises(plugin.VoiceNoteRefused, match="not on scope.inbound_allow_from"):
        plugin.transcribe_message(MESSAGE_ID)
    assert listed == [] and spooled == [], "nothing was listed or spooled for a stranger"


def test_the_sender_read_is_the_bare_address_and_domain_grants_count(
    monkeypatch: pytest.MonkeyPatch, mail_seat: None
) -> None:
    plugin = load_plugin("hermes-smd-voice-notes")
    spooled: list[str] = []
    _wire_vendor(monkeypatch, sender="paralegal@firm.example", spooled=spooled)
    monkeypatch.setattr(broker, "list_attachments", lambda mid: [])
    out = plugin.transcribe_message(MESSAGE_ID)
    assert out["transcripts"] == [] and out["note"] == "no audio attachment on this message"


def test_a_missing_speech_provider_is_a_named_refusal(
    monkeypatch: pytest.MonkeyPatch, mail_seat: None
) -> None:
    plugin = load_plugin("hermes-smd-voice-notes")
    _wire_vendor(monkeypatch, sender=ROSTERED, spooled=[])
    monkeypatch.setattr(
        plugin,
        "_transcribe",
        lambda path: {"success": False, "transcript": "", "error": "No STT provider available."},
    )
    with pytest.raises(plugin.VoiceNoteRefused, match="No STT provider"):
        plugin.transcribe_message(MESSAGE_ID)


def test_a_non_agentmail_seat_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, mail_seat: None
) -> None:
    plugin = load_plugin("hermes-smd-voice-notes")
    monkeypatch.setattr(
        email_adapter, "email_adapter", lambda *_, **__: email_adapter.ADAPTER_MSGRAPH
    )
    read: list[str] = []
    monkeypatch.setattr(broker, "message_sender", lambda mid: read.append(mid) or ROSTERED)
    with pytest.raises(plugin.VoiceNoteRefused, match="no voice-note support yet"):
        plugin.transcribe_message(MESSAGE_ID)
    assert read == []


@pytest.mark.parametrize(
    ("ctype", "audio"),
    [
        ("audio/x-m4a", True),
        ("audio/mpeg; codecs=mp3", True),
        ("video/mp4", True),
        ("application/pdf", False),
        ("image/jpeg", False),
        ("", False),
    ],
)
def test_only_audio_shaped_content_types_count(ctype: str, audio: bool) -> None:
    plugin = load_plugin("hermes-smd-voice-notes")
    assert plugin._is_audio(ctype) is audio


def test_the_handler_returns_json_and_surfaces_refusals(
    monkeypatch: pytest.MonkeyPatch, mail_seat: None
) -> None:
    plugin = load_plugin("hermes-smd-voice-notes")
    _wire_vendor(monkeypatch, sender=ROSTERED, spooled=[])
    monkeypatch.setattr(
        plugin,
        "_transcribe",
        lambda path: {"success": True, "transcript": "hello", "provider": "local"},
    )
    out = json.loads(plugin._handler({"message_id": MESSAGE_ID}))
    assert out["transcripts"][0]["transcript"] == "hello"
    monkeypatch.setattr(broker, "message_sender", lambda mid: STRANGER)
    with pytest.raises(RuntimeError, match="not on scope.inbound_allow_from"):
        plugin._handler({"message_id": MESSAGE_ID})


def test_the_plugin_registers_on_a_seat_with_mail_and_not_otherwise(
    fake_ctx: Any, monkeypatch: pytest.MonkeyPatch, mail_seat: None
) -> None:
    plugin = load_plugin("hermes-smd-voice-notes")
    plugin.register(fake_ctx)
    assert set(fake_ctx.tools) == {"voice_note_transcribe"}
    entry = fake_ctx.tools["voice_note_transcribe"]
    assert not entry.get("requires_env")
    assert entry["toolset"] == "voice_notes"
    assert "parameters" in entry["schema"]
    monkeypatch.setattr(email_adapter, "email_connector_enabled", lambda *_, **__: False)
    fresh = load_plugin("hermes-smd-voice-notes")
    ctx2 = type(fake_ctx)()
    fresh.register(ctx2)
    assert ctx2.tools == {}


# ---------------------------------------------------------------------------
# the broker's sender read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Tim Broker <Tim@TheBrokery.com>", "tim@thebrokery.com"),
        ("tim@thebrokery.com", "tim@thebrokery.com"),
        ([{"address": "Tim@thebrokery.com"}], "tim@thebrokery.com"),
    ],
)
def test_message_sender_reduces_the_from_field_to_a_bare_address(
    monkeypatch: pytest.MonkeyPatch, raw: Any, expected: str
) -> None:
    monkeypatch.setattr(broker, "own_inbox", lambda: "agentcrane@agentmail.to")
    monkeypatch.setattr(
        broker,
        "_get",
        lambda path, *, accept: (json.dumps({"from": raw}).encode(), "application/json"),
    )
    assert broker.message_sender("msg_1") == expected


def test_message_sender_refuses_a_message_with_no_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker, "own_inbox", lambda: "agentcrane@agentmail.to")
    monkeypatch.setattr(broker, "_get", lambda path, *, accept: (b"{}", "application/json"))
    with pytest.raises(broker.AgentMailReadError, match="no sender"):
        broker.message_sender("msg_1")
