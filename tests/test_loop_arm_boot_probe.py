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


def _handler_src() -> str:
    return (
        Path(__file__).parent.parent / "hooks" / "smd-overlay-activation" / "handler.py"
    ).read_text()


def _gate_body() -> str:
    h = _handler_src()
    return h[h.index("async def _loop_arm_self_check") : h.index("def _gateway_config")]


def _gate_code() -> str:
    """The gate's executable body, docstring stripped.

    The docstring quotes the bad path deliberately, to record what went wrong.
    An earlier draft of this file flagged its own explanation — a
    forbidden-string check that cannot tell prose from code fires on the
    documentation of the fix.
    """
    b = _gate_body()
    return b[b.index('"""', b.index('"""') + 3) + 3 :]


def test_activation_handler_wires_the_check() -> None:
    """Built is not wired — the same distinction the probe itself is about."""
    handler = _handler_src()
    assert "async def _loop_arm_self_check" in handler
    assert re.search(r"^\s+\w+ = await _loop_arm_self_check\(", handler, re.M), (
        "the gate must be called AND its outcome captured, or the completion "
        "line cannot report what actually happened"
    )


def test_the_check_resolves_the_probe_from_the_live_manager_not_a_path() -> None:
    """The regression that crash-looped hermes-smd-staging for 29 minutes.

    The first version computed ``parents[2]/plugins/hermes-smd-audit/__init__.py``
    — the REPO layout. On a seat the handler installs to
    ``/opt/data/profiles/crane/hooks/``, so the path did not exist and the
    gateway exited 1 on every boot (ss-console#2590).

    A path assertion cannot be tested from a checkout, because the checkout is
    the one place the path resolves. So this is structural.
    """
    code = _gate_code()
    assert "_hooks" in code and "post_tool_call" in code
    for forbidden in ("__file__", "parents[", "Path("):
        assert forbidden not in code, (
            f"the check builds a filesystem path ({forbidden!r}). Repo layout is "
            "not seat layout, and no test run from a checkout can catch it."
        )


def test_the_loop_arm_gate_is_WARN_TIER_and_cannot_kill_a_seat() -> None:
    """The keystone of the retier, asserted directly.

    Per `shared/webhook_read_surface.py`: "A crash-loop is the right answer only
    when serving is worse than being down." Un-brake-proven serving costs a
    client some hours with the cost breaker still backstopping; a crash-loop
    costs the firm its paralegal mid-engagement. So this gate reports and the
    seat runs — and no future edit may quietly make it fatal again.
    """
    code = _gate_code()
    assert "fatal=False" in code, "the loop-arm gate must be WARN tier"
    assert "_die(" not in code, (
        "the loop-arm gate must have NO path to _die. Its own fatal version took "
        "a seat down for 29 minutes on a lookup bug."
    )


def test_the_cost_gate_stays_FATAL() -> None:
    """The retier is scoped, not a blanket softening. An operator whose SPEND
    breaker cannot fire must still refuse to serve."""
    h = _handler_src()
    body = h[
        h.index("async def _cost_breaker_self_check") : h.index("async def _loop_arm_self_check")
    ]
    assert "fatal=True" in body


def test_run_gate_never_dies_on_a_probe_it_could_not_evaluate() -> None:
    """SKIPPED is not UNPROVEN. Conflating them is what killed the seat: a
    lookup miss was treated as a proven-broken brake."""
    h = _handler_src()
    body = h[h.index("async def _run_gate") : h.index("async def _cost_breaker_self_check")]
    timeout_arm = body[body.index("except TimeoutError:") : body.index("except Exception")]
    raise_arm = body[body.index("except Exception") : body.index("if ok:")]
    for name, arm in (("timeout", timeout_arm), ("raise", raise_arm)):
        assert "_die(" not in arm, f"the {name} path must not be fatal"
        assert "GATE_SKIPPED" in arm, f"the {name} path must report SKIPPED, not UNPROVEN"


