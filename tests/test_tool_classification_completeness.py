"""Completeness gate for tool-action-class classification (EFF-07).

THE FINDING. ``shared.action_classes.classify_tool()`` maps an unknown tool
name to ``ActionClass.READ`` with ``unmapped=True`` (the conservative
fallback). ``plugins/hermes-smd-trust/enforce.py`` then ALWAYS allows READ
under any non-REFUSED ceiling (``enforce.py`` ~L296) and the taint-gate gates
only EXTERNAL_SEND / DESTRUCTIVE / COMMITMENT / CODE_EXECUTION — never READ.
Net effect: a write-capable tool that nobody added to ``TOOL_ACTION_CLASS_MAP``
classifies as READ and executes AUTONOMOUSLY, even on a turn tainted by
injected inbound content. The known surfaces are mapped, but the convention
"every tool MUST appear in TOOL_ACTION_CLASS_MAP or BANNED_TOOLS" (a comment
in action_classes.py) was enforced by NO test. A forgotten or newly-vendored
write verb shipped a fail-open hole silently.

THE GATE. This module forces every tool the overlay's connectors can register
to be a DECIDED tool — present in ``TOOL_ACTION_CLASS_MAP`` or ``BANNED_TOOLS``
— so that a new, unmapped verb fails CI instead of falling to the READ default.

ANCHORS. The overlay has two in-repo, enumerable sources of "tools that can
register", plus one external surface that the overlay does not vendor:

  1. ``bootstrap.mcp_registry.MCP_CONNECTOR_REGISTRY`` — the ``mcp:`` server
     keys (e.g. ``agentmail``, ``clio-oktopeak``). Hermes registers each
     server's tools as ``mcp_<server>_<tool>`` (underscore-joined, dashes in
     the server name folded to underscores — see the runtime-naming note in
     action_classes.py). Every WIREABLE server must therefore have its tool
     surface classified under that prefix, OR be an explicit, rationale-bearing
     carve-out in ``UNCLASSIFIED_CONNECTORS_BY_DESIGN`` below.
  2. ``plugins/hermes-smd-workspace`` ``TOOLS`` — the build-side Google
     Workspace tools the overlay itself registers in-process. Every one must
     be classified.

  The Smokeball wedge backend (``mcp_smokeball_*``) is classified in
  action_classes.py but is NOT in MCP_CONNECTOR_REGISTRY — it is a ``build:``
  adapter wired by another path. Its documented surface is pinned below so a
  newly-added Smokeball verb that misses classification fails here too.

WHAT THIS TEST IS NOT. It does not assert the COMPLETE external tool list of
a vendored MCP package the overlay does not ship (e.g. @oktopeak/clio-mcp):
that list is not enumerable from this repo, so asserting it would encode an
unverified guess. Instead the gate forces a per-connector DECISION — classify
the surface, or declare the connector dormant with a written reason — which is
the property that actually prevents a fail-open hole.
"""

from __future__ import annotations

from bootstrap.mcp_registry import MCP_CONNECTOR_REGISTRY
from shared.action_classes import BANNED_TOOLS, TOOL_ACTION_CLASS_MAP
from tests.conftest import load_plugin


def _mcp_prefix(server_name: str) -> str:
    """Runtime tool-name prefix for an MCP server.

    Mirrors the Hermes naming the classifier targets: ``mcp_<server>_`` with
    dashes in the server name folded to underscores (action_classes.py L203).
    ``clio-oktopeak`` -> ``mcp_clio_oktopeak_``.
    """
    return f"mcp_{server_name.replace('-', '_')}_"


def _is_decided(tool_name: str) -> bool:
    """A tool is DECIDED iff it is mapped or explicitly banned.

    Both forms keep the tool off the unmapped->READ fallback: a mapped tool
    carries its real action class; a banned tool raises ``BannedToolError``
    before enforcement. Anything else is an undecided fail-open surface.
    """
    return tool_name in TOOL_ACTION_CLASS_MAP or tool_name in BANNED_TOOLS


