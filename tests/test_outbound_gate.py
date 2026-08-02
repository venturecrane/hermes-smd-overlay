"""Tests for the ADR 0028 outbound provenance gate.

Three layers under test:

  1. ``shared.fabrication_markers`` — the HIGH_RISK_MARKERS registry loads,
     is non-empty, carries a version, and matches the banned strings.
  2. ``shared.outbound_gate.evaluate`` — the fail-closed policy core. Tier-1
     universal markers, Tier-2 law-vertical citations, most-restrictive on an
     indeterminate vertical, block when the filter raises.
  3. ``plugins/hermes-smd-trust`` integration — ``on_pre_tool_call`` blocks a
     draft tool whose body carries a fabricated citation / banned marker, and
     allows a clean body. A ``FABRICATION_FILTER_TRIGGERED`` audit row is
     written on block when the audit env is configured.
"""

import hashlib
import importlib
from pathlib import Path

import pytest

from shared import outbound_gate, provenance
from shared.fabrication_markers import FabricationMarkersError, load_markers
from tests.conftest import load_plugin

# ---------------------------------------------------------------------------
# Layer 1 — fabrication marker registry
# ---------------------------------------------------------------------------


# Canonical artifact provenance. The vendored shared/fabrication_markers.json is
# a byte-exact copy of the ss-console source of truth
# (operator/safety-substrate/fabrication_markers.json). This sha256 pins the
# vendored bytes so the two repos cannot silently drift.
#
# Pinned to the PR-B artifact at branch feat/aie-inbound-spine-0027
# (version 2026-05-29.2). TODO(PR-B-merge): when PR-B lands on main, re-pin this
# to the merged artifact's sha256 (it should not change if the file is
# unmodified at merge) and switch the loader's vendoring note to the pinned
# raw-URL on main.
_CANONICAL_MARKERS_SHA256 = "e666b2a24d2b4198db30ae8225ad252dbf6ace0acda8ce66f3a36ce8bad69142"

_VENDORED_MARKERS_PATH = (
    Path(__file__).resolve().parent.parent / "shared" / "fabrication_markers.json"
)


def test_markers_registry_non_empty_and_versioned() -> None:
    """The vendored registry must load, be non-empty, and carry a version.

    The loader fails closed on a missing/empty/all-malformed registry; this
    pins the structural invariant the gate depends on.
    """
    reg = load_markers()
    assert isinstance(reg.version, str) and reg.version
    assert len(reg.markers) > 0


def test_vendored_markers_match_canonical_sha256() -> None:
    """The vendored JSON must be byte-identical to the ss-console artifact.

    Strict cross-repo drift guard: if either repo edits the marker set, this
    fails until the vendored copy + this pin are updated together.
    """
    raw = _VENDORED_MARKERS_PATH.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    assert actual == _CANONICAL_MARKERS_SHA256, (
        "vendored shared/fabrication_markers.json drifted from the canonical "
        "ss-console artifact; re-vendor the exact bytes and update "
        f"_CANONICAL_MARKERS_SHA256 (expected {_CANONICAL_MARKERS_SHA256}, got {actual})"
    )


def test_markers_match_pattern_a_strings() -> None:
    reg = load_markers()
    assert reg.contains_marker("We'll reach out to schedule kickoff.")
    assert reg.contains_marker("Work begins within two weeks of signing.")
    assert reg.contains_marker("A 2-week stabilization period follows.")


def test_markers_match_em_dash_and_dollar() -> None:
    reg = load_markers()
    assert reg.contains_marker("This is great — really.")
    assert reg.contains_marker("The price is $2,500 for the engagement.")


def test_markers_clean_body_has_no_hit() -> None:
    reg = load_markers()
    clean = "Thanks for the note. I will review the files and follow up after that."
    assert reg.contains_marker(clean) is False
    assert reg.scan(clean) == []


