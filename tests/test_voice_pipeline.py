"""Tests for the voice sample ingestion pipeline.

Ported from ss-console/ai-employee/adapter/tests/test_voice_pipeline.py.

Covers:

* Structural-diff extraction is deterministic, body-free, and labels
  greetings/signoffs/cohorts correctly.
* Partner-authored filter excludes adapter-tagged drafts, audit-log
  digest matches, and shape-heuristic hits.
* Runner: scheduled + on-demand modes share the same write path.
* Runner: each ingested message writes to R2 at the right key, inserts a
  provenance row, and upserts the state row's cohort histogram.
* Filtered messages get a provenance row with ``partner_authored=0``,
  no R2 object, and the reason persisted for dashboard drill-down.
* Deduplication: re-running over the same cursor does not re-write.
* Retention enforcer deletes R2 objects older than the configured
  window and soft-deletes provenance rows.
* Decommission hook removes every R2 object for one source.
* Privacy: the raw body never lands in the structural-diff JSON or in
  any D1 column.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest


def load_plugin(plugin_name: str):
    """Load the plugin package so submodule imports inside ``__init__.py`` resolve."""
    root = Path(__file__).parent.parent
    init_path = root / "plugins" / plugin_name / "__init__.py"
    sanitized = plugin_name.replace("-", "_")
    mod_name = f"plugin_{sanitized}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, init_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin spec for {plugin_name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


load_plugin("hermes-smd-voice")
import plugin_hermes_smd_voice.diff as _diff_mod  # noqa: E402
import plugin_hermes_smd_voice.filter as _filter_mod  # noqa: E402
import plugin_hermes_smd_voice.pipeline as _pipeline_mod  # noqa: E402
import plugin_hermes_smd_voice.state as _state_mod  # noqa: E402

GreetingStyle = _diff_mod.GreetingStyle
SignoffStyle = _diff_mod.SignoffStyle
extract_structural_diff = _diff_mod.extract_structural_diff
structural_diff_digest = _diff_mod.structural_diff_digest

ACCEPT_REASON = _filter_mod.ACCEPT_REASON
CandidateMessage = _filter_mod.CandidateMessage
PartnerAuthoredFilter = _filter_mod.PartnerAuthoredFilter
REASON_ADAPTER_AGENT_DRAFTED = _filter_mod.REASON_ADAPTER_AGENT_DRAFTED
REASON_AUDIT_LOG_DIGEST_MATCH = _filter_mod.REASON_AUDIT_LOG_DIGEST_MATCH
REASON_EMPTY_BODY = _filter_mod.REASON_EMPTY_BODY
REASON_SHAPE_HEURISTIC = _filter_mod.REASON_SHAPE_HEURISTIC
REASON_TOO_SHORT = _filter_mod.REASON_TOO_SHORT
compute_body_digest = _filter_mod.compute_body_digest

COHORT_UNASSIGNED = _state_mod.COHORT_UNASSIGNED
INGEST_STATUS_ERROR = _state_mod.INGEST_STATUS_ERROR
INGEST_STATUS_OK = _state_mod.INGEST_STATUS_OK
IngestionItemRecord = _state_mod.IngestionItemRecord
IngestionStateUpdate = _state_mod.IngestionStateUpdate
VoiceSourceStateStore = _state_mod.VoiceSourceStateStore

IngestionMode = _pipeline_mod.IngestionMode
NoEmailSource = _pipeline_mod.NoEmailSource
SentMessage = _pipeline_mod.SentMessage
StaticCohortResolver = _pipeline_mod.StaticCohortResolver
VoiceIngestionRunner = _pipeline_mod.VoiceIngestionRunner
decommission_source = _pipeline_mod.decommission_source
enforce_retention = _pipeline_mod.enforce_retention


# ---------------------------------------------------------------------------
# Schema: in-test copy of the migration so the tests do not shell out.
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE voice_source_state (
  source_kind             TEXT NOT NULL,
  source_id               TEXT NOT NULL,
  last_ingestion_at       TEXT NOT NULL,
  last_success_at         TEXT,
  last_error              TEXT,
  ingest_status           TEXT NOT NULL,
  items_last_run          INTEGER NOT NULL DEFAULT 0,
  samples_by_cohort_json  TEXT,
  schema_version          INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (source_kind, source_id)
);

CREATE TABLE voice_ingestion_items (
  id                       TEXT PRIMARY KEY,
  source_kind              TEXT NOT NULL,
  source_id                TEXT NOT NULL,
  source_message_digest    TEXT NOT NULL,
  recipient_cohort_id      TEXT NOT NULL,
  partner_authored         INTEGER NOT NULL,
  filter_reason            TEXT,
  ingested_at              TEXT NOT NULL,
  sent_at                  TEXT NOT NULL,
  r2_key                   TEXT,
  structural_diff_digest   TEXT,
  word_count               INTEGER,
  schema_version           INTEGER NOT NULL DEFAULT 1,
  deleted_at               TEXT
);

CREATE INDEX idx_voice_items_source
  ON voice_ingestion_items(source_kind, source_id, deleted_at);

CREATE UNIQUE INDEX idx_voice_items_dedupe
  ON voice_ingestion_items(source_kind, source_id, source_message_digest)
  WHERE deleted_at IS NULL;
"""


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class SqliteWriteExecutor:
    """Write executor."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: list) -> None:
        cur = self._conn.cursor()
        cur.execute(sql, params)
        self._conn.commit()


class SqliteQueryExecutor:
    """Query executor returning ``list[dict]``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def query(self, sql: str, params: list) -> list[dict]:
        cur = self._conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


