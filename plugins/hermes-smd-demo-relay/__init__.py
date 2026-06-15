"""hermes-smd-demo-relay — autonomous demo reply WITHOUT weakening any floor.

Attaches to one hook at the pinned Hermes ref (v2026.5.16):

- ``post_tool_call`` (``model_tools.py:826-836``) — fires after every tool
  dispatch. The relay acts only on the AgentMail draft-creation tool, which
  reaches the hook under its live Hermes MCP runtime name
  ``mcp_agentmail_create_draft`` (``mcp_<server>_<tool>``).

What it does (design: ``docs/security/demo-reply-relay-design.md``, ss-console):
the tangible law demo needs the Operator's intake result emailed back to the
prospect who emailed in. Under the law external-send-draft floor
(``enforce.py``) and the taint-gate, the agent on an inbound-tainted turn
DRAFTS the reply — exactly the safe behavior — but cannot autonomously send it.
This relay sends that already-governed draft back to the verified inbound
sender, OUTSIDE the model's governed tool path, with fixed demo-scoped behavior:

  1. **Fail-closed flag, read live.** Acts only when the customer authored
     ``demo.reply_relay: enabled``, re-read from customer.yaml on every call
     (ADR 0044 WS2) so authoring it on/off takes effect on the next draft with
     no restart. Absent / unreadable ⇒ the hook no-ops. A real customer can
     never be regressed: the flag is checked live and they never author it.
  2. **Recipient-lock.** Sends only to the recorded inbound sender
     (``SESSION_INBOUND_ORIGIN``, first-inbound-wins), keyed on the recorded
     inbox + message id — an injected/substituted recipient cannot redirect it.
  3. **Re-applied floors.** Re-runs ``content_floor.classify`` +
     ``outbound_gate.evaluate`` on the draft body before sending (the same
     content/fabrication floors the autonomous-send path would have applied).
  4. **Rate-limit.** Per-sender + global rolling-window bound.
  5. **Audit.** Emits ``DEMO_RELAY_SENT`` on send, ``DEMO_RELAY_BLOCKED`` on a
     refused relay (reason only — never the body), ``DEMO_RELAY_FAILED`` on a
     send error. Digest + recipient + message id only; never the content.

It defeats NO agent floor — the trust gate, taint-gate, content floor, and
fabrication gate are byte-for-byte unchanged. "Autonomous send" lives in this
trusted, demo-scoped code, not in a loosened model capability.

Hook callbacks are exception-safe per AGENTS.md hard rule #3.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from shared import inbound
from shared.audit_client import audit_client_from_env
from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.audit_contract import agent_event_params
from shared.customer_config import CustomerConfig, CustomerConfigError
from shared.secrets import get_secret

from . import relay  # noqa: F401 - surface for tests

logger = logging.getLogger(__name__)


# The tool the relay acts on. AgentMail draft creation is INTERNAL_WRITE
# (shared/action_classes.py) — it passes the taint-gate by design (drafting is
# the safe behavior); the relay turns that governed draft into a sent reply.
#
# Hermes registers MCP tools as ``mcp_<server>_<tool>``, so the live runtime
# name is ``mcp_agentmail_create_draft`` — the ONLY form the agent emits. The
# colon spelling is retained as an accepted alias (capability-contract / tests);
# matching a set keeps the hook firing regardless of which form reaches it. The
# earlier code matched only the colon form, so the hook never fired in
# production and the relay was dead on demo-law (2026-06-12 live test).
_CREATE_DRAFT_TOOLS = frozenset({"mcp_agentmail_create_draft", "agentmail:create_draft"})

_DEFAULT_CUSTOMER_YAML_PATH = "/opt/data/customer.yaml"


# Module-level state — populated by ``register()``. ``_INFRA_READY`` is the
# register-time gate: True only when the relay CAN send (AgentMail key resolved
# and the rate-limiter built). It does NOT mean the relay is authorized — that
# is the live ``demo.reply_relay`` flag, re-read from customer.yaml on every
# call (ADR 0044 WS2). Splitting "can send" (infra, register-bound: env secrets
# + process objects that only change on a restart) from "should send" (flag,
# live) is what lets authoring the flag on/off take effect without a restart
# while keeping the send credential off the hot path.
_INFRA_READY: bool = False
_API_KEY: str | None = None
_CUSTOMER_SLUG: str | None = None
_D1_CLIENT: Any | None = None
_LIMITER: relay.RateLimiter | None = None
_YAML_PATH: Path = Path(_DEFAULT_CUSTOMER_YAML_PATH)


def _emit_relay_event(*, action_type: str, metadata: dict) -> None:
    """Write one demo-relay audit row directly via D1Client (mirror-don't-gate).

    Shares the ``shared.audit_contract`` row shape with the audit plugin so the
    two can never desync. Metadata carries digest + recipient + message id +
    reason ONLY — never the draft body. Best-effort: a failed emission is logged
    and swallowed (the relay's send decision already happened)."""
    if _D1_CLIENT is None or _CUSTOMER_SLUG is None:
        return
    try:
        params = agent_event_params(
            action_type=action_type,
            metadata={"customer": _CUSTOMER_SLUG, "per_demo_relay": True, **metadata},
        )
        _D1_CLIENT.execute(_INSERT_SQL, *params)
    except Exception as exc:  # noqa: BLE001 — audit must never break the hook
        logger.warning("hermes-smd-demo-relay: %s emission failed (%s)", action_type, exc)


def _blocked(reason: str, origin: inbound.InboundOrigin, **extra: Any) -> None:
    _emit_relay_event(
        action_type="DEMO_RELAY_BLOCKED",
        metadata={
            "reason": reason,
            "recipient": origin.sender_address,
            "message_id": origin.message_id,
            **extra,
        },
    )


def on_post_tool_call(**kwargs: Any) -> None:
    """Relay the agent's governed draft back to the verified inbound sender.

    Returns ``None`` always — ``post_tool_call`` cannot block (the draft is
    already created); the relay performs an out-of-band send and never alters
    the tool result. Exception-safe: any failure is logged and swallowed.
    """
    if not _INFRA_READY:
        return
    try:
        if (kwargs.get("tool_name") or "") not in _CREATE_DRAFT_TOOLS:
            return

        # Authorization is read LIVE (ADR 0044 WS2): demo.reply_relay can be
        # authored on/off without a restart, so the relay re-reads it here and
        # acts only if the customer currently authors it. Fail closed if
        # customer.yaml is unreadable — a relay that cannot confirm it is
        # authored never sends.
        try:
            cfg = CustomerConfig.from_volume(str(_YAML_PATH))
        except (CustomerConfigError, OSError) as exc:
            logger.warning(
                "hermes-smd-demo-relay: customer.yaml live-read failed (%s); "
                "not relaying this call",
                exc,
            )
            return
        if not cfg.demo_reply_relay_enabled:
            return
        vertical = cfg.vertical or None

        session_id = kwargs.get("session_id") or ""
        args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}

        origin = inbound.SESSION_INBOUND_ORIGIN.get(session_id)
        if origin is None:
            # Recovery path. The router records the recipient-lock origin under
            # the DISPATCH-time session_id, which can be empty or differ from
            # this agent-loop session_id — so the session-keyed lookup misses
            # even though a verified inbound DID open the work. Recover the
            # origin by matching THIS draft's own recipient against the verified
            # address index. Injection-safe: only Svix-verified inbound senders
            # populate the index, so a draft addressed to someone who never
            # emailed in matches nothing; and the recipient-lock below still
            # enforces that the draft names ONLY the recovered sender.
            recovered = inbound.SESSION_INBOUND_ORIGIN.find_for_recipient(
                relay.draft_recipients(args)
            )
            if recovered is not None:
                logger.info(
                    "hermes-smd-demo-relay: session-keyed origin missed (session=%r); "
                    "recovered verified inbound origin by recipient address",
                    session_id,
                )
                origin = recovered
        if origin is None:
            # Fail closed: no verified inbound sender matches this draft, so
            # there is no address to reply to. A create_draft that did NOT
            # originate from an inbound email never relays.
            return

        # (b) Recipient-lock — the reply can go ONLY to the address that emailed
        # in. An injected extra/substituted recipient fails the lock here.
        if not relay.recipient_locked(args, origin.sender_address):
            _blocked("recipient_mismatch", origin, draft_to=sorted(relay.draft_recipients(args)))
            return
        if not origin.inbox_id:
            # No inbox to thread the reply into — fail closed.
            _blocked("no_inbox_id", origin)
            return

        scan_text, send_text, send_html = relay.draft_body(args)

        # (c) Re-apply the content + fabrication floors to the draft body.
        gate = relay.gate_body(scan_text, vertical=vertical, cohort=_CUSTOMER_SLUG)
        if not gate.allowed:
            _blocked(gate.reason, origin, categories=list(gate.categories))
            return

        if not (send_text or send_html):
            # Subject-only draft — nothing to relay (the gate scans the subject,
            # but a reply transmits only the body). Fail closed.
            _blocked("empty_body", origin)
            return

        # (d) Rate-limit (per-sender + global).
        if _LIMITER is None or not _LIMITER.allow(origin.sender_address):
            _blocked("rate_limited", origin)
            return

        # (e) Send the threaded reply via the AgentMail REST API, keyed on the
        # recorded inbox + message id (structural recipient-lock).
        try:
            sent_id = relay.send_reply(
                api_key=_API_KEY or "",
                inbox_id=origin.inbox_id,
                message_id=origin.message_id,
                text=send_text,
                html=send_html,
            )
        except relay.RelaySendError as exc:
            _emit_relay_event(
                action_type="DEMO_RELAY_FAILED",
                metadata={
                    "reason": str(exc),
                    "recipient": origin.sender_address,
                    "message_id": origin.message_id,
                },
            )
            return

        # (f) Audit the send — digest + recipient + message ids, never the body.
        _emit_relay_event(
            action_type="DEMO_RELAY_SENT",
            metadata={
                "recipient": origin.sender_address,
                "in_reply_to": origin.message_id,
                "inbox_id": origin.inbox_id,
                "sent_message_id": sent_id,
                "body_digest": inbound.content_digest(scan_text),
            },
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-demo-relay: post_tool_call handler error: %s", exc)


def register(ctx) -> None:
    """Plugin entry point. Wires ``post_tool_call``.

    Resolves the send infrastructure at register time — the AgentMail API key,
    the customer slug, the audit binding, the rate-limiter — and sets
    ``_INFRA_READY`` accordingly. The ``demo.reply_relay`` authorization flag is
    deliberately NOT bound here: it is re-read from customer.yaml on every call
    (``on_post_tool_call``) so authoring it on/off takes effect on the next
    draft with no restart (ADR 0044 WS2). The hook is registered unconditionally
    so the plugin set is uniform across customers; it no-ops whenever the relay
    is not infra-ready or the live flag is off.
    """
    global _INFRA_READY, _API_KEY, _CUSTOMER_SLUG, _D1_CLIENT, _LIMITER, _YAML_PATH

    _INFRA_READY = False  # fail closed until the send infra resolves
    _YAML_PATH = Path(os.environ.get("SMD_CUSTOMER_YAML_PATH") or _DEFAULT_CUSTOMER_YAML_PATH)

    # Resolve the send credential UNCONDITIONALLY — do not gate it on the flag.
    # Because the flag is now read live, the relay must be ready to act the
    # instant a customer authors demo.reply_relay on, with no reprovision. A
    # customer without the AgentMail key simply never becomes infra-ready and
    # never relays (the live flag is moot without a credential to send with).
    try:
        _API_KEY = get_secret("AGENTMAIL_API_KEY")
    except KeyError:
        logger.info(
            "hermes-smd-demo-relay: AGENTMAIL_API_KEY unset; relay cannot send "
            "(infra not ready). Hook registered; it no-ops every call."
        )
        ctx.register_hook("post_tool_call", on_post_tool_call)
        return

    try:
        _CUSTOMER_SLUG = get_secret("SMD_CUSTOMER_SLUG")
        # Audit MUST go through the broker-aware factory (OP-P1-4): when the
        # broker is configured this returns a BrokerAuditClient so the agent
        # cannot write its own tamper-resistant ledger directly; otherwise it
        # falls back to a D1 client. Same ``.execute(sql, *params)`` seam.
        _D1_CLIENT = audit_client_from_env(customer_slug=_CUSTOMER_SLUG)
    except KeyError as exc:
        # Audit is observability, not a gate — the relay can still send. Run
        # without it rather than disabling a working demo for a missing binding.
        _CUSTOMER_SLUG = None
        _D1_CLIENT = None
        logger.warning(
            "hermes-smd-demo-relay: audit binding unconfigured (%s); relay will "
            "send without emitting audit rows",
            exc,
        )

    _LIMITER = relay.RateLimiter()
    _INFRA_READY = True
    ctx.register_hook("post_tool_call", on_post_tool_call)
    logger.info(
        "hermes-smd-demo-relay registered (infra_ready=True, customer=%s); "
        "relay gated on the live demo.reply_relay flag",
        _CUSTOMER_SLUG,
    )
