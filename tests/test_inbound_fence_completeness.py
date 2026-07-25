"""Completeness guard for the inbound fence (2026-06-12 code review).

``_FENCED_READ_TOOLS`` in the inbound plugin is a closed allowlist: a READ
tool whose result carries third-party / attacker-influenceable content gets
nonce-fenced and taints the session. The failure mode the review flagged is
silent drift — a new Workspace READ tool added to the registry without
anyone deciding whether its output is untrusted bypasses the entire inbound
quarantine, and nothing fails.

This test forces the decision: every registered Workspace tool classified
READ must appear EITHER in ``_FENCED_READ_TOOLS`` or in the explicit
``UNFENCED_READ_BY_DESIGN`` set below (with its rationale). Adding a READ
tool without updating one of the two sets fails CI.

The same guard runs over the agent's OWN AgentMail inbox (``mcp_agentmail_*``
READ tools, the PULL path — SEC-05/13 residual). The original guard filtered
on ``startswith("workspace_")``, so a raw ``mcp_agentmail_*`` read — the live
runtime form the agent actually emits — would have been NEITHER fenced NOR
flagged, leaving injected mail in the agent's own inbox an untainted ingest
channel. The AgentMail surface is enumerated in ``shared.action_classes``
(not a separate tool registry), so it is read from ``TOOL_ACTION_CLASS_MAP``.
"""

from shared.action_classes import TOOL_ACTION_CLASS_MAP, ActionClass
from tests.conftest import load_plugin

# READ tools whose output is treated as internal / not third-party-authored.
# Membership here is a deliberate security decision, not a default — see the
# membership rule in plugins/hermes-smd-inbound/__init__.py. If you are
# adding a tool here, write down why its result cannot carry
# attacker-influenceable content.
UNFENCED_READ_BY_DESIGN: frozenset[str] = frozenset(
    {
        # Drive listing: filename/id metadata, no document body content
        # (body reads go through workspace_drive_get/export, which are
        # fenced).
        # (workspace_calendar_list/get were fence CANDIDATES here until
        # 2026-06-12; Captain ruled fence-both — external invites carry
        # third-party content — and they moved to _FENCED_READ_TOOLS.)
        "workspace_drive_list",
        # Gmail search/list: returns only message {id, threadId} metadata
        # (messages.list contract — no snippet, subject, or body), so it carries
        # no attacker-influenceable content. The message BODY read
        # (workspace_gmail_get) IS fenced. Unfenced so the agent can reuse the
        # returned ids as the message_id for the body read — fencing the id list
        # breaks the inherent list->get read pattern for zero security gain (same
        # rationale as workspace_drive_list / drive_get).
        "workspace_gmail_search",
        # AgentMail (mcp_agentmail_*) READ tools whose output is agent-owned
        # config/identity/metadata, NOT sender-authored content. The
        # content-bearing reads (threads/messages/attachments/draft body) are
        # fenced in _FENCED_READ_TOOLS.
        # - list_inboxes / get_inbox: the persona's own inbox config + address
        #   (the agent owns these; no third-party message bodies).
        # - list_drafts: id/subject metadata of the agent's OWN drafts (the
        #   draft BODY read, get_draft, IS fenced — a reply draft can quote
        #   inbound text).
        # - auth_me: the agent's own AgentMail auth identity.
        "mcp_agentmail_list_inboxes",
        "mcp_agentmail_get_inbox",
        "mcp_agentmail_list_drafts",
        "mcp_agentmail_auth_me",
        # Durable-job status (ss #1916): broker-authored metadata only. The
        # projection in plugins/hermes-smd-jobs/_job_status deliberately
        # excludes the row's free-text error column (replaced with a boolean)
        # because runtime exception prose can echo content the job read — with
        # that column out, no field carries attacker-influenceable content
        # (ids, closed-set status, cents, attempts, opaque result_ref).
        "job_status",
        # Escalation-ledger state (ss #1915): folds the broker-owned ledger
        # twin — every field is broker-validated telemetry (event kinds are a
        # closed set, ts/id are broker-stamped, tokens are derived hashes).
        # item_key/matter_id originate from Smokeball identifiers, not
        # sender-authored prose.
        "escalation_state",
    }
)


