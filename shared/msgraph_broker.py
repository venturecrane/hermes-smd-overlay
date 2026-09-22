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

import json
import os
import socket
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


def _call(
    action: str,
    payload: dict[str, Any],
    *,
    session_id: str = "",
    matter_ref: str | None = None,
    audit_extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not transmit_available():
        raise MsGraphBrokerUnavailable(
            f"{SOCKET_ENV} is unset; this seat has no broker transmit path"
        )
    # ss-console#2497 — see the twin note in ``agentmail_broker``. Beside the
    # payload, not inside it, and optional on the wire so the broker and the
    # overlay can be deployed in either order. ``audit_extra`` (WS-RENDER)
    # rides the same seam with the same freedom.
    envelope: dict[str, Any] = {"action": action, "payload": payload}
    if session_id:
        envelope["session_id"] = session_id
    if matter_ref:
        envelope["matter_ref"] = matter_ref
    if audit_extra:
        envelope["audit_extra"] = audit_extra
    try:
        return request(envelope, timeout=SEND_TIMEOUT_SECONDS)
    except OSError as exc:
        # Transport-level (OSError covers socket timeouts: TimeoutError has
        # subclassed it since 3.10). The broker may or may not have sent.
        # Distinguished from BrokerError — a decision the broker made and
        # recorded — because reporting "you may not write to this person" when
        # the truth is "the socket timed out" would be a lie in the ledger's own
        # language.
        raise MsGraphBrokerUnavailable(f"broker unreachable: {exc}") from exc


def send_message(
    payload: dict[str, Any],
    *,
    session_id: str = "",
    matter_ref: str | None = None,
    audit_extra: dict[str, str] | None = None,
) -> str:
    """Transmit a fresh message via Graph ``/sendMail``.

    ``payload`` carries only content and recipients, flat (``to``/``cc``/
    ``subject``/``body_text``) — the shape the gateway's gate already saw. The
    broker applies the recipient fence and pins the From.

    Graph answers ``sendMail`` with 202 and no body, so ``message_id`` is empty
    and always will be -- it is the id the CALL returned, and the call returns
    none. ss-console#2499 added the id the broker goes and LOOKS UP afterwards:
    it stamps an ``X-SMD-Audit-Row`` header on the message, finds that message in
    Sent Items on its read credential, and returns the RFC2822
    ``vendor_message_id`` it found there.

    Preferred, with ``message_id`` behind it, so this reads the same against a
    broker on either side of that change and needs no deployment ordering.
    """
    return _vendor_id(
        _call(
            "msgraph_send",
            payload,
            session_id=session_id,
            matter_ref=matter_ref,
            audit_extra=audit_extra,
        )
    )


def send_reply(
    message_id: str,
    comment: str,
    *,
    html: str = "",
    session_id: str = "",
    matter_ref: str | None = None,
) -> str:
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
    return _vendor_id(_call("msgraph_reply", payload, session_id=session_id, matter_ref=matter_ref))


def _vendor_id(response: dict[str, Any]) -> str:
    """The id of the message the broker just sent, or ``""`` if it has none.

    ss-console#2499. ``vendor_message_id`` is the RFC2822 id the broker resolved
    out of Sent Items after the 202; ``message_id`` is what the vendor call
    itself returned, which on Graph is always empty. Preferring the first and
    falling back to the second means this works against a broker from either
    side of that change, which is what lets the pin move without ordering.

    An empty string is the truthful answer when the broker could not find the
    message -- the caller decides what to record, and the broker has already
    recorded WHY on its own row. Inventing an id here would put a value in the
    ledger that matches nothing in the mailbox.
    """
    for key in ("vendor_message_id", "message_id"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Staff send-as (ss ADR 0089)
#
# Three verbs, and none of them sends on the gateway's word. ``send_as_propose``
# stores a DRAFT row the broker composes an approval email about and sends to
# the one person the row names; ``send_as_decide`` carries that person's
# answer, and the broker -- not this process -- checks who answered, consumes
# the row and transmits; ``send_as_match_reply`` tells the broker an inbound
# arrived on a conversation it may have sent into.
#
# VERDICTS, NOT EXCEPTIONS. Unlike ``_call`` above, a refusal here must reach
# the caller as the value it is (``{"ok": false, "reason": ...}`` or a
# ``status``), because the caller turns it into a sentence for the person. So
# these use their own framing and raise only on a transport fault, which is
# :class:`MsGraphBrokerUnavailable` -- the outcome is unknown, never "refused".
# ---------------------------------------------------------------------------

ACTION_SEND_AS_PROPOSE = "send_as_propose"
ACTION_SEND_AS_DECIDE = "send_as_decide"
ACTION_SEND_AS_MATCH_REPLY = "send_as_match_reply"
ACTION_SEND_AS_DECIDE_LINK = "send_as_decide_link"

#: The decisions ``send_as_decide`` accepts. Closed: anything else is a bug in
#: the caller, refused here before it can reach a verb that transmits.
SEND_AS_DECISIONS: frozenset[str] = frozenset({"send", "change", "cancel"})

#: The terminal statuses ``send_as_decide`` answers with.
SEND_AS_STATUSES: frozenset[str] = frozenset(
    {"DISPATCHED", "FAILED", "REVISED", "CANCELLED", "REFUSED", "EXPIRED", "SUPERSEDED"}
)


def _verdict(payload: dict[str, Any], *, timeout: float = SEND_TIMEOUT_SECONDS) -> dict[str, Any]:
    """One newline-framed request over the broker socket, verdict verbatim.

    Same framing as ``shared.act_broker.verdict``. Raises
    :class:`MsGraphBrokerUnavailable` on any transport or decode fault.
    """
    socket_path = os.environ.get(SOCKET_ENV, "").strip()
    if not socket_path:
        raise MsGraphBrokerUnavailable(
            f"{SOCKET_ENV} is unset; this seat has no broker transmit path"
        )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path)
            sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = sock.recv(65_536)
                if not chunk:
                    break
                raw += chunk
        decoded = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise MsGraphBrokerUnavailable(f"broker unreachable: {exc}") from exc
    if not isinstance(decoded, dict):
        raise MsGraphBrokerUnavailable("broker returned a malformed verdict")
    return decoded


def send_as_propose(
    *,
    session_id: str,
    instructed_by: str,
    payload: dict[str, Any],
    gate_pass: dict[str, Any],
    tainted: bool,
    sources: list[str],
) -> dict[str, Any]:
    """Ask the broker to store a send-as draft and email its approver.

    ``payload`` is ``{from, to, cc, subject, body_text}`` and nothing else: the
    broker forces ``reply_to``, renders ``body_html`` from ``body_text``, and
    takes the digest over what it will transmit. ``gate_pass`` records that the
    fabrication, matter and identifier gates passed on THIS session, the one
    that holds the read provenance. Returns the broker's verdict:
    ``{"ok": true, "tag", "act_id", "digest", "expires_at", "notified"}`` or
    ``{"ok": false, "reason"}``.
    """
    return _verdict(
        {
            "action": ACTION_SEND_AS_PROPOSE,
            "session_id": session_id,
            "instructed_by": instructed_by,
            "payload": payload,
            "gate_pass": gate_pass,
            "tainted": bool(tainted),
            "sources": list(sources),
        }
    )


def send_as_decide(
    *,
    tag_or_act_id: str,
    decision: str,
    decided_by: str,
    internet_message_id: str,
    instruction: str | None = None,
    graph_message_id: str = "",
) -> dict[str, Any]:
    """Carry a person's ``send`` / ``change`` / ``cancel`` to the broker.

    The broker decides everything that matters: whether the row is open and
    unexpired, whether ``decided_by`` may make this decision, whether the
    answering message is a forgery out of the Operator's own Sent Items, and
    only then consumes and transmits. ``tag_or_act_id`` is the bare 8-hex id
    or the full ``[draft xxxxxxxx]`` tag; the broker accepts either. These two
    verbs are gateway-only at the broker; call them from the gateway process
    (``pre_tool_call`` / ``pre_llm_call``). Returns ``{"status", "reason",
    "instruction", "replaced_by"}``.

    ``graph_message_id`` is additive to the contract: the Graph id of the
    answering message in the Operator's mailbox, sent beside the RFC 2822 id so
    the forgery check can look the item up directly when the internet id is
    absent. A broker that does not read it ignores it.
    """
    if decision not in SEND_AS_DECISIONS:
        raise ValueError(f"send_as_decide: unknown decision {decision!r}")
    request: dict[str, Any] = {
        "action": ACTION_SEND_AS_DECIDE,
        "tag_or_act_id": tag_or_act_id,
        "decision": decision,
        "decided_by": decided_by,
        "internet_message_id": internet_message_id,
        "instruction": instruction,
    }
    if graph_message_id:
        request["graph_message_id"] = graph_message_id
    return _verdict(request)


def send_as_decide_link(*, token: str) -> dict[str, Any]:
    """Carry an approve-button click to the broker (ss ADR 0089 amendment 5a).

    The token IS the authorization and the broker holds the key, so this call
    carries nothing else: no approver, no decision, nothing a caller could
    choose. The broker re-reads the row and applies every check the emailed lane
    applies. Callable from any broker peer on purpose — the seat's web gate is
    not the gateway process — which is safe only because the key is broker-owned
    and unreadable here.
    """
    return _verdict({"action": ACTION_SEND_AS_DECIDE_LINK, "token": token})


def send_as_match_reply(
    *, conversation_id: str, internet_message_id: str, from_addr: str
) -> dict[str, Any]:
    """Tell the broker an inbound arrived on ``conversation_id``.

    The broker matches it against a DISPATCHED send-as row and, on a match,
    emails that row's approver itself. Returns ``{"matched", "tag"}``.
    """
    return _verdict(
        {
            "action": ACTION_SEND_AS_MATCH_REPLY,
            "conversation_id": conversation_id,
            "internet_message_id": internet_message_id,
            "from": from_addr,
        }
    )


__all__ = [
    "ACTION_SEND_AS_DECIDE",
    "ACTION_SEND_AS_DECIDE_LINK",
    "ACTION_SEND_AS_MATCH_REPLY",
    "ACTION_SEND_AS_PROPOSE",
    "BrokerError",
    "MsGraphBrokerUnavailable",
    "SEND_AS_DECISIONS",
    "SEND_AS_STATUSES",
    "send_as_decide",
    "send_as_decide_link",
    "send_as_match_reply",
    "send_as_propose",
    "send_message",
    "send_reply",
    "transmit_available",
]
