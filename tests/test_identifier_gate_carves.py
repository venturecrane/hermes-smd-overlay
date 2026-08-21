"""The identifier gate's per-tool carves and its ledger row (ss-console#2511).

Three changes to the outbound draft gate land here, and each of them is a change
to what the LEDGER says, not only to what the gate does. That is the reason they
share a file: after 2026-08-21 the question "what does the audit record claim
about this call?" is the one that got answered wrong.

1. **Two writes were never scanned at all.** `mcp_smokeball_add_file` carries its
   payload in `content_text` and `mcp_smokeball_render_docx_draft` in
   `draft_markdown`. Neither key was in `_BODY_ARG_KEYS` or `_DRAFT_SCAN_KEYS`,
   and neither tool was body-required, so `check_outbound_draft` found nothing to
   scan and returned `None`. The demand letter and the discovery responses went
   into the firm's file with no identifier check, and `read_document` then seeded
   every number in them straight back into the register as though read.

2. **They reported rather than refused, per tool, until the rate was read.** A
   demand letter is dense with figures and dates the firm authored elsewhere. The
   ss#2247 note in `outbound.py` records what happened the last time a gate
   started refusing that kind of content without its false-positive rate being
   read first: the agent deleted the wage rates from a letter so it would stage.
   So both tools measured and logged. `mode=report_tool` is deliberately not the
   same string as the operator-set `SMD_IDENTIFIER_GATE_MODE=report`, so a ledger
   reader can tell a per-tool posture from a whole-seat rollback.

   `mcp_smokeball_render_docx_draft` now BLOCKS, and the tests below pin that.
   Four pilot drafting lanes on 2026-08-21 (ss-console#2511,
   `vfy_01M0JG54ATP5ZA1TDTQJ6CEVWA`) put ten render calls through the gate for
   zero false positives and one genuine catch: computed response deadlines
   reached a filed Word draft while the same values were refused on the memo and
   on the email in the same turn. `mcp_smokeball_add_file` stays report-only, on
   the same reasoning as before, because no lane has exercised it yet.

3. **An executed internal write stopped being described as a draft.** Covered in
   `test_trust_enforce.py` alongside the rest of the decision vocabulary.
"""

from __future__ import annotations

import json

import pytest

from shared import provenance
from tests.conftest import load_plugin

SENTINEL = "ZZ-9999-0001"
UNREAD_BODY = f"Filed under matter {SENTINEL}, hearing 2027-03-04."

MATTER_BLOB = json.dumps({"id": "m1", "matterNumber": "2026-PI-101"})


