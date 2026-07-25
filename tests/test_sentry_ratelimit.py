"""Behavior suite for the Sentry client-side throttle (incident 2026-07-16).

The load-bearing test is :func:`test_replays_the_2026_07_16_incident_volume` —
it reproduces the event volume that consumed the organization's entire monthly
error budget and asserts the throttle would have reduced it by >99%.
"""

from __future__ import annotations

import threading

from shared.sentry_ratelimit import (
    EventThrottle,
    event_key,
    normalize,
    throttle_event,
)

# ---------------------------------------------------------------------------
# normalize / event_key — grouping stability
# ---------------------------------------------------------------------------


def test_normalize_collapses_run_varying_tokens() -> None:
    a = normalize("route=smokeball id=8e3a571f-d53e-4abd-89db-18b780abcdef attempt 41")
    b = normalize("route=smokeball id=11111111-2222-3333-4444-555555555555 attempt 87")
    assert a == b
    assert "<uuid>" in a


def test_normalize_preserves_the_signal() -> None:
    """Paths and prose are what make an error identifiable — keep them."""
    out = normalize("Permission denied: '/opt/data/profiles/operator/cron/jobs.json'")
    assert "/opt/data/profiles/operator/cron/jobs.json" in out
    assert "Permission denied" in out


def test_event_key_separates_distinct_faults() -> None:
    a = {"logger": "cron.jobs", "message": "cannot read jobs.json"}
    b = {"logger": "cron.jobs", "message": "cannot write output dir"}
    c = {"logger": "mail.send", "message": "cannot read jobs.json"}
    assert event_key(a) != event_key(b)
    assert event_key(a) != event_key(c)


def test_event_key_groups_the_same_fault_across_occurrences() -> None:
    a = {"logger": "cron.jobs", "message": "tick 1041 failed for id=abc12345def"}
    b = {"logger": "cron.jobs", "message": "tick 2933 failed for id=99887766aa"}
    assert event_key(a) == event_key(b)


def test_event_key_uses_exception_type_and_value() -> None:
    base = {
        "logger": "cron.jobs",
        "exception": {"values": [{"type": "RuntimeError", "value": "boom 12"}]},
    }
    other = {
        "logger": "cron.jobs",
        "exception": {"values": [{"type": "IOError", "value": "boom 12"}]},
    }
    assert event_key(base) != event_key(other)


# ---------------------------------------------------------------------------
# Logarithmic suppression
# ---------------------------------------------------------------------------


def _sent_indices(throttle: EventThrottle, event: dict, n: int) -> list[int]:
    sent = []
    for i in range(1, n + 1):
        ok, _ = throttle.should_send(dict(event))
        if ok:
            sent.append(i)
    return sent


def test_sends_on_powers_of_two() -> None:
    throttle = EventThrottle(max_events_per_hour=0)
    sent = _sent_indices(throttle, {"logger": "x", "message": "repeat"}, 100)
    assert sent == [1, 2, 4, 8, 16, 32, 64]


def test_rare_errors_are_never_throttled() -> None:
    """The first two occurrences always go — one-off errors are not sampled away."""
    throttle = EventThrottle(max_events_per_hour=0)
    for i in range(50):
        # _word(), not the index: normalize() collapses multi-digit runs, so
        # "fault 10" and "fault 11" deliberately share a key.
        ok, _ = throttle.should_send({"logger": "x", "message": f"distinct fault {_word(i)}"})
        assert ok, f"first occurrence of distinct fault {i} was suppressed"


def test_annotations_report_true_volume() -> None:
    throttle = EventThrottle(max_events_per_hour=0)
    event = {"logger": "x", "message": "repeat"}
    seen: list[dict[str, int]] = []
    for _ in range(16):
        ok, ann = throttle.should_send(dict(event))
        if ok:
            seen.append(ann)
    assert [a["occurrence"] for a in seen] == [1, 2, 4, 8, 16]
    # Each send reports everything suppressed since the previous send.
    assert [a["suppressed_since_last"] for a in seen] == [0, 0, 1, 3, 7]


def test_distinct_keys_have_independent_budgets() -> None:
    throttle = EventThrottle(max_events_per_hour=0)
    a = {"logger": "a", "message": "one"}
    b = {"logger": "b", "message": "two"}
    for _ in range(3):
        throttle.should_send(dict(a))
    ok, ann = throttle.should_send(dict(b))
    assert ok and ann["occurrence"] == 1