class FakeR2Client:
    """In-memory R2 client. Records every put + delete for assertions."""

    def __init__(self, customer_slug: str) -> None:
        self.customer_slug = customer_slug
        self.objects: dict[str, bytes] = {}
        self.deletes: list[str] = []
        self.fail_next_put: bool = False

    async def put(self, key: str, body: bytes, content_type: str) -> None:  # noqa: ARG002
        if self.fail_next_put:
            self.fail_next_put = False
            raise RuntimeError("simulated R2 outage")
        self.objects[key] = body

    async def delete(self, key: str) -> None:
        self.deletes.append(key)
        self.objects.pop(key, None)


class FakeCursorStore:
    def __init__(self, initial: str | None = None) -> None:
        self.value = initial
        self.history: list[str | None] = []

    async def get(self) -> str | None:
        return self.value

    async def set(self, cursor: str | None) -> None:
        self.value = cursor
        self.history.append(cursor)


class FakeAuditLookup:
    def __init__(self, matching: set[str] | None = None) -> None:
        self.matching = matching or set()

    async def has_draft_with_digest(self, digest: str) -> bool:
        return digest in self.matching


class FakeEmailSource:
    def __init__(self, messages: list[SentMessage], next_cursor: str | None) -> None:
        self.source_id = "test-adapter"
        self._messages = messages
        self._next_cursor = next_cursor
        self.calls = 0

    async def list_sent_since(self, cursor: str | None):  # noqa: ARG002
        self.calls += 1
        return self._messages, self._next_cursor


class ErroringEmailSource:
    source_id = "test-adapter"

    async def list_sent_since(self, cursor):  # noqa: ARG002
        raise RuntimeError("connector exploded")


class DictCohortResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    async def resolve(self, recipient_email: str) -> str | None:
        return self.mapping.get(recipient_email.lower())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(_SCHEMA_SQL)
    return c


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _store(conn: sqlite3.Connection) -> VoiceSourceStateStore:
    return VoiceSourceStateStore(SqliteWriteExecutor(conn), SqliteQueryExecutor(conn))


