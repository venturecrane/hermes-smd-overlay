"""hermes-smd-memory-mirror — mirror Honcho conclusions to per-customer D1.

Ported from ss-console/operator/adapter/memory/. The original
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
import os
from typing import Any

from . import archive, dismiss, honcho_client, mirror, schemas, state  # noqa: F401
from .mirror import mirror_session

logger = logging.getLogger(__name__)

# The complete env contract for the Honcho mirror lane. ADR 0016's 2026-05-30
# revision deferred the Honcho inference engine to Phase 2 and ADR 0048's lane
# table marks the inferred lane "deferred until Honcho runs" — so on a
# correctly provisioned seat today NONE of these are set, and that absence is
# an AUTHORED state (unconfigured = fail-closed, ADR 0037 tenet 3), not a
# fault. Registration is gated on this contract so an inactive lane is one
# quiet boot line instead of a misleading "degraded" WARNING on every session
# end (ss-console#1643, which mistook that noise for a provisioning gap).
_MIRROR_ENV = ("SMD_D1_OBSERVATIONS_BINDING", "HONCHO_BASE_URL", "HONCHO_API_KEY")

# Partial-contract error is reported once per process from the callback path
# (register() also reports at boot); a per-session ERROR would just be new noise.
_partial_reported = False


def _mirror_lane_state() -> tuple[str, list[str]]:
    """Classify the Honcho-mirror env contract: 'configured' (all set),
    'inactive' (none set — the authored deferred state), or 'partial'
    (some set — a real provisioning error that must be loud)."""
    present = [name for name in _MIRROR_ENV if os.environ.get(name)]
    if len(present) == len(_MIRROR_ENV):
        return "configured", present
    if not present:
        return "inactive", present
    return "partial", present


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
    state_name, present = _mirror_lane_state()
    if state_name == "inactive":
        # The authored deferred state (ADR 0016 Phase 2): silent by design.
        return
    if state_name == "partial":
        global _partial_reported
        if not _partial_reported:
            _partial_reported = True
            _log_partial(present)
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
    """Plugin entry point. Wires the one hook (always — plugin.yaml parity),
    and reports the Honcho lane's env-contract state once at boot:

    * configured — all three env vars set: the callback mirrors normally.
    * inactive — none set (the authored deferred state, ADR 0016 Phase 2 /
      ADR 0048 lane table): one INFO line here; the callback then returns
      silently every session end, because a lane that is deliberately off
      is not "degraded" (ss-console#1643).
    * partial — some-but-not-all set: a real provisioning error, reported
      as ERROR here (and once more from the callback if the state changes
      mid-life) so the conformance sweep catches it.
    """
    ctx.register_hook("on_session_end", on_session_end)
    state_name, present = _mirror_lane_state()
    if state_name == "configured":
        logger.info("hermes-smd-memory-mirror registered (on_session_end); lane configured")
    elif state_name == "inactive":
        logger.info(
            "hermes-smd-memory-mirror registered (on_session_end); Honcho lane "
            "unconfigured — mirror inactive by design (ADR 0016 Phase 2 deferred; "
            "ADR 0048 lane table)"
        )
    else:
        _log_partial(present)


def _log_partial(present: list[str]) -> None:
    missing = [name for name in _MIRROR_ENV if name not in present]
    logger.error(
        "hermes-smd-memory-mirror: PARTIAL env contract — %s set but %s missing. "
        "This is a provisioning error: set all of %s to activate the mirror, or "
        "none to leave the lane off.",
        present,
        missing,
        list(_MIRROR_ENV),
    )
