"""MCP connector registry — vendor wiring for ``mcp:`` connector backends.

``customer.yaml`` declares connectors abstractly::

    connectors:
      Email:
        adapter: agentmail
        backend: mcp:agentmail
        enabled: true

The ``backend`` prefix ``mcp:<name>`` says "wire this capability to the MCP
server named ``<name>``" (ADR 0020: MCP-first connectors). This module is the
lookup from that ``<name>`` to the concrete Hermes ``mcp_servers`` entry —
endpoint URL, the auth header carrying the key, the env var the key lives in,
and the native send-tools that must be excluded from the agent's toolset.

:func:`bootstrap.translate._materialize_mcp_servers` consumes this registry to
emit the per-profile ``mcp_servers`` block. A ``mcp:`` backend whose name is
NOT in this registry is left unwired (logged, not fatal) — that covers the
OAuth-based Google connectors, which are wired by a different (token-on-volume)
path, not by a static header key.

Adding a header-key MCP vendor = one :data:`MCP_CONNECTOR_REGISTRY` entry. No
code change in the translator.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class McpConnectorSpec:
    """Concrete wiring for one ``mcp:<name>`` connector backend.

    Attributes:
        name: The MCP server name (the part after ``mcp:``). Used as the
            key in the generated ``mcp_servers`` mapping AND as the
            ``<server>:`` prefix on runtime tool names.
        url: The hosted MCP endpoint URL.
        auth_header: HTTP header name carrying the API key (e.g.
            ``x-api-key``). ``None`` for servers that authenticate another
            way (OAuth) — those are not materialized here.
        secret_env: Process env var holding the key value (populated from a
            Fly secret at boot). ``None`` when ``auth_header`` is ``None``.
        blocked_tools: Native (un-prefixed) tool names that must be excluded
            from the agent's toolset — the autonomous-send capabilities
            (ADR 0005 reviewer-as-sender). These are emitted into the server's
            ``tools.exclude`` list. They are ALSO banned at the trust layer
            (``shared.action_classes.BANNED_TOOLS``, under the ``<name>:``
            prefix) as the durable guarantee; the exclude here keeps them off
            the menu in normal operation. The two lists are kept consistent by
            a test.
    """

    name: str
    url: str
    auth_header: str | None = None
    secret_env: str | None = None
    blocked_tools: tuple[str, ...] = field(default=())


# AgentMail (https://agentmail.to) — API-first email built for AI agents. The
# persona gets its OWN inbox (not the principal's Gmail); the native
# create_draft / send separation maps onto reviewer-as-sender exactly. Hosted
# MCP authenticates with an `x-api-key` header.
MCP_CONNECTOR_REGISTRY: dict[str, McpConnectorSpec] = {
    "agentmail": McpConnectorSpec(
        name="agentmail",
        url="https://mcp.agentmail.to/mcp",
        auth_header="x-api-key",
        secret_env="AGENTMAIL_API_KEY",
        blocked_tools=(
            "send_message",
            "send_draft",
            "reply_to_message",
            "forward_message",
        ),
    ),
}


__all__ = ["McpConnectorSpec", "MCP_CONNECTOR_REGISTRY"]
