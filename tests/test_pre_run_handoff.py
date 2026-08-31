"""Tests for the pre-run handoff (shared/pre_run_handoff.py, ss-console#2547).

The handoff turns a routine's PRE-RUN read into a provenance source for the one
session that read was performed for. Everything worth testing here is a way it
could stop being "the one session": the recency window, the consume-once
rename, a file from yesterday, the persona-home split that made the first
shipped version inert (2026-08-24 defect A), and — the property the whole
design rests on — that only DATE atoms and validated ``(matterNumber, dates)``
records come out of it, so an ACK code or a caption the script happened to
write down can never verify a draft.

BINDING IS TESTED ON THE READER'S CLOCK. The first shipped version bound on the
cron session id's wall-clock digits and was falsified by the pilot the first
morning it ran (defect B: the id is stamped in the routine's cron timezone, the
container runs UTC, nothing ever bound). These tests pass ``now=`` explicitly —
the same seam the production caller leaves unset — so every window case is
deterministic.
"""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

from shared import cron_attribution, pre_run_handoff

_SKILL = "deadline-miss-escalator"
_STARTED = datetime(2026, 8, 22, 14, 0, 0, tzinfo=timezone.utc)
#: A fresh reader clock: the scheduler starts the turn seconds after the script.
_NOW = _STARTED + timedelta(minutes=2)
#: Any non-None value proves "this is a cron session". Its VALUE is irrelevant
#: to binding by design — see the defect-B regression test below.
_CRON_PROOF = datetime(2026, 8, 22, 7, 0, 26)


def _write(
    tmp_path,
    *,
    started_at=_STARTED,
    dates=("2026-08-29",),
    matter_ids=("2026-PI-101",),
    records=None,
):
    return pre_run_handoff.write_handoff(
        _SKILL, started_at, dates, matter_ids, hermes_home=str(tmp_path), records=records
    )


def _take(tmp_path, *, now=_NOW, session=_CRON_PROOF, persona=None):
    return pre_run_handoff.take_handoff(
        _SKILL, session, hermes_home=str(tmp_path), persona=persona, now=now
    )


def test_write_then_take_returns_the_dates(tmp_path):
    _write(tmp_path)
    assert _take(tmp_path) == {"dates": ["2026-08-29"], "records": []}


def test_the_projection_carries_dates_and_records_and_nothing_else(tmp_path):
    """The load-bearing property. The FILE may record what the script saw; what
    comes back out is the date atoms and validated records alone. An ACK code, a
    caption, a bare matter id or a sentence of the script's own prose reaching
    the register would let a script certify values nobody read."""
    pre_run_handoff.write_handoff(
        _SKILL,
        _STARTED,
        ["2026-08-29", "2026-09-02"],
        ["f220c8e4-eab5-4fd9-8f1d-0becf715b390"],
        hermes_home=str(tmp_path),
    )
    taken = _take(tmp_path)
    assert set(taken) == {"dates", "records"}
    assert taken["dates"] == ["2026-08-29", "2026-09-02"]
    assert taken["records"] == []


def test_a_non_date_in_the_dates_field_does_not_come_back(tmp_path):
    """The field name is not the projection; the SHAPE is."""
    path = pre_run_handoff.write_handoff(
        _SKILL,
        _STARTED,
        ["2026-08-29", "1:24-cv-01234", "A123456789", "call the paralegal"],
        [],
        hermes_home=str(tmp_path),
    )
    assert len(json.loads(path.read_text(encoding="utf-8"))["dates"]) == 4
    assert _take(tmp_path) == {"dates": ["2026-08-29"], "records": []}


def test_a_longhand_date_is_still_a_date(tmp_path):
    pre_run_handoff.write_handoff(
        _SKILL, _STARTED, ["August 29, 2026", "8/29/26"], [], hermes_home=str(tmp_path)
    )
    assert _take(tmp_path)["dates"] == ["August 29, 2026", "8/29/26"]


# ---- recency binding (defect B's replacement) -----------------------------


def test_a_fresh_file_binds(tmp_path):
    _write(tmp_path)
    assert _take(tmp_path, now=_STARTED + timedelta(seconds=30)) is not None


def test_a_file_older_than_the_window_does_not_bind(tmp_path):
    _write(tmp_path)
    assert _take(tmp_path, now=_STARTED + timedelta(minutes=21)) is None


def test_yesterdays_handoff_cannot_certify_todays_session(tmp_path):
    """The daily routine's own failure mode: the same file name, written every
    morning. Without the window a run that crashed before its turn would leave a
    handoff that verifies tomorrow's dates."""
    _write(tmp_path, started_at=_STARTED - timedelta(days=1))
    assert _take(tmp_path) is None


def test_a_stamp_from_the_future_does_not_bind(tmp_path):
    """Beyond the skew allowance, a future stamp is corruption, not clock drift."""
    _write(tmp_path, started_at=_STARTED + timedelta(minutes=10))
    assert _take(tmp_path, now=_STARTED) is None


