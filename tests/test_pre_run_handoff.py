"""Tests for the pre-run handoff (shared/pre_run_handoff.py, ss-console#2547).

The handoff turns a routine's PRE-RUN read into a provenance source for the one
session that read was performed for. Everything worth testing here is a way it
could stop being "the one session": the binding window, the consume-once rename,
a file from yesterday, and — the property the whole design rests on — that only
DATE atoms come out of it, so an ACK code or a caption the script happened to
write down can never verify a draft.
"""

from __future__ import annotations

import json
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest

from shared import cron_attribution, pre_run_handoff

_SKILL = "deadline-miss-escalator"
_STARTED = datetime(2026, 8, 22, 14, 0, 0, tzinfo=timezone.utc)


def _write(tmp_path, *, started_at=_STARTED, dates=("2026-08-29",), matter_ids=("2026-PI-101",)):
    return pre_run_handoff.write_handoff(
        _SKILL, started_at, dates, matter_ids, hermes_home=str(tmp_path)
    )


def test_write_then_take_returns_the_dates(tmp_path):
    _write(tmp_path)
    taken = pre_run_handoff.take_handoff(
        _SKILL, _STARTED + timedelta(minutes=2), hermes_home=str(tmp_path)
    )
    assert taken == {"dates": ["2026-08-29"]}


def test_the_projection_carries_dates_and_nothing_else(tmp_path):
    """The load-bearing property. The FILE may record what the script saw; what
    comes back out is the date atoms alone. An ACK code, a caption, a matter
    number or a sentence of the script's own prose reaching the register would
    let a script certify values nobody read."""
    pre_run_handoff.write_handoff(
        _SKILL,
        _STARTED,
        ["2026-08-29", "2026-09-02"],
        ["2026-PI-101"],
        hermes_home=str(tmp_path),
    )
    taken = pre_run_handoff.take_handoff(
        _SKILL, _STARTED + timedelta(minutes=1), hermes_home=str(tmp_path)
    )
    assert set(taken) == {"dates"}
    assert taken["dates"] == ["2026-08-29", "2026-09-02"]


def test_a_non_date_in_the_dates_field_does_not_come_back(tmp_path):
    """The field name is not the projection; the SHAPE is.

    The writer is an inline copy in another repository, so what lands in the
    ``dates`` list is authored by code this module cannot test. If the field name
    were the whole rule, a script that printed a case number into that list would
    be laundering it into the register.
    """
    path = pre_run_handoff.write_handoff(
        _SKILL,
        _STARTED,
        ["2026-08-29", "1:24-cv-01234", "A123456789", "call the paralegal"],
        [],
        hermes_home=str(tmp_path),
    )
    # The FILE keeps what the script offered, so the difference is diagnosable.
    assert len(json.loads(path.read_text(encoding="utf-8"))["dates"]) == 4
    taken = pre_run_handoff.take_handoff(
        _SKILL, _STARTED + timedelta(minutes=1), hermes_home=str(tmp_path)
    )
    assert taken == {"dates": ["2026-08-29"]}


def test_a_longhand_date_is_still_a_date(tmp_path):
    """The scripts emit whatever the firm's record spells. The predicate is the
    gate's own extractor, so anything the gate would CALL a date qualifies."""
    pre_run_handoff.write_handoff(
        _SKILL, _STARTED, ["August 29, 2026", "8/29/26"], [], hermes_home=str(tmp_path)
    )
    taken = pre_run_handoff.take_handoff(
        _SKILL, _STARTED + timedelta(minutes=1), hermes_home=str(tmp_path)
    )
    assert taken["dates"] == ["August 29, 2026", "8/29/26"]


def test_a_session_that_started_before_the_script_does_not_bind(tmp_path):
    _write(tmp_path)
    assert (
        pre_run_handoff.take_handoff(
            _SKILL, _STARTED - timedelta(seconds=1), hermes_home=str(tmp_path)
        )
        is None
    )


def test_a_session_past_the_window_does_not_bind(tmp_path):
    _write(tmp_path)
    assert (
        pre_run_handoff.take_handoff(
            _SKILL, _STARTED + timedelta(minutes=21), hermes_home=str(tmp_path)
        )
        is None
    )


def test_yesterdays_handoff_cannot_certify_todays_session(tmp_path):
    """The daily routine's own failure mode: the same file name, written every
    morning. Without the window a run that crashed before its turn would leave a
    handoff that verifies tomorrow's dates."""
    _write(tmp_path, started_at=_STARTED - timedelta(days=1))
    assert (
        pre_run_handoff.take_handoff(
            _SKILL, _STARTED + timedelta(minutes=1), hermes_home=str(tmp_path)
        )
        is None
    )


def test_an_out_of_window_file_is_left_in_place(tmp_path):
    """It may still belong to a session that has not started yet. Consuming on a
    miss would let one early consult burn the handoff its own turn needs."""
    path = _write(tmp_path)
    pre_run_handoff.take_handoff(_SKILL, _STARTED - timedelta(hours=1), hermes_home=str(tmp_path))
    assert path.exists()
    assert not pre_run_handoff.consumed_path(_SKILL, str(tmp_path)).exists()


def test_taking_consumes_the_handoff_exactly_once(tmp_path):
    path = _write(tmp_path)
    first = pre_run_handoff.take_handoff(
        _SKILL, _STARTED + timedelta(minutes=1), hermes_home=str(tmp_path)
    )
    second = pre_run_handoff.take_handoff(
        _SKILL, _STARTED + timedelta(minutes=2), hermes_home=str(tmp_path)
    )
    assert first == {"dates": ["2026-08-29"]}
    assert second is None
    assert not path.exists()
    assert pre_run_handoff.consumed_path(_SKILL, str(tmp_path)).exists()


