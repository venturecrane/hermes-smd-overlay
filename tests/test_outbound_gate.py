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
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shared import outbound_gate, pre_run_handoff, provenance
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
# Pinned to version 2026-08-22.1 (ss-console#2552 added the
# record-narration-about-a-person marker). TODO(PR-B-merge): switch the loader's
# vendoring note to the pinned raw-URL on main.
#
# NOTE on what this guard does NOT cover. It hashes the VENDORED file against a
# constant in this repo; it never reads ss-console. So it catches a vendored edit
# that forgot to update the pin, and it does NOT catch the canonical artifact and
# the vendored copy drifting apart. Updating both repos in the same change is a
# discipline requirement, not something CI enforces.
_CANONICAL_MARKERS_SHA256 = "18230fbb712afe6279913af65864102b6b8e18939ea96b59cf16da9ffe0c2746"

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


# ---------------------------------------------------------------------------
# record-narration-about-a-person (ss-console#2552)
#
# The Operator speaks about what it will do, never about what it holds on you.
# A colleague who states a preference gets "here is what I will do", not "that is
# recorded to your profile" — the notebook is fine, announcing it is not.
# ---------------------------------------------------------------------------


def test_marker_catches_the_live_trigger_sentence() -> None:
    """The exact sentence that shipped to Scott, and its active-voice variants."""
    reg = load_markers()
    assert reg.contains_marker(
        "That preference is recorded to your profile and applies from this turn forward."
    )
    assert reg.contains_marker("I've added that to your profile.")
    assert reg.contains_marker("Saved to your profile.")
    assert reg.contains_marker("I have updated your preferences.")
    assert reg.contains_marker("Your profile has been updated with this.")
    assert reg.contains_marker("I keep a profile on you for this.")


def test_marker_does_not_hit_attributed_firm_prose() -> None:
    """A firm's own letter says "your file" and "profile" legitimately.

    This is why the marker's nouns are scoped to profile/preferences and exclude
    file/record: an earlier draft matched "added ... your file" and would have
    refused a real PI letter. A gate that cannot be satisfied honestly teaches
    the model to satisfy it dishonestly (the 2026-08-11 staging incident).
    """
    reg = load_markers()
    assert reg.contains_marker("We have added your medical records to your file.") is False
    assert reg.contains_marker("Please review your file before the hearing.") is False
    assert reg.contains_marker("The claimant profile shows three prior injuries.") is False
    assert reg.contains_marker("Your driving record was requested from MVD.") is False


def test_marker_does_not_block_pull_side_disclosure() -> None:
    """Asked what it knows, the Operator answers in full.

    The rule bans volunteering, not disclosure. Refusing to say what is held
    would be the genuinely sinister posture, so an over-broad marker is a defect
    in the other direction — this is that falsifier.
    """
    reg = load_markers()
    assert reg.contains_marker("You told me you prefer open tasks first.") is False
    assert reg.contains_marker("I'll remember that.") is False
    assert (
        reg.contains_marker("Here's what I have for you: open tasks first, completed last.")
        is False
    )
    assert (
        reg.contains_marker("Open tasks first, completed tasks last under a Completed heading.")
        is False
    )


def test_marker_known_paraphrase_limits_are_pinned() -> None:
    """The marker catches this VOCABULARY, not the behavior.

    Paraphrase evades it. These are asserted as misses on purpose so the limit is
    a recorded fact rather than a later surprise: the capture nudge is what
    generalizes, and the nudge is an instruction, not a control. If one of these
    starts matching, that is an improvement — update this test deliberately.
    """
    reg = load_markers()
    assert reg.contains_marker("I'll note that in your file.") is False
    assert reg.contains_marker("It's in your preferences now.") is False
    assert reg.contains_marker("I've made a note of how you like this.") is False


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


