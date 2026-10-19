"""Fleet-table heartbeat fields (shared/fleet_facts.py + build_payload wiring).

Null is unknown, 0 is none. The tests that matter most are the ones where a
seat must NOT look idle: a stale or malformed lane file makes every job sum
unknown, because a 0 is what tells a rollout it may restart the seat.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from shared import fleet_facts as ff
from shared import heartbeat as hb

NOW = 1_800_000_000.0


def _lane(run_dir, name, **fields):
    doc = {"last_poll_at": NOW - 5, "running": None, "queued": 0, "wakes_pending": 0}
    doc.update(fields)
    (run_dir / f"{name}.json").write_text(json.dumps(doc))


def _counts(run_dir):
    return ff.read_lane_counts(run_dir, clock=lambda: NOW)


def test_image_ref_from_env_else_none():
    assert ff.read_image_ref({"FLY_IMAGE_REF": "registry.fly.io/hermes-fleet:rel-1"}) == (
        "registry.fly.io/hermes-fleet:rel-1"
    )
    assert ff.read_image_ref({}) is None
    assert ff.read_image_ref({"FLY_IMAGE_REF": "  "}) is None


def test_config_digest_is_sha256_of_the_bytes(tmp_path):
    cfg = tmp_path / "customer.yaml"
    cfg.write_bytes(b"customer:\n  slug: acme\n")
    assert ff.read_config_digest(cfg) == hashlib.sha256(cfg.read_bytes()).hexdigest()
    assert ff.read_config_digest(tmp_path / "absent.yaml") is None


def test_skills_version_stripped_capped_and_missing(tmp_path):
    v = tmp_path / "VERSION"
    v.write_text("sk-20261019-abc123\n")
    assert ff.read_skills_version(v) == "sk-20261019-abc123"
    v.write_text("x" * 500)
    assert ff.read_skills_version(v) == "x" * 300
    v.write_text("   \n")
    assert ff.read_skills_version(v) is None
    assert ff.read_skills_version(tmp_path / "nope") is None


def test_no_lane_files_means_zero_not_unknown(tmp_path):
    # A seat with no lanes installed has no jobs.
    assert _counts(tmp_path) == ff.LaneCounts(0, 0, 0)
    assert _counts(tmp_path / "no-such-dir") == ff.LaneCounts(0, 0, 0)


def test_sums_across_lanes_and_normalizes_running(tmp_path):
    _lane(tmp_path, "heartbeat", running="job-1", queued=2, wakes_pending=1)
    _lane(tmp_path, "demand-heartbeat", running=None, queued=1)
    _lane(tmp_path, "negotiation-heartbeat", running=["a", "b"], wakes_pending=3)
    _lane(tmp_path, "note-heartbeat", running=4)
    assert _counts(tmp_path) == ff.LaneCounts(1 + 0 + 2 + 4, 3, 4)


def test_stale_lane_makes_every_sum_unknown(tmp_path):
    _lane(tmp_path, "heartbeat", running="job-1", queued=2)
    _lane(tmp_path, "drafting-heartbeat", last_poll_at=NOW - ff.LANE_STALE_SECONDS - 1)
    assert _counts(tmp_path) == ff.LaneCounts(None, None, None)


def test_stale_by_mtime_when_last_poll_at_absent(tmp_path):
    path = tmp_path / "litigation-heartbeat.json"
    path.write_text(json.dumps({"running": None, "queued": 0, "wakes_pending": 0}))
    old = NOW - 600
    os.utime(path, (old, old))
    assert _counts(tmp_path).running is None
    os.utime(path, (NOW - 10, NOW - 10))
    assert _counts(tmp_path) == ff.LaneCounts(0, 0, 0)


@pytest.mark.parametrize("body", ["not json", "[]", "{"])
def test_malformed_lane_is_unknown(tmp_path, body):
    (tmp_path / "heartbeat.json").write_text(body)
    assert _counts(tmp_path) == ff.LaneCounts(None, None, None)


def test_malformed_field_makes_that_sum_unknown(tmp_path):
    _lane(tmp_path, "heartbeat", queued=-1, running=True)
    counts = _counts(tmp_path)
    assert counts.queued is None and counts.running is None
    assert counts.wakes_pending == 0


def test_missing_count_field_is_unknown_not_zero(tmp_path):
    (tmp_path / "heartbeat.json").write_text(json.dumps({"last_poll_at": NOW, "running": None}))
    counts = _counts(tmp_path)
    assert counts.running == 0
    assert counts.queued is None and counts.wakes_pending is None


def test_read_fleet_facts_composes(tmp_path):
    cfg = tmp_path / "customer.yaml"
    cfg.write_bytes(b"a: 1\n")
    ver = tmp_path / "VERSION"
    ver.write_text("v7\n")
    run = tmp_path / "run"
    run.mkdir()
    _lane(run, "heartbeat", running="j", queued=1, wakes_pending=0)
    facts = ff.read_fleet_facts(
        env={"FLY_IMAGE_REF": "img"},
        config_path=cfg,
        skills_version_path=ver,
        run_dir=run,
        clock=lambda: NOW,
    )
    assert facts == ff.FleetFacts("img", hashlib.sha256(b"a: 1\n").hexdigest(), "v7", 1, 1, 0)


_BASE = dict(
    heartbeat_ts="t", last_audit_ts=None, last_skill_ts=None, uptime_seconds=None, version=None
)


def test_payload_carries_the_six_fields_with_exact_names():
    p = hb.build_payload(
        **_BASE,
        image_ref="img",
        config_digest="d" * 64,
        skills_version="v7",
        jobs_running=0,
        jobs_queued=0,
        wakes_pending=0,
    )
    assert p["image_ref"] == "img"
    assert p["config_digest"] == "d" * 64
    assert p["skills_version"] == "v7"
    # 0 is a real value (idle) and must reach the wire.
    assert p["jobs_running"] == 0 and p["jobs_queued"] == 0 and p["wakes_pending"] == 0


def test_payload_omits_unknown_fleet_fields():
    p = hb.build_payload(**_BASE)
    for name in (
        "image_ref",
        "config_digest",
        "skills_version",
        "jobs_running",
        "jobs_queued",
        "wakes_pending",
    ):
        assert name not in p


def test_emitter_posts_fleet_facts(monkeypatch):
    """The wire seam: the emitter must pass what it read, not just read it."""
    from tests.test_heartbeat import _emitter

    monkeypatch.setattr(
        hb, "_read_fleet_facts", lambda: ff.FleetFacts("img", "c" * 64, "v9", 2, 0, 1)
    )
    em, calls = _emitter()
    em._tick()
    body = json.loads(calls["posts"][0][2])
    assert body["image_ref"] == "img"
    assert body["config_digest"] == "c" * 64
    assert body["skills_version"] == "v9"
    assert body["jobs_running"] == 2 and body["jobs_queued"] == 0 and body["wakes_pending"] == 1


def test_emitter_omits_fleet_fields_when_the_read_crashes(monkeypatch):
    from tests.test_heartbeat import _emitter

    def boom(**_kw):
        raise RuntimeError("x")

    monkeypatch.setattr(ff, "read_fleet_facts", boom)
    em, calls = _emitter()
    em._tick()
    body = json.loads(calls["posts"][0][2])
    assert "jobs_running" not in body and "image_ref" not in body
