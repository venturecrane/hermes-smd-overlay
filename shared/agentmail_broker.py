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

The functions below deliberately take NO api_key. There is nothing to pass — the
gateway's AgentMail key is inbox-scoped with ``message_send``/``draft_send``
withheld, so it could not transmit even if this code tried.
"""

from __future__ import annotations

import os
from typing import Any

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


def _call(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not transmit_available():
        raise AgentMailBrokerUnavailable(
            f"{SOCKET_ENV} is unset; this seat has no broker transmit path"
        )
    try:
        return request({"action": action, "payload": payload}, timeout=SEND_TIMEOUT_SECONDS)
    except OSError as exc:
        # Transport-level (OSError covers socket timeouts: TimeoutError has
        # subclassed it since 3.10). The broker may or may not have sent. Distinguished
        # from BrokerError (a decision the broker made and recorded) because
        # reporting "you may not write to this person" when the truth is "the
        # socket timed out" would be a lie in the ledger's own language.
        raise AgentMailBrokerUnavailable(f"broker unreachable: {exc}") from exc


def send_message(payload: dict[str, Any]) -> str:
    """Transmit a fresh message; return the AgentMail message id.

    ``payload`` carries only content and recipients — the broker applies the
    recipient fence and pins the From. Raises :class:`BrokerError` when the
    broker refuses (an authored-policy decision, already audited there) and
    :class:`AgentMailBrokerUnavailable` when it could not be asked.
    """
    return str(_call("agentmail_send", payload).get("message_id") or "")


def send_reply(message_id: str, text: str = "", html: str = "") -> str:
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
    return str(_call("agentmail_reply", body).get("message_id") or "")


__all__ = [
    "AgentMailBrokerUnavailable",
    "BrokerError",
    "send_message",
    "send_reply",
    "transmit_available",
]
