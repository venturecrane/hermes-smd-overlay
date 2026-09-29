"""``voice_note_transcribe``: a rostered person's voice memo, as their words.

WHY THIS PLUGIN EXISTS (ss-console#2793, the open-house add-on). A real estate
agent walks out of an open house and talks into their phone. The recording
arrives on the seat as an email attachment. Two things stood between that file
and a visitor record:

1. The only way a turn could reach an attachment was ``mail_list_attachments``
   + ``mail_spool_attachment``, and both are FENCED reads that TAINT the session
   (a filename is sender-chosen text). A tainted turn cannot write a record
   store (``hermes-smd-record-store`` refuses it, by design) and the reply
   relay withholds the reply. So the very act of looking at the memo would
   have made it impossible to keep.
2. The transcript needs a speech-to-text provider, and the turn needs a tool
   that runs it on a spooled file.

This tool closes both. It takes ONLY a message id. It asks the vendor who sent
the message and refuses, before touching any bytes, unless that address is on
the seat's own roster (``scope.inbound_allow_from``, the same list that already
decides whose email body is trusted content). Then it spools every audio
attachment on the message, transcribes each through Hermes' own speech-to-text
path (``tools.transcription_tools.transcribe_audio``: local faster-whisper, or
Groq / OpenAI / Mistral / xAI by key), and returns the transcripts. The model
never sees a filename it did not already trust the author of, never sees an
attachment id, and never carries bytes.

WHY THE RESULT IS UNFENCED. A rostered sender's email BODY already arrives as
internal-class content (the webhook router marks it so). Their voice, spoken
into the same message, is the same person's words on the same channel. The
roster check happens inside the tool, before the read, and an unrostered sender
gets a refusal with no content at all, so there is no path by which an
outsider's audio becomes untainted text. The filenames in the result are
reduced by ``attachment_spool.safe_filename`` and belong to the rostered
sender.

WHAT IT IS NOT. Not a provenance source: a transcript is dictation, and the
open-house skill writes it into a record store, whose READ is the source. Not a
general audio tool: it refuses non-audio attachments and anything over the
spool ceiling, and it transcribes only what a person the seat answers has sent.

Exception-safe is NOT the contract for the handler (that rule governs hooks): a
tool that cannot transcribe must say so to the model, so it raises a reason
Hermes surfaces. No reason string ever carries a credential.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from shared import agentmail_broker, attachment_spool, email_adapter
from shared.customer_config import CustomerConfig, CustomerConfigError
from shared.tool_registration import register_wrapped_tool

logger = logging.getLogger(__name__)

TOOL_NAME = "voice_note_transcribe"

#: Content types this tool will hand to the transcriber. iPhone Voice Memos
#: ships ``audio/x-m4a`` or ``audio/mp4``; Android recorders ship ``audio/mpeg``,
#: ``audio/ogg``, ``audio/webm``, ``audio/wav``; mail clients sometimes label an
#: m4a ``video/mp4``. Anything else is not a voice note.
AUDIO_PREFIXES: tuple[str, ...] = ("audio/",)
AUDIO_EXACT: frozenset[str] = frozenset({"video/mp4", "application/ogg", "video/webm"})

#: Every tool this plugin registers. The completeness suites read this.
TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    TOOL_NAME: (
        "Transcribe the voice recordings attached to ONE message from a person "
        "this seat answers (a rostered sender), and return each recording's text. "
        "Give it the message id from the inbound event; it finds the audio "
        "attachments itself. It refuses, with no content, when the sender is not "
        "on the seat's roster, when an attachment is not audio, or when no "
        "speech-to-text provider is configured, and it says which. The transcript "
        "is the sender's own words: keep them as dictation, do not tidy them.",
        {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The message id (on the inbound event).",
                },
            },
            "required": ["message_id"],
            "additionalProperties": False,
        },
    ),
}


class VoiceNoteRefused(RuntimeError):
    """The tool refused before or after reading; the message says why."""


def _transcribe(path: str) -> dict[str, Any]:
    """Hermes' own speech-to-text path. Imported lazily: the module lives in the
    Hermes tree, which tests do not have, and they monkeypatch this seam."""
    from tools.transcription_tools import transcribe_audio

    return transcribe_audio(path)


def _is_audio(content_type: str) -> bool:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    return ctype.startswith(AUDIO_PREFIXES) or ctype in AUDIO_EXACT


#: The extensions Hermes' transcriber accepts (transcription_tools
#: SUPPORTED_FORMATS at the pinned ref), and the content types that map onto
#: them when the sender's filename carries none. iPhone Voice Memos arrive as
#: audio/x-m4a or audio/mp4; some clients label an m4a video/mp4.
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac"}
)
_CONTENT_TYPE_EXT: dict[str, str] = {
    "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}


def _audio_suffix(filename: Any, content_type: str) -> str:
    """The extension to hand the transcriber: the sender's own when it is one
    Hermes accepts, else the one the content type implies, else ``.m4a`` (the
    phone default; the decoder sniffs the container anyway)."""
    name = attachment_spool.safe_filename(filename)
    ext = Path(name).suffix.lower()
    if ext in AUDIO_EXTENSIONS:
        return ext
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXT.get(ctype, ".m4a")


def _typed_link(spooled: Path, entry: dict[str, Any]) -> Path:
    """A hard link to the spooled bytes, in the same directory, with the
    recording's real extension. No second copy of the audio, no path the model
    ever sees, and the spool's own prune still owns the bytes."""
    suffix = _audio_suffix(entry.get("filename"), str(entry.get("content_type") or ""))
    typed = spooled.with_name(f"{spooled.stem}-audio{suffix}")
    try:
        typed.unlink()
    except OSError:
        pass
    try:
        os.link(spooled, typed)
    except OSError:
        shutil.copyfile(spooled, typed)
    return typed