# ---------------------------------------------------------------------------
# Connectors deliberately left without a classified tool surface.
#
# Membership here is a reviewed security DECISION, not a default. A connector
# may sit in MCP_CONNECTOR_REGISTRY without any classified tools ONLY if it is
# not bound to any live customer — i.e. it is wireable in principle but
# dormant in practice. Each entry MUST carry a rationale AND a follow-up
# pointer, because a dormant-but-wireable registry entry is itself a latent
# fail-open surface: the moment a customer.yaml binds it, every one of its
# write verbs would classify READ and run autonomously. Removing the dormant
# entry from the registry entirely is the stronger fix; that is a coordinated
# call for the lead (see this PR's description).
# ---------------------------------------------------------------------------
UNCLASSIFIED_CONNECTORS_BY_DESIGN: dict[str, str] = {
    # clio-oktopeak (@oktopeak/clio-mcp): the law wedge's ORIGINAL practice-
    # management backend. Superseded by the build-side Smokeball adapter
    # ("the Operator is a Smokeball expert, not a Clio facade" — law-firm
    # smokeball-surface.md). The registry entry and its translate path are
    # retained, but NO in-repo customer config binds mcp:clio-oktopeak (only
    # tests/test_bootstrap_translate.py exercises it synthetically). Its tools
    # are therefore intentionally unclassified: were it bound to a live
    # customer, every Clio write verb (e.g. mcp_clio_oktopeak_create_matter)
    # would hit the unmapped->READ fallback and run autonomously — the exact
    # EFF-07 hole. Disposition deferred to the lead: either (a) classify the
    # full @oktopeak/clio-mcp surface here, or (b) drop the dormant
    # clio-oktopeak entry from MCP_CONNECTOR_REGISTRY. Tracked in this PR.
    "clio-oktopeak": (
        "dormant — superseded by the Smokeball build adapter; bound to no live "
        "customer config; pending lead disposition (classify-surface vs "
        "remove-registry-entry)"
    ),
}


