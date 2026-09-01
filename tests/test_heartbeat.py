"""Tests for the Machine → control-plane heartbeat emitter (shared/heartbeat.py).

Covers the pure pieces (payload assembly, audit-timestamp read) and the tick
logic with injected transport, without opening a socket. The emitter's
fail-soft contract — a failing POST or ping never escapes the thread — is the
load-bearing property, so it gets explicit coverage.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
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
        audit_db_path_fn=overrides.get("audit_db_path_fn", lambda: None),
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


def test_payload_carries_the_stop_cause_beside_the_level():
    """A level alone cannot tell an operator which of the four meters tripped,
    and the four need four different investigations."""
    p = hb.build_payload(
        heartbeat_ts="2026-07-03T00:00:00Z",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        sticky_stop_level="HARD_STOP",
        sticky_stop_reason="consecutive_tool_failures=8 (window=600s, skill=mcp_x)",
        sticky_stop_condition="consecutive_tool_failures",
    )
    assert p["sticky_stop_level"] == "HARD_STOP"
    assert p["sticky_stop_condition"] == "consecutive_tool_failures"
    assert "skill=mcp_x" in p["sticky_stop_reason"]


def test_payload_never_sends_a_cause_without_its_level():
    """A cause beside an absent level would let the console render "why"
    against whatever level it already held — a stale pairing. The seat sends
    both or neither."""
    p = hb.build_payload(
        heartbeat_ts="2026-07-03T00:00:00Z",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        sticky_stop_reason="orphaned reason",
        sticky_stop_condition="cost_threshold",
    )
    assert "sticky_stop_reason" not in p
    assert "sticky_stop_condition" not in p
    # And a level with no recorded cause still ships the level.
    p2 = hb.build_payload(
        heartbeat_ts="2026-07-03T00:00:00Z",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        sticky_stop_level="OK",
    )
    assert p2["sticky_stop_level"] == "OK"
    assert "sticky_stop_reason" not in p2


def test_tick_carries_the_stop_cause_onto_the_wire(monkeypatch):
    """The seam that build_payload's own tests cannot see: the emitter must
    actually PASS what it read. Reading the cause and then dropping it before
    the call is silent, and it is the failure mode this whole change exists to
    close — so it gets a test at the wire, not at the helper."""
    import json

    import shared.cost_breaker as cb

    monkeypatch.setattr(
        cb,
        "read_stop_state",
        lambda *a, **k: cb.StopStateView(
            level="HARD_STOP",
            reason="consecutive_tool_failures=8 (window=600s, skill=mcp_smokeball_list_matters)",
            condition="consecutive_tool_failures",
        ),
    )
    em, calls = _emitter()
    em._tick()
    _url, _headers, body = calls["posts"][0]
    sent = json.loads(body)
    assert sent["sticky_stop_level"] == "HARD_STOP"
    assert sent["sticky_stop_condition"] == "consecutive_tool_failures"
    assert "skill=mcp_smokeball_list_matters" in sent["sticky_stop_reason"]


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


# ---------------------------------------------------------------------------
# Audit-ledger facts on the wire (ss-console #2498 + #2500)
#
# Three fields, one purpose: make a quiet ledger distinguishable from a broken
# one, off the Machine, without asking SMD.
# ---------------------------------------------------------------------------

_CHAINED_AUDIT_DDL = (
    "CREATE TABLE audit_log ("
    "  id TEXT PRIMARY KEY, ts TEXT NOT NULL, action_type TEXT NOT NULL,"
    "  skill_name TEXT, prev_hash TEXT, row_hash TEXT)"
)


def _make_chained_audit_db(path: str, rows: list[tuple[str, str, str | None]]) -> None:
    """rows = [(id, ts, row_hash), ...]; row_hash None = a pre-#1686 legacy row."""
    conn = sqlite3.connect(path)
    conn.execute(_CHAINED_AUDIT_DDL)
    conn.executemany(
        "INSERT INTO audit_log (id, ts, action_type, row_hash) VALUES (?, ?, 'x', ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_audit_facts_returns_head_and_count(tmp_path):
    db = tmp_path / "audit.db"
    _make_chained_audit_db(
        str(db),
        [
            ("01A", "2026-08-01T10:00:00+00:00", "a" * 64),
            ("01B", "2026-08-01T11:00:00+00:00", "b" * 64),
        ],
    )
    facts = hb.read_audit_facts(str(db))
    assert facts.head == "b" * 64
    assert facts.rows == 2
    assert facts.last_audit_ts == "2026-08-01T11:00:00+00:00"


def test_audit_facts_head_skips_unchained_legacy_rows(tmp_path):
    """Rows written before #1686 carry NULL row_hash and are not part of the
    chain. Pinning a NULL head would tell the console the chain vanished."""
    db = tmp_path / "audit.db"
    _make_chained_audit_db(
        str(db),
        [
            ("01A", "2026-08-01T10:00:00+00:00", "a" * 64),
            ("01B", "2026-08-01T11:00:00+00:00", None),
        ],
    )
    facts = hb.read_audit_facts(str(db))
    assert facts.head == "a" * 64
    assert facts.rows == 2  # the count is of ALL rows, chained or not


def test_audit_facts_on_a_pre_chain_ledger_still_reports_timestamps(tmp_path):
    """A ledger with no row_hash COLUMN at all. The chain read must degrade on
    its own — sharing a handler with the timestamp read would let a missing
    column report a working seat as silent, which is the #2498 confusion in
    reverse."""
    db = tmp_path / "audit.db"
    _make_audit_db(str(db), [("01A", "2026-08-01T10:00:00+00:00", "matter_memo")])
    facts = hb.read_audit_facts(str(db))
    assert facts.last_audit_ts == "2026-08-01T10:00:00+00:00"
    assert facts.head is None
    assert facts.rows == 1


def test_audit_facts_empty_ledger_counts_zero(tmp_path):
    db = tmp_path / "audit.db"
    _make_chained_audit_db(str(db), [])
    facts = hb.read_audit_facts(str(db))
    assert facts.rows == 0
    assert facts.head is None


def test_audit_facts_missing_file_answers_nothing(tmp_path):
    # Compared against a CONSTRUCTED empty facts object rather than a literal
    # tuple: every field defaults to None, so this keeps saying "the seat has no
    # opinion about anything" as fields are added, instead of counting them.
    assert hb.read_audit_facts("/no/such/audit.db") == hb.AuditLedgerFacts(None, None, None, None)


def test_payload_carries_a_zero_write_failure_count():
    """0 is the value that says 'the writer is up and has lost nothing'. It is
    the ONLY thing that distinguishes a quiet ledger from a broken one, so
    truthiness-omitting it would send exactly the healthy case as silence."""
    p = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        audit_write_failures=0,
    )
    assert p["audit_write_failures"] == 0