def test_establishment_staging_is_exempt_but_submission_is_not() -> None:
    """ss #2247: the draft gate scans AGENT-COMPOSED text.

    ``establish_stage_document`` carries the firm's own document, read in place
    and staged byte for byte, so scanning it for fabrication asks whether the
    firm fabricated its own letter. On the first live run the gate refused the
    firm's demand letter (dollar figures) and trial binder (dates), and the
    agent deleted the figures to make it stage.

    ``establish_submit`` is the other half of the pair and MUST stay gated: its
    ``spec_body`` is composed by the agent. Both directions are asserted here,
    because an exemption written without its falsifier is how the whole gate
    quietly goes away.
    """
    ob = load_plugin("hermes-smd-trust").outbound
    assert "establish_stage_document" not in ob.GATED_DRAFT_TOOLS
    assert "establish_submit" in ob.GATED_DRAFT_TOOLS


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
# Layer 3b — A1 identifier-integrity gate (REFUSING, ss #2171)
#
# The gate BLOCKS an unverified identifier (every kind except NAME). Carves:
# ambient dates (utc today/yesterday verify against the clock), and the
# empty-register carve on the DRAFT gate only — the SEND gate blocks even with
# an empty register. SMD_IDENTIFIER_GATE_MODE=report is the operator-only
# downgrade; anything else (including garbage) blocks.
# ---------------------------------------------------------------------------


def _wire_fake_audit(ob) -> "_FakeD1Client":
    fake = _FakeD1Client()
    ob._AUDIT_CLIENT = fake
    ob._AUDIT_CUSTOMER_SLUG = "acme"
    ob._AUDIT_WIRED = True
    return fake


def _identifier_rows(fake: "_FakeD1Client") -> list:
    return [c for c in fake.calls if "IDENTIFIER_UNVERIFIED" in c[1]]


def _seed_unrelated_read(trust_plugin, session_id: str) -> None:
    """Put ONE unrelated identifier in the session register so it is non-empty
    (the empty-register carve must not be what a blocking test exercises)."""
    trust_plugin.on_post_tool_call(
        tool_name="email_list_messages",
        result="Unrelated matter; alien number A555555555 on file.",
        session_id=session_id,
        tool_call_id="seed",
    )


def test_unverified_identifier_blocks(trust_plugin, env_autonomous, monkeypatch) -> None:
    """A clean draft carrying an identifier the agent never read is REFUSED
    (register non-empty) and emits one IDENTIFIER_UNVERIFIED row with
    mode=block / blocked=true."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, "sess-blk")

    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Your alien number on file is A123456789."},
        task_id="t",
        session_id="sess-blk",
        tool_call_id="c",
    )
    assert result is not None and result["action"] == "block"
    # The refusal instructs re-read-or-remove and names the kind, not the value's mechanics.
    assert "a_number" in result["message"]
    assert "Re-read" in result["message"]
    rows = _identifier_rows(fake)
    assert len(rows) == 1
    metadata_json = rows[0][1][-1]
    assert "tier3_identifier" in metadata_json
    assert '"mode":"block"' in metadata_json
    assert '"blocked":true' in metadata_json
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


def test_empty_register_draft_allows_and_reports(trust_plugin, env_autonomous, monkeypatch) -> None:
    """The DRAFT-gate empty-register carve: nothing read this session → a draft
    with unverified identifiers is allowed (a refusal with no source to re-read
    is a brick) but the row records the bypass so the carve stays measurable."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    result = trust_plugin.on_pre_tool_call(
        tool_name="practice_management_create_note",
        args={"note": "Hearing set for June 8, 2026; ref A999999999."},
        task_id="t",
        session_id="sess-empty",
        tool_call_id="c",
    )
    assert result is None  # draft-gate carve: empty register allows
    rows = _identifier_rows(fake)
    assert len(rows) == 1
    metadata_json = rows[0][1][-1]
    assert '"register_was_empty":true' in metadata_json
    assert '"blocked":false' in metadata_json
    assert '"block_bypass":"register_empty"' in metadata_json


def test_send_gate_blocks_with_empty_register(trust_plugin, monkeypatch) -> None:
    """The SEND gate gets NO empty-register carve: an autonomous external send
    composed with nothing read is exactly 'cannot verify' → block."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    ob = trust_plugin.outbound
    result = ob.check_outbound_send(
        tool_name="mcp_agentmail_send_message",
        args={"subject": "Update", "text": "Your hearing is set for June 8, 2026."},
        session_id="sess-send-empty",
        tool_call_id="c",
    )
    assert result is not None and result["action"] == "block"
    rows = _identifier_rows(fake)
    assert len(rows) == 1
    metadata_json = rows[0][1][-1]
    assert '"blocked":true' in metadata_json
    assert '"register_was_empty":true' in metadata_json


def test_send_gate_blocks_unread_identifier(trust_plugin, monkeypatch) -> None:
    """The send path blocks an unread identifier with a NON-empty register too —
    the highest-stakes surface gets its own pin, not a ride on the draft tests."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, "sess-send-blk")
    ob = trust_plugin.outbound
    result = ob.check_outbound_send(
        tool_name="mcp_agentmail_send_message",
        args={"subject": "Update", "text": "Your case number is 1:24-cv-01234."},
        session_id="sess-send-blk",
        tool_call_id="c",
    )
    assert result is not None and result["action"] == "block"


