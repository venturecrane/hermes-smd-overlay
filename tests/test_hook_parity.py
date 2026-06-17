"""Hook-parity guard — spec↔code drift fails CI (SEC-33).

WHY THIS EXISTS
---------------
The 2026-06-15 security audit (SEC-33) flagged a "refusal cascade" hook that
was retired in ss-console commit ``d2bfe213`` (the 2026-05-24 realignment that
deleted the fork-side ``aie_adapter.py`` / ``hermes_hook.py`` register surface,
per ADRs 0015/0016/0017) while ~3 ss-console specs still described it as live.
The removal was a Captain-signed decision, not a silent drop — but the failure
*class* it exposed is real and recurs cheaply: a hook can drift out of code
while a contract still claims it, or get wired in code while the contract never
acknowledges it. Either direction is a governance hazard for an overlay whose
whole job is to *govern* a live agent.

This test makes that drift class fail CI. It enforces three parity invariants
against the live fan-out (the exact module Hermes loads), so a contracted hook
cannot silently disappear and a wired hook cannot silently bypass the contract:

  1. **Manifest ↔ register parity (per plugin).** Every hook a plugin's
     ``register(ctx)`` actually wires MUST be declared in that plugin's
     ``plugin.yaml`` ``hooks:`` list, and every declared hook MUST be wired.
     A hook that is declared-but-never-wired is the silent-drop shape; a hook
     that is wired-but-undeclared hides a real attachment from the manifest.

  2. **Contract ↔ registered parity (the SEC-33 guard).** ``docs/hook-surface.md``
     is the authoritative hook contract (AGENTS.md hard rule #2). Every hook its
     appendix marks "Used by overlay? yes" MUST be registered by at least one
     plugin in the live fan-out, and every hook some plugin registers MUST be
     marked "yes" in that table. A contracted-but-unregistered hook fails here —
     that is exactly the SEC-33 silent-drop that would otherwise only surface in
     a later audit.

  3. **Every registered hook is a valid Hermes hook.** No plugin may attach to a
     hook name Hermes does not expose at the pinned ref (the appendix is the
     enumerated ``VALID_HOOKS`` set). A fork-side invention like the retired
     ``refusal`` hook would fail here.

The test loads plugins through the umbrella fan-out (``__init__.py``), i.e. the
real gateway load path, so "registered" means "would actually attach on a live
Machine", not "a function named register exists".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# Reuse the umbrella's canonical discovery + fan-out so this test exercises the
# same load path the gateway uses. test_overlay_fanout.py loads the umbrella by
# path under the hermes_plugins namespace; we reuse its helpers to avoid a second
# divergent loader.
from tests.test_overlay_fanout import RecordingCtx, _load_umbrella

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SURFACE = _REPO / "docs" / "hook-surface.md"


# --------------------------------------------------------------------------- #
# Helpers — parse the manifests and the contract doc.
# --------------------------------------------------------------------------- #

def _declared_subplugin_dirs() -> list[Path]:
    """Sub-plugin directories from the umbrella manifest (single source of
    truth). Mirrors umbrella.declared_subplugins() but returns absolute dirs."""
    mod = _load_umbrella()
    return [_REPO / rel for rel in mod.declared_subplugins()]


def _manifest_hooks(plugin_dir: Path) -> set[str]:
    """The ``hooks:`` list declared in a sub-plugin's ``plugin.yaml`` (empty set
    if the key is absent — tool-only plugins like workspace declare no hooks)."""
    manifest = yaml.safe_load((plugin_dir / "plugin.yaml").read_text(encoding="utf-8"))
    hooks = manifest.get("hooks", []) if isinstance(manifest, dict) else []
    return {str(h) for h in (hooks or [])}


def _registered_hooks_per_plugin() -> dict[str, set[str]]:
    """Fan out through the umbrella and record which hooks each sub-plugin
    actually wires onto a fresh ctx. One ctx per plugin so attribution is
    exact (the shared fan-out ctx would conflate plugins)."""
    mod = _load_umbrella()
    per_plugin: dict[str, set[str]] = {}
    for plugin_dir in _declared_subplugin_dirs():
        ctx = RecordingCtx()
        # load_and_register a single plugin by pointing the loader at its dir.
        module = mod._load_subplugin_module(plugin_dir)
        register_fn = getattr(module, "register", None)
        if register_fn is None:
            per_plugin[plugin_dir.name] = set()
            continue
        register_fn(ctx)
        per_plugin[plugin_dir.name] = set(ctx.hooks.keys())
    return per_plugin


def _contract_table() -> dict[str, bool]:
    """Parse the appendix ``VALID_HOOKS`` table in docs/hook-surface.md into
    ``{hook_name: used_by_overlay}``. The table rows look like:

        | `pre_tool_call` | yes | trust ceiling |
        | `transform_tool_result` | no | ... |
    """
    text = _HOOK_SURFACE.read_text(encoding="utf-8")
    row = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*(yes|no)\s*\|", re.MULTILINE)
    table: dict[str, bool] = {}
    for name, used in row.findall(text):
        table[name] = used == "yes"
    return table


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """The audit/peer-memory/etc. sub-plugins resolve per-customer namespace +
    audit binding from env at register time. Set them so register() runs the
    full path, not its degraded no-op branch — otherwise a plugin's hooks
    wouldn't attach and the parity check would be vacuously satisfied."""
    import os

    tmp = tmp_path_factory.mktemp("hookparity")
    prev = {}
    for k, v in {
        "SMD_CUSTOMER_SLUG": "acme",
        "SMD_D1_AUDIT_BINDING": str(tmp / "audit.db"),
    }.items():
        prev[k] = os.environ.get(k)
        os.environ[k] = v
    yield tmp
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# --------------------------------------------------------------------------- #
# Invariant 1 — manifest ↔ register parity, per plugin.
# --------------------------------------------------------------------------- #

