"""Tests for the ``hermes-smd-memory-mirror`` plugin.

Coverage:
  - Plugin registers ``on_session_end``.
  - Hook callback degrades gracefully on missing env / unreachable Honcho.
  - ``compute_evidence_status`` correctly classifies the three buckets.
  - ``conclusion_to_record`` extracts provenance from varying Honcho
    payload shapes (direct ``source_message_ids``, ``evidence`` list,
    nested ``reasoning.source_messages``).
  - ``mirror_session`` writes one row per conclusion with provenance,
    skips malformed conclusions without aborting the pass, and uses the
    high-water-mark to bound the Honcho poll.
  - TTL archival copies the row, physically deletes from Honcho, then
    removes the live row in that order. Halts on Honcho outage.
  - ``dismiss_conclusion`` physically deletes from Honcho and stamps the
    D1 mirror row; refuses to dismiss already-dismissed rows; refuses
    missing rows; tolerates Honcho 404.
  - DDL strings declare both tables and the expected provenance columns.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import load_plugin


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def test_memory_mirror_registers_expected_hooks(fake_ctx) -> None:
    """hermes-smd-memory-mirror must attach to on_session_end."""
    mod = load_plugin("hermes-smd-memory-mirror")
    assert callable(mod.register)

    mod.register(fake_ctx)

    assert "on_session_end" in fake_ctx.registered
    assert len(fake_ctx.registered["on_session_end"]) == 1


def test_on_session_end_is_exception_safe_when_env_missing(
    monkeypatch, fake_ctx
) -> None:
    """A missing env var must not propagate from on_session_end."""
    # Ensure required secrets are missing.
    for name in (
        "SMD_CUSTOMER_SLUG",
        "SMD_D1_OBSERVATIONS_BINDING",
        "HONCHO_BASE_URL",
        "HONCHO_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    mod = load_plugin("hermes-smd-memory-mirror")
    mod.register(fake_ctx)
    callback = fake_ctx.registered["on_session_end"][0]

    # Must NOT raise even though shared.secrets.require will raise KeyError.
    callback(
        session_id="sess-1",
        completed=True,
        interrupted=False,
        model="claude-opus-4-7",
        platform="cli",
    )


def test_on_session_end_no_op_when_session_id_missing(fake_ctx) -> None:
    """Empty session_id returns early without touching env."""
    mod = load_plugin("hermes-smd-memory-mirror")
    mod.register(fake_ctx)
    callback = fake_ctx.registered["on_session_end"][0]

    # No env set; no exception because we return before reading any env.
    callback(session_id="", completed=True, interrupted=False, model="m", platform="p")
    callback(completed=True, interrupted=False, model="m", platform="p")


# ---------------------------------------------------------------------------
# Evidence-status classification
# ---------------------------------------------------------------------------


def test_compute_evidence_status_evidenced() -> None:
    """A type-appropriate non-empty source list is 'evidenced'."""
    mod = load_plugin("hermes-smd-memory-mirror")
    assert (
        mod.mirror.compute_evidence_status(
            observation_type="voice_drift",
            source_message_ids=["m-1"],
        )
        == "evidenced"
    )
    assert (
        mod.mirror.compute_evidence_status(
            observation_type="recurring_correction",
            source_message_ids=["m-1", "m-2"],
        )
        == "evidenced"
    )


def test_compute_evidence_status_unevidenced_when_empty() -> None:
    """Empty source list is 'unevidenced'."""
    mod = load_plugin("hermes-smd-memory-mirror")
    assert (
        mod.mirror.compute_evidence_status(
            observation_type="voice_drift",
            source_message_ids=[],
        )
        == "unevidenced"
    )


def test_compute_evidence_status_insufficient_when_below_floor() -> None:
    """A recurring_correction with one message falls below the floor of 2."""
    mod = load_plugin("hermes-smd-memory-mirror")
    assert (
        mod.mirror.compute_evidence_status(
            observation_type="recurring_correction",
            source_message_ids=["m-1"],
        )
        == "insufficient"
    )


def test_compute_evidence_status_unknown_type_defaults_to_evidenced() -> None:
    """An unrecognized type with any evidence still lands as 'evidenced'."""
    mod = load_plugin("hermes-smd-memory-mirror")
    assert (
        mod.mirror.compute_evidence_status(
            observation_type="future_type_we_don_t_know",
            source_message_ids=["m-1"],
        )
        == "evidenced"
    )


# ---------------------------------------------------------------------------
# Honcho conclusion → ObservationRecord
# ---------------------------------------------------------------------------


def test_conclusion_to_record_extracts_direct_source_ids() -> None:
    """Direct source_message_ids list is preferred."""
    mod = load_plugin("hermes-smd-memory-mirror")
    record = mod.mirror.conclusion_to_record(
        {
            "id": "concl-1",
            "type": "voice_drift",
            "body": {"says": "client prefers shorter signoffs"},
            "source_message_ids": ["msg-1", "msg-2"],
            "confidence": 0.82,
            "created_at": "2026-05-20T12:00:00.000Z",
        },
        session_id="sess-1",
        mirrored_at="2026-05-20T12:00:01.000Z",
    )
    assert record.honcho_conclusion_id == "concl-1"
    assert record.session_id == "sess-1"
    assert record.observation_type.value == "voice_drift"
    assert record.source_message_ids == ["msg-1", "msg-2"]
    assert record.confidence == 0.82
    assert record.evidence_status == "evidenced"
    assert record.honcho_created_at == "2026-05-20T12:00:00.000Z"
    assert record.mirrored_at == "2026-05-20T12:00:01.000Z"


def test_conclusion_to_record_extracts_evidence_list() -> None:
    """When source_message_ids is absent, 'evidence' list is used."""
    mod = load_plugin("hermes-smd-memory-mirror")
    record = mod.mirror.conclusion_to_record(
        {
            "id": "concl-2",
            "observation_type": "preference_signal",
            "body": {"prefers": "concise"},
            "evidence": [
                {"message_id": "msg-3", "score": 0.9},
                {"message_id": "msg-4"},
                "msg-5",
            ],
        },
        session_id="sess-2",
        mirrored_at="2026-05-20T12:00:00.000Z",
    )
    assert record.source_message_ids == ["msg-3", "msg-4", "msg-5"]
    assert record.evidence_status == "evidenced"


def test_conclusion_to_record_extracts_nested_reasoning() -> None:
    """Nested reasoning.source_messages is used as a last fallback."""
    mod = load_plugin("hermes-smd-memory-mirror")
    record = mod.mirror.conclusion_to_record(
        {
            "id": "concl-3",
            "type": "voice_drift",
            "body": {},
            "reasoning": {"source_messages": [{"id": "msg-6"}, "msg-7"]},
        },
        session_id="sess-3",
        mirrored_at="2026-05-20T12:00:00.000Z",
    )
    assert record.source_message_ids == ["msg-6", "msg-7"]


def test_conclusion_to_record_unknown_type_becomes_other() -> None:
    """Unrecognized observation types coerce to 'other' without dropping data."""
    mod = load_plugin("hermes-smd-memory-mirror")
    record = mod.mirror.conclusion_to_record(
        {
            "id": "concl-4",
            "type": "totally_new_type",
            "body": {"raw": "payload"},
            "source_message_ids": ["msg-8"],
        },
        session_id="sess-4",
        mirrored_at="2026-05-20T12:00:00.000Z",
    )
    assert record.observation_type.value == "other"


def test_conclusion_to_record_missing_id_raises() -> None:
    """No id → cannot mirror; raise so caller skips."""
    mod = load_plugin("hermes-smd-memory-mirror")
    with pytest.raises(ValueError):
        mod.mirror.conclusion_to_record(
            {"type": "voice_drift", "body": {}},
            session_id="sess-5",
            mirrored_at="2026-05-20T12:00:00.000Z",
        )


def test_conclusion_to_record_empty_evidence_stamps_synthetic() -> None:
    """Empty evidence becomes evidence_status='unevidenced' with a sentinel id."""
    mod = load_plugin("hermes-smd-memory-mirror")
    record = mod.mirror.conclusion_to_record(
        {"id": "concl-6", "type": "voice_drift", "body": {}},
        session_id="sess-6",
        mirrored_at="2026-05-20T12:00:00.000Z",
    )
    assert record.evidence_status == "unevidenced"
    # Schema requires source_message_ids non-empty; sentinel preserves the CHECK.
    assert record.source_message_ids == ["__none__"]


# ---------------------------------------------------------------------------
# mirror_session — wiring + provenance recording
# ---------------------------------------------------------------------------


class _FakeHonchoClient:
    """Records calls; returns canned conclusions."""

    def __init__(self, conclusions: list[dict]) -> None:
        self._conclusions = conclusions
        self.list_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []

    def list_conclusions(self, *, session_id: str, since: str | None = None) -> list[dict]:
        self.list_calls.append({"session_id": session_id, "since": since})
        return list(self._conclusions)

    def delete_conclusion(self, conclusion_id: str) -> bool:
        self.delete_calls.append(conclusion_id)
        return True


class _FakeD1Client:
    """Records execute/query calls; returns canned query rows."""

    def __init__(self, query_rows: list[list[dict]] | None = None) -> None:
        self._query_rows = list(query_rows or [])
        self.executes: list[tuple[str, tuple]] = []
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, sql: str, *params: Any) -> None:
        self.executes.append((sql, params))

    def query(self, sql: str, *params: Any) -> list[dict]:
        self.queries.append((sql, params))
        if self._query_rows:
            return self._query_rows.pop(0)
        return []


def test_mirror_session_writes_provenance_columns() -> None:
    """Each Honcho conclusion produces one persona_observations INSERT."""
    mod = load_plugin("hermes-smd-memory-mirror")
    honcho = _FakeHonchoClient(
        [
            {
                "id": "concl-A",
                "type": "voice_drift",
                "body": {"says": "client prefers casual"},
                "source_message_ids": ["m-1", "m-2"],
                "confidence": 0.7,
                "created_at": "2026-05-20T11:00:00.000Z",
            }
        ]
    )
    d1 = _FakeD1Client()

    result = mod.mirror.mirror_session(
        session_id="sess-A", honcho_client=honcho, d1_client=d1
    )

    assert result.conclusions_polled == 1
    assert result.rows_written == 1
    assert result.rows_skipped == 0

    # One INSERT executed with the provenance columns populated.
    assert len(d1.executes) == 1
    sql, params = d1.executes[0]
    assert "INSERT INTO persona_observations" in sql
    # Position 1: honcho_conclusion_id; position 8: evidence_status (per
    # the column order in mirror._INSERT_OBSERVATION_SQL).
    assert params[1] == "concl-A"
    assert params[8] == "evidenced"
    # source_message_ids serialized as JSON.
    assert json.loads(params[6]) == ["m-1", "m-2"]


def test_mirror_session_skips_malformed_conclusion() -> None:
    """A bad conclusion is counted as skipped; the rest still land."""
    mod = load_plugin("hermes-smd-memory-mirror")
    honcho = _FakeHonchoClient(
        [
            {"type": "voice_drift", "body": {}},  # missing id
            {
                "id": "concl-good",
                "type": "voice_drift",
                "body": {},
                "source_message_ids": ["m-1"],
            },
        ]
    )
    d1 = _FakeD1Client()

    result = mod.mirror.mirror_session(
        session_id="sess-B", honcho_client=honcho, d1_client=d1
    )
    assert result.conclusions_polled == 2
    assert result.rows_written == 1
    assert result.rows_skipped == 1


def test_mirror_session_passes_high_water_mark_to_honcho() -> None:
    """The next poll bounds 'since' to the last mirrored honcho_created_at."""
    mod = load_plugin("hermes-smd-memory-mirror")
    honcho = _FakeHonchoClient([])
    d1 = _FakeD1Client(query_rows=[[{"honcho_created_at": "2026-05-20T11:00:00.000Z"}]])

    mod.mirror.mirror_session(session_id="sess-C", honcho_client=honcho, d1_client=d1)

    assert len(honcho.list_calls) == 1
    assert honcho.list_calls[0]["since"] == "2026-05-20T11:00:00.000Z"


def test_mirror_session_records_unevidenced_status() -> None:
    """A conclusion with no source ids lands with evidence_status='unevidenced'."""
    mod = load_plugin("hermes-smd-memory-mirror")
    honcho = _FakeHonchoClient(
        [
            {
                "id": "concl-no-evidence",
                "type": "voice_drift",
                "body": {"text": "claim with no backing"},
            }
        ]
    )
    d1 = _FakeD1Client()

    result = mod.mirror.mirror_session(
        session_id="sess-D", honcho_client=honcho, d1_client=d1
    )

    assert result.rows_written == 1
    sql, params = d1.executes[0]
    assert params[8] == "unevidenced"


# ---------------------------------------------------------------------------
# Archive — TTL sweep
# ---------------------------------------------------------------------------


def test_archive_copies_row_then_physically_deletes_from_honcho_then_live() -> None:
    """Order must be archive → Honcho delete → live delete."""
    mod = load_plugin("hermes-smd-memory-mirror")

    aged_row = {
        "observation_id": "obs-1",
        "honcho_conclusion_id": "honcho-1",
        "session_id": "sess-X",
        "persona_slug": None,
        "observation_type": "voice_drift",
        "observation_body": '{"k":"v"}',
        "source_message_ids": '["m-1"]',
        "confidence": 0.5,
        "evidence_status": "evidenced",
        "honcho_created_at": "2025-11-01T00:00:00.000Z",
        "mirrored_at": "2025-11-01T00:00:01.000Z",
        "schema_version": 1,
        "dismissed_at": None,
        "dismissed_by": None,
        "dismissed_reason": None,
    }
    d1 = _FakeD1Client(query_rows=[[aged_row]])
    honcho = _FakeHonchoClient([])

    result = mod.archive.archive_aged_conclusions(
        archive_after_days=180,
        honcho_client=honcho,
        d1_client=d1,
    )

    assert result.rows_archived == 1
    assert result.errors == 0
    # Honcho delete called on the correct id.
    assert honcho.delete_calls == ["honcho-1"]
    # D1 executes in the expected order: INSERT archive → DELETE live.
    insert_sqls = [sql for sql, _ in d1.executes]
    assert any("INSERT INTO persona_observations_archive" in s for s in insert_sqls)
    assert any("DELETE FROM persona_observations WHERE" in s for s in insert_sqls)
    # The INSERT archive call comes before the DELETE live call.
    insert_idx = next(
        i for i, (s, _) in enumerate(d1.executes)
        if "INSERT INTO persona_observations_archive" in s
    )
    delete_idx = next(
        i for i, (s, _) in enumerate(d1.executes)
        if "DELETE FROM persona_observations WHERE" in s
    )
    assert insert_idx < delete_idx


def test_archive_halts_sweep_on_honcho_outage() -> None:
    """Honcho unreachable mid-sweep stops the loop and counts the error."""
    mod = load_plugin("hermes-smd-memory-mirror")

    aged_rows = [
        {
            "observation_id": f"obs-{i}",
            "honcho_conclusion_id": f"honcho-{i}",
            "session_id": "sess-X",
            "persona_slug": None,
            "observation_type": "voice_drift",
            "observation_body": "{}",
            "source_message_ids": '["m-1"]',
            "confidence": 0.5,
            "evidence_status": "evidenced",
            "honcho_created_at": "2025-11-01T00:00:00.000Z",
            "mirrored_at": "2025-11-01T00:00:01.000Z",
            "schema_version": 1,
            "dismissed_at": None,
            "dismissed_by": None,
            "dismissed_reason": None,
        }
        for i in range(3)
    ]
    d1 = _FakeD1Client(query_rows=[aged_rows])

    HonchoUnreachable = mod.honcho_client.HonchoUnreachable

    class _BrokenHoncho:
        def __init__(self) -> None:
            self.delete_calls = 0

        def delete_conclusion(self, conclusion_id: str) -> bool:
            self.delete_calls += 1
            raise HonchoUnreachable("simulated outage")

    honcho = _BrokenHoncho()
    result = mod.archive.archive_aged_conclusions(
        archive_after_days=180,
        honcho_client=honcho,
        d1_client=d1,
    )
    # First row triggered the outage; no further rows processed.
    assert honcho.delete_calls == 1
    assert result.rows_archived == 0
    assert result.errors >= 1


# ---------------------------------------------------------------------------
# Dismissal
# ---------------------------------------------------------------------------


def test_dismiss_conclusion_physical_delete_and_d1_stamp() -> None:
    """Dismissal triggers Honcho DELETE and stamps the D1 mirror row."""
    mod = load_plugin("hermes-smd-memory-mirror")
    d1 = _FakeD1Client(
        query_rows=[
            [{"honcho_conclusion_id": "honcho-7", "dismissed_at": None}]
        ]
    )
    honcho = _FakeHonchoClient([])

    result = mod.dismiss.dismiss_conclusion(
        "obs-7",
        reason="false positive — client never said that",
        dismissed_by="captain",
        honcho_client=honcho,
        d1_client=d1,
    )

    assert result.honcho_conclusion_id == "honcho-7"
    assert result.honcho_row_existed is True
    assert honcho.delete_calls == ["honcho-7"]
    # D1 stamp UPDATE issued.
    update_sqls = [sql for sql, _ in d1.executes]
    assert any("UPDATE persona_observations" in s for s in update_sqls)


def test_dismiss_conclusion_requires_reason() -> None:
    """Empty reason is rejected."""
    mod = load_plugin("hermes-smd-memory-mirror")
    with pytest.raises(ValueError):
        mod.dismiss.dismiss_conclusion(
            "obs-1",
            reason="",
            dismissed_by="captain",
            honcho_client=_FakeHonchoClient([]),
            d1_client=_FakeD1Client(),
        )


def test_dismiss_conclusion_requires_dismissed_by() -> None:
    """Empty dismissed_by is rejected."""
    mod = load_plugin("hermes-smd-memory-mirror")
    with pytest.raises(ValueError):
        mod.dismiss.dismiss_conclusion(
            "obs-1",
            reason="x",
            dismissed_by="",
            honcho_client=_FakeHonchoClient([]),
            d1_client=_FakeD1Client(),
        )


def test_dismiss_conclusion_refuses_already_dismissed() -> None:
    """Re-dismissing a row raises AlreadyDismissed."""
    mod = load_plugin("hermes-smd-memory-mirror")
    d1 = _FakeD1Client(
        query_rows=[
            [{"honcho_conclusion_id": "h-1", "dismissed_at": "2026-05-20T00:00:00.000Z"}]
        ]
    )
    with pytest.raises(mod.dismiss.AlreadyDismissed):
        mod.dismiss.dismiss_conclusion(
            "obs-1",
            reason="x",
            dismissed_by="captain",
            honcho_client=_FakeHonchoClient([]),
            d1_client=d1,
        )


def test_dismiss_conclusion_missing_row_raises_not_found() -> None:
    """Missing observation_id raises ObservationNotFound."""
    mod = load_plugin("hermes-smd-memory-mirror")
    d1 = _FakeD1Client(query_rows=[[]])
    with pytest.raises(mod.dismiss.ObservationNotFound):
        mod.dismiss.dismiss_conclusion(
            "obs-missing",
            reason="x",
            dismissed_by="captain",
            honcho_client=_FakeHonchoClient([]),
            d1_client=d1,
        )


# ---------------------------------------------------------------------------
# DDL surface
# ---------------------------------------------------------------------------


def test_schemas_declare_provenance_columns() -> None:
    """Both DDLs name the provenance columns required by ADR 0016."""
    mod = load_plugin("hermes-smd-memory-mirror")
    live = mod.schemas.PERSONA_OBSERVATIONS_DDL
    archive = mod.schemas.PERSONA_OBSERVATIONS_ARCHIVE_DDL
    for ddl in (live, archive):
        assert "honcho_conclusion_id" in ddl
        assert "source_message_ids" in ddl
        assert "confidence" in ddl
        assert "evidence_status" in ddl
        assert "mirrored_at" in ddl
    assert "archived_at" in archive


def test_schemas_all_ddls_tuple_ordered_live_then_archive() -> None:
    """ALL_DDLS exposes both statements in materialization order."""
    mod = load_plugin("hermes-smd-memory-mirror")
    assert mod.schemas.ALL_DDLS[0] == mod.schemas.PERSONA_OBSERVATIONS_DDL
    assert mod.schemas.ALL_DDLS[1] == mod.schemas.PERSONA_OBSERVATIONS_ARCHIVE_DDL
