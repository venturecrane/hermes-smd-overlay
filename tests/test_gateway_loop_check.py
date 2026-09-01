"""shared.gateway_loop_check (ss-console#2488 part 2).

Every test here is a hold/report boundary. The console treats an ABSENT field as
"nothing to say" and holds whatever it last knew; a PRESENT field resolves or
opens an alert. So a wrong None here is an alert that never resolves, and a
wrong number here is a false page on every deploy. Both have happened to
sibling checks in this overlay (ss-console#2287, #2291).
"""

from __future__ import annotations

import os
import time

import pytest

from shared.gateway_loop_check import (
    LEDGER_WINDOW_SECONDS,
    SUPERVISOR_STATES,
    GatewayLoopChecker,
    heartbeat_path,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    run = tmp_path / "run"
    ledger = tmp_path / "ledger"
    for d in (home, run, ledger):
        d.mkdir()
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "crane")
    monkeypatch.setenv("HERMES_HOME", str(home))

    class W:
        pass

    w = W()
    w.home, w.run, w.ledger = home, run, ledger

    def beat(age: float = 0.0, profile: str = "crane"):
        p = heartbeat_path(profile, str(home))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"pid": 657}')
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    w.beat = beat
    w.checker = lambda: GatewayLoopChecker(home=str(home), run_dir=str(run), ledger_dir=str(ledger))
    return w


# -- arming latch -------------------------------------------------------------


def test_fresh_beat_arms_and_reports_age(world):
    world.beat(age=5)
    c = world.checker()
    r = c.check(uptime_seconds=10_000)
    assert r.ok is True
    assert c.armed is True
    assert 4 <= r.age_seconds <= 7


def test_stale_beat_from_previous_boot_does_not_arm(world):
    """The volume persists: a 2-hour-old beat is on disk at every cold start and
    this gate is forked before the gateway exec. Reporting that age would open
    gateway_loop_wedged on every deploy. Hold until seen fresh once."""
    world.beat(age=7200)
    c = world.checker()
    r = c.check(uptime_seconds=10_000)
    assert r.ok is True
    assert r.age_seconds is None
    assert c.armed is False
    assert "not armed" in r.reason


def test_once_armed_a_stale_beat_is_reported(world):
    p = world.beat(age=5)
    c = world.checker()
    c.check(uptime_seconds=10_000)
    assert c.armed
    old = time.time() - 400
    os.utime(p, (old, old))
    r = c.check(uptime_seconds=10_000)
    assert r.ok is True
    assert 399 <= r.age_seconds <= 402


def test_boot_suppression_withholds_age_even_when_armed(world):
    world.beat(age=5)
    c = world.checker()
    r = c.check(uptime_seconds=30)
    assert c.armed is True  # the latch still opens
    assert r.age_seconds is None
    assert "boot suppression" in r.reason


# -- hold vs blindness ----------------------------------------------------------


def test_missing_heartbeat_file_is_a_hold_not_blindness(world):
    """hermes-smd-staging runs a Hermes pin with no loop heartbeat at all
    (vfy_01M0HBR1NZHSRMWSFPSQM32D1E). That is not a fault of this check, so it
    must not page unprovable; it is simply nothing to say."""
    r = world.checker().check(uptime_seconds=10_000)
    assert r.ok is True
    assert r.age_seconds is None


def test_missing_profile_directory_is_also_a_hold(world):
    """FileNotFoundError covers a missing parent too; a PermissionError does not
    hide behind it (the overlay's never-path.exists() rule)."""
    monkey_home = world.home / "nowhere"
    r = GatewayLoopChecker(
        home=str(monkey_home), run_dir=str(world.run), ledger_dir=str(world.ledger)
    ).check(uptime_seconds=10_000)
    assert r.ok is True
    assert r.age_seconds is None


def test_unreadable_heartbeat_is_blindness(world):
    """os.stat on a directory we cannot traverse raises PermissionError. That is
    ok=False: a check that cannot look must never read as green."""
    if os.geteuid() == 0:
        pytest.skip("root ignores mode bits")
    p = world.beat(age=5)
    state_dir = p.parent
    state_dir.chmod(0o000)
    try:
        r = world.checker().check(uptime_seconds=10_000)
    finally:
        state_dir.chmod(0o755)
    assert r.ok is False
    assert r.age_seconds is None
    assert "cannot read heartbeat" in r.reason


def test_no_active_profile_is_blindness(world, monkeypatch):
    monkeypatch.delenv("HERMES_ACTIVE_PROFILE")
    world.beat(age=5)
    r = world.checker().check(uptime_seconds=10_000)
    assert r.ok is False
    assert "HERMES_ACTIVE_PROFILE" in r.reason


# -- clock and type hygiene ------------------------------------------------------


def test_age_is_an_int_and_never_negative(world):
    """The console's parseNonNegInt rejects floats (would NULL the column
    forever) and negatives (would hold). A future mtime after a Fly resume is
    real; clamp it."""
    p = world.beat(age=0)
    future = time.time() + 3600
    os.utime(p, (future, future))
    c = world.checker()
    r = c.check(uptime_seconds=10_000)
    assert c.armed  # 0 <= 120
    assert isinstance(r.age_seconds, int)
    assert r.age_seconds == 0


# -- supervisor artefacts (part 1) ---------------------------------------------


def test_supervisor_state_is_read_and_vocabulary_is_closed(world):
    world.beat(age=5)
    (world.run / "state").write_text("refusing\n")
    assert world.checker().check(uptime_seconds=10_000).supervisor_state == "refusing"
    (world.run / "state").write_text("banana\n")
    assert world.checker().check(uptime_seconds=10_000).supervisor_state is None


@pytest.mark.parametrize("word", sorted(SUPERVISOR_STATES))
def test_every_word_the_entrypoint_can_write_is_forwarded(world, word):
    """The closed vocabulary is only safe while it is COMPLETE.

    ``_read_supervisor_state`` drops anything it does not recognise, and a drop
    is a NULL, and a NULL is a hold -- so a state the seat writes and this set
    omits is not a loud failure, it is silence. That is the failure mode the
    2026-09-01 crash loop produced from the other direction, so the parity is
    asserted per word rather than by eyeballing a frozenset.

    The other half of the parity lives in ss-console: the entrypoint's
    ``gateway_liveness_state`` calls and this set move in the same change.
    """
    world.beat(age=5)
    (world.run / "state").write_text(f"{word}\n")
    assert world.checker().check(uptime_seconds=10_000).supervisor_state == word


def test_no_supervisor_artefacts_report_none(world):
    """A pin without the part-1 supervisor has no state file and no ledger
    directory. Both fields must be absent, not zero."""
    world.beat(age=5)
    world.ledger.rmdir()
    r = world.checker().check(uptime_seconds=10_000)
    assert r.supervisor_state is None
    assert r.restarts_last_hour is None


def test_ledger_dir_without_file_is_zero_restarts(world):
    world.beat(age=5)
    assert world.checker().check(uptime_seconds=10_000).restarts_last_hour == 0


def test_ledger_counts_only_the_last_hour_and_ignores_garbage(world):
    world.beat(age=5)
    now = int(time.time())
    (world.ledger / "kills").write_text(
        "\n".join(
            [
                f"{now - 10} iso loop-wedge",
                f"{now - 3000} iso loop-wedge",
                f"{now - LEDGER_WINDOW_SECONDS - 5} iso loop-wedge",  # outside
                "not-a-line",
                "",
            ]
        )
    )
    assert world.checker().check(uptime_seconds=10_000).restarts_last_hour == 2
