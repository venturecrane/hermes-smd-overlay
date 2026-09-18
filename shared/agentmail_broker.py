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


#: The seat's own inbox, resolved from its own inbox-scoped key and cached for
#: the process. THE MODEL MUST NOT NAME AN INBOX. On 2026-09-18 the first live
#: run asked for the SENDER's inbox (the address it could see on the event) and
#: the vendor answered 404, which the turn then reported to that sender as an
#: unreadable file. A message exists only in the mailbox that holds it, and this
#: seat has exactly one, so the id is ours to know rather than the model's to
#: supply.
_own_inbox_cache: str | None = None


def own_inbox() -> str:
    """The one inbox this seat's read key covers.

    Asks the vendor which inboxes the key can see. Exactly one is the authored
    shape (provisioning gives a seat one inbox and an inbox-scoped key); zero or
    several is a configuration fault and refuses loudly rather than guessing.
    """
    global _own_inbox_cache
    if _own_inbox_cache:
        return _own_inbox_cache
    body, _ = _get("/inboxes?limit=10", accept="application/json")
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise AgentMailReadError("agentmail returned an inbox list that is not JSON") from exc
    raw = parsed.get("inboxes") if isinstance(parsed, dict) else None
    ids = [
        str(entry.get("inbox_id"))
        for entry in (raw if isinstance(raw, list) else [])
        if isinstance(entry, dict) and entry.get("inbox_id")
    ]
    if len(ids) != 1:
        raise AgentMailReadError(
            f"this seat's mail key covers {len(ids)} inboxes; exactly one is required to read its own mail"
        )
    _own_inbox_cache = ids[0]
    return _own_inbox_cache


def list_attachments(message_id: str) -> list[dict[str, Any]]:
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
    if not isinstance(message_id, str) or not message_id.strip():
        raise AgentMailReadError("message_id is required")
    body, _ = _get(_message_path(own_inbox(), message_id.strip()), accept="application/json")
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


#: Hosts the vendor's own attachment links live on. The signed URL is the
#: credential, so no Authorization header is sent and no redirect is followed.
DOWNLOAD_HOSTS = ("cdn.agentmail.to", "download.agentmail.to")


def _download(url: str) -> bytes:
    """Fetch the bytes behind one vendor-minted, time-limited attachment link.

    The link comes back inside the attachment record, so it is vendor-authored:
    the host is checked against a closed list before anything is fetched, and
    the read stops one byte past the spool ceiling so an oversized file never
    lands in memory whole.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in DOWNLOAD_HOSTS:
        raise AgentMailReadError(
            f"attachment link host {parsed.hostname!r} is not one the vendor serves attachments from"
        )
    req = urllib.request.Request(url, method="GET")  # noqa: S310 - host checked above
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=READ_TIMEOUT_SECONDS) as response:  # noqa: S310
            blob = response.read(attachment_spool.MAX_SPOOL_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise AgentMailReadError(f"attachment download failed: HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - the reason is reported, never a credential
        raise AgentMailReadError(f"attachment download failed: {exc.__class__.__name__}") from exc
    if len(blob) > attachment_spool.MAX_SPOOL_BYTES:
        raise AgentMailReadError(
            f"attachment is over the {attachment_spool.MAX_SPOOL_BYTES}-byte limit; it is not spooled"
        )
    return blob


def spool_attachment(message_id: str, attachment_id: str) -> dict[str, Any]:
    """Fetch one attachment's bytes and leave them in the seat-local spool.

    The vendor returns raw bytes to an authenticated caller; it mints no
    download URL, which is why nothing here hands one back. The return is the
    spool receipt — ``spool_token``, ``filename``, ``content_type``, ``size``,
    ``sha256`` — and the records connector turns that token back into bytes on
    the same machine. The model never sees the attachment's content and never
    sees a credential.
    """
    for label, value in (("message_id", message_id), ("attachment_id", attachment_id)):
        if not isinstance(value, str) or not value.strip():
            raise AgentMailReadError(f"{label} is required")
    path = _message_path(own_inbox(), message_id.strip(), "attachments", attachment_id.strip())
    receipt_body, _ = _get(path, accept="application/json")
    try:
        receipt = json.loads(receipt_body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise AgentMailReadError(
            "agentmail returned an attachment record that is not JSON"
        ) from exc
    if not isinstance(receipt, dict):
        raise AgentMailReadError("agentmail returned an attachment record that is not an object")
    url = receipt.get("download_url")
    if not isinstance(url, str) or not url.strip():
        raise AgentMailReadError(
            "agentmail's attachment record carries no download_url; the bytes cannot be fetched"
        )
    blob = _download(url.strip())
    stated = receipt.get("size")
    if isinstance(stated, int) and stated != len(blob):
        # 2026-09-18: the first live spool wrote this JSON record itself, 1187
        # bytes of it, in place of the 1983-byte PDF, and every downstream step
        # reported "unsupported format" about a file that was never fetched. A
        # length that disagrees with the vendor's own number is the cheapest
        # possible proof that what landed is not the document.
        raise AgentMailReadError(
            f"agentmail said the attachment is {stated} bytes and {len(blob)} arrived; it is not spooled"
        )
    return attachment_spool.write(
        blob,
        filename=receipt.get("filename"),
        content_type=str(receipt.get("content_type") or ""),
    )


__all__ = [
    "AgentMailBrokerUnavailable",
    "DOWNLOAD_HOSTS",
    "own_inbox",
    "AgentMailReadError",
    "BrokerError",
    "list_attachments",
    "send_message",
    "send_reply",
    "spool_attachment",
    "transmit_available",
]
