"""Tests for the person-scoped establishment predicate + pointer (ADR 0085 §6).

One hook, two predicates. The load-bearing properties (Law 12 — each fails on
an input the broken behavior would wave through):

 1. A person can establish THEIR OWN preferences (sender == subject) and NOT
    anyone else's — the exact-match refusal is the "stamp": ``pre_tool_call``
    cannot rewrite tool args (docs/hook-surface.md), so a mismatched subject is
    blocked, never repaired.
 2. Admin identity does NOT bypass the person predicate for someone else's
    preferences (surfaced decision, default NO — the person's voice is theirs).
 3. Fail-closed: no stash, missing subject, unattributed session — all refuse.
 4. The FIRM predicate is untouched: scope absent/firm still requires admin.
 5. ``pre_llm_call`` injects a POINTER to the attributed sender's installed
    preferences (root manifest), and only for the matching sender.
"""

from __future__ import annotations

import json

import pytest

from shared.person_prefs import PREFS_MANIFEST_NAME, person_slug
from tests.conftest import load_plugin

ADMIN = "chris@firm.com"
PERSON = "sarah@firm.com"


class _FakeConfig:
    def __init__(self, admins, connectors):
        self._admins = admins
        self.connectors = dict(connectors)

    def sender_is_admin(self, sender):
        return isinstance(sender, str) and sender.strip().lower() in self._admins

    def sender_on_roster(self, sender):
        # Both test identities are rostered firm people.
        return isinstance(sender, str) and sender.strip().lower() in {ADMIN, PERSON}


class _FakeCustomerConfig:
    admins: list[str] = []
    # Default custody is msgraph (tenant-authenticated) so the predicate tests
    # exercise the MATCH logic in isolation: the possession ceremony is exempt
    # on that custody by design. The AgentMail-custody ceremony has its own
    # test below (and the full matrix in tests/test_admin_possession.py).
    connectors: dict = {"Email": {"adapter": "msgraph", "backend": "mcp:msgraph", "enabled": True}}

    @classmethod
    def from_volume(cls, path=None):
        return _FakeConfig(cls.admins, cls.connectors)


@pytest.fixture
def establishment(monkeypatch):
    plugin = load_plugin("hermes-smd-establishment")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "run_id": "run-1"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    _FakeCustomerConfig.admins = [ADMIN]
    _FakeCustomerConfig.connectors = {
        "Email": {"adapter": "msgraph", "backend": "mcp:msgraph", "enabled": True}
    }
    monkeypatch.setattr(plugin, "CustomerConfig", _FakeCustomerConfig)
    plugin._ADMIN_STASH.clear()
    return plugin, requests


def _turn(plugin, sender, session="sess-1"):
    return plugin.on_pre_llm_call(session_id=session, sender_id=sender, user_message="x")


def _submit_gate(plugin, *, person, session="sess-1", scope="person"):
    return plugin.on_pre_tool_call(
        tool_name=plugin.TOOL_SUBMIT,
        session_id=session,
        args={"scope": scope, "person": person, "phase": "install", "spec_body": "Bullets."},
    )


# ---------------------------------------------------------------------------
# 1 + 2 + 3 — the person predicate
# ---------------------------------------------------------------------------


def test_a_person_may_establish_their_own_preferences(establishment):
    plugin, _ = establishment
    _turn(plugin, PERSON)
    assert _submit_gate(plugin, person=PERSON) is None


def test_the_match_is_normalized_not_literal(establishment):
    plugin, _ = establishment
    _turn(plugin, PERSON)
    assert _submit_gate(plugin, person="  Sarah@Firm.COM ") is None


def test_a_person_may_not_establish_someone_elses(establishment):
    plugin, _ = establishment
    _turn(plugin, PERSON)
    verdict = _submit_gate(plugin, person=ADMIN)
    assert verdict is not None and verdict["action"] == "block"
    assert "person themselves" in verdict["message"]


