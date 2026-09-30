"""The output checklist where each surface meets it (ss-console Option B, 2026-09-29).

Sends meet it at ``outbound.check_outbound_send``, the one choke point the
in-turn hook, the out-of-turn dispatcher and send_as all reach. File notes meet
it in ``check_outbound_draft`` (create_memo and update_memo), Word documents on
render_docx_draft's ``draft_markdown``. The scanner itself is tested in
``tests/test_output_checklist.py``.
"""

from __future__ import annotations

import json

import pytest

from shared import output_checklist, provenance, send_dispatch, spec_gate
from shared.spec_gate import TEMPLATED_BODY_ARG
from tests.conftest import load_plugin

_STAFF = "scott@smd.services"
_CLIENT = "jane@gmail.example"


class _FakeD1Client:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql: str, *params):
        self.calls.append((sql, params))
        return 1


@pytest.fixture
def trust_plugin(monkeypatch):
    plugin = load_plugin("hermes-smd-trust")
    ob = plugin.outbound
    ob._AUDIT_CLIENT = None
    ob._AUDIT_CUSTOMER_SLUG = None
    ob._AUDIT_WIRED = False
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    return plugin


@pytest.fixture(autouse=True)
def notices(monkeypatch):
    """The seat's out-of-turn sender, replaced by a recorder for every test here,
    so an exhausted staff send can never reach a real transport."""
    sent: list[dict] = []

    def fake_sender(**kwargs):
        sent.append(kwargs)
        return send_dispatch.DispatchResult(
            sent=True, message_id="m-notice", recipients=tuple(kwargs["to"])
        )

    monkeypatch.setattr(send_dispatch, "_SENDER", fake_sender)
    return sent


@pytest.fixture
def rows(monkeypatch):
    """The SPEC_GATE_TRIGGERED writer, wired to a fake."""
    fake = _FakeD1Client()
    monkeypatch.setattr(spec_gate, "_AUDIT_CLIENT", fake)
    monkeypatch.setattr(spec_gate, "_AUDIT_CUSTOMER_SLUG", "acme")
    monkeypatch.setattr(spec_gate, "_AUDIT_WIRED", True)
    return fake


@pytest.fixture
def rostered(trust_plugin, monkeypatch):
    enforce = trust_plugin.enforce
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: [_STAFF])
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: [(_CLIENT, "client")])
    yield


def _checklist_rows(fake: _FakeD1Client) -> list[dict]:
    out = []
    for _sql, params in fake.calls:
        if "SPEC_GATE_TRIGGERED" not in params:
            continue
        meta = json.loads(params[-1])
        if str(meta.get("reason", "")).startswith("output_checklist"):
            out.append(meta)
    return out


def _send(trust_plugin, text: str, session: str, **extra):
    args = {"to": [_STAFF], "subject": "Garcia hearing", "text": text, **extra}
    return trust_plugin.outbound.check_outbound_send(
        tool_name="smd_send_message", args=args, session_id=session, tool_call_id="c"
    )


# ---------------------------------------------------------------------------
# Sends
# ---------------------------------------------------------------------------


def test_a_staff_send_with_a_utc_clock_is_refused_with_the_fragment(trust_plugin, rostered, rows):
    result = _send(trust_plugin, "The Garcia hearing is at 16:30 in Dept 31.", "s-clock")
    assert result is not None and result["action"] == "block"
    assert result["message"].startswith("Refused:")
    assert "'16:30'" in result["message"]
    (row,) = _checklist_rows(rows)
    assert row["reason"] == "output_checklist"
    assert row["rules"] == "bare_clock"
    assert row["output_class"] == "staff"
    assert row["tool"] == "smd_send_message"
    assert "16:30" not in json.dumps(row)


