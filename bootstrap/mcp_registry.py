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
    # How the platform provisions this connector's credentials (mirrors the
    # author-built connector manifest's auth_model; ADR 0053). One of
    # "static" | "client_credentials" | "authorization_code", or None for
    # vendor entries that predate the field. Additive and informational at the
    # translate layer (static/client_credentials already flow via env_secrets;
    # authorization_code via the existing token-on-volume custody); ss-console
    # provisioning routes secret staging on it (PR-3).
    auth_model: str | None = None
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
    # OPTIONAL per-seat env, same ``(subprocess_env_var, source_secret_env)``
    # shape as ``env_secrets`` but with the opposite missing-source policy: a
    # source that is unset is SKIPPED (the var is left out of the env block) and
    # the server is still wired. For per-seat runtime selections the connector
    # treats as optional (e.g. Smokeball's auth_mode / refresh_token / account_id
    # — present only on an authorization_code or multi-account seat). NEVER use
    # this for a credential the connector requires to function; those belong in
    # ``env_secrets`` so a missing one fail-closes the server.
    env_secrets_optional: tuple[tuple[str, str], ...] = field(default=())
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
# persona gets its OWN inbox (not the principal's Gmail). Hosted MCP, `x-api-key`.
#
# The send tools ARE excluded here as of ss#2258, reversing the earlier posture.
# ADR 0025's principle — autonomous send is a configurable per-action ceiling,
# not a hard ban — is unchanged and still enforced; what changed is which tool
# carries it. The key this server receives is inbox-scoped WITHOUT message_send,
# so these four would 403, and sending now runs through `smd_send_message`
# (hermes-smd-trust), same EXTERNAL_SEND class and same ceiling, executed by the
# broker. Excluding them no longer hides a capability from its governing layer;
# it points the capability at the layer that can actually hold it.
MCP_CONNECTOR_REGISTRY: dict[str, McpConnectorSpec] = {
    "agentmail": McpConnectorSpec(
        name="agentmail",
        auth_model="static",
        url="https://mcp.agentmail.to/mcp",
        auth_header="x-api-key",
        secret_env="AGENTMAIL_API_KEY",
        # Two different reasons for exclusion live in this one tuple.
        #
        # SURFACE REDUCTION (the original eight): inbox-admin and destructive
        # thread/message mutations. No Operator routine provisions inboxes or
        # rewrites message state, and provisioning is Captain-side. Measured on
        # pilot-smokeball 2026-07-15: the full 26-tool catalog costs ~5.2k tokens
        # of prompt-cache write per turn; these were dead weight.
        #
        # GOVERNANCE (the four sends, added for ss#2258): every AgentMail tool
        # that TRANSMITS is off the menu, because the key materialized into
        # config.yaml for this MCP server is now inbox-scoped with message_send
        # and draft_send withheld — the vendor would refuse these calls anyway,
        # and a tool that is advertised but always 403s is worse than one that is
        # absent: the agent retries it, and the failure surfaces as a mystery
        # rather than as a routing decision.
        #
        # This is NOT a capability removal. Sending still happens — through the
        # broker verb, which fences the recipient against the seat's own authored
        # config and writes the audit row itself. What is removed is the agent's
        # ability to transmit directly, at ANY exposure ceiling. That last part
        # matters: enforce.py allows a direct MCP send at the `autonomous` tier,
        # so leaving send_message on the menu would leave the autonomous tier
        # wired to a credential that cannot send.
        #
        # reply_to_message is here for a third reason worth stating: the reply
        # channel owns that path (hermes-smd-reply hooks create_draft and relays),
        # so a model-invoked reply was never the intended route.
        blocked_tools=(
            "create_inbox",
            "delete_inbox",
            "update_inbox",
            "list_organizations",
            "select_organization",
            "delete_thread",
            "update_thread",
            "update_message",
            # --- transmit (ss#2258) ---
            "send_message",
            "send_draft",
            "reply_to_message",
            "forward_message",
        ),
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
        auth_model="authorization_code",
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
    # Reference connector (mcp:reference) — the SYNTHETIC author-built connector
    # platform self-test fixture (ss-console operator/connectors/_reference;
    # ADR 0053). NOT a vendor integration. It is a local stdio MCP server
    # launched by the ABSOLUTE path to its own venv console-script — the install
    # posture every author-built connector uses (Dockerfile installs each
    # connector into /opt/connectors/<dir>/.venv; the dir is `_reference`). Inert
    # unless a customer.yaml binds it (only a test seat does); its `surprise`
    # tool is deliberately unclassified so binding it proves fail-closed REFUSED.
    # auth_model=static exercises the env-secret staging path with a dummy key.
    "reference": McpConnectorSpec(
        name="reference",
        auth_model="static",
        command="/opt/connectors/_reference/.venv/bin/reference-mcp",
        args=(),
        env_static=(("REFERENCE_MODE", "selftest"),),
        env_secrets=(("REFERENCE_API_KEY", "REFERENCE_API_KEY"),),
        blocked_tools=(),
    ),
    # Smokeball (mcp:smokeball) — the law wedge's practice-management system of
    # record, and the FIRST real author-built connector on the ADR 0053 platform
    # (ss-console operator/connectors/smokeball; built + live-verified against the
    # US staging tenant 2026-06-23). A local stdio MCP server launched by the
    # ABSOLUTE path to its own venv console-script.
    #
    # Two auth modes (ADR 0053): client_credentials (default — the server mints its
    # own Bearer from client_id/secret for our own staging tenant) and
    # authorization_code (the firm-delegated grant for a real firm's seat — the
    # connect flow obtains the refresh_token, which is staged per-seat). The
    # mcp_smokeball_<tool> action classes are hand-authored in
    # shared/action_classes.py; trust-account fund-movement verbs are in
    # BANNED_TOOLS and never exposed.
    #
    # SMOKEBALL_ENVIRONMENT is a REQUIRED per-seat secret (staging|production) —
    # NOT a hardcoded static default — so a production seat can never silently
    # default to the staging hosts. Region defaults to "us" in the connector and
    # is an optional per-seat override. auth_mode/refresh_token/account_id are
    # optional per-seat runtime selections (present only on an authorization_code
    # or multi-account seat). The staging/prod → generic secret mapping from /ss
    # SMOKEBALL_STAGING_* / SMOKEBALL_PROD_* happens at provisioning.
    "smokeball": McpConnectorSpec(
        name="smokeball",
        auth_model="client_credentials",
        command="/opt/connectors/smokeball/.venv/bin/smokeball-mcp",
        args=(),
        env_static=(),
        env_secrets=(
            ("SMOKEBALL_CLIENT_ID", "SMOKEBALL_CLIENT_ID"),
            ("SMOKEBALL_CLIENT_SECRET", "SMOKEBALL_CLIENT_SECRET"),
            ("SMOKEBALL_API_KEY", "SMOKEBALL_API_KEY"),
            # Required per-seat: no safe default — a missing value fail-closes the
            # server rather than risk a prod seat pointing at staging hosts.
            ("SMOKEBALL_ENVIRONMENT", "SMOKEBALL_ENVIRONMENT"),
        ),
        env_secrets_optional=(
            ("SMOKEBALL_REGION", "SMOKEBALL_REGION"),  # connector defaults to "us"
            ("SMOKEBALL_AUTH_MODE", "SMOKEBALL_AUTH_MODE"),  # default client_credentials
            ("SMOKEBALL_REFRESH_TOKEN", "SMOKEBALL_REFRESH_TOKEN"),  # authorization_code seats
            ("SMOKEBALL_ACCOUNT_ID", "SMOKEBALL_ACCOUNT_ID"),  # multi-account seats
        ),
        blocked_tools=(),
    ),
    # Microsoft Graph mail (mcp:msgraph-mail) — the client-custody email connector
    # (ADR 0078 / email-channel-seam D4; ss-console operator/connectors/msgraph-mail,
    # live-verified app-only against the smdopslab sandbox mailbox 2026-07-24). Local
    # stdio MCP server launched by the ABSOLUTE path to its own venv console-script,
    # same shape as smokeball. The server is named "msgraph-mail"; Hermes sanitizes
    # the hyphen so runtime tools are mcp_msgraph_mail_<tool> (matches the
    # hand-authored classes in shared/action_classes.py). App-only client_credentials:
    # the four MSGRAPH_* are REQUIRED per-seat secrets the connector validates at
    # construction (a missing one fail-closes the server rather than booting a
    # half-wired mail surface). Tenant-side least privilege is the Exchange
    # ApplicationAccessPolicy pinning the app to exactly the operator mailbox,
    # enforced outside this process. INBOUND does NOT run through this server (the
    # overlay delta poller pulls + fences); this materializes the OUTBOUND/read tool
    # surface the agent acts with (send / reply / draft / list / read).
    "msgraph-mail": McpConnectorSpec(
        name="msgraph-mail",
        auth_model="client_credentials",
        command="/opt/connectors/msgraph-mail/.venv/bin/msgraph-mail-mcp",
        args=(),
        env_static=(),
        env_secrets=(
            ("MSGRAPH_TENANT_ID", "MSGRAPH_TENANT_ID"),
            ("MSGRAPH_CLIENT_ID", "MSGRAPH_CLIENT_ID"),
            ("MSGRAPH_CLIENT_SECRET", "MSGRAPH_CLIENT_SECRET"),
            ("MSGRAPH_MAILBOX", "MSGRAPH_MAILBOX"),
        ),
        env_secrets_optional=(),
        # ss#2258: the two EXTERNAL_SEND tools leave the menu, replaced by the
        # broker-backed `smd_send_message` (hermes-smd-trust), which carries the
        # same action class and so the same authored ceiling.
        #
        # The reason is sharper here than it was for AgentMail. There, the
        # gateway's key simply could not transmit, so an advertised send tool
        # would have 403'd. Here the credential CAN transmit — which is worse: on
        # a seat whose posture is `autonomous` the gate returns allow and this
        # tool executes, reaching Graph directly with no recipient fence and no
        # audit row. That is the incident's own shape, still reachable, on
        # precisely the seats that withhold least.
        #
        # reply_message goes for the additional reason its AgentMail counterpart
        # did: the reply channel owns that path (hermes-smd-reply hooks
        # create_draft and relays through the broker), so a model-invoked reply
        # was never the intended route.
        blocked_tools=(
            "send_message",
            "reply_message",
        ),
    ),
    # NOTE: web search is NOT an mcp: connector. It is wired natively via Hermes'
    # bundled web providers (plugins/web/*, e.g. brave-free) and selected by
    # `connectors.WebSearch.backend: native:<provider>` -> config web.search_backend
    # in bootstrap.translate._materialize_web_search. The former mcp:brave entry
    # (ADR 0070, first cut) was removed 2026-07-08: MCP-wrapping a native feature
    # was the redundant layer. See shared/action_classes.py ("web_search").
}


__all__ = ["McpConnectorSpec", "MCP_CONNECTOR_REGISTRY"]