# ---------------------------------------------------------------------------
# Pinned connector surfaces with a known runtime tool list.
#
# For connectors whose tool surface IS enumerable from in-repo documentation,
# we pin the full runtime tool-name list. The point is drift defense: a NEW
# verb added to one of these surfaces (a vendor adds a tool, or we extend the
# build adapter) that is not classified fails THIS test rather than shipping a
# fail-open hole. Names are the full ``mcp_<server>_<tool>`` runtime form.
# ---------------------------------------------------------------------------
PINNED_CONNECTOR_SURFACES: dict[str, frozenset[str]] = {
    # AgentMail (mcp:agentmail) — the persona's own mailbox. The 24-tool
    # surface is enumerated in action_classes.py from a live tools/list
    # (2026-06-12); pinned here so a 25th agentmail verb cannot slip in
    # unclassified. Sends are EXTERNAL_SEND (ceiling-governed), drafts/inbox
    # mutations INTERNAL_WRITE, delete_inbox/delete_thread DESTRUCTIVE, reads
    # READ — every one MUST be decided.
    "agentmail": frozenset(
        {
            "mcp_agentmail_send_message",
            "mcp_agentmail_send_draft",
            "mcp_agentmail_reply_to_message",
            "mcp_agentmail_forward_message",
            "mcp_agentmail_create_draft",
            "mcp_agentmail_update_draft",
            "mcp_agentmail_create_inbox",
            "mcp_agentmail_update_inbox",
            "mcp_agentmail_update_thread",
            "mcp_agentmail_update_message",
            "mcp_agentmail_delete_draft",
            "mcp_agentmail_delete_inbox",
            "mcp_agentmail_delete_thread",
            "mcp_agentmail_list_inboxes",
            "mcp_agentmail_get_inbox",
            "mcp_agentmail_list_threads",
            "mcp_agentmail_search_threads",
            "mcp_agentmail_get_thread",
            "mcp_agentmail_get_attachment",
            "mcp_agentmail_list_messages",
            "mcp_agentmail_search_messages",
            "mcp_agentmail_list_drafts",
            "mcp_agentmail_get_draft",
            "mcp_agentmail_auth_me",
        }
    ),
    # Smokeball (mcp:smokeball) — the law wedge's system of record. NOT in
    # MCP_CONNECTOR_REGISTRY (a build: adapter wired by another path), but its
    # surface is authored by us, so we pin it. Source: law-firm
    # smokeball-surface.md. Trust-account writes (create_transaction /
    # protect_funds / unprotect_funds) are in BANNED_TOOLS, not mapped — both
    # forms count as decided. A new Smokeball verb added to the adapter must
    # be classified or it fails here.
    "smokeball": frozenset(
        {
            # reads
            "mcp_smokeball_auth_status",
            "mcp_smokeball_list_matters",
            "mcp_smokeball_get_matter",
            "mcp_smokeball_list_matter_types",
            "mcp_smokeball_get_stage_sets",
            "mcp_smokeball_get_stage_to_matter_mappings",
            "mcp_smokeball_get_contacts",
            "mcp_smokeball_get_contact",
            "mcp_smokeball_get_contact_relations",
            "mcp_smokeball_list_tasks",
            "mcp_smokeball_get_task",
            "mcp_smokeball_search_staff",
            "mcp_smokeball_get_staff",
            "mcp_smokeball_get_roles_on_matter",
            "mcp_smokeball_get_relationships_on_matter",
            "mcp_smokeball_get_files_on_matter",
            "mcp_smokeball_get_file",
            "mcp_smokeball_get_download_url",
            "mcp_smokeball_get_memos_on_matter",
            "mcp_smokeball_get_bank_accounts",
            "mcp_smokeball_get_matter_balances",
            "mcp_smokeball_get_matter_billing_config",
            "mcp_smokeball_get_fees",
            "mcp_smokeball_get_expenses",
            "mcp_smokeball_get_webhook_subscriptions",
            "mcp_smokeball_get_event_types",
            # writes (mapped)
            "mcp_smokeball_create_memo",
            "mcp_smokeball_patch_matter",
            "mcp_smokeball_create_contact",
            "mcp_smokeball_create_task",
            "mcp_smokeball_update_task",
            "mcp_smokeball_add_file",
            "mcp_smokeball_get_upload_url",
            "mcp_smokeball_create_webhook_subscription",
            "mcp_smokeball_create_matter",
            "mcp_smokeball_delete_file",
            # trust-account writes (BANNED — never autonomous, never configurable)
            "mcp_smokeball_create_transaction",
            "mcp_smokeball_protect_funds",
            "mcp_smokeball_unprotect_funds",
        }
    ),
}


def _workspace_tools() -> frozenset[str]:
    plugin = load_plugin("hermes-smd-workspace")
    return frozenset(plugin.TOOLS)


# ---------------------------------------------------------------------------
# Layer A — every WIREABLE MCP connector must have a classified tool surface.
# ---------------------------------------------------------------------------


def test_every_registered_mcp_connector_has_a_classified_surface() -> None:
    """Each MCP_CONNECTOR_REGISTRY server must EITHER expose at least one
    classified tool under its mcp_<server>_ prefix OR be an explicit
    dormant-by-design carve-out.

    A server with zero classified tools and no carve-out is the EFF-07
    fail-open hole: bind it to a customer and every write verb runs
    autonomously. This forces the per-connector decision.
    """
    undecided: list[str] = []
    for server_name in MCP_CONNECTOR_REGISTRY:
        if server_name in UNCLASSIFIED_CONNECTORS_BY_DESIGN:
            continue
        prefix = _mcp_prefix(server_name)
        has_classified = any(name.startswith(prefix) for name in TOOL_ACTION_CLASS_MAP) or any(
            name.startswith(prefix) for name in BANNED_TOOLS
        )
        if not has_classified:
            undecided.append(server_name)
    assert undecided == [], (
        f"Registered MCP connector(s) with NO classified tool surface: {sorted(undecided)}. "
        f"Every wireable server must have its mcp_<server>_<tool> verbs in "
        f"TOOL_ACTION_CLASS_MAP/BANNED_TOOLS, or be declared dormant in "
        f"UNCLASSIFIED_CONNECTORS_BY_DESIGN with a rationale. Leaving a write-capable "
        f"connector unclassified ships an unmapped->READ->autonomous fail-open hole (EFF-07)."
    )