def test_env_report_mode_downgrades(trust_plugin, env_autonomous, monkeypatch) -> None:
    """SMD_IDENTIFIER_GATE_MODE=report is the operator rollback lever: the gate
    keeps reporting (telemetry continuity) but allows."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    monkeypatch.setenv("SMD_IDENTIFIER_GATE_MODE", "report")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, "sess-dwn")
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Your alien number on file is A123456789."},
        task_id="t",
        session_id="sess-dwn",
        tool_call_id="c",
    )
    assert result is None
    rows = _identifier_rows(fake)
    assert len(rows) == 1
    metadata_json = rows[0][1][-1]
    assert '"mode":"report"' in metadata_json
    assert '"blocked":false' in metadata_json


@pytest.mark.parametrize("value", ["disabled", "off", "false", "0", "no", "block"])
def test_env_garbage_value_still_blocks(trust_plugin, env_autonomous, monkeypatch, value) -> None:
    """Fail-closed parse: ONLY the literal 'report' downgrades. Typos and
    would-be disables keep blocking."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    monkeypatch.setenv("SMD_IDENTIFIER_GATE_MODE", value)
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, f"sess-garb-{value}")
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Your alien number on file is A123456789."},
        task_id="t",
        session_id=f"sess-garb-{value}",
        tool_call_id="c",
    )
    assert result is not None and result["action"] == "block"


def test_ambient_today_date_does_not_flag(trust_plugin, env_autonomous, monkeypatch) -> None:
    """Today's date verifies against the system clock, not a read — 'as of
    today' composition is legitimate and must neither block nor report."""
    from datetime import date

    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, "sess-amb")
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": f"As of {date.today().isoformat()}, no response has been received."},
        task_id="t",
        session_id="sess-amb",
        tool_call_id="c",
    )
    assert result is None
    assert _identifier_rows(fake) == []


def test_identifier_audit_failure_never_rescinds_block(
    trust_plugin, env_autonomous, monkeypatch
) -> None:
    """An identifier-audit emission failure must not rescind the refusal."""

    class _RaisingD1Client:
        def execute(self, sql: str, *params):
            raise RuntimeError("d1 down")

    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    ob = trust_plugin.outbound
    ob._AUDIT_CLIENT = _RaisingD1Client()
    ob._AUDIT_CUSTOMER_SLUG = "acme"
    ob._AUDIT_WIRED = True
    _seed_unrelated_read(trust_plugin, "sess-adf")
    result = trust_plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={"body": "Your alien number on file is A123456789."},
        task_id="t",
        session_id="sess-adf",
        tool_call_id="c",
    )
    assert result is not None and result["action"] == "block"


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


def test_create_event_structured_args_block_unread_date(
    trust_plugin, env_autonomous, monkeypatch
) -> None:
    """A structured-only create_event with an unread start_time date is REFUSED
    — the #2132 surface is not just visible, it enforces. The write would land
    on the firm calendar with no reviewer between."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, "sess-2132-event")
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
    assert result is not None and result["action"] == "block"
    rows = _identifier_rows(fake)
    assert len(rows) == 1
    metadata_json = rows[0][1][-1]
    assert "date" in metadata_json
    assert '"blocked":true' in metadata_json
    # redaction: the raw date value must not appear in the audit row
    assert "2026-09-14" not in metadata_json


def test_create_task_due_date_blocks_past_note(trust_plugin, env_autonomous, monkeypatch) -> None:
    """create_task carries a `note` (the old first-match scan stopped there);
    the unread due_date must still be seen — and now refuses the write."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, "sess-2132-task")
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
    assert result is not None and result["action"] == "block"
    rows = _identifier_rows(fake)
    assert len(rows) == 1
    assert "date" in rows[0][1][-1]


def test_structured_args_all_read_do_not_report(trust_plugin, env_autonomous, monkeypatch) -> None:
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