def test_marker_scan_returns_ids_not_full_body() -> None:
    reg = load_markers()
    hits = reg.scan("We'll reach out soon and guarantee results.")
    ids = {h.marker_id for h in hits}
    assert "well-reach-out" in ids
    assert "guarantee" in ids
    # The hit carries the matched substring, never the whole body.
    for h in hits:
        assert h.match.lower() in "we'll reach out soon and guarantee results."


# ---------------------------------------------------------------------------
# Layer 2 — policy core: evaluate()
# ---------------------------------------------------------------------------


def test_evaluate_citation_at_law_vertical_blocks() -> None:
    d = outbound_gate.evaluate("Roe v. Wade, 410 U.S. 113", cohort=None, vertical="law-firm")
    assert d.allowed is False
    assert d.tier == "tier2_citation"
    assert d.evaluated_law_tier is True


def test_evaluate_pattern_a_string_blocks_at_any_vertical() -> None:
    for vertical in ("law-firm", "home-services", "retail", None, ""):
        d = outbound_gate.evaluate(
            "We'll reach out to schedule kickoff.", cohort=None, vertical=vertical
        )
        assert d.allowed is False, f"expected block at vertical={vertical!r}"
        assert d.tier == "tier1_marker"
        assert "well-reach-out" in d.marker_hits


def test_evaluate_clean_body_allows() -> None:
    d = outbound_gate.evaluate(
        "Thanks for your message. I will look into the files and circle back.",
        cohort=None,
        vertical="law-firm",
    )
    assert d.allowed is True
    assert d.audit_action == outbound_gate.AUDIT_ALLOW


def test_evaluate_indeterminate_vertical_runs_most_restrictive() -> None:
    """vertical=None must run BOTH tiers — a citation blocks even with no vertical."""
    d = outbound_gate.evaluate("Roe v. Wade, 410 U.S. 113", cohort=None, vertical=None)
    assert d.allowed is False
    assert d.tier == "tier2_citation"
    assert d.evaluated_law_tier is True


def test_evaluate_citation_at_non_law_vertical_skips_tier2() -> None:
    """A positively-identified non-law vertical skips the citation tier.

    The body has a citation but NO banned marker; under a non-law cohort the
    Tier-2 scan does not run, so the body is allowed. (Tier-1 markers still
    apply universally — see the pattern-A test above.)
    """
    d = outbound_gate.evaluate(
        "Per Roe v. Wade, 410 U.S. 113, the rule applies.",
        cohort=None,
        vertical="home-services",
    )
    assert d.allowed is True
    assert d.evaluated_law_tier is False


def test_evaluate_blocks_when_citation_filter_raises(monkeypatch) -> None:
    """If the citation filter raises, the gate must BLOCK (fail-closed)."""

    def boom(_text: str) -> bool:
        raise RuntimeError("synthetic citation-filter failure")

    monkeypatch.setattr(outbound_gate.citation_filter, "contains_citation", boom)
    d = outbound_gate.evaluate(
        "Some clean-looking body with no markers.", cohort=None, vertical="law-firm"
    )
    assert d.allowed is False
    assert d.tier == "tier2_citation"


def test_evaluate_blocks_when_marker_registry_raises(monkeypatch) -> None:
    """If the marker registry cannot load, the gate must BLOCK (fail-closed)."""

    def boom() -> None:
        raise FabricationMarkersError("synthetic registry load failure")

    monkeypatch.setattr(outbound_gate, "load_markers", boom)
    d = outbound_gate.evaluate("anything", cohort=None, vertical=None)
    assert d.allowed is False
    assert d.tier == "load_error"


def test_evaluate_empty_body_blocks() -> None:
    d = outbound_gate.evaluate("", cohort=None, vertical="law-firm")
    assert d.allowed is False
    assert d.tier == "load_error"


def test_evaluate_cohort_none_does_not_relax() -> None:
    """An indeterminate cohort must not relax any tier (most-restrictive)."""
    d = outbound_gate.evaluate("Roe v. Wade, 410 U.S. 113", cohort=None, vertical=None)
    assert d.allowed is False


