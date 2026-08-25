"""Runaway-loop arms of the sticky-stop ladder (ADR 0062).

WHAT WAS BROKEN. The ladder has four arms. Only the cost arm was ever fed:
``record_cost_cents`` from the interactive meter and the job segment loop.
``record_tool_failure`` and ``record_refusal`` were fully implemented,
thresholded, audited and unit-tested, and had no caller in either repo. An
Operator that overspent stopped; an Operator stuck in a loop failing the same
call, or refusing every call, ran until a human noticed.

WHAT THESE TESTS PIN.

  1. The wrapper arms exist and climb the ladder (the ``CostBreaker`` half).
  2. ``post_tool_call`` actually FEEDS them (the wiring half) — built is not
     wired, and a breaker with perfect arms that nothing calls is the exact
     state this change is fixing.
  3. Success resets the streak. Without it the ladder only ever climbs and a
     healthy long-lived seat eventually stops for no reason. That is the
     regression most likely to be introduced by someone "simplifying" the
     handler, so it is asserted directly rather than implied.
  4. Detection is POSITIVE-ONLY: an absent or unfamiliar ``status`` records
     nothing. An upstream envelope rename must degrade to the old unbraked
     behaviour, never manufacture a stop on a live client seat.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from shared.cost_breaker import build_breaker
from shared.sticky_stop import DEFAULT_THRESHOLDS


def load_plugin(plugin_name: str):
    """Load the plugin package so its relative imports resolve.

    Same local loader as ``tests/test_audit_emit.py``, and for the same
    reason recorded there: the shared ``conftest.load_plugin`` executes the
    module before registering it in ``sys.modules``, so ``from . import emit``
    inside the plugin raises ``ModuleNotFoundError``. Registering first is the
    whole difference.
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


class FakeAuditClient:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def execute(self, sql: str, *params) -> None:
        self.rows.append((sql, params))


def _breaker(tmp_path: Path):
    return build_breaker(
        customer="acme",
        persona="_machine",
        audit_client=FakeAuditClient(),
        path=str(tmp_path / "sticky.db"),
    )


# ----------------------------------------------------------- the arms ------


def test_consecutive_tool_failures_climb_to_hard_stop(tmp_path: Path) -> None:
    breaker = _breaker(tmp_path)
    state = None
    for _ in range(DEFAULT_THRESHOLDS.tool_failure_hard_stop):
        state = breaker.record_tool_failure("demand-letter-drafter")
    assert state is not None
    assert state.level.value == "HARD_STOP"


def test_soft_stop_arrives_before_hard_stop(tmp_path: Path) -> None:
    # The intermediate rung matters: SOFT_STOP pins every skill to
    # draft_for_review rather than refusing outright, so a wobbling seat
    # degrades to "needs a human" before it degrades to "does nothing".
    breaker = _breaker(tmp_path)
    state = None
    for _ in range(DEFAULT_THRESHOLDS.tool_failure_soft_stop):
        state = breaker.record_tool_failure()
    assert state is not None
    assert state.level.value == "SOFT_STOP"


def test_success_resets_the_failure_streak(tmp_path: Path) -> None:
    breaker = _breaker(tmp_path)
    for _ in range(DEFAULT_THRESHOLDS.tool_failure_hard_stop - 1):
        breaker.record_tool_failure()
    breaker.record_tool_success()
    # One more failure after the reset must NOT be the hard-stop-th.
    state = breaker.record_tool_failure()
    assert state.level.value != "HARD_STOP"


def test_refusal_cascade_climbs_to_hard_stop(tmp_path: Path) -> None:
    breaker = _breaker(tmp_path)
    state = None
    for _ in range(DEFAULT_THRESHOLDS.refusal_hard_stop):
        state = breaker.record_refusal("email-triage")
    assert state is not None
    assert state.level.value == "HARD_STOP"


# --------------------------------------------------------- the wiring ------


class _RecordingBreaker:
    """Stands in for the armed CostBreaker so the handler's decisions are
    observable without a database."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def record_tool_failure(self, skill_name=None):
        self.calls.append(("failure", skill_name))
        return None

    def record_tool_success(self):
        self.calls.append(("success", None))
        return None

    def record_refusal(self, skill_name=None):
        self.calls.append(("refusal", skill_name))
        return None


@pytest.fixture
def audit_plugin():
    """The audit plugin, via the repo's own hyphen-safe loader (conftest)."""
    return load_plugin("hermes-smd-audit")


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"status": "ok", "tool_name": "read_file"}, [("success", None)]),
        (
            {"status": "error", "tool_name": "email_create_draft"},
            [("failure", "email_create_draft")],
        ),
        (
            {
                "status": "blocked",
                "error_type": "plugin_block",
                "tool_name": "email_send",
            },
            [("refusal", "email_send")],
        ),
        # A block that is NOT ours belongs to neither ladder.
        ({"status": "blocked", "error_type": "tool_error"}, []),
        # POSITIVE-ONLY. Each of these must record nothing: an envelope that
        # changed shape has to leave the seat exactly as unbraked as it was
        # before this code existed, never trip it.
        ({"tool_name": "read_file"}, []),
        ({"status": None}, []),
        ({"status": "OK"}, []),
        ({"status": "succeeded"}, []),
        ({}, []),
    ],
)
def test_post_tool_call_feeds_the_right_arm(audit_plugin, monkeypatch, kwargs, expected):
    recorder = _RecordingBreaker()
    monkeypatch.setattr(audit_plugin, "_cost_breaker", lambda: recorder)
    audit_plugin._meter_loop_arms(kwargs)
    assert recorder.calls == expected


def test_metering_runs_even_when_the_audit_writer_is_dark(audit_plugin, monkeypatch):
    """A dark ledger must not disarm the brake.

    ``on_post_tool_call`` returns early when the audit writer is unconfigured.
    The arms are fed BEFORE that return on purpose: D1 being unreachable and
    the agent being stuck in a loop are different failures, and the first is
    not a reason to stop watching for the second.
    """
    recorder = _RecordingBreaker()
    monkeypatch.setattr(audit_plugin, "_cost_breaker", lambda: recorder)
    monkeypatch.setattr(audit_plugin, "_writer", lambda: None)
    audit_plugin.on_post_tool_call(status="error", tool_name="practice_management_search")
    assert recorder.calls == [("failure", "practice_management_search")]


def test_handler_never_raises_out_of_the_hook(audit_plugin, monkeypatch):
    """AGENTS.md hard rule 3. A breaker fault is not a tool fault."""

    class _Exploding:
        def record_tool_failure(self, skill_name=None):
            raise RuntimeError("db gone")

    monkeypatch.setattr(audit_plugin, "_cost_breaker", lambda: _Exploding())
    audit_plugin._meter_loop_arms({"status": "error", "tool_name": "x"})  # must not raise


def test_unarmed_breaker_is_a_no_op(audit_plugin, monkeypatch):
    monkeypatch.setattr(audit_plugin, "_cost_breaker", lambda: None)
    audit_plugin._meter_loop_arms({"status": "error", "tool_name": "x"})  # must not raise