def test_a_staff_digest_with_headings_and_list_items_goes_out(trust_plugin, rostered, rows):
    digest = (
        "## Needs you today\n"
        "- Garcia: hearing Tuesday at 9:30 a.m., Dept 31\n"
        "- **Nguyen**: records request is late\n"
    )
    # No matter numbers or dates here: nothing was read this session, so the
    # identifier gate (which runs first) would refuse them on its own grounds.
    assert _send(trust_plugin, digest, "s-digest") is None
    assert _checklist_rows(rows) == []


def test_a_templated_body_is_not_scanned(trust_plugin, rostered, rows):
    result = _send(
        trust_plugin, "Rendered by the repo at 16:30.", "s-tpl", **{TEMPLATED_BODY_ARG: True}
    )
    assert result is None
    assert _checklist_rows(rows) == []


def test_a_report_only_rule_writes_a_row_and_the_send_proceeds(trust_plugin, rostered, rows):
    body = "\n".join(f"Line {i} of the Garcia update." for i in range(21))
    assert _send(trust_plugin, body, "s-long") is None
    (row,) = _checklist_rows(rows)
    assert row["reason"] == "output_checklist" and row["rules"] == "max_lines"


def test_capitals_for_emphasis_refuse_a_staff_send(trust_plugin, rostered, rows):
    # Report-only until 2026-09-30; see output_checklist's module docstring.
    result = _send(trust_plugin, "URGENT: the Garcia file needs you today.", "s-caps")
    assert result["action"] == "block" and "URGENT" in result["message"]
    (row,) = _checklist_rows(rows)
    assert row["rules"] == "caps_emphasis"


def test_the_third_refusal_of_the_same_message_withholds_it(trust_plugin, rostered, rows):
    body = "The Garcia hearing is at 16:30."
    first = _send(trust_plugin, body, "s-loop")
    second = _send(trust_plugin, body, "s-loop")
    third = _send(trust_plugin, body, "s-loop")
    fourth = _send(trust_plugin, body, "s-loop")
    assert first["message"].startswith("Refused:")
    assert second["message"].startswith("Refused:")
    for held in (third, fourth):
        assert held is not None and held["action"] == "block"
        assert held["message"].startswith("Withheld:")
        assert "will not go out" in held["message"]
        assert "run output" in held["message"]
    reasons = [r["reason"] for r in _checklist_rows(rows)]
    assert reasons == [
        "output_checklist",
        "output_checklist",
        "output_checklist_exhausted",
        "output_checklist_exhausted",
    ]


def test_a_different_message_after_two_refusals_is_scanned_on_its_own(trust_plugin, rostered, rows):
    _send(trust_plugin, "The Garcia hearing is at 16:30.", "s-other")
    _send(trust_plugin, "The Garcia hearing is at 16:30.", "s-other")
    other = _send(trust_plugin, "The Nguyen call is at 17:15.", "s-other")
    assert other["message"].startswith("Refused:")
    clean = _send(trust_plugin, "The Nguyen call is at 5:15 p.m.", "s-other")
    assert clean is None


def test_a_client_send_meets_the_every_output_set(trust_plugin, rostered, rows):
    args = {"to": [_CLIENT], "subject": "Your hearing", "text": "Your hearing is at 16:30."}
    result = trust_plugin.outbound.check_outbound_send(
        tool_name="smd_send_message", args=args, session_id="s-client", tool_call_id="c"
    )
    assert result is not None and "'16:30'" in result["message"]
    (row,) = _checklist_rows(rows)
    assert row["output_class"] == "outbound_client"


def test_the_out_of_turn_dispatcher_is_refused_at_the_same_choke_point(trust_plugin, rostered):
    """``_dispatch_internal_message`` reaches check_outbound_send itself; the
    same text is refused there, and a templated one is not scanned."""
    ob = trust_plugin.outbound
    payload = {"to": [_STAFF], "subject": "Brief", "text": "Hearing at 16:30."}
    assert ob.check_outbound_send(
        tool_name="smd_send_message", args=dict(payload), session_id="s-oot"
    )["message"].startswith("Refused:")
    payload[TEMPLATED_BODY_ARG] = True
    assert (
        ob.check_outbound_send(tool_name="smd_send_message", args=payload, session_id="s-oot2")
        is None
    )