def test_structured_args_read_mismatch_blocks(trust_plugin, env_autonomous, monkeypatch) -> None:
    """MUTATION COMPANION to the false control above (Law 12): identical
    fixture except the read says 09-15 while the draft says 09-14 → BLOCK.
    Executable proof the control passes because verification HAPPENED, not
    because the gate is dead — a one-character mutation flips the outcome."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    trust_plugin.on_post_tool_call(
        tool_name="email_list_messages",
        result="Hearing scheduled 2026-09-15T09:00:00 per the court notice.",
        session_id="sess-2132-mut",
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
        session_id="sess-2132-mut",
        tool_call_id="c",
    )
    assert result is not None and result["action"] == "block"


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
    importlib.import_module("shared.pre_run_handoff")


# ---------------------------------------------------------------------------
# The escalator's two silences (ss-console#2547)
#
# On 2026-08-19 the deadline escalator tried five times to tell Scott about a
# court date seven days out. Four attempts were refused for the DATES — read
# from Smokeball by the routine's own pre-run script, which no session could see
# — and the fifth for an EM DASH, a marker written to keep the firm's voice out
# of client copy, applied to an ops email to the firm's own principal.
#
# Both halves are tested here rather than in isolation, because the thing under
# test is whether a routine can reach a human, and either half alone still
# leaves it mute.
# ---------------------------------------------------------------------------

_STAFF = "scott@smd.services"
_CLIENT = "jane@gmail.example"


@pytest.fixture
def rostered(trust_plugin, monkeypatch):
    """A roster on which ``_STAFF`` is firm staff and ``_CLIENT`` is a client."""
    enforce = trust_plugin.enforce
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: [_STAFF])
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: [(_CLIENT, "client")])
    yield


def _fabrication_rows(fake: "_FakeD1Client") -> list:
    return [c for c in fake.calls if "FABRICATION_FILTER_TRIGGERED" in c[1]]


def test_a_staff_send_with_an_em_dash_goes_out_normalized(
    trust_plugin, rostered, monkeypatch
) -> None:
    """The 08-19 fifth attempt. The dash is a tone rule about the firm's voice
    leaving the firm; the recipient here IS the firm. Normalize, do not refuse —
    and record no fabrication row, because nothing was fabricated."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    fake = _wire_fake_audit(trust_plugin.outbound)
    args = {
        "to": [_STAFF],
        "subject": "Deadline digest",
        "text": "Two deadlines need you — both this week.",
    }
    assert (
        trust_plugin.outbound.check_outbound_send(
            tool_name="smd_send_message", args=args, session_id="sess-dash", tool_call_id="c"
        )
        is None
    )
    assert args["text"] == "Two deadlines need you, both this week."
    assert _fabrication_rows(fake) == []


def test_the_normalized_body_is_the_body_that_ships(trust_plugin, rostered, monkeypatch) -> None:
    """The args dict the tool will run on is what was rewritten. A normalizer
    that scanned one string and shipped another would be a bypass, not a fix."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    args = {
        "to": [_STAFF],
        "subject": "Digest — two items",
        "text": "Item one—item two.",
        "html": "<p>Item one — item two.</p>",
    }
    trust_plugin.outbound.check_outbound_send(
        tool_name="smd_send_message", args=args, session_id="sess-ship", tool_call_id="c"
    )
    assert "—" not in json.dumps(args)
    assert args["subject"] == "Digest, two items"
    assert args["html"] == "<p>Item one, item two.</p>"


def test_normalization_does_not_join_two_bullets(trust_plugin, rostered, monkeypatch) -> None:
    """A dash at the start of a line is a list item. Absorbing the newline would
    change what the message says, which a punctuation rewrite may not do."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    args = {"to": [_STAFF], "text": "Needs you:\n— first item — urgent\n— second"}
    trust_plugin.outbound.check_outbound_send(
        tool_name="smd_send_message", args=args, session_id="sess-bullets", tool_call_id="c"
    )
    assert "—" not in args["text"]  # it DID normalize...
    assert args["text"].count("\n") == 2  # ...without swallowing a line break
    assert args["text"].splitlines()[1] == ", first item, urgent"


