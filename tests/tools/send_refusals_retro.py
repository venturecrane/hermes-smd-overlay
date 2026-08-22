#!/usr/bin/env python3
"""Run the heartbeat's send-refusal query over a ledger copy, day by day.

WHY THIS EXISTS. ``send_refusals`` is a new fact about a seat, and a new fact
with no history is a claim. This script points the SAME function the ticker
calls — ``shared.heartbeat.count_send_refusals`` — at an arbitrary ``audit.db``
and asks it about days whose answer is already known from reading the ledger by
hand. A query that reports zero on 2026-08-19 (five refusals in 26 seconds) or
on 2026-08-20 (five needs-you items, nothing sent) is wrong and must be widened
BEFORE it ships, not after a month of quiet dashboards.

It shares the query rather than restating it, deliberately. A retro-falsifier
with its own copy of the SQL measures the copy.

Usage:

    python tests/tools/send_refusals_retro.py /path/to/audit.db \\
        --from 2026-08-04 --to 2026-08-21

    python tests/tools/send_refusals_retro.py /path/to/audit.db --day 2026-08-19 --events

Each row is that day's count as the ticker would have reported it at 23:59:59
UTC on that date — i.e. a trailing-24h window ending at the day's end, which is
the same window shape the live field carries. ``--events`` prints the newest few
events for each day, exactly as they would ride the heartbeat.

Read-only: the ledger is opened ``mode=ro`` so running this against a live copy
cannot perturb the audit writer.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.heartbeat import SEND_REFUSAL_WINDOW_HOURS, count_send_refusals  # noqa: E402


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


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
        print(
            f"# {path}  (trailing {SEND_REFUSAL_WINDOW_HOURS}h window, ending each day 23:59:59Z)"
        )
        print(f"{'day':<12}{'count':>6}  last_ts")
        for day in days:
            at = datetime.combine(day, datetime.max.time()).replace(
                microsecond=0, tzinfo=timezone.utc
            )
            facts = count_send_refusals(conn, at)
            print(f"{day.isoformat():<12}{facts.count:>6}  {facts.last_ts or '-'}")
            if args.events:
                for event in facts.events:
                    print(f"              {json.dumps(event, sort_keys=True)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
