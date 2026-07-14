"""Out-of-band AgentMail send for the confirm round-trip (ADR 0071 / #1806 harden).

When the owner approves a withheld send over Telegram, the OVERLAY dispatches the
send itself rather than waiting for the LLM to re-invoke the send tool (which it
does not reliably do — it sometimes reasons/investigates instead). The overlay is
deterministic; the model is not.

**This module is the transport only** — the raw AgentMail REST call. It does NOT
decide whether a send is allowed. The caller (the trust plugin) authorizes the
payload through the SAME ``evaluate_tool_call`` gate first (taint-gate,
content-floor, fabrication scan, confirm-approval), and only calls here when the
gate returns allow. So the out-of-band path inherits the full safety envelope of
the tool path; the only thing that changes is who executes the send.

Mirrors ``hermes-smd-reply/relay.py`` (the reply channel's AgentMail transport):
same base URL, Bearer auth, ``urllib`` with an injectable ``opener`` for tests,
and a typed error the exception-safe caller audits. The one difference is the
endpoint — a fresh send (``/inboxes/{id}/messages/send``) rather than a threaded
reply (``/inboxes/{id}/messages/{mid}/reply``).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Verified against AgentMail docs (api-reference/inboxes/messages/send +
# inboxes/list) and the reply plugin's transport, 2026-07-14.
AGENTMAIL_API_BASE = "https://api.agentmail.to/v0"
_SEND_TIMEOUT_S = 10.0

# The only AgentMail send-body fields the overlay forwards from the stored
# payload. Anything else the agent may have put on the tool args (internal keys,
# the stripped approval flag, a broker grant) is NOT forwarded — the wire body is
# built from a closed allowlist so nothing unexpected reaches the send API.
_SEND_BODY_FIELDS: tuple[str, ...] = ("to", "cc", "bcc", "subject", "text", "html", "reply_to")

# Cache the resolved primary inbox id for the process (single tenant per Machine;
# the inbox is stable for the life of the seat). Resolved lazily on first send.
_INBOX_ID: str | None = None


class AgentMailSendError(RuntimeError):
    """A raw AgentMail send/list call failed. The caller is exception-safe and
    audits the failure (send_failed) rather than crashing the hook."""


def _request_json(
    url: str,
    *,
    api_key: str,
    method: str,
    body: dict[str, Any] | None,
    timeout_s: float,
    opener: Callable[..., Any] | None,
) -> Any:
    """One authenticated AgentMail REST call → parsed JSON (or {} on empty body).

    Raises :class:`AgentMailSendError` on any HTTP / transport / decode failure.
    ``opener`` is injectable for tests (defaults to ``urllib.request.urlopen``)."""
    data = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    _open = opener or urllib.request.urlopen
    try:
        with _open(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise AgentMailSendError(f"agentmail {method} returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AgentMailSendError(f"agentmail {method} unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise AgentMailSendError(f"agentmail {method} timed out after {timeout_s}s") from exc
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AgentMailSendError("agentmail returned undecodable JSON") from exc


def resolve_inbox_id(
    api_key: str,
    *,
    base_url: str = AGENTMAIL_API_BASE,
    timeout_s: float = _SEND_TIMEOUT_S,
    opener: Callable[..., Any] | None = None,
    _refresh: bool = False,
) -> str:
    """The primary inbox id for this seat (``GET /v0/inboxes``), cached.

    Single tenant per Machine, so the first inbox is the agent's own. Raises
    :class:`AgentMailSendError` if the list call fails or returns no inbox."""
    global _INBOX_ID
    if _INBOX_ID and not _refresh:
        return _INBOX_ID
    parsed = _request_json(
        base_url + "/inboxes", api_key=api_key, method="GET", body=None, timeout_s=timeout_s, opener=opener
    )
    inboxes = parsed.get("inboxes") if isinstance(parsed, dict) else None
    if not isinstance(inboxes, list) or not inboxes:
        raise AgentMailSendError("agentmail returned no inboxes")
    first = inboxes[0]
    inbox_id = first.get("inbox_id") if isinstance(first, dict) else None
    if not isinstance(inbox_id, str) or not inbox_id:
        raise AgentMailSendError("agentmail inbox has no inbox_id")
    _INBOX_ID = inbox_id
    return inbox_id


def _send_body(payload: dict[str, Any]) -> dict[str, Any]:
    """The AgentMail send body from a stored send payload — allowlisted fields only."""
    body: dict[str, Any] = {}
    for key in _SEND_BODY_FIELDS:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            body[key] = value
    return body


def send_message(
    *,
    api_key: str,
    inbox_id: str,
    payload: dict[str, Any],
    base_url: str = AGENTMAIL_API_BASE,
    timeout_s: float = _SEND_TIMEOUT_S,
    opener: Callable[..., Any] | None = None,
) -> str:
    """POST a fresh send via ``/v0/inboxes/{inbox_id}/messages/send``; return the
    new message id. ``payload`` is the STORED (approved) send args; only the
    allowlisted body fields are forwarded. Raises :class:`AgentMailSendError`."""
    body = _send_body(payload)
    if not body.get("to"):
        raise AgentMailSendError("refusing to send: payload has no recipient")
    url = f"{base_url}/inboxes/{urllib.parse.quote(inbox_id, safe='')}/messages/send"
    parsed = _request_json(
        url, api_key=api_key, method="POST", body=body, timeout_s=timeout_s, opener=opener
    )
    message_id = parsed.get("message_id") if isinstance(parsed, dict) else None
    if not isinstance(message_id, str) or not message_id:
        # A 2xx with no id is still a successful send per AgentMail; surface a
        # placeholder so the caller can audit delivery without failing the turn.
        logger.warning("hermes-smd-trust: agentmail send returned no message_id; body kept minimal")
        return "(sent, id unavailable)"
    return message_id