def test_a_client_send_with_an_em_dash_is_still_refused(
    trust_plugin, rostered, monkeypatch
) -> None:
    """The falsifier. The tone rule keeps its whole meaning on the classes it
    was written about; only the staff class moves."""
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    args = {"to": [_CLIENT], "text": "Your matter is progressing — we will update you."}
    blocked = trust_plugin.outbound.check_outbound_send(
        tool_name="smd_send_message", args=args, session_id="sess-client", tool_call_id="c"
    )
    assert blocked is not None and blocked["action"] == "block"
    assert "—" in args["text"]  # untouched


def test_an_unrostered_send_with_an_em_dash_is_still_refused(
    trust_plugin, rostered, monkeypatch
) -> None:
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    args = {"to": ["opposing@counsel.example"], "text": "Received — thank you."}
    blocked = trust_plugin.outbound.check_outbound_send(
        tool_name="smd_send_message", args=args, session_id="sess-outside", tool_call_id="c"
    )
    assert blocked is not None and blocked["action"] == "block"


# ---- the pre-run handoff, on the send path it was refused on --------------


def test_a_pre_run_date_is_refused_before_seeding_and_allowed_after(
    trust_plugin, rostered, monkeypatch, tmp_path
) -> None:
    """The 08-19 date refusals, and their fix, in one test.

    The escalator's script read a due date out of Smokeball and handed it to the
    model as text. Before the handoff nothing in the session had read it, so the
    gate refused — correctly, on the evidence it had. After the handoff is taken
    for that session, the same body sends.

    The date is computed rather than written down. This test asserts a REFUSAL,
    and `_ambient_dates` deliberately exempts today and yesterday ("as of
    today..." is legitimate composition without a read). A literal date is
    therefore a time bomb: this test hardcoded ``2026-08-26``, the real incident
    date, and began failing on 2026-08-26 when that date became ambient and the
    gate correctly stopped refusing it. Any future date keeps the assertion about
    provenance instead of about the calendar.
    """
    due = (datetime.now(timezone.utc).date() + timedelta(days=45)).isoformat()
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, "sess-handoff")  # non-empty: not the empty carve
    ob = trust_plugin.outbound

    def send():
        return ob.check_outbound_send(
            tool_name="smd_send_message",
            args={
                "to": [_STAFF],
                "subject": "Deadline digest",
                "text": f"Response to written discovery is due {due}.",
            },
            session_id="sess-handoff",
            tool_call_id="c",
        )

    before = send()
    assert before is not None and before["action"] == "block"

    started = datetime.now(timezone.utc)  # recency binding: the file must be fresh
    pre_run_handoff.write_handoff(
        "deadline-miss-escalator", started, [due], [], hermes_home=str(tmp_path)
    )
    taken = pre_run_handoff.take_handoff(
        "deadline-miss-escalator", started + timedelta(minutes=1), hermes_home=str(tmp_path)
    )
    provenance.record_read("sess-handoff", " ".join(taken["dates"]))

    assert send() is None


def test_an_ack_code_in_the_handoff_does_not_verify(
    trust_plugin, rostered, monkeypatch, tmp_path
) -> None:
    """The projection is the safety property, so it gets its own falsifier.

    A script may write anything into its handoff. Only the date atoms come back
    out, so a case-number-shaped token sitting in the same file certifies
    nothing — otherwise a pre-run would become a way to launder any identifier
    the script cared to print.
    """
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, "sess-ack")

    started = datetime.now(timezone.utc)  # recency binding: the file must be fresh
    pre_run_handoff.write_handoff(
        "deadline-miss-escalator",
        started,
        ["2026-08-26", "1:24-cv-01234"],  # the script wrote both down
        [],
        hermes_home=str(tmp_path),
    )
    taken = pre_run_handoff.take_handoff(
        "deadline-miss-escalator", started + timedelta(minutes=1), hermes_home=str(tmp_path)
    )
    provenance.record_read("sess-ack", " ".join(taken["dates"]))

    blocked = trust_plugin.outbound.check_outbound_send(
        tool_name="smd_send_message",
        args={"to": [_STAFF], "text": "Filed under case number 1:24-cv-01234."},
        session_id="sess-ack",
        tool_call_id="c",
    )
    assert blocked is not None and blocked["action"] == "block"


