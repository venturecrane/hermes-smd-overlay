"""Thinking-off on current Claude models: adaptive at low effort, never "disabled".

Hermes asks for thinking off on its own side-calls: the session auto-title
(``agent/title_generator.py`` passes ``reasoning_config={"enabled": False}``),
and any ``auxiliary.<task>`` or main turn configured ``reasoning_effort: none``
(``hermes_constants.py`` parses it to the same dict). Every Anthropic-wire
request funnels through ``agent/anthropic_adapter.py`` ``_thinking_kwargs``
(called from ``build_anthropic_kwargs``), which renders thinking-off as
``thinking: {"type": "disabled"}`` for every adaptive Claude model outside a
hard-coded mandatory list that names only ``claude-fable`` (pinned Hermes
v2026.9.14).

The current API does not take that. ``disabled`` is a 400 on Claude Opus 5.5
and Claude Sonnet 5.5 at every effort, and on Fable 5/5.1; on Claude Opus 5
and Claude Haiku 5.5 it is accepted only at effort ``high`` or below and has
known failure modes (tool calls written into visible text). The API's own
answer to "think as little as possible" on these models is adaptive thinking
with ``output_config.effort: "low"``. Sentry SMD-OPERATOR-1M was the visible
cost: every session on an Opus 5.5 seat lost its title and paged (175 events
from 2026-08-20).

So for any Claude model on the adaptive contract (4.6 and later, including
every 5.x and any future Claude model, by Hermes' own default-to-modern rule
``_supports_adaptive_thinking``) a thinking-off request becomes
``{"type": "adaptive"}`` plus ``output_config.effort: "low"``. No ``display``
key: the reasoning is not shown, so nothing asks for a summary. Every other
model (Haiku 4.5, the 3.x/4.0/4.1/4.5 manual-thinking families, non-Claude
Anthropic-wire endpoints such as Kimi or MiniMax) and every thinking-ON request
keep Hermes' behaviour unchanged.

Installed by the hermes-smd-usage plugin at register time (the plugin that
already observes every API request), so it is in the gateway process that
makes these calls. Fail-open: if the pinned Hermes no longer has the seam,
install logs and leaves Hermes untouched, and the overlay test that runs
against the pinned source fails first.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

THINKING_OFF_EFFORT = "low"
_MARKER = "_smd_thinking_off_wrapped"


def thinking_off_kwargs(
    model: str, supports_adaptive: Callable[[str], bool]
) -> dict[str, Any] | None:
    """The thinking kwargs for a thinking-off request, or None to defer to Hermes.

    ``supports_adaptive`` is Hermes' own ``_supports_adaptive_thinking`` so the
    model classification stays Hermes' (one source of truth for which Claude
    families are on the adaptive contract)."""
    name = (model or "").lower()
    if "claude" not in name or not supports_adaptive(model):
        return None
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": THINKING_OFF_EFFORT}}


def wrap(original: Callable[..., dict[str, Any]], supports_adaptive: Callable[[str], bool]):
    """Wrap Hermes' ``_thinking_kwargs(reasoning_config, model, effective_max_tokens)``."""
    if getattr(original, _MARKER, False):
        return original

    def _thinking_kwargs(
        reasoning_config: dict[str, Any], model: str, effective_max_tokens: int
    ) -> dict[str, Any]:
        if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is False:
            mapped = thinking_off_kwargs(model, supports_adaptive)
            if mapped is not None:
                return mapped
        return original(reasoning_config, model, effective_max_tokens)

    setattr(_thinking_kwargs, _MARKER, True)
    _thinking_kwargs.__wrapped__ = original  # type: ignore[attr-defined]
    return _thinking_kwargs


def install() -> bool:
    """Patch the pinned Hermes adapter in this process. Idempotent; never raises."""
    try:
        from agent import anthropic_adapter
    except Exception as exc:  # noqa: BLE001 - no Hermes in this process (tests, tools)
        logger.debug("anthropic_thinking_off: Hermes adapter not importable (%s)", exc)
        return False
    original = getattr(anthropic_adapter, "_thinking_kwargs", None)
    supports = getattr(anthropic_adapter, "_supports_adaptive_thinking", None)
    if not callable(original) or not callable(supports):
        logger.warning(
            "anthropic_thinking_off: pinned Hermes has no _thinking_kwargs/_supports_adaptive_thinking; "
            "thinking-off requests keep Hermes' rendering"
        )
        return False
    anthropic_adapter._thinking_kwargs = wrap(original, supports)
    return True
