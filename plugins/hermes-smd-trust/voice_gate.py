"""Voice live-gate — ADR 0028 §2, issue #855.

Closes the "primed, not gated" gap. Voice transformation already runs on live
output (``hermes-smd-voice`` ``transform_llm_output``), but nothing ENFORCED it
on the outbound path: an autonomous OUTSIDE send could ship in generic model
prose, not the principal's voice, with no human to catch it. This gate is that
enforcement.

Binding
-------
The gate binds a seat **iff the customer's live config authors a
``voice_library`` block** (voice is part of the engagement). No ``voice_library``
⇒ the gate does not fire (ADR 0035: an unauthored capability is never imposed —
this is not an imposed default). Any failure to positively confirm voice is
authored reads as "not authored" and the gate stays silent; it is not this
gate's job to fail-close a send whose voice-authorship is unknown (the ceiling,
content floor, and fabrication gates govern the send on their own axes).

Firing site (``enforce.evaluate_tool_call``)
--------------------------------------------
Fires on the SAME path as the content floor: only when the resolved decision is
an ALLOWED **outside** ``EXTERNAL_SEND`` whose effective ceiling is
``autonomous``. Draft-for-review / confirm paths have a human in the loop;
``external_send_internal`` is ops traffic to rostered staff, not client-voice
impersonation. Those are deliberately out of scope.

Pass condition (both required)
------------------------------
(a) voice samples are retrievable for this seat right now (the samples probe
    reports ≥1), AND
(b) the voice transform demonstrably ran on this turn's output (the per-turn
    cross-hook mark is set — see ``shared.voice_status``).

Because a successful transform implies samples were retrieved moments earlier,
condition (b) being true implies (a): the mark is the happy-path pass, and the
samples probe is consulted only to distinguish the failure reason. This also
keeps the R2/vault probe off the happy path.

Fail behavior (fail-closed, ADR 0028 §4)
----------------------------------------
Downgrade the send to draft-for-review — using the EXACT same block-directive
plumbing the content floor uses (a ``{"action": "block", ...}`` return that the
agent turns into a draft) — and write a ``VOICE_GATE_TRIGGERED`` audit row
(``reason``: ``no_samples`` | ``transform_not_applied`` | ``gate_error``). ANY
internal error in the gate downgrades. The gate never silently passes.

Overlay-only (like the content floor, ADR 0031 §5). NOT mirrored into
ss-console's ``trust_ceiling.py`` adapter.
"""

import logging
from typing import Any

from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.audit_contract import agent_event_params
from shared.audit_status import NoAuditWarner
from shared.customer_config import CustomerConfig
from shared.voice_status import VOICE_STATUS

logger = logging.getLogger(__name__)


# Fail reasons (audit + the operator-facing draft message).
_REASON_NO_SAMPLES = "no_samples"
_REASON_TRANSFORM_NOT_APPLIED = "transform_not_applied"
_REASON_GATE_ERROR = "gate_error"


# ---------------------------------------------------------------------------
# Binding condition — voice authored for this seat?
# ---------------------------------------------------------------------------


