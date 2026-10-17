"""hermes_segment_cost charges every job segment (overlay review OS-1).

THE FINDING. The pinned Hermes (v2026.9.14, SHA 345cd2b0, agent/usage_pricing.py)
returns a frozen ``CostResult`` dataclass from ``estimate_usage_cost``, whose
``amount_usd`` is a ``Decimal`` (or ``None`` when the route is unpriced). The
reader did ``float(cost)`` on it, which raised, and its broad except recorded 0.
No job segment was ever charged to the job budget or fed to the cost breaker.

THE FIX. Read ``amount_usd``; keep the dict/number shapes; and when Hermes cannot
price a segment (unpriced model, unknown shape, a raise) charge it from the
overlay's own price table, then from conservative Opus-class rates. Never 0.

These stub ``agent.usage_pricing`` with the pinned shape, because CI has no
Hermes on the import path for this module.
"""

from __future__ import annotations

import dataclasses
import sys
import types
from decimal import Decimal

import pytest

from shared.job_worker_runtime import hermes_segment_cost


@dataclasses.dataclass(frozen=True)
class _CostResult:
    amount_usd: Decimal | None
    status: str = "estimated"
    source: str = "official_docs_snapshot"
    label: str = "x"


def _segment_agent(model: str = "claude-sonnet-4-6", **tokens: int) -> types.SimpleNamespace:
    base = {
        "session_input_tokens": 4_000_000,
        "session_output_tokens": 200_000,
        "session_cache_read_tokens": 0,
        "session_cache_write_tokens": 0,
    }
    base.update(tokens)
    return types.SimpleNamespace(model=model, provider="anthropic", base_url=None, **base)


@pytest.fixture
def stub_pricing(monkeypatch):
    """Install a stub ``agent.usage_pricing`` whose estimate returns ``result``."""
    seen: dict = {}

    def install(result):
        mod = types.ModuleType("agent.usage_pricing")

        def estimate_usage_cost(model, usage, **kwargs):
            seen.update(model=model, usage=usage, kwargs=kwargs)
            if isinstance(result, BaseException):
                raise result
            return result

        mod.estimate_usage_cost = estimate_usage_cost
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
        monkeypatch.setitem(sys.modules, "agent.usage_pricing", mod)
        return seen

    return install


def test_segment_cost_reads_the_pinned_hermes_cost_result(stub_pricing):
    seen = stub_pricing(_CostResult(Decimal("12.34")))
    assert hermes_segment_cost(_segment_agent()) == 1234
    assert seen["usage"].input_tokens == 4_000_000
    assert seen["kwargs"]["provider"] == "anthropic"


@pytest.mark.parametrize(
    ("result", "cents"),
    [({"total": 1.5}, 150), ({"amount_usd": "2.25"}, 225), (0.42, 42), (Decimal("3"), 300)],
)
def test_segment_cost_accepts_dict_and_number_shapes(stub_pricing, result, cents):
    stub_pricing(result)
    assert hermes_segment_cost(_segment_agent()) == cents


def test_unpriced_model_falls_to_the_overlay_price_table(stub_pricing):
    # Hermes returns amount_usd=None for a model its table lacks (the pinned tag
    # does not price claude-sonnet-5-5). shared/anthropic_pricing.json does:
    # $2/MTok in, $10/MTok out -> 4M in + 0.2M out = $10.
    stub_pricing(_CostResult(None, status="unknown"))
    assert hermes_segment_cost(_segment_agent("claude-sonnet-5-5")) == 1000


@pytest.mark.parametrize("result", [object(), "n/a", None, RuntimeError("pricing blew up")])
def test_unpriceable_segment_is_charged_conservatively_never_zero(stub_pricing, result):
    stub_pricing(result)
    # Opus-class fallback: 4M * $15 + 0.2M * $75 = $75.
    assert hermes_segment_cost(_segment_agent("no-such-model")) == 7500


def test_missing_hermes_pricing_module_is_charged_conservatively(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.usage_pricing", None)
    assert hermes_segment_cost(_segment_agent("no-such-model")) == 7500
