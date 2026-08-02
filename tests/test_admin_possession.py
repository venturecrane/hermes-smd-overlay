"""Mailbox-possession ceremony tests (ss ADR 0085 §5, ss#2164).

The ceremony exists because the #2164 probe returned WEAK: AgentMail exposes no
per-message SPF/DKIM/DMARC verdict a seat can require, so on AgentMail-custody
seats an admin-classed instruction's From header is a claim, not proof. Every
property below is asserted WITH its failing counterpart (Law 12):

 1. NONCE + WITHHOLD. Unconfirmed admin on agentmail custody: establish_*
    refused, one challenge minted; confirmed admin passes.
 2. ISSUED ONCE. A second establish call re-uses the SAME outstanding nonce
    (pending message, no duplicate-send instruction); after TTL expiry a FRESH
    nonce supersedes it.
 3. SINGLE-USE + TTL. The right nonce confirms once; wrong, expired, reused,
    other-admin, and non-admin-sender nonces never confirm.
 4. CHANNEL SCOPING. msgraph (tenant-auth) custody and no-mail-channel seats
    pass with NO ceremony and NO state minted; an unprobed mail adapter gets
    the ceremony; an unreadable config refuses (fail closed).
 5. RESTART-DURABLE. Confirmed state and an in-flight nonce both survive a
    restart (fresh reads over the same volume file; the in-memory admin stash
    is what a restart clears, and that re-classifies without re-arming).
 6. RE-ARM ON ENTRY CHANGE. Removing the admin's scope.admins entry revokes
    possession AND kills the old nonce; re-adding runs the ceremony afresh; an
    unrelated list change disturbs nothing.
 7. RECIPIENT LOCK. A live code ships only in a send addressed to exactly the
    rostered admin; any other recipient, any reply-shaped tool, and any draft
    addressed elsewhere is blocked; consumed codes stop matching.
"""

from __future__ import annotations

import re

import pytest

from shared import admin_possession
from tests.conftest import load_plugin

ADMIN = "chris@firm.com"
OTHER_ADMIN = "christa@firm.com"
ATTACKER = "attacker@evil.example"

_NONCE_RE = re.compile(r"smd-confirm-[0-9a-f]{32}")


class _FakeConfig:
    def __init__(self, admins, connectors):
        self._admins = admins
        self.connectors = dict(connectors)

    @property
    def admins(self):
        return list(self._admins)

    def sender_is_admin(self, sender):
        return isinstance(sender, str) and sender.strip().lower() in self._admins


class _FakeCustomerConfig:
    admins: list[str] = []
    connectors: dict = {}

    @classmethod
    def from_volume(cls, path=None):
        return _FakeConfig(cls.admins, cls.connectors)


def _agentmail_connectors():
    return {"Email": {"adapter": "agentmail", "backend": "mcp:agentmail", "enabled": True}}


@pytest.fixture
def db_path(monkeypatch, tmp_path):
    path = str(tmp_path / "possession.db")
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", path)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "pilot-test")
    return path


@pytest.fixture
def plugin(monkeypatch, db_path):
    plugin = load_plugin("hermes-smd-establishment")
    monkeypatch.setattr(plugin, "_broker_request", lambda payload: {"ok": True, "run_id": "run-1"})
    _FakeCustomerConfig.admins = [ADMIN, OTHER_ADMIN]
    _FakeCustomerConfig.connectors = _agentmail_connectors()
    monkeypatch.setattr(plugin, "CustomerConfig", _FakeCustomerConfig)
    plugin._ADMIN_STASH.clear()
    return plugin


def _turn(plugin, sender, message="please establish the voice", session="sess-1"):
    return plugin.on_pre_llm_call(session_id=session, sender_id=sender, user_message=message)


def _gate(plugin, tool=None, session="sess-1", args=None):
    tool = tool or plugin.TOOL_SUBMIT
    return plugin.on_pre_tool_call(tool_name=tool, session_id=session, args=args)


def _nonce_from(message: str) -> str:
    match = _NONCE_RE.search(message)
    assert match, f"no challenge code in message: {message!r}"
    return match.group(0)


# ---------------------------------------------------------------------------
# 1. Withhold + challenge on agentmail custody
# ---------------------------------------------------------------------------


