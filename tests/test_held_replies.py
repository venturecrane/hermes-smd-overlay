"""Held-reply store + auto-release sweeper (ss-console #2070 O2).

The behavior under test is the one the 2026-07-30 burst rehearsal exposed: a
rate-limited reply used to be audited and DROPPED, so the Operator went silent
mid-conversation with no release and no notification. These tests pin the fix —
persistence, FIFO release, at-most-once, TTL expiry, body hygiene — and the
regression that release must not reorder a conversation.
"""

from __future__ import annotations

import pytest

from shared.send_policy import DEFAULT_SEND_POLICY, SendPolicy
from tests.conftest import load_plugin

_MOD = load_plugin("hermes-smd-reply")
held_store = _MOD.held_store
sweeper = _MOD.sweeper
relay = _MOD.relay


RELEASE_ON = SendPolicy(
    internal_exempt=False,
    per_sender_max=3,
    per_sender_window_s=600.0,
    global_max=20,
    global_window_s=3600.0,
    backstop_max=0,
    backstop_window_s=3600.0,
    held_release_enabled=True,
    held_ttl_s=86400.0,
)


@pytest.fixture
def store(tmp_path):
    s = held_store.HeldReplyStore(str(tmp_path / "held.db"))
    yield s
    s.close()


def _enqueue(store, sender="greg@x.test", *, reason="rate_limited_per_sender", text="body"):
    return store.enqueue(
        sender=sender,
        sender_class="internal",
        adapter="agentmail",
        inbox_id="inbox_x",
        message_id=f"msg_{sender}_{text}",
        send_text=text,
        send_html="",
        body_digest="d1g3st",
        hold_reason=reason,
    )


class _Recorder:
    """Captures released sends + audit rows the way the live path emits them."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.sent: list[str] = []
        self.events: list[tuple[str, dict]] = []
        self.notified: list[dict] = []
        self._fail_on = fail_on or set()

    def send(self, row) -> str:
        if row.message_id in self._fail_on:
            raise RuntimeError("transport exploded")
        self.sent.append(row.message_id)
        return f"sent_{row.id}"

    def emit(self, *, action_type: str, metadata: dict) -> None:
        self.events.append((action_type, metadata))

    def notify(self, **kw) -> None:
        self.notified.append(kw)


def _sweep(store, limiter, rec, *, policy=RELEASE_ON, internal=None, now=None):
    return sweeper.run_sweep_once(
        store=store,
        limiter=limiter,
        policy=policy,
        send_fn=rec.send,
        emit_fn=rec.emit,
        notify_fn=rec.notify,
        internal_senders=internal,
        now=now,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_enqueue_then_pending(store) -> None:
    _enqueue(store)
    assert store.pending_count() == 1
    assert store.has_pending("greg@x.test") is True
    assert store.has_pending("other@x.test") is False


def test_claim_is_at_most_once(store) -> None:
    row_id = _enqueue(store)
    assert store.claim(row_id) is True
    assert store.claim(row_id) is False  # a second sweeper cannot take it


def test_terminal_transition_drops_the_body(store) -> None:
    """Held bodies are client work product; only the digest outlives the hold."""
    row_id = _enqueue(store, text="privileged draft text")
    store.claim(row_id)
    store.mark_terminal(row_id, held_store.STATUS_SENT)
    row = store.get(row_id)
    assert row is not None
    assert row["status"] == "sent"
    assert row["send_text"] is None and row["send_html"] is None
    assert row["body_digest"] == "d1g3st"  # audit correlation survives


def test_interrupted_rows_are_never_auto_resent(store) -> None:
    row_id = _enqueue(store)
    store.claim(row_id)  # process dies here
    assert store.fail_interrupted_on_boot() == [row_id]
    assert store.get(row_id)["status"] == "failed_interrupted"
    assert store.pending_count() == 0  # NOT requeued — duplicate > lost


def test_purge_terminal_respects_retention(store) -> None:
    row_id = _enqueue(store)
    store.claim(row_id)
    store.mark_terminal(row_id, held_store.STATUS_SENT)
    assert store.purge_terminal(older_than_s=3600.0) == 0  # too fresh
    assert store.purge_terminal(older_than_s=0.0) == 1


# ---------------------------------------------------------------------------
# Sweeper — release
# ---------------------------------------------------------------------------


def test_release_when_window_clears(store) -> None:
    clock = {"t": 0.0}
    limiter = relay.RateLimiter(clock=lambda: clock["t"])
    rec = _Recorder()
    # Fill the sender's window on the live path, then hold one.
    for _ in range(3):
        limiter.check("greg@x.test", internal=False, policy=RELEASE_ON)
    _enqueue(store)

    assert _sweep(store, limiter, rec).released == 0  # window still full
    assert store.pending_count() == 1

    clock["t"] = 601.0  # window elapsed
    result = _sweep(store, limiter, rec)
    assert result.released == 1
    assert store.pending_count() == 0
    action, meta = rec.events[-1]
    assert action == "REPLY_SENT"
    assert meta["released_from_hold"] is True
    assert meta["in_reply_to"] and meta["body_digest"] == "d1g3st"


def test_release_preserves_per_sender_order(store) -> None:
    """A blocked sender's LATER rows never jump ahead of its earlier ones."""
    clock = {"t": 0.0}
    limiter = relay.RateLimiter(clock=lambda: clock["t"])
    rec = _Recorder()
    policy = SendPolicy(**{**RELEASE_ON.__dict__, "per_sender_max": 1})
    _enqueue(store, "a@x.test", text="a1")
    _enqueue(store, "a@x.test", text="a2")
    _enqueue(store, "b@x.test", text="b1")

    result = _sweep(store, limiter, rec, policy=policy)
    # a1 releases (fills a's window), a2 is skipped behind it, b1 still flows.
    assert result.released == 2
    assert rec.sent == ["msg_a@x.test_a1", "msg_b@x.test_b1"]
    assert store.pending_count() == 1


