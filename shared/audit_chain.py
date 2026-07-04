"""Audit-ledger hash chain — canonicalization, linking, verification (#1686).

Per-row payload hashing made a mutated row detectable; it did nothing about a
DELETED row. This module upgrades the ledger to a hash chain: every row's
``row_hash`` commits to its full canonical content AND to the ``row_hash`` of
the row before it, so removing (or reordering, or inserting) a row breaks the
chain at a verifiable point. The write side runs ONLY in the capability
broker's ``LedgerWriter.append`` — the single process holding the ledger's RW
handle (OP-P1-4) — which is what makes the chain trustworthy: there is exactly
one serialization point, and the agent uid cannot reach it.

TWIN CONTRACT: this file exists byte-identically in two repos —
``hermes-smd-overlay/shared/audit_chain.py`` (the Machine side: export /
verification) and ``ss-console/operator/workspace_broker/chain.py`` (the
broker side: writing). The pair is tracked in ss-console
``operator/contracts/overlay-pairs.json`` (SEC-32); a one-sided edit fails CI.
Stdlib only, no repo-local imports, so the bytes can match exactly.

Chain semantics:

* ``row_hash = sha256(prev_hash || 0x1e || canonical_row(values))`` where
  ``values`` are the 12 contract columns (id..metadata) in contract order.
* The first chained row's ``prev_hash`` is ``GENESIS`` on a fresh ledger, or
  ``legacy_anchor(<id of the last pre-chain row>)`` when the ledger predates
  the upgrade — anchoring the chain to the legacy tail so deleting legacy
  rows after the upgrade is also detectable.
* Rows written before the upgrade have NULL ``row_hash``/``prev_hash`` and are
  outside the chain by construction; the verifier reports them as ``legacy``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

GENESIS = "GENESIS"

#: The 12 contract columns a row is canonicalized over, in contract order.
#: Mirrors audit_contract.COLUMNS / the broker's _ALL_COLUMNS; asserted equal
#: by tests on both sides so the three cannot drift.
CHAIN_COLUMNS: tuple[str, ...] = (
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

_FIELD_SEP = "\x1f"  # unit separator between canonical fields
_LINK_SEP = "\x1e"  # record separator between prev_hash and canonical body
_NONE = "\x00"  # SQL NULL marker (cannot collide with real text fields)


def canonical_row(values: Sequence[Any]) -> str:
    """Deterministic serialization of the 12 contract values.

    ``None`` becomes a NUL marker (distinct from empty string); everything
    else is ``str()``-ed. Length is asserted so a column-count drift between
    the contract and a caller fails loudly, never silently truncates.
    """
    if len(values) != len(CHAIN_COLUMNS):
        raise ValueError(f"canonical_row: expected {len(CHAIN_COLUMNS)} values, got {len(values)}")
    return _FIELD_SEP.join(_NONE if v is None else str(v) for v in values)


def compute_row_hash(prev_hash: str, values: Sequence[Any]) -> str:
    """The chain link: sha256 over the parent link + this row's canonical body."""
    if not prev_hash:
        raise ValueError("compute_row_hash: prev_hash must be non-empty")
    payload = prev_hash + _LINK_SEP + canonical_row(values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def legacy_anchor(last_unchained_id: str | None) -> str:
    """The ``prev_hash`` for the FIRST chained row.

    ``GENESIS`` on an empty ledger; otherwise a hash committing to the id of
    the last pre-upgrade row, so the upgrade point is pinned to the legacy
    tail that existed when chaining began.
    """
    if last_unchained_id is None:
        return GENESIS
    return hashlib.sha256(f"LEGACY:{last_unchained_id}".encode()).hexdigest()


def verify_chain(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Verify a full ledger export. Order-independent: the chain is
    reconstructed by following ``prev_hash`` links, not by trusting the input
    ordering (ULIDs from a single writer are time-ordered per millisecond but
    not within one, so link-following is the only exact order).

    Each row mapping must carry the 12 ``CHAIN_COLUMNS`` plus ``prev_hash``
    and ``row_hash`` (NULL/absent on legacy rows).

    Returns a report dict::

        {
          "ok": bool,          # every chained row links and hashes correctly
          "chained": int,      # rows participating in the chain
          "legacy": int,       # pre-upgrade rows (NULL row_hash)
          "head": str | None,  # row_hash of the chain tip
          "breaks": [ {"id", "reason"}, ... ],
        }

    Detection coverage: a MUTATED chained row fails its own recomputation; a
    DELETED chained row leaves its successor's ``prev_hash`` dangling (no such
    parent); an INSERTED row cannot produce a valid link without the broker's
    write path; a FORKED chain (two rows naming the same parent) is reported.
    """
    chained: dict[str, Mapping[str, Any]] = {}
    legacy = 0
    breaks: list[dict[str, str]] = []

    by_prev: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        row_hash = row.get("row_hash")
        prev_hash = row.get("prev_hash")
        if row_hash is None and prev_hash is None:
            legacy += 1
            continue
        if row_hash is None or prev_hash is None:
            breaks.append(
                {
                    "id": str(row.get("id")),
                    "reason": "half-chained row (one of prev/row hash is NULL)",
                }
            )
            continue
        recomputed = compute_row_hash(prev_hash, [row.get(c) for c in CHAIN_COLUMNS])
        if recomputed != row_hash:
            breaks.append({"id": str(row.get("id")), "reason": "row_hash mismatch (row mutated)"})
            continue
        chained[row_hash] = row
        by_prev.setdefault(prev_hash, []).append(row)

    # Chain start: prev is GENESIS or a LEGACY anchor (a hash that is not any
    # chained row's row_hash). Walk forward following links.
    starts = [r for p, rs in by_prev.items() if p == GENESIS or p not in chained for r in rs]
    if len(chained) > 0 and len(starts) == 0:
        breaks.append({"id": "-", "reason": "no chain start found (anchor row missing)"})
    if len(starts) > 1:
        for r in starts[1:]:
            breaks.append(
                {"id": str(r.get("id")), "reason": "multiple chain starts (fork or deleted parent)"}
            )

    head: str | None = None
    visited = 0
    if starts:
        cursor: Mapping[str, Any] | None = starts[0]
        while cursor is not None:
            visited += 1
            head = str(cursor["row_hash"])
            children = by_prev.get(head, [])
            if len(children) > 1:
                for extra in children[1:]:
                    breaks.append(
                        {"id": str(extra.get("id")), "reason": "fork (two rows share a parent)"}
                    )
            cursor = children[0] if children else None

    unreached = len(chained) - visited
    if unreached > 0:
        breaks.append(
            {
                "id": "-",
                "reason": f"{unreached} chained row(s) unreachable from the start (deleted parent)",
            }
        )

    return {
        "ok": len(breaks) == 0,
        "chained": len(chained),
        "legacy": legacy,
        "head": head,
        "breaks": breaks,
    }


__all__ = [
    "GENESIS",
    "CHAIN_COLUMNS",
    "canonical_row",
    "compute_row_hash",
    "legacy_anchor",
    "verify_chain",
]