# ---------------------------------------------------------------------------
# Layer 3 — plugin integration: on_pre_tool_call + check_outbound_draft
# ---------------------------------------------------------------------------


@pytest.fixture
def trust_plugin():
    """Load the trust plugin fresh and reset the outbound audit cache."""
    plugin = load_plugin("hermes-smd-trust")
    ob = plugin.outbound
    # Reset the lazily-cached audit client so each test starts clean.
    ob._AUDIT_CLIENT = None
    ob._AUDIT_CUSTOMER_SLUG = None
    ob._AUDIT_WIRED = False
    return plugin


@pytest.fixture
def env_autonomous(trust_plugin, monkeypatch):
    """Grant the active persona autonomous internal_write exposure so the trust
    layer allows the draft (INTERNAL_WRITE) and the outbound provenance gate runs.
    Patches the SAME plugin module object the test drives (fixture caching gives
    both this fixture and the test one trust_plugin instance)."""
    enforce = trust_plugin.enforce
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.INTERNAL_WRITE: enforce.Ceiling.AUTONOMOUS},
    )
    yield


class _FakeD1Client:
    """Records execute() calls so tests can assert an audit row was written."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql: str, *params):
        self.calls.append((sql, params))
        return 1


def test_gated_draft_tools_cover_expected_set() -> None:
    ob = load_plugin("hermes-smd-trust").outbound
    # Body-required prose drafts must all be gated.
    for name in (
        "email_create_draft",
        "email_update_draft",
        "sms_create_draft",
        "practice_management_create_note",
    ):
        assert name in ob.GATED_DRAFT_TOOLS
    # The pure delete is NOT gated.
    assert "email_delete_draft" not in ob.GATED_DRAFT_TOOLS
    # Read tools are never gated.
    assert "email_list_messages" not in ob.GATED_DRAFT_TOOLS


def test_draft_gate_scans_html_key(monkeypatch) -> None:
    """EFF-01: a draft whose body is under the AgentMail `html` key must be
    scanned (previously only html_body/text were recognized, so an html-only
    fabricated citation slipped through)."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    ob = load_plugin("hermes-smd-trust").outbound
    blocked = ob.check_outbound_draft(
        tool_name="mcp_agentmail_create_draft",
        args={"html": "As held in Mata v. Avianca, 925 F.3d 1339, you prevail."},
    )
    assert blocked is not None and blocked["action"] == "block"


