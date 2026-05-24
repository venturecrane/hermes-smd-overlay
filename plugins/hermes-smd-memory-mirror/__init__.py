"""hermes-smd-memory-mirror — mirror Honcho conclusions to per-customer D1 with provenance.

Attaches to one hook at the pinned Hermes ref (v2026.5.16):
- on_session_end (run_agent.py:16016-16024) — fires per-turn at end of run_conversation().
  Safety-net firing site: cli.py:13831-13839 — fires only on interrupted CLI exit while
  the agent was mid-turn (the run_conversation path did not fire).

Kwargs per docs/hook-surface.md: session_id, completed, interrupted, model, platform.

Real implementation ports from ss-console/ai-employee/adapter/memory/ in §7 of the
build plan. Approach is mirror-don't-gate per ADR 0016: Honcho remains the live store,
D1 holds a parallel record with provenance (source_message_ids, confidence,
evidence_status) so Captain can dismiss/restore via the admin portal.
"""

import logging
from typing import Any

from . import archive, dismiss, mirror, schemas  # noqa: F401 — surface module imports for downstream tests

logger = logging.getLogger(__name__)


def on_session_end(**kwargs: Any) -> None:
    """Stub. Real impl mirrors any new Honcho conclusions for this session to D1.

    Expected kwargs per docs/hook-surface.md:
        session_id, completed (bool), interrupted (bool), model, platform
    """
    logger.debug("hermes-smd-memory-mirror: on_session_end stub (port logic in §7)")


def register(ctx) -> None:
    """Plugin entry point. Wires the one hook."""
    ctx.register_hook("on_session_end", on_session_end)
    logger.info("hermes-smd-memory-mirror registered (stub mode; awaiting §7 logic port)")