def test_manifest_declares_exactly_the_hooks_register_wires(env):
    """Every hook a plugin wires must be declared in its manifest, and every
    declared hook must be wired. Catches both the silent-drop shape
    (declared-but-not-wired) and the hidden-attachment shape
    (wired-but-not-declared)."""
    registered = _registered_hooks_per_plugin()
    mismatches: dict[str, dict[str, set[str]]] = {}
    for plugin_dir in _declared_subplugin_dirs():
        name = plugin_dir.name
        declared = _manifest_hooks(plugin_dir)
        wired = registered.get(name, set())
        if declared != wired:
            mismatches[name] = {
                "declared_but_not_wired": declared - wired,
                "wired_but_not_declared": wired - declared,
            }
    assert not mismatches, (
        "plugin.yaml hooks: list is out of sync with register(ctx) wiring:\n"
        + "\n".join(
            f"  {name}: declared_but_not_wired={sorted(d['declared_but_not_wired'])} "
            f"wired_but_not_declared={sorted(d['wired_but_not_declared'])}"
            for name, d in sorted(mismatches.items())
        )
    )


# --------------------------------------------------------------------------- #
# Invariant 2 — contract ↔ registered parity (the SEC-33 guard).
# --------------------------------------------------------------------------- #

def test_every_contracted_hook_is_registered(env):
    """Every hook docs/hook-surface.md marks "Used by overlay? yes" must be
    registered by some plugin in the live fan-out. This is the SEC-33 guard:
    a contracted hook that silently vanishes from code fails here instead of
    only surfacing in a later security audit."""
    contract = _contract_table()
    contracted_yes = {h for h, used in contract.items() if used}
    registered = _registered_hooks_per_plugin()
    all_registered: set[str] = set().union(*registered.values()) if registered else set()
    missing = contracted_yes - all_registered
    assert not missing, (
        "docs/hook-surface.md marks these hooks 'Used by overlay? yes' but NO "
        f"plugin registers them on the live fan-out: {sorted(missing)}. Either a "
        "contracted hook was dropped from code (re-home it) or the contract is "
        "stale (correct the appendix table)."
    )


def test_every_registered_hook_is_contracted_yes(env):
    """Every hook some plugin actually registers must be marked "yes" in the
    contract table. Catches a wired hook the contract never acknowledged (the
    inverse drift) — the manifest/code grew an attachment the governing doc
    doesn't record."""
    contract = _contract_table()
    registered = _registered_hooks_per_plugin()
    all_registered: set[str] = set().union(*registered.values()) if registered else set()
    not_contracted = {
        h for h in all_registered if not contract.get(h, False)
    }
    assert not not_contracted, (
        "these hooks are registered by a plugin but docs/hook-surface.md does "
        f"NOT mark them 'Used by overlay? yes': {sorted(not_contracted)}. Update "
        "the appendix table so the contract records the attachment."
    )


# --------------------------------------------------------------------------- #
# Invariant 3 — every registered hook is a valid Hermes hook.
# --------------------------------------------------------------------------- #

def test_every_registered_hook_is_a_valid_hermes_hook(env):
    """No plugin may attach to a hook name Hermes does not expose at the pinned
    ref. The appendix table enumerates the full VALID_HOOKS set; a fork-side
    invention (e.g. the retired ``refusal`` hook) would not appear there and
    would fail here."""
    valid = set(_contract_table().keys())
    registered = _registered_hooks_per_plugin()
    all_registered: set[str] = set().union(*registered.values()) if registered else set()
    invalid = all_registered - valid
    assert not invalid, (
        f"plugins register hook names not in the VALID_HOOKS appendix: {sorted(invalid)}. "
        "Either the name is wrong or docs/hook-surface.md needs the new Hermes hook added."
    )


def test_refusal_cascade_hook_is_absent(env):
    """SEC-33 regression pin. The retired fork-side ``refusal`` hook (which
    delegated to ss-console's RefusalHandler cascade) must NOT reappear under
    any name on the overlay's registered surface. If a future change re-homes
    cascade escalation, it rides an existing upstream hook (post_tool_call —
    the audit observation seam), never a bespoke ``refusal``/``cascade`` hook."""
    registered = _registered_hooks_per_plugin()
    all_registered: set[str] = set().union(*registered.values()) if registered else set()
    offenders = {h for h in all_registered if "refusal" in h or "cascade" in h}
    assert not offenders, (
        "a 'refusal'/'cascade' hook reappeared on the registered surface "
        f"({sorted(offenders)}). The fork-side refusal hook was retired in "
        "d2bfe213 (ADRs 0015/0016/0017); cascade escalation, if re-homed, rides "
        "post_tool_call, not a bespoke hook."
    )
