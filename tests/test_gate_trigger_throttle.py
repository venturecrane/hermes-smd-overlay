"""Per-(trigger, matter) cooldown (ss-console #1781) — the deterministic
break for write-then-echo webhook loops. Pins: the observed live loop shape
(create_memo → matter.updated echo ~12 min later) terminates; platform
default on unauthored triggers; authored 0 disables; fail-open on every
malformed input; per-matter independence; window-start-only-on-forward.

Run::

    pytest tests/test_gate_trigger_throttle.py -q
"""

import json

from shared.gate_trigger_throttle import (
    DEFAULT_COOLDOWN_SECONDS,
    TriggerThrottle,
    resolve_throttles,
)

_MATTER_A = "68df1d38-b9a3-4855-b32f-6af1aae2f258"
_MATTER_B = "aaaa1111-2222-4333-8444-bbbbcccc0002"

_CONFIG = {
    "webhook_triggers": [
        {
            "source": "smokeball",
            "event_type": "matter.updated",
            "skill": "matter-memo-on-update",
            "persona": "operator",
            "throttle": {"cooldown_minutes": 30},
        },
        # No throttle block — platform default applies.
        {"source": "agentmail", "event_type": "message.received", "skill": "matter-inbox-router"},
        # Authored 0 — throttle disabled for this trigger.
        {
            "source": "smokeball",
            "event_type": "matter.created",
            "skill": "some-skill",
            "throttle": {"cooldown_minutes": 0},
        },
    ]
}


def _body(**fields) -> bytes:
    return json.dumps({"type": "matter.updated", **fields}).encode()


def _live_envelope(matter_id: str) -> bytes:
    """The verbatim live Smokeball envelope shape: top-level ``id`` is the
    DELIVERY id, the matter is nested at ``payload.id``."""
    return json.dumps(
        {
            "type": "matter.updated",
            "id": "de11very-0000-4444-8888-aaaaaaaaaaaa",
            "userId": None,
            "payload": {"id": matter_id, "number": "10042"},
        }
    ).encode()


# ---------------------------------------------------------------- resolve


def test_resolve_authored_default_and_disabled() -> None:
    throttles = resolve_throttles(_CONFIG)
    assert throttles[("smokeball", "matter.updated")] == 30 * 60
    assert throttles[("agentmail", "message.received")] == DEFAULT_COOLDOWN_SECONDS
    assert throttles[("smokeball", "matter.created")] == 0


def test_resolve_malformed_block_fails_toward_default_never_disabled() -> None:
    cfg = {
        "webhook_triggers": [
            {"source": "smokeball", "event_type": "matter.updated", "throttle": "nope"},
            {
                "source": "smokeball",
                "event_type": "matter.created",
                "throttle": {"cooldown_minutes": -5},
            },
            {
                "source": "smokeball",
                "event_type": "matter.closed",
                "throttle": {"cooldown_minutes": True},
            },
        ]
    }
    throttles = resolve_throttles(cfg)
    assert throttles[("smokeball", "matter.updated")] == DEFAULT_COOLDOWN_SECONDS
    assert throttles[("smokeball", "matter.created")] == DEFAULT_COOLDOWN_SECONDS
    assert throttles[("smokeball", "matter.closed")] == DEFAULT_COOLDOWN_SECONDS


def test_resolve_tolerates_garbage_config() -> None:
    assert resolve_throttles(None) == {}
    assert resolve_throttles({"webhook_triggers": "nope"}) == {}
    assert resolve_throttles({"webhook_triggers": [42, {"source": 1}]}) == {}


def test_resolve_duplicate_trigger_keeps_larger_window() -> None:
    cfg = {
        "webhook_triggers": [
            {
                "source": "smokeball",
                "event_type": "matter.updated",
                "throttle": {"cooldown_minutes": 5},
            },
            {
                "source": "smokeball",
                "event_type": "matter.updated",
                "throttle": {"cooldown_minutes": 60},
            },
        ]
    }
    assert resolve_throttles(cfg)[("smokeball", "matter.updated")] == 60 * 60


# ---------------------------------------------------------------- check


