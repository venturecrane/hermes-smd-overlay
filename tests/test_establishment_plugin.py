"""Tests for plugins/hermes-smd-establishment (ss ADR 0085).

The load-bearing properties, each with an input the OLD/broken behavior would
have mishandled (Law 12):

 1. THE TOOLS ARE MAPPED. Unmapped = terminal REFUSED (the ss #1915
    invisibility): stage + submit INTERNAL_WRITE, status READ.
 2. NON-ADMIN REFUSED, ADMIN PASSES. The admin predicate is the ONLY gate, it
    fails closed (no stash, unreadable config), and the refusal names who can.
 3. TAINT IS DELIBERATELY NOT CHECKED. Asserted positively, with the decision
    cited, so a future "hardening" that adds a taint gate fails this suite and
    has to confront the decision rather than silently reverting it.
 4. LAST-WRITER-WINS DOWNGRADE. A mid-session non-admin message strips the
    session's establishment authority (fail-safe direction).
 5. THE WIRE SHAPE IS §3's. The broker's C0 half is built from the same design
    section; a renamed field here would strand the two halves.

The broker is faked; its validation is tested where it lives (console side).
"""

from __future__ import annotations

import json

import pytest

from shared.action_classes import ActionClass, classify_tool
from shared.inbound import SESSION_TAINT
from tests.conftest import load_plugin


class _FakeConfig:
    def __init__(self, admins, connectors=None):
        self._admins = admins
        self.connectors = dict(connectors or {})

    @property
    def admins(self):
        return list(self._admins)

    def sender_is_admin(self, sender):
        return isinstance(sender, str) and sender.strip().lower() in self._admins


class _FakeCustomerConfig:
    admins: list[str] = []
    #: No Email connector by default — no mail channel, so the possession
    #: ceremony (ss#2164) never binds and the pre-ceremony gate behavior below
    #: is asserted unchanged. Custody-specific behavior is covered in
    #: tests/test_admin_possession.py.
    connectors: dict = {}

    @classmethod
    def from_volume(cls, path=None):
        return _FakeConfig(cls.admins, cls.connectors)


@pytest.fixture
def establishment(monkeypatch, tmp_path):
    plugin = load_plugin("hermes-smd-establishment")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "staging_id": "set-1", "doc_id": "doc-1", "run_id": "run-1"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", str(tmp_path / "possession.db"))
    _FakeCustomerConfig.admins = ["chris@firm.com"]
    _FakeCustomerConfig.connectors = {}
    monkeypatch.setattr(plugin, "CustomerConfig", _FakeCustomerConfig)
    plugin._ADMIN_STASH.clear()
    return plugin, requests


def _turn(plugin, sender, session="sess-1"):
    return plugin.on_pre_llm_call(session_id=session, sender_id=sender, user_message="x")


def _gate(plugin, tool, session="sess-1"):
    return plugin.on_pre_tool_call(tool_name=tool, session_id=session)


# ---------------------------------------------------------------------------
# 1. Mapping + registration
# ---------------------------------------------------------------------------


def test_tools_are_mapped_with_the_declared_classes(establishment):
    plugin, _ = establishment
    assert classify_tool(plugin.TOOL_STAGE).action_class is ActionClass.INTERNAL_WRITE
    assert classify_tool(plugin.TOOL_SUBMIT).action_class is ActionClass.INTERNAL_WRITE
    assert classify_tool(plugin.TOOL_STATUS).action_class is ActionClass.READ


def test_registers_three_tools_and_both_hooks(establishment):
    plugin, _ = establishment
    tools: list[dict] = []
    hooks: list[str] = []

    class Ctx:
        def register_tool(self, **kwargs):
            tools.append(kwargs)

        def register_hook(self, name, _cb):
            hooks.append(name)

    plugin.register(Ctx())
    assert [t["name"] for t in tools] == list(plugin.ESTABLISH_TOOLS)
    # Wrapped function shape — a bare JSON-schema advertises empty parameters.
    assert all("parameters" in t["schema"] for t in tools)
    assert sorted(hooks) == ["pre_llm_call", "pre_tool_call"]


# ---------------------------------------------------------------------------
# 2. The admin gate
# ---------------------------------------------------------------------------


def test_non_admin_is_refused_and_told_who_can(establishment):
    """Firm-level establishment stays admin-only. ``establish_status`` is the
    O5 exception (ADR 0085 §6): a classified non-admin session may poll — a
    person who established their own preferences must be able to read their
    run's result, and run ids are broker-minted secrets."""
    plugin, _ = establishment
    _turn(plugin, "sarah@firm.com")
    for tool in (plugin.TOOL_STAGE, plugin.TOOL_SUBMIT):
        verdict = _gate(plugin, tool)
        assert verdict is not None and verdict["action"] == "block"
        assert "Operator admins" in verdict["message"]
        assert "correction_capture" in verdict["message"]
    assert _gate(plugin, plugin.TOOL_STATUS) is None


def test_unclassified_session_is_refused_fail_closed(establishment):
    """No stash entry — a session where pre_llm_call never classified a sender
    (restart, hook fault) admits nobody."""
    plugin, _ = establishment
    assert _gate(plugin, plugin.TOOL_SUBMIT, session="never-seen")["action"] == "block"


def test_admin_passes_all_three_tools(establishment):
    plugin, _ = establishment
    _turn(plugin, "chris@firm.com")
    for tool in plugin.ESTABLISH_TOOLS:
        assert _gate(plugin, tool) is None


