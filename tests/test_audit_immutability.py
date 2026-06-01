"""Tests for plugins/hermes-smd-audit/immutability.py.

Ported from ss-console/operator/adapter/tests/test_audit_log_immutability.py.

Covers the Worker-layer enforcement wrapper, the SQL inspection helper,
the Logpush mirror protocol stub, and the LegalHoldException bypass path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def load_plugin(plugin_name: str):
    """Load the plugin package so submodule imports inside ``__init__.py`` resolve.

    See test_audit_emit.py for the explanation; the shared conftest loader
    doesn't pre-register the parent module in ``sys.modules``.
    """
    root = Path(__file__).parent.parent
    init_path = root / "plugins" / plugin_name / "__init__.py"
    sanitized = plugin_name.replace("-", "_")
    mod_name = f"plugin_{sanitized}"
    spec = importlib.util.spec_from_file_location(mod_name, init_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin spec for {plugin_name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake underlying executor — mirrors shared.d1_client.D1Client.execute(sql, *params)
# ---------------------------------------------------------------------------


class FakeExec:
    """Records every execute call so tests can assert pass-through."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, *params) -> None:
        self.calls.append((sql, tuple(params)))


# ---------------------------------------------------------------------------
# Pure-SQL inspection helper
# ---------------------------------------------------------------------------


def test_inspection_passes_insert_into_audit_log() -> None:
    mod = load_plugin("hermes-smd-audit")
    sql = "INSERT INTO audit_log (id, ts, action_type, actor) VALUES (?, ?, ?, ?)"
    assert mod.immutability.is_mutation_against_audit_log(sql) is False


def test_inspection_passes_select_from_audit_log() -> None:
    mod = load_plugin("hermes-smd-audit")
    sql = "SELECT * FROM audit_log WHERE id = ?"
    assert mod.immutability.is_mutation_against_audit_log(sql) is False


def test_inspection_passes_writes_to_other_tables() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert (
        mod.immutability.is_mutation_against_audit_log(
            "UPDATE memory_rules SET deleted_at = ? WHERE id = ?"
        )
        is False
    )
    assert (
        mod.immutability.is_mutation_against_audit_log("DELETE FROM draft_queue WHERE id = ?")
        is False
    )


def test_inspection_blocks_update_on_audit_log() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert (
        mod.immutability.is_mutation_against_audit_log(
            "UPDATE audit_log SET actor = 'bogus' WHERE id = ?"
        )
        is True
    )


def test_inspection_blocks_delete_from_audit_log() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert (
        mod.immutability.is_mutation_against_audit_log("DELETE FROM audit_log WHERE id = ?") is True
    )


def test_inspection_blocks_replace_on_audit_log() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert (
        mod.immutability.is_mutation_against_audit_log("REPLACE INTO audit_log VALUES (?)") is True
    )


def test_inspection_blocks_truncate_drop_alter() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert mod.immutability.is_mutation_against_audit_log("DROP TABLE audit_log") is True
    assert (
        mod.immutability.is_mutation_against_audit_log(
            "ALTER TABLE audit_log ADD COLUMN bogus TEXT"
        )
        is True
    )


def test_inspection_is_case_insensitive() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert mod.immutability.is_mutation_against_audit_log("delete from audit_log") is True
    assert mod.immutability.is_mutation_against_audit_log("Delete From Audit_Log") is True


def test_inspection_strips_comments_so_they_cannot_hide_the_table() -> None:
    """Comments are stripped before the verb check so they cannot hide DELETE."""
    mod = load_plugin("hermes-smd-audit")
    sql = "/* select * from audit_log */ DELETE FROM audit_log WHERE id = ?"
    assert mod.immutability.is_mutation_against_audit_log(sql) is True


def test_inspection_blocks_multistatement_targeting_audit_log() -> None:
    mod = load_plugin("hermes-smd-audit")
    sql = "SELECT 1; DELETE FROM audit_log WHERE id = ?"
    assert mod.immutability.is_mutation_against_audit_log(sql) is True


def test_inspection_tolerates_trailing_semicolon() -> None:
    mod = load_plugin("hermes-smd-audit")
    sql = "SELECT * FROM audit_log WHERE id = ?;"
    assert mod.immutability.is_mutation_against_audit_log(sql) is False


def test_inspection_blocks_when_table_name_appears_only_in_block_comment() -> None:
    """Mutation against another table; audit_log only in a comment → allowed."""
    mod = load_plugin("hermes-smd-audit")
    sql = "/* update audit_log placeholder */ UPDATE memory_rules SET deleted_at = ? WHERE id = ?"
    assert mod.immutability.is_mutation_against_audit_log(sql) is False


# ---------------------------------------------------------------------------
# D1Executor wrapper behavior
# ---------------------------------------------------------------------------


