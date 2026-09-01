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
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
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


class AuditLedgerFacts(NamedTuple):
    """What one read-only pass over the ledger can say about it.

    ``last_audit_ts`` / ``last_skill_ts`` are the pre-existing pair. ``head``
    and ``rows`` are ss-console #2500's off-Machine chain pin: the ``row_hash``
    of the newest CHAINED row and the total row count, read in the same pass so
    the two can never describe different moments of the ledger.

    ``send_refusals`` / ``send_refusals_last_ts`` / ``send_refusals_json`` are
    ss-console#2547: how many times in the last day a routine tried to reach a
    human and could not, or had something to say and never tried. Three fields
    rather than one because the console pages on the TIMESTAMP (a new event, not
    a nonzero level) and a human reading the page needs the routine and the
    reason without opening the seat.
    """

    last_audit_ts: str | None
    last_skill_ts: str | None
    head: str | None
    rows: int | None
    send_refusals: int | None = None
    send_refusals_last_ts: str | None = None
    send_refusals_json: list[dict] | None = None


class SendRefusalFacts(NamedTuple):
    """A day of a seat's failures to reach a human, counted two ways.

    ``count`` is refusals PLUS silent wakes; ``last_ts`` is the newest event of
    either kind (the console's paging marker); ``events`` is the newest few,
    each naming the routine, the tool, and the reason — never a value.

    ``refused``, ``unsent``, and ``degraded`` break the total apart. Only
    ``count`` rides the heartbeat — the console pages on the timestamp, not on a
    level, and more integers would be more fields to ingest for no decision they
    change. The split exists for the retro-falsifier, whose known-good table is
    per kind, so a run that matched the total by getting the halves wrong would
    still be caught.
    """

    count: int
    last_ts: str | None
    events: list[dict]
    refused: int = 0
    unsent: int = 0
    degraded: int = 0


#: Trailing window. A day, because the routines this watches fire daily: a
#: shorter window would let a morning refusal age out before anyone read the
#: page, and a longer one would keep re-describing an event already handled.
SEND_REFUSAL_WINDOW_HOURS = 24

#: How many events ride the beat. Enough to show a pattern (five refusals in one
#: minute IS the 2026-08-19 shape), few enough to keep the payload small.
_SEND_REFUSAL_EVENT_CAP = 5

#: Cap on a reason string. Reasons here are refusal MESSAGES, which name kinds
#: rather than values by construction (``outbound._identifier_refusal_message``
#: names the identifier kinds and never the identifier), so this bound is about
#: payload size and a readable subject line, NOT redaction. The kinds sit at the
#: front of every message this path produces, so the cap keeps the diagnostic
#: half.
_MAX_REASON_CHARS = 200

#: A wake whose routine wrote no ``LLM_TURN_COMPLETED`` at all gets this much
#: room to have sent something. Reached when the turn crashed or the ledger has
#: a gap, where the honest bound is a short fixed one rather than "until the
#: next thing happened".
_UNCLOSED_SESSION_SPAN = 30 * 60

#: And when the routine DID write a turn row, the span still stops here. A turn
#: row found four hours after the wake belongs to the next run, and borrowing
#: its dispatch would clear a silence it had nothing to do with.
_SESSION_SPAN_CAP = 60 * 60

#: Recipients the falsifier and the smoke tests send to. A dispatch that reached
#: ONLY these is not a routine reaching a human, so it does not clear a wake.
_PROBE_RECIPIENT_PREFIX = "ss-probe"


def _iso_floor(value: str | None) -> str:
    """The comparable prefix of an audit ``ts``: ``YYYY-MM-DDTHH:MM:SS``.

    Rows are written by three different processes and two repos —
    ``shared.ids.iso_utc`` stamps ``...123Z`` while test fixtures and older rows
    carry ``...+00:00`` — so the offset spelling is not something this query may
    depend on. Every timestamp on both sides of every comparison goes through
    here, which makes the comparison a comparison of INSTANTS in a fixed-width
    field rather than of two spellings that happen to sort.
    """
    return (value or "")[:19]


