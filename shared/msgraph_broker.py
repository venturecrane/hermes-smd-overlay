"""Client for the broker's Microsoft Graph transmit verbs (ss#2258, msgraph wave).

The msgraph sibling of ``agentmail_broker``. Same reason, same shape: transmit is
a decision about authority, and authority does not belong in the process the
model can steer. The broker fences the recipient against the seat's own
customer.yaml, pins the mailbox from that same file, and writes the audit row
before it answers — so a transmit with no ledger entry stops being a reachable
state on this channel too.

ONE DIFFERENCE, AND IT IS NOT COSMETIC. ``agentmail_broker`` can say the agent
holds no send-capable credential, because AgentMail issues per-inbox keys with a
permission whitelist. Microsoft Graph does not: an app-only token is always
``/.default`` — every application permission its app registration holds — and the
agent legitimately needs Graph credentials for the inbound delta poller and its
own mail tools. So on this channel the sentence is narrower and stays narrower
until a second, read-only app registration exists in the tenant:

    every send the seat MAKES goes through the broker and is fenced and audited;
    a rogue path that mints its own token can still reach Graph directly.

Do not paper over that in a docstring, a PR description, or an audit answer. The
mechanism that would close it is a tenant-admin action (a send-only app whose
secret only ever reaches the broker's 0600 file), which on a client seat is the
client's to grant.
"""

from __future__ import annotations

import os
from typing import Any

from shared.workspace_broker import BrokerError, request

SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"

#: Longer than the broker's own 15s Graph timeout so a slow Graph response
#: surfaces as the broker's typed error (which carries a reason and an audit row)
#: rather than as a socket timeout here (which carries neither).
SEND_TIMEOUT_SECONDS = 30.0


class MsGraphBrokerUnavailable(RuntimeError):
    """The broker could not be reached. NOT a refusal — the outcome is unknown."""


def transmit_available() -> bool:
    """Whether a broker transmit path exists at all on this seat."""
    return bool(os.environ.get(SOCKET_ENV, "").strip())


def _call(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not transmit_available():
        raise MsGraphBrokerUnavailable(
            f"{SOCKET_ENV} is unset; this seat has no broker transmit path"
        )
    try:
        return request({"action": action, "payload": payload}, timeout=SEND_TIMEOUT_SECONDS)
    except OSError as exc:
        # Transport-level (OSError covers socket timeouts: TimeoutError has
        # subclassed it since 3.10). The broker may or may not have sent.
        # Distinguished from BrokerError — a decision the broker made and
        # recorded — because reporting "you may not write to this person" when
        # the truth is "the socket timed out" would be a lie in the ledger's own
        # language.
        raise MsGraphBrokerUnavailable(f"broker unreachable: {exc}") from exc


def send_message(payload: dict[str, Any]) -> str:
    """Transmit a fresh message via Graph ``/sendMail``.

    ``payload`` carries only content and recipients, flat (``to``/``cc``/
    ``subject``/``body_text``) — the shape the gateway's gate already saw. The
    broker applies the recipient fence and pins the From.

    Graph answers ``sendMail`` with 202 and no body, so there is no vendor
    message id to return; the caller surfaces a placeholder and the audit row's
    ``input_digest`` is what identifies the transmit.
    """
    return str(_call("msgraph_send", payload).get("message_id") or "")


def send_reply(message_id: str, comment: str, *, html: str = "") -> str:
    """Reply in-thread to an inbound Graph message.

    The recipient is structural — Graph derives it from the source message — and
    the broker independently re-fetches that message to check its sender against
    ``inbound_allow_from``. This module cannot name the recipient, which is the
    point: anyone on the internet can email the operator mailbox.

    ``html`` (ss#2489) carries the rendered body. It is OPTIONAL on the wire so
    the two sides can be deployed in either order: a broker that predates the
    field ignores it and replies exactly as it does today, and a caller that
    sends none gets today's plain ``comment``. ``comment`` still rides along
    even when ``html`` is present — the broker keeps it as the plain-text
    fallback and it is what the audit digest is taken over, so the ledger keeps
    recording the words rather than the markup.
    """
    payload: dict[str, Any] = {"message_id": message_id, "comment": comment}
    if html.strip():
        payload["html"] = html
    return str(_call("msgraph_reply", payload).get("message_id") or "")


__all__ = [
    "BrokerError",
    "MsGraphBrokerUnavailable",
    "send_message",
    "send_reply",
    "transmit_available",
]
