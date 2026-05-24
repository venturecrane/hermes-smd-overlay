"""D1 table shapes for mirrored Honcho conclusions.

Per ADR 0016 (Honcho disposition — mirror, don't gate), the overlay
maintains a parallel record of Honcho conclusions in per-customer D1:

* ``persona_observations`` — live mirror of current Honcho conclusions.
  Every row carries provenance (source_message_ids, confidence,
  evidence_status, mirrored_at) so Captain can review, dismiss, or
  promote via the admin portal without standing between the agent and
  its working memory.

* ``persona_observations_archive`` — TTL'd rows aged out of Honcho by
  :mod:`archive`. Captain can restore from here back into the live
  Honcho store via the admin portal.

Both DDLs are idempotent (``CREATE TABLE IF NOT EXISTS``) so the
materialization step at Machine boot can run them unconditionally. The
schema versioning follows the SS migrations convention (a top-level
``schema_version`` integer column on the live table; archive rows
carry their schema_version snapshot at archive time).

Ported from ss-console/ai-employee/adapter/memory/state.py
``memory_source_state`` + ``memory_ingested_items`` shape. The original
"customer-owned memory artifact" model (ADR 0008, superseded) is
replaced by the Honcho mirror pattern (ADR 0016); the provenance
columns (``source_message_ids``, ``confidence``, ``evidence_status``)
are new under ADR 0016 and reflect the fact that Honcho is now the
authoritative live store rather than D1.
"""

# ---------------------------------------------------------------------------
# Evidence status — closed vocabulary
#
# Computed at mirror time by inspecting the source-message reference set
# returned by Honcho. See :func:`mirror.compute_evidence_status` for the
# classification rules.
# ---------------------------------------------------------------------------

EVIDENCE_STATUS_EVIDENCED = "evidenced"
EVIDENCE_STATUS_UNEVIDENCED = "unevidenced"
EVIDENCE_STATUS_INSUFFICIENT = "insufficient"

VALID_EVIDENCE_STATUSES = frozenset(
    {
        EVIDENCE_STATUS_EVIDENCED,
        EVIDENCE_STATUS_UNEVIDENCED,
        EVIDENCE_STATUS_INSUFFICIENT,
    }
)


# ---------------------------------------------------------------------------
# Live mirror table
# ---------------------------------------------------------------------------


PERSONA_OBSERVATIONS_DDL: str = """
CREATE TABLE IF NOT EXISTS persona_observations (
    -- Identity ---------------------------------------------------------
    observation_id          TEXT PRIMARY KEY,
    honcho_conclusion_id    TEXT NOT NULL UNIQUE,
    session_id              TEXT NOT NULL,
    persona_slug            TEXT,

    -- Payload ---------------------------------------------------------
    observation_type        TEXT NOT NULL,
    observation_body        TEXT NOT NULL,

    -- Provenance (ADR 0016) ------------------------------------------
    source_message_ids      TEXT NOT NULL,
    confidence              REAL,
    evidence_status         TEXT NOT NULL CHECK (
        evidence_status IN ('evidenced', 'unevidenced', 'insufficient')
    ),

    -- Lifecycle -------------------------------------------------------
    honcho_created_at       TEXT NOT NULL,
    mirrored_at             TEXT NOT NULL,
    schema_version          INTEGER NOT NULL DEFAULT 1,

    -- Captain workflow stamps ----------------------------------------
    dismissed_at            TEXT,
    dismissed_by            TEXT,
    dismissed_reason        TEXT,

    -- Constraints -----------------------------------------------------
    CHECK (length(source_message_ids) > 0),
    CHECK (length(observation_body) > 0)
);

CREATE INDEX IF NOT EXISTS persona_observations_session_idx
    ON persona_observations(session_id, mirrored_at);

CREATE INDEX IF NOT EXISTS persona_observations_evidence_idx
    ON persona_observations(evidence_status, mirrored_at);
""".strip()


# ---------------------------------------------------------------------------
# Archive table
# ---------------------------------------------------------------------------


PERSONA_OBSERVATIONS_ARCHIVE_DDL: str = """
CREATE TABLE IF NOT EXISTS persona_observations_archive (
    -- Same identity columns as the live table ------------------------
    observation_id          TEXT PRIMARY KEY,
    honcho_conclusion_id    TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    persona_slug            TEXT,

    -- Same payload + provenance --------------------------------------
    observation_type        TEXT NOT NULL,
    observation_body        TEXT NOT NULL,
    source_message_ids      TEXT NOT NULL,
    confidence              REAL,
    evidence_status         TEXT NOT NULL CHECK (
        evidence_status IN ('evidenced', 'unevidenced', 'insufficient')
    ),

    -- Same lifecycle timestamps + the archival timestamp -------------
    honcho_created_at       TEXT NOT NULL,
    mirrored_at             TEXT NOT NULL,
    archived_at             TEXT NOT NULL,
    archive_reason          TEXT NOT NULL DEFAULT 'ttl',
    schema_version          INTEGER NOT NULL DEFAULT 1,

    -- Same Captain workflow stamps (carried forward from live row) ---
    dismissed_at            TEXT,
    dismissed_by            TEXT,
    dismissed_reason        TEXT,

    -- Constraints -----------------------------------------------------
    CHECK (length(source_message_ids) > 0),
    CHECK (length(observation_body) > 0)
);

CREATE INDEX IF NOT EXISTS persona_observations_archive_session_idx
    ON persona_observations_archive(session_id, archived_at);

CREATE INDEX IF NOT EXISTS persona_observations_archive_archived_idx
    ON persona_observations_archive(archived_at);
""".strip()


# ---------------------------------------------------------------------------
# Convenience export — both DDLs in a stable order for materialization.
# ---------------------------------------------------------------------------


ALL_DDLS: tuple[str, ...] = (
    PERSONA_OBSERVATIONS_DDL,
    PERSONA_OBSERVATIONS_ARCHIVE_DDL,
)


__all__ = [
    "ALL_DDLS",
    "EVIDENCE_STATUS_EVIDENCED",
    "EVIDENCE_STATUS_INSUFFICIENT",
    "EVIDENCE_STATUS_UNEVIDENCED",
    "PERSONA_OBSERVATIONS_ARCHIVE_DDL",
    "PERSONA_OBSERVATIONS_DDL",
    "VALID_EVIDENCE_STATUSES",
]