def count_send_refusals(conn: sqlite3.Connection, now: datetime) -> SendRefusalFacts:
    """Every time in the trailing day this seat had something for a human and
    did not deliver it. THE PURE QUERY — the ticker and the retro-falsifier
    (``tests/tools/send_refusals_retro.py``) both call exactly this, so a number
    reported from a live seat and a number computed from a ledger copy cannot
    be produced by two different definitions.

    Two kinds, because "refused" and "did not try" look identical from outside
    and BOTH happened:

    * ``refused`` — a routine's send was turned down. Two row shapes carry that:
      a ``TOOL_CALL_COMPLETED`` whose outcome is not ``ok`` on a resolved
      ``external_send*`` class from a cron-shaped session (the gate refusing
      before dispatch — the 2026-08-19 identifier and em-dash refusals), and a
      ``CONFIRM_SEND_FAILED`` (the broker refusing or the transport failing
      after dispatch was authorized). The first is scoped to cron sessions on
      purpose: a person talking to the Operator can see their own refusal and
      does not need a page.
    * ``unsent`` — an ``EMITTED_WAKE`` that woke the routine WITH needs-you
      items, whose session then dispatched nothing to a real address. This is
      the 2026-08-20 instance: five items waiting and not one send attempted,
      which no refusal-shaped query can see because there is no refusal.
    * ``degraded`` — a ``SUPPRESSED_WAKE`` whose ``decision_basis`` starts with
      ``digest_degraded``: the routine's own pre-run judged its output unfit to
      send (2026-08-24 — a digest naming zero matters) and withheld it. The
      person the digest was for got nothing, deliberately; this kind is what
      turns that deliberate nothing into a page instead of a silence. The reason
      carries the run's own counts from the row metadata so the page reads
      "N deadlines withheld", not a bare basis token. The literal
      ``digest_degraded`` prefix is written by ``ss-console``'s
      ``operator/skills/deadline-miss-escalator/pre_run.py`` — two repos, one
      string; the pin lives in ``tests/test_heartbeat.py``.

    THE SESSION SPAN, and why it is joined on skill and time. Neither end of it
    can be joined on a session id, because neither row has one: ``EMITTED_WAKE``
    is written by the ``pre_run.py`` child before the turn exists (0 of 17 pilot
    rows carry a session id, read live 2026-08-22) and the broker writes
    ``CONFIRM_SEND_DISPATCHED`` without one on both live seats. So the span runs
    from the wake to the first ``LLM_TURN_COMPLETED`` of the same ROUTINE at or
    after it — ``skill_name`` is a column on both — bounded at either end by
    ``_SESSION_SPAN_CAP`` / ``_UNCLOSED_SESSION_SPAN``. Overlapping a
    neighbouring routine's dispatch can only SUPPRESS a page, never invent one,
    which is the direction an alert should err in when its own join is
    approximate.

    Raises on a ledger this query cannot run against (an older schema, a missing
    JSON1 build). The caller runs it in its own try and omits the fields, so a
    seat that cannot answer holds rather than reporting a reassuring zero.
    """
    cutoff = _iso_floor(_as_utc(now).isoformat())
    horizon = _iso_floor((_as_utc(now) - timedelta(hours=SEND_REFUSAL_WINDOW_HOURS)).isoformat())
    refused = _refused_events(conn, horizon, cutoff)
    unsent = _unsent_events(conn, horizon, cutoff)
    degraded = _degraded_events(conn, horizon, cutoff)
    events = refused + unsent + degraded
    # Newest first, so the cap keeps the newest rather than whichever kind the
    # queries happened to run in.
    events.sort(key=lambda e: e["ts"], reverse=True)
    last_ts = events[0]["ts"] if events else None
    return SendRefusalFacts(
        count=len(events),
        last_ts=last_ts,
        events=events[:_SEND_REFUSAL_EVENT_CAP],
        refused=len(refused),
        unsent=len(unsent),
        degraded=len(degraded),
    )


