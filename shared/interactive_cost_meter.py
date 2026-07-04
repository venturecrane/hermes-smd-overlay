"""Interactive-turn cost meter — ADR 0062 §4, ss-console #1701.

The cost breaker's exact-cents enforcement (shared/cost_breaker.py) covers the
durable-job path, where the overlay constructs the agent and reads its real
`session_*_tokens`. But the DOMINANT spend path is interactive turns
(webhook-routed inbound + the console `/mcp/turn`), which Hermes drives — the
overlay sees them only through the `post_llm_call` hook, and our doctrine
(ADR 0015: pin-only Hermes, plugin-only overlay) forbids patching core to
enrich that hook with token counts.

So this module meters interactive turns from what the hook DOES provide —
`conversation_history`, `assistant_response`, `model` — with a local estimate,
and feeds the same sticky_stop ladder as the job path (one `record_cost_cents`,
one HARD_STOP state per Machine). This is the correct architecture for a
real-time safety cap: it computes locally and instantly, on every turn, with
no external call; the nightly Anthropic usage-report ingest (#1660) is the
exact reconciliation for COGS / invoicing.

**Enforcement loop.** This module RECORDS the just-run turn's estimated cents.
A turn already executed cannot be un-run — but once the ladder trips HARD_STOP,
the gate stops the NEXT interactive driver: `_INBOUND_GUARD` parks inbound
webhooks and `/mcp/turn` + `/webhooks/handoff` return 503 (both wired in
webhook_gate.py). So the single threshold-crossing turn leaks (negligible),
and everything after is halted until a Captain clear.

**Estimate model.** Each API call re-sends the whole context as input (minus
caching), so per-turn input ≈ the full conversation size. To avoid
false-tripping a long-but-idle conversation (whose context is mostly
cache-read at ~0.1x), we meter only the NEW content since the last turn (the
delta), priced at the full input rate. A runaway generates genuinely new
tokens every turn, so the delta model still catches it; a chatty-but-static
context does not over-count. Biases slightly conservative (chars/token low,
cache ignored, all-input at full rate) so the cap trips a touch early rather
than late — the safe direction. Known under-count: hidden extended-thinking
tokens aren't in `assistant_response`; the nightly reconciliation covers them.

**Meter-fail posture (Captain decision 2026-07-04).** If the turn cannot be
metered (model absent from the pricing table, unreadable content), KEEP GOING
and raise a loud alarm — a meter glitch must not freeze a customer's Operator,
and the alarm surfaces it in minutes. Never silently pass.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from shared.audit_contract import INSERT_SQL, agent_event_params

logger = logging.getLogger(__name__)

# Conservative English chars-per-token (~3.5 < the ~4 rule of thumb → more
# tokens → higher cost estimate → trips slightly early, the safe direction).
_CHARS_PER_TOKEN = 3.5

_PRICING_PATH = Path(__file__).resolve().parent / "anthropic_pricing.json"

# Per-session cumulative-chars cursor for the delta estimate. In-memory: the
# gate/agent process is long-lived per Machine. On restart the cursors reset,
# so the first turn after a restart meters its full history once (a one-time
# conservative over-estimate per session) — acceptable and safe.
_CURSORS: dict[str, int] = {}

# Meter-fail alarm rate limit: one audit row per (reason) per window, so a
# persistent fault (e.g. an unpriced model) does not spam the ledger.
_ALARM_WINDOW_SECONDS = 300.0
_last_alarm: dict[str, float] = {}


def _load_pricing() -> dict[str, dict[str, int]]:
    try:
        doc = json.loads(_PRICING_PATH.read_text())
        return doc.get("models", {})
    except Exception as exc:  # noqa: BLE001
        logger.error("interactive_cost_meter: pricing table unreadable: %s", exc)
        return {}


_PRICING = _load_pricing()


def _history_chars(conversation_history: Any) -> int:
    if not isinstance(conversation_history, list):
        return 0
    total = 0
    for msg in conversation_history:
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                # content blocks (tool results, multimodal) — sum text parts
                for block in content:
                    if isinstance(block, dict):
                        total += len(str(block.get("text") or block.get("content") or ""))
    return total


def estimate_turn_cents(
    *,
    model: str,
    conversation_history: Any,
    assistant_response: str,
    session_id: str,
    now_fn=time.monotonic,  # unused here; kept for signature symmetry in tests
) -> tuple[int, bool, str | None]:
    """Estimate the just-run turn's cost in cents.

    Returns ``(cents, ok, reason)``. ``ok=False`` (reason set) means the turn
    could not be metered — the caller must alarm and keep going, never record a
    fabricated 0 as if metered. Advances the per-session cursor as a side
    effect (only when ``ok``).
    """
    rates = _PRICING.get(model or "")
    if not rates:
        return 0, False, f"model_unpriced:{model or '<none>'}"

    total_chars = _history_chars(conversation_history)
    prior = _CURSORS.get(session_id, 0)
    new_input_chars = max(0, total_chars - prior)
    out_chars = len(assistant_response or "")

    in_tok = math.ceil(new_input_chars / _CHARS_PER_TOKEN)
    out_tok = math.ceil(out_chars / _CHARS_PER_TOKEN)
    in_rate = int(rates.get("input_per_million_cents", 0))
    out_rate = int(rates.get("output_per_million_cents", 0))
    cents = math.ceil(in_tok * in_rate / 1_000_000 + out_tok * out_rate / 1_000_000)

    _CURSORS[session_id] = total_chars
    return cents, True, None


def _emit_alarm(audit_client: Any, *, reason: str, model: str, session_id: str) -> None:
    """Loud, rate-limited alarm that the cost meter could not run. Best-effort:
    an audit-write failure must not raise out of the hook (we are already in a
    degraded state; log and move on)."""
    key = reason.split(":", 1)[0]
    now = time.monotonic()
    # Absent key => always fire. (Must not default to 0.0: on a fresh process
    # time.monotonic() can be < the window, which would suppress the very first
    # alarm — the failure mode this rate-limiter exists to make visible.)
    last = _last_alarm.get(key)
    if last is not None and now - last < _ALARM_WINDOW_SECONDS:
        return
    _last_alarm[key] = now
    logger.error(
        "interactive_cost_meter: METER UNAVAILABLE (%s) — turn NOT cost-capped "
        "(keep-going per ADR 0062 §4); session=%s model=%s",
        reason,
        session_id,
        model,
    )
    if audit_client is None:
        return
    try:
        params = agent_event_params(
            action_type="INVARIANT_VIOLATION",
            metadata={
                "cost_meter_unavailable": True,
                "reason": reason,
                "model": model,
                "session_id": session_id,
            },
        )
        audit_client.execute(INSERT_SQL, *params)
    except Exception as exc:  # noqa: BLE001
        logger.error("interactive_cost_meter: alarm audit write failed: %s", exc)


def meter_interactive_turn(
    *,
    model: str,
    conversation_history: Any,
    assistant_response: str,
    session_id: str,
    breaker: Any,
    audit_client: Any = None,
) -> None:
    """Meter one completed interactive turn into the cost breaker.

    Called from the audit plugin's ``on_post_llm_call`` (fires once per turn for
    every interactive turn). Records the estimated cents into the shared ladder
    — a HARD_STOP transition emits AGENT_STOPPED through the breaker's own audit
    sink, and the gate halts the next interactive driver. Never raises: a meter
    fault alarms and keeps going (Captain decision 2026-07-04).
    """
    try:
        cents, ok, reason = estimate_turn_cents(
            model=model,
            conversation_history=conversation_history,
            assistant_response=assistant_response,
            session_id=session_id,
        )
        if not ok:
            _emit_alarm(
                audit_client, reason=reason or "unknown", model=model, session_id=session_id
            )
            return
        if cents > 0 and breaker is not None:
            breaker.record_cost_cents(cents)
    except Exception as exc:  # noqa: BLE001 — a meter fault must never break a turn
        _emit_alarm(
            audit_client,
            reason=f"exception:{type(exc).__name__}",
            model=model,
            session_id=session_id,
        )


__all__ = [
    "estimate_turn_cents",
    "meter_interactive_turn",
]
