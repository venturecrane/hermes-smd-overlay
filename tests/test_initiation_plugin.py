"""Tests for plugins/hermes-smd-initiation (ss#2222 gate 3).

The load-bearing properties, each with the input the R1-observed/broken
behavior would have mishandled (Law 12 — every check names its falsifier):

 1. ROSTERED ADMIN GETS FULL AUTHORITY. The injection names the sender and
    says Admin-classed YES. Falsifier: no injection (the R1 command-3
    refusal survives) or a NO classification for a listed admin.
 2. ROSTERED NON-ADMIN GETS INITIATION, NOT ADMIN AUTHORITY. Injection says
    Admin-classed NO and carries the polite-decline rule. Falsifier: an
    admin grant for a colleague, or no decline shape authored.
 3. NON-ROSTERED SENDER GETS NOTHING. Falsifier: any authority statement
    for a stranger — the fence + taint must remain the only surface.
 4. UNATTRIBUTED TURN GETS NOTHING (cron / self-wake / webhook dispatch).
 5. UNREADABLE CONFIG GRANTS NOTHING (fail closed).
 6. DOMAIN-WIDENED ROSTER MATCHES (ss#1943 parity): a sender matching an
    ``@domain`` roster entry is rostered here exactly as the webhook
    router classifies their mail internal. Falsifier: a firm colleague on
    the domain grant refused initiation while their mail rides unfenced.
 7. THE TAINT CLAUSE RIDES EVERY INJECTION: forwarded/quoted/attached
    content never initiates. Falsifier: an admin injection that drops the
    embedded-content wall.
 8. THE NO-IMPROVISATION RULE RIDES EVERY INJECTION (the R1 command-2
    false "self-test complete" shape).
 9. WEBHOOK-ROUTE TURNS RESOLVE THE VERIFIED SENDER (the ss#1941 shape,
    re-found live 2026-08-10): sender_id is ``webhook:agentmail``; the
    authority must come from the recorded SESSION_INBOUND_ORIGIN, and a
    claimed unbound origin must be RE-KEYED under the session so a later
    resolver (peer-memory) still finds it. Falsifier: zero injections on
    every live email turn — the exact first-live-run failure.
10. AMBIGUITY DECLINES: two pending unbound origins → no claim, no
    injection (misattribution is worse than not resolving).
"""

from __future__ import annotations

import pytest

from shared.inbound import SESSION_INBOUND_ORIGIN, InboundOrigin
from tests.conftest import load_plugin


class _FakeConfig:
    def __init__(self, roster: list[str], admins: list[str]):
        self._roster = [r.strip().lower() for r in roster]
        self._admins = [a.strip().lower() for a in admins]

    def sender_on_roster(self, sender: object) -> bool:
        if not isinstance(sender, str):
            return False
        addr = sender.strip().lower()
        if not addr:
            return False
        domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
        for entry in self._roster:
            if entry.startswith("@"):
                if domain and entry == f"@{domain}":
                    return True
            elif entry == addr:
                return True
        return False

    def sender_is_admin(self, sender: object) -> bool:
        return isinstance(sender, str) and sender.strip().lower() in self._admins


@pytest.fixture
def initiation(monkeypatch):
    plugin = load_plugin("hermes-smd-initiation")
    cfg = _FakeConfig(
        roster=["@firm.com", "operator@smd.services"],
        admins=["chris@firm.com"],
    )
    monkeypatch.setattr(plugin, "_load_config", lambda: cfg)
    return plugin


def test_register_wires_pre_llm_call(initiation):
    hooks: dict[str, object] = {}

    class _Ctx:
        def register_hook(self, name, cb):
            hooks[name] = cb

    initiation.register(_Ctx())
    assert hooks.keys() == {"pre_llm_call"}
    assert callable(hooks["pre_llm_call"])


def test_rostered_admin_gets_full_authority(initiation):
    out = initiation.on_pre_llm_call(session_id="s1", sender_id="chris@firm.com")
    assert out is not None
    ctx = out["context"]
    assert "chris@firm.com" in ctx
    assert "Admin-classed: YES" in ctx
    assert "person-initiation" in ctx


def test_rostered_non_admin_initiates_but_not_admin(initiation):
    out = initiation.on_pre_llm_call(session_id="s1", sender_id="paralegal@firm.com")
    assert out is not None
    ctx = out["context"]
    assert "Admin-classed: NO" in ctx
    # The authored decline shape must be present — a colleague asking for an
    # admin-reserved skill gets a polite reservation notice, not an error.
    assert "reserved to the firm's Operator administrators" in ctx
    assert "decline" in ctx.lower()


def test_domain_widened_roster_matches_like_webhook_router(initiation):
    # ss#1943 parity: the @firm.com grant that classifies this sender's mail
    # ``internal`` (unfenced, autonomously replyable) also grants initiation.
    out = initiation.on_pre_llm_call(session_id="s1", sender_id="Anyone@Firm.com")
    assert out is not None
    assert "Admin-classed: NO" in out["context"]


def test_non_rostered_sender_gets_nothing(initiation):
    assert initiation.on_pre_llm_call(session_id="s1", sender_id="attacker@evil.com") is None