def _refused_events(conn: sqlite3.Connection, horizon: str, cutoff: str) -> list[dict]:
    """Refusal rows in the window, as events.

    The window is closed at BOTH ends. For the live ticker the upper bound is
    "now" and costs nothing, but this same function answers the retro-falsifier's
    question about a day in the past, and an open-ended window there counts
    events that had not happened yet: a dry run over the pilot ledger reported
    2026-08-19's five refusals against 08-18, a day whose known answer is zero.
    The instrument's own falsifier found it, which is what a falsifier is for.

    ``json_extract`` does the field predicates in SQLite so the trailing day's
    ``TOOL_CALL_COMPLETED`` rows — the bulk of any seat's ledger — are filtered
    by the engine rather than parsed one at a time in a 1-vCPU gate process on
    every beat.

    ``LIKE 'cron\\_%' ESCAPE '\\'`` is deliberate: unescaped, ``_`` is a
    single-character wildcard and the clause would also match a session named
    ``cronjob-...``. Belt and braces beside ``cron_job_id``, which is what a
    MANAGED job's row actually carries; the LIKE is what catches an unmanaged
    one-shot whose id never resolved to a stored job.
    """
    sql = (
        "SELECT ts, action_type, skill_name,"
        " json_extract(metadata,'$.routine') AS routine,"
        " json_extract(metadata,'$.skill') AS skill,"
        " json_extract(metadata,'$.tool') AS tool,"
        " json_extract(metadata,'$.verb') AS verb,"
        " json_extract(metadata,'$.error_type') AS error_type,"
        " json_extract(metadata,'$.outcome') AS outcome"
        " FROM audit_log"
        " WHERE substr(ts,1,19) >= ? AND substr(ts,1,19) <= ?"
        " AND ("
        "   (action_type = 'TOOL_CALL_COMPLETED'"
        "    AND json_extract(metadata,'$.outcome') IS NOT NULL"
        "    AND json_extract(metadata,'$.outcome') <> 'ok'"
        "    AND json_extract(metadata,'$.resolved_action_class') LIKE 'external\\_send%' ESCAPE '\\'"
        "    AND (json_extract(metadata,'$.cron_job_id') IS NOT NULL"
        "         OR json_extract(metadata,'$.session_id') LIKE 'cron\\_%' ESCAPE '\\'))"
        "   OR action_type = 'CONFIRM_SEND_FAILED'"
        " )"
    )
    out: list[dict] = []
    for (
        ts,
        action_type,
        skill_name,
        routine,
        skill,
        tool,
        verb,
        error_type,
        outcome,
    ) in conn.execute(sql, (horizon, cutoff)):
        if action_type == "CONFIRM_SEND_FAILED":
            # The broker's row carries ``reason = str(exc)``, which can quote the
            # recipient it refused. ``outcome`` is the closed vocabulary next to
            # it (``refused`` / ``transport_error``) and says the same thing about
            # kind without carrying an address off the seat.
            reason = outcome
        else:
            reason = error_type or outcome
        out.append(
            _event(
                ts=ts,
                kind="refused",
                routine=routine or skill or skill_name,
                tool=tool or verb,
                reason=reason,
            )
        )
    return out


def _unsent_events(conn: sqlite3.Connection, horizon: str, cutoff: str) -> list[dict]:
    """Wakes that carried needs-you items and produced no send to a real person."""
    wakes = conn.execute(
        "SELECT ts, skill_name,"
        " json_extract(metadata,'$.routine') AS routine,"
        " json_extract(metadata,'$.digest_needs_you') AS needs_you"
        " FROM audit_log"
        " WHERE action_type = 'EMITTED_WAKE'"
        " AND substr(ts,1,19) >= ? AND substr(ts,1,19) <= ?"
        " AND CAST(COALESCE(json_extract(metadata,'$.digest_needs_you'), 0) AS INTEGER) > 0",
        (horizon, cutoff),
    ).fetchall()
    out: list[dict] = []
    for ts, skill_name, routine, needs_you in wakes:
        start = _iso_floor(ts)
        end = _session_span_end(conn, skill_name, start)
        if end > cutoff:
            # The session may still be running. Nothing to conclude yet, and a
            # page for a routine that is mid-turn would be a page for nothing.
            continue
        if _dispatched_to_a_person(conn, start, end):
            continue
        event = _event(
            ts=ts,
            kind="unsent",
            routine=routine or skill_name,
            tool=None,
            reason="no_send_attempted",
        )
        try:
            event["needs_you"] = int(needs_you)
        except (TypeError, ValueError):
            pass
        out.append(event)
    return out


