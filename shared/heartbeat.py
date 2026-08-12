"""Machine → control-plane heartbeat emitter (ADR 0023 Wave 1).

A background daemon thread, hosted inside the always-on webhook-gate
process, that every ``HEARTBEAT_PERIOD_SECONDS`` (default 60):

  1. POSTs a heartbeat to the console at ``/api/internal/heartbeat`` with
     the shared ``MACHINE_HEARTBEAT_KEY`` bearer + ``X-Tenant-Slug`` header,
     carrying ``heartbeat_ts``, ``last_audit_ts``, ``last_skill_ts``,
     ``process_uptime_seconds`` and ``version``. The console upserts the
     ``fleet_status`` row; the admin fleet view (``/admin/operator/costs/``)
     renders liveness / staleness / uptime from it. Before this emitter
     existed the receiver + admin columns were built but every row read
     "no signal yet" forever because no Machine ever phoned home.

  2. Pings the customer's healthchecks.io URL (``HEALTHCHECKS_PING_URL``,
     if provisioned) so the external dead-man switch stays green. Grace
     expiration there fires an alert row via the console webhook,
     independent of the control-plane POST — the outside-the-trust-boundary
     liveness signal (ADR 0023 locked-decision #8).

Fail-soft by construction. Every tick is wrapped so a network error, a
missing secret, or an unreadable audit DB logs at WARNING and the thread
keeps ticking. The emitter NEVER raises into the gate: observability must
not take down the customer-facing surface.

Why the gate hosts it. The gate is the one non-agent process that already
runs on every Machine (it serves the MCP door and the runtime-read seam),
and it keeps its inherited copy of ``MACHINE_HEARTBEAT_KEY``. bootstrap.sh
strips that key from the *agent* (hermes gateway) env before the exec, so
a code-executing agent cannot forge heartbeats for another tenant's slug —
the Wave-1 shared-key + attacker-controlled ``X-Tenant-Slug`` weakness
(ADR 0023 locked-decision #10). Keeping the emitter in the gate keeps the
key out of the agent.
"""

from __future__ import annotations

import http.client
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

logger = logging.getLogger("hermes-smd-heartbeat")

DEFAULT_INGEST_URL = "https://smd.services/api/internal/heartbeat"
DEFAULT_PERIOD_SECONDS = 60
_HTTP_TIMEOUT_SECONDS = 10


def _iso_utc_now() -> str:
    """Current instant as an ISO-8601 UTC string, matching the audit ``ts``
    shape the console already parses (``...+00:00``)."""
    return datetime.now(timezone.utc).isoformat()


def read_uptime_seconds() -> int | None:
    """Seconds since the Machine (container) booted, from ``/proc/uptime``.

    On a Fly Machine the container IS the unit of restart, so ``/proc/uptime``
    is exactly "time since last Machine restart" (ADR 0023 ``/health`` shape).
    Returns ``None`` if ``/proc/uptime`` is unreadable (non-Linux dev host);
    the field is optional at the receiver.
    """
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def read_audit_timestamps(db_path: str | None) -> tuple[str | None, str | None]:
    """(last_audit_ts, last_skill_ts) from the read-only audit DB.

    ``last_audit_ts`` is the newest ``audit_log.ts``; ``last_skill_ts`` is the
    newest ``ts`` on a row that carries a ``skill_name``. Both are ``None`` when
    the DB or the ``audit_log`` table does not exist yet (a freshly-booted
    Machine that has done no work) — a legitimate empty state, not an error.
    Opens the DB read-only so a heartbeat can never perturb the audit writer.
    """
    if not db_path or not os.path.exists(db_path):
        return (None, None)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        logger.warning("heartbeat: cannot open audit DB read-only: %s", exc)
        return (None, None)
    try:
        conn.execute("PRAGMA busy_timeout=2000")
        last_audit = _scalar(conn, "SELECT ts FROM audit_log ORDER BY id DESC LIMIT 1")
        last_skill = _scalar(
            conn,
            "SELECT ts FROM audit_log "
            "WHERE skill_name IS NOT NULL AND skill_name != '' "
            "ORDER BY id DESC LIMIT 1",
        )
        return (last_audit, last_skill)
    except sqlite3.Error:
        # DB exists but audit_log table not created yet, or a transient lock.
        return (None, None)
    finally:
        conn.close()


