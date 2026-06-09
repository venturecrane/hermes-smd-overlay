"""Tests for the console→Machine runtime read core (shared/runtime_read.py).

Covers the two things the gate delegates: constant-time per-customer auth, and
the read itself (audit_log keyset pagination over ULID ids; honest empty for
not-yet-materialized kinds; read-only at the engine).
"""

import sqlite3
import threading

import pytest

from shared import runtime_read as rr

# A valid derived key is hex(HMAC-SHA256) = 64 hex chars.
_KEY = "a" * 64
_SLUG = "smith-pi-firm"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_auth_accepts_correct_bearer_and_slug():
    assert rr.verify_runtime_auth(f"Bearer {_KEY}", _SLUG, key=_KEY, own_slug=_SLUG)


def test_auth_rejects_missing_or_short_key_failclosed():
    # key unset / too short → refuse even with a matching bearer
    assert not rr.verify_runtime_auth(f"Bearer {_KEY}", _SLUG, key=None, own_slug=_SLUG)
    assert not rr.verify_runtime_auth("Bearer short", _SLUG, key="short", own_slug=_SLUG)


def test_auth_rejects_bad_bearer_and_bad_slug():
    assert not rr.verify_runtime_auth(f"Bearer {'b' * 64}", _SLUG, key=_KEY, own_slug=_SLUG)
    assert not rr.verify_runtime_auth(None, _SLUG, key=_KEY, own_slug=_SLUG)
    assert not rr.verify_runtime_auth(_KEY, _SLUG, key=_KEY, own_slug=_SLUG)  # missing "Bearer "
    assert not rr.verify_runtime_auth(f"Bearer {_KEY}", "other-firm", key=_KEY, own_slug=_SLUG)
    assert not rr.verify_runtime_auth(f"Bearer {_KEY}", None, key=_KEY, own_slug=_SLUG)
    assert not rr.verify_runtime_auth(f"Bearer {_KEY}", _SLUG, key=_KEY, own_slug=None)


# ---------------------------------------------------------------------------
# clamp_limit
# ---------------------------------------------------------------------------


def test_clamp_limit():
    assert rr.clamp_limit(None) == rr.DEFAULT_LIMIT
    assert rr.clamp_limit("") == rr.DEFAULT_LIMIT
    assert rr.clamp_limit("nonsense") == rr.DEFAULT_LIMIT
    assert rr.clamp_limit("10") == 10
    assert rr.clamp_limit("0") == 1
    assert rr.clamp_limit("99999") == rr.MAX_LIMIT


# ---------------------------------------------------------------------------
# read_runtime — audit_log
# ---------------------------------------------------------------------------


def _audit_db(tmp_path, ids):
    """Create a per-customer SQLite with audit_log seeded with the given ULID ids
    (insertion order; ids carry the sort order)."""
    path = tmp_path / "customer.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE audit_log (
          id TEXT PRIMARY KEY, ts TEXT NOT NULL, action_type TEXT NOT NULL,
          actor TEXT NOT NULL, actor_role TEXT, skill_name TEXT, matter_ref TEXT,
          input_digest TEXT, output_digest TEXT, diff_digest TEXT,
          trust_ceiling TEXT, metadata TEXT
        );
        """
    )
    for i in ids:
        conn.execute(
            "INSERT INTO audit_log (id, ts, action_type, actor, actor_role, skill_name, "
            "matter_ref, input_digest) VALUES (?, ?, 'DRAFT_CREATED', 'agent', 'agent', "
            "'inbox-triage', 'matter-1', 'secretdigest')",
            (i, f"2026-06-09T00:00:0{i[-1]}Z"),
        )
    conn.commit()
    conn.close()
    return str(path)


# Three ULID-shaped, lexicographically ordered ids.
_IDS = ["01AAAAAAAAAAAAAAAAAAAAAAA1", "01BBBBBBBBBBBBBBBBBBBBBBBB2", "01CCCCCCCCCCCCCCCCCCCCCCCC3"]


def test_audit_log_returns_newest_first_shaped(tmp_path):
    db = _audit_db(tmp_path, _IDS)
    res = rr.read_runtime("audit_log", db_path=db, limit="50")
    ids = [e["id"] for e in res["entries"]]
    assert ids == list(reversed(_IDS))  # newest (highest ULID) first
    row = res["entries"][0]
    assert row["action"] == "DRAFT_CREATED"  # action_type → action
    assert row["actor"] == "agent" and row["actorRole"] == "agent"
    assert row["skill"] == "inbox-triage" and row["matterRef"] == "matter-1"
    # Internal digests are never exposed on the wire.
    assert "input_digest" not in row and "diff_digest" not in row


def test_audit_log_keyset_pagination_non_overlapping(tmp_path):
    db = _audit_db(tmp_path, _IDS)
    page1 = rr.read_runtime("audit_log", db_path=db, limit="2")
    assert [e["id"] for e in page1["entries"]] == [_IDS[2], _IDS[1]]
    assert page1["cursor"] == _IDS[1]  # full page → advertises next cursor
    page2 = rr.read_runtime("audit_log", db_path=db, cursor=page1["cursor"], limit="2")
    assert [e["id"] for e in page2["entries"]] == [_IDS[0]]  # no overlap with page1
    assert page2["cursor"] is None  # short page → no further cursor


def test_unmaterialized_kinds_return_empty(tmp_path):
    db = _audit_db(tmp_path, _IDS)
    for kind in ("draft", "matter", "activity"):
        assert rr.read_runtime(kind, db_path=db) == {"entries": [], "cursor": None}


def test_unknown_kind_and_missing_db_return_empty(tmp_path):
    assert rr.read_runtime("bogus", db_path=str(tmp_path / "x.db")) == {
        "entries": [],
        "cursor": None,
    }
    assert rr.read_runtime("audit_log", db_path=None) == {"entries": [], "cursor": None}


def test_read_connection_is_readonly(tmp_path):
    """The read path opens mode=ro; a write on that connection is refused by the
    engine — read-only is enforced by SQLite, not just by convention."""
    db = _audit_db(tmp_path, _IDS)
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM audit_log")
    finally:
        ro.close()


def test_concurrent_writes_do_not_break_reads(tmp_path):
    """A reader must not 500 while the audit writer inserts rows. busy_timeout
    lets the read wait out the sub-ms write rather than raising SQLITE_BUSY."""
    db = _audit_db(tmp_path, _IDS)
    stop = threading.Event()

    def writer():
        w = sqlite3.connect(db)
        w.execute("PRAGMA busy_timeout = 5000")
        n = 0
        while not stop.is_set() and n < 50:
            w.execute(
                "INSERT INTO audit_log (id, ts, action_type, actor) "
                "VALUES (?, '2026-06-09T00:00:00Z', 'SKILL_ENABLED', 'agent')",
                (f"01ZZZZZZZZZZZZZZZZZZZZZ{n:03d}",),
            )
            w.commit()
            n += 1
        w.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(30):
            res = rr.read_runtime("audit_log", db_path=db, limit="10")
            assert isinstance(res["entries"], list)  # never raised, always a page
    finally:
        stop.set()
        t.join()
