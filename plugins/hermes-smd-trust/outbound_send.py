"""Out-of-band AgentMail send for the confirm round-trip (ADR 0071 / #1806 harden).

When the owner approves a withheld send over Telegram, the OVERLAY dispatches the
send itself rather than waiting for the LLM to re-invoke the send tool (which it
does not reliably do — it sometimes reasons/investigates instead). The overlay is
deterministic; the model is not.

**This module no longer transmits.** Since ss#2258 the AgentMail path is a
broker verb: the agent process holds no send-capable AgentMail credential, so
what remains here is payload shaping and error typing. The caller (the trust
plugin) still authorizes through ``evaluate_tool_call`` first — taint-gate,
content-floor, fabrication scan, confirm-approval — and the broker then applies
an INDEPENDENT recipient fence from the seat's own customer.yaml before it uses
the key. Two checks, in two processes, because four fabricated messages once
reached a real client principal by way of a path that consulted neither.

The msgraph half is behind the same broker seam now. It gets the recipient fence
and the broker-written row, but NOT the vendor half of the AgentMail story: a
Graph app-only token is always ``/.default``, so there is no send-incapable
credential to leave the agent with, and it legitimately needs Graph for reads.
``shared/msgraph_broker`` states that limit and what would close it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from shared import agentmail_broker, msgraph_broker

logger = logging.getLogger(__name__)

# The only AgentMail send-body fields the overlay forwards from the stored
# payload. Anything else the agent may have put on the tool args (internal keys,
# the stripped approval flag, a broker grant) is NOT forwarded — the wire body is
# built from a closed allowlist so nothing unexpected reaches the send API.
_SEND_BODY_FIELDS: tuple[str, ...] = ("to", "cc", "bcc", "subject", "text", "html", "reply_to")


class OutboundSendError(RuntimeError):
    """An out-of-band confirm-dispatch send failed, for any transport. The caller
    is exception-safe and audits the failure (CONFIRM_SEND_FAILED) rather than
    crashing the hook."""


class AgentMailSendError(OutboundSendError):
    """A raw AgentMail send/list call failed."""


class MsGraphSendError(OutboundSendError):
    """A Microsoft Graph confirm-dispatch send failed (bad creds or a Graph 4xx/5xx)."""


# ss#2258: `_request_json`, `resolve_inbox_id` and `seat_inbox_address` were
# DELETED here, not merely left unused. They resolved WHICH MAILBOX THIS SEAT
# SENDS AS, inside the agent process, from an account-wide listing — and a bug in
# exactly that logic had every seat ready to send as whichever inbox happened to
# be created most recently. Inbox identity is now decided by the broker from the
# seat's own customer.yaml, where the agent cannot reach it. Leaving a dead copy
# here would invite a future caller to resolve identity agent-side again.


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
    payload: dict[str, Any],
    sender: Callable[..., Any] | None = None,
) -> str:
    """Ask the broker to send a fresh message; return the new message id.

    Was a direct ``POST /v0/inboxes/{inbox_id}/messages/send`` carrying an
    account-wide Bearer token. Both halves of that were the ss#2258 defect: the
    token could reach any inbox in the account, and the recipient check lived in
    this process — the one an unaudited path had already proven it could skip.

    Now the broker owns all three decisions this function used to make or trust:
    which inbox the message comes FROM (pinned from the seat's own customer.yaml),
    who it may go TO (the seat's authored counterparty surface), and whether an
    audit row exists (it writes one before returning). ``payload`` still carries
    only the stored, approved send args; the broker applies its own closed body
    allowlist on top, so nothing extra rides the wire either.

    ``sender`` is injectable for tests. Raises :class:`AgentMailSendError` on
    refusal or transport failure alike — the caller's contract is unchanged.
    """
    body = _send_body(payload)
    if not body.get("to"):
        raise AgentMailSendError("refusing to send: payload has no recipient")
    send = sender or agentmail_broker.send_message
    try:
        message_id = send(body)
    except agentmail_broker.BrokerError as exc:
        # A refusal the broker made and recorded. Its message names the reason
        # (an unauthored recipient, a blocked domain), which is far more useful
        # to the operator than a generic delivery failure.
        raise AgentMailSendError(f"broker refused the send: {exc}") from exc
    except agentmail_broker.AgentMailBrokerUnavailable as exc:
        raise AgentMailSendError(f"broker transmit unavailable: {exc}") from exc
    if not isinstance(message_id, str) or not message_id:
        # A 2xx with no id is still a successful send per AgentMail; surface a
        # placeholder so the caller can audit delivery without failing the turn.
        logger.warning("hermes-smd-trust: agentmail send returned no message_id; body kept minimal")
        return "(sent, id unavailable)"
    return message_id


# The send-body fields forwarded to the broker's Graph verb. Two payload shapes
# reach it and both are covered here: the out-of-band confirm dispatch carries the
# flat `mcp_msgraph_mail_send_message` args (`body_text`, ADR 0078 D4), while the
# `smd_send_message` tool carries what its schema advertises (`text`, `html`,
# `bcc`, `reply_to`). Anything else on the args is NOT forwarded — the wire body is
# built from a closed allowlist at both ends.
_MSGRAPH_SEND_FIELDS: tuple[str, ...] = (
    "to",
    "cc",
    "bcc",
    "subject",
    "body_text",
    "text",
    "html",
    "reply_to",
)


def send_via_msgraph(
    payload: dict[str, Any],
    *,
    sender: Callable[..., Any] | None = None,
) -> str:
    """Ask the broker to send an approved message via Graph ``/sendMail``.

    Was a direct Graph call built from ``MSGRAPH_*`` in this process. The mailbox
    was already pinned there, so the identity half was sound — what was missing is
    the half the incident turned on: nothing checked the RECIPIENT outside the
    process that chose it, and nothing wrote a row that the sender could not skip.
    Both now happen in the broker, against the seat's own customer.yaml.

    The honest limit, because this reads like its AgentMail sibling and is not:
    the agent still holds ``MSGRAPH_*`` for the delta poller and its mail tools,
    and Graph app-only auth has no send-incapable credential to give it. So this
    fences the path the seat TAKES, not every path that exists. See
    ``shared/msgraph_broker`` for what closes the rest.

    ``sender`` is injectable for tests. Raises :class:`MsGraphSendError` on
    refusal or transport failure alike — the caller's contract is unchanged.
    """
    body = {
        k: payload.get(k) for k in _MSGRAPH_SEND_FIELDS if payload.get(k) not in (None, "", [], {})
    }
    if not body.get("to"):
        raise MsGraphSendError("refusing to send: payload has no recipient")
    send = sender or msgraph_broker.send_message
    try:
        send(body)
    except msgraph_broker.BrokerError as exc:
        # A refusal the broker made and recorded. Its message names the reason
        # (an unauthored recipient, a blocked domain), which is far more useful
        # to the operator than a generic delivery failure.
        raise MsGraphSendError(f"broker refused the send: {exc}") from exc
    except msgraph_broker.MsGraphBrokerUnavailable as exc:
        raise MsGraphSendError(f"broker transmit unavailable: {exc}") from exc
    # Graph answers sendMail with 202 and no body, so no id exists to return —
    # unchanged by the reseam, and the reason the audit row leans on its digest.
    return "(sent via msgraph, id unavailable)"
