"""The per-boot proof that the runaway-loop arms are FED (overlay#319/#320).

WHY THIS PROBE EXISTS AT ALL. `cost_breaker.run_boot_probe` proves the ladder
HALTS, by driving the state machine directly. That is a real proof of a real
property, and it is blind to the one that actually failed: for months
`record_tool_failure` and `record_refusal` were implemented, thresholded,
audited and unit-tested with NO CALLER anywhere in either repo. Everything was
green. A seat looping on a failing tool stopped only on spend.

The distinction is "the brake exists" versus "the brake is connected", and only
the second one is worth a boot gate. So `run_loop_arm_boot_probe` drives the
REAL `post_tool_call` handler with the REAL envelope shape rather than calling
the state machine itself — a probe that re-implemented the handler would have
passed happily throughout the entire period the bug existed.

WHAT THESE TESTS PIN. Mostly that the probe can come back RED, and for each of
the three distinct ways the control can be broken. A boot gate that cannot fail
is worse than no boot gate: it converts an unchecked property into a checked-
looking one.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from pathlib import Path

import pytest


def load_plugin(plugin_name: str):
    """Load the plugin package so its relative imports resolve.

    Same local loader as `tests/test_audit_emit.py`, for the reason recorded
    there: the shared `conftest.load_plugin` executes the module before
    registering it in `sys.modules`, so `from . import emit` raises.
    """
    root = Path(__file__).parent.parent
    init_path = root / "plugins" / plugin_name / "__init__.py"
    mod_name = f"plugin_{plugin_name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, init_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin spec for {plugin_name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def audit_plugin():
    return load_plugin("hermes-smd-audit")


def test_probe_passes_against_the_real_handler(audit_plugin) -> None:
    ok, reason = asyncio.run(audit_plugin.run_loop_arm_boot_probe())
    assert ok, reason
    assert "error trips" in reason


def test_probe_runs_the_sync_handler_from_inside_a_running_loop(audit_plugin) -> None:
    """The failure mode this guards is specific and would only appear in prod.

    The activation handler owns the gateway's event loop, and `CostBreaker`
    bridges to async via `asyncio.run`, which RAISES inside a running loop. If
    the probe ever stopped threading the handler, it would pass in a test that
    called it synchronously and `_die` on every real boot — a crash-loop caused
    by the safety check rather than by the thing it checks.
    """

    async def driver():
        return await audit_plugin.run_loop_arm_boot_probe()

    ok, reason = asyncio.run(driver())
    assert ok, reason


# --------------------------------------------------------------------------- #
# The probe must come back RED. Three ways, one per assertion it makes.        #
# --------------------------------------------------------------------------- #


def test_probe_fails_when_the_hook_stops_feeding_the_failure_arm(audit_plugin, monkeypatch) -> None:
    """The original bug, reintroduced: the handler no longer calls the arm."""
    monkeypatch.setattr(audit_plugin, "_meter_loop_arms", lambda *a, **k: None)
    ok, reason = asyncio.run(audit_plugin.run_loop_arm_boot_probe())
    assert not ok
    assert "did not trip the tool-failure arm" in reason


def test_probe_fails_when_success_stops_resetting_the_streak(audit_plugin, monkeypatch) -> None:
    """The regression a careless simplification introduces: failures are fed,
    successes are not, and every long-lived seat marches to HARD_STOP."""
    real = audit_plugin._meter_loop_arms

    def failures_only(kwargs, breaker=None):
        if kwargs.get("status") == "ok":
            return
        return real(kwargs, breaker)

    monkeypatch.setattr(audit_plugin, "_meter_loop_arms", failures_only)
    ok, reason = asyncio.run(audit_plugin.run_loop_arm_boot_probe())
    assert not ok
    assert "did not reset the failure streak" in reason


def test_probe_fails_when_detection_stops_being_positive_only(audit_plugin, monkeypatch) -> None:
    """The failure that would let an upstream envelope rename stop a live seat.

    Invisible to both assertions above, which is why the probe makes a third.
    """
    real = audit_plugin._meter_loop_arms

    def treats_unknown_as_failure(kwargs, breaker=None):
        k = dict(kwargs)
        if k.get("status") not in ("ok", "error", "blocked"):
            k["status"] = "error"
        return real(k, breaker)

    monkeypatch.setattr(audit_plugin, "_meter_loop_arms", treats_unknown_as_failure)
    ok, reason = asyncio.run(audit_plugin.run_loop_arm_boot_probe())
    assert not ok
    assert "positive-only" in reason


def test_probe_never_raises_out_of_a_broken_breaker(audit_plugin, monkeypatch) -> None:
    """A probe fault must be reported as a red probe, not an exception. The
    activation handler awaits this inside the gateway startup coroutine, where a
    raise is swallowed by HookRegistry — which would turn a failed safety check
    into a silent pass."""

    def boom(*_a, **_k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(audit_plugin, "_meter_loop_arms", boom)
    ok, reason = asyncio.run(audit_plugin.run_loop_arm_boot_probe())
    assert not ok


def test_probe_leaves_no_state_file_behind(audit_plugin, tmp_path, monkeypatch) -> None:
    """It must never touch /opt/data/smd/sticky_stop.db, and must not litter."""
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("smd-looparm-probe-*"))
    asyncio.run(audit_plugin.run_loop_arm_boot_probe())
    after = set(Path(tempfile.gettempdir()).glob("smd-looparm-probe-*"))
    assert after == before


# --------------------------------------------------------------------------- #
# The activation handler must actually call it.                               #
# --------------------------------------------------------------------------- #


def test_activation_handler_wires_the_check() -> None:
    """Built is not wired — the same distinction the probe itself is about.

    A perfect probe nobody calls at boot is the exact shape of the defect being
    fixed, so the wiring is asserted rather than assumed.
    """
    handler = (
        Path(__file__).parent.parent / "hooks" / "smd-overlay-activation" / "handler.py"
    ).read_text()
    assert "async def _loop_arm_self_check" in handler
    assert re.search(r"^\s+await _loop_arm_self_check\(", handler, re.M)
    # And it must sit with the other fail-closed boot gates, not in a branch.
    assert "RUNAWAY-LOOP BRAKE INERT" in handler


def test_the_check_resolves_the_probe_from_the_live_manager_not_a_path() -> None:
    """The regression that crash-looped hermes-smd-staging for 29 minutes.

    The first version computed ``parents[2]/plugins/hermes-smd-audit/__init__.py``
    — the REPO layout. On a seat the handler installs to
    ``/opt/data/profiles/crane/hooks/``, so the path did not exist, the check
    reported failure, and the gateway exited 1 on every boot (ss-console#2590).

    A path assertion cannot be tested from a checkout, because the checkout is
    the one place the path resolves. So the assertion here is structural: the
    check must read the live manager's registered callbacks and must NOT build a
    filesystem path to the plugin at all.
    """
    handler = (
        Path(__file__).parent.parent / "hooks" / "smd-overlay-activation" / "handler.py"
    ).read_text()
    body = handler[
        handler.index("async def _loop_arm_self_check") : handler.index("def _gateway_config")
    ]
    # Scan CODE only. The docstring quotes the bad path on purpose, to record
    # what went wrong — and the first draft of this test flagged its own
    # explanation. A forbidden-string check that cannot tell prose from code
    # fires on the documentation of the fix.
    code = body[body.index('"""', body.index('"""') + 3) + 3 :]
    assert "_hooks" in code and "post_tool_call" in code, (
        "the check must resolve the probe from the live PluginManager's registered "
        "post_tool_call callbacks"
    )
    for forbidden in ("__file__", "parents[", "Path("):
        assert forbidden not in code, (
            f"the check builds a filesystem path ({forbidden!r}). That is the exact "
            "defect that crash-looped a seat: repo layout is not seat layout, and no "
            "test run from a checkout can catch it."
        )


def test_a_missing_probe_does_not_kill_the_seat() -> None:
    """Version skew must not be fatal.

    An overlay predating #320 has no ``run_loop_arm_boot_probe``. Dying for that
    would mean no older overlay could ever boot — a rollback that cannot roll
    back is a worse safety property than the gap it closes. The hook surface is
    already asserted separately, so this branch means "skew", never "ungoverned".
    """
    handler = (
        Path(__file__).parent.parent / "hooks" / "smd-overlay-activation" / "handler.py"
    ).read_text()
    body = handler[
        handler.index("async def _loop_arm_self_check") : handler.index("def _gateway_config")
    ]
    probe_missing = body[body.index("if probe is None:") : body.index("try:")]
    assert "_die(" not in probe_missing, (
        "a probe that cannot be FOUND must not _die — conflating 'cannot evaluate' "
        "with 'evaluated and failed' is what took the seat down"
    )
    assert "UNPROVEN" in probe_missing and "logger.critical" in probe_missing, (
        "it must still be loud: silent skew is how an unproven brake reads as a proven one"
    )


def test_a_raising_probe_does_not_kill_the_seat() -> None:
    """A fault in the checker is not evidence the control is broken. The checker
    must not be able to take a seat down by being wrong about itself — which is
    precisely what happened."""
    handler = (
        Path(__file__).parent.parent / "hooks" / "smd-overlay-activation" / "handler.py"
    ).read_text()
    body = handler[
        handler.index("async def _loop_arm_self_check") : handler.index("def _gateway_config")
    ]
    except_block = body[body.index("except Exception") : body.index("if not ok:")]
    assert "_die(" not in except_block
    assert "logger.critical" in except_block


def test_a_probe_that_ran_and_failed_still_kills_the_seat() -> None:
    """The whole point survives: a brake PROVEN broken must stop the seat."""
    handler = (
        Path(__file__).parent.parent / "hooks" / "smd-overlay-activation" / "handler.py"
    ).read_text()
    body = handler[
        handler.index("async def _loop_arm_self_check") : handler.index("def _gateway_config")
    ]
    tail = body[body.index("if not ok:") :]
    assert "_die(" in tail and "BRAKE INERT" in tail