def _make_runner(
    *,
    messages: list[SentMessage] | None = None,
    cohort_mapping: dict[str, str] | None = None,
    audit_matches: set[str] | None = None,
    cursor: str | None = None,
    next_cursor: str | None = "cursor-2",
    customer_slug: str = "demo-firm",
):
    conn = _conn()
    source = FakeEmailSource(messages or [], next_cursor)
    resolver = (
        DictCohortResolver(cohort_mapping)
        if cohort_mapping is not None
        else StaticCohortResolver()
    )
    r2 = FakeR2Client(customer_slug)
    cursor_store = FakeCursorStore(cursor)
    audit_lookup = FakeAuditLookup(audit_matches)
    runner = VoiceIngestionRunner(
        source=source,
        cohort_resolver=resolver,
        r2_client=r2,
        state_store=_store(conn),
        cursor_store=cursor_store,
        audit_lookup=audit_lookup,
    )
    return runner, conn, r2, cursor_store, source


def _mk_msg(
    *,
    id_: str = "msg-1",
    body: str = "Hi Sarah,\n\nLet's plan to meet next week to discuss the matter.\n\nBest,\nMarcus",
    subject: str = "Meeting next week",
    recipients=("sarah@example.com",),
    likely_agent_drafted: bool | None = False,
    sent_at: str = "2026-05-21T12:00:00.000Z",
) -> SentMessage:
    return SentMessage(
        message_id=id_,
        sent_at=sent_at,
        body_text=body,
        subject=subject,
        recipients=list(recipients),
        likely_agent_drafted=likely_agent_drafted,
    )


# ---------------------------------------------------------------------------
# Structural diff
# ---------------------------------------------------------------------------


def test_structural_diff_is_deterministic():
    body = "Hi Sarah,\n\nThanks for the update. We will respond Monday.\n\nBest,\nMarcus"
    a = extract_structural_diff(body_text=body, subject="Re: matter", recipient_cohort="client")
    b = extract_structural_diff(body_text=body, subject="Re: matter", recipient_cohort="client")
    assert a.as_dict() == b.as_dict()
    assert structural_diff_digest(a) == structural_diff_digest(b)


def test_structural_diff_drops_raw_body():
    body = "Hi Sarah,\n\nThe deposition for Smith vs. Jones is set for Tuesday.\n\nBest,\nMarcus"
    diff = extract_structural_diff(body_text=body, subject="depo", recipient_cohort="client")
    blob = diff.to_json_bytes()
    decoded = blob.decode("utf-8")
    # No content tokens survive.
    assert "Sarah" not in decoded
    assert "Smith" not in decoded
    assert "Jones" not in decoded
    assert "deposition" not in decoded
    assert "Marcus" not in decoded


def test_structural_diff_word_and_sentence_counts():
    body = "We will call you tomorrow. The discovery deadline moved. Plan for Monday."
    diff = extract_structural_diff(body_text=body, subject="", recipient_cohort="client")
    assert diff.sentence_count == 3
    # 5 + 4 + 3 words across the three sentences.
    assert diff.word_count == 12
    assert diff.avg_sentence_length == pytest.approx(12 / 3, rel=1e-2)


def test_structural_diff_classifies_greetings():
    cases = {
        "Dear Mr. Smith,\n\nLet's discuss the matter.\n\nBest,\nMarcus": GreetingStyle.FORMAL_NAMED,
        "Hi Sarah,\n\nLet's discuss the matter.\n\nBest,\nMarcus": GreetingStyle.FIRST_NAME,
        "Hi Mr. Smith,\n\nLet's discuss the matter.\n\nBest,\nMarcus": GreetingStyle.SEMI_FORMAL,
        "Team,\n\nLet's discuss the matter.\n\nBest,\nMarcus": GreetingStyle.GROUP,
        "Hello,\n\nLet's discuss the matter.\n\nBest,\nMarcus": GreetingStyle.BARE_HI,
    }
    for body, expected in cases.items():
        diff = extract_structural_diff(body_text=body, subject="", recipient_cohort="client")
        assert diff.greeting_style == expected.value


def test_structural_diff_classifies_signoffs():
    body = "Hi Sarah,\n\nLet's plan to meet.\n\nThanks,\nMarcus"
    diff = extract_structural_diff(body_text=body, subject="", recipient_cohort="client")
    assert diff.signoff_style == SignoffStyle.THANKS.value


