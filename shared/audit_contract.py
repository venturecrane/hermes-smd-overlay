"""The audit_log row contract — one source for SQL + column order.

Previously the ``INSERT INTO audit_log`` statement and its 12-value parameter
tuple were hand-copied into three plugins (hermes-smd-audit/emit.py,
hermes-smd-webhook-router/__init__.py, hermes-smd-trust/outbound.py). A column
reorder in one place silently corrupted the others. This module is the single
definition; all writers build their row through ``build_audit_params`` so the
positional tuple can never drift from ``INSERT_SQL``.

The canonical audit_log schema lives ss-console-side in
``docs/specs/operator/d1-schema.md``; ``COLUMNS`` here mirrors it and is
pinned by the schema-snapshot CI guard.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from shared.ids import iso_utc, sha256, ulid

# Column order is the contract. Keep in lockstep with d1-schema.md and the
# schema-snapshot test. The VALUES placeholder count is derived from this.
COLUMNS: tuple[str, ...] = (
    "id",
    "ts",
    "action_type",
    "actor",
    "actor_role",
    "skill_name",
    "matter_ref",
    "input_digest",
    "output_digest",
    "diff_digest",
    "trust_ceiling",
    "metadata",
)

INSERT_SQL = (
    "INSERT INTO audit_log (" + ", ".join(COLUMNS) + ") "
    "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")"
)

# Canonical CREATE for the per-customer audit_log, beside INSERT_SQL/COLUMNS as
# the single schema source. The Machine's bootstrap does NOT apply the
# ss-console operator/migrations, so the audit writer ensures the table exists on
# its own (ss-console#1285 — without this, writes hit "no such table" and
# audit_log is never created). IF NOT EXISTS keeps it idempotent and safe if a
# future bootstrap migration step lands. Column names are asserted equal to
# COLUMNS by the contract test so the two cannot drift. Mirrors
# ss-console operator/migrations/0001 (immutability is enforced at the Worker
# layer via D1Executor, not DB triggers — there are none to replicate).
CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS audit_log ("
    "id TEXT PRIMARY KEY, "
    "ts TEXT NOT NULL, "
    "action_type TEXT NOT NULL, "
    "actor TEXT NOT NULL, "
    "actor_role TEXT, "
    "skill_name TEXT, "
    "matter_ref TEXT, "
    "input_digest TEXT, "
    "output_digest TEXT, "
    "diff_digest TEXT, "
    "trust_ceiling TEXT, "
    "metadata TEXT, "
    "prev_hash TEXT, "
    "row_hash TEXT"
    ")"
)

# Hash-chain columns (#1686). Stamped by the capability broker at append time
# (the single RW holder, OP-P1-4) — writers never supply them, so COLUMNS /
# INSERT_SQL are unchanged. Pre-chain ledgers gain the columns via these
# ALTERs at ensure_schema time (each wrapped in try/except: "duplicate column"
# means already upgraded). Chain semantics live in shared/audit_chain.py (a
# tracked twin of the broker's chain.py).
CHAIN_COLUMN_ALTERS: tuple[str, ...] = (
    "ALTER TABLE audit_log ADD COLUMN prev_hash TEXT",
    "ALTER TABLE audit_log ADD COLUMN row_hash TEXT",
)

# The tool-call correlation key, as spelled inside the ``metadata`` blob.
#
# audit_log has no tool_call_id COLUMN (see COLUMNS above), so correlating one
# dispatch across emitters means json_extract(metadata, '$.tool_call_id') — and
# that only works if every emitter agrees on the spelling. Six audit-metadata
# writers already used this name; hermes-smd-audit's per-tool builder wrote the
# same value as ``trace_id``, so a single query silently missed one side or the
# other (ss-console #2312). Pinned here beside the column contract because this
# is the same class of drift COLUMNS exists to prevent.
#
# ``trace_id`` is NOT a synonym: the safety substrate documents it as an opaque
# request/turn id (ss-console operator/safety-substrate/trust_ceiling_log.py),
# and a turn contains many tool calls. It survives on the per-tool path as a
# deprecated alias only so queries still reach rows written before the fix.
CANONICAL_TOOL_CALL_KEY = "tool_call_id"
DEPRECATED_TOOL_CALL_KEY = "trace_id"

# The JOIN keys, as spelled inside the ``metadata`` blob (ss-console#2497).
#
# audit_log has twelve columns and none of them names a person, a message, or a
# tool's output object — so on the live ashton-price ledger (1,473 rows, 2026-08-21,
# vfy_01M0H8DR6JAPYVHFMNJZXQZ517) an INBOUND_RECEIVED row carried a random
# ``item_id`` and no sender at all, and a REPLY_SENT row carried neither the
# session nor the matter. Reconstructing "who caused this, answering what, about
# which matter" was a join by TIMESTAMP ADJACENCY, which is not a join.
#
# These names are the join, so they are pinned here beside COLUMNS for exactly
# the reason CANONICAL_TOOL_CALL_KEY is: six writers spread across two repos have
# to spell them identically or a query silently reaches half the rows.
#
#   sender_key          sha256 of the CANONICAL address that sent an inbound
#                       message (:func:`sender_key`). Never the address itself —
#                       an export leaves the Machine.
#   vendor_message_id   the provider's own id for that message (AgentMail message
#                       id; the Graph message id the connector normalized).
#   session_id          the agent session the row belongs to. Already the spelling
#                       used by the per-tool and fabrication-gate writers.
#   matter_ref          the matter the action concerned. This one ALSO has a
#                       column, and the column is where it belongs — the metadata
#                       spelling exists only so a consumer reading the blob finds
#                       the same name.
#   document_id / memo_id / draft_id
#                       the object a read/write tool touched or created.
#   written_body_sha256 sha256 of the body a write tool actually wrote.
JOIN_KEYS: tuple[str, ...] = (
    "sender_key",
    "vendor_message_id",
    "session_id",
    "matter_ref",
    "document_id",
    "memo_id",
    "draft_id",
    "written_body_sha256",
)

CREATE_INDEX_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action_type ON audit_log(action_type, ts)",
    "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor, ts)",
)


def canonical_address(address: str) -> str:
    """The ONE canonical form an address is reduced to before it is hashed.

    ``unicodedata.normalize("NFC", …).strip().lower()`` — a deliberate twin of
    ss-console ``operator/workspace_broker/recipient_policy.py:48``
    (``canonicalize``) and of ``shared/recipient_classifier.py``'s
    ``_canonicalize_roster_entry``. All three must agree, because a hash is only
    a join key if both ends reduce the same human to the same bytes.

    NFC is the load-bearing part, for the same reason it is at the fence: ``é``
    has two valid encodings, they are one character to every mail system, and
    ``.lower()`` alone leaves them unequal — so the same person would produce two
    different ``sender_key`` values and the ledger would report two people.

    NOT ``casefold()``: it maps ``ß`` to ``ss``, which would collide two
    different mailboxes into one key. Under a fence a collision widens the
    allow set; under an audit key it merges two people's actions into one
    identity, which is the worse failure of the two.
    """
    return unicodedata.normalize("NFC", address).strip().lower()


def sender_key(address: object) -> str | None:
    """Hex sha256 of :func:`canonical_address`, or ``None`` when there is none.

    The audit row's answer to "which person", and the reason it is a HASH: an
    audit export is a file that leaves the Machine, and a ledger of raw client
    addresses is a disclosure surface the record does not need. A firm holding
    the address can reproduce the key and find every row that person caused; a
    reader who does not already know the address learns nothing from it.

    A rostered display name may ride alongside as a LABEL. It is never the
    identity — a display name is attacker-controlled text in every mail system.
    """
    if not isinstance(address, str):
        return None
    canonical = canonical_address(address)
    if not canonical:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dumps(metadata: dict | None) -> str | None:
    """Deterministic metadata serialization (sorted keys, no whitespace).

    Returns ``None`` for empty/absent metadata so the column stores SQL NULL.
    """
    if not metadata:
        return None
    return json.dumps(metadata, sort_keys=True, separators=(",", ":"))


def build_audit_params(
    *,
    row_id: str,
    ts: str,
    action_type: str,
    actor: str | None = None,
    actor_role: str | None = None,
    skill_name: str | None = None,
    matter_ref: str | None = None,
    input_digest: str | None = None,
    output_digest: str | None = None,
    diff_digest: str | None = None,
    trust_ceiling: str | None = None,
    metadata: dict | None = None,
) -> list[Any]:
    """Build the positional parameter list for ``INSERT_SQL``.

    Keyword-only by design: callers name every column, so a future column
    insertion is a compile-time-visible change at each call site rather than a
    silently-misaligned positional tuple. ``metadata`` is serialized here.
    """
    return [
        row_id,
        ts,
        action_type,
        actor,
        actor_role,
        skill_name,
        matter_ref,
        input_digest,
        output_digest,
        diff_digest,
        trust_ceiling,
        _dumps(metadata),
    ]


# Actor-role literal for agent-authored event rows. The canonical ActorRole
# enum lives in hermes-smd-audit/schemas.py (the plugin layer); shared/ is the
# lower layer and must not import upward, so the agent literal is pinned here
# and asserted equal to ActorRole.AGENT.value by the audit plugin's tests.
ACTOR_AGENT = "agent"


def agent_event_params(
    *,
    action_type: str,
    metadata: dict | None = None,
    skill_name: str | None = None,
    session_id: str | None = None,
    matter_ref: str | None = None,
    now_ms: int | None = None,
    now=None,
) -> list[Any]:
    """Convenience builder for an agent-actor event row (the webhook-router /
    outbound-gate shape): fresh ULID + ISO-Z timestamp, ``actor="agent"`` /
    ``actor_role="agent"``, all digest columns NULL. ``now_ms``/``now`` are
    injectable for deterministic tests.

    ``session_id`` and ``matter_ref`` are ss-console#2497. Every row this builder
    wrote carried neither: measured on the live A&P ledger, ``session_id`` was
    absent from 0 of 8 REPLY_SENT rows and ``matter_ref`` from every send row on
    both seats, because this function had no argument for either. A reply that
    cannot name the matter it was about is not an audit record of a reply.

    They land in different places, on purpose:

    * ``matter_ref`` goes to the **COLUMN**. It has had one since the first
      migration; leaving the value in metadata would keep it out of the only
      field the portal record filters and indexes on
      (``src/lib/portal/operator/object-audit-record.ts``).
    * ``session_id`` goes to **metadata**, because there is no column for it and
      adding one would be a schema change. ``session_id`` is already its
      spelling on the per-tool and fabrication-gate rows, so one query reaches
      all of them.

    An explicit ``session_id`` wins over one a caller also put in ``metadata`` —
    the argument is the newer, deliberate channel. Both are omitted when falsy:
    an absent join key must read as absent, never as an empty string, which the
    chain canonicalizes distinctly from NULL.

    Canonicalization is UNTOUCHED. ``metadata`` is already one of the twelve
    hashed columns (``shared/audit_chain.py``), so new keys change the canonical
    body of NEW rows only — every existing row keeps verifying against its own
    stored values.
    """
    if session_id:
        metadata = {**(metadata or {}), "session_id": session_id}
    return build_audit_params(
        row_id=ulid(now_ms=now_ms),
        ts=iso_utc(now),
        action_type=action_type,
        actor=ACTOR_AGENT,
        actor_role=ACTOR_AGENT,
        skill_name=skill_name,
        matter_ref=matter_ref or None,
        metadata=metadata,
    )


__all__ = [
    "COLUMNS",
    "INSERT_SQL",
    "CREATE_TABLE_SQL",
    "CHAIN_COLUMN_ALTERS",
    "CREATE_INDEX_SQL",
    "CANONICAL_TOOL_CALL_KEY",
    "DEPRECATED_TOOL_CALL_KEY",
    "JOIN_KEYS",
    "ACTOR_AGENT",
    "build_audit_params",
    "agent_event_params",
    "canonical_address",
    "sender_key",
    "sha256",
]
