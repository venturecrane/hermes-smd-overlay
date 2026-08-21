"""Tests for the Machine → control-plane heartbeat emitter (shared/heartbeat.py).

Covers the pure pieces (payload assembly, audit-timestamp read) and the tick
logic with injected transport, without opening a socket. The emitter's
fail-soft contract — a failing POST or ping never escapes the thread — is the
load-bearing property, so it gets explicit coverage.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from shared import heartbeat as hb
from shared.connector_check import ConnectorCheck
from shared.scheduler_check import SchedulerCheck
from shared.spec_control_check import SpecControlCheck

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


def test_payload_carries_connector_token_age_and_omits_empty():
    # ss#2148: token ages ride a SEPARATE field from the health map, and an
    # empty/absent map is omitted (nothing to report is a hold, never zero).
    p = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        connector_token_age={"smokeball": 86400},
    )
    assert p["connector_token_age"] == {"smokeball": 86400}
    p2 = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        connector_token_age=None,
    )
    assert "connector_token_age" not in p2


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
        # Hermetic default: tests never run the real filesystem check.
        scheduler_check_fn=overrides.get(
            "scheduler_check_fn",
            lambda: SchedulerCheck(ok=True, job_count=0, max_overdue_seconds=None),
        ),
        scheduler_check_debounce=overrides.get("scheduler_check_debounce", 3),
        # Hermetic default for the connector check too (ADR 0080).
        connector_check_fn=overrides.get(
            "connector_check_fn",
            lambda: ConnectorCheck(ok=True, servers={}),
        ),
        connector_check_debounce=overrides.get("connector_check_debounce", 3),
        # Hermetic default for the authored-spec control check (ss-console
        # #2234). Without this the emitter would read the real customer.yaml.
        spec_control_check_fn=overrides.get(
            "spec_control_check_fn",
            lambda: SpecControlCheck(ok=True, entries={}),
        ),
        spec_control_check_debounce=overrides.get("spec_control_check_debounce", 3),
        # Hermetic default for the webhook expected-tools check (#2222).
        # ``None`` is the HOLD state (no usable boot sentinel), so the default
        # keeps both fields off every unrelated payload assertion.
        webhook_surface_check_fn=overrides.get("webhook_surface_check_fn", lambda: None),
        # Hermetic default for the gateway loop check (ss-console#2488 part 2).
        # ``None`` is the HOLD state; without it the emitter would stat the real
        # /opt/data and /run on the developer's machine.
        gateway_loop_check_fn=overrides.get("gateway_loop_check_fn", lambda: None),
        gateway_loop_check_debounce=overrides.get("gateway_loop_check_debounce", 3),
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


# ---------------------------------------------------------------------------
# scheduler self-check wiring (ss work-liveness fix)
# ---------------------------------------------------------------------------


def _last_payload(calls):
    return json.loads(calls["posts"][-1][2])


def test_payload_sends_scheduler_fields_including_falsy_values():
    """ok=False -> 0 and job_count=0 are REAL values that must reach the
    wire; truthiness-omission would silence exactly the states the alerter
    exists to see."""
    p = hb.build_payload(
        heartbeat_ts="2026-07-24T00:00:00Z",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        scheduler_ok=False,
        scheduler_job_count=0,
    )
    assert p["scheduler_ok"] == 0
    assert p["scheduler_job_count"] == 0
    assert "scheduler_max_overdue_seconds" not in p
    p2 = hb.build_payload(
        heartbeat_ts="2026-07-24T00:00:00Z",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
    )
    assert "scheduler_ok" not in p2
    assert "scheduler_job_count" not in p2


def test_tick_carries_scheduler_check_result():
    em, calls = _emitter(
        scheduler_check_fn=lambda: SchedulerCheck(ok=True, job_count=4, max_overdue_seconds=1234)
    )
    em._tick()
    p = _last_payload(calls)
    assert p["scheduler_ok"] == 1
    assert p["scheduler_job_count"] == 4
    assert p["scheduler_max_overdue_seconds"] == 1234


def test_scheduler_check_crash_debounces_then_reports_not_omits():
    """Two failed ticks keep last-known-good; the third reports ok=0 with
    the last-good job count. REPORTED, never omitted — an omitted field on
    a crashed checker recreates 'monitoring green while broken'."""
    state = {"good": True}

    def flappy():
        if not state["good"]:
            raise RuntimeError("checker exploded")
        return SchedulerCheck(ok=True, job_count=7, max_overdue_seconds=None)

    em, calls = _emitter(scheduler_check_fn=flappy, scheduler_check_debounce=3)
    em._tick()  # good tick establishes last-known-good
    state["good"] = False
    em._tick()  # failure 1 -> last-good carried
    em._tick()  # failure 2 -> last-good carried
    assert _last_payload(calls)["scheduler_ok"] == 1
    em._tick()  # failure 3 -> debounce reached, report ok=0
    p = _last_payload(calls)
    assert p["scheduler_ok"] == 0
    assert p["scheduler_job_count"] == 7  # last-good count carried
    assert "scheduler_max_overdue_seconds" not in p


def test_scheduler_check_recovery_resets_debounce():
    state = {"good": False}

    def flappy():
        if not state["good"]:
            raise RuntimeError("checker exploded")
        return SchedulerCheck(ok=True, job_count=2, max_overdue_seconds=None)

    em, calls = _emitter(scheduler_check_fn=flappy, scheduler_check_debounce=3)
    em._tick()  # failure 1 (no prior good -> fields omitted entirely)
    assert "scheduler_ok" not in _last_payload(calls)
    state["good"] = True
    em._tick()  # recovery
    assert _last_payload(calls)["scheduler_ok"] == 1
    state["good"] = False
    em._tick()  # failure 1 again (counter was reset)
    assert _last_payload(calls)["scheduler_ok"] == 1  # last-good, not ok=0


def test_scheduler_check_crash_never_escapes_the_tick():
    def boom():
        raise RuntimeError("checker exploded")

    em, calls = _emitter(scheduler_check_fn=boom, scheduler_check_debounce=1)
    em._tick()  # must not raise; POST still happens
    assert len(calls["posts"]) == 1
    assert _last_payload(calls)["scheduler_ok"] == 0


# ---------------------------------------------------------------------------
# connector check wiring (ADR 0080)
# ---------------------------------------------------------------------------


def test_payload_sends_connector_fields_including_empty_map():
    p = hb.build_payload(
        heartbeat_ts="2026-07-25T00:00:00+00:00",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        connector_check_ok=True,
        connectors={},
    )
    # An empty map is a REAL "check ran, nothing observed yet" state — it
    # must reach the wire (truthiness-omitting it would be a silent hold).
    assert p["connector_check_ok"] == 1
    assert p["connectors"] == {}


def test_payload_omits_connector_fields_when_absent():
    p = hb.build_payload(
        heartbeat_ts="2026-07-25T00:00:00+00:00",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
    )
    assert "connector_check_ok" not in p
    assert "connectors" not in p


def test_payload_sends_spec_control_fields_including_empty_map():
    """The empty map is the state that RESOLVES an open alert — every declared
    spec is installed. Truthiness-omitting it would leave a repaired control
    paging forever (ss-console #2234)."""
    p = hb.build_payload(
        heartbeat_ts="2026-08-10T00:00:00+00:00",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        spec_control_ok=True,
        spec_control={},
    )
    assert p["spec_control_ok"] == 1
    assert p["spec_control"] == {}


def test_payload_omits_spec_control_fields_when_absent():
    p = hb.build_payload(
        heartbeat_ts="2026-08-10T00:00:00+00:00",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
    )
    assert "spec_control_ok" not in p
    assert "spec_control" not in p


def test_tick_carries_spec_control_result():
    entry = {"declared": True, "installed": False}
    em, calls = _emitter(
        spec_control_check_fn=lambda: SpecControlCheck(ok=True, entries={"staff.voice": entry})
    )
    em._tick()
    p = _last_payload(calls)
    assert p["spec_control_ok"] == 1
    assert p["spec_control"] == {"staff.voice": entry}


def test_spec_control_check_crash_debounces_then_reports_not_omits():
    """A check that cannot run must eventually SAY so. Omitting forever would
    make a broken alarm indistinguishable from a healthy seat — which is the
    failure shape this whole change exists to remove."""

    def boom():
        raise RuntimeError("spec control checker exploded")

    em, calls = _emitter(spec_control_check_fn=boom, spec_control_check_debounce=2)
    em._tick()  # failure 1: no prior good -> fields omitted (console holds)
    assert "spec_control_ok" not in _last_payload(calls)
    em._tick()  # failure 2: debounce reached -> reported as ok=0
    p = _last_payload(calls)
    assert p["spec_control_ok"] == 0
    assert "spec_control" not in p  # never emit a map you cannot trust


def test_tick_carries_connector_check_result():
    entry = {"consecutive_failures": 4, "run_age_seconds": 400, "conn_evidence": True}
    em, calls = _emitter(
        connector_check_fn=lambda: ConnectorCheck(ok=True, servers={"smokeball": entry})
    )
    em._tick()
    p = _last_payload(calls)
    assert p["connector_check_ok"] == 1
    assert p["connectors"] == {"smokeball": entry}


def test_connector_check_crash_debounces_then_reports_not_omits():
    def boom():
        raise RuntimeError("connector checker exploded")

    em, calls = _emitter(connector_check_fn=boom, connector_check_debounce=3)
    em._tick()  # failure 1: no prior good -> fields omitted (console holds)
    assert "connector_check_ok" not in _last_payload(calls)
    em._tick()  # failure 2: still held
    assert "connector_check_ok" not in _last_payload(calls)
    em._tick()  # failure 3: REPORTED as broken, map withheld
    p = _last_payload(calls)
    assert p["connector_check_ok"] == 0
    assert "connectors" not in p


def test_connector_check_recovery_resets_debounce():
    state = {"good": False}

    def flappy():
        if not state["good"]:
            raise RuntimeError("connector checker exploded")
        return ConnectorCheck(ok=True, servers={"agentmail": {"consecutive_failures": 0}})

    em, calls = _emitter(connector_check_fn=flappy, connector_check_debounce=3)
    em._tick()  # failure 1
    assert "connector_check_ok" not in _last_payload(calls)
    state["good"] = True
    em._tick()  # recovery
    assert _last_payload(calls)["connector_check_ok"] == 1
    state["good"] = False
    em._tick()  # failure 1 again (counter reset) -> last-good reported
    p = _last_payload(calls)
    assert p["connector_check_ok"] == 1
    assert p["connectors"] == {"agentmail": {"consecutive_failures": 0}}


def test_connector_check_crash_never_escapes_the_tick():
    def boom():
        raise RuntimeError("connector checker exploded")

    em, calls = _emitter(connector_check_fn=boom, connector_check_debounce=1)
    em._tick()  # must not raise; POST still happens
    assert len(calls["posts"]) == 1
    assert _last_payload(calls)["connector_check_ok"] == 0


# --------------------------------------------------------------------------- #
# Webhook expected-tools surface (ss-console #2222). The heartbeat is where the
# WARN tier becomes visible: its absence is deliberately not boot-fatal, so if
# it did not reach a field here it would not be reported anywhere at all.
# --------------------------------------------------------------------------- #


def test_payload_sends_webhook_surface_fields_including_empty_map():
    """An empty map is a REAL "checked, every expected tool is offered" state,
    and it is what RESOLVES an open alert. Truthiness-omitting it would leave a
    repaired surface paging forever — the same is-not-None discipline the
    scheduler / connector / spec_control fields already carry."""
    p = hb.build_payload(
        heartbeat_ts="2026-08-11T00:00:00+00:00",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        webhook_surface_ok=True,
        webhook_surface={},
    )
    assert p["webhook_surface_ok"] == 1
    assert p["webhook_surface"] == {}


def test_payload_omits_webhook_surface_fields_when_absent():
    """Absence is a HOLD: a seat serving no webhook platform, or a boot whose
    sentinel is stale, must not overwrite what the console last knew."""
    p = hb.build_payload(
        heartbeat_ts="2026-08-11T00:00:00+00:00",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
    )
    assert "webhook_surface_ok" not in p
    assert "webhook_surface" not in p


def test_tick_carries_the_webhook_surface_result():
    from shared.webhook_surface_check import WebhookSurfaceCheck

    entry = {"expected": True, "offered": False}
    em, calls = _emitter(
        webhook_surface_check_fn=lambda: WebhookSurfaceCheck(
            ok=True, tools={"operator_seat_facts": entry}
        )
    )
    em._tick()
    p = _last_payload(calls)
    assert p["webhook_surface_ok"] == 1
    assert p["webhook_surface"] == {"operator_seat_facts": entry}


def test_a_held_webhook_surface_omits_both_fields_from_the_tick():
    em, calls = _emitter(webhook_surface_check_fn=lambda: None)
    em._tick()
    p = _last_payload(calls)
    assert "webhook_surface_ok" not in p
    assert "webhook_surface" not in p


def test_webhook_surface_check_crash_reports_rather_than_going_dark():
    """No debounce here, unlike the three live-subsystem checks: this reads one
    small local file written once per boot, which has no transient-failure mode a
    debounce would smooth. A crash reports ok=0 on the FIRST tick — and never
    escapes it."""

    def boom():
        raise RuntimeError("surface checker exploded")

    em, calls = _emitter(webhook_surface_check_fn=boom)
    em._tick()  # must not raise; POST still happens
    assert len(calls["posts"]) == 1
    p = _last_payload(calls)
    assert p["webhook_surface_ok"] == 0
    assert "webhook_surface" not in p, "a broken check must never emit a map it cannot trust"


# ---------------------------------------------------------------------------
# cron_containment field (ss-console#2276)
# ---------------------------------------------------------------------------


def test_payload_sends_cron_containment_including_false():
    base = dict(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
    )
    assert hb.build_payload(**base, cron_containment=True)["cron_containment"] == 1
    # False is a REAL "not contained" value and must reach the wire as 0
    assert hb.build_payload(**base, cron_containment=False)["cron_containment"] == 0
    # None (check failed) omits — the console holds rather than resolves
    assert "cron_containment" not in hb.build_payload(**base)


def test_read_cron_containment_reflects_sentinel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hb._read_cron_containment() is False
    (tmp_path / "CRON_CONTAINMENT").write_text("ss#2258 containment\n")
    assert hb._read_cron_containment() is True


def _payload_with_containment():
    return hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        cron_containment=hb._read_cron_containment(),
    )


