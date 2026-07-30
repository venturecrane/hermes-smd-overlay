"""Authored reply send-rate policy (customer.yaml ``send_policy``, ss-console #2070).

The reply relay's rate caps were hardcoded platform constants (3 per sender per
10 minutes, 20 per seat-hour) that treated a rostered colleague identically to
a stranger — which kills a sustained email dialogue on the fourth exchange
(the 2026-07-30 burst rehearsal, ss-console #2069). This module resolves the
authored ``send_policy`` block into a frozen :class:`SendPolicy` the relay
consults per call (ADR 0044 read-fresh posture: authoring the block takes
effect without a restart).

Fail-closed semantics: an absent, non-mapping, or in ANY field malformed block
resolves to :data:`DEFAULT_SEND_POLICY` — byte-for-byte today's behavior
(no internal exemption, no backstop, no held-release). A typo can only ever
tighten the seat back to the platform default, never loosen it.

Scope honesty: this policy governs the REPLY channel only (the
``hermes-smd-reply`` relay). The autonomous/confirm send lane has its own
controls and never consults this limiter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Platform defaults — the pre-#2070 constants, preserved exactly. These apply
# whenever the block is unauthored or malformed.
_DEFAULT_PER_SENDER_MAX = 3
_DEFAULT_PER_SENDER_WINDOW_S = 600.0
_DEFAULT_GLOBAL_MAX = 20
_DEFAULT_GLOBAL_WINDOW_S = 3600.0
_DEFAULT_HELD_TTL_S = 86400.0

_REPLY_KEYS = frozenset(
    {
        "internal_exempt",
        "per_sender_max",
        "per_sender_window_seconds",
        "global_max",
        "global_window_seconds",
        "backstop_max",
        "backstop_window_seconds",
    }
)
_HELD_RELEASE_KEYS = frozenset({"enabled", "ttl_seconds"})
_TOP_KEYS = frozenset({"reply", "held_release"})


@dataclass(frozen=True)
class SendPolicy:
    """Resolved reply-channel send policy.

    ``backstop_max == 0`` means the backstop is disabled (the platform-default
    posture). ``internal_exempt`` exempts rostered-INTERNAL senders from the
    per-sender and external-global windows; exempt sends are still counted
    against (and bounded by) the reply backstop when one is authored.
    """

    internal_exempt: bool
    per_sender_max: int
    per_sender_window_s: float
    global_max: int
    global_window_s: float
    backstop_max: int
    backstop_window_s: float
    held_release_enabled: bool
    held_ttl_s: float


DEFAULT_SEND_POLICY = SendPolicy(
    internal_exempt=False,
    per_sender_max=_DEFAULT_PER_SENDER_MAX,
    per_sender_window_s=_DEFAULT_PER_SENDER_WINDOW_S,
    global_max=_DEFAULT_GLOBAL_MAX,
    global_window_s=_DEFAULT_GLOBAL_WINDOW_S,
    backstop_max=0,
    backstop_window_s=_DEFAULT_GLOBAL_WINDOW_S,
    held_release_enabled=False,
    held_ttl_s=_DEFAULT_HELD_TTL_S,
)


def _as_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"expected bool, got {type(value).__name__}")
    return value


def _as_count(value: Any) -> int:
    # bool is an int subclass; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"expected non-negative int, got {value!r}")
    return value


def _as_window(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"expected positive number, got {value!r}")
    return float(value)


def resolve_send_policy(raw: Any) -> SendPolicy:
    """Resolve a raw ``send_policy`` mapping into a :class:`SendPolicy`.

    Whole-block fail-closed: ANY fault (unknown key, wrong type, negative
    count, non-positive window) resolves the ENTIRE block to
    :data:`DEFAULT_SEND_POLICY`, logged once. Per-field salvage is deliberately
    not attempted — a partially-applied policy is harder to reason about than
    "the block is either exactly what you authored or exactly the default".
    """
    if raw is None:
        return DEFAULT_SEND_POLICY
    try:
        if not isinstance(raw, dict):
            raise ValueError(f"send_policy must be a mapping, got {type(raw).__name__}")
        unknown = set(raw) - _TOP_KEYS
        if unknown:
            raise ValueError(f"unknown send_policy keys: {sorted(unknown)}")

        reply = raw.get("reply") or {}
        if not isinstance(reply, dict):
            raise ValueError("send_policy.reply must be a mapping")
        unknown = set(reply) - _REPLY_KEYS
        if unknown:
            raise ValueError(f"unknown send_policy.reply keys: {sorted(unknown)}")

        held = raw.get("held_release") or {}
        if not isinstance(held, dict):
            raise ValueError("send_policy.held_release must be a mapping")
        unknown = set(held) - _HELD_RELEASE_KEYS
        if unknown:
            raise ValueError(f"unknown send_policy.held_release keys: {sorted(unknown)}")

        d = DEFAULT_SEND_POLICY
        return SendPolicy(
            internal_exempt=(
                _as_bool(reply["internal_exempt"]) if "internal_exempt" in reply else False
            ),
            per_sender_max=(
                _as_count(reply["per_sender_max"])
                if "per_sender_max" in reply
                else d.per_sender_max
            ),
            per_sender_window_s=(
                _as_window(reply["per_sender_window_seconds"])
                if "per_sender_window_seconds" in reply
                else d.per_sender_window_s
            ),
            global_max=(_as_count(reply["global_max"]) if "global_max" in reply else d.global_max),
            global_window_s=(
                _as_window(reply["global_window_seconds"])
                if "global_window_seconds" in reply
                else d.global_window_s
            ),
            backstop_max=(_as_count(reply["backstop_max"]) if "backstop_max" in reply else 0),
            backstop_window_s=(
                _as_window(reply["backstop_window_seconds"])
                if "backstop_window_seconds" in reply
                else d.backstop_window_s
            ),
            held_release_enabled=(_as_bool(held["enabled"]) if "enabled" in held else False),
            held_ttl_s=(_as_window(held["ttl_seconds"]) if "ttl_seconds" in held else d.held_ttl_s),
        )
    except ValueError as exc:
        logger.warning("send_policy malformed (%s); applying platform defaults", exc)
        return DEFAULT_SEND_POLICY


def live_send_policy(yaml_path: str | None = None) -> SendPolicy:
    """Live-read the authored policy from customer.yaml (per-call, ADR 0044).

    Any read/parse failure yields :data:`DEFAULT_SEND_POLICY` — the relay
    already fails closed on an unreadable roster before rate limiting, so this
    default only ever governs the rare window where the roster read succeeded
    and a re-read raced an author.
    """
    try:
        from shared.customer_config import CustomerConfig

        cfg = CustomerConfig.from_volume(yaml_path)
        return resolve_send_policy(cfg.send_policy)
    except Exception:  # noqa: BLE001 — defaults, never fail-open
        return DEFAULT_SEND_POLICY


__all__ = [
    "DEFAULT_SEND_POLICY",
    "SendPolicy",
    "live_send_policy",
    "resolve_send_policy",
]