def test_the_hook_drops_a_model_supplied_templated_key(trust_plugin):
    args = {"to": [_STAFF], "text": "Hearing at 16:30.", TEMPLATED_BODY_ARG: True}
    trust_plugin.on_pre_tool_call(
        tool_name="smd_send_message", args=args, session_id="s-hook", tool_call_id="c"
    )
    assert TEMPLATED_BODY_ARG not in args


# ---------------------------------------------------------------------------
# File notes and Word documents
# ---------------------------------------------------------------------------


def _draft(trust_plugin, tool: str, args: dict, session: str):
    return trust_plugin.outbound.check_outbound_draft(
        tool_name=tool, args=args, session_id=session, tool_call_id="c"
    )


def test_a_memo_with_a_quoted_bold_label_is_left_to_the_connector(trust_plugin, rows):
    body = "> **What:** the Garcia motion was served.\n> **Next:** Nothing to do."
    assert (
        _draft(trust_plugin, "mcp_smokeball_create_memo", {"matter_id": "m", "text": body}, "s-m1")
        is None
    )
    assert _checklist_rows(rows) == []


@pytest.mark.parametrize("tool", ["mcp_smokeball_create_memo", "mcp_smokeball_update_memo"])
def test_a_memo_with_a_table_is_refused_on_either_memo_write(trust_plugin, rows, tool):
    args = {"matter_id": "m", "memo_id": "memo-1", "text": "| Exhibit | Page |\n| 1 | 4 |"}
    result = _draft(trust_plugin, tool, args, f"s-{tool}")
    assert result is not None and result["message"].startswith("Refused:")
    assert "file note" in result["message"]
    assert "render_docx_draft" in result["message"]
    (row,) = _checklist_rows(rows)
    assert row["rules"] == "pipe_table" and row["output_class"] == "record"
    assert row["tool"] == tool


def test_a_docx_with_a_caption_table_goes_through(trust_plugin, rows):
    markdown = "| SUPERIOR COURT | Case No. 24STCV01234 |\n|---|---|\n# EXHIBIT LIST\n1. Photos\n"
    result = _draft(
        trust_plugin,
        "mcp_smokeball_render_docx_draft",
        {"matter_id": "m", "draft_markdown": markdown, "file_name": "exhibits.docx"},
        "s-docx-ok",
    )
    assert result is None
    assert _checklist_rows(rows) == []


def test_a_docx_carrying_an_internal_id_is_refused(trust_plugin, rows):
    markdown = "| Caption | x |\nMatter a1b2c3d4-1111-2222-3333-444455556666\n"
    result = _draft(
        trust_plugin,
        "mcp_smokeball_render_docx_draft",
        {"matter_id": "m", "draft_markdown": markdown},
        "s-docx-id",
    )
    assert result is not None and result["message"].startswith("Refused:")
    assert "document" in result["message"]
    (row,) = _checklist_rows(rows)
    assert row["rules"] == "internal_id" and row["output_class"] == "work_product"


def test_update_memo_is_an_internal_write_the_draft_gate_covers(trust_plugin):
    assert "mcp_smokeball_update_memo" in trust_plugin.outbound.GATED_DRAFT_TOOLS


# ---------------------------------------------------------------------------
# The withheld notice: a held staff message is never silence
# ---------------------------------------------------------------------------


def test_an_exhausted_staff_send_tells_its_recipients_once(trust_plugin, rostered, rows, notices):
    body = "The Garcia hearing is at 16:30."
    results = [_send(trust_plugin, body, "s-notice") for _ in range(4)]
    assert [r["message"].split(":")[0] for r in results] == [
        "Refused",
        "Refused",
        "Withheld",
        "Withheld",
    ]
    assert len(notices) == 1
    (notice,) = notices
    assert notice["to"] == [_STAFF]
    assert notice["subject"] == "Withheld: Garcia hearing"
    assert notice["templated"] is True
    assert notice["session_id"] == "s-notice"
    assert "16:30" not in notice["text"]
    assert "a time that was not in the firm's local clock" in notice["text"]
    assert "no client was contacted" in notice["text"]
    assert "notice that a message was withheld went to" in results[2]["message"]


