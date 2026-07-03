"""Tests for the Machine → control-plane heartbeat emitter (shared/heartbeat.py).

Covers the pure pieces (payload assembly, audit-timestamp read) and the tick
logic with injected transport, without opening a socket. The emitter's
fail-soft contract — a failing POST or ping never escapes the thread — is the
load-bearing property, so it gets explicit coverage.
"""

from __future__ import annotations

import sqlite3

from shared import heartbeat as hb

_KEY = "shared-fleet-key"
_SLUG = "ashton-price"

_AUDIT_DDL = (
    "CREATE TABLE audit_log ("
    " id TEXT PRIMARY KEY, ts TEXT NOT NULL, action_type TEXT, actor TEXT,"
    " actor_role TEXT, skill_name TEXT, matter_ref TEXT)"
)


def _make_audit_db(path: str, rows: list[tuple[str, str, str | None]]) -> None:
    """rows = [(id, ts, skill_name), ...]."""
    conn = sqlite3.connect(path)
    conn.execute(_AUDIT_DDL)
    conn.executemany(
        "INSERT INTO audit_log (id, ts, action_type, skill_name) VALUES (?, ?, 'x', ?)",
        [(rid, ts, skill) for (rid, ts, skill) in rows],
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# build_payload
# ---------------------------------------------------------------------------


def test_payload_carries_only_heartbeat_ts_when_others_absent():
    p = hb.build_payload(
        heartbeat_ts="2026-07-02T00:00:00+00:00",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
    )
    assert p == {"heartbeat_ts": "2026-07-02T00:00:00+00:00"}


def test_payload_includes_present_optionals_and_omits_null():
    p = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts="a",
        last_skill_ts=None,
        uptime_seconds=42,
        version="abc123",
    )
    assert p == {
        "heartbeat_ts": "t",
        "last_audit_ts": "a",
        "process_uptime_seconds": 42,
        "version": "abc123",
    }
    assert "last_skill_ts" not in p


def test_uptime_zero_is_sent_not_dropped():
    # 0 is a legitimate value (just booted); the None-check must not treat it
    # as absent.
    p = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=0,
        version=None,
    )
    assert p["process_uptime_seconds"] == 0


# ---------------------------------------------------------------------------
# read_audit_timestamps
# ---------------------------------------------------------------------------


def test_audit_timestamps_missing_file_is_empty():
    assert hb.read_audit_timestamps("/no/such/audit.db") == (None, None)
    assert hb.read_audit_timestamps(None) == (None, None)


def test_audit_timestamps_empty_table_is_empty(tmp_path):
    db = tmp_path / "audit.db"
    _make_audit_db(str(db), [])
    assert hb.read_audit_timestamps(str(db)) == (None, None)


def test_audit_timestamps_returns_newest_audit_and_newest_skill(tmp_path):
    db = tmp_path / "audit.db"
    # ULID-style ascending ids; newest audit row has no skill, newest skill row
    # is older — so last_audit_ts != last_skill_ts and both must be correct.
    _make_audit_db(
        str(db),
        [
            ("01A", "2026-07-01T10:00:00+00:00", "matter_memo"),
            ("01B", "2026-07-01T11:00:00+00:00", None),
            ("01C", "2026-07-01T12:00:00+00:00", None),
        ],
    )
    last_audit, last_skill = hb.read_audit_timestamps(str(db))
    assert last_audit == "2026-07-01T12:00:00+00:00"
    assert last_skill == "2026-07-01T10:00:00+00:00"


def test_audit_timestamps_ignores_empty_string_skill(tmp_path):
    db = tmp_path / "audit.db"
    _make_audit_db(str(db), [("01A", "2026-07-01T10:00:00+00:00", "")])
    _last_audit, last_skill = hb.read_audit_timestamps(str(db))
    assert last_skill is None


def test_audit_read_does_not_write(tmp_path):
    # Opening ?mode=ro must reject writes — a heartbeat can never perturb audit.
    db = tmp_path / "audit.db"
    _make_audit_db(str(db), [("01A", "2026-07-01T10:00:00+00:00", None)])
    hb.read_audit_timestamps(str(db))
    # Reopen read-write and confirm the single row is intact (no side effects).
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------
# tick logic (injected transport)
# ---------------------------------------------------------------------------


def _emitter(**overrides):
    calls = {"posts": [], "pings": []}

    def post_fn(url, headers, body):
        calls["posts"].append((url, headers, body))
        return overrides.get("post_status", 200)

    def ping_fn(url):
        calls["pings"].append(url)

    kwargs = dict(
        slug=_SLUG,
        key=_KEY,
        ingest_url="https://smd.services/api/internal/heartbeat",
        healthchecks_url=overrides.get("healthchecks_url"),
        version="ref123",
        audit_db_path_fn=lambda: None,
        post_fn=post_fn,
        ping_fn=ping_fn,
    )
    return hb.HeartbeatEmitter(**kwargs), calls


