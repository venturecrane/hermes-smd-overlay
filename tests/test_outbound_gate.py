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

import importlib

import pytest

from shared import outbound_gate
from shared.fabrication_markers import FabricationMarkersError, load_markers
from tests.conftest import load_plugin

# ---------------------------------------------------------------------------
# Layer 1 — fabrication marker registry
# ---------------------------------------------------------------------------


def test_markers_registry_non_empty_and_versioned() -> None:
    """The vendored registry must load, be non-empty, and carry a version.

    TODO(PR-B-merge): extend this to a strict hash/version check against the
    canonical ss-console artifact once it is published. Until then this pins
    the structural invariant (non-empty + versioned) the loader fails closed on.
    """
    reg = load_markers()
    assert isinstance(reg.version, str) and reg.version
    assert len(reg.markers) > 0


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
def env_autonomous(monkeypatch):
    """Ceiling = autonomous so the trust-ceiling layer allows INTERNAL_WRITE."""
    monkeypatch.setenv("SMD_TRUST_CEILING", "autonomous")
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
    """A REFUSED ceiling blocks first; the outbound gate is never consulted."""
    monkeypatch.setenv("SMD_TRUST_CEILING", "refused")
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
