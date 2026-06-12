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


def test_unfenced_by_design_does_not_overlap_fenced() -> None:
    overlap = UNFENCED_READ_BY_DESIGN & _fenced_read_tools()
    assert overlap == frozenset(), f"tools in both sets: {sorted(overlap)}"


def test_unfenced_by_design_has_no_stale_entries() -> None:
    """Every by-design entry must still be a registered workspace tool —
    a renamed/removed tool must not leave a stale carve-out behind."""
    registered = set(_workspace_tools())
    stale = {t for t in UNFENCED_READ_BY_DESIGN if t not in registered}
    assert stale == set(), f"stale UNFENCED_READ_BY_DESIGN entries: {sorted(stale)}"


def test_fenced_workspace_entries_are_registered_tools() -> None:
    """Every workspace_* name in the fence list must exist in the registry —
    catches typos that would silently fence nothing."""
    registered = set(_workspace_tools())
    ghosts = {t for t in _fenced_read_tools() if t.startswith("workspace_") and t not in registered}
    assert ghosts == set(), f"fenced workspace tools not in registry: {sorted(ghosts)}"


def test_every_workspace_tool_has_an_action_class() -> None:
    """A registered workspace tool absent from TOOL_ACTION_CLASS_MAP would
    fall to the unmapped READ default — every registered tool must be
    deliberately classified."""
    unmapped = [t for t in _workspace_tools() if t not in TOOL_ACTION_CLASS_MAP]
    assert unmapped == [], f"workspace tools missing from TOOL_ACTION_CLASS_MAP: {sorted(unmapped)}"
