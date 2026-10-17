"""peer_preferences table — per-peer working-preference memory (ADR 0048 learned lane).

One row per captured preference. A peer (``peer_id`` = the stable per-person
sender id Hermes threads on ``pre_llm_call``) accumulates many rows over time;
the ACTIVE set for a peer is the rows with ``superseded_by IS NULL``. Recency
wins: re-stating an identical preference supersedes the prior copy rather than
piling duplicates (see :func:`store.record_preference`).

The table lives on the per-customer AGENT-STATE D1 binding
(``SMD_D1_AGENT_STATE_BINDING``, falling back to ``SMD_D1_AUDIT_BINDING``) —
the same hermes-writable file as ``agent_skills_inventory``, NOT the
broker-owned audit ledger and NOT the Honcho observations mirror. The plugin
creates it idempotently at register time (the Machine bootstrap does not run
per-customer migrations), mirroring the audit plugin's ensure-schema pattern.

Trust contract (ADR 0048): a row records a concrete preference plus optional
why / how-to-apply, sourced as either ``stated`` (the person said it) or
``demonstrated`` (observed concretely in how they work). Never a trait or
psychological label — there is deliberately no column for one.
"""

from __future__ import annotations

PEER_PREFERENCES_DDL: str = """
CREATE TABLE IF NOT EXISTS peer_preferences (
    id              TEXT PRIMARY KEY,
    customer_slug   TEXT NOT NULL,
    peer_id         TEXT NOT NULL,
    persona_slug    TEXT NOT NULL DEFAULT '',
    preference      TEXT NOT NULL,
    why             TEXT,
    how_to_apply    TEXT,
    source          TEXT NOT NULL DEFAULT 'stated'
        CHECK (source IN ('stated', 'demonstrated')),
    session_id      TEXT NOT NULL DEFAULT '',
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_by   TEXT,
    space           TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT 'preference',
    from_web        INTEGER NOT NULL DEFAULT 0
);
"""

# Privacy by space (ADR 0091, ss-console #3264). Added after the table shipped
# to seats: a database created before has neither column, and CREATE TABLE IF
# NOT EXISTS does not add one. ``store.ensure_schema`` adds each missing column
# with these statements, and that IS this table's migration (the plugin owns
# its schema; the Machine bootstrap runs no per-customer migrations). Every
# older row reads ``space = ''`` and ``kind = 'preference'``: a preference
# captured on a seat with no spaces, the only thing an older row ever was. A
# seat in space mode never injects a row whose space is '' (it is in no space).
#
# ``space``  ``person:<id>`` or ``shared`` on a space-mode seat; '' elsewhere.
# ``kind``   ``preference`` (record_peer_preference: how a person likes to work)
#            or ``memory`` (remember: a fact kept for the space it was said in).
PEER_PREFERENCES_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("space", "ALTER TABLE peer_preferences ADD COLUMN space TEXT NOT NULL DEFAULT ''"),
    ("kind", "ALTER TABLE peer_preferences ADD COLUMN kind TEXT NOT NULL DEFAULT 'preference'"),
    # ss-console #3283: 1 when the row was kept on a turn that had read outside
    # content (a web search); see shared.space_taint.
    ("from_web", "ALTER TABLE peer_preferences ADD COLUMN from_web INTEGER NOT NULL DEFAULT 0"),
)

#: The values ``kind`` may hold.
KINDS: frozenset[str] = frozenset({"preference", "memory"})

PEER_PREFERENCES_ACTIVE_INDEX_DDL: str = """
CREATE INDEX IF NOT EXISTS peer_preferences_active
    ON peer_preferences (peer_id, recorded_at)
    WHERE superseded_by IS NULL;
"""

PEER_PREFERENCES_BY_PERSONA_INDEX_DDL: str = """
CREATE INDEX IF NOT EXISTS peer_preferences_by_persona
    ON peer_preferences (persona_slug, peer_id)
    WHERE superseded_by IS NULL;
"""


PEER_PREFERENCES_BY_SPACE_INDEX_DDL: str = """
CREATE INDEX IF NOT EXISTS peer_preferences_by_space
    ON peer_preferences (space, kind, recorded_at)
    WHERE superseded_by IS NULL;
"""


#: Run by ``store.ensure_schema`` in this order: the table, then each added
#: column a pre-existing table lacks, then the indexes (the space index names a
#: column that exists only after the migration).
PEER_MEMORY_TABLE_DDLS: tuple[str, ...] = (PEER_PREFERENCES_DDL,)
PEER_MEMORY_INDEX_DDLS: tuple[str, ...] = (
    PEER_PREFERENCES_ACTIVE_INDEX_DDL,
    PEER_PREFERENCES_BY_PERSONA_INDEX_DDL,
    PEER_PREFERENCES_BY_SPACE_INDEX_DDL,
)
PEER_MEMORY_DDLS: tuple[str, ...] = (*PEER_MEMORY_TABLE_DDLS, *PEER_MEMORY_INDEX_DDLS)


__all__ = [
    "KINDS",
    "PEER_MEMORY_DDLS",
    "PEER_MEMORY_INDEX_DDLS",
    "PEER_MEMORY_TABLE_DDLS",
    "PEER_PREFERENCES_ADDED_COLUMNS",
    "PEER_PREFERENCES_BY_SPACE_INDEX_DDL",
    "PEER_PREFERENCES_ACTIVE_INDEX_DDL",
    "PEER_PREFERENCES_BY_PERSONA_INDEX_DDL",
    "PEER_PREFERENCES_DDL",
]