def _scalar(conn: sqlite3.Connection, sql: str) -> str | None:
    row = conn.execute(sql).fetchone()
    return row[0] if row and row[0] is not None else None


def build_payload(
    *,
    heartbeat_ts: str,
    last_audit_ts: str | None,
    last_skill_ts: str | None,
    uptime_seconds: int | None,
    version: str | None,
    sticky_stop_level: str | None = None,
    scheduler_ok: bool | None = None,
    scheduler_job_count: int | None = None,
    scheduler_max_overdue_seconds: int | None = None,
    connector_check_ok: bool | None = None,
    connectors: dict[str, dict] | None = None,
    connector_token_age: dict[str, int] | None = None,
    spec_control_ok: bool | None = None,
    spec_control: dict[str, dict] | None = None,
    webhook_surface_ok: bool | None = None,
    webhook_surface: dict[str, dict] | None = None,
    cron_containment: bool | None = None,
) -> dict[str, object]:
    """Assemble the heartbeat body. ``heartbeat_ts`` is the only required
    field at the receiver; optional fields are omitted when absent rather
    than sent as null (the receiver COALESCEs, but a smaller body is
    cleaner and never overwrites a good prior value with null).

    The scheduler_* fields use ``is not None`` checks deliberately: a failing
    check (``scheduler_ok=False`` → 0) and an empty store (``job_count=0``)
    are REAL values that must reach the wire — truthiness-omitting them would
    silence exactly the states the work-liveness alerter exists to see. The
    console stores these three as overwrite-including-NULL (not COALESCE) and
    holds open alerts rather than resolving when a field is absent."""
    payload: dict[str, object] = {"heartbeat_ts": heartbeat_ts}
    if last_audit_ts:
        payload["last_audit_ts"] = last_audit_ts
    if last_skill_ts:
        payload["last_skill_ts"] = last_skill_ts
    if uptime_seconds is not None:
        payload["process_uptime_seconds"] = uptime_seconds
    if version:
        payload["version"] = version
    if sticky_stop_level:
        payload["sticky_stop_level"] = sticky_stop_level
    if scheduler_ok is not None:
        payload["scheduler_ok"] = 1 if scheduler_ok else 0
    if scheduler_job_count is not None:
        payload["scheduler_job_count"] = scheduler_job_count
    if scheduler_max_overdue_seconds is not None:
        payload["scheduler_max_overdue_seconds"] = scheduler_max_overdue_seconds
    # Connector health (ADR 0080). Same is-not-None discipline: an empty map
    # ({}) is a REAL "check ran, no MCP calls observed" state and a failing
    # check (connector_check_ok=False → 0) must reach the wire; the console
    # stores both overwrite-including-NULL and holds alerts on absence.
    if connector_check_ok is not None:
        payload["connector_check_ok"] = 1 if connector_check_ok else 0
    if connectors is not None:
        payload["connectors"] = connectors
    # Durable-credential ages (ss#2148). A separate field from the health map
    # by design: it must never synthesize a health entry (a fabricated
    # consecutive_failures=0 would falsely resolve an open alert). Absent map
    # or absent server = nothing to report (hold), never zero.
    if connector_token_age:
        payload["connector_token_age"] = connector_token_age
    # Cron containment (ss-console#2276). is-not-None discipline like the
    # scheduler fields: 1 = the volume sentinel is present and boot converged
    # the cron stores to zero managed jobs; 0 = normal. A contained seat must
    # be visibly contained on the console, never mistaken for a quiet one.
    if cron_containment is not None:
        payload["cron_containment"] = 1 if cron_containment else 0
    # Authored-spec control health (ss-console #2234). Same is-not-None
    # discipline for the same reason: an empty map is a REAL "checked, every
    # declared spec is installed" state, and it is the state that RESOLVES an
    # open alert — truthiness-omitting it would leave a repaired control paging
    # forever. `spec_control_ok=False` means the check could not read the config
    # or the manifest, which pages on its own rather than being reported as a
    # missing spec: the firm's authoring gap and our own blindness want opposite
    # responses.
    if spec_control_ok is not None:
        payload["spec_control_ok"] = 1 if spec_control_ok else 0
    if spec_control is not None:
        payload["spec_control"] = spec_control
    # Webhook expected-tool surface (ss-console #2222, the WARN tier). Same
    # is-not-None discipline once more: an empty map is a REAL "checked, every
    # expected tool is offered" state and it is what RESOLVES an open alert, and
    # `webhook_surface_ok=False` means the boot check could not resolve the
    # surface at all — our blindness, which pages separately from a missing tool
    # for the same reason spec_control splits the two.
    if webhook_surface_ok is not None:
        payload["webhook_surface_ok"] = 1 if webhook_surface_ok else 0
    if webhook_surface is not None:
        payload["webhook_surface"] = webhook_surface
    return payload