def _degraded_events(conn: sqlite3.Connection, horizon: str, cutoff: str) -> list[dict]:
    """Suppressed wakes whose basis says the routine withheld a degraded output.

    The matching is a PREFIX (``digest_degraded``), not an equality, because the
    suppression path has more than one basis (``digest_degraded_suppressed`` for
    the clean suppress, ``digest_degraded_audit_unavailable`` for the stripped
    wake when the suppress row itself could not be written) and a new sibling
    basis must page by default rather than by remembering to update this query.
    """
    rows = conn.execute(
        "SELECT ts, skill_name,"
        " json_extract(metadata,'$.routine') AS routine,"
        " json_extract(metadata,'$.decision_basis') AS basis,"
        " json_extract(metadata,'$.degraded_reason') AS reason"
        " FROM audit_log"
        " WHERE action_type = 'SUPPRESSED_WAKE'"
        " AND substr(ts,1,19) >= ? AND substr(ts,1,19) <= ?"
        " AND json_extract(metadata,'$.decision_basis') LIKE 'digest_degraded%'"
        # PARTIAL degradation: the digest shipped (explicit absences per item)
        # but some lookups failed, and the pre_run stamped degraded_reason onto
        # the EMITTED_WAKE row instead. Same page, different row shape — a
        # 1-of-40 run must not sail silently just because it sailed.
        " UNION ALL"
        " SELECT ts, skill_name,"
        " json_extract(metadata,'$.routine') AS routine,"
        " json_extract(metadata,'$.decision_basis') AS basis,"
        " json_extract(metadata,'$.degraded_reason') AS reason"
        " FROM audit_log"
        " WHERE action_type = 'EMITTED_WAKE'"
        " AND substr(ts,1,19) >= ? AND substr(ts,1,19) <= ?"
        " AND json_extract(metadata,'$.degraded_reason') IS NOT NULL",
        (horizon, cutoff, horizon, cutoff),
    ).fetchall()
    out: list[dict] = []
    for ts, skill_name, routine, basis, reason in rows:
        out.append(
            _event(
                ts=ts,
                kind="degraded",
                routine=routine or skill_name,
                tool=None,
                reason=str(reason or basis or "digest_degraded"),
            )
        )
    return out


def _session_span_end(conn: sqlite3.Connection, skill_name: str | None, start: str) -> str:
    """When the wake's turn stopped being able to send.

    THE JOIN IS SKILL + TIME, NOT SESSION, and that is a measured fact rather
    than a preference. ``EMITTED_WAKE`` is written by the ``pre_run.py`` CHILD
    before the turn it decides for exists, so it has no session to name: 0 of 17
    rows on the pilot ledger carry one (read live 2026-08-22). A session join
    here would have matched nothing and reported every needs-you wake as silent,
    or — worse, depending on which way the fallback fell — none of them.

    So the span runs from the wake to the FIRST ``LLM_TURN_COMPLETED`` of the
    same routine at or after it, capped at ``_SESSION_SPAN_CAP``; and when that
    routine wrote no turn row at all, to ``_UNCLOSED_SESSION_SPAN`` past the
    wake. Both bounds are deliberately short. A long span borrows the NEXT run's
    dispatch to clear this run's silence, and a fact that can be cleared by work
    done an hour later is not a fact about this wake.

    ``skill_name`` is a COLUMN on both row types — ``write_emitted_wake`` passes
    it, and ``emit_llm_event`` stamps the resolved routine's skill — so the join
    needs no metadata parse and no cron-store lookup.
    """
    cap = _plus_seconds(start, _SESSION_SPAN_CAP)
    if skill_name:
        row = conn.execute(
            "SELECT MIN(substr(ts,1,19)) FROM audit_log"
            " WHERE action_type = 'LLM_TURN_COMPLETED'"
            " AND skill_name = ? AND substr(ts,1,19) >= ?",
            (skill_name, start),
        ).fetchone()
        if row and row[0]:
            return min(str(row[0]), cap)
    return _plus_seconds(start, _UNCLOSED_SESSION_SPAN)