def test_a_seeded_record_verifies_its_own_pairing_and_refuses_a_mispairing(
    trust_plugin, rostered, monkeypatch, tmp_path
) -> None:
    """The 2026-08-24 widening, and its safety property, in one test.

    A handoff record seeds (number, dates) THROUGH ``add_record``, so the
    pairing registers with the atoms. The digest's own line — this matter's
    number beside this matter's date — sends; the same number beside another
    matter's date is exactly the mispairing the pair check exists to catch, and
    still refuses.
    """
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, "sess-rec")
    ob = trust_plugin.outbound

    started = datetime.now(timezone.utc)
    pre_run_handoff.write_handoff(
        "deadline-miss-escalator",
        started,
        ["2026-08-26", "2026-09-04"],
        [],
        hermes_home=str(tmp_path),
        records=[
            {"matterNumber": "2026-PI-101", "dates": ["2026-08-26"]},
            {"matterNumber": "2026-PI-102", "dates": ["2026-09-04"]},
        ],
    )
    taken = pre_run_handoff.take_handoff(
        "deadline-miss-escalator", started + timedelta(minutes=1), hermes_home=str(tmp_path)
    )
    provenance.record_read("sess-rec", " ".join(taken["dates"]))
    provenance.record_records("sess-rec", taken["records"])

    def send(text):
        return ob.check_outbound_send(
            tool_name="smd_send_message",
            args={"to": [_STAFF], "subject": "Deadline digest", "text": text},
            session_id="sess-rec",
            tool_call_id="c",
        )

    # The POSITIVE falsifier: a correctly-rendered multi-item digest, each
    # matter beside its own date, adjacent across the list boundary. Pairs are
    # active for the first time in this flow; a correct digest must PASS, or
    # the fix trades a degraded digest for a refused one.
    assert (
        send(
            "1. matter 2026-PI-101, response due 2026-08-26.\n"
            "2. matter 2026-PI-102, hearing 2026-09-04."
        )
        is None
    )
    # And the mispairing still refuses.
    blocked = send("matter 2026-PI-101, hearing 2026-09-04.")
    assert blocked is not None and blocked["action"] == "block"


def test_an_empty_register_refusal_offers_no_removal_hatch(
    trust_plugin, rostered, monkeypatch
) -> None:
    """The 2026-08-24 degraded digest was the model COMPLYING with the refusal:
    "or remove the unverified value and state that it needs confirmation",
    fifteen items at a time, register empty. When nothing was read, the refusal
    must offer reading or stopping — never a send with the values stripped."""
    # Relative, not hardcoded: the same time bomb the handoff test above
    # documents. This literal was 2026-09-04 and stopped being a fabrication
    # the day the calendar reached it (the gate correctly let it through).
    due = (datetime.now(timezone.utc).date() + timedelta(days=45)).isoformat()
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    blocked = trust_plugin.outbound.check_outbound_send(
        tool_name="smd_send_message",
        args={"to": [_STAFF], "text": f"Deadline {due} is approaching."},
        session_id="sess-empty-reg",
        tool_call_id="c",
    )
    assert blocked is not None and blocked["action"] == "block"
    message = blocked["message"]
    assert "remove the unverified value" not in message
    assert "not a deliverable" in message
    assert "run failed" in message


def test_a_populated_register_refusal_keeps_the_single_value_hatch(
    trust_plugin, rostered, monkeypatch
) -> None:
    """Removing ONE stray unverified value from an otherwise-sourced draft is a
    legitimate path and stays offered — the hatch closes only when the register
    is empty and there is nothing sourced to fall back on."""
    due = (datetime.now(timezone.utc).date() + timedelta(days=45)).isoformat()
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    provenance._reset_for_tests()
    _wire_fake_audit(trust_plugin.outbound)
    _seed_unrelated_read(trust_plugin, "sess-populated")
    blocked = trust_plugin.outbound.check_outbound_send(
        tool_name="smd_send_message",
        args={"to": [_STAFF], "text": f"Deadline {due} is approaching."},
        session_id="sess-populated",
        tool_call_id="c",
    )
    assert blocked is not None and blocked["action"] == "block"
    assert "remove the unverified value" in blocked["message"]


# ---- the fence: a turn cannot author its own provenance -------------------