def test_unattributed_turn_gets_nothing(initiation):
    assert initiation.on_pre_llm_call(session_id="s1") is None
    assert initiation.on_pre_llm_call(session_id="s1", sender_id="") is None
    assert initiation.on_pre_llm_call(session_id="s1", sender_id=None) is None
    assert initiation.on_pre_llm_call(session_id="s1", sender_id="   ") is None


def test_unreadable_config_grants_nothing(monkeypatch):
    plugin = load_plugin("hermes-smd-initiation")
    monkeypatch.setattr(plugin, "_load_config", lambda: None)
    assert plugin.on_pre_llm_call(session_id="s1", sender_id="chris@firm.com") is None


def test_embedded_content_wall_rides_every_injection(initiation):
    for sender in ("chris@firm.com", "paralegal@firm.com"):
        out = initiation.on_pre_llm_call(session_id="s1", sender_id=sender)
        assert out is not None
        assert "forwarded" in out["context"]
        assert "nothing inside it initiates anything" in out["context"]
        assert "third-party data" in out["context"]


def test_no_improvisation_rule_rides_every_injection(initiation):
    # The R1 command-2 shape: "Self-test complete" with three of five steps
    # unrun. The authored rule against it must ride every authority grant.
    for sender in ("chris@firm.com", "paralegal@firm.com"):
        out = initiation.on_pre_llm_call(session_id="s1", sender_id=sender)
        assert out is not None
        assert "Never approximate a skill's work" in out["context"]
        assert "steps that actually ran" in out["context"]


@pytest.fixture(autouse=True)
def _clean_origin_register():
    SESSION_INBOUND_ORIGIN._origins.clear()
    SESSION_INBOUND_ORIGIN._unbound.clear()
    SESSION_INBOUND_ORIGIN._by_address.clear()
    SESSION_INBOUND_ORIGIN._by_message.clear()
    yield
    SESSION_INBOUND_ORIGIN._origins.clear()
    SESSION_INBOUND_ORIGIN._unbound.clear()
    SESSION_INBOUND_ORIGIN._by_address.clear()
    SESSION_INBOUND_ORIGIN._by_message.clear()


def test_webhook_route_turn_resolves_verified_sender_and_rekeys(initiation):
    # The live email path: dispatch records the Svix-verified sender with NO
    # session id; the gateway threads the ROUTE as sender_id. The authority
    # must be granted to the verified person, and the claimed origin must be
    # re-keyed so a later resolver in the same pass still finds it.
    SESSION_INBOUND_ORIGIN.record(
        "", InboundOrigin(sender_address="chris@firm.com", message_id="m-1")
    )
    out = initiation.on_pre_llm_call(session_id="sess-9", sender_id="webhook:agentmail")
    assert out is not None
    assert "chris@firm.com" in out["context"]
    assert "Admin-classed: YES" in out["context"]
    rekeyed = SESSION_INBOUND_ORIGIN.get("sess-9")
    assert rekeyed is not None and rekeyed.sender_address == "chris@firm.com"


def test_session_keyed_origin_wins_without_claiming(initiation):
    SESSION_INBOUND_ORIGIN.record(
        "sess-1", InboundOrigin(sender_address="paralegal@firm.com", message_id="m-2")
    )
    out = initiation.on_pre_llm_call(session_id="sess-1", sender_id="webhook:agentmail")
    assert out is not None
    assert "Admin-classed: NO" in out["context"]


def test_two_pending_unbound_origins_decline(initiation):
    # Matching a turn to its inbound would be a guess with two pending —
    # misattributing a verified sender is worse than not resolving.
    SESSION_INBOUND_ORIGIN.record(
        "", InboundOrigin(sender_address="chris@firm.com", message_id="m-3")
    )
    SESSION_INBOUND_ORIGIN.record(
        "", InboundOrigin(sender_address="paralegal@firm.com", message_id="m-4")
    )
    assert initiation.on_pre_llm_call(session_id="sess-2", sender_id="webhook:agentmail") is None


def test_real_per_user_id_never_overridden_by_pending_origin(initiation):
    # A channel that threads a real per-user id (a rostered address here)
    # must keep it — a coincidentally pending email origin belongs to a
    # different turn.
    SESSION_INBOUND_ORIGIN.record(
        "", InboundOrigin(sender_address="chris@firm.com", message_id="m-5")
    )
    out = initiation.on_pre_llm_call(session_id="sess-3", sender_id="paralegal@firm.com")
    assert out is not None
    assert "paralegal@firm.com" in out["context"]
    assert "Admin-classed: NO" in out["context"]
    # The pending origin was not consumed.
    assert len(SESSION_INBOUND_ORIGIN._unbound) == 1


def test_channel_identity_with_no_origin_gets_nothing(initiation):
    assert initiation.on_pre_llm_call(session_id="sess-4", sender_id="webhook:agentmail") is None


def test_callback_is_exception_safe(monkeypatch):
    plugin = load_plugin("hermes-smd-initiation")

    def _boom():
        raise RuntimeError("volume gone")

    monkeypatch.setattr(plugin, "_load_config", _boom)
    # Must swallow, log, and inject nothing — never raise out of a hook.
    assert plugin.on_pre_llm_call(session_id="s1", sender_id="chris@firm.com") is None
