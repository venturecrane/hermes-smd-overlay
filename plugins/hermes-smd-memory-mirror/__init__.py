"""hermes-smd-memory-mirror — mirror Honcho conclusions to per-customer D1.

Ported from ss-console/ai-employee/adapter/memory/. The original
"customer-owned memory artifact" model (ADR 0008, superseded) is
replaced by the Honcho mirror pattern (ADR 0016): Honcho is the live
store; D1 holds a parallel record with provenance
(``source_message_ids``, ``confidence``, ``evidence_status``,
``mirrored_at``) so Captain operates on it through the admin portal
without standing between the agent and its working memory.

Attaches to one hook at the pinned Hermes ref (v2026.5.16):

* ``on_session_end`` (run_agent.py:16016-16024) — fires per-turn at
  the end of ``run_conversation()``. Safety-net firing site
  ``cli.py:13831-13839`` fires on interrupted CLI exit while the
  agent was mid-turn (the ``run_conversation`` path did not fire).

The callback polls Honcho for new conclusions on this session and
writes them to ``persona_observations`` with provenance. The callback
is exception-safe per AGENTS.md hard rule #3: any failure (missing
env, Honcho down, D1 write failure) degrades to a warning log and
returns; the session continues. The audit plugin emits a
``MEMORY_PROVIDER_DEGRADED`` row for Honcho outages — cross-correlate
via ``session_id``.

Modules:

* :mod:`mirror`         — poller + D1 writer (the hot path).
* :mod:`archive`        — TTL archival (called by cron, not the hook).
* :mod:`dismiss`        — Captain dismissal entry point.
* :mod:`honcho_client`  — thin HTTP client for the local sidecar.
* :mod:`schemas`        — D1 DDL + evidence-status vocabulary.
* :mod:`state`          — observation dataclasses + closed enums.
"""

import logging
from typing import Any

from . import archive, dismiss, honcho_client, mirror, schemas, state  # noqa: F401
from .mirror import mirror_session

logger = logging.getLogger(__name__)


def on_session_end(**kwargs: Any) -> None:
    """Honcho mirror trigger. Exception-safe; never raises.

    Per docs/hook-surface.md, kwargs are:
        session_id (str), completed (bool), interrupted (bool),
        model (str), platform (str).

    The hook fires per-turn (not per-conversation), matching Honcho's
    ``writeFrequency: session`` cadence that produces new conclusions
    at the same boundary. We mirror on every turn so D1 stays current.

    Failure modes (all degrade to a warning log):
        * Required env var missing → KeyError caught.
        * Honcho sidecar unreachable → HonchoUnreachable caught.
        * D1 write failure → caught inside :func:`mirror_session`.
        * Any other unexpected exception → caught by the broad except
          block below per AGENTS.md hard rule #3.
    """
    session_id = kwargs.get("session_id") or ""
    if not session_id:
        # Nothing to mirror without a session id. Hermes' dispatcher
        # always provides one, but defend against future changes.
        return
    try:
        result = mirror_session(session_id=session_id)
        logger.debug(
            "memory-mirror on_session_end: session=%s polled=%d written=%d skipped=%d",
            result.session_id,
            result.conclusions_polled,
            result.rows_written,
            result.rows_skipped,
        )
    except Exception as exc:  # noqa: BLE001 — hook callback must not raise
        logger.warning(
            "memory-mirror on_session_end degraded session=%s err=%s",
            session_id,
            exc,
        )


def register(ctx) -> None:
    """Plugin entry point. Wires the one hook."""
    ctx.register_hook("on_session_end", on_session_end)
    logger.info("hermes-smd-memory-mirror registered (on_session_end)")
