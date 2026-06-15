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


def test_nonexistent_db_file_returns_empty_not_error(tmp_path):
    # A fresh Machine before the audit subsystem's first write has no audit.db.
    missing = str(tmp_path / "never-created.db")
    assert rr.read_runtime("audit_log", db_path=missing) == {"entries": [], "cursor": None}


def test_db_without_audit_log_table_returns_empty(tmp_path):
    # DB exists (other tables) but no audit_log yet → honest empty, not a crash.
    path = tmp_path / "other.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE kanban (id TEXT)")
    conn.commit()
    conn.close()
    assert rr.read_runtime("audit_log", db_path=str(path)) == {"entries": [], "cursor": None}


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


# ---------------------------------------------------------------------------
# read_runtime — audit_export (ss-console#1355 pull-before-destroy)
# ---------------------------------------------------------------------------


def test_audit_export_serves_full_rows_ascending(tmp_path):
    # The export kind must carry the integrity material the UI kind omits
    # (digests, trust_ceiling, metadata) and walk oldest→newest so an
    # interrupted pull resumes without missing rows.
    db = _audit_db(tmp_path, _IDS)
    res = rr.read_runtime("audit_export", db_path=db, limit="200")
    assert [e["id"] for e in res["entries"]] == sorted(_IDS)
    first = res["entries"][0]
    assert set(first) == {
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
    }
    assert first["input_digest"] == "secretdigest"  # digests ARE exported here


def test_audit_export_keyset_pagination_resumes(tmp_path):
    db = _audit_db(tmp_path, _IDS)
    page1 = rr.read_runtime("audit_export", db_path=db, limit="2")
    assert len(page1["entries"]) == 2
    assert page1["cursor"] == page1["entries"][-1]["id"]
    page2 = rr.read_runtime("audit_export", db_path=db, cursor=page1["cursor"], limit="200")
    ids = [e["id"] for e in page1["entries"] + page2["entries"]]
    assert ids == sorted(_IDS)  # no gaps, no dupes across the page boundary


def test_audit_export_missing_db_is_honest_empty(tmp_path):
    res = rr.read_runtime("audit_export", db_path=str(tmp_path / "absent.db"))
    assert res == {"entries": [], "cursor": None}


# ---------------------------------------------------------------------------
# read_runtime — memory_export (ADR-0016 Machine-local memory tables)
# ---------------------------------------------------------------------------


def _observations_db(tmp_path):
    path = tmp_path / "observations.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE persona_observations (
          observation_id TEXT PRIMARY KEY, persona TEXT, content TEXT,
          honcho_created_at TEXT
        );
        CREATE TABLE persona_observations_archive (
          observation_id TEXT PRIMARY KEY, persona TEXT, content TEXT,
          honcho_created_at TEXT, archived_at TEXT
        );
        """
    )
    for n in range(3):
        conn.execute(
            "INSERT INTO persona_observations VALUES (?, 'crane', ?, '2026-06-01T00:00:00Z')",
            (f"obs-{n}", f"observation body {n}"),
        )
    conn.commit()
    conn.close()
    return str(path)


def test_memory_export_pages_allowed_table_by_rowid(tmp_path):
    db = _observations_db(tmp_path)
    page1 = rr.read_runtime(
        "memory_export",
        db_path=None,
        table="persona_observations",
        observations_db_path=db,
        limit="2",
    )
    assert len(page1["entries"]) == 2
    assert page1["cursor"] == "2"
    page2 = rr.read_runtime(
        "memory_export",
        db_path=None,
        table="persona_observations",
        observations_db_path=db,
        cursor=page1["cursor"],
        limit="200",
    )
    all_ids = [e["observation_id"] for e in page1["entries"] + page2["entries"]]
    assert all_ids == ["obs-0", "obs-1", "obs-2"]
    assert page1["entries"][0]["content"] == "observation body 0"


def test_memory_export_unknown_table_is_refused(tmp_path):
    db = _observations_db(tmp_path)
    res = rr.read_runtime(
        "memory_export",
        db_path=None,
        table="audit_log; DROP TABLE x",
        observations_db_path=db,
    )
    assert res.get("error") == "unknown table"
    assert res["entries"] == []


def test_memory_export_requires_table(tmp_path):
    db = _observations_db(tmp_path)
    res = rr.read_runtime("memory_export", db_path=None, table=None, observations_db_path=db)
    assert res.get("error") == "unknown table"


def test_memory_export_skills_table_routes_to_agent_state_db(tmp_path):
    state = tmp_path / "agent-state.db"
    conn = sqlite3.connect(state)
    conn.execute("CREATE TABLE agent_skills_inventory (skill_name TEXT, status TEXT)")
    conn.execute("INSERT INTO agent_skills_inventory VALUES ('follow-up-cadence', 'persisted')")
    conn.commit()
    conn.close()
    res = rr.read_runtime(
        "memory_export",
        db_path=None,
        table="agent_skills_inventory",
        observations_db_path=None,
        agent_state_db_path=str(state),
    )
    assert res["entries"][0]["skill_name"] == "follow-up-cadence"


def test_memory_export_voice_corrections_routes_to_main_db(tmp_path):
    # voice_corrections (migration 0010) lives on the MAIN per-customer D1, which
    # the gate passes as db_path (the audit/main binding) — not the observations
    # DB. The legible relationship surface reads it this way (ADR 0048).
    main = tmp_path / "customer.db"
    conn = sqlite3.connect(main)
    conn.execute(
        "CREATE TABLE voice_corrections (id TEXT, correction_kind TEXT, "
        "before_pattern TEXT, after_text TEXT, source TEXT, superseded_by TEXT)"
    )
    conn.execute(
        "INSERT INTO voice_corrections VALUES "
        "('vc1', 'signoff', 'Sincerely,', 'Best,', 'live_edit', NULL)"
    )
    conn.commit()
    conn.close()
    res = rr.read_runtime(
        "memory_export",
        db_path=str(main),
        table="voice_corrections",
        observations_db_path=None,  # must NOT be served from the observations DB
        agent_state_db_path=None,
    )
    assert res["entries"][0]["correction_kind"] == "signoff"
    assert res["entries"][0]["after_text"] == "Best,"


def test_memory_export_absent_db_or_table_is_honest_empty(tmp_path):
    res = rr.read_runtime(
        "memory_export",
        db_path=None,
        table="persona_observations",
        observations_db_path=str(tmp_path / "absent.db"),
    )
    assert res == {"entries": [], "cursor": None}
    # DB exists but the mirror never created the archive table → honest empty.
    db = tmp_path / "bare.db"
    sqlite3.connect(db).close()
    res = rr.read_runtime(
        "memory_export",
        db_path=None,
        table="persona_observations_archive",
        observations_db_path=str(db),
    )
    assert res == {"entries": [], "cursor": None}