def _voice_authored() -> bool:
    """True iff the seat's live config authors a non-empty ``voice_library`` block.

    The gate BINDS only when voice is positively confirmed part of the
    engagement (ADR 0035 — an unauthored capability is never imposed). Any
    failure to confirm (missing / unreadable config) reads as NOT authored, so
    the gate stays silent rather than imposing a voice downgrade on a seat that
    may not use voice at all.

    This "positively-confirm-or-silent" posture governs BINDING; it is distinct
    from the gate's INTERNAL fail-closed posture (ADR 0028 §4) which, once bound,
    downgrades to draft on any evaluation error. The two are reconcilable because
    the gate only runs after the ceiling resolver ALREADY read the same config
    to resolve an autonomous exposure — a config the resolver read is a config
    this check can read, so the silent path here is not a fail-open hole."""
    try:
        return bool(CustomerConfig.from_volume().voice_library)
    except Exception:  # noqa: BLE001 — unconfirmed ⇒ not authored ⇒ gate silent
        logger.debug(
            "voice gate: voice_library unresolved; treating as not authored (gate silent)",
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Audit emission — VOICE_GATE_TRIGGERED
#
# Mirrors the FABRICATION_FILTER_TRIGGERED writer in ``outbound.py``: one
# audit_log row via the shared D1Client + the canonical audit_log INSERT
# contract, preserving the trust/audit loose coupling (no cross-import of the
# audit plugin's hook surface). Best-effort RELATIVE TO THE DOWNGRADE — a write
# failure logs a warning but the draft downgrade still stands.
# ---------------------------------------------------------------------------


_AUDIT_CLIENT: Any = None
_AUDIT_CUSTOMER_SLUG: str | None = None
_AUDIT_WIRED: bool = False

_NO_AUDIT_WARNER = NoAuditWarner()


def _audit_client() -> tuple[Any, str | None]:
    """Lazily resolve ``(D1Client, customer_slug)``. Cached across calls.

    Returns ``(None, None)`` when the audit env is not configured — the gate
    still downgrades; the row is skipped (rate-limited WARNING per skip, #64).
    Tests reset the cache by restoring the module globals."""
    global _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG, _AUDIT_WIRED
    if _AUDIT_WIRED:
        return _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG
    _AUDIT_WIRED = True
    try:
        from shared.audit_client import audit_client_from_env
        from shared.secrets import require

        secrets_map = require("SMD_CUSTOMER_SLUG", "SMD_D1_AUDIT_BINDING")
        slug = secrets_map["SMD_CUSTOMER_SLUG"]
        _AUDIT_CLIENT = audit_client_from_env(customer_slug=slug)
        _AUDIT_CUSTOMER_SLUG = slug
    except Exception as exc:  # noqa: BLE001 — audit is best-effort vs the downgrade
        logger.debug("voice gate: audit client unconfigured (%s); downgrades won't emit a row", exc)
        _AUDIT_CLIENT = None
        _AUDIT_CUSTOMER_SLUG = None
    return _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG


def _emit_voice_gate_audit(
    *,
    tool_name: str,
    reason: str,
    session_id: str,
    tool_call_id: str,
) -> None:
    """Write one ``VOICE_GATE_TRIGGERED`` row. Best-effort, never raises.

    Provenance only — never the draft body. Carries the fail ``reason`` so the
    audit review can tell a no-samples seat from a transform-skipped turn."""
    client, slug = _audit_client()
    if client is None or slug is None:
        _NO_AUDIT_WARNER.warn(
            logger, f"VOICE_GATE_TRIGGERED on tool={tool_name} (reason={reason}) not recorded"
        )
        return
    try:
        metadata: dict = {
            "voice_gate": True,
            "customer": slug,
            "tool": tool_name,
            "reason": reason,
        }
        if session_id:
            metadata["session_id"] = session_id
        if tool_call_id:
            metadata["tool_call_id"] = tool_call_id
        params = agent_event_params(action_type="VOICE_GATE_TRIGGERED", metadata=metadata)
        client.execute(_INSERT_SQL, *params)
    except Exception as exc:  # noqa: BLE001 — audit row is best-effort vs downgrade
        logger.warning(
            "voice gate: VOICE_GATE_TRIGGERED emission failed (tool=%s reason=%s err=%s); "
            "the draft downgrade still stands",
            tool_name,
            reason,
            exc,
        )


# ---------------------------------------------------------------------------
# The gate entry point — called from enforce.evaluate_tool_call
# ---------------------------------------------------------------------------


def _draft_message(reason: str) -> str:
    return (
        "Refused: this autonomous send is not certified to be in the principal's "
        f"authored voice (voice live-gate reason={reason}); routing to draft for "
        "human review instead of autonomous send (ADR 0028 §2). Create a draft instead."
    )


def check_voice_gate(
    *,
    tool_name: str,
    session_id: str = "",
    tool_call_id: str = "",
) -> dict | None:
    """Voice live-gate on an allowed autonomous OUTSIDE send.

    The caller (``enforce.evaluate_tool_call``) invokes this ONLY when the
    decision is an allowed ``EXTERNAL_SEND`` whose effective ceiling is
    ``autonomous`` — this function adds the voice-authored BINDING check and the
    pass/fail evaluation. Returns a draft-routing block directive
    ``{"action": "block", "message": ...}`` to downgrade, or ``None`` to let the
    autonomous send proceed.

    Fail-closed (ADR 0028 §4): a bound seat that cannot certify voice ran this
    turn downgrades; any internal error downgrades."""
    if not _voice_authored():
        # Not bound for this seat — voice is not part of the engagement.
        return None

    try:
        if VOICE_STATUS.was_applied(session_id):
            # The transform demonstrably reshaped this turn's output. A successful
            # transform implies samples were retrieved, so both pass conditions hold.
            return None
        # The transform did NOT apply this turn. Distinguish the reason so the
        # audit review separates a no-samples seat from a transform-skipped turn.
        reason = (
            _REASON_TRANSFORM_NOT_APPLIED
            if VOICE_STATUS.samples_available()
            else _REASON_NO_SAMPLES
        )
    except Exception:  # noqa: BLE001 — a bound-seat evaluation fault fails closed
        logger.exception(
            "voice gate: bound-seat evaluation failed for %s; failing toward draft (ADR 0028 §4)",
            tool_name,
        )
        reason = _REASON_GATE_ERROR

    _emit_voice_gate_audit(
        tool_name=tool_name,
        reason=reason,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    return {"action": "block", "message": _draft_message(reason)}


__all__ = ["check_voice_gate"]