def test_payload_omits_write_failures_when_the_seat_cannot_answer():
    p = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        audit_write_failures=None,
    )
    assert "audit_write_failures" not in p


def test_payload_carries_head_and_rows():
    p = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        audit_head="c" * 64,
        audit_rows=0,
    )
    assert p["audit_head"] == "c" * 64
    assert p["audit_rows"] == 0


def test_payload_omits_head_on_an_unchained_ledger():
    p = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        audit_head=None,
        audit_rows=None,
    )
    assert "audit_head" not in p
    assert "audit_rows" not in p


# ---------------------------------------------------------------------------
# Send refusals on the wire (ss-console#2547)
#
# The 2026-08-19 shape: five refusals in 26 seconds, every one an audit row
# nobody was watching. The 08-20 shape: five needs-you items and no attempt at
# all. From outside the seat "refused", "did not try" and "nothing to report"
# were the same picture; these fields are what tells them apart.
# ---------------------------------------------------------------------------

_LEDGER_DDL = (
    "CREATE TABLE audit_log ("
    "  id TEXT PRIMARY KEY, ts TEXT NOT NULL, action_type TEXT NOT NULL,"
    "  actor TEXT, actor_role TEXT, skill_name TEXT, matter_ref TEXT,"
    "  metadata TEXT, prev_hash TEXT, row_hash TEXT)"
)

_NOW = datetime(2026, 8, 21, 18, 0, 0, tzinfo=timezone.utc)

_CRON_SESSION = "cron_a726fd5efd24_20260820_070000"


def _ts(hours_ago: float) -> str:
    """An audit ``ts`` in the ledger's real spelling (millisecond + ``Z``)."""
    at = _NOW - timedelta(hours=hours_ago)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