def test_send_gate_blocks_fabricated_citation(monkeypatch) -> None:
    """EFF-01: an autonomous EXTERNAL_SEND delivers content unreviewed, so a
    fabricated legal citation in its (html) body must block the send."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    ob = load_plugin("hermes-smd-trust").outbound
    blocked = ob.check_outbound_send(
        tool_name="mcp_agentmail_send_message",
        args={"subject": "Re: your matter", "html": "Per Roe v. Wade, 410 U.S. 113."},
    )
    assert blocked is not None and blocked["action"] == "block"


def test_send_gate_blocks_tier1_marker_regardless_of_vertical(monkeypatch) -> None:
    """A Tier-1 fabrication marker blocks a send in any vertical."""
    monkeypatch.delenv("SMD_VERTICAL", raising=False)
    ob = load_plugin("hermes-smd-trust").outbound
    blocked = ob.check_outbound_send(
        tool_name="mcp_agentmail_send_message",
        args={"text": "We will reach out — soon."},  # em dash: banned marker
    )
    assert blocked is not None and blocked["action"] == "block"


def test_send_gate_allows_clean_body(monkeypatch) -> None:
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    ob = load_plugin("hermes-smd-trust").outbound
    assert (
        ob.check_outbound_send(
            tool_name="mcp_agentmail_send_message",
            args={"subject": "Hi", "text": "Are you free to talk tomorrow?"},
        )
        is None
    )
    # A read tool is never a gated send.
    assert ob.check_outbound_send(tool_name="mcp_agentmail_list_messages", args={}) is None


def test_pre_tool_call_blocks_draft_with_fabricated_citation(
    trust_plugin, env_autonomous, monkeypatch
) -> None:
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Per Roe v. Wade, 410 U.S. 113, you have a strong claim."},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert result["message"].startswith("Refused:")


def test_pre_tool_call_blocks_draft_with_pattern_a_marker(
    trust_plugin, env_autonomous, monkeypatch
) -> None:
    # Any vertical — Tier-1 markers are universal.
    monkeypatch.setenv("SMD_VERTICAL", "home-services")
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "We'll reach out to schedule kickoff."},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert isinstance(result, dict)
    assert result["action"] == "block"


def test_pre_tool_call_allows_clean_draft(trust_plugin, env_autonomous, monkeypatch) -> None:
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Thank you for the documents. I will review them and follow up."},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert result is None


def test_pre_tool_call_body_required_tool_missing_body_blocks(trust_plugin, env_autonomous) -> None:
    """A create-draft with no recognizable body key fails closed (block)."""
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"subject": "Hello"},  # no body key
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "failing closed" in result["message"].lower()


def test_pre_tool_call_body_optional_tool_missing_body_allows(trust_plugin, env_autonomous) -> None:
    """A body-optional draft (calendar) with structured-only args is allowed."""
    result = trust_plugin.on_pre_tool_call(
        tool_name="calendar_create_event_draft",
        args={"start": "2026-06-01T10:00:00Z", "end": "2026-06-01T11:00:00Z"},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert result is None


def test_pre_tool_call_non_draft_tool_not_gated(trust_plugin, env_autonomous) -> None:
    """A READ tool flows through the ceiling check and is not gated by outbound."""
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_list_messages",
        args={"body": "We'll reach out — irrelevant, this isn't a draft."},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert result is None


def test_block_emits_fabrication_audit_row(trust_plugin, env_autonomous, monkeypatch) -> None:
    """On block, a FABRICATION_FILTER_TRIGGERED row is written via D1Client."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    fake = _FakeD1Client()
    ob = trust_plugin.outbound
    # Pre-wire the audit cache with a fake client so emission runs.
    ob._AUDIT_CLIENT = fake
    ob._AUDIT_CUSTOMER_SLUG = "acme"
    ob._AUDIT_WIRED = True

    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Roe v. Wade, 410 U.S. 113 applies here."},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert result["action"] == "block"
    assert len(fake.calls) == 1
    sql, params = fake.calls[0]
    assert "INSERT INTO audit_log" in sql
    assert "FABRICATION_FILTER_TRIGGERED" in params
    # The metadata blob (last param) must NOT contain the draft body prose.
    metadata_json = params[-1]
    assert "Roe v. Wade" not in metadata_json
    assert "410 U.S. 113" not in metadata_json
    assert "fabrication_filter" in metadata_json


# ---------------------------------------------------------------------------
# Layer 3b — A1 identifier-integrity gate (REPORT-ONLY, never blocks)
# ---------------------------------------------------------------------------


def _wire_fake_audit(ob) -> "_FakeD1Client":
    fake = _FakeD1Client()
    ob._AUDIT_CLIENT = fake
    ob._AUDIT_CUSTOMER_SLUG = "acme"
    ob._AUDIT_WIRED = True
    return fake


def _identifier_rows(fake: "_FakeD1Client") -> list:
    return [c for c in fake.calls if "IDENTIFIER_UNVERIFIED" in c[1]]