def test_unconfirmed_admin_on_agentmail_is_withheld_with_a_challenge(plugin):
    _turn(plugin, ADMIN)
    verdict = _gate(plugin)
    assert verdict is not None and verdict["action"] == "block"
    # The withhold names the ROSTERED address and carries the code the model
    # must email there — never a Reply-To.
    assert ADMIN in verdict["message"]
    nonce = _nonce_from(verdict["message"])
    assert admin_possession.outstanding_nonces() == {nonce: ADMIN}


def test_confirmed_admin_passes_all_three_tools(plugin):
    _turn(plugin, ADMIN)
    nonce = _nonce_from(_gate(plugin)["message"])
    assert admin_possession.try_confirm(ADMIN, f"Confirming: {nonce}", [ADMIN, OTHER_ADMIN])
    for tool in plugin.ESTABLISH_TOOLS:
        assert _gate(plugin, tool) is None


def test_non_admin_gets_the_admin_refusal_not_the_ceremony(plugin):
    """The ceremony binds admins only; a non-admin is refused by the admin
    predicate, with the who-can message, and no challenge is ever minted."""
    _turn(plugin, "sarah@firm.com")
    verdict = _gate(plugin)
    assert verdict["action"] == "block"
    assert "Operator admins" in verdict["message"]
    assert not _NONCE_RE.search(verdict["message"])
    assert admin_possession.outstanding_nonces() == {}


# ---------------------------------------------------------------------------
# 2. Issued once — no re-spam per call; expiry supersedes
# ---------------------------------------------------------------------------


def test_second_call_reuses_the_outstanding_nonce_and_forbids_a_duplicate(plugin):
    _turn(plugin, ADMIN)
    first = _gate(plugin)["message"]
    second = _gate(plugin)["message"]
    assert _nonce_from(first) == _nonce_from(second)
    assert len(admin_possession.outstanding_nonces()) == 1
    assert "already" in second and "duplicate" in second
    assert "already" not in first  # the two states speak differently


def test_expired_nonce_is_superseded_by_a_fresh_one(db_path):
    first = admin_possession.verdict(ADMIN, [ADMIN], now=1_000.0)
    assert first["state"] == admin_possession.STATE_CHALLENGE_ISSUED
    still = admin_possession.verdict(ADMIN, [ADMIN], now=1_000.0 + 60)
    assert still["state"] == admin_possession.STATE_CHALLENGE_PENDING
    assert still["nonce"] == first["nonce"]
    late = admin_possession.verdict(
        ADMIN, [ADMIN], now=1_000.0 + admin_possession.NONCE_TTL_SECONDS + 1
    )
    assert late["state"] == admin_possession.STATE_CHALLENGE_ISSUED
    assert late["nonce"] != first["nonce"]


# ---------------------------------------------------------------------------
# 3. Confirmation: single-use, TTL, sender-bound
# ---------------------------------------------------------------------------


def test_reply_with_the_live_nonce_confirms_and_unlocks(plugin):
    _turn(plugin, ADMIN)
    nonce = _nonce_from(_gate(plugin)["message"])
    # The confirming reply arrives as a NEW attributed turn at pre_llm_call —
    # the same seam sender attribution already rides.
    context = _turn(plugin, ADMIN, message=f"Yes, that was me. {nonce}", session="sess-2")
    assert "confirmed" in context["context"]
    assert _gate(plugin, session="sess-2") is None
    assert _gate(plugin, session="sess-1") is None  # possession is per-admin, not per-session


def test_wrong_nonce_does_not_confirm(plugin):
    _turn(plugin, ADMIN)
    _gate(plugin)
    fake = "smd-confirm-" + "0" * 32
    context = _turn(plugin, ADMIN, message=f"Yes: {fake}", session="sess-2")
    assert "confirmed" not in context["context"]
    assert _gate(plugin, session="sess-2")["action"] == "block"


def test_expired_nonce_does_not_confirm(db_path):
    issued = admin_possession.verdict(ADMIN, [ADMIN], now=1_000.0)
    late = 1_000.0 + admin_possession.NONCE_TTL_SECONDS + 1
    assert not admin_possession.try_confirm(ADMIN, issued["nonce"], [ADMIN], now=late)
    assert admin_possession.verdict(ADMIN, [ADMIN], now=late)["state"] != (
        admin_possession.STATE_CONFIRMED
    )