class _Ledger:
    """A ledger builder that speaks in the row shapes the seat actually writes."""

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(_LEDGER_DDL)
        self._n = 0

    def add(self, ts, action_type, metadata, skill_name=None):
        self._n += 1
        self.conn.execute(
            "INSERT INTO audit_log (id, ts, action_type, actor, actor_role,"
            " skill_name, metadata) VALUES (?, ?, ?, 'operator', 'agent', ?, ?)",
            (f"row{self._n:04d}", ts, action_type, skill_name, json.dumps(metadata)),
        )
        self.conn.commit()

    def refused_tool_call(self, ts, *, skill="deadline-miss-escalator", error_type="Refused: date"):
        self.add(
            ts,
            "TOOL_CALL_COMPLETED",
            {
                "tool": "smd_send_message",
                "outcome": "error",
                "error_type": error_type,
                "resolved_action_class": "external_send_internal",
                "cron_job_id": "a726fd5efd24",
                "session_id": _CRON_SESSION,
                "routine": f"op-managed:operator:{skill}",
            },
            skill_name=skill,
        )

    def ok_tool_call(self, ts):
        self.add(
            ts,
            "TOOL_CALL_COMPLETED",
            {
                "tool": "smd_send_message",
                "outcome": "ok",
                "resolved_action_class": "external_send_internal",
                "cron_job_id": "a726fd5efd24",
            },
        )

    def confirm_send_failed(self, ts, *, outcome="refused", reason="recipient x@y.example"):
        self.add(
            ts,
            "CONFIRM_SEND_FAILED",
            {
                "customer": "pilot-smokeball",
                "verb": "msgraph_send",
                "outcome": outcome,
                "reason": reason,
                "recipients": ["scott@smd.services"],
            },
        )

    def wake(self, ts, *, needs_you, skill="deadline-miss-escalator"):
        # NO session_id, matching the live rows: ``pre_run.py`` writes this
        # before the turn exists, and 0 of 17 pilot rows carry one. The routine
        # is on the ``skill_name`` COLUMN, which is what the span joins on.
        self.add(
            ts,
            "EMITTED_WAKE",
            {"decision_basis": "deadline", "digest_needs_you": needs_you},
            skill_name=skill,
        )

    def turn(self, ts, skill="deadline-miss-escalator"):
        self.add(ts, "LLM_TURN_COMPLETED", {"session_id": _CRON_SESSION}, skill_name=skill)

    def dispatched(self, ts, recipients):
        self.add(
            ts,
            "CONFIRM_SEND_DISPATCHED",
            {"outcome": "sent", "verb": "msgraph_send", "recipients": recipients},
        )


_facts_seq = 0


def _facts(tmp_path, build):
    # A fresh DB per call: a test that builds two ledgers is comparing two
    # worlds, and reusing one file would silently union them.
    global _facts_seq
    _facts_seq += 1
    db = tmp_path / f"audit-{_facts_seq}.db"
    ledger = _Ledger(db)
    build(ledger)
    ledger.conn.close()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return hb.count_send_refusals(conn, _NOW)
    finally:
        conn.close()


def test_send_refusals_zero_reaches_the_wire(tmp_path):
    """A ledger with nothing to report answers 0, and 0 is EMITTED.

    This is the field's whole reason for existing: a seat whose routines all
    reached their humans must be distinguishable from a seat nobody is reading.
    Truthiness-omitting the zero would send the healthy case as silence to a
    console that is watching for silence.
    """
    facts = _facts(tmp_path, lambda led: led.ok_tool_call(_ts(1)))
    assert facts.count == 0
    assert facts.last_ts is None
    assert facts.events == []
    payload = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        send_refusals=facts.count,
        send_refusals_json=facts.events,
    )
    assert payload["send_refusals"] == 0
    assert payload["send_refusals_json"] == []
    assert "send_refusals_last_ts" not in payload


def test_send_refusals_counts_a_cron_send_refusal(tmp_path):
    facts = _facts(tmp_path, lambda led: led.refused_tool_call(_ts(2)))
    assert facts.count == 1
    assert facts.events[0]["kind"] == "refused"
    assert facts.events[0]["routine"] == "op-managed:operator:deadline-miss-escalator"
    assert facts.events[0]["tool"] == "smd_send_message"
    assert facts.events[0]["reason"] == "Refused: date"


def test_send_refusals_excludes_rows_older_than_the_window(tmp_path):
    def build(led):
        led.refused_tool_call(_ts(25))
        led.refused_tool_call(_ts(23))

    assert _facts(tmp_path, build).count == 1