class _FakeD1Client:
    """Records execute() calls so a test can read the row the gate wrote."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql: str, *params):
        self.calls.append((sql, params))
        return 1


@pytest.fixture
def gate(monkeypatch):
    """The trust plugin with a fake audit sink and a seeded session.

    The register is seeded from a real tenant read so no test here rides the
    empty-register carve; these are about the per-tool carve, and a test that
    passes for the wrong carve has measured the wrong thing.
    """
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    monkeypatch.delenv("SMD_IDENTIFIER_GATE_MODE", raising=False)
    plugin = load_plugin("hermes-smd-trust")
    provenance._reset_for_tests()
    fake = _FakeD1Client()
    plugin.outbound._AUDIT_CLIENT = fake
    plugin.outbound._AUDIT_CUSTOMER_SLUG = "acme"
    plugin.outbound._AUDIT_WIRED = True
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        result=MATTER_BLOB,
        session_id="s",
        tool_call_id="r",
    )
    yield plugin, fake
    provenance._reset_for_tests()


def _rows(fake: _FakeD1Client) -> list[str]:
    return [c[1][-1] for c in fake.calls if "IDENTIFIER_UNVERIFIED" in c[1]]


# ---------------------------------------------------------------------------
# The two unscanned writes
# ---------------------------------------------------------------------------


def test_add_file_is_measured_and_allowed(gate) -> None:
    """The half of the carve that survives: no lane has exercised add_file, so
    its false-positive rate is unread and it still measures rather than refuses."""
    plugin, fake = gate
    directive = plugin.outbound.check_outbound_draft(
        tool_name="mcp_smokeball_add_file",
        args={"content_text": UNREAD_BODY},
        session_id="s",
        tool_call_id="c",
    )
    assert directive is None, "the report carve must not refuse a filed document"
    rows = _rows(fake)
    assert len(rows) == 1, rows
    assert '"mode":"report_tool"' in rows[0]
    assert '"blocked":false' in rows[0]
    assert '"case_number"' in rows[0]
    # The row names shapes, never the value, on this path as on every other.
    assert SENTINEL not in rows[0]


def test_a_word_draft_refuses_an_unread_identifier(gate) -> None:
    """The half of the carve that ended: render_docx_draft blocks.

    Same body, same session, same unread case number as the add_file test above.
    Ten render calls across four pilot lanes on 2026-08-21 produced no false
    positive and one genuine catch, so the tool falls through to the ordinary
    mode resolution and refuses like a memo does. The ledger row has to say
    `mode=block` and not `mode=report_tool`, because a reader who sees the carve
    string on this tool is looking at a seat that never took the flip.
    """
    plugin, fake = gate
    directive = plugin.outbound.check_outbound_draft(
        tool_name="mcp_smokeball_render_docx_draft",
        args={"draft_markdown": UNREAD_BODY},
        session_id="s",
        tool_call_id="c",
    )
    assert directive is not None and directive["action"] == "block"
    rows = _rows(fake)
    assert len(rows) == 1, rows
    assert '"mode":"block"' in rows[0]
    assert '"blocked":true' in rows[0]
    assert '"case_number"' in rows[0]
    assert "report_tool" not in rows[0]
    assert SENTINEL not in rows[0]


def test_a_word_draft_refuses_a_seat_sourced_identifier(gate) -> None:
    """The seat-text path blocks on render_docx_draft too.

    A value the seat read out of its own instructions is the case the report
    carve used to swallow whole: `source=seat_text` was recorded and the draft
    still went into the firm's file. Pinned separately from the ordinary refusal
    because the two reach `should_block` by different routes, and the flip has to
    close both.
    """
    plugin, fake = gate
    plugin.on_post_tool_call(
        tool_name="read_file",
        result=f"The sentinel case number is {SENTINEL}.",
        session_id="s",
        tool_call_id="r2",
    )
    directive = plugin.outbound.check_outbound_draft(
        tool_name="mcp_smokeball_render_docx_draft",
        args={"draft_markdown": UNREAD_BODY},
        session_id="s",
        tool_call_id="c",
    )
    assert directive is not None and directive["action"] == "block"
    rows = _rows(fake)
    assert len(rows) == 1, rows
    assert '"mode":"block"' in rows[0]
    assert '"blocked":true' in rows[0]
    assert '"source":"seat_text"' in rows[0]
    assert SENTINEL not in rows[0]


def test_the_same_body_still_blocks_on_a_memo(gate) -> None:
    """The control that makes the carve a carve rather than a hole.

    Identical body, identical session, a tool that is not on the list. If this
    ever returns None the report carve has stopped being per tool.
    """
    plugin, fake = gate
    directive = plugin.outbound.check_outbound_draft(
        tool_name="mcp_smokeball_create_memo",
        args={"note": UNREAD_BODY},
        session_id="s",
        tool_call_id="c",
    )
    assert directive is not None and directive["action"] == "block"
    rows = _rows(fake)
    assert len(rows) == 1
    assert '"mode":"block"' in rows[0]
    assert '"blocked":true' in rows[0]


def test_report_tool_is_distinguishable_from_an_operator_rollback(gate, monkeypatch) -> None:
    """`mode` has to answer "why did this not block?" on its own.

    With the seat in rollback every row says `report`; with only the carved tool,
    exactly that row says `report_tool`. If the two shared a string, a ledger
    reader could not tell a per-tool posture from a seat whose gate someone had
    switched off during an incident.
    """
    plugin, fake = gate
    monkeypatch.setenv("SMD_IDENTIFIER_GATE_MODE", "report")
    plugin.outbound.check_outbound_draft(
        tool_name="mcp_smokeball_create_memo",
        args={"note": UNREAD_BODY},
        session_id="s",
        tool_call_id="c1",
    )
    plugin.outbound.check_outbound_draft(
        tool_name="mcp_smokeball_add_file",
        args={"content_text": UNREAD_BODY},
        session_id="s",
        tool_call_id="c2",
    )
    modes = [r.split('"mode":"')[1].split('"')[0] for r in _rows(fake)]
    assert modes == ["report", "report_tool"]


def test_the_report_carve_covers_exactly_one_tool() -> None:
    """Pinned as a set, so a second tool cannot be added without a decision.

    `mcp_smokeball_render_docx_draft` left this set on 2026-08-21 with its
    false-positive rate read. It is named here rather than merely absent, so a
    revert has to argue with a test instead of quietly re-widening the carve.
    """
    ob = load_plugin("hermes-smd-trust").outbound
    assert ob._REPORT_ONLY_DRAFT_TOOLS == frozenset({"mcp_smokeball_add_file"})
    assert "mcp_smokeball_render_docx_draft" not in ob._REPORT_ONLY_DRAFT_TOOLS
    # And the render TEMPLATE path was never among them: it composes from
    # authored fields rather than from a model-written markdown body.
    assert "mcp_smokeball_render_docx_template" not in ob._REPORT_ONLY_DRAFT_TOOLS


# ---------------------------------------------------------------------------
# The row the kill test reads
# ---------------------------------------------------------------------------


def test_a_seat_sourced_refusal_is_legible_in_the_ledger(gate) -> None:
    """`IDENTIFIER_UNVERIFIED blocked=true source=seat_text`, verbatim.

    The runtime AC on ss-console#2511 is scored off this row and no other
    signal: a refusal carrying `register_was_empty=true` and no source is a
    DIFFERENT outcome, and passing the kill test on it would certify the carve
    rather than the fix. So the field name and its value are pinned here as
    literals. Renaming either is a runtime-visible change and has to be a
    decision, not a refactor.
    """
    plugin, fake = gate
    plugin.on_post_tool_call(
        tool_name="read_file",
        result=f"The sentinel case number is {SENTINEL}.",
        session_id="s",
        tool_call_id="r2",
    )
    directive = plugin.outbound.check_outbound_draft(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"subject": "Self-test", "body": f"Internal note on matter {SENTINEL}."},
        session_id="s",
        tool_call_id="c",
    )
    assert directive is not None and directive["action"] == "block"

    rows = _rows(fake)
    assert len(rows) == 1, rows
    assert '"blocked":true' in rows[0]
    assert '"source":"seat_text"' in rows[0]
    assert '"gate_tier":"tier3_identifier"' in rows[0]
    # Still no raw value in the journal, on this path as on every other.
    assert SENTINEL not in rows[0]


def test_an_ordinary_unverified_refusal_claims_no_seat_source(gate) -> None:
    """The falsifier for the row above.

    Without this, an implementation that stamped `source=seat_text` on every
    refusal would satisfy the kill test while telling the auditor nothing.
    """
    plugin, fake = gate
    directive = plugin.outbound.check_outbound_draft(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"subject": "Update", "body": "Your matter is XX-1111-2222."},
        session_id="s",
        tool_call_id="c",
    )
    assert directive is not None and directive["action"] == "block"
    rows = _rows(fake)
    assert len(rows) == 1
    assert '"blocked":true' in rows[0]
    assert "seat_text" not in rows[0]


# ---------------------------------------------------------------------------
# Body-required drafts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    ["mcp_msgraph_mail_create_draft", "mcp_agentmail_create_draft"],
)
def test_a_mail_draft_with_no_body_fails_closed(gate, tool: str) -> None:
    """Both connectors make the body schema-required, so a call without one is a
    shape surprise. The gate must not wave through what it could not read."""
    plugin, _ = gate
    directive = plugin.outbound.check_outbound_draft(
        tool_name=tool,
        args={"to": "someone@example.com", "subject": "no body here"},
        session_id="s",
        tool_call_id="c",
    )
    assert directive is not None and directive["action"] == "block"
    assert "no recognizable body" in directive["message"]


def test_a_mail_draft_with_a_body_is_scanned_not_blocked_outright(gate) -> None:
    """The falsifier for the test above: body-required must not mean
    body-always-blocked."""
    plugin, _ = gate
    assert (
        plugin.outbound.check_outbound_draft(
            tool_name="mcp_msgraph_mail_create_draft",
            args={"subject": "Hello", "body": "Are you free to talk tomorrow?"},
            session_id="s",
            tool_call_id="c",
        )
        is None
    )
