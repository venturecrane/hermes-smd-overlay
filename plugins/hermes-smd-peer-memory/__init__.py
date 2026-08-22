"""hermes-smd-peer-memory — per-peer working-preference memory (ADR 0048 learned lane).

The Operator's personality is its relationships, applied: for each colleague it
works with, a separate memory of how THAT person likes to work with it, captured
from the content of their requests on any channel and surfaced before each turn.
Hermes' native memory is per-profile and identity-blind (one MEMORY.md/USER.md,
no per-peer keying, no capture nudge), so this plugin builds the per-peer layer
on top of it without touching Hermes core.

Three hooks at the pinned Hermes ref (v2026.5.16):

- ``pre_llm_call`` (run_agent.py:12447-12457) — carries the ONLY per-peer id
  Hermes threads (``sender_id``). This hook does double duty: (1) stash
  ``sender_id`` keyed by ``session_id`` so the capture path can attribute the
  peer server-side, and (2) inject that peer's active preferences into the turn.
- ``post_tool_call`` (model_tools.py) — carries ``args`` + ``session_id`` but
  NOT ``sender_id``. When the agent called :data:`TOOL_NAME`, this is where the
  row is written: the peer is resolved from the stash (the agent never supplies
  it, so it cannot record a preference for someone else), the session taint-gate
  is checked, and the validated preference is persisted.
- ``on_session_end`` — drop the session's stashed sender.

Capture is explicit, not black-box: the agent (which can read the request) calls
:data:`TOOL_NAME` to record a concrete stated/demonstrated preference. Inference
of traits is rejected by construction (the schema has no trait field and the
tool's source enum is the only provenance).

Per AGENTS.md hard rule #3 every hook is exception-safe: a failure logs a
warning and degrades to no-op (``None`` for pre_llm_call) — peer memory never
breaks the agent loop.
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from typing import Any

from shared.d1_client import D1Client
from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT
from shared.tool_registration import register_wrapped_tool

from . import schemas, store  # noqa: F401 — surface module imports for tests

logger = logging.getLogger(__name__)


TOOL_NAME = "record_peer_preference"

TOOL_DESCRIPTION = (
    "Record how the person you are currently working with likes you to work with "
    "them, so you apply it next time. Use this when they STATE a preference "
    "('reply in bullet points', 'always loop in my partner', 'don't send anything "
    "without my OK') or when you DEMONSTRABLY observe one in how they work. Record "
    "a concrete, actionable preference, never a personality judgement or trait "
    "label. The person is attributed automatically from the current conversation; "
    "you do not name them."
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "preference": {
            "type": "string",
            "description": (
                "The concrete, actionable preference, phrased as how to work with "
                "them. e.g. 'Wants short bullet summaries, not prose.' Never a trait "
                "label like 'is impatient'."
            ),
        },
        "why": {
            "type": "string",
            "description": "Optional. Their stated reason, if they gave one.",
        },
        "how_to_apply": {
            "type": "string",
            "description": "Optional. Concretely how to apply this next time.",
        },
        "source": {
            "type": "string",
            "enum": ["stated", "demonstrated"],
            "description": (
                "'stated' = they explicitly said it. 'demonstrated' = you observed "
                "it concretely in how they worked. Never infer a trait."
            ),
        },
    },
    "required": ["preference", "source"],
}


# Module-level runtime bindings, wired by register() (or bind_runtime() in tests).
# ``None`` means "store not active" — the hooks degrade to no-op rather than raise.
_D1: D1Client | None = None
_CUSTOMER_SLUG: str | None = None

# sender_id stash, keyed by session_id. Hermes carries sender_id only on
# pre_llm_call; the capture path (post_tool_call) reads it back from here so the
# peer is attributed server-side. Bounded LRU so a long-lived Machine cannot
# grow it without limit; on_session_end evicts eagerly.
_MAX_SESSIONS = 4096
_sender_by_session: OrderedDict[str, str] = OrderedDict()


def bind_runtime(*, customer_slug: str, client: D1Client | None) -> None:
    """Bind the runtime collaborators that make the hooks active.

    Called by :func:`register` after it constructs the D1 client, and directly
    by tests that inject a client against a tmp sqlite file. Until this runs (or
    if ``client`` is ``None``), capture/inject no-op.
    """
    global _D1, _CUSTOMER_SLUG
    _D1 = client
    _CUSTOMER_SLUG = customer_slug
    logger.info(
        "hermes-smd-peer-memory: runtime bound customer=%s store=%s",
        customer_slug,
        "active" if client is not None else "inactive",
    )


def _resolve_peer(session_id: str, sender_id: str) -> str:
    """The person this turn belongs to — never a channel identity.

    Webhook-dispatched turns thread the ROUTE as ``sender_id`` (e.g.
    ``webhook:agentmail``), which is a channel, not a person: keying on it
    collapses every email correspondent into one shared peer (found by the ss
    #1941 live probe — the first captured preference keyed ``webhook:agentmail``
    instead of the sender's address). The webhook router records the
    Svix-verified sender of the inbound (``SESSION_INBOUND_ORIGIN``, the reply
    channel's recipient-lock anchor) — prefer that address; it is verified
    attribution, never content-derived. On the live email path the dispatch
    carries NO session id (observed 2026-07-15), so the session lookup misses
    and the claim-once unbound handoff resolves it instead; the claim declines
    under ambiguity, falling back to the channel identity rather than ever
    guessing a person. Channels that thread a real per-user id (Telegram) have
    no recorded origin and keep their ``sender_id`` unchanged.
    """
    try:
        origin = SESSION_INBOUND_ORIGIN.get(session_id) if session_id else None
        # Only a channel-shaped sender consults the unbound handoff: a real
        # per-user id (Telegram) must never be overridden by a coincidentally
        # pending email origin.
        if origin is None and sender_id.startswith("webhook:"):
            origin = SESSION_INBOUND_ORIGIN.claim_unbound()
    except Exception:  # noqa: BLE001 — resolution must never break the hook
        origin = None
    if origin is not None and origin.sender_address:
        addr = origin.sender_address.strip().lower()
        if addr:
            return addr
    return sender_id


def _stash_sender(session_id: str, sender_id: str) -> None:
    _sender_by_session[session_id] = sender_id
    _sender_by_session.move_to_end(session_id)
    while len(_sender_by_session) > _MAX_SESSIONS:
        _sender_by_session.popitem(last=False)


def _persona_slug(args: Any = None) -> str:
    """Resolve the active persona slug (mirrors the audit plugin's resolution).

    Single-persona Machines (ADR 0011 v1) commonly leave these unset; an empty
    slug is stored as-is and read back as the all-persona superset, so capture
    and inject stay consistent within a Machine.
    """
    persona = os.getenv("HERMES_ACTIVE_PROFILE") or os.getenv("SMD_ACTIVE_PERSONA") or ""
    if not persona and isinstance(args, dict):
        candidate = args.get("persona_slug") or args.get("persona")
        if isinstance(candidate, str) and candidate.strip():
            persona = candidate.strip()
    return persona


def on_pre_llm_call(**kwargs: Any) -> dict | None:
    """Stash the turn's sender, then inject the per-peer context block.

    Expected kwargs (pre_llm_call): session_id, user_message,
    conversation_history, is_first_turn, model, platform, sender_id.

    Returns ``{"context": block}`` on every sender-attributed turn while the
    store is active: the peer's active preferences (read side) when any exist,
    always followed by the capture instruction (write side — the lane never
    fills if nothing tells the agent to record; ss #1941). ``None`` only when
    there is no sender to attribute or the store is inactive. Exception-safe.
    """
    try:
        session_id = kwargs.get("session_id") or ""
        sender_id = kwargs.get("sender_id") or ""
        peer_id = _resolve_peer(session_id, sender_id)
        if session_id and peer_id:
            _stash_sender(session_id, peer_id)

        if not peer_id or _D1 is None or not _CUSTOMER_SLUG:
            return None

        rows = store.active_preferences(_D1, peer_id=peer_id, persona_slug=_persona_slug())
        block = store.render_preference_block(rows, peer_id=peer_id)
        return {"context": block} if block else None
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-peer-memory: pre_llm_call failed", exc_info=True)
        return None


def record_peer_preference_tool(args: dict[str, Any], **_: Any) -> str:
    """Agent-callable capture tool. Validates and acknowledges.

    By Hermes' dispatch contract this handler receives no session/sender
    context, so it does NOT write — it validates the agent's input and returns
    an ack. The actual attributed write happens in :func:`on_post_tool_call`,
    which has both the args and the session_id needed to resolve the peer.

    The ack key is ``ok``, not ``recorded`` (ss-console#2552). This string is the
    last thing the model reads before composing its reply, and handing it the
    word "recorded" is part of how a confirm email came to read "That preference
    is recorded to your profile". No caller reads this key — the write is driven
    from the tool ARGS in :func:`on_post_tool_call` — so the vocabulary is free
    to be service-shaped.
    """
    clean, error = store.parse_capture_args(args if isinstance(args, dict) else {})
    if error:
        return json.dumps({"ok": False, "error": error})
    return json.dumps({"ok": True, "preference": clean["preference"], "source": clean["source"]})


def on_post_tool_call(**kwargs: Any) -> None:
    """When the agent called the capture tool, persist the preference.

    Expected kwargs (post_tool_call): tool_name, args, result, task_id,
    session_id, tool_call_id, duration_ms.

    Attribution is server-side: the peer is the stashed sender for this
    session, never an agent-supplied value. A taint-flagged session cannot
    write (ADR 0048 §2f) — durable per-peer memory must not be plantable by
    injected content; reads and drafts on a tainted turn are unaffected.
    Exception-safe.
    """
    try:
        if (kwargs.get("tool_name") or "") != TOOL_NAME:
            return
        if _D1 is None or not _CUSTOMER_SLUG:
            logger.warning("hermes-smd-peer-memory: capture skipped (store inactive)")
            return

        session_id = kwargs.get("session_id") or ""
        if session_id and SESSION_TAINT.is_tainted(session_id):
            logger.warning(
                "hermes-smd-peer-memory: capture refused on tainted session %s", session_id
            )
            return

        sender_id = _sender_by_session.get(session_id)
        if not sender_id:
            logger.warning(
                "hermes-smd-peer-memory: capture skipped — no sender stashed for session %s",
                session_id,
            )
            return

        raw_args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
        clean, error = store.parse_capture_args(raw_args)
        if error:
            logger.warning("hermes-smd-peer-memory: capture rejected: %s", error)
            return

        store.record_preference(
            _D1,
            customer_slug=_CUSTOMER_SLUG,
            peer_id=sender_id,
            persona_slug=_persona_slug(raw_args),
            preference=clean["preference"],
            why=clean["why"],
            how_to_apply=clean["how_to_apply"],
            source=clean["source"],
            session_id=session_id,
        )
        logger.info(
            "hermes-smd-peer-memory: recorded preference (session=%s source=%s)",
            session_id,
            clean["source"],
        )
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-peer-memory: post_tool_call failed", exc_info=True)


def on_session_end(**kwargs: Any) -> None:
    """Evict the session's stashed sender. Exception-safe."""
    try:
        session_id = kwargs.get("session_id") or ""
        _sender_by_session.pop(session_id, None)
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-peer-memory: on_session_end failed", exc_info=True)


def register(ctx) -> None:
    """Plugin entry point. Wires three hooks + the capture tool, then binds D1.

    Always registers the hooks and the tool (so Hermes' contract holds), then
    resolves the customer slug + agent-state binding and creates the table
    idempotently. If env is missing the plugin registers but stays INACTIVE and
    says so at WARNING — never a silent healthy-looking no-op.
    """
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)
    register_wrapped_tool(
        ctx,
        name=TOOL_NAME,
        toolset="relationship",
        schema=TOOL_SCHEMA,
        handler=record_peer_preference_tool,
        requires_env=["SMD_CUSTOMER_SLUG"],
        description=TOOL_DESCRIPTION,
        emoji="",
    )

    slug = os.environ.get("SMD_CUSTOMER_SLUG") or None
    if not slug:
        bind_runtime(customer_slug="", client=None)
        _set_inactive_slug(None)
        logger.warning(
            "hermes-smd-peer-memory registered but INACTIVE — SMD_CUSTOMER_SLUG missing; "
            "capture/inject will no-op until configured."
        )
        return

    state_binding = os.environ.get("SMD_D1_AGENT_STATE_BINDING") or os.environ.get(
        "SMD_D1_AUDIT_BINDING"
    )
    if not state_binding:
        bind_runtime(customer_slug=slug, client=None)
        logger.warning(
            "hermes-smd-peer-memory registered but store INACTIVE — neither "
            "SMD_D1_AGENT_STATE_BINDING nor SMD_D1_AUDIT_BINDING set; capture/inject no-op."
        )
        return

    try:
        client = D1Client(binding_name=state_binding, customer_slug=slug)
        store.ensure_schema(client)
        bind_runtime(customer_slug=slug, client=client)
        logger.info(
            "hermes-smd-peer-memory registered and ACTIVE (customer=%s state_binding=%s)",
            slug,
            state_binding,
        )
    except Exception as exc:  # noqa: BLE001 — never crash Hermes plugin load
        bind_runtime(customer_slug=slug, client=None)
        logger.error(
            "hermes-smd-peer-memory: store init failed; capture/inject will no-op: %s", exc
        )


def _set_inactive_slug(slug: str | None) -> None:
    """Internal: set the slug without a client (keeps bind_runtime semantics)."""
    global _CUSTOMER_SLUG
    _CUSTOMER_SLUG = slug


__all__ = [
    "TOOL_DESCRIPTION",
    "TOOL_NAME",
    "TOOL_SCHEMA",
    "bind_runtime",
    "on_post_tool_call",
    "on_pre_llm_call",
    "on_session_end",
    "record_peer_preference_tool",
    "register",
]
