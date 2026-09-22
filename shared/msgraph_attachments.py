"""The seat's own mailbox attachments, over Microsoft Graph.

The msgraph sibling of ``agentmail_broker``'s ``list_attachments`` /
``spool_attachment``. Same two jobs, same spool contract, same receipt shape, so
the two tools a skill calls mean the same thing on either channel and no skill
has to know which mail vendor a seat runs.

WHY IT DID NOT ALREADY EXIST. The attachment tools were built on 2026-09-18 for
the vendor the seats ran then, and their plugin declares ``AGENTMAIL_API_KEY``,
so on a seat that authors ``adapter: msgraph`` the plugin does not load and the
tools are simply absent. A firm on Microsoft 365 therefore had a mailbox its
Operator could read and attachments it could not, which from inside a turn looks
exactly like a message that arrived without any.

THE CREDENTIAL IS THE READ APP, AND ONLY EVER THE READ APP. A seat carries two
Graph registrations: the agent holds the read one (Mail.ReadWrite, no
Mail.Send), and the send-capable one reaches the workspace broker alone, its
secret stripped from the agent's environment before the exec-drop. Everything
here rides :class:`shared.msgraph_client.MsGraphClient`, which is built from
``MSGRAPH_*`` and is the read app by construction. Nothing in this module can
transmit, and a seat whose read app was ever granted Mail.Send fails
``msgraph-read-app-cannot-send-probe`` at boot.

WHAT THE MODEL GETS, AND WHAT IT DOES NOT. It gets a filename, a content type,
a size and a token. It never gets the bytes, which take the filesystem instead
(see ``shared.attachment_spool`` for the layout that is the contract with the
records connector), and it never gets a credential. Both tools are fenced reads:
a filename is written by whoever sent the mail, so it is inbound data exactly as
the body is, and it taints the session.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from shared import attachment_spool, msgraph_client


class MsGraphAttachmentError(RuntimeError):
    """An attachment could not be listed, fetched, or spooled.

    Raised rather than returned. A tool that cannot read must say so to the
    model: the failure this whole path exists to close was a turn reporting
    "your message arrived without any attachments" when the truth was that the
    read was unreachable.
    """


#: The ``@odata.type`` values whose bytes are a document.
#:
#: ``fileAttachment`` is a file someone attached. The other two are not, and
#: both are refused BY NAME rather than fetched and inspected:
#:
#: ``itemAttachment``       an Outlook item (an email, an event) embedded in the
#:                          message. ``$value`` on it returns MIME, not the
#:                          document, so spooling it would file an .eml dressed
#:                          as the client's letter.
#: ``referenceAttachment``  a link to OneDrive or SharePoint. There are no bytes
#:                          on the message at all; the content lives behind a
#:                          separate authorization this seat may not hold.
FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"
_REFUSED_TYPES = {
    "#microsoft.graph.itemattachment": (
        "it is an embedded Outlook item, not a file: its bytes are MIME, not the document"
    ),
    "#microsoft.graph.referenceattachment": (
        "it is a link to cloud storage, not a file: the message carries no bytes for it"
    ),
}

#: The fields the list asks for, and the ONLY fields it hands back. Graph's
#: attachment collection includes ``contentBytes`` (the whole file, base64) in
#: the default representation, and this tool's result is fenced and goes to the
#: model, so a scanned letter would land in the turn, the transcript, and the
#: provider. The ``$select`` asks Graph not to send it; the allowlist below is
#: what makes that request non-load-bearing.
_LIST_SELECT = "id,name,contentType,size,isInline"

#: Graph ids are base64url-ish and long. They reach this module FROM THE MODEL
#: on a tainted turn, and they are the first such values to reach
#: ``MsGraphClient``, whose url builder passes what it is given straight into
#: the path (its own docstring says so). So the shape is checked here before a
#: url exists, and every segment is percent-encoded after that: a value that
#: cannot be a Graph id must never get the chance to be a path segment.
_ID_RE = re.compile(r"^[A-Za-z0-9_=+/\-]{1,1024}$")


def _client() -> msgraph_client.MsGraphClient:
    """The seat's Graph read client, or refuse by name.

    ``build_client_from_env`` returns ``None`` and logs a WARNING when a
    credential is unset. A warning is not a channel to the model and, on this
    fleet, not a channel to anyone: the alert floor is ERROR. An absent
    credential has to arrive as a refusal the turn can say out loud, or the seat
    reports a message with no attachments for the second time in its life and
    for a different reason.
    """
    client = msgraph_client.build_client_from_env()
    if client is None:
        raise MsGraphAttachmentError(
            "this seat has no Microsoft Graph mail credential, so its own mailbox cannot be read; "
            "the message was NOT checked for attachments"
        )
    return client


def _checked_id(value: Any, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise MsGraphAttachmentError(f"{label} is required")
    if not _ID_RE.match(text):
        raise MsGraphAttachmentError(f"{label} is not the shape of a Graph id")
    return text


def _attachments_path(message_id: str, attachment_id: str = "", *, value: bool = False) -> str:
    segments = ["messages", message_id, "attachments"]
    if attachment_id:
        segments.append(attachment_id)
    quoted = "/".join(urllib.parse.quote(s, safe="") for s in segments)
    return f"{quoted}/$value" if value else quoted


def _entry_type(entry: dict[str, Any]) -> str:
    raw = entry.get("@odata.type")
    return raw.strip().lower() if isinstance(raw, str) else ""


def _refusal_for(odata_type: str) -> str:
    """Why this attachment's bytes are not a document, or ``""`` if they are.

    An UNREADABLE type is refused, not accepted. Whether Graph returns the type
    annotation for a derived type under a narrowing ``$select`` is a property of
    Graph, not of this code, and the safe default when the answer is unknown is
    the one that does not spool a MIME blob as if it were the letter.
    """
    if not odata_type:
        return "its type could not be read, so it cannot be shown to be a file"
    if odata_type in _REFUSED_TYPES:
        return _REFUSED_TYPES[odata_type]
    if odata_type != FILE_ATTACHMENT_TYPE.lower():
        return f"its type is {odata_type}, which is not a file attachment"
    return ""


def _summarize(entry: dict[str, Any]) -> dict[str, Any] | None:
    """One attachment, rebuilt FIELD BY FIELD from an allowlist.

    Never a filtered copy of Graph's object and never Graph's object itself.
    Constructing the result is what makes the ``$select`` above an optimisation
    rather than a security control: if Graph ignores it and sends
    ``contentBytes``, nothing here has a key to put it in.
    """
    attachment_id = entry.get("id")
    if not isinstance(attachment_id, str) or not attachment_id:
        return None
    size = entry.get("size")
    return {
        "attachment_id": attachment_id,
        "filename": attachment_spool.safe_filename(entry.get("name")),
        "content_type": str(entry.get("contentType") or ""),
        "size": size if isinstance(size, int) else None,
        "is_inline": bool(entry.get("isInline")),
        "refused": _refusal_for(_entry_type(entry)),
    }


def list_attachments(message_id: str) -> list[dict[str, Any]]:
    """Every attachment Graph holds for one message in this seat's own mailbox.

    THE EVENT DOES NOT CARRY THEM, on this channel either: the inbound message
    the poller re-injects is a fixed set of fields with no attachment key, so a
    turn driven by it cannot know an attachment exists. This asks Graph.

    Each entry carries ``attachment_id``, ``filename``, ``content_type``,
    ``size``, ``is_inline`` and ``refused``. ``refused`` is a REASON, not a
    boolean: an empty string means the bytes are a file and can be spooled,
    and anything else is the sentence explaining why they are not, so a turn can
    tell someone what arrived instead of silently skipping it.

    ``is_inline`` is reported rather than filtered. A signature logo is inline
    and worth skipping; a scan pasted into the body is also inline and is the
    whole document. Which one a message holds is not this layer's judgement.

    Every value is written by whoever sent the mail, so this result is fenced
    and taints the session exactly as reading the body would.
    """
    message = _checked_id(message_id, "message_id")
    client = _client()
    url = client.mail_url(_attachments_path(message))
    try:
        raw = client.request("GET", url, params={"$select": _LIST_SELECT})
    except msgraph_client.MsGraphApiError as exc:
        raise MsGraphAttachmentError(
            f"the message's attachments could not be listed: {exc}"
        ) from exc
    entries = raw.get("value") if isinstance(raw, dict) else None
    found: list[dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        summary = _summarize(entry)
        if summary is not None:
            found.append(summary)
    return found


def spool_attachment(message_id: str, attachment_id: str) -> dict[str, Any]:
    """Fetch ONE attachment's bytes server-side and leave them in the spool.

    Returns the spool receipt: ``spool_token``, ``filename``, ``content_type``,
    ``size``, ``sha256``. Never the bytes.

    Three checks happen BEFORE any bytes move, because each one is cheaper and
    more truthful than discovering the same thing from a malformed file later:
    the metadata read settles the ``@odata.type`` (so a refused type costs one
    small JSON call, not a download), it settles ``size`` against the spool
    ceiling, and it gives the sanitised filename the receipt carries.

    Then the bytes, then the length cross-check. A short read and a complete
    small file are indistinguishable from the bytes alone, and
    ``attachment_spool.write`` would hash whatever arrived and hand back a
    receipt that looks perfectly healthy. The AgentMail side added the same
    check on 2026-09-18 after spooling 1187 bytes of a JSON record in place of a
    1983-byte PDF, and every step downstream reported "unsupported format" about
    a file that had never been fetched. A truncated document filed as a client's
    record is worse than a fetch that failed out loud.
    """
    message = _checked_id(message_id, "message_id")
    attachment = _checked_id(attachment_id, "attachment_id")
    client = _client()

    meta_url = client.mail_url(_attachments_path(message, attachment))
    try:
        meta = client.request("GET", meta_url, params={"$select": _LIST_SELECT})
    except msgraph_client.MsGraphApiError as exc:
        raise MsGraphAttachmentError(f"the attachment could not be read: {exc}") from exc
    if not isinstance(meta, dict):
        raise MsGraphAttachmentError("Graph returned an attachment record that is not an object")

    refusal = _refusal_for(_entry_type(meta))
    if refusal:
        raise MsGraphAttachmentError(f"this attachment is not spooled because {refusal}")

    stated = meta.get("size")
    if isinstance(stated, int) and stated > attachment_spool.MAX_SPOOL_BYTES:
        raise MsGraphAttachmentError(
            f"Graph says the attachment is {stated} bytes, over the "
            f"{attachment_spool.MAX_SPOOL_BYTES}-byte spool limit; it is not spooled"
        )

    value_url = client.mail_url(_attachments_path(message, attachment, value=True))
    try:
        blob = client.request_bytes("GET", value_url, max_bytes=attachment_spool.MAX_SPOOL_BYTES)
    except msgraph_client.MsGraphApiError as exc:
        raise MsGraphAttachmentError(f"the attachment's bytes could not be fetched: {exc}") from exc
    if len(blob) > attachment_spool.MAX_SPOOL_BYTES:
        raise MsGraphAttachmentError(
            f"the attachment is over the {attachment_spool.MAX_SPOOL_BYTES}-byte limit; it is not spooled"
        )
    if isinstance(stated, int) and stated != len(blob):
        raise MsGraphAttachmentError(
            f"Graph said the attachment is {stated} bytes and {len(blob)} arrived; it is not spooled"
        )

    return attachment_spool.write(
        blob,
        filename=meta.get("name"),
        content_type=str(meta.get("contentType") or ""),
    )


__all__ = [
    "FILE_ATTACHMENT_TYPE",
    "MsGraphAttachmentError",
    "list_attachments",
    "spool_attachment",
]