def test_admin_identity_does_not_bypass_the_person_predicate(establishment):
    """The surfaced decision (default NO): an Operator admin may establish the
    FIRM's voice, and their OWN preferences — never another person's. The
    person's voice is theirs; the admin's lever for firm-wide shape is the
    firm layer."""
    plugin, _ = establishment
    _turn(plugin, ADMIN)
    assert _submit_gate(plugin, person=ADMIN) is None  # their own — fine
    verdict = _submit_gate(plugin, person=PERSON)  # someone else's — refused
    assert verdict is not None and verdict["action"] == "block"
    assert "does not change that" in verdict["message"]


@pytest.mark.parametrize("subject", [None, "", 42])
def test_a_missing_or_malformed_subject_refuses(establishment, subject):
    plugin, _ = establishment
    _turn(plugin, PERSON)
    verdict = _submit_gate(plugin, person=subject)
    assert verdict is not None and verdict["action"] == "block"


def test_an_unclassified_session_refuses_person_scope(establishment):
    plugin, _ = establishment
    verdict = _submit_gate(plugin, person=PERSON, session="never-seen")
    assert verdict is not None and verdict["action"] == "block"


# ---------------------------------------------------------------------------
# 4 — the firm predicate is untouched
# ---------------------------------------------------------------------------


def test_firm_scope_still_requires_admin(establishment):
    plugin, _ = establishment
    _turn(plugin, PERSON)
    for scope in (None, "firm"):
        verdict = _submit_gate(plugin, person=None, scope=scope)
        assert verdict is not None and verdict["action"] == "block"
        assert "Operator admins" in verdict["message"]
    _turn(plugin, ADMIN)
    assert _submit_gate(plugin, person=None, scope="firm") is None


def test_staging_stays_admin_only_regardless_of_args(establishment):
    plugin, _ = establishment
    _turn(plugin, PERSON)
    verdict = plugin.on_pre_tool_call(
        tool_name=plugin.TOOL_STAGE,
        session_id="sess-1",
        args={"scope": "person", "person": PERSON, "name": "x", "text": "y"},
    )
    assert verdict is not None and verdict["action"] == "block"


# ---------------------------------------------------------------------------
# 5 — the per-person pointer rides pre_llm_call, for the matching sender only
# ---------------------------------------------------------------------------


def _install_manifest(tmp_path, person=PERSON):
    slug = person_slug(person)
    (tmp_path / PREFS_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "preferences": {
                    slug: {
                        "person": person,
                        "rel_path": f"preferences/{slug}.json",
                        "sha256": "ab" * 32,
                        "bytes": 42,
                    }
                },
            }
        )
    )
    return slug


def test_pointer_is_injected_for_the_attributed_sender(establishment, tmp_path, monkeypatch):
    plugin, _ = establishment
    slug = _install_manifest(tmp_path)
    monkeypatch.setenv("SMD_SPEC_DIR", str(tmp_path))
    context = _turn(plugin, PERSON)["context"]
    assert f"preferences/{slug}.json" in context
    assert ("ab" * 32)[:16] in context
    # Pointer, never prose — and never a substitute for a required spec read.
    assert "never override the firm's authored specs" in context


def test_no_pointer_for_a_sender_without_installed_preferences(
    establishment, tmp_path, monkeypatch
):
    plugin, _ = establishment
    slug = _install_manifest(tmp_path)  # sarah's, not chris's
    monkeypatch.setenv("SMD_SPEC_DIR", str(tmp_path))
    context = _turn(plugin, ADMIN)["context"]
    assert f"preferences/{slug}.json" not in context


def test_a_broken_manifest_costs_the_pointer_never_the_turn(establishment, tmp_path, monkeypatch):
    plugin, _ = establishment
    (tmp_path / PREFS_MANIFEST_NAME).write_text("{torn")
    monkeypatch.setenv("SMD_SPEC_DIR", str(tmp_path))
    result = _turn(plugin, PERSON)
    assert result is not None and plugin._ESTABLISH_NUDGE in result["context"]