def test_tick_posts_with_bearer_and_tenant_slug():
    em, calls = _emitter()
    em._tick()
    assert len(calls["posts"]) == 1
    url, headers, body = calls["posts"][0]
    assert url.endswith("/api/internal/heartbeat")
    assert headers["Authorization"] == f"Bearer {_KEY}"
    assert headers["X-Tenant-Slug"] == _SLUG
    assert b"heartbeat_ts" in body


def test_tick_pings_healthchecks_when_url_present():
    em, calls = _emitter(healthchecks_url="https://hc-ping.com/abc")
    em._tick()
    assert calls["pings"] == ["https://hc-ping.com/abc"]


def test_tick_skips_ping_when_no_url():
    em, calls = _emitter()
    em._tick()
    assert calls["pings"] == []


def test_tick_swallows_post_failure(caplog):
    def boom(url, headers, body):
        raise ConnectionError("network down")

    em = hb.HeartbeatEmitter(
        slug=_SLUG,
        key=_KEY,
        ingest_url="https://smd.services/api/internal/heartbeat",
        healthchecks_url="https://hc-ping.com/abc",
        version=None,
        audit_db_path_fn=lambda: None,
        post_fn=boom,
        ping_fn=lambda url: None,
    )
    # Must not raise; the ping leg must still run despite the POST failure.
    em._tick()


def test_tick_swallows_ping_failure():
    def boom(url):
        raise ConnectionError("hc down")

    em = hb.HeartbeatEmitter(
        slug=_SLUG,
        key=_KEY,
        ingest_url="https://smd.services/api/internal/heartbeat",
        healthchecks_url="https://hc-ping.com/abc",
        version=None,
        audit_db_path_fn=lambda: None,
        post_fn=lambda url, headers, body: 200,
        ping_fn=boom,
    )
    em._tick()  # no raise


def test_tick_401_does_not_raise():
    em, _calls = _emitter(post_status=401)
    em._tick()  # logged, not raised


# ---------------------------------------------------------------------------
# start() gating + emitter_from_env
# ---------------------------------------------------------------------------


def test_start_returns_false_without_key_or_healthchecks():
    em = hb.HeartbeatEmitter(
        slug=None,
        key=None,
        ingest_url="https://smd.services/api/internal/heartbeat",
        healthchecks_url=None,
        version=None,
        audit_db_path_fn=lambda: None,
    )
    assert em.start() is False


def test_start_runs_for_healthchecks_only_when_key_missing():
    em = hb.HeartbeatEmitter(
        slug=None,
        key=None,
        ingest_url="https://smd.services/api/internal/heartbeat",
        healthchecks_url="https://hc-ping.com/abc",
        version=None,
        audit_db_path_fn=lambda: None,
        ping_fn=lambda url: None,
    )
    assert em.start() is True
    em.stop()


def test_emitter_from_env_reads_config(monkeypatch):
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "pilot-smokeball")
    monkeypatch.setenv("MACHINE_HEARTBEAT_KEY", "k")
    monkeypatch.setenv("HEALTHCHECKS_PING_URL", "https://hc-ping.com/xyz")
    monkeypatch.setenv("SMD_OVERLAY_REF", "deadbeef")
    monkeypatch.delenv("HEARTBEAT_INGEST_URL", raising=False)
    em = hb.emitter_from_env(lambda: None)
    assert em._slug == "pilot-smokeball"
    assert em._key == "k"
    assert em._healthchecks_url == "https://hc-ping.com/xyz"
    assert em._version == "deadbeef"
    assert em._ingest_url == hb.DEFAULT_INGEST_URL


def test_emitter_from_env_slug_falls_back_to_customer_slug(monkeypatch):
    monkeypatch.delenv("SMD_CUSTOMER_SLUG", raising=False)
    monkeypatch.setenv("CUSTOMER_SLUG", "fallback-slug")
    monkeypatch.setenv("MACHINE_HEARTBEAT_KEY", "k")
    em = hb.emitter_from_env(lambda: None)
    assert em._slug == "fallback-slug"


def test_payload_carries_sticky_stop_level_when_present():
    p = hb.build_payload(
        heartbeat_ts="2026-07-03T00:00:00Z",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        sticky_stop_level="HARD_STOP",
    )
    assert p["sticky_stop_level"] == "HARD_STOP"
    # Absent/None omits the field entirely (receiver treats absence as
    # unknown, never as OK).
    p2 = hb.build_payload(
        heartbeat_ts="2026-07-03T00:00:00Z",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
    )
    assert "sticky_stop_level" not in p2