# ---------------------------------------------------------------------------
# Quiet reset
# ---------------------------------------------------------------------------


def test_quiet_period_resets_so_a_new_burst_reports_promptly() -> None:
    throttle = EventThrottle(quiet_reset_seconds=0.0, max_events_per_hour=0)
    event = {"logger": "x", "message": "repeat"}
    # Every call looks like a new burst when the quiet window is zero.
    sent = _sent_indices(throttle, event, 10)
    assert sent == list(range(1, 11))


def test_no_reset_within_the_quiet_window() -> None:
    throttle = EventThrottle(quiet_reset_seconds=3600.0, max_events_per_hour=0)
    sent = _sent_indices(throttle, {"logger": "x", "message": "repeat"}, 10)
    assert sent == [1, 2, 4, 8]


# ---------------------------------------------------------------------------
# Backstop + memory bound + concurrency
# ---------------------------------------------------------------------------


def test_global_backstop_caps_key_cardinality_explosion() -> None:
    """Unique-per-occurrence keys defeat per-key suppression; the backstop catches it."""
    throttle = EventThrottle(max_events_per_hour=10)
    sent = 0
    for i in range(500):
        # A message shape normalize() cannot collapse: distinct words, not ids.
        ok, _ = throttle.should_send({"logger": "x", "message": f"unique-{_word(i)}"})
        sent += int(ok)
    assert sent == 10


def test_backstop_disabled_by_zero() -> None:
    throttle = EventThrottle(max_events_per_hour=0)
    sent = 0
    for i in range(50):
        ok, _ = throttle.should_send({"logger": "x", "message": f"unique-{_word(i)}"})
        sent += int(ok)
    assert sent == 50


def test_key_table_is_bounded() -> None:
    throttle = EventThrottle(max_tracked_keys=16, max_events_per_hour=0)
    for i in range(200):
        throttle.should_send({"logger": "x", "message": f"k-{_word(i)}"})
    assert len(throttle._keys) <= 16


def test_concurrent_callers_do_not_corrupt_counts() -> None:
    throttle = EventThrottle(max_events_per_hour=0)
    event = {"logger": "x", "message": "repeat"}
    sent = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(100):
            ok, ann = throttle.should_send(dict(event))
            if ok:
                with lock:
                    sent.append(ann["occurrence"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 800 occurrences total; every send is a distinct power of two.
    assert sorted(sent) == [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------


def test_replays_the_2026_07_16_incident_volume() -> None:
    """~3,800 events in 48h from one unreadable jobs.json burned a 5,000/mo budget.

    Two errors per ~90s scheduler tick, two distinct issues. Assert the throttle
    turns that into a volume the free quota absorbs without going blind.
    """
    throttle = EventThrottle(max_events_per_hour=0)
    ticks = 1900
    path = "/opt/data/profiles/operator/cron/jobs.json"
    sent = 0
    for _ in range(ticks):
        for event in (
            {
                "logger": "cron.jobs",
                "exception": {
                    "values": [
                        {
                            "type": "RuntimeError",
                            "value": f"Failed to read cron database: [Errno 13] "
                            f"Permission denied: '{path}'",
                        }
                    ]
                },
            },
            {
                "logger": "cron.jobs",
                "message": f"IOError reading jobs.json: [Errno 13] Permission denied: '{path}'",
            },
        ):
            ok, _ = throttle.should_send(event)
            sent += int(ok)

    # 11 sends per issue (2^0..2^10 fit under 1900), two issues.
    assert sent == 22, f"expected 11 sends per issue, got {sent}"
    assert sent / (ticks * 2) < 0.01, "throttle must cut incident volume by >99%"


def test_module_level_hook_drops_repeats_and_annotates() -> None:
    from shared.sentry_ratelimit import reset_for_tests

    reset_for_tests()
    event = {"logger": "cron.jobs", "message": "stuck"}
    first = throttle_event(dict(event))
    assert first is not None
    assert first["extra"]["smd.occurrence"] == 1
    throttle_event(dict(event))
    assert throttle_event(dict(event)) is None


def _word(i: int) -> str:
    """A distinct alphabetic token per index — survives normalize() intact."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    out = ""
    n = i
    while True:
        out = letters[n % 26] + out
        n //= 26
        if n == 0:
            break
    return out