def test_the_notice_passes_the_staff_checklist_itself():
    ob = load_plugin("hermes-smd-trust").outbound
    every_rule = list(ob._NOTICE_PLAIN)
    for rules in [every_rule, ["bare_clock"], ["internal_id", "exclamation"]]:
        body = ob._notice_body(rules)
        assert output_checklist.check(body, output_checklist.STAFF_SEND) == []
        assert len(body.splitlines()) < 8
    assert output_checklist.check(ob._notice_subject("Garcia hearing"), "staff_send") == []


def test_a_subject_that_would_not_pass_is_not_repeated(trust_plugin, rostered, rows, notices):
    for _ in range(3):
        trust_plugin.outbound.check_outbound_send(
            tool_name="smd_send_message",
            args={"to": [_STAFF], "subject": "Hearing 16:30", "text": "Hearing at 16:30."},
            session_id="s-subj",
            tool_call_id="c",
        )
    (notice,) = notices
    assert notice["subject"] == "Withheld: an Operator message"


def test_a_different_body_exhausting_later_gets_its_own_notice(
    trust_plugin, rostered, rows, notices
):
    for _ in range(3):
        _send(trust_plugin, "The Garcia hearing is at 16:30.", "s-two")
    for _ in range(3):
        _send(trust_plugin, "The Nguyen call is at 17:15.", "s-two")
    assert len(notices) == 2


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("mcp_smokeball_create_memo", {"matter_id": "m", "text": "| a | b |\n| 1 | 2 |"}),
        (
            "mcp_smokeball_render_docx_draft",
            {"matter_id": "m", "draft_markdown": "a1b2c3d4-1111-2222-3333-444455556666"},
        ),
    ],
)
def test_a_note_or_document_withheld_sends_no_notice(trust_plugin, rows, notices, tool, args):
    results = [
        trust_plugin.outbound.check_outbound_draft(
            tool_name=tool, args=dict(args), session_id=f"s-nn-{tool}", tool_call_id="c"
        )
        for _ in range(3)
    ]
    assert results[2]["message"].startswith("Withheld:")
    assert notices == []


def test_a_client_send_withheld_sends_no_notice(trust_plugin, rostered, rows, notices):
    for _ in range(3):
        trust_plugin.outbound.check_outbound_send(
            tool_name="smd_send_message",
            args={"to": [_CLIENT], "subject": "Hearing", "text": "Your hearing is at 16:30."},
            session_id="s-client-nn",
            tool_call_id="c",
        )
    assert notices == []


def test_a_notice_can_never_raise_a_notice(monkeypatch):
    ob = load_plugin("hermes-smd-trust").outbound
    inner: list = []

    def reentrant(**kwargs):
        inner.append(
            ob._send_withheld_notice(
                to=kwargs["to"], cc=[], subject="x", rules=["bare_clock"], session_id="s"
            )
        )
        return send_dispatch.DispatchResult(sent=True, recipients=tuple(kwargs["to"]))

    monkeypatch.setattr(send_dispatch, "_SENDER", reentrant)
    outer = ob._send_withheld_notice(
        to=[_STAFF], cc=[], subject="Garcia", rules=["bare_clock"], session_id="s"
    )
    assert outer.sent is True
    assert len(inner) == 1 and inner[0].sent is False


def test_an_undeliverable_notice_is_said_in_the_hold(trust_plugin, rostered, rows, monkeypatch):
    monkeypatch.setattr(send_dispatch, "_SENDER", None)
    results = [_send(trust_plugin, "The Garcia hearing is at 16:30.", "s-nowire") for _ in range(3)]
    assert "did not go (this seat has no send path wired)" in results[2]["message"]