def test_small_clock_skew_is_tolerated(tmp_path):
    _write(tmp_path, started_at=_STARTED + timedelta(seconds=45))
    assert _take(tmp_path, now=_STARTED) is not None


def test_a_naive_file_stamp_does_not_bind(tmp_path):
    """The writer stamps an explicit UTC offset. A stamp that does not say which
    clock it was on cannot be compared against any clock honestly."""
    directory = pre_run_handoff.handoff_dir(str(tmp_path))
    directory.mkdir(mode=0o700, parents=True)
    pre_run_handoff.handoff_path(_SKILL, str(tmp_path)).write_text(
        json.dumps({"skill": _SKILL, "started_at": "2026-08-22T14:00:00", "dates": ["2026-08-29"]}),
        encoding="utf-8",
    )
    assert _take(tmp_path) is None


def test_the_session_stamp_value_is_irrelevant_to_binding(tmp_path):
    """THE DEFECT-B REGRESSION. The pilot's scheduler stamps the cron session id
    with the fire time in the routine's cron timezone (``…_070026`` for a
    14:00Z fire) while the container clock is UTC — so any binding rule that
    interprets those digits on a clock this module can see reads them seven
    hours wrong, and the first shipped version never bound once in production.
    Binding is by file recency now; the stamp is only proof the session is a
    cron session, and a stamp from the WRONG clock must still seed."""
    _write(tmp_path)
    phoenix_local_stamp = datetime(2026, 8, 22, 7, 0, 26)  # 14:00Z, stamped Phoenix
    assert _take(tmp_path, session=phoenix_local_stamp) is not None


# ---- one session, one claim -----------------------------------------------


def test_an_out_of_window_file_is_left_in_place(tmp_path):
    path = _write(tmp_path)
    _take(tmp_path, now=_STARTED + timedelta(hours=2))
    assert path.exists()
    assert not pre_run_handoff.consumed_path(_SKILL, str(tmp_path)).exists()


def test_taking_consumes_the_handoff_exactly_once(tmp_path):
    path = _write(tmp_path)
    first = _take(tmp_path)
    second = _take(tmp_path, now=_NOW + timedelta(minutes=1))
    assert first == {"dates": ["2026-08-29"], "records": []}
    assert second is None
    assert not path.exists()
    assert pre_run_handoff.consumed_path(_SKILL, str(tmp_path)).exists()


def test_a_missing_handoff_is_not_an_error(tmp_path):
    assert _take(tmp_path) is None


def test_a_malformed_handoff_seeds_nothing(tmp_path):
    directory = pre_run_handoff.handoff_dir(str(tmp_path))
    directory.mkdir(mode=0o700, parents=True)
    pre_run_handoff.handoff_path(_SKILL, str(tmp_path)).write_text("{not json", encoding="utf-8")
    assert _take(tmp_path) is None


def test_a_non_cron_session_never_takes_a_handoff(tmp_path):
    """``session_started_at is None`` is how an interactive turn arrives here.
    A person's conversation must not inherit a routine's reads."""
    _write(tmp_path)
    assert _take(tmp_path, session=None) is None


# ---- the persona home (defect A) ------------------------------------------


def test_a_handoff_written_under_the_persona_home_is_found(tmp_path):
    """THE DEFECT-A REGRESSION. The scheduler runs the writer with the PERSONA
    home as ``HERMES_HOME`` (``/opt/data/profiles/operator``), so the file lands
    under it — probed on the running pilot 2026-08-24, one unconsumed file per
    routine, while the reader looked one root up and seeded nothing. A reader
    that knows the persona looks where the writer wrote."""
    persona_home = tmp_path / "profiles" / "operator"
    pre_run_handoff.write_handoff(
        _SKILL, _STARTED, ["2026-08-29"], [], hermes_home=str(persona_home)
    )
    taken = _take(tmp_path, persona="operator")
    assert taken == {"dates": ["2026-08-29"], "records": []}
    assert pre_run_handoff.consumed_path(_SKILL, str(tmp_path), "operator").exists()


def test_the_plain_root_is_still_the_fallback(tmp_path):
    """A seat where both processes share one ``HERMES_HOME`` keeps working."""
    _write(tmp_path)
    assert _take(tmp_path, persona="operator") == {"dates": ["2026-08-29"], "records": []}


def test_without_a_persona_the_persona_home_is_not_searched(tmp_path):
    persona_home = tmp_path / "profiles" / "operator"
    pre_run_handoff.write_handoff(
        _SKILL, _STARTED, ["2026-08-29"], [], hermes_home=str(persona_home)
    )
    assert _take(tmp_path) is None


def test_a_persona_name_cannot_choose_the_path(tmp_path):
    path = pre_run_handoff.handoff_path(_SKILL, str(tmp_path), "../../etc")
    assert str(tmp_path) in str(path)
    assert ".." not in path.parts


