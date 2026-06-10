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

Two transports are supported: a hosted-HTTP server (a ``url`` + optional
API-key header, e.g. AgentMail) and a local stdio server (a launched
``command`` + ``args`` + per-subprocess ``env``, e.g. Clio). Adding a vendor of
either kind = one :data:`MCP_CONNECTOR_REGISTRY` entry; the translator branches
on :attr:`McpConnectorSpec.transport`.
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
        command: For a LOCAL stdio MCP server (e.g. Clio), the launch command
            (a binary on PATH, e.g. ``clio-mcp``). ``None`` for hosted-URL
            servers. Setting it switches the spec to the stdio transport.
        args: Argument vector passed to ``command``.
        env_secrets: ``(subprocess_env_var, source_secret_env)`` pairs for a
            stdio server. Each value is read from the process env via
            ``get_secret`` at materialize time and written into the server's
            ``env`` block, supporting a remap (subprocess wants ``ENCRYPTION_KEY``
            while the Fly secret is ``CLIO_ENCRYPTION_KEY``). All pairs are
            required; a missing source leaves the server unwired this boot.
        blocked_tools: Native (un-prefixed) tool names that must be excluded
            from the agent's toolset — the autonomous-send capabilities
            (ADR 0035). These are emitted into the server's
            ``tools.exclude`` list. They are ALSO banned at the trust layer
            (``shared.action_classes.BANNED_TOOLS``, under the ``<name>:``
            prefix) as the durable guarantee; the exclude here keeps them off
            the menu in normal operation. The two lists are kept consistent by
            a test.
    """

    name: str
    # hosted-HTTP transport (e.g. AgentMail): a URL + optional API-key header
    url: str | None = None
    auth_header: str | None = None
    secret_env: str | None = None
    # local stdio transport (e.g. Clio): a launched command + per-subprocess env.
    # Hermes' mcp_config supports both {url} and {command, args, env} server shapes
    # (NousResearch/hermes-agent hermes_cli/mcp_config.py).
    command: str | None = None
    args: tuple[str, ...] = field(default=())
    env_secrets: tuple[tuple[str, str], ...] = field(default=())
    # Static (non-secret) env for a stdio subprocess: ``(var, literal_value)``
    # pairs written verbatim into the server's ``env`` block. For CLI-mode
    # switches the binary needs but that aren't secrets — e.g. clio-mcp's
    # ``TRANSPORT=stdio`` (without it the binary defaults to HTTP mode and
    # fatals on a missing MCP_BASE_URL). Applied before ``env_secrets``.
    env_static: tuple[tuple[str, str], ...] = field(default=())
    blocked_tools: tuple[str, ...] = field(default=())

    @property
    def transport(self) -> str:
        """``"stdio"`` when a launch ``command`` is set, otherwise ``"http"``."""
        return "stdio" if self.command else "http"


# AgentMail (https://agentmail.to) — API-first email built for AI agents. The
# persona gets its OWN inbox (not the principal's Gmail). The send tools are
# NOT excluded: ADR 0025 makes autonomous send a CONFIGURABLE per-action
# ceiling, so the sends stay on the menu and are governed by the trust layer
# (default draft_for_review; raised to autonomous only by
# authored action_ceilings; the content-sensitivity floor forces money /
# contract / scope / legal to draft regardless). Excluding them here would
# hide the capability from the very layer meant to govern it. Hosted MCP
# authenticates with an `x-api-key` header.
MCP_CONNECTOR_REGISTRY: dict[str, McpConnectorSpec] = {
    "agentmail": McpConnectorSpec(
        name="agentmail",
        url="https://mcp.agentmail.to/mcp",
        auth_header="x-api-key",
        secret_env="AGENTMAIL_API_KEY",
        blocked_tools=(),
    ),
    # Clio (oktopeak/clio-mcp v2.0.0, MIT) — practice-management system of record
    # for the law vertical. Unlike AgentMail this is a LOCAL stdio server: the
    # `clio-mcp` binary (installed into the image from @oktopeak/clio-mcp) is
    # launched per profile and reads its OAuth token from ~/.clio-mcp/tokens.enc
    # (seeded by bootstrap.sh from the CLIO_TOKENS_ENC_B64 Fly secret) decrypted
    # with ENCRYPTION_KEY. The client_id/secret + encryption key flow in via the
    # env block below. Writes (create_matter/task/note) are NOT excluded here —
    # the wedge skills are authored draft-and-surface, and tool-level gating is
    # governed at the trust layer (follow-on hardening); excluding them would hide
    # the capability from the layer meant to govern it (same rationale as AgentMail).
    "clio-oktopeak": McpConnectorSpec(
        name="clio-oktopeak",
        command="clio-mcp",
        args=(),
        # clio-mcp defaults to HTTP mode and fatals ("MCP_BASE_URL is required
        # in HTTP mode") unless told to run as a local stdio server. Hermes
        # launches it over stdio, so pin TRANSPORT=stdio.
        env_static=(("TRANSPORT", "stdio"),),
        env_secrets=(
            ("CLIO_CLIENT_ID", "CLIO_CLIENT_ID"),
            ("CLIO_CLIENT_SECRET", "CLIO_CLIENT_SECRET"),
            ("ENCRYPTION_KEY", "CLIO_ENCRYPTION_KEY"),  # remap: subprocess reads ENCRYPTION_KEY
        ),
        blocked_tools=(),
    ),
}


__all__ = ["McpConnectorSpec", "MCP_CONNECTOR_REGISTRY"]