def _plus_seconds(start: str, seconds: int) -> str:
    return _iso_floor(
        (
            datetime.strptime(start, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            + timedelta(seconds=seconds)
        ).isoformat()
    )


def _dispatched_to_a_person(conn: sqlite3.Connection, start: str, end: str) -> bool:
    """True iff a dispatch inside ``[start, end]`` reached a non-probe address.

    ``recipients`` is a JSON ARRAY, and it is parsed here rather than in SQL on
    purpose: ``json_each`` is a table-valued function that not every SQLite build
    exposes, while the row count it would save is tiny (dispatch rows are rare).
    The predicate that matters — "somebody other than the probe was written to" —
    is worth more than the microseconds.
    """
    rows = conn.execute(
        "SELECT metadata FROM audit_log"
        " WHERE action_type = 'CONFIRM_SEND_DISPATCHED'"
        " AND substr(ts,1,19) >= ? AND substr(ts,1,19) <= ?",
        (start, end),
    ).fetchall()
    for (metadata,) in rows:
        try:
            recipients = json.loads(metadata or "{}").get("recipients")
        except (ValueError, TypeError, AttributeError):
            # An unparseable dispatch row is still a dispatch. Treat it as one:
            # suppressing a page is the recoverable direction, inventing one on a
            # seat that DID email its owner is not.
            return True
        if not isinstance(recipients, list):
            return True
        for recipient in recipients:
            if isinstance(recipient, str) and not recipient.strip().lower().startswith(
                _PROBE_RECIPIENT_PREFIX
            ):
                return True
    return False


def _event(*, ts: str, kind: str, routine, tool, reason) -> dict:
    """One wire-shaped event. Strings only, bounded, never a value from a body."""
    return {
        "ts": str(ts or ""),
        "kind": kind,
        "routine": str(routine or ""),
        "tool": str(tool or ""),
        "reason": str(reason or "")[:_MAX_REASON_CHARS],
    }


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def read_audit_facts(db_path: str | None) -> AuditLedgerFacts:
    """Every ledger fact the beat carries, from ONE read-only connection.

    ``last_audit_ts`` is the newest ``audit_log.ts``; ``last_skill_ts`` is the
    newest ``ts`` on a row that carries a ``skill_name``; ``head`` is the
    ``row_hash`` of the newest row that has one (pre-#1686 rows carry NULL and
    are not part of the chain — see ``shared.audit_chain``); ``rows`` is
    ``COUNT(*)``. All are ``None`` when the DB or the ``audit_log`` table does
    not exist yet (a freshly-booted Machine that has done no work) — a
    legitimate empty state, not an error.

    ONE connection by construction. The head and the count are the two halves
    of an off-Machine integrity pin, and a pin whose halves were read seconds
    apart can accuse an honest ledger of losing rows. Opens read-only so a
    heartbeat can never perturb the audit writer.
    """
    if not db_path or not os.path.exists(db_path):
        return AuditLedgerFacts(None, None, None, None)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        logger.warning("heartbeat: cannot open audit DB read-only: %s", exc)
        return AuditLedgerFacts(None, None, None, None)
    try:
        conn.execute("PRAGMA busy_timeout=2000")
        last_audit = _scalar(conn, "SELECT ts FROM audit_log ORDER BY id DESC LIMIT 1")
        last_skill = _scalar(
            conn,
            "SELECT ts FROM audit_log "
            "WHERE skill_name IS NOT NULL AND skill_name != '' "
            "ORDER BY id DESC LIMIT 1",
        )
        # Chain columns in their OWN try. A pre-#1686 ledger has no ``row_hash``
        # column at all, and on such a seat the SELECT raises — sharing the
        # outer handler would have made a missing chain column take
        # ``last_audit_ts`` down with it and report a working seat as silent.
        # (Caught by the pre-existing tests/test_heartbeat.py fixture, which
        # builds exactly that older table.)
        head: str | None = None
        try:
            head = _scalar(
                conn,
                "SELECT row_hash FROM audit_log "
                "WHERE row_hash IS NOT NULL AND row_hash != '' "
                "ORDER BY id DESC LIMIT 1",
            )
        except sqlite3.Error as exc:
            logger.debug("heartbeat: audit chain column unavailable: %s", exc)
        rows = _scalar(conn, "SELECT COUNT(*) FROM audit_log")
        # Send refusals in their OWN try, for the same reason the chain read has
        # one: this query needs ``json_extract`` and a ``metadata`` column, and a
        # ledger too old for either must not take ``last_audit_ts`` down with it
        # and report a working seat as silent. Absence here means the seat cannot
        # answer, and the console holds — never a reassuring zero (ss#2547).
        refusals: SendRefusalFacts | None = None
        try:
            refusals = count_send_refusals(conn, datetime.now(timezone.utc))
        except (sqlite3.Error, ValueError) as exc:
            logger.debug("heartbeat: send-refusal read unavailable: %s", exc)
        return AuditLedgerFacts(
            last_audit,
            last_skill,
            head,
            rows if isinstance(rows, int) else None,
            refusals.count if refusals is not None else None,
            refusals.last_ts if refusals is not None else None,
            refusals.events if refusals is not None else None,
        )
    except sqlite3.Error:
        # DB exists but audit_log table not created yet, or a transient lock.
        return AuditLedgerFacts(None, None, None, None)
    finally:
        conn.close()


def read_audit_timestamps(db_path: str | None) -> tuple[str | None, str | None]:
    """(last_audit_ts, last_skill_ts). Kept as the pre-#2498 name for callers
    that want only the pair; :func:`read_audit_facts` is the full read."""
    facts = read_audit_facts(db_path)
    return (facts.last_audit_ts, facts.last_skill_ts)


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
    sticky_stop_reason: str | None = None,
    sticky_stop_condition: str | None = None,
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
    gateway_loop_ok: bool | None = None,
    gateway_loop_age_seconds: int | None = None,
    gateway_supervisor_state: str | None = None,
    gateway_restarts_last_hour: int | None = None,
    audit_write_failures: int | None = None,
    audit_head: str | None = None,
    audit_rows: int | None = None,
    send_refusals: int | None = None,
    send_refusals_last_ts: str | None = None,
    send_refusals_json: list[dict] | None = None,
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
    # The cause travels with the level or not at all: a reason paired with an
    # absent level would let the console render "why" beside a stale "what".
    if sticky_stop_level and sticky_stop_reason:
        payload["sticky_stop_reason"] = sticky_stop_reason
    if sticky_stop_level and sticky_stop_condition:
        payload["sticky_stop_condition"] = sticky_stop_condition
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
    # Gateway loop liveness + the part-1 supervisor's own state (ss-console#2488
    # part 2, shared/gateway_loop_check.py). Same is-not-None discipline, and
    # here it is the WHOLE design: `gateway_loop_age_seconds=0` and
    # `gateway_restarts_last_hour=0` are real values that must reach the wire,
    # while an absent age (the arming latch, a pin with no heartbeat, boot
    # suppression) must stay absent so the console HOLDS rather than resolving
    # an open wedge alert on a number nobody measured. `gateway_loop_ok=False`
    # means this check could not look -- our blindness, paged on its own,
    # never a verdict on the loop.
    if gateway_loop_ok is not None:
        payload["gateway_loop_ok"] = 1 if gateway_loop_ok else 0
    if gateway_loop_age_seconds is not None:
        payload["gateway_loop_age_seconds"] = gateway_loop_age_seconds
    if gateway_supervisor_state is not None:
        payload["gateway_supervisor_state"] = gateway_supervisor_state
    if gateway_restarts_last_hour is not None:
        payload["gateway_restarts_last_hour"] = gateway_restarts_last_hour
    # ss-console #2498: how many audit rows this seat has failed to persist,
    # ever (shared.audit_failure_counter). is-not-None discipline, and here it
    # is the WHOLE POINT: 0 is the value that says "the writer is up and has
    # lost nothing", which is the only thing that distinguishes a quiet ledger
    # from a broken one. Truthiness-omitting it would send exactly the healthy
    # case as silence — the shape this field exists to end. None means the seat
    # cannot answer (no .smd dir), and the console holds what it last knew.
    if audit_write_failures is not None:
        payload["audit_write_failures"] = audit_write_failures
    # ss-console #2500: the off-Machine chain pin. Read in the same pass as
    # last_audit_ts (read_audit_facts), so the head and the count always
    # describe one moment of the ledger. Both omitted on a ledger that has no
    # chained rows yet — absence is "nothing to pin", never "the chain broke".
    if audit_head:
        payload["audit_head"] = audit_head
    if audit_rows is not None:
        payload["audit_rows"] = audit_rows
    # ss-console#2547: how often a routine could not reach a human. Same
    # is-not-None discipline as the write-failure tally above, and here it is the
    # whole point twice over. 0 is what says "every routine that had something to
    # say said it" — the value that lets a repaired seat stop paging — and it is
    # exactly the value truthiness would drop, sending the healthy case as
    # silence to a console watching for silence. The TIMESTAMP is what the pager
    # keys on (a new event, never a nonzero level), so it must ride even on a
    # beat where the count has not moved; and the events must ride with it,
    # because a page that names neither the routine nor the reason sends someone
    # to the seat to find out what it was about.
    if send_refusals is not None:
        payload["send_refusals"] = send_refusals
    if send_refusals_last_ts:
        payload["send_refusals_last_ts"] = send_refusals_last_ts
    if send_refusals_json is not None:
        payload["send_refusals_json"] = send_refusals_json
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
        gateway_loop_check_fn=None,
        gateway_loop_check_debounce: int = 3,
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
        # Gateway loop liveness (shared.gateway_loop_check, ss-console#2488 pt 2).
        # The checker object holds the arming latch across ticks, so it is
        # constructed ONCE here and its bound method is the check_fn. Injectable
        # + debounced like the scheduler check: a crash reports ok=False after
        # the debounce, never silence -- silence is the defect class being closed.
        if gateway_loop_check_fn is None:
            from shared.gateway_loop_check import GatewayLoopChecker

            self._gateway_loop_checker = GatewayLoopChecker()
            gateway_loop_check_fn = self._default_gateway_loop_check
        self._gateway_loop_check_fn = gateway_loop_check_fn
        self._loop_debounce = max(1, gateway_loop_check_debounce)
        self._loop_fail_count = 0
        self._loop_last_good = None

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

    def _default_gateway_loop_check(self):
        return self._gateway_loop_checker.check(uptime_seconds=read_uptime_seconds())

    def _read_gateway_loop_check(self):
        """Run the gateway loop-liveness check with the standard debounce.

        A persistent crash reports ``GatewayLoopCheck(ok=False, ...)`` -- the
        boolean IS the failure state and the console pages
        ``gateway_loop_unprovable`` for it. Returns None only before the
        first-ever success (console holds on absence)."""
        from shared.gateway_loop_check import GatewayLoopCheck

        try:
            result = self._gateway_loop_check_fn()
        except Exception as exc:  # noqa: BLE001 -- the check must never kill the beat
            self._loop_fail_count += 1
            logger.warning(
                "heartbeat: gateway loop check failed (%d consecutive): %s",
                self._loop_fail_count,
                exc,
            )
            if self._loop_fail_count >= self._loop_debounce:
                return GatewayLoopCheck(
                    ok=False,
                    age_seconds=None,
                    supervisor_state=None,
                    restarts_last_hour=None,
                    reason=f"checker crashed: {exc.__class__.__name__}",
                )
            return self._loop_last_good
        self._loop_fail_count = 0
        self._loop_last_good = result
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
        ledger = read_audit_facts(self._audit_db_path_fn())
        # ss-console #2498. Read from the volume, not from this process: the
        # failures happen in the AGENT process and in cron pre_run children,
        # and this beat runs in the gate. Fail-soft like every other read here.
        audit_write_failures: int | None = None
        try:
            from shared.audit_failure_counter import read_audit_write_failures

            audit_write_failures = read_audit_write_failures()
        except Exception as exc:  # noqa: BLE001 — heartbeat stays fail-soft
            logger.debug("heartbeat: audit-write-failure tally read failed: %s", exc)
        # ADR 0062: surface the sticky-stop ladder level so the fleet view can
        # escalate a tripped seat, AND the cause that tripped it. Four meters
        # drive this ladder (tool failures, refusals, runtime, cost) and they
        # need four different investigations, so a level without its reason
        # sends the reader looking in the wrong place. Read-only; any failure
        # omits the fields (the receiver treats absence as unknown, never OK).
        level: str | None = None
        stop_reason: str | None = None
        stop_condition: str | None = None
        try:
            from shared.cost_breaker import read_stop_state

            stop_state = read_stop_state()
            level = stop_state.level
            stop_reason = stop_state.reason
            stop_condition = stop_state.condition
        except Exception as exc:  # noqa: BLE001 — heartbeat stays fail-soft
            logger.debug("heartbeat: sticky_stop state read failed: %s", exc)
        sched = self._read_scheduler_check()
        conn = self._read_connector_check()
        spec = self._read_spec_control_check()
        surface = self._read_webhook_surface_check()
        loop = self._read_gateway_loop_check()
        token_age: dict[str, int] | None = None
        try:
            from shared.connector_check import token_ages

            token_age = token_ages() or None
        except Exception as exc:  # noqa: BLE001 — heartbeat stays fail-soft
            logger.debug("heartbeat: token-age read failed: %s", exc)
        payload = build_payload(
            heartbeat_ts=_iso_utc_now(),
            last_audit_ts=ledger.last_audit_ts,
            last_skill_ts=ledger.last_skill_ts,
            uptime_seconds=read_uptime_seconds(),
            version=self._version,
            sticky_stop_level=level,
            sticky_stop_reason=stop_reason,
            sticky_stop_condition=stop_condition,
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
            gateway_loop_ok=loop.ok if loop is not None else None,
            gateway_loop_age_seconds=loop.age_seconds if loop is not None else None,
            gateway_supervisor_state=loop.supervisor_state if loop is not None else None,
            gateway_restarts_last_hour=loop.restarts_last_hour if loop is not None else None,
            audit_write_failures=audit_write_failures,
            audit_head=ledger.head,
            audit_rows=ledger.rows,
            send_refusals=ledger.send_refusals,
            send_refusals_last_ts=ledger.send_refusals_last_ts,
            send_refusals_json=ledger.send_refusals_json,
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
    try:
        loop_debounce = int(os.environ.get("GATEWAY_LOOP_CHECK_DEBOUNCE", "3"))
    except ValueError:
        loop_debounce = 3
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
        gateway_loop_check_debounce=loop_debounce,
    )


__all__ = [
    "AuditLedgerFacts",
    "HeartbeatEmitter",
    "build_payload",
    "emitter_from_env",
    "read_audit_facts",
    "read_audit_timestamps",
    "read_uptime_seconds",
]
