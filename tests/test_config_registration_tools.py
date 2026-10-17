"""Config-registration tools are off the menu AND taint-gated (overlay review OS-2).

THE FINDING. ``mcp_smokeball_create_webhook_subscription`` was classified
INTERNAL_WRITE. The taint gate (``_TAINT_GATED_CLASSES`` in
plugins/hermes-smd-trust/enforce.py) leaves INTERNAL_WRITE open on a turn that
ingested untrusted inbound content, and A&P authors ``internal_write`` at a
ceiling that allows it. So an injected inbound email could make the seat register
a PERSISTENT Smokeball webhook that pushes firm matter events to an attacker's URL
from then on.

THE FIX, two walls:
  1. The tool is excluded from the seat's tool menu (smokeball ``blocked_tools``,
     emitted as the server's ``tools.exclude``). No routine registers webhooks at
     runtime; that is provisioning.
  2. If it ever comes back on the menu, it classifies COMMITMENT, which the taint
     gate refuses on a tainted turn even with an approval present and every
     ceiling at its most permissive.

AgentMail's inbox provisioning verbs (create_inbox / update_inbox) are the same
shape (account config, not drafting) and get the same second wall; they were
already off the menu.
"""

from __future__ import annotations

import pytest

from bootstrap.mcp_registry import MCP_CONNECTOR_REGISTRY
from shared.action_classes import ActionClass, classify_tool
from tests.conftest import load_plugin

# (registry server, native tool, runtime name)
CONFIG_REGISTRATION_TOOLS = (
    ("smokeball", "create_webhook_subscription", "mcp_smokeball_create_webhook_subscription"),
    ("agentmail", "create_inbox", "mcp_agentmail_create_inbox"),
    ("agentmail", "update_inbox", "mcp_agentmail_update_inbox"),
)


@pytest.mark.parametrize(("server", "tool", "runtime"), CONFIG_REGISTRATION_TOOLS)
def test_config_registration_tool_is_off_the_menu(server: str, tool: str, runtime: str) -> None:
    assert tool in MCP_CONNECTOR_REGISTRY[server].blocked_tools


@pytest.mark.parametrize(("server", "tool", "runtime"), CONFIG_REGISTRATION_TOOLS)
def test_config_registration_tool_is_taint_gated(server: str, tool: str, runtime: str) -> None:
    enforce = load_plugin("hermes-smd-trust").enforce
    action = classify_tool(runtime).action_class
    assert action in enforce._TAINT_GATED_CLASSES, f"{runtime} classifies {action}"


@pytest.mark.parametrize(("server", "tool", "runtime"), CONFIG_REGISTRATION_TOOLS)
def test_injected_email_cannot_register_config(server: str, tool: str, runtime: str) -> None:
    """The attack itself: a tainted turn, every class at its most permissive
    ceiling, an approval flag set. The call must still be refused."""
    enforce = load_plugin("hermes-smd-trust").enforce
    action = classify_tool(runtime).action_class
    wide_open = dict.fromkeys(enforce.ActionClass, enforce.Ceiling.AUTONOMOUS)
    decision = enforce.enforce(
        action=action,
        exposure=wide_open,
        tool_name=runtime,
        current_turn_approval=True,
        inbound_trust_class="external_untrusted",
    )
    assert decision.allowed is False
    assert decision.audit_action == "refuse"


def test_webhook_subscription_is_not_banned_so_the_boot_probe_can_resolve_it() -> None:
    """ss-console's boot probe (operator/templates/connector-classification-probe.py)
    resolves every manifest-declared tool through classify_tool(); a banned name
    raises there and kills the boot. The manifest must therefore declare it with
    the class asserted here."""
    verdict = classify_tool("mcp_smokeball_create_webhook_subscription")
    assert verdict.action_class is ActionClass.COMMITMENT
    assert verdict.unmapped is False


def test_translate_excludes_webhook_subscription_from_smokeball(monkeypatch) -> None:
    from bootstrap.translate import _materialize_mcp_servers

    for var, value in (
        ("SMOKEBALL_CLIENT_ID", "cid"),
        ("SMOKEBALL_CLIENT_SECRET", "sec"),
        ("SMOKEBALL_API_KEY", "key"),
        ("SMOKEBALL_ENVIRONMENT", "staging"),
    ):
        monkeypatch.setenv(var, value)
    servers = _materialize_mcp_servers(
        {"PracticeManagement": {"enabled": True, "backend": "mcp:smokeball"}}
    )
    assert "create_webhook_subscription" in servers["smokeball"]["tools"]["exclude"]
    # The read stays: listing subscriptions is how the Captain audits them.
    assert "get_webhook_subscriptions" not in servers["smokeball"]["tools"]["exclude"]