def test_a_nonce_confirms_exactly_once(db_path):
    issued = admin_possession.verdict(ADMIN, [ADMIN], now=1_000.0)
    assert admin_possession.try_confirm(ADMIN, issued["nonce"], [ADMIN], now=1_001.0)
    # Reuse is inert: already confirmed, the code is consumed.
    assert not admin_possession.try_confirm(ADMIN, issued["nonce"], [ADMIN], now=1_002.0)
    assert admin_possession.outstanding_nonces(now=1_002.0) == {}


def test_someone_elses_nonce_and_a_non_admin_sender_never_confirm(plugin):
    _turn(plugin, ADMIN)
    nonce = _nonce_from(_gate(plugin)["message"])
    # Another admin echoing chris's code does not confirm chris (or herself).
    _turn(plugin, OTHER_ADMIN, message=f"fwd: {nonce}", session="sess-o")
    assert _gate(plugin, session="sess-o")["action"] == "block"
    assert _gate(plugin, session="sess-1")["action"] == "block"
    # A non-admin echoing it confirms nothing either (sender-bound).
    _turn(plugin, "sarah@firm.com", message=f"fwd: {nonce}", session="sess-n")
    _turn(plugin, ADMIN, session="sess-verify")
    assert _gate(plugin, session="sess-verify")["action"] == "block"


# ---------------------------------------------------------------------------
# 4. Channel scoping — custody decides
# ---------------------------------------------------------------------------


def test_msgraph_custody_gets_no_ceremony(plugin):
    """Tenant-authenticated custody (ADR 0085 §5): straight through, and no
    possession state is ever minted for it."""
    _FakeCustomerConfig.connectors = {"Email": {"adapter": "msgraph", "enabled": True}}
    _turn(plugin, ADMIN)
    for tool in plugin.ESTABLISH_TOOLS:
        assert _gate(plugin, tool) is None
    assert admin_possession.outstanding_nonces() == {}


def test_no_mail_channel_gets_no_ceremony(plugin):
    _FakeCustomerConfig.connectors = {}
    _turn(plugin, ADMIN)
    assert _gate(plugin) is None
    assert admin_possession.outstanding_nonces() == {}


def test_disabled_email_connector_is_no_mail_channel(plugin):
    _FakeCustomerConfig.connectors = {"Email": {"adapter": "agentmail", "enabled": False}}
    _turn(plugin, ADMIN)
    assert _gate(plugin) is None


def test_unprobed_mail_adapter_gets_the_ceremony(plugin):
    """Unknown custody is spoofable until proven otherwise (fail closed)."""
    _FakeCustomerConfig.connectors = {"Email": {"adapter": "google-gmail", "enabled": True}}
    _turn(plugin, ADMIN)
    assert _gate(plugin)["action"] == "block"


def test_unreadable_config_at_gate_time_refuses(plugin, monkeypatch):
    _turn(plugin, ADMIN)  # classified while the config was readable

    class Broken:
        @classmethod
        def from_volume(cls, path=None):
            raise RuntimeError("volume gone")

    monkeypatch.setattr(plugin, "CustomerConfig", Broken)
    verdict = _gate(plugin)
    assert verdict["action"] == "block"
    assert "fail closed" in verdict["message"]


# ---------------------------------------------------------------------------
# 5. Restart durability
# ---------------------------------------------------------------------------


def test_confirmed_possession_survives_restart(plugin):
    _turn(plugin, ADMIN)
    nonce = _nonce_from(_gate(plugin)["message"])
    _turn(plugin, ADMIN, message=f"confirming {nonce}", session="sess-2")
    # A restart clears the in-process admin stash but not the volume store.
    plugin._ADMIN_STASH.clear()
    _turn(plugin, ADMIN, session="sess-after-restart")
    assert _gate(plugin, session="sess-after-restart") is None


def test_inflight_nonce_survives_restart(plugin):
    _turn(plugin, ADMIN)
    nonce = _nonce_from(_gate(plugin)["message"])
    plugin._ADMIN_STASH.clear()  # restart mid-round-trip
    _turn(plugin, ADMIN, session="sess-after-restart")
    resumed = _gate(plugin, session="sess-after-restart")["message"]
    assert _nonce_from(resumed) == nonce  # the admin's in-flight reply still lands
    context = _turn(plugin, ADMIN, message=f"here: {nonce}", session="sess-reply")
    assert "confirmed" in context["context"]


# ---------------------------------------------------------------------------
# 6. Re-arm on a scope.admins change for the entry
# ---------------------------------------------------------------------------