# ---------------------------------------------------------------------------
# Wire shape — what the broker's person half must accept (the C6 contract)
# ---------------------------------------------------------------------------


def test_person_submit_marshals_scope_and_person(establishment):
    plugin, requests = establishment
    plugin._submit(
        {
            "scope": "person",
            "person": PERSON,
            "phase": "install",
            "spec_body": "Bullets. Short emails.",
            "instructed_by": PERSON,
            "source_ref": "msg-9",
        }
    )
    assert requests[0]["action"] == "establish_submit"
    assert requests[0]["scope"] == "person"
    assert requests[0]["person"] == PERSON
    assert requests[0]["staging_id"] is None


def test_person_scope_on_agentmail_custody_requires_possession_first(
    establishment, tmp_path, monkeypatch
):
    """Captain-directed ruling 2026-08-02: on AgentMail custody (no per-message
    auth verdict, ss#2164 WEAK) a forged From naming a rostered person could
    author that person's own drafting context — same attack as the admin
    forgery at smaller blast radius. The person's FIRST person-scoped
    establishment is therefore withheld until their mailbox is confirmed once.
    Falsifier: on the msgraph-custody default fixture the same call passes
    (test_a_person_may_establish_their_own_preferences)."""
    plugin, _ = establishment
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", str(tmp_path / "possession.db"))
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "pilot-test")
    _FakeCustomerConfig.connectors = {
        "Email": {"adapter": "agentmail", "backend": "mcp:agentmail", "enabled": True}
    }
    _turn(plugin, PERSON)
    verdict = _submit_gate(plugin, person=PERSON)
    assert verdict is not None and verdict.get("action") == "block"
    assert "confirm" in verdict.get("message", "").lower()


# ---------------------------------------------------------------------------
# 6 - the person ceremony's confirmation note is the PERSON's (live 2026-08-21)
#
# ss-probe-runner, 21:35Z: a rostered non-admin answered their
# personal-preference possession challenge and the Operator told them
# firm-level establishment was now unlocked for them. The two notes are
# adjacent constants and the person lane appended the admin one. The person
# ceremony confirms one mailbox so that ONE PERSON'S OWN preferences can be
# recorded; it confers nothing over the firm, and a person told otherwise
# spends their next reply asking for a firm change that is refused.
# ---------------------------------------------------------------------------


def test_the_person_note_never_promises_firm_level_authority():
    """Falsifier: put "firm-level" back in the person note and this fails."""
    plugin = load_plugin("hermes-smd-establishment")
    note = plugin._PERSON_POSSESSION_CONFIRMED_NOTE.lower()
    assert "firm-level" not in note
    assert "firm level" not in note
    assert "personal preferences" in note


def test_the_person_lane_injects_the_person_note_not_the_admins(establishment, monkeypatch):
    """The lane, not just the constant: a non-admin whose person ceremony
    confirms this turn must be told about their own preferences. Falsifier:
    point line back at _POSSESSION_CONFIRMED_NOTE and this fails."""
    plugin, _ = establishment
    monkeypatch.setattr(plugin, "_maybe_confirm_possession", lambda *a, **k: False)
    monkeypatch.setattr(plugin, "_maybe_confirm_person_possession", lambda *a, **k: True)
    result = _turn(plugin, PERSON)
    context = result["context"]
    assert plugin._PERSON_POSSESSION_CONFIRMED_NOTE.format(sender=PERSON) in context
    assert "firm-level establishment is" not in context


def test_the_admin_lane_still_gets_the_admin_note(establishment, monkeypatch):
    """The twin stays intact: an admin's ceremony DOES unlock firm-level
    establishment, and saying so is the whole point of that note."""
    plugin, _ = establishment
    monkeypatch.setattr(plugin, "_maybe_confirm_possession", lambda *a, **k: True)
    monkeypatch.setattr(plugin, "_maybe_confirm_person_possession", lambda *a, **k: False)
    context = _turn(plugin, ADMIN)["context"]
    assert plugin._POSSESSION_CONFIRMED_NOTE.format(sender=ADMIN) in context