def test_a_missing_handoff_is_not_an_error(tmp_path):
    assert pre_run_handoff.take_handoff(_SKILL, _STARTED, hermes_home=str(tmp_path)) is None


def test_a_malformed_handoff_seeds_nothing(tmp_path):
    directory = pre_run_handoff.handoff_dir(str(tmp_path))
    directory.mkdir(mode=0o700, parents=True)
    pre_run_handoff.handoff_path(_SKILL, str(tmp_path)).write_text("{not json", encoding="utf-8")
    assert pre_run_handoff.take_handoff(_SKILL, _STARTED, hermes_home=str(tmp_path)) is None


def test_a_non_cron_session_never_takes_a_handoff(tmp_path):
    """``session_started_at is None`` is how an interactive turn arrives here.
    A person's conversation must not inherit a routine's reads."""
    _write(tmp_path)
    assert pre_run_handoff.take_handoff(_SKILL, None, hermes_home=str(tmp_path)) is None


def test_the_handoff_is_private_to_its_owner(tmp_path):
    path = _write(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(pre_run_handoff.handoff_dir(str(tmp_path)).stat().st_mode) == 0o700


def test_write_leaves_no_temp_file_behind(tmp_path):
    _write(tmp_path)
    names = sorted(p.name for p in pre_run_handoff.handoff_dir(str(tmp_path)).iterdir())
    assert names == [f"{_SKILL}.json"]


def test_a_skill_name_cannot_choose_the_path(tmp_path):
    """The skill name reaches this module from a script's own argument."""
    path = pre_run_handoff.handoff_path("../../etc/passwd", str(tmp_path))
    assert path.parent == pre_run_handoff.handoff_dir(str(tmp_path))
    assert "/" not in path.name.removesuffix(".json")


def test_the_file_records_matter_ids_even_though_the_projection_omits_them(tmp_path):
    """The file is also a forensic record of what the script saw. Recording is
    not seeding, and the two must be visibly different."""
    path = _write(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["matter_ids"] == ["2026-PI-101"]
    assert payload["skill"] == _SKILL


@pytest.fixture
def phoenix_clock(monkeypatch):
    """Run the body on a seat seven hours behind UTC — the real seats' zone.

    Pinned rather than inherited, because CI runs in UTC and on a UTC host the
    two clock readings below are the SAME value: the pair of tests would both
    pass while proving nothing about the ambiguity they exist for.
    """
    monkeypatch.setenv("TZ", "America/Phoenix")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_local_clock_session_stamp_binds(tmp_path, phoenix_clock):
    """The scheduler stamps the session id in wall-clock digits and does not say
    which clock. A stamp written on the SEAT's clock binds..."""
    _write(tmp_path)
    local_stamp = (_STARTED + timedelta(minutes=1)).astimezone().replace(tzinfo=None)
    assert local_stamp.hour == 7  # the ambiguity is real on this clock
    assert pre_run_handoff.take_handoff(_SKILL, local_stamp, hermes_home=str(tmp_path)) == {
        "dates": ["2026-08-29"]
    }


def test_a_utc_clock_session_stamp_binds(tmp_path, phoenix_clock):
    """...and so does the same instant stamped in UTC. Picking one clock and
    picking wrong would leave the control inert with no signal that it was."""
    _write(tmp_path)
    utc_stamp = (_STARTED + timedelta(minutes=1)).replace(tzinfo=None)
    assert utc_stamp.hour == 14
    assert pre_run_handoff.take_handoff(_SKILL, utc_stamp, hermes_home=str(tmp_path)) == {
        "dates": ["2026-08-29"]
    }


def test_a_stamp_that_is_neither_clock_does_not_bind(tmp_path, phoenix_clock):
    """Trying both readings is not the same as trying every reading. Seven hours
    is what separates the two candidates; twenty minutes is what separates a
    session from someone else's."""
    _write(tmp_path)
    stray = (_STARTED + timedelta(hours=3)).replace(tzinfo=None)
    assert pre_run_handoff.take_handoff(_SKILL, stray, hermes_home=str(tmp_path)) is None


def test_the_session_start_comes_out_of_the_cron_session_id():
    assert cron_attribution.parse_cron_session_started_at(
        "cron_a726fd5efd24_20260822_070000"
    ) == datetime(2026, 8, 22, 7, 0, 0)


def test_a_non_cron_session_id_has_no_start():
    assert cron_attribution.parse_cron_session_started_at("telegram-3391") is None
    assert cron_attribution.parse_cron_session_started_at("") is None
    assert cron_attribution.parse_cron_session_started_at(None) is None


def test_an_impossible_stamp_has_no_start():
    assert cron_attribution.parse_cron_session_started_at("cron_x_20260231_120000") is None


def test_write_on_an_unusable_home_returns_none_rather_than_raising(tmp_path):
    """A pre-run must not die because an optimization could not be recorded.

    A FILE standing where ``$HERMES_HOME`` should be is the cheapest way to make
    every path operation under it fail, and it exercises the same handler a
    read-only volume would.
    """
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    assert _write(blocked) is None


def test_atoms_are_bounded_and_deduplicated(tmp_path):
    path = pre_run_handoff.write_handoff(
        _SKILL,
        _STARTED,
        ["2026-08-29", "2026-08-29", "", "x" * 200, 42, None],
        [],
        hermes_home=str(tmp_path),
    )
    assert json.loads(path.read_text(encoding="utf-8"))["dates"] == ["2026-08-29"]