def test_run_gate_bounds_every_probe() -> None:
    """A hang is worse than a crash-loop: a crash-loop shows in the restart
    count, a hang reads as a slow boot."""
    h = _handler_src()
    body = h[h.index("async def _run_gate") : h.index("async def _cost_breaker_self_check")]
    assert "wait_for" in body and "_GATE_TIMEOUT_S" in body


def test_handle_has_a_top_level_guard() -> None:
    """A raise anywhere in handle() is swallowed by HookRegistry (handler.py:56),
    which would silently skip every downstream gate and serve ungoverned with no
    _die. One unguarded line disarms the checker."""
    h = _handler_src()
    body = h[h.index("async def handle(") :]
    assert "except BaseException" in body, (
        "handle() needs a top-level guard so a checker fault cannot silently "
        "disarm the remaining gates"
    )


# --------------------------------------------------------------------------- #
# THE MUTATION TEST. This is the one that makes the probe mean anything.       #
# --------------------------------------------------------------------------- #


def test_probe_goes_red_when_the_hook_stops_calling_the_arms(audit_plugin, monkeypatch) -> None:
    """Delete the WIRING and the probe must fail.

    This is the defect the whole feature is about, reproduced exactly:
    ``record_tool_failure`` sat implemented, thresholded, audited and
    unit-tested for months with nothing calling it. The call that was missing is
    ``on_post_tool_call``'s call to ``_meter_loop_arms`` — the registered
    callback's call, not the inner function.

    The first version of this probe invoked ``_meter_loop_arms`` DIRECTLY, so
    removing that call left the probe green: a check named "prove the arms are
    FED" that skipped the feeding. Same failure class as a wiring check that
    greps an identifier and matches the import after the call is gone.

    Making the registered callback a no-op is the faithful mutation — it is
    precisely "the hook no longer reaches the arms".
    """
    monkeypatch.setattr(audit_plugin, "on_post_tool_call", lambda **_kw: None)
    ok, reason = asyncio.run(audit_plugin.run_loop_arm_boot_probe())
    assert not ok, (
        "the probe passed while the hook did not call the arms at all — it is "
        "measuring the inner function instead of the wiring, which is the exact "
        "defect this feature exists to prevent"
    )
    assert "did not trip the tool-failure arm" in reason


def test_probe_does_not_leak_its_throwaway_breaker(audit_plugin) -> None:
    """A leaked self-check breaker would meter LIVE tool calls into a temp file
    — worse than the defect the probe catches — so the restore is asserted
    rather than assumed."""
    assert audit_plugin._SELFCHECK_BREAKER is None
    asyncio.run(audit_plugin.run_loop_arm_boot_probe())
    assert audit_plugin._SELFCHECK_BREAKER is None, "probe leaked its throwaway breaker"


class _RecordingBreaker:
    """Records which arm was fed, without a database."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def record_tool_failure(self, skill_name=None):
        self.calls.append(("failure", skill_name))

    def record_tool_success(self):
        self.calls.append(("success", None))

    def record_refusal(self, skill_name=None):
        self.calls.append(("refusal", skill_name))


def test_a_real_turn_never_picks_up_the_selfcheck_breaker(audit_plugin, monkeypatch) -> None:
    """Two conditions guard the substitution: the global being set AND the
    self-check session id. Set the global by hand, drive a REAL-looking
    envelope, and require it to reach the seat's own breaker."""
    throwaway = _RecordingBreaker()
    real = _RecordingBreaker()
    monkeypatch.setattr(audit_plugin, "_SELFCHECK_BREAKER", throwaway)
    monkeypatch.setattr(audit_plugin, "_cost_breaker", lambda: real)

    audit_plugin._meter_loop_arms(
        {"status": "error", "tool_name": "t", "session_id": "a-real-turn"}
    )
    assert real.calls == [("failure", "t")], "a real turn must use the seat's breaker"
    assert throwaway.calls == [], "a real turn must never reach the self-check breaker"