def test_write_file_to_the_handoff_path_is_refused(trust_plugin, monkeypatch, tmp_path) -> None:
    """A sentinel the agent can write is not a sentinel."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    target = str(pre_run_handoff.handoff_path("deadline-miss-escalator", str(tmp_path)))
    blocked = trust_plugin.on_pre_tool_call(
        tool_name="write_file",
        args={"path": target, "content": '{"dates": ["2026-08-26"]}'},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert isinstance(blocked, dict) and blocked["action"] == "block"
    assert ".smd" in blocked["message"]


def test_code_execution_naming_the_handoff_path_is_refused(
    trust_plugin, monkeypatch, tmp_path
) -> None:
    """Through code, too. A fence only the file tools honour is a detour, and a
    path inside a program is a string, not an argument anyone can resolve."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    blocked = trust_plugin.on_pre_tool_call(
        tool_name="execute_code",
        args={"code": "open('.smd/pre_run/deadline-miss-escalator.json','w').write('{}')"},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert isinstance(blocked, dict) and blocked["action"] == "block"
    assert ".smd" in blocked["message"]


def test_reading_the_state_directory_is_not_fenced(
    trust_plugin, env_autonomous, monkeypatch, tmp_path
) -> None:
    """Reads are how an operator debugs a seat, and a read certifies nothing —
    ``record_seat_text`` files the seat's own text as explicitly NOT a record."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert (
        trust_plugin.on_pre_tool_call(
            tool_name="read_file",
            args={"path": str(tmp_path / ".smd" / "audit_status.json")},
            task_id="t",
            session_id="s",
            tool_call_id="c",
        )
        is None
    )


def test_a_write_elsewhere_is_not_fenced(
    trust_plugin, env_autonomous, monkeypatch, tmp_path
) -> None:
    """The falsifier for the fence: it must refuse one directory, not writing."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert (
        trust_plugin.on_pre_tool_call(
            tool_name="write_file",
            args={"path": str(tmp_path / "notes" / "scratch.txt"), "content": "hello"},
            task_id="t",
            session_id="s",
            tool_call_id="c",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Every refusal tells the model what to DO (2026-09-02)
#
# Scoped to tools that write client work product, 88% of refused attempts
# recover in the same session. The ones that STRAND cluster on markers whose
# refusal carried only a reason:
#
#   identifier gate  "... or remove the unverified value and state that it
#                     needs confirmation."                        -> recovers
#   em-dash marker   "Banned typographic marker on shipped
#                     user-facing copy (tone rules)."             -> stranded a
#                        daily-needs-you-digest memo on 2026-08-19 and again on
#                        2026-09-02, each on ONE attempt with no retry.
# ---------------------------------------------------------------------------


def test_every_marker_authors_a_remedy() -> None:
    """A marker without a remedy is a refusal that says only why the rule
    exists, which is the shape that strands work."""
    reg = load_markers()
    missing = [m.marker_id for m in reg.markers if not m.remedy.strip()]
    assert missing == [], (
        "these markers refuse without telling the model what to do, which is "
        f"how a write gets abandoned on the first attempt: {missing}"
    )


def test_a_refusal_carries_the_remedy_to_the_model() -> None:
    """The reason alone is not the deliverable; the remedy has to reach the
    refusal text the model actually reads."""
    decision = outbound_gate.evaluate(
        "This sentence has an em dash — right here.", cohort=None, vertical="law-firm"
    )
    assert decision.allowed is False
    assert "em-dash" in decision.reason
    # The remedy, verbatim from the registry.
    assert "Replace it with a comma, a semicolon, or a full stop" in decision.reason


def test_remedies_say_what_to_do_and_never_restate_the_rule() -> None:
    """The 2026-08-24 lesson, kept mechanical.

    A refusal that named its rule taught the model to strip 38 matter numbers
    out of a digest. Remedies are instructions, so they must not carry
    rule-language ("must", "never", "banned", "not allowed", "policy") -- that
    is what the reason field is for, and mixing them is what over-corrects.
    """
    reg = load_markers()
    offenders = []
    for m in reg.markers:
        low = m.remedy.lower()
        for word in (" must ", "banned", "not allowed", "policy", "prohibited"):
            if word in low:
                offenders.append((m.marker_id, word.strip()))
    assert offenders == [], (
        f"a remedy is an instruction, not a restatement of the rule: {offenders}"
    )
    # And a remedy that said nothing would pass the two checks above while
    # teaching nothing: require an imperative verb.
    verbless = [
        m.marker_id
        for m in reg.markers
        if not any(
            m.remedy.lstrip().lower().startswith(v)
            for v in ("replace", "delete", "remove", "re-read", "rewrite", "state", "give")
        )
    ]
    assert verbless == [], f"these remedies do not open with an instruction: {verbless}"
