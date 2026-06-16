"""hermes-smd-mcp-result-sink — capture a completed turn for synchronous MCP return.

The MCP channel (Claude as an inbound channel,
docs/design/operator/03-mcp-server-exposure.md) needs to answer a `tools/call`
synchronously, but Hermes dispatches webhook-borne turns fire-and-forget
(``gateway/platforms/webhook.py`` returns 202 and delivers the answer
out-of-band). This plugin is the agent-side half of the bridge: it captures the
completed turn's answer and hands it to the cross-process result store, where
the gate's ``/mcp`` long-poll picks it up (``shared/mcp_result_store.py``).

Attaches to ONE hook at the pinned Hermes ref (v2026.5.16):

- ``post_llm_call`` (``run_agent.py`` L15902) — fires once per completed,
  non-interrupted turn, carrying ``session_id``, ``user_message`` (the clean
  ``original_user_message``, L12289/L12517), and ``assistant_response``.

**Correlation.** The gateway builds a dispatch chat-id ``webhook:mcp:<cid>``
(``webhook.py`` L529), but the agent-loop ``self.session_id`` post_llm_call
reports is a DIFFERENT, timestamped id — so keying on ``session_id`` misses
every time (verified on staging 2026-06-16). Instead the gate plants the
correlation id INTO the turn's message via the route prompt
(``[[mcp-cid:<cid>]]``, translate ``_INBOUND_MCP_PROMPT``), and this sink
recovers it from ``user_message`` — a reliable, session-id-independent handle.
Non-MCP turns carry no marker, so the sink no-ops for every other channel.

Observer-only and exception-safe per AGENTS.md hard rule #3: the callback always
returns None and swallows its own exceptions (a capture failure must never break
the agent turn; it only means the gate long-poll times out and the client
retries).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from shared import mcp_result_store

logger = logging.getLogger(__name__)

# The correlation marker the gate plants in the route prompt. The id charset
# matches mcp_result_store's safe-id rule (gate mints a uuid4 hex).
_CID_RE = re.compile(r"\[\[mcp-cid:([A-Za-z0-9_-]{1,128})\]\]")


def _correlation_id(user_message: str) -> str | None:
    """Recover the MCP correlation id planted in the turn's message, or None."""
    match = _CID_RE.search(user_message)
    return match.group(1) if match else None


def on_post_llm_call(**kwargs: Any) -> None:
    """Capture a completed MCP-channel turn into the cross-process result store.

    Acts only on turns whose message carries the ``[[mcp-cid:...]]`` marker;
    no-ops for every other channel. Returns None always; never raises into
    Hermes.
    """
    try:
        user_message = kwargs.get("user_message")
        if not isinstance(user_message, str):
            return
        cid = _correlation_id(user_message)
        if cid is None:
            return  # not an MCP-channel turn

        answer = kwargs.get("assistant_response")
        if not isinstance(answer, str):
            # Defensive: an unexpected shape still resolves the long-poll with a
            # truthful empty answer rather than letting the client hang.
            answer = "" if answer is None else str(answer)

        ok = mcp_result_store.put(cid, {"answer": answer})
        if not ok:
            logger.warning(
                "hermes-smd-mcp-result-sink: failed to store result for an MCP turn"
            )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-mcp-result-sink: post_llm_call handler error: %s", exc)


def register(ctx) -> None:
    """Plugin entry point. Wires ``post_llm_call`` unconditionally.

    No infra to resolve and no authorization flag: the sink is a structural
    capture for MCP-channel turns only (identified by the prompt marker), with a
    single local-file side effect.
    """
    ctx.register_hook("post_llm_call", on_post_llm_call)
    logger.info(
        "hermes-smd-mcp-result-sink registered (captures [[mcp-cid:*]] turns → %s)",
        mcp_result_store.store_dir(),
    )