def test_send_refusals_excludes_rows_after_the_window(tmp_path):
    """The window is closed at BOTH ends.

    For the ticker the upper bound is "now" and never bites. For the retro
    falsifier — the same function, asked about a day in the past — an open window
    counts events that had not happened yet: a dry run reported 08-19's five
    refusals against 08-18, whose known answer is zero.
    """

    def build(led):
        led.refused_tool_call(_ts(1))
        led.refused_tool_call(_ts(-3))  # three hours after ``now``

    assert _facts(tmp_path, build).count == 1


def test_send_refusals_ignores_a_wake_after_the_window(tmp_path):
    def build(led):
        led.wake(_ts(-3), needs_you=4)

    assert _facts(tmp_path, build).count == 0


def test_send_refusals_ignores_an_interactive_refusal(tmp_path):
    """A refusal on a session a PERSON is driving is not a page.

    They can see the refusal in front of them. The pager exists for refusals
    nobody is looking at, which is what the cron-shaped-session clause selects.
    """

    def build(led):
        led.add(
            _ts(1),
            "TOOL_CALL_COMPLETED",
            {
                "tool": "smd_send_message",
                "outcome": "error",
                "error_type": "Refused: date",
                "resolved_action_class": "external_send_internal",
                "session_id": "telegram-3391",
            },
        )

    assert _facts(tmp_path, build).count == 0


def test_send_refusals_ignores_a_failed_read(tmp_path):
    """An errored READ is a connector problem, not a message nobody got."""

    def build(led):
        led.add(
            _ts(1),
            "TOOL_CALL_COMPLETED",
            {
                "tool": "mcp_smokeball_list_matters",
                "outcome": "error",
                "error_type": "HTTPError",
                "resolved_action_class": "read",
                "cron_job_id": "a726fd5efd24",
            },
        )

    assert _facts(tmp_path, build).count == 0


def test_send_refusals_counts_confirm_send_failed(tmp_path):
    """The broker's own refusal counts, and its REASON is the closed vocabulary.

    ``CONFIRM_SEND_FAILED.reason`` is ``str(exc)`` and can quote the address the
    broker refused; ``outcome`` says the same thing about kind and carries no
    address off the seat.
    """
    facts = _facts(
        tmp_path,
        lambda led: led.confirm_send_failed(_ts(3), reason="not on roster: jane@gmail.example"),
    )
    assert facts.count == 1
    assert facts.events[0]["reason"] == "refused"
    assert facts.events[0]["tool"] == "msgraph_send"
    assert "gmail.example" not in json.dumps(facts.events)


def test_send_refusals_counts_a_wake_that_sent_nothing(tmp_path):
    """The 2026-08-20 instance: five items waiting, no attempt, no refusal row.

    Nothing refusal-shaped can see this, which is why the fact carries two kinds
    rather than being a refusal counter.
    """

    def build(led):
        led.wake(_ts(4), needs_you=5)
        led.turn(_ts(3.9))

    facts = _facts(tmp_path, build)
    assert facts.count == 1
    assert facts.events[0]["kind"] == "unsent"
    assert facts.events[0]["needs_you"] == 5
    assert facts.events[0]["routine"] == "deadline-miss-escalator"


def test_send_refusals_ignores_a_wake_with_nothing_to_say(tmp_path):
    def build(led):
        led.wake(_ts(4), needs_you=0)
        led.turn(_ts(3.9))

    assert _facts(tmp_path, build).count == 0


def test_send_refusals_clears_a_wake_that_reached_a_person(tmp_path):
    def build(led):
        led.wake(_ts(4), needs_you=3)
        led.dispatched(_ts(3.95), ["scott@smd.services"])
        led.turn(_ts(3.9))

    assert _facts(tmp_path, build).count == 0


def test_send_refusals_a_probe_only_dispatch_does_not_clear_a_wake(tmp_path):
    """A send to the falsifier's own probe address is not a routine reaching a
    human. If it cleared the fact, the instrument used to prove the pager works
    would be the thing that silences it."""

    def build(led):
        led.wake(_ts(4), needs_you=3)
        led.dispatched(_ts(3.95), ["ss-probe-7f2@agentmail.to"])
        led.turn(_ts(3.9))

    facts = _facts(tmp_path, build)
    assert facts.count == 1
    assert facts.events[0]["kind"] == "unsent"