def _default_post(url: str, headers: dict[str, str], body: bytes) -> int:
    """POST ``body`` to ``url`` over HTTPS/HTTP, returning the status code.

    Stdlib ``http.client`` (no third-party dependency), matching the gate's
    existing forward path. Raises on connection failure; the caller catches.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "http":
        conn: http.client.HTTPConnection = http.client.HTTPConnection(
            host, parts.port or 80, timeout=_HTTP_TIMEOUT_SECONDS
        )
    else:
        conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=_HTTP_TIMEOUT_SECONDS)
    try:
        conn.request("POST", parts.path or "/", body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def _default_ping(url: str) -> None:
    """Best-effort GET to a healthchecks.io ping URL. Errors are swallowed by
    the caller's wrapper; a missed ping just delays the external dead-man."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "http":
        conn: http.client.HTTPConnection = http.client.HTTPConnection(
            host, parts.port or 80, timeout=_HTTP_TIMEOUT_SECONDS
        )
    else:
        conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=_HTTP_TIMEOUT_SECONDS)
    try:
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        conn.request("GET", path)
        conn.getresponse().read()
    finally:
        conn.close()


class HeartbeatEmitter:
    """Background ticker that emits control-plane + healthchecks heartbeats.

    Construct with the runtime config; call :meth:`start` to launch the daemon
    thread and :meth:`stop` to end it. ``post_fn`` / ``ping_fn`` are injectable
    so tests exercise the tick logic without a socket.
    """

    def __init__(
        self,
        *,
        slug: str | None,
        key: str | None,
        ingest_url: str,
        healthchecks_url: str | None,
        version: str | None,
        audit_db_path_fn,
        period_seconds: int = DEFAULT_PERIOD_SECONDS,
        post_fn=_default_post,
        ping_fn=_default_ping,
        scheduler_check_fn=None,
        scheduler_check_debounce: int = 3,
        connector_check_fn=None,
        connector_check_debounce: int = 3,
        spec_control_check_fn=None,
        spec_control_check_debounce: int = 3,
        webhook_surface_check_fn=None,
    ) -> None:
        self._slug = slug
        self._key = key
        self._ingest_url = ingest_url
        self._healthchecks_url = healthchecks_url
        self._version = version
        self._audit_db_path_fn = audit_db_path_fn
        self._period = max(5, period_seconds)
        self._post_fn = post_fn
        self._ping_fn = ping_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Work-liveness self-check (shared.scheduler_check). Injectable for
        # tests; None = the real check with uptime-based boot suppression.
        self._scheduler_check_fn = scheduler_check_fn or _default_scheduler_check
        self._sched_debounce = max(1, scheduler_check_debounce)
        self._sched_fail_count = 0
        self._sched_last_good = None
        # Connector-health self-check (shared.connector_check, ADR 0080).
        # Same injectable + debounce shape as the scheduler check.
        self._connector_check_fn = connector_check_fn or _default_connector_check
        self._conn_debounce = max(1, connector_check_debounce)
        self._conn_fail_count = 0
        self._conn_last_good = None
        # Authored-spec control self-check (shared.spec_control_check, #2234).
        # Same injectable + debounce shape again: three checks behaving alike is
        # the point — an operator should not have to learn each one's moods.
        self._spec_control_check_fn = spec_control_check_fn or _default_spec_control_check
        self._spec_debounce = max(1, spec_control_check_debounce)
        self._spec_fail_count = 0
        self._spec_last_good = None
        # Webhook expected-tool surface check (#2222). No debounce, unlike the
        # three above: it reads one local sentinel written once per boot, which
        # has no transient-failure mode a debounce would smooth — see
        # shared/webhook_surface_check.py.
        self._webhook_surface_check_fn = webhook_surface_check_fn or _default_webhook_surface_check

    def start(self) -> bool:
        """Launch the daemon thread. Returns False (and logs) when the
        control-plane heartbeat cannot be sent for lack of a slug or key —
        the healthchecks ping still runs if its URL is present, so a
        misconfigured shared key does not also silence the external
        dead-man switch."""
        if not self._slug or not self._key:
            if self._healthchecks_url:
                logger.warning(
                    "heartbeat: MACHINE_HEARTBEAT_KEY or slug missing; "
                    "control-plane POST disabled, healthchecks ping still active"
                )
            else:
                logger.warning(
                    "heartbeat: MACHINE_HEARTBEAT_KEY or slug missing and no "
                    "healthchecks URL; emitter not started (admin fleet view "
                    "will read 'no signal yet')"
                )
                return False
        self._thread = threading.Thread(target=self._run, name="smd-heartbeat", daemon=True)
        self._thread.start()
        logger.info(
            "heartbeat: emitter started (period=%ds, control-plane=%s, healthchecks=%s)",
            self._period,
            "on" if (self._slug and self._key) else "off",
            "on" if self._healthchecks_url else "off",
        )
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while True:
            self._tick()
            if self._stop.wait(self._period):
                return

    def _tick(self) -> None:
        """One heartbeat cycle. Each leg is independently wrapped so one
        failing does not skip the other, and neither ever escapes the thread."""
        if self._slug and self._key:
            try:
                self._post_control_plane()
            except Exception as exc:  # never let the emitter die
                logger.warning("heartbeat: control-plane POST failed: %s", exc)
        if self._healthchecks_url:
            try:
                self._ping_fn(self._healthchecks_url)
            except Exception as exc:
                logger.warning("heartbeat: healthchecks ping failed: %s", exc)

    def _read_scheduler_check(self):
        """Run the work-liveness self-check with a consecutive-failure
        debounce. A transient crash (< debounce ticks) keeps reporting the
        last-known-good verdict; a persistent crash reports ``ok=False``
        with the last-good job count — REPORTED, never omitted, because an
        omitted field on a crashed checker would recreate the exact
        "monitoring green while broken" class this exists to close. Returns
        None only before the first-ever success (console holds on absence)."""
        from shared.scheduler_check import SchedulerCheck

        try:
            result = self._scheduler_check_fn()
        except Exception as exc:  # noqa: BLE001 — the check must never kill the beat
            self._sched_fail_count += 1
            logger.warning(
                "heartbeat: scheduler check failed (%d consecutive): %s",
                self._sched_fail_count,
                exc,
            )
            if self._sched_fail_count >= self._sched_debounce:
                last = self._sched_last_good
                return SchedulerCheck(
                    ok=False,
                    job_count=last.job_count if last else 0,
                    max_overdue_seconds=None,
                )
            return self._sched_last_good
        self._sched_fail_count = 0
        self._sched_last_good = result
        return result

    def _read_connector_check(self):
        """Run the connector-health self-check with the same consecutive-
        failure debounce as the scheduler check. A connectors MAP has no
        natural degraded value the way ``scheduler_ok=False`` is one, so a
        persistent crash reports ``ConnectorCheck(ok=False, servers=None)``
        — the boolean IS the reported failure state, and the console pages
        ``connector_check_error`` instead of the whole connector alert
        class going silently dark. Returns None only before the first-ever
        success (console holds on absence)."""
        from shared.connector_check import ConnectorCheck

        try:
            result = self._connector_check_fn()
        except Exception as exc:  # noqa: BLE001 — the check must never kill the beat
            self._conn_fail_count += 1
            logger.warning(
                "heartbeat: connector check failed (%d consecutive): %s",
                self._conn_fail_count,
                exc,
            )
            if self._conn_fail_count >= self._conn_debounce:
                return ConnectorCheck(ok=False, servers=None)
            return self._conn_last_good
        self._conn_fail_count = 0
        self._conn_last_good = result
        return result

    def _read_spec_control_check(self):
        """Run the authored-spec control self-check, debounced like the others.

        Same shape as ``_read_connector_check`` and for the same reason: an
        entries MAP has no natural degraded value, so a persistent crash reports
        ``SpecControlCheck(ok=False, entries=None)`` — the boolean IS the
        reported failure, and the console pages ``spec_control_unprovable``
        rather than the class going dark. Returns None only before the
        first-ever success (console holds on absence).
        """
        from shared.spec_control_check import SpecControlCheck

        try:
            result = self._spec_control_check_fn()
        except Exception as exc:  # noqa: BLE001 — the check must never kill the beat
            self._spec_fail_count += 1
            logger.warning(
                "heartbeat: spec control check failed (%d consecutive): %s",
                self._spec_fail_count,
                exc,
            )
            if self._spec_fail_count >= self._spec_debounce:
                return SpecControlCheck(ok=False, entries=None)
            return self._spec_last_good
        self._spec_fail_count = 0
        self._spec_last_good = result
        return result

    def _read_webhook_surface_check(self):
        """Read the warn-tier webhook-surface sentinel (#2222).

        ``None`` (no usable sentinel, or a seat that serves no webhook platform)
        omits both fields so the console holds. A raise reports
        ``ok=False, tools=None`` rather than going dark — the same
        broken-check-pages posture the other three take.
        """
        from shared.webhook_surface_check import WebhookSurfaceCheck

        try:
            return self._webhook_surface_check_fn()
        except Exception as exc:  # noqa: BLE001 — the check must never kill the beat
            logger.warning("heartbeat: webhook surface check failed: %s", exc)
            return WebhookSurfaceCheck(ok=False, tools=None)

    def _post_control_plane(self) -> None:
        last_audit_ts, last_skill_ts = read_audit_timestamps(self._audit_db_path_fn())
        # ADR 0062: surface the cost-breaker ladder level so the fleet view
        # can escalate a tripped seat. Read-only; any failure omits the field
        # (the receiver treats absence as unknown, never as OK).
        level: str | None = None
        try:
            from shared.cost_breaker import read_level

            level = read_level()
        except Exception as exc:  # noqa: BLE001 — heartbeat stays fail-soft
            logger.debug("heartbeat: sticky_stop level read failed: %s", exc)
        sched = self._read_scheduler_check()
        conn = self._read_connector_check()
        spec = self._read_spec_control_check()
        surface = self._read_webhook_surface_check()
        token_age: dict[str, int] | None = None
        try:
            from shared.connector_check import token_ages

            token_age = token_ages() or None
        except Exception as exc:  # noqa: BLE001 — heartbeat stays fail-soft
            logger.debug("heartbeat: token-age read failed: %s", exc)
        payload = build_payload(
            heartbeat_ts=_iso_utc_now(),
            last_audit_ts=last_audit_ts,
            last_skill_ts=last_skill_ts,
            uptime_seconds=read_uptime_seconds(),
            version=self._version,
            sticky_stop_level=level,
            scheduler_ok=sched.ok if sched is not None else None,
            scheduler_job_count=sched.job_count if sched is not None else None,
            scheduler_max_overdue_seconds=(
                sched.max_overdue_seconds if sched is not None else None
            ),
            connector_check_ok=conn.ok if conn is not None else None,
            connectors=conn.servers if conn is not None else None,
            connector_token_age=token_age,
            spec_control_ok=spec.ok if spec is not None else None,
            spec_control=spec.entries if spec is not None else None,
            webhook_surface_ok=surface.ok if surface is not None else None,
            webhook_surface=surface.tools if surface is not None else None,
            cron_containment=_read_cron_containment(),
        )
        import json

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._key}",
            "X-Tenant-Slug": self._slug or "",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        status = self._post_fn(self._ingest_url, headers, body)
        if status == 200:
            logger.debug("heartbeat: control-plane accepted (200)")
        elif status == 401:
            logger.warning(
                "heartbeat: control-plane 401 — MACHINE_HEARTBEAT_KEY mismatch "
                "or slug not in customer_configs (admin view stays 'no signal yet')"
            )
        else:
            logger.warning("heartbeat: control-plane returned %d", status)


