"""Normalized inbound-mail seam — the provider-neutral ``InboundMessage`` DTO
(ADR 0078 / email-channel-seam spec D2).

Every mail provider normalizes into ONE shape at the gate/router boundary, so
nothing downstream of the seam branches on provider. Roster, taint, prompts, and
skills consume only :class:`InboundMessage`; the reply/send transport is the only
code allowed to read the opaque, provider-specific :attr:`InboundMessage.provider_refs`.

Two adapters live here:

* ``agentmail`` — adapter #1, MIGRATED onto the DTO. The parsing was extracted
  verbatim from the webhook router's old ``_inbound_origin_from`` plus the payload
  fields the AgentMail inbound prompt consumes (``message.from`` / ``subject`` /
  ``message_id`` / ``inbox_id`` / ``text``), so AgentMail's live behavior is
  unchanged — the router derives the same sender/message-id/inbox anchor from the
  DTO that it used to parse inline.
* ``msgraph`` — the Microsoft 365 app-only connector normalizes to the DTO shape
  on ITS side (Graph nests recipients + body deeply; that flattening belongs with
  the transport that speaks Graph, spec D4), so this entry only validates and
  accepts an already-DTO-shaped dict. Strict: a dict missing ``provider`` /
  ``message_id`` / ``from_addr`` yields ``None`` (fail toward quarantine).

Fail-safe is a requirement (spec D2): a payload an adapter cannot parse yields
``None``, never a guessed DTO. Missing individual fields degrade to ``""`` / ``None``
/ ``[]`` — never invented. ``normalize_inbound`` returns ``None`` for an unknown
source (fail-closed): a channel with no seam normalizer produces no agent turn.
"""

from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parseaddr

# Closed provider vocabulary — grows by adapter, in lock-step with the seam
# normalizer registry below and ss-console's ACCEPTED_SEND_PROVIDERS.
PROVIDER_AGENTMAIL = "agentmail"
PROVIDER_MSGRAPH = "msgraph"
ACCEPTED_PROVIDERS: frozenset[str] = frozenset({PROVIDER_AGENTMAIL, PROVIDER_MSGRAPH})


@dataclass(frozen=True)
class InboundMessage:
    """One normalized inbound email at the seam (spec D2).

    Every field is either populated from the provider payload or degraded to an
    empty value — NEVER invented. ``from_addr`` is a bare, lower-cased address
    (the roster's input); ``to``/``cc`` are lists of the same. ``provider_refs``
    is opaque: only the matching provider's send/reply transport reads it (it
    carries the ids the reply path threads on — AgentMail inbox/message ids, the
    Graph message/conversation ids).
    """

    provider: str
    mailbox: str
    message_id: str
    thread_ref: str | None
    from_addr: str
    to: list[str]
    cc: list[str]
    subject: str
    body_text: str
    received_at: str
    provider_refs: dict

    def to_dict(self) -> dict:
        """Plain-dict projection for embedding in the dispatch directive."""
        return {
            "provider": self.provider,
            "mailbox": self.mailbox,
            "message_id": self.message_id,
            "thread_ref": self.thread_ref,
            "from_addr": self.from_addr,
            "to": list(self.to),
            "cc": list(self.cc),
            "subject": self.subject,
            "body_text": self.body_text,
            "received_at": self.received_at,
            "provider_refs": dict(self.provider_refs),
        }


# ---------------------------------------------------------------------------
# Field-coercion helpers (degrade, never invent)
# ---------------------------------------------------------------------------


def _bare_address(raw: object) -> str:
    """A single address → bare, lower-cased (roster form). Empty on anything else.

    Accepts a ``"Display Name <addr@host>"`` string (parsed via ``parseaddr``) or
    a mapping carrying the address under ``address``/``email`` (Graph-ish shapes,
    already flattened by the msgraph connector but tolerated here). Never invents.
    """
    if isinstance(raw, str):
        return parseaddr(raw)[1].strip().lower()
    if isinstance(raw, dict):
        for key in ("address", "email", "emailAddress"):
            v = raw.get(key)
            if isinstance(v, dict):
                v = v.get("address") or v.get("email")
            if isinstance(v, str) and v.strip():
                return parseaddr(v)[1].strip().lower()
    return ""