def test_send_refusals_span_ends_at_this_routines_own_turn(tmp_path):
    """The span is joined on SKILL and time — neither row carries a session id.

    ``EMITTED_WAKE`` is written by the pre_run child before the turn exists (0 of
    17 pilot rows carry a session id, read live 2026-08-22), and the broker
    writes ``CONFIRM_SEND_DISPATCHED`` without one. So another routine's turn row
    must not close this routine's span, or a busy seat would clear every silence
    with whatever happened to run next.
    """

    def build(led):
        led.wake(_ts(4), needs_you=2)
        led.turn(_ts(3.99), skill="medical-records-chaser")  # someone else's turn
        led.dispatched(_ts(3.95), ["scott@smd.services"])  # after it, before ours
        led.turn(_ts(3.9))

    # The dispatch is inside OUR span (wake .. our turn), so the wake is cleared.
    assert _facts(tmp_path, build).count == 0


def test_send_refusals_a_span_stops_at_the_cap(tmp_path):
    """A turn row four hours after the wake belongs to the next run. Borrowing
    its dispatch would clear a silence it had nothing to do with."""

    def build(led):
        led.wake(_ts(6), needs_you=2)
        led.dispatched(_ts(4.0), ["scott@smd.services"])  # two hours later
        led.turn(_ts(2.0))  # the next run's turn

    assert _facts(tmp_path, build).count == 1


def test_send_refusals_a_wake_with_no_turn_row_uses_the_fixed_span(tmp_path):
    """The routine crashed, or the ledger has a gap. The span is then a short
    fixed window from the wake: a dispatch inside it clears the fact, one an hour
    later belongs to somebody else."""

    def build(led):
        led.wake(_ts(4), needs_you=2)
        led.dispatched(_ts(3.9), ["scott@smd.services"])

    assert _facts(tmp_path, build).count == 0

    def build_silent(led):
        led.wake(_ts(4), needs_you=2)
        led.dispatched(_ts(3.0), ["scott@smd.services"])

    assert _facts(tmp_path, build_silent).count == 1


def test_send_refusals_holds_a_wake_whose_turn_may_still_be_running(tmp_path):
    """A wake five minutes ago has not yet failed to send anything."""
    assert _facts(tmp_path, lambda led: led.wake(_ts(0.08), needs_you=2)).count == 0


def test_send_refusals_last_ts_is_the_max_across_both_kinds(tmp_path):
    def build(led):
        led.refused_tool_call(_ts(6))
        led.wake(_ts(4), needs_you=1)
        led.turn(_ts(3.9))
        led.confirm_send_failed(_ts(2))

    facts = _facts(tmp_path, build)
    assert facts.count == 3
    assert (facts.refused, facts.unsent) == (2, 1)
    assert facts.last_ts == _ts(2)
    assert facts.events[0]["kind"] == "refused"


def test_send_refusals_json_is_capped_at_the_newest_five(tmp_path):
    def build(led):
        for hours in (9, 8, 7, 6, 5, 4, 3):
            led.refused_tool_call(_ts(hours))

    facts = _facts(tmp_path, build)
    assert facts.count == 7
    assert [e["ts"] for e in facts.events] == [_ts(3), _ts(4), _ts(5), _ts(6), _ts(7)]


def test_send_refusals_reason_is_bounded(tmp_path):
    facts = _facts(
        tmp_path, lambda led: led.refused_tool_call(_ts(1), error_type="Refused: " + "x" * 900)
    )
    assert len(facts.events[0]["reason"]) == 200


def test_read_audit_facts_carries_the_refusal_fields(tmp_path):
    """The ticker's own read, not only the pure query."""
    db = tmp_path / "audit.db"
    ledger = _Ledger(db)
    ledger.refused_tool_call(datetime.now(timezone.utc).isoformat())
    ledger.conn.close()
    facts = hb.read_audit_facts(str(db))
    assert facts.send_refusals == 1
    assert facts.send_refusals_last_ts is not None
    assert facts.send_refusals_json[0]["kind"] == "refused"


def test_read_audit_facts_on_a_pre_metadata_ledger_still_reports_timestamps(tmp_path):
    """A ledger with no ``metadata`` column at all. The refusal read must degrade
    on its own — sharing a handler with the timestamp read would let a missing
    column report a working seat as silent, the #2498 confusion again."""
    db = tmp_path / "audit.db"
    _make_audit_db(str(db), [("01A", "2026-08-01T10:00:00+00:00", "escalator")])
    facts = hb.read_audit_facts(str(db))
    assert facts.last_audit_ts == "2026-08-01T10:00:00+00:00"
    assert facts.send_refusals is None  # cannot answer — the console HOLDS
    assert facts.send_refusals_json is None