def _read_cron_containment() -> bool | None:
    """Sentinel presence for the heartbeat (ss-console#2276). A cheap stat per
    tick. Tri-state by construction: True contained, False genuinely not
    contained, None omitted when the volume cannot be read — a read error must
    never report a false 'not contained'.

    That guarantee lives in ``containment_state``, not in the except clause
    below: ``containment_active`` swallows OSError by design for bootstrap, so
    calling it here made this wrapper's None path unreachable for the very
    failure it claimed to cover (ss-console#2291). The except stays only for
    the lazy import, which can genuinely fail."""
    try:
        from shared.cron_containment import containment_state

        return containment_state()
    except Exception as exc:  # noqa: BLE001 — the check must never kill the beat
        logger.debug("heartbeat: cron-containment read failed: %s", exc)
        return None


def _default_scheduler_check():
    """The real work-liveness check, with uptime-based boot suppression.
    Lazy import keeps heartbeat importable even if the check module is
    somehow absent (the emitter's debounce then reports the failure)."""
    from shared.scheduler_check import check

    return check(uptime_seconds=read_uptime_seconds())


def _default_connector_check():
    """The real connector-health check (ADR 0080). Lazy import for the same
    reason as the scheduler default: a missing module surfaces through the
    emitter's debounce as connector_check_ok=0, reported not omitted."""
    from shared.connector_check import check

    return check()