def test_gate_ignores_other_tools(establishment):
    plugin, _ = establishment
    assert _gate(plugin, "email_send", session="never-seen") is None


def test_unreadable_config_classifies_nobody_as_admin(establishment, monkeypatch):
    plugin, _ = establishment

    class Broken:
        @classmethod
        def from_volume(cls, path=None):
            raise RuntimeError("volume gone")

    monkeypatch.setattr(plugin, "CustomerConfig", Broken)
    _turn(plugin, "chris@firm.com", session="sess-broken")
    assert _gate(plugin, plugin.TOOL_SUBMIT, session="sess-broken")["action"] == "block"


# ---------------------------------------------------------------------------
# 3. Taint is DELIBERATELY not checked
# ---------------------------------------------------------------------------


def test_establishment_proceeds_on_a_turn_that_read_fenced_content(establishment):
    """Captain decision 2026-08-02 (same-breath establishment; recorded in the
    intake design's amendment block, point 1): the establishment turn READS the
    firm's documents through a connector, so the session is necessarily marked
    non-internal by the very act the admin instructed — a taint gate here would
    refuse every legitimate establishment. The admin allow-list is the gate;
    the broker's server-side provenance verification and the compiler gates
    (leak check above all) bound what a hostile document could smuggle into a
    spec. This test asserts the decision POSITIVELY so a future taint check
    fails here and must confront the decision rather than silently revert it.
    """
    plugin, _ = establishment
    session = "sess-tainted-establishment"
    SESSION_TAINT.mark(session, "unknown_external")
    _turn(plugin, "chris@firm.com", session=session)
    for tool in plugin.ESTABLISH_TOOLS:
        assert _gate(plugin, tool, session=session) is None, (
            f"{tool} was refused on a tainted turn — the establishment gate must "
            "check the admin predicate ONLY (Captain decision 2026-08-02)"
        )


# ---------------------------------------------------------------------------
# 4. Last-writer-wins stash
# ---------------------------------------------------------------------------


def test_mid_session_non_admin_message_downgrades(establishment):
    plugin, _ = establishment
    _turn(plugin, "chris@firm.com")
    assert _gate(plugin, plugin.TOOL_SUBMIT) is None
    _turn(plugin, "stranger@elsewhere.com")
    assert _gate(plugin, plugin.TOOL_SUBMIT)["action"] == "block"


def test_unattributed_turn_leaves_the_classification(establishment):
    """A cron/self-wake turn carries no sender; it is not a new person, so it
    neither grants nor strips the session's classification."""
    plugin, _ = establishment
    _turn(plugin, "chris@firm.com")
    _turn(plugin, "")
    assert _gate(plugin, plugin.TOOL_SUBMIT) is None


# ---------------------------------------------------------------------------
# 5. Nudge — admin turns only
# ---------------------------------------------------------------------------


def test_nudge_rides_admin_turns_only(establishment):
    """The ADMIN nudge stays admin-only (it advertises what the gate would
    refuse anyone else). Since O5, every attributed turn also carries the
    person-scope nudge — that gate any attributed sender can satisfy for
    themselves, so advertising it to everyone is correct (overlay #170)."""
    plugin, _ = establishment
    admin_context = _turn(plugin, "chris@firm.com")["context"]
    assert plugin._NUDGE in admin_context
    assert plugin._PERSON_NUDGE in admin_context
    non_admin_context = _turn(plugin, "sarah@firm.com")["context"]
    assert plugin._NUDGE not in non_admin_context
    assert plugin._PERSON_NUDGE in non_admin_context
    assert _turn(plugin, "") is None


# ---------------------------------------------------------------------------
# 6. Wire shape — the C0 contract
# ---------------------------------------------------------------------------


def test_stage_marshals_exactly_the_design_fields(establishment):
    plugin, requests = establishment
    plugin._stage(
        {
            "staging_id": None,
            "name": "letter-01.md",
            "text": "Dear Ms. Reyes,",
            "source": {"connector": "smokeball", "document_id": "d1", "matter_id": "m1"},
            "status": "approved",  # the model tries; nothing forwards it
        }
    )
    assert requests[0]["action"] == "establish_stage_document"
    assert set(requests[0]) == {"action", "staging_id", "name", "text", "source"}


def test_submit_marshals_exactly_the_design_fields(establishment):
    plugin, requests = establishment
    plugin._submit(
        {
            "staging_id": "set-1",
            "phase": "install",
            "output_class": "work_product",
            "property": "voice",
            "spec_body": "Plain sentences.",
            "assertions": {"rules": []},
            "corpus_manifest": [{"doc_id": "doc-1", "sha256": "ab" * 32}],
            "instructed_by": "chris@firm.com",
            "source_ref": "msg-123",
            "extra_field": "dropped",
        }
    )
    assert requests[0]["action"] == "establish_submit"
    assert set(requests[0]) == {
        "action",
        "scope",
        "person",
        "staging_id",
        "phase",
        "output_class",
        "property",
        "spec_body",
        "assertions",
        "corpus_manifest",
        "instructed_by",
        "source_ref",
    }


def test_status_returns_the_broker_verdict_verbatim(establishment):
    plugin, requests = establishment
    out = json.loads(plugin._status({"run_id": "run-1"}))
    assert requests[0] == {"action": "establish_status", "run_id": "run-1"}
    assert out["ok"] is True