def test_wrapper_allows_insert_into_audit_log() -> None:
    mod = load_plugin("hermes-smd-audit")
    raw = FakeExec()
    safe = mod.immutability.D1Executor(raw)
    safe.execute(
        "INSERT INTO audit_log (id, ts, action_type, actor) VALUES (?, ?, ?, ?)",
        "01HZZZ",
        "2026-05-21T12:00:00.000Z",
        "DRAFT_CREATED",
        "agent",
    )
    assert len(raw.calls) == 1


def test_wrapper_allows_select_against_audit_log() -> None:
    mod = load_plugin("hermes-smd-audit")
    raw = FakeExec()
    safe = mod.immutability.D1Executor(raw)
    safe.execute("SELECT * FROM audit_log WHERE id = ?", "01HZZZ")
    assert len(raw.calls) == 1


def test_wrapper_blocks_delete() -> None:
    mod = load_plugin("hermes-smd-audit")
    raw = FakeExec()
    safe = mod.immutability.D1Executor(raw)
    with pytest.raises(mod.immutability.AuditLogImmutabilityError, match="append-only"):
        safe.execute("DELETE FROM audit_log WHERE id = ?", "01HZZZ")
    # The raw executor was never called.
    assert raw.calls == []


def test_wrapper_blocks_update() -> None:
    mod = load_plugin("hermes-smd-audit")
    raw = FakeExec()
    safe = mod.immutability.D1Executor(raw)
    with pytest.raises(mod.immutability.AuditLogImmutabilityError):
        safe.execute("UPDATE audit_log SET actor = 'forged' WHERE id = ?", "01HZZZ")
    assert raw.calls == []


def test_wrapper_passes_through_writes_to_other_tables() -> None:
    mod = load_plugin("hermes-smd-audit")
    raw = FakeExec()
    safe = mod.immutability.D1Executor(raw)
    safe.execute("UPDATE memory_rules SET deleted_at = ? WHERE id = ?", "2026-05-21", "rule-1")
    assert len(raw.calls) == 1


# ---------------------------------------------------------------------------
# LegalHoldException bypass
# ---------------------------------------------------------------------------


def test_legal_hold_ticket_allows_bypass() -> None:
    mod = load_plugin("hermes-smd-audit")
    raw = FakeExec()
    safe = mod.immutability.D1Executor(raw)
    # Captain-side redaction script (out of scope) clears its multi-confirmation
    # guard, writes the exceptions-ledger row, then calls the wrapper with the
    # matching ticket.
    safe.execute(
        "DELETE FROM audit_log WHERE id = ?",
        "01HZZZ",
        legal_hold_ticket="EXCEPTION-2026-001",
    )
    assert len(raw.calls) == 1


def test_legal_hold_exception_requires_non_empty_ticket() -> None:
    mod = load_plugin("hermes-smd-audit")
    with pytest.raises(ValueError, match="non-empty ticket"):
        mod.immutability.LegalHoldException("")


def test_legal_hold_exception_requires_non_none_ticket() -> None:
    mod = load_plugin("hermes-smd-audit")
    with pytest.raises(ValueError):
        mod.immutability.LegalHoldException(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LogpushMirror protocol + no-op stub
# ---------------------------------------------------------------------------


def test_noop_mirror_satisfies_protocol() -> None:
    mod = load_plugin("hermes-smd-audit")
    mirror = mod.immutability.NoopLogpushMirror()
    row = mod.immutability.MirroredAuditRow(
        id="01HZZZ",
        ts="2026-05-21T12:00:00.000Z",
        action_type="DRAFT_CREATED",
        actor="agent",
        actor_role="agent",
        skill_name="inbox-triage",
        matter_ref=None,
        input_digest=None,
        output_digest=None,
        diff_digest=None,
        trust_ceiling="draft_for_review",
        metadata=None,
    )
    # No raise, no return value beyond None
    assert mirror.mirror_audit_event(row) is None


def test_mirrored_audit_row_is_immutable_dataclass() -> None:
    mod = load_plugin("hermes-smd-audit")
    row = mod.immutability.MirroredAuditRow(
        id="01HZZZ",
        ts="2026-05-21T12:00:00.000Z",
        action_type="DRAFT_CREATED",
        actor="agent",
        actor_role=None,
        skill_name=None,
        matter_ref=None,
        input_digest=None,
        output_digest=None,
        diff_digest=None,
        trust_ceiling=None,
        metadata=None,
    )
    with pytest.raises((AttributeError, Exception)):
        row.id = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Error message points the caller at the right docs
# ---------------------------------------------------------------------------


def test_error_message_cites_the_spec_path() -> None:
    mod = load_plugin("hermes-smd-audit")
    safe = mod.immutability.D1Executor(FakeExec())
    with pytest.raises(mod.immutability.AuditLogImmutabilityError) as excinfo:
        safe.execute("DELETE FROM audit_log")
    msg = str(excinfo.value)
    assert "audit-log-immutability.md" in msg
    assert "AuditLogWriter" in msg