def test_removed_admin_entry_revokes_and_readd_rearms(db_path):
    issued = admin_possession.verdict(ADMIN, [ADMIN], now=1_000.0)
    assert admin_possession.try_confirm(ADMIN, issued["nonce"], [ADMIN], now=1_001.0)
    assert (
        admin_possession.verdict(ADMIN, [ADMIN], now=1_002.0)["state"]
        == admin_possession.STATE_CONFIRMED
    )
    # The entry leaves scope.admins: possession is revoked with it.
    admin_possession.reconcile([OTHER_ADMIN])
    # Re-added: the ceremony runs afresh, and the OLD nonce cannot confirm.
    rearmed = admin_possession.verdict(ADMIN, [ADMIN, OTHER_ADMIN], now=1_003.0)
    assert rearmed["state"] == admin_possession.STATE_CHALLENGE_ISSUED
    assert rearmed["nonce"] != issued["nonce"]
    assert not admin_possession.try_confirm(
        ADMIN, issued["nonce"], [ADMIN, OTHER_ADMIN], now=1_004.0
    )


def test_unrelated_admins_change_does_not_rearm(db_path):
    issued = admin_possession.verdict(ADMIN, [ADMIN], now=1_000.0)
    assert admin_possession.try_confirm(ADMIN, issued["nonce"], [ADMIN], now=1_001.0)
    grown = admin_possession.verdict(ADMIN, [ADMIN, OTHER_ADMIN], now=1_002.0)
    assert grown["state"] == admin_possession.STATE_CONFIRMED


# ---------------------------------------------------------------------------
# 7. Recipient lock — the code ships only to the rostered admin
# ---------------------------------------------------------------------------


@pytest.fixture
def outstanding(plugin):
    _turn(plugin, ADMIN)
    return _nonce_from(_gate(plugin)["message"])


def test_challenge_send_to_the_rostered_admin_passes(plugin, outstanding):
    args = {"to": ADMIN, "subject": "confirm", "text": f"code: {outstanding}"}
    assert _gate(plugin, tool="mcp_agentmail_send_message", args=args) is None


def test_nonce_to_any_other_recipient_is_blocked(plugin, outstanding):
    args = {"to": ATTACKER, "subject": "fwd", "text": f"code: {outstanding}"}
    verdict = _gate(plugin, tool="mcp_agentmail_send_message", args=args)
    assert verdict is not None and verdict["action"] == "block"
    assert ADMIN in verdict["message"]


def test_nonce_on_a_reply_tool_is_blocked(plugin, outstanding):
    """Replies resolve no ``to`` from args — a reply can honor a hostile
    Reply-To, which is exactly the hijack the rostered-address rule kills."""
    args = {"message_id": "m-1", "text": f"code: {outstanding}"}
    verdict = _gate(plugin, tool="mcp_agentmail_reply_to_message", args=args)
    assert verdict is not None and verdict["action"] == "block"


def test_nonce_in_a_draft_is_locked_to_the_admin_too(plugin, outstanding):
    ok = {"to": ADMIN, "subject": "confirm", "text": outstanding}
    bad = {"to": ATTACKER, "subject": "confirm", "text": outstanding}
    assert _gate(plugin, tool="mcp_agentmail_create_draft", args=ok) is None
    assert _gate(plugin, tool="mcp_agentmail_create_draft", args=bad)["action"] == "block"


def test_sends_without_the_nonce_are_untouched(plugin, outstanding):
    args = {"to": ATTACKER, "subject": "hi", "text": "ordinary mail"}
    assert _gate(plugin, tool="mcp_agentmail_send_message", args=args) is None


def test_consumed_nonce_no_longer_locks_sends(plugin, outstanding):
    _turn(plugin, ADMIN, message=f"confirming {outstanding}", session="sess-2")
    args = {"to": ATTACKER, "subject": "quote", "text": f"stale: {outstanding}"}
    assert _gate(plugin, tool="mcp_agentmail_send_message", args=args) is None


def test_display_name_spoof_on_the_recipient_is_blocked(plugin, outstanding):
    """The lock reads the routable address, not the display text."""
    args = {"to": f"{ADMIN} <{ATTACKER}>", "text": outstanding}
    assert _gate(plugin, tool="mcp_agentmail_send_message", args=args)["action"] == "block"