def test_dormant_connector_carveouts_are_real_registry_entries() -> None:
    """A dormant-by-design carve-out must name a server that is actually in
    the registry — a renamed/removed server must not leave a stale carve-out
    masking a future hole."""
    stale = {s for s in UNCLASSIFIED_CONNECTORS_BY_DESIGN if s not in MCP_CONNECTOR_REGISTRY}
    assert stale == set(), (
        f"stale UNCLASSIFIED_CONNECTORS_BY_DESIGN entries (no longer in "
        f"MCP_CONNECTOR_REGISTRY): {sorted(stale)}"
    )


def test_dormant_connector_carveouts_have_a_rationale() -> None:
    """Every carve-out must carry a non-empty written reason."""
    missing = {s for s, reason in UNCLASSIFIED_CONNECTORS_BY_DESIGN.items() if not reason.strip()}
    assert missing == set(), f"carve-outs missing a rationale: {sorted(missing)}"


def test_dormant_connector_is_actually_unclassified() -> None:
    """A connector declared dormant must genuinely have NO classified tools.

    If someone classifies a clio-oktopeak tool but leaves the dormant
    carve-out in place, the carve-out is now lying — drop it from
    UNCLASSIFIED_CONNECTORS_BY_DESIGN and let Layer A enforce the surface.
    """
    lying: list[str] = []
    for server_name in UNCLASSIFIED_CONNECTORS_BY_DESIGN:
        prefix = _mcp_prefix(server_name)
        classified = [n for n in TOOL_ACTION_CLASS_MAP if n.startswith(prefix)] + [
            n for n in BANNED_TOOLS if n.startswith(prefix)
        ]
        if classified:
            lying.append(f"{server_name}: {sorted(classified)}")
    assert lying == [], (
        f"Connector(s) marked dormant-by-design but with classified tools present: {lying}. "
        f"Remove them from UNCLASSIFIED_CONNECTORS_BY_DESIGN so Layer A enforces the full surface."
    )


# ---------------------------------------------------------------------------
# Layer B — pinned connector surfaces stay fully classified (drift defense).
# ---------------------------------------------------------------------------


def test_pinned_connector_surfaces_are_fully_classified() -> None:
    """Every tool in a pinned connector surface must be decided (mapped or
    banned). A new verb added to a pinned surface without classification
    fails here."""
    undecided: list[str] = []
    for surface in PINNED_CONNECTOR_SURFACES.values():
        for tool_name in surface:
            if not _is_decided(tool_name):
                undecided.append(tool_name)
    assert undecided == [], (
        f"Pinned connector tool(s) not classified (unmapped->READ fail-open): "
        f"{sorted(undecided)}. Add each to TOOL_ACTION_CLASS_MAP or BANNED_TOOLS."
    )


def test_pinned_surface_tools_match_their_prefix() -> None:
    """Guard the pin itself: every pinned tool name must carry its server's
    mcp_<server>_ prefix, so a typo can't make the surface assert nothing."""
    mismatched: list[str] = []
    for server_name, surface in PINNED_CONNECTOR_SURFACES.items():
        prefix = _mcp_prefix(server_name)
        mismatched += [t for t in surface if not t.startswith(prefix)]
    assert mismatched == [], f"pinned tools not matching their server prefix: {sorted(mismatched)}"


# ---------------------------------------------------------------------------
# Layer C — every build-side workspace tool must be classified.
# ---------------------------------------------------------------------------


def test_every_workspace_tool_is_classified() -> None:
    """Every tool the hermes-smd-workspace plugin registers in-process must be
    decided (mapped or banned). An unclassified workspace tool falls to the
    unmapped->READ default — the same fail-open shape. (Reinforces the inbound
    fence's action-class guard from the classification side.)"""
    undecided = sorted(t for t in _workspace_tools() if not _is_decided(t))
    assert undecided == [], (
        f"workspace tool(s) not classified (unmapped->READ fail-open): {undecided}. "
        f"Add each to TOOL_ACTION_CLASS_MAP or BANNED_TOOLS."
    )