def _workspace_tools() -> dict:
    plugin = load_plugin("hermes-smd-workspace")
    return plugin.TOOLS


def _fenced_read_tools() -> frozenset[str]:
    plugin = load_plugin("hermes-smd-inbound")
    return plugin._FENCED_READ_TOOLS


def test_every_workspace_read_tool_is_fenced_or_explicitly_unfenced() -> None:
    fenced = _fenced_read_tools()
    undecided = []
    for name in _workspace_tools():
        if TOOL_ACTION_CLASS_MAP.get(name) is not ActionClass.READ:
            continue
        if name in fenced or name in UNFENCED_READ_BY_DESIGN:
            continue
        undecided.append(name)
    assert undecided == [], (
        f"Workspace READ tool(s) with no fencing decision: {sorted(undecided)}. "
        "Add to _FENCED_READ_TOOLS (untrusted content) or "
        "UNFENCED_READ_BY_DESIGN (with rationale) — never neither."
    )


def _agentmail_read_tools() -> list[str]:
    """The agent's own AgentMail inbox READ tools (live ``mcp_agentmail_*``
    runtime form). The colon-form aliases (``agentmail:*``) never occur at
    runtime, so they are excluded — only the form the agent actually emits is
    a real ingest channel that must carry a fencing decision."""
    return [
        name
        for name, cls in TOOL_ACTION_CLASS_MAP.items()
        if name.startswith("mcp_agentmail_") and cls is ActionClass.READ
    ]


def _msgraph_read_tools() -> list[str]:
    """msgraph-mail connector READ tools (live ``mcp_msgraph_mail_*`` runtime
    form) — the client-custody operator mailbox (ss #1978 / ADR 0078). Same
    rationale as the AgentMail clause: without this prefix in the guard, a
    future msgraph read tool could slip both the fence and CI."""
    return [
        name
        for name, cls in TOOL_ACTION_CLASS_MAP.items()
        if name.startswith("mcp_msgraph_mail_") and cls is ActionClass.READ
    ]


def test_every_msgraph_read_tool_is_fenced_or_explicitly_unfenced() -> None:
    """Client-custody mailbox reads are the same primary untrusted channel as
    AgentMail's PULL path — every msgraph READ tool must carry a fencing
    decision. (All three initial tools are fenced: even list_messages returns
    sender-authored subject + bodyPreview.)"""
    fenced = _fenced_read_tools()
    undecided = [
        n for n in _msgraph_read_tools() if n not in fenced and n not in UNFENCED_READ_BY_DESIGN
    ]
    assert undecided == [], (
        f"msgraph READ tools with no fencing decision: {undecided} — add to "
        "_FENCED_READ_TOOLS (sender content) or UNFENCED_READ_BY_DESIGN (with rationale)"
    )


def test_every_agentmail_read_tool_is_fenced_or_explicitly_unfenced() -> None:
    """The PULL-path guard (SEC-05/13 residual): every AgentMail READ tool the
    agent can call must be fenced (sender content) or explicitly unfenced
    (agent-owned config/metadata). A future unfenced agent-inbox read tool
    fails CI here — closing the gap where the old workspace_-only filter let
    raw ``mcp_agentmail_*`` reads slip both the fence and this guard."""
    fenced = _fenced_read_tools()
    reads = _agentmail_read_tools()
    # Guard against the live surface vanishing (a rename in action_classes
    # would silently empty this test). The live server exposes 11 READ tools.
    assert reads, (
        "no mcp_agentmail_* READ tools found in TOOL_ACTION_CLASS_MAP — the "
        "AgentMail surface may have been renamed; this guard would pass vacuously."
    )
    undecided = [
        name for name in reads if name not in fenced and name not in UNFENCED_READ_BY_DESIGN
    ]
    assert undecided == [], (
        f"AgentMail READ tool(s) with no fencing decision: {sorted(undecided)}. "
        "Add to _FENCED_READ_TOOLS (sender-authored content) or "
        "UNFENCED_READ_BY_DESIGN (agent-owned, with rationale) — never neither. "
        "An unfenced agent-inbox read is an untainted injection channel."
    )


