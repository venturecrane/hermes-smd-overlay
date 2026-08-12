"""One correlation query must find every emitter's rows (ss-console #2312).

``audit_log`` carries no ``tool_call_id`` COLUMN — see
``shared/audit_contract.py`` ``COLUMNS``. The tool-call correlation id lives
inside the ``metadata`` TEXT blob, so correlating a call across emitters means
``json_extract(metadata, '$.<key>')``. That only works if every emitter agrees
on ``<key>``.

These tests run the real query against a real SQLite database rather than
comparing dicts, because the defect is a property of the QUERY, not of the
metadata builder: a dict-level assertion would pass on any key name at all and
so could not observe the layer it claims to check.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.audit_contract import (  # noqa: E402
    CANONICAL_TOOL_CALL_KEY,
    CREATE_TABLE_SQL,
)


def load_plugin(plugin_name: str):
    """Load the plugin package so submodule imports inside ``__init__.py`` resolve.

    Same sequencing as ``test_audit_emit.load_plugin`` — the shared conftest
    loader does not pre-register the parent module in ``sys.modules``.
    """
    init_path = REPO_ROOT / "plugins" / plugin_name / "__init__.py"
    sanitized = plugin_name.replace("-", "_")
    mod_name = f"plugin_{sanitized}"
    spec = importlib.util.spec_from_file_location(mod_name, init_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin spec for {plugin_name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class SqliteAuditClient:
    """D1Client-shaped executor backed by a real in-memory SQLite database.

    ``execute(sql, *params)`` matches ``shared.d1_client.D1Client``. Unlike a
    dict-recording fake, this one actually runs the SQL, so a correlation query
    written against the wrong metadata key returns zero rows here.
    """

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(CREATE_TABLE_SQL)

    def execute(self, sql: str, *params):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def correlate(self, key: str, value: str) -> list[sqlite3.Row]:
        """The correlation query an investigator would actually run."""
        cur = self.conn.execute(
            f"SELECT id, action_type, metadata FROM audit_log "  # noqa: S608 — key is a test literal
            f"WHERE json_extract(metadata, '$.{key}') = ? ORDER BY id",
            (value,),
        )
        return list(cur.fetchall())


TOOL_CALL_ID = "toolu_01CORRELATE0001"


def _emit_both_rows(mod, client) -> None:
    """Write one row from each of the two hermes-smd-audit emitters.

    ``emit_tool_event`` is the per-tool path (the highest-volume row type);
    ``emit_agent_skill_created_event`` is the skill-creation path. Both bracket
    the SAME dispatch, so both carry the same tool-call id.
    """
    writer = mod.emit.AuditLogWriter(client)
    mod.emit.emit_tool_event(
        writer,
        customer="acme",
        tool_name="skill_manage",
        args={"action": "create", "slug": "inbox-triage"},
        result="{}",
        task_id="task-1",
        session_id="sess-1",
        tool_call_id=TOOL_CALL_ID,
        duration_ms=8,
    )
    mod.emit.emit_agent_skill_created_event(
        writer,
        customer="acme",
        session_id="sess-1",
        skill_name_created="inbox-triage",
        skill_manage_args={"action": "create", "slug": "inbox-triage"},
        tool_call_id=TOOL_CALL_ID,
    )


def test_one_correlation_query_finds_both_emitters_rows() -> None:
    """The load-bearing assertion: ONE query, BOTH rows.

    Pre-fix this returns a single row — ``emit_tool_event`` wrote the id under
    ``metadata.trace_id`` while ``emit_agent_skill_created_event`` wrote it
    under ``metadata.tool_call_id``, so no single ``json_extract`` predicate
    could see both.
    """
    mod = load_plugin("hermes-smd-audit")
    client = SqliteAuditClient()
    _emit_both_rows(mod, client)

    found = client.correlate(CANONICAL_TOOL_CALL_KEY, TOOL_CALL_ID)
    action_types = sorted(row["action_type"] for row in found)
    assert action_types == ["AGENT_SKILL_CREATED", "TOOL_CALL_COMPLETED"], (
        f"one correlation query on '{CANONICAL_TOOL_CALL_KEY}' must find every "
        f"emitter's row for this dispatch; found {action_types}"
    )


# The retired spelling survives in exactly two files: the per-tool emitter that
# writes the transition alias, and the contract module that names it deprecated.
# Any THIRD site reopens the split this issue closed.
_RETIRED_KEY_ALLOWED = {
    "plugins/hermes-smd-audit/emit.py",
    "shared/audit_contract.py",
}

# Both spellings a metadata writer can use: dict-literal key and subscript
# assignment. The pre-fix defect was the LITERAL form (emit.py's builder), which
# a subscript-only scanner cannot see — so the scanner checks for both.
_RETIRED_KEY_PATTERNS = (
    '"trace_id":',
    "'trace_id':",
    'metadata["trace_id"]',
    "metadata['trace_id']",
)


def _scan_for_retired_key(text: str) -> list[int]:
    """Return the 1-based line numbers writing the retired metadata key."""
    return [
        lineno
        for lineno, line in enumerate(text.splitlines(), start=1)
        if any(pattern in line for pattern in _RETIRED_KEY_PATTERNS)
    ]


def test_the_retired_key_scanner_can_actually_fail() -> None:
    """The instrument's own falsifier — a scanner that never fires measures nothing.

    Exercised against both writer spellings, because the defect this test
    guards was written in the dict-literal form.
    """
    assert _scan_for_retired_key('    metadata["trace_id"] = tool_call_id\n') == [1]
    assert _scan_for_retired_key('x = 1\n    "trace_id": trace_id,\n') == [2]
    assert _scan_for_retired_key('    "tool_call_id": tool_call_id,\n') == []


def test_no_new_writer_of_the_retired_key() -> None:
    """Permanent falsifier: only the two sanctioned files may name ``trace_id``."""
    offenders: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in {"tests", ".venv", ".worktrees"}:
            continue
        if rel.as_posix() in _RETIRED_KEY_ALLOWED:
            continue
        offenders.extend(f"{rel}:{n}" for n in _scan_for_retired_key(path.read_text("utf-8")))
    assert offenders == [], (
        "'trace_id' is the retired spelling of the tool-call correlation key "
        f"(ss-console #2312); use CANONICAL_TOOL_CALL_KEY. Found at {offenders}"
    )


def test_transition_alias_carries_the_same_value_as_the_canonical_key() -> None:
    """Historical rows are only findable by the old name, so both are written.

    Rows written before #2312 carry ONLY ``trace_id``. Dropping it outright
    would leave a query that finds new rows and misses old ones — the same
    defect pointed the other way. The alias is retained on the per-tool path
    until the audit retention window clears the pre-fix rows.
    """
    mod = load_plugin("hermes-smd-audit")
    client = SqliteAuditClient()
    _emit_both_rows(mod, client)

    aliased = client.correlate("trace_id", TOOL_CALL_ID)
    assert len(aliased) == 1, "the per-tool row keeps the deprecated alias"
    md = json.loads(aliased[0]["metadata"])
    assert md["trace_id"] == md[CANONICAL_TOOL_CALL_KEY], (
        "alias and canonical key must hold the same value while both are written"
    )
