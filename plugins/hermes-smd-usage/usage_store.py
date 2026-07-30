"""Per-person token meter (ss-console #2070 O4).

The cost plane knows what a SEAT spent (one Anthropic workspace per customer,
nightly, day-grained). It cannot say which person's work drove it — which is
exactly the question the sustained-dialogue program raises: if a firm's people
converse with the Operator all day instead of with claude.ai, whose usage is
that, and is the retainer still margin-safe?

This store answers it on-seat. One row per (UTC day, attributed person, model),
token buckets summed — aggregate grain so a chatty seat cannot grow the table
without bound. The console reads it live over the runtime-read seam
(``usage_export``) and renders it on the admin cost plane; nothing here is ever
shown to the client (cost is smd_only).

Attribution is best-effort by construction: a turn opened by an inbound email
attributes to that sender via the recorded origin; everything else (cron,
skills, delegated sub-agents, MCP) attributes to ``system:<platform>``. That
fallback is honest rather than clever — the alternative is guessing a person
onto scheduled work.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS usage_meter (
  day TEXT NOT NULL,
  attributed_to TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  attribution_source TEXT NOT NULL DEFAULT 'fallback',
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  requests INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (day, attributed_to, model)
)
"""

_UPSERT_SQL = """
INSERT INTO usage_meter (
  day, attributed_to, model, attribution_source,
  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
  reasoning_tokens, requests, updated_at
) VALUES (?,?,?,?,?,?,?,?,?,1,?)
ON CONFLICT(day, attributed_to, model) DO UPDATE SET
  input_tokens = input_tokens + excluded.input_tokens,
  output_tokens = output_tokens + excluded.output_tokens,
  cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
  cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
  reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
  requests = requests + 1,
  updated_at = excluded.updated_at,
  attribution_source = excluded.attribution_source
"""

_BUCKETS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _as_count(value: Any) -> int:
    """Non-negative int from a usage field; anything else is 0.

    Providers differ in which buckets they report, and a missing bucket must
    never poison the whole row.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


class UsageStore:
    """SQLite meter on the seat's agent-state db."""

    def __init__(self, path: str, *, clock: Callable[[], float] | None = None) -> None:
        self._path = path
        self._clock = clock or time.time
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def record(
        self,
        *,
        attributed_to: str,
        attribution_source: str,
        model: str,
        usage: dict[str, Any],
    ) -> None:
        """Fold one API request's usage into its (day, person, model) row."""
        now = self._clock()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        day = stamp[:10]
        counts = [_as_count(usage.get(bucket)) for bucket in _BUCKETS]
        conn = self._connect()
        conn.execute(
            _UPSERT_SQL,
            (day, attributed_to, model, attribution_source, *counts, stamp),
        )
        conn.commit()

    def rows(self) -> list[dict[str, Any]]:
        """All meter rows (tests + diagnostics; the console reads via runtime_read)."""
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        out = [
            dict(r) for r in conn.execute("SELECT * FROM usage_meter ORDER BY day, attributed_to")
        ]
        conn.row_factory = None
        return out


__all__ = ["UsageStore"]