def test_structural_diff_strips_quoted_reply_chain():
    body = (
        "Hi Sarah,\n\nLet's plan to meet next week.\n\nBest,\nMarcus\n\n"
        "On 2026-05-20, Sarah wrote:\n> Original message\n> with content"
    )
    diff = extract_structural_diff(body_text=body, subject="", recipient_cohort="client")
    blob = diff.to_json_bytes().decode("utf-8")
    assert "Original message" not in blob


def test_structural_diff_punctuation_rhythm():
    body = "We need to confirm. Are you available tomorrow? Thanks."
    diff = extract_structural_diff(body_text=body, subject="", recipient_cohort="client")
    rhythm = diff.punctuation_rhythm
    assert rhythm["period_per_100"] > 0
    assert rhythm["question_per_100"] > 0


# ---------------------------------------------------------------------------
# Partner-authored filter
# ---------------------------------------------------------------------------


def test_filter_excludes_empty_body():
    fil = PartnerAuthoredFilter(FakeAuditLookup())
    result = _run(
        fil.evaluate(
            CandidateMessage(
                body_text="",
                word_count=0,
                likely_agent_drafted=None,
                body_digest=compute_body_digest(""),
            )
        )
    )
    assert not result.accept
    assert result.reason == REASON_EMPTY_BODY


def test_filter_excludes_too_short():
    body = "Got it, thanks."
    fil = PartnerAuthoredFilter(FakeAuditLookup())
    result = _run(
        fil.evaluate(
            CandidateMessage(
                body_text=body,
                word_count=3,
                likely_agent_drafted=False,
                body_digest=compute_body_digest(body),
            )
        )
    )
    assert not result.accept
    assert result.reason == REASON_TOO_SHORT


def test_filter_excludes_adapter_tagged_agent_drafts():
    body = "Hello team, here is a sufficiently long body to pass the length gate."
    fil = PartnerAuthoredFilter(FakeAuditLookup())
    result = _run(
        fil.evaluate(
            CandidateMessage(
                body_text=body,
                word_count=20,
                likely_agent_drafted=True,
                body_digest=compute_body_digest(body),
            )
        )
    )
    assert not result.accept
    assert result.reason == REASON_ADAPTER_AGENT_DRAFTED


def test_filter_excludes_audit_log_digest_match():
    body = "Hello, here is a sufficiently long body that the AI Employee originally drafted."
    digest = compute_body_digest(body)
    fil = PartnerAuthoredFilter(FakeAuditLookup({digest}))
    result = _run(
        fil.evaluate(
            CandidateMessage(
                body_text=body,
                word_count=20,
                likely_agent_drafted=None,
                body_digest=digest,
            )
        )
    )
    assert not result.accept
    assert result.reason == REASON_AUDIT_LOG_DIGEST_MATCH


def test_filter_excludes_shape_heuristic_marker():
    body = (
        "Hi Sarah, here is the response we discussed.\n"
        "[Drafted by your AI Employee for review]\n"
        "Please review before sending."
    )
    fil = PartnerAuthoredFilter(FakeAuditLookup())
    result = _run(
        fil.evaluate(
            CandidateMessage(
                body_text=body,
                word_count=20,
                likely_agent_drafted=False,
                body_digest=compute_body_digest(body),
            )
        )
    )
    assert not result.accept
    assert result.reason == REASON_SHAPE_HEURISTIC


def test_filter_accepts_clean_partner_authored_message():
    body = (
        "Hi Sarah,\n\nLet's plan to meet next Tuesday at two PM to walk through the "
        "deposition outline together.\n\nBest,\nMarcus"
    )
    fil = PartnerAuthoredFilter(FakeAuditLookup())
    result = _run(
        fil.evaluate(
            CandidateMessage(
                body_text=body,
                word_count=30,
                likely_agent_drafted=False,
                body_digest=compute_body_digest(body),
            )
        )
    )
    assert result.accept
    assert result.reason == ACCEPT_REASON


# ---------------------------------------------------------------------------
# Runner — happy path
# ---------------------------------------------------------------------------


