"""hermes-smd-mcp-result-sink — capture a completed turn for synchronous MCP return.

The MCP channel (Claude as an inbound channel,
docs/design/operator/03-mcp-server-exposure.md) needs to answer a `tools/call`
synchronously, but Hermes dispatches webhook-borne turns fire-and-forget
(``gateway/platforms/webhook.py`` returns 202 and delivers the answer
out-of-band). This plugin is the agent-side half of the bridge: it captures the
completed turn's answer and hands it to the cross-process result store, where
the gate's ``/mcp`` long-poll picks it up (``shared/mcp_result_store.py``).

Attaches to ONE hook at the pinned Hermes ref (v2026.5.16):

- ``post_llm_call`` (``run_agent.py``) — fires once per completed, non-interrupted
  turn, carrying ``session_id`` + ``assistant_response`` (see
  ``plugins/hermes-smd-hook-probe`` / ``docs/hook-surface.md``).

Scope is structural, not behavioural: the plugin acts only on sessions whose id
is ``webhook:mcp:<correlation_id>`` (the session key Hermes builds from the
``mcp`` webhook route + the gate-supplied ``X-Request-ID``,
``webhook.py`` L529). Every other channel's turns (Telegram, email, cron) are
ignored. There is no customer flag and no external side effect — it writes one
local file — so it is registered unconditionally and is safe on every customer.

Observer-only and exception-safe per AGENTS.md hard rule #3: the callback always
returns None and swallows its own exceptions (a capture failure must never break
the agent turn; it only means the gate long-poll times out and the client
retries).
"""

from __future__ import annotations

import logging
from typing import Any

from shared import mcp_result_store

logger = logging.getLogger(__name__)

# The session-id prefix Hermes builds for the `mcp` webhook route:
# ``webhook:{route}:{delivery_id}`` with route=="mcp" and delivery_id == the
# gate-supplied correlation id (``webhook.py`` L529 / ``X-Request-ID`` L445-447).
_SESSION_PREFIX = "webhook:mcp:"


def _correlation_id(session_id: str) -> str | None:
    """Extract the correlation id from an ``webhook:mcp:<id>`` session id."""
    if session_id.startswith(_SESSION_PREFIX):
        cid = session_id[len(_SESSION_PREFIX) :]
        return cid or None
    return None


def on_post_llm_call(**kwargs: Any) -> None:
    """Capture a completed MCP-channel turn into the cross-process result store.

    Acts only on ``webhook:mcp:*`` sessions; no-ops for every other channel.
    Returns None always; never raises into Hermes.
    """
    try:
        session_id = kwargs.get("session_id") or ""
        cid = _correlation_id(session_id)
        if cid is None:
            return  # not an MCP-channel turn

        answer = kwargs.get("assistant_response")
        if not isinstance(answer, str):
            # Defensive: an unexpected shape still resolves the long-poll with a
            # truthful empty answer rather than letting the client hang.
            answer = "" if answer is None else str(answer)

        ok = mcp_result_store.put(cid, {"answer": answer, "session_id": session_id})
        if not ok:
            logger.warning(
                "hermes-smd-mcp-result-sink: failed to store result for an MCP turn"
            )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-mcp-result-sink: post_llm_call handler error: %s", exc)


def register(ctx) -> None:
    """Plugin entry point. Wires ``post_llm_call`` unconditionally.

    No infra to resolve and no authorization flag: the sink is a structural
    capture for MCP-channel sessions only, with a single local-file side effect.
    """
    ctx.register_hook("post_llm_call", on_post_llm_call)
    logger.info(
        "hermes-smd-mcp-result-sink registered (captures webhook:mcp:* turns → %s)",
        mcp_result_store.store_dir(),
    )