def test_payload_omits_refusals_when_the_seat_cannot_answer():
    p = hb.build_payload(
        heartbeat_ts="t",
        last_audit_ts=None,
        last_skill_ts=None,
        uptime_seconds=None,
        version=None,
        send_refusals=None,
        send_refusals_last_ts=None,
        send_refusals_json=None,
    )
    assert "send_refusals" not in p
    assert "send_refusals_last_ts" not in p
    assert "send_refusals_json" not in p


def test_ticker_puts_the_refusal_fields_on_the_wire(tmp_path):
    """End to end through the emitter: the fields reach the POST body."""
    db = tmp_path / "audit.db"
    ledger = _Ledger(db)
    ledger.refused_tool_call(datetime.now(timezone.utc).isoformat())
    ledger.conn.close()
    em, calls = _emitter(audit_db_path_fn=lambda: str(db))
    em._tick()
    body = json.loads(calls["posts"][0][2])
    assert body["send_refusals"] == 1
    assert body["send_refusals_last_ts"]
    assert body["send_refusals_json"][0]["tool"] == "smd_send_message"


# ---- the degraded kind (2026-08-24, the withheld digest) -------------------
#
# CROSS-REPO LITERAL PIN: the ``digest_degraded`` prefix below is written by
# ``ss-console:operator/skills/deadline-miss-escalator/pre_run.py`` (bases
# ``digest_degraded_suppressed`` and ``digest_degraded_audit_unavailable``) and
# read by ``shared/heartbeat.py:_degraded_events``. Neither repo's CI can see
# the other; these tests are the overlay-side half of the pin, and
# ``ss-console``'s escalator tests are the writer-side half.


def _suppressed(led, ts, basis, reason=None, skill="deadline-miss-escalator"):
    metadata = {"decision_basis": basis, "platform": "cron-pre-run"}
    if reason is not None:
        metadata["degraded_reason"] = reason
    led.add(ts, "SUPPRESSED_WAKE", metadata, skill_name=skill)


def test_a_degraded_suppression_counts_and_moves_the_marker(tmp_path):
    """A routine that withheld its own unfit output must page, not vanish. The
    suppression was deliberate; the silence it creates must not be."""
    facts = _facts(
        tmp_path,
        lambda led: _suppressed(
            led,
            _ts(3),
            "digest_degraded_suppressed",
            reason="12 deadlines withheld, nearest 2 days out, 12 lookups failed",
        ),
    )
    assert facts.count == 1
    assert facts.degraded == 1
    assert facts.refused == 0 and facts.unsent == 0
    assert facts.last_ts == _ts(3)
    (event,) = facts.events
    assert event["kind"] == "degraded"
    assert "12 deadlines withheld" in event["reason"]


def test_the_stripped_wake_basis_also_pages(tmp_path):
    """``digest_degraded_audit_unavailable`` — the suppress row could not be
    written so the turn woke stripped — is the SAME failure to reach a human,
    and the prefix match is what keeps a new sibling basis paging by default."""
    facts = _facts(
        tmp_path, lambda led: _suppressed(led, _ts(4), "digest_degraded_audit_unavailable")
    )
    assert facts.degraded == 1
    assert facts.events[0]["reason"] == "digest_degraded_audit_unavailable"


def test_an_ordinary_suppressed_wake_is_not_degraded(tmp_path):
    """The daily quiet tick — nothing in escalation range — is the healthy case
    and must never page."""
    facts = _facts(
        tmp_path, lambda led: _suppressed(led, _ts(5), "no_deadline_in_escalation_range")
    )
    assert facts.count == 0
    assert facts.degraded == 0


def test_a_partial_degradation_on_an_emitted_wake_also_pages(tmp_path):
    """The digest shipped (explicit absences) but lookups failed — the
    degraded_reason the pre_run stamped on the EMITTED_WAKE row pages too."""

    def build(led):
        led.add(
            _ts(6),
            "EMITTED_WAKE",
            {
                "decision_basis": "deadline_in_escalation_range",
                "degraded_reason": "digest sent with explicit absences: 3 of 40 matter lookup(s) failed",
            },
            skill_name="deadline-miss-escalator",
        )

    facts = _facts(tmp_path, build)
    assert facts.degraded == 1
    assert "explicit absences" in facts.events[0]["reason"]


def test_an_ordinary_emitted_wake_is_not_degraded(tmp_path):
    def build(led):
        led.wake(_ts(7), needs_you=0)

    facts = _facts(tmp_path, build)
    assert facts.degraded == 0