def test_uncontained_seat_still_reports_zero(tmp_path, monkeypatch):
    """The negative must stay a real negative: a readable volume with no
    sentinel is genuinely not contained and reaches the wire as 0, not as
    'unknown' (ss-console#2291 must not trade one blind spot for another)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert _payload_with_containment()["cron_containment"] == 0


def test_read_cron_containment_omits_when_sentinel_unreadable(tmp_path, monkeypatch):
    """ss-console#2291: a volume we cannot read must report UNKNOWN (field
    omitted), never a false 'not contained'. Pre-fix this returned False and
    the wire carried cron_containment: 0 — a contained seat indistinguishable
    from a normal one, which is exactly what ss-console#2276 exists to prevent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def _denied(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_file", _denied)

    assert hb._read_cron_containment() is None
    assert "cron_containment" not in _payload_with_containment()


def test_read_cron_containment_omits_when_volume_absent(tmp_path, monkeypatch):
    """Same rule for the other half of the failure the wrapper named: if the
    volume that would hold the sentinel is not mounted, containment state is
    unknowable, not false."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "not-mounted"))

    assert hb._read_cron_containment() is None
    assert "cron_containment" not in _payload_with_containment()


# ---------------------------------------------------------------------------
# Gateway loop liveness (ss-console#2488 part 2)
# ---------------------------------------------------------------------------


def _loop(**kw):
    from shared.gateway_loop_check import GatewayLoopCheck

    base = dict(ok=True, age_seconds=None, supervisor_state=None, restarts_last_hour=None)
    base.update(kw)
    return GatewayLoopCheck(**base)


def test_payload_sends_gateway_loop_fields_including_zero():
    """age=0 and restarts=0 are REAL values and must reach the wire: a beat
    that just landed and a supervisor that has never had to kill are both facts
    the console resolves alerts on. Truthiness-omitting them would strand an
    open wedge alert forever."""
    p = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        gateway_loop_ok=True,
        gateway_loop_age_seconds=0,
        gateway_supervisor_state="armed",
        gateway_restarts_last_hour=0,
    )
    assert p["gateway_loop_ok"] == 1
    assert p["gateway_loop_age_seconds"] == 0
    assert p["gateway_supervisor_state"] == "armed"
    assert p["gateway_restarts_last_hour"] == 0


def test_payload_omits_gateway_loop_fields_when_none():
    p = hb.build_payload(
        heartbeat_ts="t", last_audit_ts=None, last_skill_ts=None, uptime_seconds=None, version=None
    )
    for k in (
        "gateway_loop_ok",
        "gateway_loop_age_seconds",
        "gateway_supervisor_state",
        "gateway_restarts_last_hour",
    ):
        assert k not in p


def test_payload_sends_gateway_loop_ok_false_as_zero():
    p = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        gateway_loop_ok=False,
    )
    assert p["gateway_loop_ok"] == 0
    assert "gateway_loop_age_seconds" not in p


def test_tick_carries_gateway_loop_check_result():
    import json

    em, calls = _emitter(
        gateway_loop_check_fn=lambda: _loop(
            age_seconds=400, supervisor_state="armed", restarts_last_hour=1
        )
    )
    em._tick()
    body = json.loads(calls["posts"][0][2])
    assert body["gateway_loop_ok"] == 1
    assert body["gateway_loop_age_seconds"] == 400
    assert body["gateway_supervisor_state"] == "armed"
    assert body["gateway_restarts_last_hour"] == 1


def test_gateway_loop_check_crash_debounces_then_reports_not_omits():
    """Report-late beats report-never: after the debounce a crashed checker
    ships ok=0, which pages gateway_loop_unprovable, instead of the field
    vanishing -- which would hold an open wedge alert open and read as the
    'monitoring green while broken' class this whole feature exists to close."""
    import json

    def boom():
        raise RuntimeError("stat exploded")

    em, calls = _emitter(gateway_loop_check_fn=boom, gateway_loop_check_debounce=2)
    em._tick()
    b1 = json.loads(calls["posts"][0][2])
    assert "gateway_loop_ok" not in b1  # first crash: hold (no last-good yet)
    em._tick()
    b2 = json.loads(calls["posts"][1][2])
    assert b2["gateway_loop_ok"] == 0
    assert "gateway_loop_age_seconds" not in b2
