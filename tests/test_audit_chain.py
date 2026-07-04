"""Tests for the audit-ledger hash chain (shared/audit_chain.py, #1686)."""

from __future__ import annotations

import pytest

from shared.audit_chain import (
    CHAIN_COLUMNS,
    GENESIS,
    canonical_row,
    compute_row_hash,
    legacy_anchor,
    verify_chain,
)
from shared.audit_contract import COLUMNS


def _values(i: int) -> list:
    return [
        f"01JROW{i:020d}",  # id
        f"2026-07-04T12:00:{i:02d}.000Z",  # ts
        "DRAFT_CREATED",  # action_type
        "agent",  # actor
        "agent",  # actor_role
        "matter-memo-on-update",  # skill_name
        None,  # matter_ref
        None,  # input_digest
        None,  # output_digest
        None,  # diff_digest
        "autonomous_internal_write",  # trust_ceiling
        None,  # metadata
    ]


def _chain(n: int, first_prev: str = GENESIS) -> list[dict]:
    rows = []
    prev = first_prev
    for i in range(n):
        vals = _values(i)
        rh = compute_row_hash(prev, vals)
        rows.append(
            {**dict(zip(CHAIN_COLUMNS, vals, strict=True)), "prev_hash": prev, "row_hash": rh}
        )
        prev = rh
    return rows


def test_chain_columns_match_the_contract():
    assert CHAIN_COLUMNS == COLUMNS


def test_canonical_row_distinguishes_null_from_empty():
    a = _values(0)
    b = _values(0)
    b[6] = ""  # matter_ref: None -> empty string
    assert canonical_row(a) != canonical_row(b)


def test_canonical_row_rejects_wrong_arity():
    with pytest.raises(ValueError):
        canonical_row(["only", "three", "values"])


def test_legacy_anchor_semantics():
    assert legacy_anchor(None) == GENESIS
    a = legacy_anchor("01JOLDROW")
    assert a != GENESIS and len(a) == 64
    assert a == legacy_anchor("01JOLDROW")  # deterministic


def test_verify_intact_chain():
    report = verify_chain(_chain(5))
    assert report["ok"] is True
    assert report["chained"] == 5
    assert report["breaks"] == []


def test_verify_is_order_independent():
    rows = _chain(6)
    shuffled = [rows[3], rows[0], rows[5], rows[1], rows[4], rows[2]]
    report = verify_chain(shuffled)
    assert report["ok"] is True
    assert report["chained"] == 6


def test_mutated_row_is_detected():
    rows = _chain(4)
    rows[2]["actor"] = "captain"  # tamper with content, keep stored hashes
    report = verify_chain(rows)
    assert report["ok"] is False
    assert any("mutated" in b["reason"] for b in report["breaks"])


def test_deleted_row_is_detected():
    rows = _chain(4)
    del rows[1]  # remove a middle link
    report = verify_chain(rows)
    assert report["ok"] is False
    # The orphaned suffix is unreachable / a second start appears.
    assert any("start" in b["reason"] or "unreachable" in b["reason"] for b in report["breaks"])


def test_deleting_the_tail_is_detected_via_head_comparison():
    # Removing the newest row leaves an internally-consistent chain — tail
    # truncation is caught by comparing the report head to an externally
    # pinned head (the evidence-packet flow records it). The report exposes
    # the head for exactly that comparison.
    rows = _chain(3)
    full_head = verify_chain(rows)["head"]
    truncated = verify_chain(rows[:-1])
    assert truncated["ok"] is True  # internally consistent...
    assert truncated["head"] != full_head  # ...but the pinned head exposes it


def test_legacy_rows_are_counted_not_failed():
    legacy = {
        **dict(zip(CHAIN_COLUMNS, _values(99), strict=True)),
        "prev_hash": None,
        "row_hash": None,
    }
    rows = [legacy, *_chain(2, first_prev=legacy_anchor(str(legacy["id"])))]
    report = verify_chain(rows)
    assert report["ok"] is True
    assert report["legacy"] == 1
    assert report["chained"] == 2


def test_fork_is_detected():
    rows = _chain(3)
    # Forge a second child of row 0's hash (same parent, different content).
    vals = _values(77)
    fork = {
        **dict(zip(CHAIN_COLUMNS, vals, strict=True)),
        "prev_hash": rows[0]["row_hash"],
        "row_hash": compute_row_hash(rows[0]["row_hash"], vals),
    }
    report = verify_chain([*rows, fork])
    assert report["ok"] is False
    assert any("fork" in b["reason"] for b in report["breaks"])