def _sender_is_rostered(message_id: str) -> str:
    """The sender's address, only if the seat answers that address. Raises otherwise."""
    sender = agentmail_broker.message_sender(message_id)
    try:
        cfg = CustomerConfig.from_volume()
    except CustomerConfigError as exc:
        raise VoiceNoteRefused(f"customer.yaml unreadable ({exc}); refusing to read audio") from exc
    if not cfg.sender_on_roster(sender):
        raise VoiceNoteRefused(
            "refused: the sender of that message is not on scope.inbound_allow_from, so "
            "their recording is not this seat's to transcribe"
        )
    return sender


def transcribe_message(message_id: str) -> dict[str, Any]:
    """Every audio attachment on a rostered sender's message, as text."""
    if email_adapter.email_adapter() != email_adapter.ADAPTER_AGENTMAIL:
        raise VoiceNoteRefused(
            "this seat's mail adapter has no voice-note support yet; the recording was NOT read"
        )
    sender = _sender_is_rostered(message_id)
    attachments = agentmail_broker.list_attachments(message_id)
    audio = [a for a in attachments if _is_audio(str(a.get("content_type") or ""))]
    skipped = len(attachments) - len(audio)
    if not audio:
        return {
            "message_id": message_id,
            "sender": sender,
            "transcripts": [],
            "skipped_non_audio": skipped,
            "note": "no audio attachment on this message",
        }
    transcripts: list[dict[str, Any]] = []
    for entry in audio:
        receipt = agentmail_broker.spool_attachment(message_id, str(entry["attachment_id"]))
        token = str(receipt.get("spool_token") or "")
        path: Path = attachment_spool.resolve(token)
        # Hermes accepts audio BY FILE EXTENSION (transcription_tools
        # SUPPORTED_FORMATS) and the spool stores bytes as <token>.bin, so the
        # spooled path itself would be refused as "Unsupported format: .bin".
        # Hand the transcriber a same-directory hard link that carries the
        # recording's real extension, and remove it afterwards whatever happens.
        typed = _typed_link(path, entry)
        try:
            result = _transcribe(str(typed))
        finally:
            try:
                typed.unlink()
            except OSError:
                pass
        if not result.get("success"):
            raise VoiceNoteRefused(
                "could not transcribe "
                f"{attachment_spool.safe_filename(entry.get('filename'))}: "
                f"{result.get('error') or 'no speech-to-text provider configured'}"
            )
        transcripts.append(
            {
                "filename": attachment_spool.safe_filename(entry.get("filename")),
                "content_type": str(entry.get("content_type") or ""),
                "size": entry.get("size"),
                "provider": result.get("provider"),
                "transcript": str(result.get("transcript") or "").strip(),
            }
        )
    return {
        "message_id": message_id,
        "sender": sender,
        "transcripts": transcripts,
        "skipped_non_audio": skipped,
    }


def _handler(args: dict[str, Any], **_: Any) -> str:
    try:
        out = transcribe_message(str(args.get("message_id") or ""))
    except (VoiceNoteRefused, agentmail_broker.AgentMailReadError) as exc:
        raise RuntimeError(str(exc)) from exc
    return json.dumps(out, ensure_ascii=False)


def register(ctx: Any) -> None:
    """Register the tool on any seat that has a mailbox.

    The capability question, not the vendor one, and no ``requires_env``: a
    seat without a speech provider still gets the tool, whose handler then
    says exactly that, rather than a silently absent tool that reads as "the
    message carried no recording".
    """
    if not email_adapter.email_connector_enabled():
        logger.info("hermes-smd-voice-notes: no enabled Email connector on this seat; no tool")
        return
    description, schema = TOOLS[TOOL_NAME]
    register_wrapped_tool(
        ctx,
        name=TOOL_NAME,
        toolset="voice_notes",
        schema=schema,
        handler=_handler,
        description=description,
        emoji="",
    )
    logger.info("hermes-smd-voice-notes registered %s", TOOL_NAME)


__all__ = ["TOOLS", "TOOL_NAME", "VoiceNoteRefused", "register", "transcribe_message"]
