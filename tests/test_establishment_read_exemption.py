"""The establishment READ exemption covers establishment work, nothing else.

``shared/matter_gate.py``'s read-time mixing fence refuses a second matter's
content in a session that already holds one. Establishment (ss ADR 0085) is the
one job that reads the firm's letters across matters by design, so it carries an
exemption. The defect this file pins: the exemption was marked on EVERY admin
turn and never cleared, so on a seat whose admins are the firm staff doing the
ordinary work (A&P) the fence was off for their ordinary drafting, and stayed
off after a non-admin took over the session.

Every positive is paired with the negative that would make it false: an
establishment flow must still be exempt, or "never exempt" would pass too.
"""

from __future__ import annotations

import pytest

from shared import matter_binding, matter_gate
from shared.inbound import SESSION_INBOUND_ORIGIN
from tests.conftest import load_plugin

SID = "s-est-exempt"
ADMIN = "chris@firm.com"
OTHER_ADMIN = "partner@firm.com"
STAFF = "paralegal@firm.com"
M_A = "aaaaaaaa-1111-2222-3333-444444444444"
M_B = "bbbbbbbb-1111-2222-3333-444444444444"
DOC = "mcp_smokeball_read_document"


class _Cfg:
    connectors: dict = {}
    rule_requests_to: list = []

    @property
    def admins(self):
        return [ADMIN, OTHER_ADMIN]

    def sender_is_admin(self, sender):
        return isinstance(sender, str) and sender.strip().lower() in self.admins


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    p = load_plugin("hermes-smd-establishment")
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", str(tmp_path / "possession.db"))
    monkeypatch.setattr(p, "_load_config", lambda: _Cfg())
    monkeypatch.setattr(p, "_possession_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(p, "_prepare_reference_stage", lambda *_a, **_k: None)
    p._ADMIN_STASH.clear()
    getattr(p, "_EXEMPT_OWNER", {}).clear()
    SESSION_INBOUND_ORIGIN._origins.clear()
    matter_binding._reset_for_tests()
    matter_gate.clear_establishment_read_exempt(SID)
    yield p
    p._ADMIN_STASH.clear()
    getattr(p, "_EXEMPT_OWNER", {}).clear()
    matter_binding._reset_for_tests()
    matter_gate.clear_establishment_read_exempt(SID)


def _turn(p, sender, message):
    p.on_pre_llm_call(session_id=SID, sender_id=sender, user_message=message)


def _second_matter_refused() -> bool:
    """Read matter A, then ask the fence about matter B."""
    matter_binding.record_from_read(SID, "letter A", tool_name=DOC, args={"matter_id": M_A})
    return matter_gate.content_read_refusal(SID, DOC, {"matter_id": M_B}) is not None


def test_an_ordinary_admin_turn_keeps_the_mixing_fence(plugin):
    """THE DEFECT. Old code: exempt, and the second matter's read went through."""
    _turn(plugin, ADMIN, "draft a reply to the client on matter 123")
    assert matter_gate.is_establishment_read_exempt(SID) is False
    assert _second_matter_refused()


def test_establish_liability_is_ordinary_drafting_not_establishment(plugin):
    _turn(plugin, ADMIN, "draft a demand that will establish liability and damages")
    assert matter_gate.is_establishment_read_exempt(SID) is False


def test_an_admin_asking_for_a_survey_is_exempt_across_the_blessing_turn(plugin):
    """The control: establishment still works. The survey reads across matters
    before anything is staged, and the blessing turn says only "yes"."""
    _turn(plugin, ADMIN, "Survey my documents and establish the firm's voice")
    assert matter_gate.is_establishment_read_exempt(SID) is True
    assert not _second_matter_refused()
    _turn(plugin, ADMIN, "yes, use those")
    assert matter_gate.is_establishment_read_exempt(SID) is True


def test_a_non_admin_turn_revokes_the_exemption(plugin):
    _turn(plugin, ADMIN, "establish our voice from the Reyes and Ortiz letters")
    assert matter_gate.is_establishment_read_exempt(SID) is True
    _turn(plugin, STAFF, "read the memo on matter 456")
    assert matter_gate.is_establishment_read_exempt(SID) is False
    assert _second_matter_refused()


def test_another_admin_speaking_revokes_the_exemption(plugin):
    """The exemption belongs to the admin doing establishment, not to adminness."""
    _turn(plugin, ADMIN, "establish the shape of our client letters")
    _turn(plugin, OTHER_ADMIN, "draft a reply on matter 789")
    assert matter_gate.is_establishment_read_exempt(SID) is False


def test_an_establishment_tool_clearing_its_gate_grants_the_exemption(plugin):
    _turn(plugin, ADMIN, "go ahead with the two letters I named")
    assert matter_gate.is_establishment_read_exempt(SID) is False
    assert plugin.on_pre_tool_call(tool_name=plugin.TOOL_STAGE, session_id=SID, args={}) is None
    assert matter_gate.is_establishment_read_exempt(SID) is True


def test_a_refused_establishment_tool_grants_nothing(plugin):
    _turn(plugin, STAFF, "establish the firm's voice")
    verdict = plugin.on_pre_tool_call(tool_name=plugin.TOOL_STAGE, session_id=SID, args={})
    assert verdict and verdict.get("action") == "block"
    assert matter_gate.is_establishment_read_exempt(SID) is False


def test_an_unclassifiable_turn_revokes_the_exemption(plugin, monkeypatch):
    _turn(plugin, ADMIN, "establish our voice")
    assert matter_gate.is_establishment_read_exempt(SID) is True

    def boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(plugin, "_load_config", boom)
    _turn(plugin, ADMIN, "next")
    assert matter_gate.is_establishment_read_exempt(SID) is False
