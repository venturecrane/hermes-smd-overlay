"""Trusted current-turn approval capture (ADR 0071 / #1806).

The confirm ceiling withholds a proactive send until the human approves it. The
ONLY trusted approval channel is a Telegram DM from the allowlisted owner: it
arrives at ``pre_llm_call`` as native principal input (``platform="telegram"``,
``sender_id``, ``user_message``), OUTSIDE the untrusted-inbound register, so it is
not tainted and cannot be forged by the agent (SEC-36 strips agent-supplied
approval; an agent/sub-agent message never presents an allowlisted telegram
``sender_id``).

This module decides whether a given ``pre_llm_call`` is such an approval and, if
so, marks the single pending send approved. Kept separate from the hook wiring so
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

from shared.pending_send import PENDING_SEND

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


def maybe_capture_approval(
    platform: str | None, sender_id: str | None, user_message: str | None
) -> str | None:
    """Mark the single pending send approved iff this is a trusted approval.

    Returns the approval source (``"telegram:<id>"``) when it marked an approval,
    else ``None``. Requires: Telegram platform, an allowlisted ``sender_id``, and a
    bare-affirmative whole message. Idempotent — safe to call on every inner
    ``pre_llm_call`` of the turn; a no-op when nothing is pending.
    """
    if platform != _TELEGRAM_PLATFORM:
        return None
    sid = str(sender_id).strip() if sender_id is not None else ""
    if not sid or sid not in _allowlisted_senders():
        return None
    if not is_bare_affirmative(user_message or ""):
        return None
    source = f"telegram:{sid}"
    if PENDING_SEND.mark_approved(source):
        logger.info(
            "trust: current-turn approval captured from %s for the pending send (ADR 0071 #1806)",
            source,
        )
        return source
    return None