def _address_list(raw: object) -> list[str]:
    """A ``to``/``cc`` field → list of bare lower-cased addresses. ``[]`` on absent.

    A list is mapped element-wise; a bare string is treated as a single
    recipient. Entries that don't resolve to an ``@`` address are dropped (never
    invented, never a placeholder). ``from_addr`` extraction deliberately does
    NOT apply this ``@`` filter — it preserves the exact ``parseaddr`` behavior of
    the migrated router so the recipient-lock contract is unchanged."""
    if isinstance(raw, list):
        out = [_bare_address(item) for item in raw]
        return [a for a in out if a and "@" in a]
    if isinstance(raw, str):
        a = _bare_address(raw)
        return [a] if a and "@" in a else []
    return []


def _str_or_empty(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _first_present(msg: dict, *keys: str) -> str:
    """First key in ``msg`` whose value is a non-empty string, stripped; else ''."""
    for key in keys:
        v = msg.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


# ---------------------------------------------------------------------------
# AgentMail adapter (#1) — extracted from the router's old inline parsing
# ---------------------------------------------------------------------------


def _agentmail_message_block(payload: dict) -> dict | None:
    """Resolve the AgentMail ``message`` block from the (gate-stamped) payload.

    Tolerant of the two shapes the router already handled: the block nested under
    ``message`` (the vendor webhook) or under ``data`` — the Svix envelope
    (``{"type": ..., "data": {...}}``) whose message fields may sit under
    ``data.message`` OR directly under ``data``. Returns ``None`` when no block
    resolves (unparseable → the caller yields ``None``)."""
    msg = payload.get("message")
    if isinstance(msg, dict):
        return msg
    data = payload.get("data")
    if isinstance(data, dict):
        cand = data.get("message")
        return cand if isinstance(cand, dict) else data
    return None


def _normalize_agentmail(payload: dict) -> InboundMessage | None:
    """Normalize an AgentMail ``message.received`` payload into the DTO.

    Consumes exactly the fields the live AgentMail path already used — ``from``
    (normalized to a bare lower-cased address, as the recipient-lock requires),
    ``subject``, ``message_id``, ``inbox_id``, ``text`` — plus best-effort
    ``to``/``cc``/timestamp when present. ``inbox_id`` + ``message_id`` are the
    reply anchor, carried in ``provider_refs`` (the AgentMail reply transport
    threads on them). Returns ``None`` when no message block resolves."""
    if not isinstance(payload, dict):
        return None
    msg = _agentmail_message_block(payload)
    if msg is None:
        return None

    from_addr = _bare_address(msg.get("from"))
    message_id = _str_or_empty(msg.get("message_id"))
    inbox_id = _str_or_empty(msg.get("inbox_id"))
    thread_ref = _first_present(msg, "thread_id", "conversation_id", "thread_ref") or None
    provider_refs = {"inbox_id": inbox_id, "message_id": message_id}
    if thread_ref:
        provider_refs["thread_id"] = thread_ref

    return InboundMessage(
        provider=PROVIDER_AGENTMAIL,
        mailbox=inbox_id,
        message_id=message_id,
        thread_ref=thread_ref,
        from_addr=from_addr,
        to=_address_list(msg.get("to")),
        cc=_address_list(msg.get("cc")),
        subject=_str_or_empty(msg.get("subject")),
        body_text=_first_present(msg, "text", "body", "body_plain", "content"),
        received_at=_first_present(msg, "received_at", "timestamp", "created_at", "date"),
        provider_refs=provider_refs,
    )


# ---------------------------------------------------------------------------
# Microsoft Graph adapter (#2) — accepts the connector's already-DTO-shaped dict
# ---------------------------------------------------------------------------


def _msgraph_dto_block(payload: dict) -> dict | None:
    """Find the DTO-shaped dict the msgraph-mail connector emits.

    The connector (spec D4) does the deep Graph flattening and hands us a dict
    already carrying the DTO fields. Accept it at the payload root or nested under
    ``inbound_message`` / ``message`` / ``data`` (whichever the poller dispatches
    it under). The first dict carrying ``from_addr`` or ``message_id`` wins."""
    for candidate in (
        payload,
        payload.get("inbound_message"),
        payload.get("message"),
        payload.get("data"),
    ):
        if isinstance(candidate, dict) and ("from_addr" in candidate or "message_id" in candidate):
            return candidate
    return None


def _normalize_msgraph(payload: dict) -> InboundMessage | None:
    """Validate + accept an already-normalized msgraph DTO dict.

    Strict fail-closed: ``provider`` must be ``"msgraph"`` and ``message_id`` /
    ``from_addr`` must be non-empty strings, or ``None`` (the connector normalizes
    on its side, so a dict missing these is malformed, not merely sparse).
    Optional fields degrade to empty values; ``from_addr``/``to``/``cc`` are
    re-canonicalized to bare lower-cased addresses defensively."""
    if not isinstance(payload, dict):
        return None
    block = _msgraph_dto_block(payload)
    if block is None:
        return None

    if block.get("provider") != PROVIDER_MSGRAPH:
        return None
    message_id = _str_or_empty(block.get("message_id"))
    from_addr = _bare_address(block.get("from_addr"))
    if not message_id or not from_addr:
        return None

    thread_ref = block.get("thread_ref")
    thread_ref = thread_ref.strip() if isinstance(thread_ref, str) and thread_ref.strip() else None
    refs = block.get("provider_refs")
    provider_refs = dict(refs) if isinstance(refs, dict) else {}

    return InboundMessage(
        provider=PROVIDER_MSGRAPH,
        mailbox=_str_or_empty(block.get("mailbox")),
        message_id=message_id,
        thread_ref=thread_ref,
        from_addr=from_addr,
        to=_address_list(block.get("to")),
        cc=_address_list(block.get("cc")),
        subject=_str_or_empty(block.get("subject")),
        body_text=_str_or_empty(block.get("body_text")),
        received_at=_str_or_empty(block.get("received_at")),
        provider_refs=provider_refs,
    )


# ---------------------------------------------------------------------------
# Registry + entry point
# ---------------------------------------------------------------------------


# source label → normalizer. The source is the verified ingress provenance the
# gate stamps (``agentmail`` / ``msgraph``); it equals the provider vocabulary.
# A source with no entry has no seam door — ``normalize_inbound`` returns None.
NORMALIZERS: dict[str, Callable[[dict], InboundMessage | None]] = {
    PROVIDER_AGENTMAIL: _normalize_agentmail,
    PROVIDER_MSGRAPH: _normalize_msgraph,
}


def has_normalizer(source: object) -> bool:
    """Whether ``source`` is a bound seam adapter (structural D3 input)."""
    return isinstance(source, str) and source in NORMALIZERS


def normalize_inbound(source: str, payload: dict) -> InboundMessage | None:
    """Normalize ``payload`` from ``source`` into the seam DTO, or ``None``.

    Fail-closed on an unknown source (no normalizer → no door → ``None``); the
    per-provider normalizer fails toward ``None`` on an unparseable payload. This
    is the single entry the router and poller call — nothing downstream branches
    on provider."""
    normalizer = NORMALIZERS.get(source)
    if normalizer is None:
        return None
    try:
        return normalizer(payload)
    except Exception:
        # A normalizer must never raise into the dispatch/gateway path; an
        # unexpected shape fails toward quarantine, not a crash.
        return None


__all__ = [
    "ACCEPTED_PROVIDERS",
    "NORMALIZERS",
    "PROVIDER_AGENTMAIL",
    "PROVIDER_MSGRAPH",
    "InboundMessage",
    "has_normalizer",
    "normalize_inbound",
]