def test_unfenced_by_design_does_not_overlap_fenced() -> None:
    overlap = UNFENCED_READ_BY_DESIGN & _fenced_read_tools()
    assert overlap == frozenset(), f"tools in both sets: {sorted(overlap)}"


def test_unfenced_by_design_has_no_stale_entries() -> None:
    """Every by-design entry must still be a registered tool — a renamed/removed
    tool must not leave a stale carve-out behind. Workspace/jobs/escalation
    entries are checked against their plugin registries; AgentMail
    (``mcp_agentmail_*``) entries against the action-class map (their registry)."""
    registered = set(_workspace_tools())
    for plugin_dir in ("hermes-smd-jobs", "hermes-smd-escalation"):
        registered |= set(load_plugin(plugin_dir).TOOLS)
    classified = set(TOOL_ACTION_CLASS_MAP)
    stale = {
        t
        for t in UNFENCED_READ_BY_DESIGN
        if (t.startswith("mcp_agentmail_") and t not in classified)
        or (not t.startswith("mcp_agentmail_") and t not in registered)
    }
    assert stale == set(), f"stale UNFENCED_READ_BY_DESIGN entries: {sorted(stale)}"


def test_fenced_workspace_entries_are_registered_tools() -> None:
    """Every workspace_* name in the fence list must exist in the registry —
    catches typos that would silently fence nothing."""
    registered = set(_workspace_tools())
    ghosts = {t for t in _fenced_read_tools() if t.startswith("workspace_") and t not in registered}
    assert ghosts == set(), f"fenced workspace tools not in registry: {sorted(ghosts)}"


def test_fenced_agentmail_entries_are_classified_reads() -> None:
    """Every mcp_agentmail_* name in the fence list must be a READ tool in the
    action-class map — catches a typo that would silently fence nothing, or a
    write/draft tool wrongly placed in the read fence (those are governed by the
    trust ceiling, not the taint fence)."""
    reads = set(_agentmail_read_tools())
    mis = {t for t in _fenced_read_tools() if t.startswith("mcp_agentmail_") and t not in reads}
    assert mis == set(), (
        f"fenced mcp_agentmail_* tools that are not classified READS: {sorted(mis)}"
    )


def test_every_workspace_tool_has_an_action_class() -> None:
    """A registered workspace tool absent from TOOL_ACTION_CLASS_MAP would
    fail closed to REFUSED (issue #1327) and be unreachable — every registered
    tool must be deliberately classified to function."""
    unmapped = [t for t in _workspace_tools() if t not in TOOL_ACTION_CLASS_MAP]
    assert unmapped == [], f"workspace tools missing from TOOL_ACTION_CLASS_MAP: {sorted(unmapped)}"


def _plugin_tool_names(plugin_dir: str) -> list[str]:
    plugin = load_plugin(plugin_dir)
    return list(plugin.TOOLS)


def test_every_jobs_and_escalation_tool_is_mapped_and_reads_are_decided() -> None:
    """The #1916 drift class, closed as a guard: a plugin tool absent from
    TOOL_ACTION_CLASS_MAP fails closed to REFUSED and is dead at runtime
    (exactly how the durable-job tools shipped inert). Every registered
    jobs/escalation tool must be classified, and every READ among them must
    carry an explicit fencing decision."""
    fenced = _fenced_read_tools()
    for plugin_dir in ("hermes-smd-jobs", "hermes-smd-escalation"):
        names = _plugin_tool_names(plugin_dir)
        unmapped = [t for t in names if t not in TOOL_ACTION_CLASS_MAP]
        assert unmapped == [], (
            f"{plugin_dir} tools missing from TOOL_ACTION_CLASS_MAP: {sorted(unmapped)}"
        )
        undecided = [
            t
            for t in names
            if TOOL_ACTION_CLASS_MAP.get(t) is ActionClass.READ
            and t not in fenced
            and t not in UNFENCED_READ_BY_DESIGN
        ]
        assert undecided == [], (
            f"{plugin_dir} READ tool(s) with no fencing decision: {sorted(undecided)}"
        )
