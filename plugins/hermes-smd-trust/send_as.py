"""Propose a send AS a staff member, for that staff member's emailed approval.

ss ADR 0089. A send carrying ``from`` is not sent on the turn. It is PROPOSED:
this module runs the content gates on the session that composed it, hands the
broker a closed payload, and the broker stores the row and emails the draft to
the one person it names. Nothing leaves until that person replies ``[draft X]
send``, and then the broker, not this process, transmits it.

WHY A PROPOSAL MAY BE MADE ON A TAINTED TURN. The taint gate exists so an
injected "send this" cannot fire BECAUSE of outside content. A proposal fires
nothing: the named staff member reads the exact text and decides. Refusing it
would refuse the ordinary case, because nearly every real turn has read a
document or an inbound mail. So this path runs AHEAD of the taint refusal, and
the taint travels to the approver instead (``tainted`` / ``sources`` on the
row, shown in the approval email).

WHY THE GATES RUN HERE AND NOT AT APPROVAL. The matter gate and the identifier
filter judge a body against what THIS session read. The approver's reply opens a
different session, which read nothing; re-gating there would withhold every real
letter. So they run once, here, and their pass is stored on the row. The content
floor is deliberately NOT run: its purpose is that a human reviews such content
before it leaves (ADR 0031), which is exactly what the approval is.

WHAT THE MODEL CANNOT CHOOSE. ``bcc``, ``reply_to`` and ``html`` are dropped:
the broker sets ``reply_to`` to the staff member plus the Operator's mailbox and
renders the html from ``body_text`` itself, so the digest covers what is sent.
``from`` must be on the authored roster and must be the person who asked for
the send (or the request must have come from an Operator administrator).
"""

from __future__ import annotations

import logging
from typing import Any

from shared import matter_binding, matter_gate, msgraph_broker
from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT, TRUST_CLASS_INTERNAL

from . import outbound

logger = logging.getLogger(__name__)

#: The tools a ``from`` may ride. ``smd_send_message`` is the seat's send tool;
#: the msgraph connector's own send is blocked at the registry (ss#2258) and is
#: listed only so a ``from`` on it is proposed-or-refused, never sent.
SEND_AS_TOOLS: frozenset[str] = frozenset({"smd_send_message", "mcp_msgraph_mail_send_message"})

#: The tool name the content scans run under. Constant, so the fabrication and
#: identifier gates always treat this as a gated send whichever tool carried it.
_SCAN_TOOL = "smd_send_message"

#: The body keys a composed send may carry its text under, first match wins.
#: ``html`` is absent on purpose: the broker renders html from ``body_text``.
_BODY_KEYS: tuple[str, ...] = ("text", "body_text", "body")

_NO_ORIGIN = (
    "Refused: a message can be sent as a staff member only when that person "
    "asked for it by email, and this turn was not opened by a verified email. "
    "Nothing was sent or proposed. Say so plainly and do not try again on this "
    "turn."
)

_UNREADABLE = (
    "Refused: this seat could not read its configuration, so it cannot check who "
    "may be sent as. Nothing was sent or proposed. Say so and stop."
)

_NOT_ROSTERED = (
    "Refused: {address} is not someone this engagement authorizes the Operator "
    "to send as. Nothing was sent or proposed. Do not retry with a different "
    "address; offer to send it from the Operator's own mailbox instead."
)

_NOT_INITIATOR = (
    "Refused: only {address} can ask for a message to go out as {address}, and "
    "this request came from someone else. Nothing was sent or proposed. Say so "
    "plainly."
)

_MALFORMED = (
    "Refused: {what}. Nothing was sent or proposed. Fix it and call the send tool "
    "again with the same from."
)

_BROKER_DOWN = (
    "Refused: this seat could not record the draft for approval, so nothing was "
    "proposed and nothing was sent. Say so and stop."
)

_PROPOSED = (
    "Held for approval, not sent. The full draft was emailed to {name} "
    "({address}) for approval as {tag}. It goes out from {address} only when "
    '{name} replies "{tag} send"; they can also reply "{tag} change: ..." or '
    '"{tag} cancel". Do not send it again. Tell the person it is waiting on '
    "{name}'s approval, and end the turn."
)


