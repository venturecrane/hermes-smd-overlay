"""A ``completed`` casework row and the audit row that witnesses it name one session.

The broker accepts ``completed`` only when its audit log holds a successful
``mcp_smokeball_update_task`` TOOL_CALL_COMPLETED row whose ``metadata.tool_call_id``
equals the row's ``tool_call_id`` and whose ``metadata.session_id`` equals the
row's ``session_id`` (ss-console ``operator/workspace_broker/casework_verbs.py``,
``update_task_witnessed``). The audit plugin stamps the RAW ``post_tool_call``
session id (``plugins/hermes-smd-audit/__init__.py`` on_post_tool_call ->
``emit.py`` ``metadata["session_id"]``), and ships the row through the broker
client (``shared/audit_client.py`` ``audit_append``), so it lands in the broker's
own audit DB. The replay queue, by contrast, is keyed by the provenance-resolved
id. This pins that the completed row carries the RAW id, the one the witness
compares, even where the two differ.
"""

from __future__ import annotations

import json

from shared import casework_acts, casework_ledger, provenance
from shared.casework_acts import CASEWORK_ACTS, TaskWrite
from tests.conftest import load_plugin

RAW = "raw-hook-session"
RESOLVED = "resolved-session"


def test_the_completed_row_names_the_session_the_audit_row_names(monkeypatch):
    audit = load_plugin("hermes-smd-audit")
    trust = load_plugin("hermes-smd-trust")
    written: list = []

    class Writer:
        def write(self, event):
            written.append(event)
            return "ulid"

    monkeypatch.setattr(audit, "_WRITER", Writer())
    monkeypatch.setattr(audit, "_CUSTOMER_SLUG", "pilot")
    monkeypatch.setattr(provenance, "resolve_session", lambda sid: RESOLVED if sid else sid)
    rows: list[dict] = []
    CASEWORK_ACTS.set_writer(lambda event: rows.append(event) or {"ok": True})
    try:
        CASEWORK_ACTS.clear(RESOLVED)
        key = casework_ledger.item_key(matter_id="m-1", kind="task", source_id="t-1")
        CASEWORK_ACTS.load(
            RESOLVED,
            [
                TaskWrite(
                    task_id="t-1",
                    staff_id="s-1",
                    item_key=key,
                    matter_id="m-1",
                    skill="task-list-keeper",
                    is_completed=True,
                )
            ],
        )
        CASEWORK_ACTS.replay(RESOLVED, casework_acts.UPDATE_TASK_TOOL, {})
        kwargs = {
            "tool_name": casework_acts.UPDATE_TASK_TOOL,
            "args": {"task_id": "t-1"},
            "result": json.dumps({"id": "t-1"}),
            "session_id": RAW,
            "tool_call_id": "call-77",
        }
        audit.on_post_tool_call(**kwargs)
        trust.on_post_tool_call(**kwargs)
    finally:
        CASEWORK_ACTS.set_writer(None)
        CASEWORK_ACTS.clear(RESOLVED)

    [audit_row] = [e for e in written if (e.metadata or {}).get("tool_call_id") == "call-77"]
    [completed] = [r for r in rows if r["event"] == "completed"]
    meta = audit_row.metadata
    # The three keys the broker's witness reads, pairwise equal.
    assert meta["tool"] == casework_ledger.UPDATE_TASK_TOOL
    assert meta["outcome"] == "ok"
    assert completed["tool_call_id"] == meta["tool_call_id"] == "call-77"
    assert completed["session_id"] == meta["session_id"] == RAW