def test_unverified_identifier_allows_and_reports(
    trust_plugin, env_autonomous, monkeypatch
) -> None:
    """A clean draft carrying an identifier the agent never read is ALLOWED
    (report-only never blocks) and emits one IDENTIFIER_UNVERIFIED row."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)

    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Your alien number on file is A123456789."},
        task_id="t",
        session_id="sess-rep",
        tool_call_id="c",
    )
    assert result is None  # report-only NEVER blocks
    rows = _identifier_rows(fake)
    assert len(rows) == 1
    metadata_json = rows[0][1][-1]
    assert "tier3_identifier" in metadata_json
    # redaction: the raw A-number must NOT be in the row
    assert "A123456789" not in metadata_json
    assert "a_number" in metadata_json


def test_verified_identifier_does_not_report(trust_plugin, env_autonomous, monkeypatch) -> None:
    """An identifier the agent READ this session (recorded via post_tool_call)
    verifies — no IDENTIFIER_UNVERIFIED row."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)

    # The agent reads a matter record carrying the A-number.
    trust_plugin.on_post_tool_call(
        tool_name="email_list_messages",
        result="Matter for client; alien number A123456789 on file.",
        session_id="sess-ver",
        tool_call_id="r",
    )
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Confirming your alien number A123456789."},
        task_id="t",
        session_id="sess-ver",
        tool_call_id="c",
    )
    assert result is None
    assert _identifier_rows(fake) == []  # verified → nothing reported


def test_report_never_blocks_with_empty_register(trust_plugin, env_autonomous, monkeypatch) -> None:
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    result = trust_plugin.on_pre_tool_call(
        tool_name="practice_management_create_note",
        args={"note": "Hearing set for June 8, 2026; ref A999999999."},
        task_id="t",
        session_id="sess-empty",
        tool_call_id="c",
    )
    assert result is None  # never blocks even with everything unverified


def test_names_alone_do_not_report(trust_plugin, env_autonomous, monkeypatch) -> None:
    """A greeting recipient name is excluded from the runtime report (the
    register holds no names in v1) — a draft with only an unverified name and no
    structured identifier emits no IDENTIFIER_UNVERIFIED row."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Dear Robert Smith,\n\nThank you for your patience."},
        task_id="t",
        session_id="sess-name",
        tool_call_id="c",
    )
    assert result is None
    assert _identifier_rows(fake) == []


# ---------------------------------------------------------------------------
# Layer 3c — structured-arg identifier scan (#2132 / ss#2171 PR 1a)
#
# Before _DRAFT_SCAN_KEYS, mcp_smokeball_create_event matched zero body keys
# and exited the gate unscanned, and create_task's scan truncated at `note` —
# a fabricated hearing date in start_time or a fabricated matter number in a
# subject line was invisible. These tests pin the widened identifier surface
# AND that the Tier-1/2 evaluate() scope did NOT widen with it.
# ---------------------------------------------------------------------------


def test_create_event_structured_args_reach_identifier_scan(
    trust_plugin, env_autonomous, monkeypatch
) -> None:
    """A structured-only create_event with an unread start_time date is still
    ALLOWED (report mode) but now emits an IDENTIFIER_UNVERIFIED row — the
    #2132 surface is visible."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    result = trust_plugin.on_pre_tool_call(
        tool_name="mcp_smokeball_create_event",
        args={
            "subject": "Hearing",
            "start_time": "2026-09-14T09:00:00",
            "end_time": "2026-09-14T10:00:00",
            "attendees": ["s-1"],
            "time_zone": "America/Los_Angeles",
        },
        task_id="t",
        session_id="sess-2132-event",
        tool_call_id="c",
    )
    assert result is None  # report mode never blocks
    rows = _identifier_rows(fake)
    assert len(rows) == 1
    metadata_json = rows[0][1][-1]
    assert "date" in metadata_json
    # redaction: the raw date value must not appear in the audit row
    assert "2026-09-14" not in metadata_json


