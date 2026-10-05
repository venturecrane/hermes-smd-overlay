#!/usr/bin/env python3
"""Run the heartbeat's shortfall query over a ledger copy, day by day.

WHY THIS EXISTS. ``shortfalls`` is a new fact about a seat, and a new fact with
no history is a claim. This script points the SAME function the ticker calls,
``shared.heartbeat.count_shortfalls``, at an arbitrary ``audit.db`` and prints
what it would have reported at the end of each day, per class, so the numbers
can be checked against days whose answer is already known from reading the
ledger by hand. It shares the query rather than restating it: a falsifier with
its own copy of the SQL measures the copy.

WHAT A PRE-v3 LEDGER CAN AND CANNOT SHOW. Rows written before outcome
semantics v3 (``outcome_semantics_version`` < 3) carry no ``shortfall``
outcome, no ``shortfall_code``, no ``bundle_*`` stamps, no ``skill_procedure``
and no ``object_digest``. On such rows:

* ``not_allowed`` is visible (``trust_decision = 'refuse'`` predates v3);
* ``limit`` / ``failed`` show only what v2 already scored ``error``; a refusal
  or an unreadable verdict that v2 scored ``ok`` is invisible, and a v2 error
  with no ``object_digest`` can only be cleared by a retry with no digest
  either;
* ``partial`` CANNOT appear at all: there is no bundle page count to compare
  against. The 2026-10-01 mail-routine run (52 pages read, none filed) is the
  case this class was built for, and on its own pre-v3 rows this script must
  print ``partial 0`` for that day. That zero is the instrument saying it had
  nothing to read, NOT that the run was whole; the report prints the v3 row
  count per day so the two are never confused.

Usage:

    python tests/tools/shortfalls_retro.py /path/to/audit.db \\
        --from 2026-09-28 --to 2026-10-04

    python tests/tools/shortfalls_retro.py /path/to/audit.db --day 2026-10-01 --events

Each row is the trailing-24h window ending 23:59:59 UTC on that date, the same
window the live field carries. ``--events`` prints every event the beat would
have carried (capped, oldest first), exactly as it would ride the heartbeat:
class, tool, routine, closed-vocabulary code, key. Never a document's words.

Read-only: the ledger is opened ``mode=ro``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.heartbeat import SHORTFALL_WINDOW_HOURS, count_shortfalls  # noqa: E402


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _v3_rows(conn: sqlite3.Connection, at: datetime) -> int:
    """Tool rows in the window written under outcome semantics v3 or later."""
    end = at.isoformat()[:19]
    start = (at - timedelta(hours=SHORTFALL_WINDOW_HOURS)).isoformat()[:19]
    row = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action_type = 'TOOL_CALL_COMPLETED'"
        " AND substr(ts,1,19) >= ? AND substr(ts,1,19) <= ?"
        " AND CAST(COALESCE(json_extract(metadata,'$.outcome_semantics_version'), 0)"
        " AS INTEGER) >= 3",
        (start, end),
    ).fetchone()
    return int(row[0]) if row else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", help="path to an audit.db (read-only)")
    parser.add_argument("--day", type=_parse_day, help="a single day (YYYY-MM-DD)")
    parser.add_argument("--from", dest="start", type=_parse_day, help="first day (YYYY-MM-DD)")
    parser.add_argument("--to", dest="end", type=_parse_day, help="last day (YYYY-MM-DD)")
    parser.add_argument("--events", action="store_true", help="print each day's events")
    args = parser.parse_args(argv)

    if args.day:
        days = [args.day]
    elif args.start and args.end:
        span = (args.end - args.start).days
        if span < 0:
            parser.error("--from must not be after --to")
        days = [args.start + timedelta(days=n) for n in range(span + 1)]
    else:
        parser.error("pass --day, or both --from and --to")

    path = Path(args.db)
    if not path.exists():
        print(f"no such ledger: {path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        print(f"# {path}  (trailing {SHORTFALL_WINDOW_HOURS}h window, ending each day 23:59:59Z)")
        print(
            f"{'day':<12}{'total':>6}{'not_allowed':>13}{'limit':>7}{'failed':>8}"
            f"{'partial':>9}{'v3_rows':>9}  last_ts"
        )
        for day in days:
            at = datetime.combine(day, datetime.max.time()).replace(
                microsecond=0, tzinfo=timezone.utc
            )
            facts = count_shortfalls(conn, at)
            print(
                f"{day.isoformat():<12}{facts.count:>6}{facts.not_allowed:>13}{facts.limit:>7}"
                f"{facts.failed:>8}{facts.partial:>9}{_v3_rows(conn, at):>9}"
                f"  {facts.last_ts or '-'}"
            )
            if args.events:
                for event in facts.events:
                    print(f"              {json.dumps(event, sort_keys=True)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
