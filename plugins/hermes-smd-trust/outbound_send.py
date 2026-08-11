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
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from shared import msgraph_client

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

# Cache the resolved inbox id for the process, KEYED BY the address it resolved,
# so a cache hit can never answer for a different address than the caller asked
# about. Resolved lazily on first send.
_INBOX_ID_BY_ADDRESS: dict[str, str] = {}

# The seat's own inbox address. Authored value wins; absent that, the convention
# is <slug>@agentmail.to. The convention is a DEFAULT, never a guarantee — inboxes
# are created out of band (provision-customer.sh's agentmail block says so), so the
# resolved address is always checked against the account listing and a miss is
# fatal (ss#2258).
_INBOX_ADDRESS_ENV = "AGENTMAIL_INBOX_ADDRESS"
_SLUG_ENVS: tuple[str, ...] = ("SMD_CUSTOMER_SLUG", "CUSTOMER_SLUG")
_AGENTMAIL_DOMAIN = "agentmail.to"


class OutboundSendError(RuntimeError):
    """An out-of-band confirm-dispatch send failed, for any transport. The caller
    is exception-safe and audits the failure (CONFIRM_SEND_FAILED) rather than
    crashing the hook."""


class AgentMailSendError(OutboundSendError):
    """A raw AgentMail send/list call failed."""


class MsGraphSendError(OutboundSendError):
    """A Microsoft Graph confirm-dispatch send failed (bad creds or a Graph 4xx/5xx)."""


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
    """THIS SEAT'S OWN inbox id (``GET /v0/inboxes``), matched by address, cached.

    Previously this took ``inboxes[0]`` on the reasoning "single tenant per
    Machine, so the first inbox is the agent's own". That was false in production
    and dangerous (ss#2258). ``AGENTMAIL_API_KEY`` is account-wide — provisioning
    says so in as many words ("It can reach the shared account's OTHER inboxes
    (cross-tenant)") — and on 2026-08-11 the listing held EIGHT inboxes ordered
    newest-first: probe inboxes, simulation inboxes, other ventures' inboxes, and
    the pilot seat's own at index SIX. So ``inboxes[0]`` was whichever inbox
    somebody created most recently.

    The caller is ``_dispatch_approved_send``, which fires the moment a human
    approves a draft — and a client seat's day-one posture is
    ``external_send: draft_for_review``. So the unfixed path sends every approved
    letter from an arbitrary mailbox, and the moment a new client's inbox is
    created it becomes ``inboxes[0]`` and every OTHER seat starts sending as that
    client. It had never bitten only because this dispatch path had never fired.

    Resolution now: the authored ``AGENTMAIL_INBOX_ADDRESS`` if set, else the
    ``<slug>@agentmail.to`` convention. Either way the address MUST appear in the
    account listing — a miss raises rather than falling back, because sending from
    the wrong firm's mailbox is worse than not sending.
    """
    address = seat_inbox_address()
    if not address:
        raise AgentMailSendError(
            "cannot resolve this seat's inbox address: neither "
            f"{_INBOX_ADDRESS_ENV} nor a customer slug is set. Refusing to guess "
            "which mailbox to send from"
        )
    if not _refresh:
        cached = _INBOX_ID_BY_ADDRESS.get(address)
        if cached:
            return cached
    parsed = _request_json(
        base_url + "/inboxes",
        api_key=api_key,
        method="GET",
        body=None,
        timeout_s=timeout_s,
        opener=opener,
    )
    inboxes = parsed.get("inboxes") if isinstance(parsed, dict) else None
    if not isinstance(inboxes, list) or not inboxes:
        raise AgentMailSendError("agentmail returned no inboxes")
    for entry in inboxes:
        if not isinstance(entry, dict):
            continue
        inbox_id = entry.get("inbox_id")
        if not isinstance(inbox_id, str) or inbox_id.lower() != address.lower():
            continue
        _INBOX_ID_BY_ADDRESS[address] = inbox_id
        return inbox_id
    # Fail closed. The account listing is not this seat's to interpret: if the
    # address it owns is not there, something is wrong with provisioning, and the
    # safe outcome is a refused send the caller audits — never a send from a
    # mailbox that belongs to someone else.
    raise AgentMailSendError(
        f"this seat's inbox {address!r} is not in the AgentMail account listing "
        f"({len(inboxes)} inbox(es) visible); refusing to send from another "
        "inbox (ss#2258)"
    )


def seat_inbox_address() -> str | None:
    """The address this seat is entitled to send from, or None when unknowable.

    Authored value wins so a seat whose inbox does not follow the convention can
    still be pinned without a code change.
    """
    authored = (os.environ.get(_INBOX_ADDRESS_ENV) or "").strip()
    if authored:
        return authored
    for env_name in _SLUG_ENVS:
        slug = (os.environ.get(env_name) or "").strip()
        if slug:
            return f"{slug}@{_AGENTMAIL_DOMAIN}"
    return None


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


# The flat send-body fields the overlay forwards to Graph from the stored msgraph
# payload (mcp_msgraph_mail_send_message args are flat, ADR 0078 D4). Anything
# else on the args is NOT forwarded — the wire body is built from a closed
# allowlist. cc may be absent; body_text carries the reply/send prose.
_MSGRAPH_SEND_FIELDS: tuple[str, ...] = ("to", "cc", "subject", "body_text")


def send_via_msgraph(payload: dict[str, Any]) -> str:
    """POST an approved send via Microsoft Graph ``/users/{mailbox}/sendMail``.

    The msgraph counterpart of :func:`send_message`: the transport only — the
    caller has already re-authorized the payload through the same
    ``evaluate_tool_call`` gate. Builds the Graph client from ``MSGRAPH_*`` (via
    the shared client's env builder) so the mailbox is pinned and no arg can
    redirect the send. Fail-closed: a seat with no ``MSGRAPH_*`` creds raises
    :class:`MsGraphSendError` — it NEVER falls back to AgentMail. Graph returns
    202 with no id, so a placeholder is surfaced for the audit row."""
    client = msgraph_client.build_client_from_env()
    if client is None:
        raise MsGraphSendError("msgraph send unavailable: MSGRAPH_* env not configured")
    body = {
        k: payload.get(k) for k in _MSGRAPH_SEND_FIELDS if payload.get(k) not in (None, "", [], {})
    }
    if not body.get("to"):
        raise MsGraphSendError("refusing to send: payload has no recipient")
    try:
        client.send_mail(
            to=body["to"],
            subject=str(body.get("subject") or ""),
            body_text=str(body.get("body_text") or ""),
            cc=body.get("cc"),
        )
    except (msgraph_client.MsGraphApiError, msgraph_client.MsGraphAuthError) as exc:
        raise MsGraphSendError(f"graph sendMail failed: {exc}") from exc
    return "(sent via msgraph, id unavailable)"