def _default_spec_control_check():
    """The real authored-spec control check (ss-console #2234). Lazy import for
    the same reason as the other two: a missing module surfaces through the
    emitter's debounce as spec_control_ok=0, reported not omitted."""
    from shared.spec_control_check import check

    return check()


def _default_webhook_surface_check():
    """The real warn-tier webhook-surface check (ss-console #2222). Lazy import
    for the same reason as the other three: a missing module surfaces as
    webhook_surface_ok=0, reported not omitted."""
    from shared.webhook_surface_check import check

    return check()


def emitter_from_env(audit_db_path_fn) -> HeartbeatEmitter:
    """Build a :class:`HeartbeatEmitter` from the gate process environment.

    Called once from the gate's ``main()``. Reads the shared key + slug the
    gate inherited at fork (the agent has them stripped), the optional
    healthchecks ping URL, and the baked overlay ref for the ``version``
    field.
    """
    try:
        period = int(os.environ.get("HEARTBEAT_PERIOD_SECONDS", str(DEFAULT_PERIOD_SECONDS)))
    except ValueError:
        period = DEFAULT_PERIOD_SECONDS
    try:
        debounce = int(os.environ.get("SCHEDULER_CHECK_DEBOUNCE", "3"))
    except ValueError:
        debounce = 3
    try:
        conn_debounce = int(os.environ.get("CONNECTOR_CHECK_DEBOUNCE", "3"))
    except ValueError:
        conn_debounce = 3
    try:
        spec_debounce = int(os.environ.get("SPEC_CONTROL_CHECK_DEBOUNCE", "3"))
    except ValueError:
        spec_debounce = 3
    return HeartbeatEmitter(
        slug=os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG"),
        key=os.environ.get("MACHINE_HEARTBEAT_KEY"),
        ingest_url=os.environ.get("HEARTBEAT_INGEST_URL", DEFAULT_INGEST_URL),
        healthchecks_url=os.environ.get("HEALTHCHECKS_PING_URL"),
        version=os.environ.get("SMD_OVERLAY_REF"),
        audit_db_path_fn=audit_db_path_fn,
        period_seconds=period,
        scheduler_check_debounce=debounce,
        connector_check_debounce=conn_debounce,
        spec_control_check_debounce=spec_debounce,
    )


__all__ = [
    "HeartbeatEmitter",
    "build_payload",
    "emitter_from_env",
    "read_audit_timestamps",
    "read_uptime_seconds",
]