def requested_from(tool_name: str, args: Any) -> str | None:
    """The ``from`` a send asks for, or ``None`` when this is an ordinary send.

    ``None`` for any tool other than the send tools and for an absent, ``None``
    or blank ``from``: those take today's path, byte for byte. Anything else
    present, including a non-string, is returned as a string so the proposal
    path refuses it as malformed rather than letting it fall through to a send
    from the Operator's own mailbox the person did not ask for.
    """
    if tool_name not in SEND_AS_TOOLS or not isinstance(args, dict):
        return None
    raw = args.get("from")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw if raw.strip() else None
    return repr(raw)


def _addresses(raw: Any) -> list[str] | None:
    """A flat, lowercased address list, or ``None`` when the shape is wrong."""
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list):
        return None
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            return None
        addr = item.strip().lower()
        if not addr:
            continue
        if addr.count("@") != 1 or any(ch in addr for ch in ' <>,;"\t\r\n'):
            return None
        if addr not in out:
            out.append(addr)
    return out


def _body(args: dict[str, Any]) -> str:
    for key in _BODY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _block(message: str) -> dict[str, str]:
    return {"action": "block", "message": message}


def _recipient_is_vendor(recipients: list[str], tainted: bool) -> bool:
    """Whether every recipient is a records vendor on the firm's typed roster,
    the one class the matter gate exempts. Fail-strict: any fault is False."""
    try:
        from shared.customer_config import CustomerConfig
        from shared.recipient_classifier import RecipientClass, classify_recipients_typed

        cfg = CustomerConfig.from_volume()
        cls = classify_recipients_typed(
            list(recipients), cfg.inbound_roster, cfg.outbound_roster, from_tainted=tainted
        )
        return cls is RecipientClass.VENDOR
    except Exception:  # noqa: BLE001 — an unknown class is not exempt
        return False


def _sources(session_id: str, tainted: bool) -> list[str]:
    """What the approver is told this draft was prepared after reading.

    Only facts the seat recorded: the matters whose content this session read,
    and, when the session ingested outside material, its trust class. Never the
    model's account of its own reading.
    """
    out: list[str] = []
    if tainted:
        try:
            out.append(f"inbound:{SESSION_TAINT.trust_class(session_id)}")
        except Exception:  # noqa: BLE001
            out.append("inbound:unknown_external")
    try:
        matters = sorted(matter_binding.membership_for(session_id).known_matters())
    except Exception:  # noqa: BLE001 — a thinner list, never a failure
        matters = []
    out.extend(f"matter:{m}" for m in matters)
    return out


def _run_gates(
    session_id: str,
    tool_call_id: str,
    scan_args: dict[str, Any],
    recipients: list[str],
    tainted: bool,
) -> tuple[dict[str, str] | None, list[str]]:
    """Fabrication, identifier and matter gates on THIS session.

    Returns ``(block, matters)``: a block directive when any gate withholds,
    else ``None`` and the matters the body cites. ``scan_args`` may be mutated by
    the staff-class dash normalizer, and the caller proposes what was scanned.
    """
    # Fabrication markers, citations, money, and the identifier-provenance gate
    # (send surface: no empty-register carve). One call, the same one an
    # autonomous send gets.
    block = outbound.check_outbound_send(
        tool_name=_SCAN_TOOL, args=scan_args, session_id=session_id, tool_call_id=tool_call_id
    )
    if block is not None:
        return block, []
    body = matter_gate.body_from_args(scan_args)
    verdict = matter_gate.evaluate(
        session_id=session_id,
        body=body,
        recipients=set(recipients),
        recipient_is_exempt=_recipient_is_vendor(recipients, tainted),
    )
    if verdict.should_withhold and matter_gate.mode() == "block":
        return (
            _block(
                "Refused: this message cites "
                f"{', '.join(verdict.matters) or 'a matter'} but {verdict.reason}; "
                "nothing was proposed (ss#2167 matter identity). Correct it before "
                "asking for approval."
            ),
            [],
        )
    multi = matter_gate.multi_matter_session(session_id)
    if multi and matter_gate.multi_matter_mode() == "block":
        return (
            _block(
                "Refused: this session read content from "
                f"{len(multi)} matters ({', '.join(multi)}) before composing this "
                "message; nothing was proposed (ss#2167 matter mixing). Compose it "
                "in a session that read only the one matter."
            ),
            [],
        )
    try:
        known = matter_binding.membership_for(session_id).known_numbers()
        matters = sorted(matter_gate.cited_matters(body, known))
    except Exception:  # noqa: BLE001 — attribution only
        matters = sorted(verdict.matters)
    return None, matters