def test_create_task_subject_and_due_date_scanned_past_note(
    trust_plugin, env_autonomous, monkeypatch
) -> None:
    """create_task carries a `note` (the old first-match scan stopped there);
    the unread due_date must still be seen and reported."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    result = trust_plugin.on_pre_tool_call(
        tool_name="mcp_smokeball_create_task",
        args={
            "staff_id": "s-1",
            "subject": "Serve responses",
            "note": "No identifiers here.",
            "due_date": "2026-10-02",
        },
        task_id="t",
        session_id="sess-2132-task",
        tool_call_id="c",
    )
    assert result is None
    rows = _identifier_rows(fake)
    assert len(rows) == 1
    assert "date" in rows[0][1][-1]


def test_structured_args_all_read_do_not_report(
    trust_plugin, env_autonomous, monkeypatch
) -> None:
    """FALSE CONTROL (Law 12): the same structured create_event whose date the
    agent actually READ this session emits NO row — the widened scan must be
    able to pass, or the report above measures nothing."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    trust_plugin.on_post_tool_call(
        tool_name="email_list_messages",
        result="Hearing scheduled 2026-09-14T09:00:00 per the court notice.",
        session_id="sess-2132-read",
        tool_call_id="r",
    )
    result = trust_plugin.on_pre_tool_call(
        tool_name="mcp_smokeball_create_event",
        args={
            "subject": "Hearing",
            "start_time": "2026-09-14T09:00:00",
            "end_time": "2026-09-14T10:00:00",
            "attendees": ["s-1"],
            "time_zone": "America/Los_Angeles",
        },
        task_id="t",
        session_id="sess-2132-read",
        tool_call_id="c",
    )
    assert result is None
    assert _identifier_rows(fake) == []


def test_evaluate_scope_stays_prose_only(trust_plugin, env_autonomous, monkeypatch) -> None:
    """The Tier-1/2 marker/citation gate keeps scanning the PROSE BODY alone —
    the #2132 widening feeds only the identifier check. Widening an
    already-blocking gate to subject lines would be a separate, measured
    decision (ss#2171 critique issue 2)."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    ob = trust_plugin.outbound
    seen: list[str] = []
    real_evaluate = ob.evaluate

    def spy(body, *a, **kw):
        seen.append(body)
        return real_evaluate(body, *a, **kw)

    monkeypatch.setattr(ob, "evaluate", spy)
    trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"subject": "Re: status", "body": "All quiet this week."},
        task_id="t",
        session_id="sess-scope",
        tool_call_id="c",
    )
    assert seen == ["All quiet this week."]  # body only — no subject concatenation


def test_post_tool_call_ignores_non_read_tools(trust_plugin, monkeypatch) -> None:
    """Only READ tools establish provenance — a write tool's result is not
    recorded into the register."""
    provenance._reset_for_tests()
    trust_plugin.on_post_tool_call(
        tool_name="email_create_draft",  # INTERNAL_WRITE, not a read
        result="A123456789 should not be recorded from a write.",
        session_id="sess-nr",
        tool_call_id="r",
    )
    assert not bool(provenance.register_for("sess-nr"))


def test_audit_write_failure_still_blocks(trust_plugin, env_autonomous, monkeypatch) -> None:
    """If the audit write raises, the BLOCK still stands (audit is best-effort)."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")

    class _RaisingClient:
        def execute(self, *_a, **_k):
            raise RuntimeError("d1 down")

    ob = trust_plugin.outbound
    ob._AUDIT_CLIENT = _RaisingClient()
    ob._AUDIT_CUSTOMER_SLUG = "acme"
    ob._AUDIT_WIRED = True

    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Roe v. Wade, 410 U.S. 113 applies here."},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert result["action"] == "block"


def test_ceiling_refusal_short_circuits_before_gate(trust_plugin, monkeypatch) -> None:
    """A fail-closed exposure (no internal_write authored) blocks first; the
    outbound gate is never consulted."""
    enforce = trust_plugin.enforce
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", lambda slug="": {})
    # Spy on the gate to confirm it is not reached.
    called = {"v": False}
    ob = trust_plugin.outbound
    real = ob.check_outbound_draft

    def spy(**kwargs):
        called["v"] = True
        return real(**kwargs)

    monkeypatch.setattr(ob, "check_outbound_draft", spy)
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "clean body"},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert called["v"] is False


def test_module_imports_are_stable() -> None:
    """The shared modules import cleanly (no circular import regressions)."""
    importlib.import_module("shared.outbound_gate")
    importlib.import_module("shared.fabrication_markers")
    importlib.import_module("shared.citation_filter")