def test_the_live_loop_shape_terminates() -> None:
    """THE incident (pilot-smokeball 2026-07-06→07): forward at T, the seat's
    memo write echoes back as matter.updated at T+~12min. The echo must be
    suppressed — no wake, no next memo, chain dead."""
    throttle = TriggerThrottle()
    throttles = resolve_throttles(_CONFIG)
    t0 = 1_000_000.0
    assert (
        throttle.check(
            route="smokeball", body=_live_envelope(_MATTER_A), throttles=throttles, now=t0
        )
        is None
    )
    echo = throttle.check(
        route="smokeball", body=_live_envelope(_MATTER_A), throttles=throttles, now=t0 + 12 * 60
    )
    assert echo == f"trigger-cooldown:{_MATTER_A}"
    # And the suppressed echo did NOT extend the window: a real change after
    # the original 30-min window forwards.
    assert (
        throttle.check(
            route="smokeball", body=_live_envelope(_MATTER_A), throttles=throttles, now=t0 + 31 * 60
        )
        is None
    )


def test_matters_throttle_independently() -> None:
    throttle = TriggerThrottle()
    throttles = resolve_throttles(_CONFIG)
    t0 = 1_000_000.0
    assert (
        throttle.check(
            route="smokeball", body=_body(matterId=_MATTER_A), throttles=throttles, now=t0
        )
        is None
    )
    # A different matter one minute later is untouched.
    assert (
        throttle.check(
            route="smokeball", body=_body(matterId=_MATTER_B), throttles=throttles, now=t0 + 60
        )
        is None
    )


def test_authored_zero_disables() -> None:
    throttle = TriggerThrottle()
    throttles = resolve_throttles(_CONFIG)
    t0 = 1_000_000.0
    body = json.dumps({"type": "matter.created", "matterId": _MATTER_A}).encode()
    assert throttle.check(route="smokeball", body=body, throttles=throttles, now=t0) is None
    assert throttle.check(route="smokeball", body=body, throttles=throttles, now=t0 + 1) is None


def test_unauthored_trigger_is_untouched() -> None:
    throttle = TriggerThrottle()
    throttles = resolve_throttles(_CONFIG)
    body = json.dumps({"type": "matter.deleted", "matterId": _MATTER_A}).encode()
    assert throttle.check(route="smokeball", body=body, throttles=throttles, now=1.0) is None
    assert throttle.check(route="smokeball", body=body, throttles=throttles, now=2.0) is None


def test_no_matter_id_fails_open() -> None:
    throttle = TriggerThrottle()
    throttles = resolve_throttles(_CONFIG)
    body = json.dumps({"type": "matter.updated", "userId": "someone"}).encode()
    assert throttle.check(route="smokeball", body=body, throttles=throttles, now=1.0) is None
    assert throttle.check(route="smokeball", body=body, throttles=throttles, now=2.0) is None


def test_malformed_payload_fails_open() -> None:
    throttle = TriggerThrottle()
    throttles = resolve_throttles(_CONFIG)
    for body in (b"not json", b"[]", b"", json.dumps({"no": "type"}).encode()):
        assert throttle.check(route="smokeball", body=body, throttles=throttles, now=1.0) is None


def test_empty_throttles_is_noop() -> None:
    throttle = TriggerThrottle()
    assert (
        throttle.check(route="smokeball", body=_live_envelope(_MATTER_A), throttles={}, now=1.0)
        is None
    )


def test_matter_id_matching_is_case_insensitive() -> None:
    throttle = TriggerThrottle()
    throttles = resolve_throttles(_CONFIG)
    t0 = 1_000_000.0
    assert (
        throttle.check(
            route="smokeball", body=_body(matterId=_MATTER_A.upper()), throttles=throttles, now=t0
        )
        is None
    )
    assert (
        throttle.check(
            route="smokeball", body=_body(matterId=_MATTER_A), throttles=throttles, now=t0 + 60
        )
        == f"trigger-cooldown:{_MATTER_A}"
    )


def test_expired_windows_prune() -> None:
    throttle = TriggerThrottle()
    throttles = resolve_throttles(_CONFIG)
    t0 = 1_000_000.0
    assert (
        throttle.check(
            route="smokeball", body=_body(matterId=_MATTER_A), throttles=throttles, now=t0
        )
        is None
    )
    assert (
        throttle.check(
            route="smokeball",
            body=_body(matterId=_MATTER_A),
            throttles=throttles,
            now=t0 + 30 * 60 + 1,
        )
        is None
    )
    # Internal map holds exactly the fresh window (the expired one pruned).
    assert len(throttle._windows) == 1  # noqa: SLF001