def test_runner_ingests_and_writes_to_r2():
    messages = [
        _mk_msg(id_="msg-1"),
        _mk_msg(
            id_="msg-2",
            body=_mk_msg().body_text + " Plus a second paragraph here for variety.",
        ),
    ]
    runner, conn, r2, cursor_store, _ = _make_runner(
        messages=messages,
        cohort_mapping={"sarah@example.com": "client"},
    )

    result = _run(runner.run_ingestion(mode=IngestionMode.SCHEDULED))

    assert result.items_seen == 2
    assert result.items_ingested == 2
    assert result.items_filtered == 0
    assert result.items_errored == 0
    assert result.status == INGEST_STATUS_OK
    assert result.cohort_histogram == {"client": 2}
    assert result.next_cursor == "cursor-2"
    # Two R2 objects, both under the customer slug and cohort path.
    assert len(r2.objects) == 2
    for key in r2.objects:
        assert key.startswith("demo-firm/voice/cohort/client/")
        assert key.endswith(".json")
    # Cursor was advanced.
    assert cursor_store.value == "cursor-2"

    # State row reflects the cohort histogram.
    row = conn.execute(
        "SELECT items_last_run, samples_by_cohort_json, ingest_status FROM voice_source_state"
    ).fetchone()
    assert row[0] == 2
    assert json.loads(row[1]) == {"client": 2}
    assert row[2] == INGEST_STATUS_OK

    # Two provenance rows, all partner_authored=1.
    rows = conn.execute(
        "SELECT partner_authored, recipient_cohort_id, r2_key, structural_diff_digest "
        "FROM voice_ingestion_items ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    for partner_authored, cohort, r2_key, diff_digest in rows:
        assert partner_authored == 1
        assert cohort == "client"
        assert r2_key.startswith("demo-firm/voice/cohort/client/")
        assert len(diff_digest) == 64  # SHA-256 hex


def test_runner_tags_unassigned_when_cohort_resolver_returns_none():
    messages = [_mk_msg(id_="msg-1", recipients=("nobody@example.com",))]
    runner, conn, r2, _, _ = _make_runner(messages=messages, cohort_mapping={})
    result = _run(runner.run_ingestion(mode=IngestionMode.SCHEDULED))
    assert result.items_ingested == 1
    assert result.cohort_histogram == {COHORT_UNASSIGNED: 1}
    # R2 key uses 'unassigned' segment.
    assert all(f"/cohort/{COHORT_UNASSIGNED}/" in key for key in r2.objects)


def test_runner_filtered_messages_record_provenance_but_no_r2():
    body = "Quick note."
    messages = [_mk_msg(id_="msg-1", body=body, likely_agent_drafted=False)]
    runner, conn, r2, _, _ = _make_runner(messages=messages)
    result = _run(runner.run_ingestion(mode=IngestionMode.SCHEDULED))
    assert result.items_filtered == 1
    assert result.items_ingested == 0
    assert len(r2.objects) == 0
    row = conn.execute(
        "SELECT partner_authored, filter_reason, r2_key FROM voice_ingestion_items"
    ).fetchone()
    assert row == (0, REASON_TOO_SHORT, None)


def test_runner_dedupes_on_re_run():
    msg = _mk_msg(id_="msg-stable")
    runner, conn, r2, _, source = _make_runner(messages=[msg])
    first = _run(runner.run_ingestion(mode=IngestionMode.SCHEDULED))
    assert first.items_ingested == 1
    assert len(r2.objects) == 1

    # Re-run: the source yields the same message again.
    second = _run(runner.run_ingestion(mode=IngestionMode.ON_DEMAND))
    assert second.items_ingested == 0
    assert second.items_skipped_duplicate == 1
    # No new R2 objects.
    assert len(r2.objects) == 1
    rows = conn.execute(
        "SELECT COUNT(*) FROM voice_ingestion_items WHERE deleted_at IS NULL"
    ).fetchone()
    assert rows[0] == 1


def test_runner_records_error_when_source_blows_up():
    runner, conn, _, _, _ = _make_runner()
    runner.source = ErroringEmailSource()  # type: ignore[assignment]
    result = _run(runner.run_ingestion(mode=IngestionMode.SCHEDULED))
    assert result.items_seen == 0
    assert result.status == INGEST_STATUS_ERROR
    assert "connector exploded" in (result.error or "")
    # State row still upserted so the dashboard surfaces the failure.
    row = conn.execute(
        "SELECT ingest_status, last_error FROM voice_source_state"
    ).fetchone()
    assert row[0] == INGEST_STATUS_ERROR
    assert "connector exploded" in (row[1] or "")


def test_runner_continues_on_per_item_r2_failure():
    messages = [_mk_msg(id_="ok-1"), _mk_msg(id_="boom-2"), _mk_msg(id_="ok-3")]
    runner, conn, r2, _, _ = _make_runner(
        messages=messages, cohort_mapping={"sarah@example.com": "client"}
    )

    # Inject a one-shot failure on the second put.
    original_put = r2.put
    call_count = {"n": 0}

    async def flaky_put(key, body, content_type):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("transient R2 outage")
        return await original_put(key, body, content_type)

    r2.put = flaky_put  # type: ignore[assignment]

    result = _run(runner.run_ingestion(mode=IngestionMode.SCHEDULED))
    assert result.items_ingested == 2
    assert result.items_errored == 1
    # Final status remains OK because at least one item ingested.
    assert result.status == INGEST_STATUS_OK


def test_runner_with_no_email_source_returns_zero_items_state_row():
    runner, conn, _, _, _ = _make_runner()
    runner.source = NoEmailSource()  # type: ignore[assignment]
    result = _run(runner.run_ingestion(mode=IngestionMode.SCHEDULED))
    assert result.items_seen == 0
    assert result.items_ingested == 0
    assert result.status == INGEST_STATUS_OK
    row = conn.execute("SELECT source_id, items_last_run FROM voice_source_state").fetchone()
    assert row == ("none", 0)


# ---------------------------------------------------------------------------
# Retention enforcer
# ---------------------------------------------------------------------------


def test_retention_enforcer_deletes_expired_items():
    conn = _conn()
    store = _store(conn)
    r2 = FakeR2Client("demo-firm")

    # Insert one fresh + one expired item.
    _run(
        store.insert_item(
            IngestionItemRecord(
                source_kind="email",
                source_id="ms-graph",
                source_message_digest="a" * 64,
                recipient_cohort_id="client",
                partner_authored=True,
                sent_at="2026-05-20T00:00:00.000Z",
                filter_reason=ACCEPT_REASON,
                r2_key="demo-firm/voice/cohort/client/fresh.json",
                structural_diff_digest="b" * 64,
                word_count=42,
            )
        )
    )
    expired_id = _run(
        store.insert_item(
            IngestionItemRecord(
                source_kind="email",
                source_id="ms-graph",
                source_message_digest="c" * 64,
                recipient_cohort_id="client",
                partner_authored=True,
                sent_at="2024-01-01T00:00:00.000Z",
                filter_reason=ACCEPT_REASON,
                r2_key="demo-firm/voice/cohort/client/expired.json",
                structural_diff_digest="d" * 64,
                word_count=99,
            )
        )
    )
    # Backdate the expired row's ingested_at directly (the insert helper
    # always stamps "now").
    conn.execute(
        "UPDATE voice_ingestion_items SET ingested_at = ? WHERE id = ?",
        ("2024-01-01T00:00:00.000Z", expired_id),
    )
    conn.commit()
    r2.objects[
        "demo-firm/voice/cohort/client/fresh.json"
    ] = b"{}"
    r2.objects[
        "demo-firm/voice/cohort/client/expired.json"
    ] = b"{}"

    summary = _run(
        enforce_retention(
            state_store=store,
            r2_client=r2,
            voice_retention_days=365,
            now=datetime(2026, 5, 21, tzinfo=UTC),
        )
    )
    assert summary["considered"] == 1
    assert summary["deleted"] == 1
    assert summary["errors"] == 0
    # Fresh row untouched.
    assert "demo-firm/voice/cohort/client/fresh.json" in r2.objects
    # Expired row's R2 object removed.
    assert "demo-firm/voice/cohort/client/expired.json" not in r2.objects
    # Expired provenance row soft-deleted.
    deleted_at = conn.execute(
        "SELECT deleted_at FROM voice_ingestion_items WHERE id = ?", (expired_id,)
    ).fetchone()[0]
    assert deleted_at is not None


# ---------------------------------------------------------------------------
# Decommission hook
# ---------------------------------------------------------------------------


def test_decommission_source_removes_every_r2_object_and_state_row():
    messages = [_mk_msg(id_=f"msg-{i}") for i in range(3)]
    runner, conn, r2, _, _ = _make_runner(
        messages=messages, cohort_mapping={"sarah@example.com": "client"}
    )
    _run(runner.run_ingestion(mode=IngestionMode.SCHEDULED))
    assert len(r2.objects) == 3

    summary = _run(
        decommission_source(
            state_store=runner.state_store,
            r2_client=r2,
            source_kind="email",
            source_id="test-adapter",
        )
    )
    assert summary["removed"] == 3
    assert summary["errors"] == 0
    assert len(r2.objects) == 0
    # State row deleted.
    rows = conn.execute(
        "SELECT COUNT(*) FROM voice_source_state WHERE source_id = 'test-adapter'"
    ).fetchone()
    assert rows[0] == 0
    # Every provenance row soft-deleted.
    active = conn.execute(
        "SELECT COUNT(*) FROM voice_ingestion_items WHERE deleted_at IS NULL"
    ).fetchone()[0]
    assert active == 0


# ---------------------------------------------------------------------------
# Privacy: substantive content never lands anywhere
# ---------------------------------------------------------------------------


def test_no_raw_body_in_r2_or_d1():
    secret_body = (
        "Hi Sarah,\n\nThe SETTLEMENT-MAGIC-PHRASE for Smith vs. Jones is "
        "absolutely confidential and must not leak.\n\nBest,\nMarcus"
    )
    msg = _mk_msg(id_="msg-1", body=secret_body)
    runner, conn, r2, _, _ = _make_runner(
        messages=[msg], cohort_mapping={"sarah@example.com": "client"}
    )
    _run(runner.run_ingestion(mode=IngestionMode.SCHEDULED))

    # R2 object never contains the secret.
    for key, blob in r2.objects.items():
        assert b"SETTLEMENT-MAGIC-PHRASE" not in blob, f"leak in {key}"
        assert b"Sarah" not in blob, f"name leak in {key}"
        assert b"Smith" not in blob, f"matter leak in {key}"
        assert b"Marcus" not in blob, f"signer leak in {key}"

    # D1 rows never contain the secret either.
    for table in ("voice_source_state", "voice_ingestion_items"):
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            for value in row:
                if isinstance(value, str):
                    assert "SETTLEMENT-MAGIC-PHRASE" not in value
                    assert "Sarah" not in value
                    assert "Smith" not in value


# ---------------------------------------------------------------------------
# State / dashboard surface
# ---------------------------------------------------------------------------


def test_state_store_round_trip_decodes_cohort_histogram():
    conn = _conn()
    store = _store(conn)
    _run(
        store.upsert_state(
            IngestionStateUpdate(
                source_kind="email",
                source_id="ms-graph",
                ingested_at="2026-05-21T12:00:00.000Z",
                status=INGEST_STATUS_OK,
                items_last_run=4,
                samples_by_cohort={"client": 3, "opposing_counsel": 1},
            )
        )
    )
    rows = _run(store.read_states())
    assert len(rows) == 1
    assert rows[0].samples_by_cohort == {"client": 3, "opposing_counsel": 1}
    assert rows[0].ingest_status == INGEST_STATUS_OK
    assert rows[0].last_success_at == "2026-05-21T12:00:00.000Z"


def test_ingestion_state_update_rejects_invalid_status():
    with pytest.raises(ValueError, match="ingest_status"):
        IngestionStateUpdate(
            source_kind="email",
            source_id="ms-graph",
            ingested_at="2026-05-21T12:00:00.000Z",
            status="bogus",
            items_last_run=0,
            samples_by_cohort={},
        )
