"""Client for the broker's AgentMail transmit verbs (ss#2258).

WHY THIS REPLACED A DIRECT REST CALL. On four days in 2026-08 a rehearsal seat
sent fabricated email to a real client principal and produced **no audit row for
any of it**. Zero rows means the sending path never traversed the trust hook — so
the recipient check, which lives in this process, was never consulted. No control
we could add HERE would have helped, because the credential that did the sending
also lives here, and a credential in the agent's address space answers to whatever
reaches it.

So transmit moved out. The workspace broker (uid 10001, root-launched) now holds
the only send-capable AgentMail key, and it decides:

* **who may be written to** — the union of what the seat's own customer.yaml
  names, read from the copy the broker trusts, never from this request;
* **who the message is from** — pinned from that same config, so an inbox
  identity is not something this module can express, let alone get wrong;
* **that a row exists** — written broker-side before this call returns, so a
  transmit with no ledger entry is no longer a reachable state.

What this module keeps is the part that legitimately belongs to the agent: the
content, and the decision to try. Everything about authority moved.

The TRANSMIT functions below deliberately take NO api_key. There is nothing to
pass — the gateway's AgentMail key is inbox-scoped with
``message_send``/``draft_send`` withheld, so it could not transmit even if this
code tried.

READS ARE A DIFFERENT QUESTION, AND THEY STAY HERE (2026-09-18). The attachment
verbs at the bottom of this module call the mail vendor's REST API directly with
that same inbox-scoped ``AGENTMAIL_API_KEY``. That is not a partial reversal of
ss#2258: what moved out was AUTHORITY TO TRANSMIT, and the credential these
verbs use has none — the vendor refuses it. The seat's own entrypoint states the
split in the same words ("what the gateway inherits is AGENTMAIL_API_KEY, the
inbox-scoped key the vendor refuses to let transmit"), and the mailbox this key
reads is the agent's own. Reading its attachments is the same class of act as
the mail MCP server's message reads, which run on this key today.

Neither verb returns bytes and neither returns the key. The bytes land in the
seat-local spool (``shared.attachment_spool``) and the agent carries a token.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from shared import attachment_spool
from shared.secrets import get_secret
from shared.workspace_broker import BrokerError, request

SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"

#: Longer than the broker's own 15s vendor timeout so a slow AgentMail response
#: surfaces as the broker's typed error (which carries a reason and an audit row)
#: rather than as a socket timeout here (which carries neither).
SEND_TIMEOUT_SECONDS = 30.0


class AgentMailBrokerUnavailable(RuntimeError):
    """The broker could not be reached. NOT a refusal — the outcome is unknown."""


def transmit_available() -> bool:
    """Whether a transmit path exists at all on this seat.

    False on a seat with no broker socket configured. Callers use this to fail
    closed with a clear reason instead of raising from deep in a send.
    """
    return bool(os.environ.get(SOCKET_ENV, "").strip())


def _call(
    action: str,
    payload: dict[str, Any],
    *,
    session_id: str = "",
    matter_ref: str | None = None,
    audit_extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not transmit_available():
        raise AgentMailBrokerUnavailable(
            f"{SOCKET_ENV} is unset; this seat has no broker transmit path"
        )
    # ss-console#2497. The broker writes the CONFIRM_SEND_* row and cannot know
    # either fact — it does not run in the agent's process and has no session.
    # They ride BESIDE ``payload``, never inside it: the payload is what reaches
    # the vendor, and the broker builds the wire body from a closed allowlist, so
    # an audit field placed there would be silently dropped. Both are OPTIONAL on
    # the wire so the two sides deploy in either order (the same argument the
    # ss#2489 ``html`` field makes): a broker that predates them ignores unknown
    # request keys and writes exactly the row it writes today.
    envelope: dict[str, Any] = {"action": action, "payload": payload}
    if session_id:
        envelope["session_id"] = session_id
    if matter_ref:
        envelope["matter_ref"] = matter_ref
    if audit_extra:
        # WS-RENDER: the body-conformance stamps (routing_leg /
        # rendered_body_sha256 / body_variant) ride the same seam as the two
        # joins above and get the same deploy-order freedom — the broker
        # filters them through its own closed allowlist.
        envelope["audit_extra"] = audit_extra
    try:
        return request(envelope, timeout=SEND_TIMEOUT_SECONDS)
    except OSError as exc:
        # Transport-level (OSError covers socket timeouts: TimeoutError has
        # subclassed it since 3.10). The broker may or may not have sent. Distinguished
        # from BrokerError (a decision the broker made and recorded) because
        # reporting "you may not write to this person" when the truth is "the
        # socket timed out" would be a lie in the ledger's own language.
        raise AgentMailBrokerUnavailable(f"broker unreachable: {exc}") from exc


def send_message(
    payload: dict[str, Any],
    *,
    session_id: str = "",
    matter_ref: str | None = None,
    audit_extra: dict[str, str] | None = None,
) -> str:
    """Transmit a fresh message; return the AgentMail message id.

    ``payload`` carries only content and recipients — the broker applies the
    recipient fence and pins the From. Raises :class:`BrokerError` when the
    broker refuses (an authored-policy decision, already audited there) and
    :class:`AgentMailBrokerUnavailable` when it could not be asked.
    """
    return str(
        _call(
            "agentmail_send",
            payload,
            session_id=session_id,
            matter_ref=matter_ref,
            audit_extra=audit_extra,
        ).get("message_id")
        or ""
    )


def send_reply(
    message_id: str,
    text: str = "",
    html: str = "",
    *,
    session_id: str = "",
    matter_ref: str | None = None,
) -> str:
    """Reply to an inbound message; return the new message id.

    The recipient is structural — AgentMail threads the reply to the original
    sender — and the broker independently re-fetches that message to check the
    sender against ``inbound_allow_from``. This module cannot name the recipient,
    which is the point: anyone on the internet can email a seat's inbox.
    """
    body: dict[str, Any] = {"message_id": message_id}
    if text:
        body["text"] = text
    if html:
        body["html"] = html
    return str(
        _call("agentmail_reply", body, session_id=session_id, matter_ref=matter_ref).get(
            "message_id"
        )
        or ""
    )


# ---------------------------------------------------------------------------
# Attachment READS — the inbox-scoped key, and never a byte to the model.
# ---------------------------------------------------------------------------

#: The vendor's REST API (NOT the MCP gateway at mcp.agentmail.to, which
#: authenticates with ``x-api-key``). Same base and same Bearer scheme the reply
#: relay documents.
API_BASE = "https://api.agentmail.to/v0"
READ_KEY_ENV = "AGENTMAIL_API_KEY"
READ_TIMEOUT_SECONDS = 20.0


class AgentMailReadError(RuntimeError):
    """An attachment read could not be performed. Never a policy refusal."""


def _read_key() -> str:
    try:
        key = get_secret(READ_KEY_ENV)
    except KeyError as exc:
        raise AgentMailReadError(
            f"{READ_KEY_ENV} is not set; this seat cannot read its own mail"
        ) from exc
    if not key.strip():
        raise AgentMailReadError(f"{READ_KEY_ENV} is empty; this seat cannot read its own mail")
    return key


def _message_path(inbox_id: str, message_id: str, *parts: str) -> str:
    """Path-safe URL for one message under one inbox.

    Every segment is percent-encoded with ``safe=""``. Both ids reach this
    module from a webhook payload or from the model, so neither may contribute
    a path segment of its own.
    """
    segments = ["inboxes", inbox_id, "messages", message_id, *parts]
    return "/" + "/".join(urllib.parse.quote(str(s), safe="") for s in segments)


def _get(path: str, *, accept: str) -> tuple[bytes, str]:
    """One authenticated GET. Returns ``(body, content_type)``.

    Reads at most ``MAX_SPOOL_BYTES + 1`` bytes so an oversized attachment is
    refused by the caller without the whole thing landing in memory first.
    """
    req = urllib.request.Request(  # noqa: S310 - API_BASE is an https module literal; both ids are percent-encoded
        API_BASE + path,
        method="GET",
        headers={"Authorization": f"Bearer {_read_key()}", "Accept": accept},
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=READ_TIMEOUT_SECONDS) as response:  # noqa: S310
            body = response.read(attachment_spool.MAX_SPOOL_BYTES + 1)
            ctype = str(response.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as exc:
        raise AgentMailReadError(f"agentmail GET {path} failed: HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - the reason is reported, never the key
        raise AgentMailReadError(f"agentmail GET {path} failed: {exc.__class__.__name__}") from exc
    return body, ctype


def list_attachments(inbox_id: str, message_id: str) -> list[dict[str, Any]]:
    """Every attachment the vendor holds for one message.

    THE EVENT DOES NOT CARRY THEM. A ``message.received`` webhook payload has no
    ``attachments`` key, so a turn driven by that event cannot know an
    attachment exists. This asks the vendor's own copy of the message, which
    does carry them.

    Returns one dict per attachment with ``attachment_id``, ``filename``,
    ``content_type`` and ``size``. Every value is VENDOR-AUTHORED text — a
    filename is chosen by whoever sent the mail — so this tool's result is
    fenced and taints, exactly like reading the body would.
    """
    for label, value in (("inbox_id", inbox_id), ("message_id", message_id)):
        if not isinstance(value, str) or not value.strip():
            raise AgentMailReadError(f"{label} is required")
    body, _ = _get(_message_path(inbox_id.strip(), message_id.strip()), accept="application/json")
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise AgentMailReadError("agentmail returned a message that is not JSON") from exc
    raw = parsed.get("attachments") if isinstance(parsed, dict) else None
    found: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        attachment_id = entry.get("attachment_id") or entry.get("attachmentId")
        if not isinstance(attachment_id, str) or not attachment_id:
            continue
        size = entry.get("size")
        found.append(
            {
                "attachment_id": attachment_id,
                "filename": attachment_spool.safe_filename(entry.get("filename")),
                "content_type": str(entry.get("content_type") or entry.get("contentType") or ""),
                "size": size if isinstance(size, int) else None,
            }
        )
    return found


def spool_attachment(inbox_id: str, message_id: str, attachment_id: str) -> dict[str, Any]:
    """Fetch one attachment's bytes and leave them in the seat-local spool.

    The vendor returns raw bytes to an authenticated caller; it mints no
    download URL, which is why nothing here hands one back. The return is the
    spool receipt — ``spool_token``, ``filename``, ``content_type``, ``size``,
    ``sha256`` — and the records connector turns that token back into bytes on
    the same machine. The model never sees the attachment's content and never
    sees a credential.
    """
    for label, value in (
        ("inbox_id", inbox_id),
        ("message_id", message_id),
        ("attachment_id", attachment_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise AgentMailReadError(f"{label} is required")
    path = _message_path(inbox_id.strip(), message_id.strip(), "attachments", attachment_id.strip())
    blob, ctype = _get(path, accept="application/octet-stream")
    if len(blob) > attachment_spool.MAX_SPOOL_BYTES:
        raise AgentMailReadError(
            f"attachment is over the {attachment_spool.MAX_SPOOL_BYTES}-byte limit; it is not spooled"
        )
    meta = {a["attachment_id"]: a for a in list_attachments(inbox_id, message_id)}.get(
        attachment_id.strip(), {}
    )
    return attachment_spool.write(
        blob,
        filename=meta.get("filename"),
        content_type=meta.get("content_type") or ctype.split(";")[0],
    )


__all__ = [
    "AgentMailBrokerUnavailable",
    "AgentMailReadError",
    "BrokerError",
    "list_attachments",
    "send_message",
    "send_reply",
    "spool_attachment",
    "transmit_available",
]
