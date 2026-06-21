"""First-class Google Workspace tools backed by the local capability broker."""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.broker_audit import write_execution
from shared.tool_registration import register_wrapped_tool
from shared.workspace_broker import GRANT_ARG, execute

logger = logging.getLogger(__name__)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


STRING = {"type": "string"}
INTEGER = {"type": "integer", "minimum": 1, "maximum": 100}
STRINGS = {"type": "array", "items": STRING}
ROWS = {"type": "array", "items": {"type": "array"}}
# A managed mailbox the Operator is authored to act on (an executive assistant's
# delegated access). Omit to act on Crane's own mailbox. The broker fail-closes
# on any value not in the authored `google_auth.managed_mailboxes`.
MAILBOX = {
    "type": "string",
    "description": (
        "Managed mailbox address to act on (the authored primary, e.g. the "
        "principal's box). Omit to use Crane's own mailbox."
    ),
}
# The "Send mail as" identity for the From header when drafting in a managed
# mailbox. Must be one of that mailbox's authored send_as identities; the broker
# refuses any other value.
SEND_AS = {
    "type": "string",
    "description": (
        "Authored 'Send mail as' identity for the From header (managed mailbox "
        "only). For a reply, use the address the message was delivered to."
    ),
}

TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "workspace_gmail_search": (
        "Search a Gmail mailbox (Crane's own, or an authored managed mailbox).",
        _schema({"query": STRING, "max_results": INTEGER, "mailbox": MAILBOX}, ["query"]),
    ),
    "workspace_gmail_get": (
        "Read one Gmail message by ID.",
        _schema({"message_id": STRING, "mailbox": MAILBOX}, ["message_id"]),
    ),
    "workspace_gmail_create_draft": (
        "Create a Gmail draft without sending it (Crane's own or a managed mailbox).",
        _schema(
            {
                "to": STRING,
                "subject": STRING,
                "body": STRING,
                "thread_id": STRING,
                "mailbox": MAILBOX,
                "from": SEND_AS,
            },
            ["to", "subject", "body"],
        ),
    ),
    "workspace_gmail_modify": (
        "Add or remove Gmail labels.",
        _schema(
            {
                "message_id": STRING,
                "add_label_ids": STRINGS,
                "remove_label_ids": STRINGS,
                "mailbox": MAILBOX,
            },
            ["message_id"],
        ),
    ),
    "workspace_gmail_archive": (
        "Archive a Gmail message.",
        _schema({"message_id": STRING, "mailbox": MAILBOX}, ["message_id"]),
    ),
    "workspace_calendar_list": (
        "List Google Calendar events.",
        _schema(
            {
                "calendar_id": STRING,
                "time_min": STRING,
                "time_max": STRING,
                "query": STRING,
                "max_results": INTEGER,
            }
        ),
    ),
    "workspace_calendar_get": (
        "Read one Google Calendar event.",
        _schema({"event_id": STRING, "calendar_id": STRING}, ["event_id"]),
    ),
    "workspace_calendar_create_draft": (
        "Create a tentative event with no attendees or notifications.",
        _schema(
            {
                "title": STRING,
                "start": STRING,
                "end": STRING,
                "description": STRING,
                "location": STRING,
                "calendar_id": STRING,
            },
            ["title", "start", "end"],
        ),
    ),
    "workspace_calendar_update_draft": (
        "Update an event without sending notifications.",
        _schema(
            {
                "event_id": STRING,
                "calendar_id": STRING,
                "title": STRING,
                "start": STRING,
                "end": STRING,
                "description": STRING,
                "location": STRING,
            },
            ["event_id"],
        ),
    ),
    "workspace_drive_list": (
        "List Google Drive files.",
        _schema({"folder_id": STRING, "query": STRING, "max_results": INTEGER}),
    ),
    "workspace_drive_get": (
        "Read Google Drive file metadata.",
        _schema({"file_id": STRING}, ["file_id"]),
    ),
    "workspace_drive_export": (
        "Export a Google Drive file.",
        _schema({"file_id": STRING, "mime_type": STRING}, ["file_id"]),
    ),
    "workspace_docs_create": (
        "Create a Google Doc.",
        _schema({"title": STRING, "content": STRING}, ["title"]),
    ),
    "workspace_docs_get": (
        "Read a Google Doc.",
        _schema({"document_id": STRING}, ["document_id"]),
    ),
    "workspace_docs_append": (
        "Append text to a Google Doc.",
        _schema({"document_id": STRING, "text": STRING}, ["document_id", "text"]),
    ),
    "workspace_sheets_create": (
        "Create a Google Sheet.",
        _schema({"title": STRING}, ["title"]),
    ),
    "workspace_sheets_get_values": (
        "Read values from a Google Sheet range.",
        _schema({"spreadsheet_id": STRING, "range": STRING}, ["spreadsheet_id", "range"]),
    ),
    "workspace_sheets_update_values": (
        "Write values to a Google Sheet range.",
        _schema(
            {
                "spreadsheet_id": STRING,
                "range": STRING,
                "values": ROWS,
                "value_input_option": {
                    "type": "string",
                    "enum": ["RAW", "USER_ENTERED"],
                },
            },
            ["spreadsheet_id", "range", "values"],
        ),
    ),
}


def _handler(operation: str):
    def handle(args: dict[str, Any], **_: Any) -> str:
        grant = args.get(GRANT_ARG)
        if not isinstance(grant, str) or not grant:
            raise PermissionError("Workspace broker grant missing")
        payload = {key: value for key, value in args.items() if key != GRANT_ARG}
        response = execute(operation, payload, grant)
        receipt = response.get("receipt")
        if not isinstance(receipt, dict):
            raise RuntimeError("Workspace broker response missing receipt")
        write_execution(operation=operation, receipt=receipt)
        return json.dumps(response.get("result"), ensure_ascii=False)

    return handle


def register(ctx: Any) -> None:
    """Register every reviewed Workspace operation as its own Hermes tool."""
    for name, (description, schema) in TOOLS.items():
        register_wrapped_tool(
            ctx,
            name=name,
            toolset="workspace",
            schema=schema,
            handler=_handler(name),
            requires_env=["SMD_WORKSPACE_BROKER_SOCKET"],
            description=description,
            emoji="",
        )
    logger.info("hermes-smd-workspace registered %d mediated tools", len(TOOLS))