# ---- the record projection (the 2026-08-24 widening) ----------------------


def test_a_valid_record_comes_back_with_its_pairing_intact(tmp_path):
    _write(
        tmp_path,
        records=[{"matterNumber": "2026-PI-101", "dates": ["2026-08-29", "2026-09-02"]}],
    )
    taken = _take(tmp_path)
    assert taken["records"] == [
        {"matterNumber": "2026-PI-101", "dates": ["2026-08-29", "2026-09-02"]}
    ]


def test_a_record_whose_number_is_not_a_case_number_is_dropped_whole(tmp_path):
    """An ACK code, a GUID, or prose in the number slot drops the RECORD, not
    just the field — a record is an association, and an association anchored on
    a non-number associates nothing."""
    _write(
        tmp_path,
        records=[
            {"matterNumber": "ACK-6WS08D", "dates": ["2026-08-29"]},
            {"matterNumber": "f220c8e4-eab5-4fd9-8f1d-0becf715b390", "dates": ["2026-08-29"]},
            {"matterNumber": "call the paralegal", "dates": ["2026-08-29"]},
        ],
    )
    assert _take(tmp_path)["records"] == []


def test_a_record_with_no_surviving_dates_is_dropped(tmp_path):
    _write(tmp_path, records=[{"matterNumber": "2026-PI-101", "dates": ["not a date"]}])
    assert _take(tmp_path)["records"] == []


def test_a_non_date_inside_a_records_dates_is_dropped(tmp_path):
    _write(
        tmp_path,
        records=[{"matterNumber": "2026-PI-101", "dates": ["2026-08-29", "1:24-cv-01234"]}],
    )
    assert _take(tmp_path)["records"] == [{"matterNumber": "2026-PI-101", "dates": ["2026-08-29"]}]


def test_records_are_bounded(tmp_path):
    many = [{"matterNumber": f"2026-PI-{n:03d}", "dates": ["2026-08-29"]} for n in range(101, 351)]
    _write(tmp_path, records=many)
    assert len(_take(tmp_path)["records"]) == 100


# ---- hygiene ---------------------------------------------------------------


def test_the_handoff_is_private_to_its_owner(tmp_path):
    path = _write(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(pre_run_handoff.handoff_dir(str(tmp_path)).stat().st_mode) == 0o700


def test_write_leaves_no_temp_file_behind(tmp_path):
    _write(tmp_path)
    names = sorted(p.name for p in pre_run_handoff.handoff_dir(str(tmp_path)).iterdir())
    assert names == [f"{_SKILL}.json"]


def test_a_skill_name_cannot_choose_the_path(tmp_path):
    path = pre_run_handoff.handoff_path("../../etc/passwd", str(tmp_path))
    assert path.parent == pre_run_handoff.handoff_dir(str(tmp_path))
    assert "/" not in path.name.removesuffix(".json")


def test_the_file_records_matter_ids_even_though_the_projection_omits_them(tmp_path):
    path = _write(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["matter_ids"] == ["2026-PI-101"]
    assert payload["skill"] == _SKILL


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


# ---------------------------------------------------------------------------
# Bare-digit matter numbers (ss#2458). A&P's numbers are plain digit runs
# ("201537", "4853"); before the second acceptance branch, _record_entries
# dropped every such record AT SEEDING and the handoff delivered nothing for
# that firm. The branch is safe at this seam only: the value was produced by
# the writer's own connector pull and is consumed by structured add_record
# seeding — the shape check guards junk, not collision.
# ---------------------------------------------------------------------------


def test_a_bare_digit_record_is_accepted(tmp_path):
    _write(
        tmp_path,
        records=[
            {"matterNumber": "201537", "dates": ["2026-08-29"]},
            {"matterNumber": "4853", "dates": ["2026-09-02"]},
        ],
    )
    assert _take(tmp_path)["records"] == [
        {"matterNumber": "201537", "dates": ["2026-08-29"]},
        {"matterNumber": "4853", "dates": ["2026-09-02"]},
    ]


def test_junk_digit_runs_are_still_dropped(tmp_path):
    _write(
        tmp_path,
        records=[
            {"matterNumber": "12", "dates": ["2026-08-29"]},  # two digits: a day
            {"matterNumber": "123456789012", "dates": ["2026-08-29"]},  # 12 digits: never a matter
            {"matterNumber": "20-15", "dates": ["2026-08-29"]},  # punctuated junk
            {"matterNumber": "call the paralegal", "dates": ["2026-08-29"]},
        ],
    )
    assert _take(tmp_path)["records"] == []


def test_a_date_shaped_number_is_still_dropped(tmp_path):
    # "2026-08-29" extracts as a DATE, not a case number, and its digits do not
    # fullmatch the bare form — a date must never seed as a matter number.
    _write(tmp_path, records=[{"matterNumber": "2026-08-29", "dates": ["2026-08-29"]}])
    assert _take(tmp_path)["records"] == []