def propose(tool_name: str, args: dict[str, Any], session_id: str, tool_call_id: str = "") -> dict:
    """Run the send-as proposal for a ``from``-bearing send. Always a block.

    Order is load-bearing: who asked, then who may be sent as, then the payload
    shape, then the gates, and only then the broker. Every refusal before the
    broker writes nothing, so no later "send" can land on it.
    """
    requested = requested_from(tool_name, args) or ""
    origin = None
    try:
        origin = SESSION_INBOUND_ORIGIN.get(session_id) if session_id else None
    except Exception:  # noqa: BLE001 — an unresolvable origin proposes nothing
        logger.warning("trust: send-as origin unresolvable", exc_info=True)
    instructed_by = (getattr(origin, "sender_address", "") or "").strip().lower()
    if not instructed_by:
        logger.info("trust: send-as not proposed; the turn has no verified email origin")
        return _block(_NO_ORIGIN)

    try:
        from shared.customer_config import CustomerConfig  # local import (enforce idiom)

        cfg = CustomerConfig.from_volume()
        entry = cfg.staff_send_as_entry(requested)
        is_admin = cfg.sender_is_admin(instructed_by)
    except Exception:  # noqa: BLE001 — an unreadable config authorizes nobody
        logger.warning("trust: send-as config unreadable; refusing", exc_info=True)
        return _block(_UNREADABLE)
    if entry is None:
        logger.info("trust: send-as refused; %r is not on scope.staff_send_as", requested)
        return _block(_NOT_ROSTERED.format(address=requested.strip() or "that address"))
    from_addr = entry["address"]
    if from_addr != instructed_by and not is_admin:
        logger.info("trust: send-as refused; %s asked to send as %s", instructed_by, from_addr)
        return _block(_NOT_INITIATOR.format(address=from_addr))

    to = _addresses(args.get("to"))
    cc = _addresses(args.get("cc"))
    subject = args.get("subject")
    body_text = _body(args)
    if not to:
        return _block(_MALFORMED.format(what="the message has no valid recipient"))
    if cc is None:
        return _block(_MALFORMED.format(what="the cc list is not a list of addresses"))
    if not isinstance(subject, str) or not subject.strip():
        return _block(_MALFORMED.format(what="the message has no subject"))
    if not body_text:
        return _block(_MALFORMED.format(what="the message has no body text"))
    if from_addr in to or from_addr in cc:
        return _block(_MALFORMED.format(what="the staff member cannot also be a recipient"))

    try:
        tainted = SESSION_TAINT.trust_class(session_id) != TRUST_CLASS_INTERNAL
    except Exception:  # noqa: BLE001 — unknown taint is reported as tainted
        tainted = True

    # bcc / reply_to / html are never read: the scan args and the payload are
    # built from this closed set, so a model-supplied value cannot ride along.
    scan_args: dict[str, Any] = {"to": to, "cc": cc, "subject": subject, "text": body_text}
    recipients = to + [a for a in cc if a not in to]
    block, matters = _run_gates(session_id, tool_call_id, scan_args, recipients, tainted)
    if block is not None:
        return block

    payload = {
        "from": from_addr,
        "to": list(scan_args["to"]),
        "cc": list(scan_args["cc"]),
        "subject": scan_args["subject"],
        "body_text": scan_args["text"],
    }
    gate_pass = {"fabrication": True, "matter": True, "identifier": True, "matters": matters}
    try:
        response = msgraph_broker.send_as_propose(
            session_id=session_id,
            instructed_by=instructed_by,
            payload=payload,
            gate_pass=gate_pass,
            tainted=tainted,
            sources=_sources(session_id, tainted),
        )
    except Exception:  # noqa: BLE001 — an unreachable broker proposes nothing
        logger.warning("trust: send_as_propose failed; nothing proposed", exc_info=True)
        return _block(_BROKER_DOWN)
    if not (isinstance(response, dict) and response.get("ok") is True):
        reason = str((response or {}).get("reason") or "the broker refused the proposal")
        logger.info("trust: send_as_propose refused: %s", reason)
        return _block(f"Refused: {reason}. Nothing was sent or proposed.")
    tag = str(response.get("tag") or "")
    if not tag:
        return _block(
            "Refused: the proposal came back without a tag the approver could "
            "answer, so nothing can be approved. Say so and stop."
        )
    logger.info(
        "trust: send-as %s proposed (from=%s, instructed_by=%s, tainted=%s)",
        tag,
        from_addr,
        instructed_by,
        tainted,
    )
    return _block(_PROPOSED.format(name=entry["name"], address=from_addr, tag=tag))


__all__ = ["SEND_AS_TOOLS", "propose", "requested_from"]
