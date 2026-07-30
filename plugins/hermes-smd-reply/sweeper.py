"""Auto-release sweeper for rate-held replies (ss-console #2070).

Pairs with :mod:`held_store`. One daemon thread in the AGENT process wakes on a
short interval, and for each pending reply — oldest first — re-asks the same
:class:`~relay.RateLimiter` the live path uses whether the window has cleared.
If it has, the reply is transmitted through the same transports the live path
uses; if not, that sender is skipped for this pass so per-sender order is never
broken.

Why the agent process and not the gate: the limiter windows are in-memory and
belong to the reply plugin. A gate-hosted sweeper would release sends the
agent's windows never saw, silently doubling the effective rate — the exact
runaway the caps exist to bound.

Release ordering rules:

* global FIFO by row id — the oldest held reply is always the next candidate;
* a sender whose window is still full is added to ``blocked`` and ALL of that
  sender's later rows are skipped this pass (per-sender FIFO);
* a global or backstop refusal ends the pass entirely — those bound everyone,
  so continuing would just churn.

The pass re-resolves the authored policy every time, so authoring
``held_release.enabled`` (or widening a cap) takes effect without a restart —
the same ADR 0044 read-fresh posture the send path follows. The thread starts
unconditionally at register and no-ops while release is unauthored.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shared import send_policy as send_policy_mod

from . import held_store as held_store_mod

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_S = 30.0


@dataclass(frozen=True)
class SweepResult:
    """What one pass did — returned for tests and for the log line."""

    released: int = 0
    expired: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def touched(self) -> int:
        return self.released + self.expired + self.failed


def run_sweep_once(
    *,
    store: held_store_mod.HeldReplyStore,
    limiter: Any,
    policy: send_policy_mod.SendPolicy,
    send_fn: Callable[[held_store_mod.HeldReply], str],
    emit_fn: Callable[..., None],
    notify_fn: Callable[..., None] | None = None,
    internal_senders: Callable[[str], bool] | None = None,
    now: float | None = None,
) -> SweepResult:
    """One release pass. Pure with respect to time and I/O injection.

    ``send_fn`` transmits one reply and returns the sent-message id (raising on
    failure); ``emit_fn`` writes the audit row; ``notify_fn`` reports expiry.
    ``internal_senders`` answers whether a sender is rostered-INTERNAL, so the
    release re-applies the SAME exemption the live path applied.
    """
    if not policy.held_release_enabled:
        return SweepResult()

    now = time.time() if now is None else now
    released = expired = skipped = failed = 0
    blocked: set[str] = set()

    for row in store.iter_held():
        # TTL first: an expired reply is never sent, even if a slot is free.
        if now - row.created_at >= policy.held_ttl_s:
            if store.claim(row.id):
                store.mark_terminal(row.id, held_store_mod.STATUS_EXPIRED)
                emit_fn(
                    action_type="REPLY_FAILED",
                    metadata={
                        "reason": "hold_expired",
                        "recipient": row.sender,
                        "message_id": row.message_id,
                        "adapter": row.adapter,
                        "held_reason": row.hold_reason,
                        "body_digest": row.body_digest,
                    },
                )
                if notify_fn is not None:
                    notify_fn(
                        reason="hold_expired",
                        sender=row.sender,
                        sender_class=row.sender_class,
                        adapter=row.adapter,
                        message_id=row.message_id,
                        body_digest=row.body_digest,
                    )
                expired += 1
            continue

        if row.sender in blocked:
            skipped += 1
            continue

        internal = bool(internal_senders(row.sender)) if internal_senders else False
        decision = limiter.check(row.sender, internal=internal, policy=policy)
        if not decision.allowed:
            reason = decision.reason or ""
            if reason in ("rate_limited_global", "rate_limited_backstop"):
                # Bounds everyone — nothing else in this pass can release.
                skipped += 1
                break
            blocked.add(row.sender)
            skipped += 1
            continue

        if not store.claim(row.id):
            # Someone else took it (a concurrent sweeper or a restart race).
            continue
        try:
            sent_id = send_fn(row)
        except Exception as exc:  # noqa: BLE001 — one bad send never stops the pass
            store.mark_terminal(row.id, held_store_mod.STATUS_FAILED_SEND, error=str(exc)[:500])
            emit_fn(
                action_type="REPLY_FAILED",
                metadata={
                    "reason": str(exc)[:500],
                    "recipient": row.sender,
                    "message_id": row.message_id,
                    "adapter": row.adapter,
                    "released_from_hold": True,
                },
            )
            failed += 1
            continue

        store.mark_terminal(row.id, held_store_mod.STATUS_SENT)
        emit_fn(
            action_type="REPLY_SENT",
            metadata={
                "recipient": row.sender,
                "recipient_class": row.sender_class or "unclassified",
                "adapter": row.adapter,
                "in_reply_to": row.message_id,
                "inbox_id": row.inbox_id,
                "sent_message_id": sent_id,
                "body_digest": row.body_digest,
                "released_from_hold": True,
                "held_reason": row.hold_reason,
            },
        )
        released += 1

    return SweepResult(released=released, expired=expired, skipped=skipped, failed=failed)


def start_sweeper_thread(
    *,
    sweep: Callable[[], SweepResult],
    interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """Start the daemon loop. Never raises — a broken sweep degrades to drop.

    Started unconditionally at register: whether release is ON is a per-pass
    question (live-read policy), not a boot-time one, so authoring the block
    later must not require a restart.
    """
    stop = stop_event or threading.Event()

    def _loop() -> None:
        while not stop.wait(interval_s):
            try:
                result = sweep()
            except Exception as exc:  # noqa: BLE001 — the loop must survive anything
                logger.warning("hermes-smd-reply: held-reply sweep failed (%s)", exc)
                continue
            if result.touched:
                logger.info(
                    "hermes-smd-reply: held-reply sweep released=%d expired=%d failed=%d skipped=%d",
                    result.released,
                    result.expired,
                    result.failed,
                    result.skipped,
                )

    thread = threading.Thread(target=_loop, name="smd-held-reply-sweeper", daemon=True)
    thread.start()
    return thread


__all__ = [
    "DEFAULT_SWEEP_INTERVAL_S",
    "SweepResult",
    "run_sweep_once",
    "start_sweeper_thread",
]