def test_global_refusal_ends_the_pass(store) -> None:
    clock = {"t": 0.0}
    limiter = relay.RateLimiter(clock=lambda: clock["t"])
    rec = _Recorder()
    policy = SendPolicy(**{**RELEASE_ON.__dict__, "global_max": 1})
    _enqueue(store, "a@x.test", text="a1")
    _enqueue(store, "b@x.test", text="b1")

    result = _sweep(store, limiter, rec, policy=policy)
    assert result.released == 1  # global bound stops everything after the first
    assert store.pending_count() == 1


def test_release_reapplies_the_internal_exemption(store) -> None:
    """A sender removed from the roster between hold and release loses the
    exemption — the decision is made against LIVE classification, not a snapshot."""
    limiter = relay.RateLimiter()
    rec = _Recorder()
    policy = SendPolicy(**{**RELEASE_ON.__dict__, "internal_exempt": True, "per_sender_max": 0})
    _enqueue(store, "gone@x.test")

    assert _sweep(store, limiter, rec, policy=policy, internal=lambda _s: False).released == 0
    assert _sweep(store, limiter, rec, policy=policy, internal=lambda _s: True).released == 1


def test_ttl_expiry_audits_and_notifies(store) -> None:
    limiter = relay.RateLimiter()
    rec = _Recorder()
    policy = SendPolicy(**{**RELEASE_ON.__dict__, "held_ttl_s": 10.0})
    row_id = _enqueue(store)

    result = _sweep(store, limiter, rec, policy=policy, now=_created_at(store, row_id) + 11.0)
    assert result.expired == 1 and result.released == 0
    action, meta = rec.events[-1]
    assert action == "REPLY_FAILED" and meta["reason"] == "hold_expired"
    assert rec.notified and rec.notified[-1]["reason"] == "hold_expired"
    assert store.get(row_id)["send_text"] is None  # body dropped on expiry


def test_send_failure_marks_failed_and_continues(store) -> None:
    limiter = relay.RateLimiter()
    rec = _Recorder(fail_on={"msg_a@x.test_a1"})
    _enqueue(store, "a@x.test", text="a1")
    _enqueue(store, "b@x.test", text="b1")

    result = _sweep(store, limiter, rec)
    assert result.failed == 1 and result.released == 1
    assert any(a == "REPLY_FAILED" for a, _ in rec.events)


def test_disabled_release_is_a_no_op(store) -> None:
    """Unauthored held_release ⇒ the sweeper does nothing (pre-#2070 parity)."""
    limiter = relay.RateLimiter()
    rec = _Recorder()
    _enqueue(store)
    result = _sweep(store, limiter, rec, policy=DEFAULT_SEND_POLICY)
    assert result == sweeper.SweepResult()
    assert store.pending_count() == 1
    assert rec.sent == []


def _created_at(store, row_id: int) -> float:
    row = store.get(row_id)
    assert row is not None
    return float(row["created_at"])
