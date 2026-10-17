"""Trusted current-turn approval capture (ADR 0071 / #1806).

The confirm ceiling withholds a proactive send until the human approves it. The
ONLY trusted approval channel is a Telegram DM from the allowlisted owner: it
arrives at ``pre_llm_call`` as native principal input (``platform="telegram"``,
``sender_id``, ``user_message``), OUTSIDE the untrusted-inbound register, so it is
not tainted and cannot be forged by the agent (SEC-36 strips agent-supplied
approval; an agent/sub-agent message never presents an allowlisted telegram
``sender_id``).

This module decides whether a given ``pre_llm_call`` is such an approval and, if
so, approves the pending send that sender raised in that chat (never another
principal's, never one raised in a different chat). Kept separate from the hook wiring so
the matching logic is unit-testable without the runtime.

Matching is deliberately STRICT (the critique's requirement): the WHOLE message,
normalized, must BE a bare affirmative — never a substring, so "yes but change the
price to $5k" (which must NOT approve the old content) and any negation are
rejected. The harden step replaces free-text matching with an inline-keyboard
callback token; until then, tight exact-set membership stands in.
"""

from __future__ import annotations

import logging
import os
import re

from shared import turn_principal
from shared.pending_send import PENDING_SEND, PendingSend
from shared.turn_principal import Principal

logger = logging.getLogger(__name__)

_TELEGRAM_PLATFORM = "telegram"
_ALLOWLIST_ENV = "TELEGRAM_ALLOWED_USERS"  # CSV, materialized from telegram.allow_from

# Bare affirmatives (normalized: lower-cased, punctuation → space, collapsed).
# The WHOLE message must equal one of these. Curated and tight; expanded only
# deliberately. "yes" alone is excluded (too weak — it could answer an unrelated
# question). The harden path (button/token) supersedes this.
_AFFIRMATIVES: frozenset[str] = frozenset(
    {
        "yes send it",
        "yes send it now",
        "send it",
        "send it now",
        "approve",
        "approve it",
        "approved",
        "yes approve",
        "yes approve it",
        "confirm",
        "confirm send",
        "yes confirm",
        "go ahead send it",
    }
)

# Negation / hesitation tokens. Exact-set membership already rejects these, but an
# explicit guard is defense-in-depth against any future loosening of the match.
_NEGATION_TOKENS: frozenset[str] = frozenset(
    {"no", "not", "dont", "don", "wait", "stop", "cancel", "hold", "nope", "never"}
)


def _normalize(message: str) -> str:
    """Lower-case, replace punctuation with spaces, collapse whitespace.

    "Yes, send it!" -> "yes send it"; "Approve." -> "approve".
    """
    s = message.strip().lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_bare_affirmative(message: str) -> bool:
    """True iff the whole message is a bare, unconditional approval."""
    if not isinstance(message, str) or not message.strip():
        return False
    normalized = _normalize(message)
    if not normalized:
        return False
    if normalized.split()[0] in _NEGATION_TOKENS or _NEGATION_TOKENS & set(normalized.split()):
        return False
    return normalized in _AFFIRMATIVES


def _allowlisted_senders() -> frozenset[str]:
    """The Telegram sender-id allowlist (env ``TELEGRAM_ALLOWED_USERS``, CSV).

    Same source the platform gates inbound DMs on, so an approval can only come
    from a sender the platform would even deliver. Empty when unset."""
    raw = os.environ.get(_ALLOWLIST_ENV, "") or ""
    return frozenset(uid.strip() for uid in raw.split(",") if uid.strip())


def capture_approval(
    platform: str | None, sender_id: str | None, user_message: str | None
) -> PendingSend | None:
    """Approve the pending send THIS sender raised in THIS chat; return it.

    Requires: Telegram platform, an allowlisted ``sender_id``, and a
    bare-affirmative whole message. The approval is bound (``shared.pending_send``
    ``_may_approve``): the approver is the sender plus the chat this turn arrived
    in, read from Hermes' session context, so one allowlisted partner cannot
    approve the other's send and a group "yes" cannot release a DM-raised send.
    Idempotent — safe to call on every inner ``pre_llm_call`` of the turn; a
    no-op when nothing this approver may approve is pending.
    """
    if platform != _TELEGRAM_PLATFORM:
        return None
    sid = str(sender_id).strip() if sender_id is not None else ""
    allowlist = _allowlisted_senders()
    if not sid or sid not in allowlist:
        return None
    if not is_bare_affirmative(user_message or ""):
        return None
    turn = turn_principal.current()
    if turn.user_id and turn.user_id != sid:
        # The hook's sender and the session's bound user disagree. Neither can be
        # chosen over the other, so nothing is approved.
        logger.warning("trust: approval refused; the turn's sender and session user disagree")
        return None
    approver = Principal(
        platform=_TELEGRAM_PLATFORM, user_id=sid, chat_id=turn.chat_id, chat_type=turn.chat_type
    )
    source = f"telegram:{sid}"
    rec = PENDING_SEND.approve(source, approver, sole_principal=len(allowlist) == 1)
    if rec is None:
        if PENDING_SEND.peek() is not None:
            logger.info(
                "trust: affirmative from %s matched no pending send it raised in this chat",
                source,
            )
        return None
    logger.info(
        "trust: current-turn approval captured from %s for its pending send (ADR 0071 #1806)",
        source,
    )
    return rec


def maybe_capture_approval(
    platform: str | None, sender_id: str | None, user_message: str | None
) -> str | None:
    """:func:`capture_approval`, returning the approval source
    (``"telegram:<id>"``) when it approved a send, else ``None``."""
    rec = capture_approval(platform, sender_id, user_message)
    return rec.approval_source if rec is not None else None
