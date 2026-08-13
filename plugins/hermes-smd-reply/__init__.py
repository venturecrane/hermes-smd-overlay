"""hermes-smd-reply — the Operator replies to a colleague WITHOUT weakening any floor.

The Operator is an employee (ADR 0055). When someone on the organization roster
emails its inbox, the employee reads, drafts a governed reply, and — because the
sender is a colleague on the roster — actually sends it. This plugin is that
last step: it relays the agent's already-governed draft back to the verified
inbound sender, OUTSIDE the model's governed tool path, with fixed behavior.

Attaches to one hook at the pinned Hermes ref (v2026.5.16):

- ``post_tool_call`` (``model_tools.py:826-836``) — fires after every tool
  dispatch. The relay acts only on the AgentMail draft-creation tool, which
  reaches the hook under its live Hermes MCP runtime name
  ``mcp_agentmail_create_draft`` (``mcp_<server>_<tool>``).

What it does:

  1. **Roster authorization, read live.** Sends only when the verified inbound
     sender is on the organization roster (``scope.inbound_allow_from``), re-read
     from customer.yaml on every call (ADR 0044) so authoring the roster takes
     effect on the next draft with no restart. Empty / unauthored roster ⇒ the
     hook no-ops (fail-closed: the employee drafts, does not autonomously reply
     to a stranger).
  2. **Recipient-lock.** Sends only to the recorded inbound sender
     (``SESSION_INBOUND_ORIGIN``, first-inbound-wins), keyed on the recorded
     inbox + message id — an injected/substituted recipient cannot redirect it.
     Roster membership authorizes; the recipient-lock structurally bounds.
  3. **Re-applied floors, recipient-aware (ADR 0072 / ss #1932).** Re-runs the
     same floors the autonomous-send path would have applied: the fabrication
     gate (``outbound_gate.evaluate``) on EVERY reply, and the content floor
     (``content_floor.classify``) only when the locked recipient does NOT
     classify INTERNAL under ``recipient_classifier`` (the send path
     deliberately does not content-floor internal sends — firm coordination
     legitimately names deadlines, signatures, attorneys).
  4. **Matter identity (ss#2167).** Holds a reply that cites a matter the locked
     recipient is provably not a party to. This check has to live HERE: it sits
     in ``enforce.evaluate_tool_call`` behind ``is_send``, which is true only for
     EXTERNAL_SEND* classes, and the tool this lane calls is ``create_draft`` —
     INTERNAL_WRITE. So the matter gate never ran on this path while this
     function relayed the draft out as real email
     (vfy_01KZRRW066Y70TFEYKGQX6ME76). Note the exemption is NOT
     ``recipient_class is INTERNAL``: an inbound-roster match classifies INTERNAL
     before the typed roster is consulted, so that spelling would exempt 100% of
     this lane. See the comment at the call site.
  5. **Rate-limit.** Per-sender + global rolling-window bound.
  6. **Audit.** Emits ``REPLY_SENT`` on send, ``REPLY_HELD`` on a reply held
     back to draft (reason only — never the body), ``REPLY_FAILED`` on a send
     error, ``MATTER_UNRESOLVED`` when membership could be neither confirmed nor
     denied. Digest + recipient + message id only; never the content.

It defeats NO agent floor — the trust gate, taint-gate, content floor, and
fabrication gate are byte-for-byte unchanged. "Autonomous reply" lives in this
trusted code, not in a loosened model capability. Reaching anyone OUTSIDE the
roster is not this plugin's job; that requires explicit authorization and goes
through the model's governed (drafting) path (ADR 0055 §3).

Hook callbacks are exception-safe per AGENTS.md hard rule #3.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from shared import inbound, matter_gate, msgraph_broker, provenance, send_policy
from shared.audit_client import audit_client_from_env
from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.audit_contract import agent_event_params
from shared.customer_config import CustomerConfig, CustomerConfigError
from shared.recipient_classifier import RecipientClass, classify_recipients_typed
from shared.secrets import get_secret

from . import (
    held_store,
    relay,  # noqa: F401 - surface for tests
    sweeper,
)

logger = logging.getLogger(__name__)


# The tools the relay acts on. Draft creation is INTERNAL_WRITE
# (shared/action_classes.py) — it passes the taint-gate by design (drafting is
# the safe behavior); the relay turns that governed draft into a sent reply. Both
# email adapters (ADR 0078) surface draft creation, so both trigger the relay;
# the actual transport is chosen per-seat by the Email adapter below.
#
# Hermes registers MCP tools as ``mcp_<server>_<tool>``, so the live runtime
# names are ``mcp_agentmail_create_draft`` / ``mcp_msgraph_mail_create_draft`` —
# the ONLY forms the agent emits. The colon spelling is retained as an accepted
# alias (capability-contract / tests).
_CREATE_DRAFT_TOOLS = frozenset(
    {"mcp_agentmail_create_draft", "agentmail:create_draft", "mcp_msgraph_mail_create_draft"}
)

# Email adapters (customer.yaml connectors.Email.adapter, ADR 0078). The relay is
# provider-neutral: roster/recipient-lock/floors/rate-limit are shared; only the
# transport differs. msgraph replies in-thread via Graph; agentmail via REST.
_ADAPTER_MSGRAPH = "msgraph"


def _email_adapter(cfg: CustomerConfig) -> str:
    """The seat's Email connector adapter (``agentmail`` default). Read live so a
    seat that swaps providers is dispatched correctly without a code change."""
    try:
        record = cfg.connectors.get("Email")
    except CustomerConfigError:
        return "agentmail"
    if isinstance(record, dict):
        adapter = record.get("adapter")
        if isinstance(adapter, str) and adapter:
            return adapter
    return "agentmail"


_DEFAULT_CUSTOMER_YAML_PATH = "/opt/data/customer.yaml"


# Module-level state — populated by ``register()``. ``_INFRA_READY`` is the
# register-time gate: True only when the relay CAN send (AgentMail key resolved
# and the rate-limiter built). It does NOT mean a given reply is authorized —
# that is the live roster check, re-read from customer.yaml on every call
# (ADR 0044). Splitting "can send" (infra, register-bound: env secrets + process
# objects that only change on a restart) from "may reply" (roster, live) is what
# lets authoring the roster take effect without a restart while keeping the send
# credential off the hot path.
_INFRA_READY: bool = False
_CUSTOMER_SLUG: str | None = None
_D1_CLIENT: Any | None = None
_LIMITER: relay.RateLimiter | None = None
_YAML_PATH: Path = Path(_DEFAULT_CUSTOMER_YAML_PATH)
# One reply per inbound message id. Process-wide and register-independent: the
# invariant holds for every reply this Machine transmits, and a bounded ring
# keeps it cheap. Not a rate limit — the send policy owns pacing; this owns
# "the same email is never answered twice".
_REPLIED: relay.RepliedOnce = relay.RepliedOnce()
# Held-reply auto-release (#2070). The store is register-time infra; whether
# release is ON is a live per-call/per-pass question (send_policy), so the
# sweeper thread starts unconditionally and no-ops while release is unauthored.
_HELD_STORE: held_store.HeldReplyStore | None = None
_SWEEPER: Any | None = None


def _send_msgraph_reply(graph_message_id: str, comment: str) -> str:
    """Reply in-thread via Microsoft Graph, keyed on the recorded message id.

    Graph derives the recipients from the original message (POST
    /messages/{id}/reply), so the reply is structurally locked to the inbound
    thread — the same recipient-lock property the AgentMail transport has.

    ss#2258: this goes through the broker now. The lock above says the reply
    cannot be REDIRECTED; it says nothing about whether this seat should be
    answering that sender at all, and anyone on the internet can email the
    operator mailbox. So the broker re-fetches the source message itself and
    checks its sender against ``inbound_allow_from`` before replying — a check
    this process cannot make credibly, since a caller that names the sender can
    name any sender. It also writes the audit row, so a reply that left no trace
    is no longer reachable.

    Fail-closed as before: no broker path raises :class:`RelaySendError` (audited
    REPLY_FAILED) and NEVER falls back to another transport. Graph returns 202
    with no id, so a placeholder is surfaced for the audit row."""
    try:
        msgraph_broker.send_reply(graph_message_id, comment)
    except msgraph_broker.BrokerError as exc:
        raise relay.RelaySendError(f"broker refused the msgraph reply: {exc}") from exc
    except msgraph_broker.MsGraphBrokerUnavailable as exc:
        raise relay.RelaySendError(f"msgraph reply unavailable: {exc}") from exc
    return "(sent via msgraph, id unavailable)"


def _emit_reply_event(*, action_type: str, metadata: dict) -> None:
    """Write one reply-channel audit row directly via D1Client (mirror-don't-gate).

    Shares the ``shared.audit_contract`` row shape with the audit plugin so the
    two can never desync. Metadata carries digest + recipient + message id +
    reason ONLY — never the draft body. Best-effort: a failed emission is logged
    and swallowed (the reply decision already happened)."""
    if _D1_CLIENT is None or _CUSTOMER_SLUG is None:
        return
    try:
        params = agent_event_params(
            action_type=action_type,
            metadata={"customer": _CUSTOMER_SLUG, "reply_channel": True, **metadata},
        )
        _D1_CLIENT.execute(_INSERT_SQL, *params)
    except Exception as exc:  # noqa: BLE001 — audit must never break the hook
        logger.warning("hermes-smd-reply: %s emission failed (%s)", action_type, exc)


def _note_fallback_resolution() -> None:
    """Tag Sentry when the relay resolves an origin by address (#195 alarm).

    Carries no identifiers — the signal is the RATE, not the instance.
    """
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("origin_resolution", "fallback_by_address")
            sentry_sdk.capture_message("reply origin resolved by address fallback", level="warning")
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks the hook
        logger.debug("hermes-smd-reply: fallback telemetry skipped (%s)", exc)


def _held(reason: str, origin: inbound.InboundOrigin, **extra: Any) -> None:
    _emit_reply_event(
        action_type="REPLY_HELD",
        metadata={
            "reason": reason,
            "recipient": origin.sender_address,
            "message_id": origin.message_id,
            **extra,
        },
    )


def _notify_hold(
    *,
    reason: str,
    sender: str,
    sender_class: str,
    adapter: str,
    message_id: str,
    body_digest: str,
    pending: int | None = None,
) -> None:
    """Report a held/expired reply to Sentry — silence is never the failure mode.

    Ops-only by design (#2070): the audit row is the record, this is the page.
    Never carries the body or the raw address — the recipient rides as a digest
    prefix, enough to correlate two events as the same person without putting a
    client address in the monitoring stream.
    """
    try:
        import hashlib

        import sentry_sdk

        digest = hashlib.sha256(sender.encode()).hexdigest()[:8] if sender else ""
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("reason", reason)
            scope.set_tag("sender_class", sender_class or "unclassified")
            scope.set_tag("adapter", adapter)
            scope.set_extra("message_id", message_id)
            scope.set_extra("body_digest", body_digest)
            scope.set_extra("recipient_digest", digest)
            if pending is not None:
                scope.set_extra("pending_count", pending)
            sentry_sdk.capture_message(f"reply_held: {reason}", level="warning")
    except Exception as exc:  # noqa: BLE001 — monitoring must never break the hook
        logger.debug("hermes-smd-reply: hold notification skipped (%s)", exc)


def _held_pending_for(sender: str, policy: send_policy.SendPolicy) -> bool:
    """True iff this sender has a reply already waiting for release."""
    if not policy.held_release_enabled or _HELD_STORE is None:
        return False
    try:
        return _HELD_STORE.has_pending(sender)
    except Exception as exc:  # noqa: BLE001 — a broken store never blocks a send
        logger.warning("hermes-smd-reply: held-reply pending check failed (%s)", exc)
        return False


def _pending_count() -> int | None:
    if _HELD_STORE is None:
        return None
    try:
        return _HELD_STORE.pending_count()
    except Exception:  # noqa: BLE001
        return None


def _enqueue_hold(
    *,
    reason: str,
    origin: inbound.InboundOrigin,
    sender_class: str,
    adapter: str,
    send_text: str,
    send_html: str,
    body_digest: str,
    policy: send_policy.SendPolicy,
) -> bool:
    """Persist a rate-held reply for auto-release. True iff it was stored.

    Only mechanical holds reach here (the caller decides): a rate window or a
    queued-behind-held ordering hold clears with time, so the reply is still
    wanted. Semantic refusals are decisions and stay dropped.
    """
    if not policy.held_release_enabled or _HELD_STORE is None:
        return False
    try:
        _HELD_STORE.enqueue(
            sender=origin.sender_address,
            sender_class=sender_class,
            adapter=adapter,
            inbox_id=origin.inbox_id,
            message_id=origin.message_id,
            send_text=send_text,
            send_html=send_html,
            body_digest=body_digest,
            hold_reason=reason,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — a broken store degrades to drop
        logger.warning("hermes-smd-reply: held-reply enqueue failed (%s)", exc)
        return False


def on_post_tool_call(**kwargs: Any) -> None:
    """Relay the agent's governed draft back to a verified, rostered inbound sender.

    Returns ``None`` always — ``post_tool_call`` cannot block (the draft is
    already created); the relay performs an out-of-band send and never alters
    the tool result. Exception-safe: any failure is logged and swallowed.
    """
    if not _INFRA_READY:
        return
    try:
        if (kwargs.get("tool_name") or "") not in _CREATE_DRAFT_TOOLS:
            return

        # (0) The draft must actually exist. ``post_tool_call`` fires after every
        # dispatch, including one that returned an error, and the relay used to
        # act on the tool NAME alone — so a create_draft that failed still put a
        # real email in the sender's inbox, and the agent's retry sent the same
        # answer again (leg-1 turn 2, vfy_01KYTG0B88R3B5K0D7FKPACRZT). Reply
        # transmission is now strictly downstream of a draft the tool confirmed.
        if relay.draft_call_failed(
            result=kwargs.get("result"),
            status=kwargs.get("status"),
            error_type=kwargs.get("error_type"),
        ):
            logger.info(
                "hermes-smd-reply: draft tool call reported failure; not relaying "
                "(the agent's retry, if any, relays instead)"
            )
            return

        # Config is read LIVE (ADR 0044): the roster can be authored without a
        # restart, so the relay re-reads it here. Fail closed if customer.yaml is
        # unreadable — a relay that cannot confirm the roster never sends.
        try:
            cfg = CustomerConfig.from_volume(str(_YAML_PATH))
        except (CustomerConfigError, OSError) as exc:
            logger.warning(
                "hermes-smd-reply: customer.yaml live-read failed (%s); not replying this call",
                exc,
            )
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
                # LAST RESORT since #195: the inbound plugin binds session ->
                # origin by the inbound's unique message id at pre_llm_call, so
                # the session-keyed lookup above should hit on every email turn.
                # This path is address-keyed and most-recent-wins, which is
                # exactly how concurrent messages from one person got their
                # replies crossed. Reaching it means the binding did not happen
                # (template drift, a non-email path, a parse miss) — so it is
                # reported, not silently taken. A nonzero rate here is the
                # regression alarm; without it the burst failure could only be
                # rediscovered by a client.
                logger.warning(
                    "hermes-smd-reply: session-keyed origin missed (session=%r); "
                    "recovered by recipient address — origin_resolution=fallback_by_address "
                    "(expected ~never since #195)",
                    session_id,
                )
                _note_fallback_resolution()
                origin = recovered
        if origin is None:
            # Fail closed: no verified inbound sender matches this draft, so
            # there is no address to reply to. A create_draft that did NOT
            # originate from an inbound email never relays.
            return

        # (0b) One inbound message, at most one reply out. The hook fires per
        # tool CALL, so an agent that drafts twice in a turn arrives here twice
        # for the SAME email. The (0) outcome check catches the retry-after-error
        # shape; this catches every other way a second draft could reach the
        # transport, including shapes no result envelope would reveal. Committed
        # only on a real send or a durable enqueue, so a gated reply stays
        # deliverable by the release path.
        if _REPLIED.committed(origin.message_id):
            _held("duplicate_reply", origin)
            return

        # (a) Roster authorization — the Operator replies autonomously only to a
        # colleague on the organization roster (ADR 0055). A verified inbound
        # sender NOT on the roster gets a drafted (not sent) reply: reaching
        # outside the roster needs explicit authorization. Held, not an error.
        if not cfg.sender_on_roster(origin.sender_address):
            _held("sender_not_on_roster", origin)
            return

        # (b) Recipient-lock — the reply can go ONLY to the address that emailed
        # in. An injected extra/substituted recipient fails the lock here.
        if not relay.recipient_locked(args, origin.sender_address):
            _held("recipient_mismatch", origin, draft_to=sorted(relay.draft_recipients(args)))
            return
        if not origin.inbox_id:
            # No inbox to thread the reply into — fail closed.
            _held("no_inbox_id", origin)
            return

        scan_text, send_text, send_html = relay.draft_body(args)

        # (c) Re-apply the outbound floors to the draft body. ADR 0072 carve-out
        # (ss #1932): the send path deliberately does NOT content-floor a send
        # whose recipients all classify INTERNAL (enforce.py evaluate_tool_call —
        # firm-internal coordination legitimately names deadlines, signatures,
        # attorneys; flooring it held ack confirmations in drafts). Mirror it
        # with the SAME classifier and the SAME rosters the send path resolves.
        #
        # INTERNAL is an AUTHORED fact as of ss#2263, not one inferred from the
        # reply list. Before that split this line exempted every relayed reply by
        # construction — including a reply to the firm's own client, whom the firm
        # had put on `inbound_allow_from` for the sole purpose of enabling replies.
        # The floor is the larger of the two exposures the split moves (the matter
        # gate is the other); they move together, in one release, because they
        # read this one `recipient_class`.
        # ``from_tainted`` stays False deliberately: taint guards MODEL-CHOSEN
        # recipients, but this recipient is structurally pinned to the
        # Svix-verified inbound sender by the recipient-lock above — an injected
        # body cannot redirect the reply. The fabrication gate still applies to
        # every reply. Classification faults fail toward floored, never open.
        try:
            recipient_class = classify_recipients_typed(
                [origin.sender_address], cfg.inbound_roster, cfg.outbound_roster
            )
        except Exception:  # noqa: BLE001 — unclassifiable recipient keeps the floor
            logger.exception(
                "hermes-smd-reply: recipient classification raised; keeping the content floor"
            )
            recipient_class = None
        internal = recipient_class is RecipientClass.INTERNAL
        # Provenance exemption — the SAME one the drafting path applies
        # (hermes-smd-trust/outbound.py). Case captions the agent READ from a
        # system of record this session are quotable; without them the two
        # channels disagreed, and the trust gate's approval of a draft naming
        # matters read from Smokeball was overturned here as
        # fabrication:tier2_citation, leaving the sender with silence. A fault
        # degrades to the empty register — no exemption, today's behaviour.
        try:
            allowed_captions = provenance.register_for(
                provenance.resolve_session(session_id)
            ).captions()
        except Exception:  # noqa: BLE001 — an unreadable register grants nothing
            logger.debug(
                "hermes-smd-reply: provenance register unavailable; no caption exemption",
                exc_info=True,
            )
            allowed_captions = frozenset()
        gate = relay.gate_body(
            scan_text,
            vertical=vertical,
            cohort=_CUSTOMER_SLUG,
            internal_recipient=internal,
            allowed_case_names=allowed_captions,
        )
        if not gate.allowed:
            _held(gate.reason, origin, categories=list(gate.categories))
            return

        if not (send_text or send_html):
            # Subject-only draft — nothing to relay (the gate scans the subject,
            # but a reply transmits only the body). Fail closed.
            _held("empty_body", origin)
            return

        # (c2) Matter identity — is this recipient a party to the matter this
        # reply is about? (ss#2167)
        #
        # WHY IT HAS TO BE HERE AND NOT IN THE TRUST GATE. The matter check lives
        # in enforce.evaluate_tool_call, guarded by `is_send`, which is true only
        # for EXTERNAL_SEND* action classes. The tool this lane calls is
        # ``create_draft`` — INTERNAL_WRITE (shared/action_classes.py) — and
        # ``_reclassify_send`` returns a non-EXTERNAL_SEND base class unchanged.
        # So on the reply lane the matter gate never ran at all, while THIS
        # function relayed the same draft out as a real email a few lines below.
        # That was 86 of the pilot's replies, and ~74% of all sends, with no
        # matter-identity check of any kind (vfy_01KZRRW066Y70TFEYKGQX6ME76).
        # It was recorded on the issue as the gate being *blind* here; it was
        # absent, which is why widening membership capture alone would have
        # changed nothing and reported the lane closed.
        #
        # The recipient is structurally pinned to the verified inbound sender by
        # the recipient-lock at (b), so `origin.sender_address` IS the recipient
        # set — no model-chosen address participates.
        # The exemption used to be unable to say `recipient_class is INTERNAL` on
        # this lane, and getting that wrong would have shipped a control that can
        # never fire. `sender_on_roster` above IS `scope.inbound_allow_from`, and
        # `_classify_one_typed` USED TO return INTERNAL on an inbound-roster match
        # BEFORE consulting the typed roster ("a rostered internal recipient
        # outranks a typed class"). So every relayed reply classified INTERNAL by
        # construction, and an INTERNAL exemption would have skipped 100% of this
        # lane.
        #
        # That conflation is fixed at the source as of ss#2263, and this call site
        # got simpler because of it. `inbound_allow_from` answers "may I reply to
        # you"; `scope.outbound_roster` answers "what are you to this firm", and
        # `_classify_one_typed` now reads the typed roster FIRST. So the single
        # `recipient_class` computed for the content floor above is already the
        # right input here: a reply-authorized address the firm typed as a client
        # classifies CLIENT — floored AND gated — instead of INTERNAL.
        #
        # This block used to reclassify against an EMPTY internal roster to route
        # around the old precedence. That workaround is deleted rather than kept:
        # it could only ever have fired for a config the validators rejected
        # ("a recipient cannot be both internal and a typed outbound class"), so
        # it protected nothing, and leaving two classifications on one path is how
        # the floor and the gate drift apart again. One classifier, one verdict.
        #
        # A classification FAULT above leaves `recipient_class` None, which is in
        # neither exempt class, so the gate RUNS. A classification we could not
        # perform is not evidence that this recipient is firm staff.
        matter_verdict = matter_gate.evaluate(
            session_id=session_id,
            body=scan_text,
            recipients={origin.sender_address},
            # Firm staff and records vendors are not expected to be parties
            # (ADR 0072, the same carve-out enforce.py applies).
            recipient_is_exempt=recipient_class in (RecipientClass.INTERNAL, RecipientClass.VENDOR),
        )
        if matter_verdict.should_withhold and matter_gate.mode() == "block":
            _held(
                "matter_mismatch",
                origin,
                matters=list(matter_verdict.matters),
                detail=matter_verdict.reason,
            )
            return
        if matter_verdict.status == "unresolved" and matter_verdict.matters:
            # Recorded, NOT held (Captain call, 2026-08-11). A reply citing a
            # matter whose party list this turn never read is the common case
            # today — get_matter fires on 8 of 86 reply turns — so holding it
            # would withhold correct client replies at a rate nobody has
            # measured, and a control that blocks correct work gets removed
            # rather than fixed. This row is that measurement.
            _emit_reply_event(
                action_type="MATTER_UNRESOLVED",
                metadata={
                    "recipient": origin.sender_address,
                    "message_id": origin.message_id,
                    "matters": list(matter_verdict.matters),
                    "detail": matter_verdict.reason,
                },
            )

        # (d) Rate-limit under the authored send policy (#2070). Resolved LIVE
        # per call (ADR 0044): authoring `send_policy` — e.g. exempting rostered
        # INTERNAL senders so a sustained dialogue never rate-holds — applies on
        # the next reply without a restart. Unauthored/malformed resolves to the
        # platform defaults (today's exact caps), never fail-open. INTERNAL
        # classification comes from step (c)'s classifier — the same rosters the
        # content floor trusts.
        policy = send_policy.live_send_policy(str(_YAML_PATH))
        sender_class = recipient_class.value if recipient_class else "unclassified"
        adapter = _email_adapter(cfg)
        body_digest = inbound.content_digest(scan_text)
        if _LIMITER is None:
            _held("rate_limited", origin)
            return

        # Ordering guard (#2070): if this sender ALREADY has a reply waiting for
        # release, this one queues behind it — otherwise a later reply whose
        # window has cleared would overtake the earlier held one (the client
        # reads answer 5 before answer 4), and under sustained traffic the live
        # path would keep eating the freed slots so the held row never releases.
        if _held_pending_for(origin.sender_address, policy):
            decision = relay.RateDecision(False, "queued_behind_held")
        else:
            decision = _LIMITER.check(origin.sender_address, internal=internal, policy=policy)
        if not decision.allowed:
            reason = decision.reason or "rate_limited"
            enqueued = _enqueue_hold(
                reason=reason,
                origin=origin,
                sender_class=sender_class,
                adapter=adapter,
                send_text=send_text,
                send_html=send_html,
                body_digest=body_digest,
                policy=policy,
            )
            if enqueued:
                # Durably queued for release — the reply for this inbound is
                # committed. Without this a retry in the same turn would enqueue
                # a SECOND row and the sweeper would deliver the answer twice.
                _REPLIED.commit(origin.message_id)
            _held(
                reason,
                origin,
                sender_class=sender_class,
                held_for_release=enqueued,
            )
            _notify_hold(
                reason=reason,
                sender=origin.sender_address,
                sender_class=sender_class,
                adapter=adapter,
                message_id=origin.message_id,
                body_digest=body_digest,
                pending=_pending_count(),
            )
            return

        # (e) Send the threaded reply, keyed on the recorded message id (structural
        # recipient-lock), via the seat's Email transport. Provider dispatch (ADR
        # 0078): msgraph replies in-thread through Graph (Graph derives the
        # recipients from the original message id — the reply cannot be
        # redirected); agentmail via its REST reply endpoint. Fail-closed: a
        # msgraph seat with no MSGRAPH_* creds REFUSES (audited REPLY_FAILED),
        # never falls back to AgentMail.
        try:
            if adapter == _ADAPTER_MSGRAPH:
                sent_id = _send_msgraph_reply(origin.message_id, send_text or send_html)
            else:
                sent_id = relay.send_reply(
                    message_id=origin.message_id,
                    text=send_text,
                    html=send_html,
                )
        except relay.RelaySendError as exc:
            _emit_reply_event(
                action_type="REPLY_FAILED",
                metadata={
                    "reason": str(exc),
                    "adapter": adapter,
                    "recipient": origin.sender_address,
                    "message_id": origin.message_id,
                },
            )
            return

        # (f) The reply for this inbound is now committed — record it before the
        # audit emission so a retry cannot race between the send and the mark.
        _REPLIED.commit(origin.message_id)

        # (g) Audit the send — digest + recipient + message ids, never the body.
        _emit_reply_event(
            action_type="REPLY_SENT",
            metadata={
                "recipient": origin.sender_address,
                "recipient_class": recipient_class.value if recipient_class else "unclassified",
                "content_floor_applied": not internal,
                "adapter": adapter,
                "in_reply_to": origin.message_id,
                "inbox_id": origin.inbox_id,
                "sent_message_id": sent_id,
                "body_digest": inbound.content_digest(scan_text),
            },
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-reply: post_tool_call handler error: %s", exc)


def _release_send(row: held_store.HeldReply) -> str:
    """Transmit one released reply through the same transports the live path uses."""
    if row.adapter == _ADAPTER_MSGRAPH:
        return _send_msgraph_reply(row.message_id, row.send_text or row.send_html)
    return relay.send_reply(
        message_id=row.message_id,
        text=row.send_text,
        html=row.send_html,
    )


def _sender_is_internal(sender: str) -> bool:
    """Re-apply the live roster classification at release time.

    Read fresh (ADR 0044): a sender removed from the roster between hold and
    release loses the exemption, and one added gains it — the release decision
    is never made against a stale snapshot.
    """
    try:
        cfg = CustomerConfig.from_volume(str(_YAML_PATH))
        return (
            classify_recipients_typed([sender], cfg.inbound_roster, cfg.outbound_roster)
            is RecipientClass.INTERNAL
        )
    except Exception:  # noqa: BLE001 — unclassifiable is never exempt (fail closed)
        return False


def _sweep_once() -> sweeper.SweepResult:
    """One release pass, with the policy resolved live."""
    if _HELD_STORE is None or _LIMITER is None:
        return sweeper.SweepResult()
    return sweeper.run_sweep_once(
        store=_HELD_STORE,
        limiter=_LIMITER,
        policy=send_policy.live_send_policy(str(_YAML_PATH)),
        send_fn=_release_send,
        emit_fn=_emit_reply_event,
        notify_fn=_notify_hold,
        internal_senders=_sender_is_internal,
    )


def _start_held_release() -> None:
    """Open the held-reply store and start the sweeper (#2070).

    Guarded end to end: if the store cannot be opened or the thread cannot
    start, the relay degrades to the pre-#2070 behavior (rate holds are audited
    and dropped) rather than failing registration — a broken release path must
    never take the reply channel down with it.
    """
    global _HELD_STORE, _SWEEPER

    _HELD_STORE = None
    _SWEEPER = None
    try:
        store = held_store.HeldReplyStore(
            os.environ.get("SMD_HELD_REPLY_DB_PATH") or held_store.DEFAULT_HELD_DB_PATH
        )
        interrupted = store.fail_interrupted_on_boot()
        _HELD_STORE = store
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes-smd-reply: held-reply store unavailable (%s); rate holds will drop", exc
        )
        return

    if interrupted:
        # A reply was mid-transmit when the process died. It is NOT resent (the
        # send may have completed) — report it so a lost release is visible.
        logger.warning(
            "hermes-smd-reply: %d held repl(ies) were interrupted mid-send and will "
            "not be auto-resent (rows %s)",
            len(interrupted),
            interrupted,
        )
        _notify_hold(
            reason="hold_interrupted",
            sender="",
            sender_class="unclassified",
            adapter="unknown",
            message_id="",
            body_digest="",
            pending=len(interrupted),
        )

    try:
        _SWEEPER = sweeper.start_sweeper_thread(sweep=_sweep_once)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-smd-reply: held-reply sweeper failed to start (%s)", exc)


def register(ctx) -> None:
    """Plugin entry point. Wires ``post_tool_call``.

    Resolves the send infrastructure at register time — the AgentMail API key,
    the customer slug, the audit binding, the rate-limiter — and sets
    ``_INFRA_READY`` accordingly. The roster authorization is deliberately NOT
    bound here: it is re-read from customer.yaml on every call
    (``on_post_tool_call``) so authoring the roster takes effect on the next
    draft with no restart (ADR 0044). The hook is registered unconditionally so
    the plugin set is uniform across customers; it no-ops whenever the relay is
    not infra-ready or the sender is not on the live roster.
    """
    global _INFRA_READY, _CUSTOMER_SLUG, _D1_CLIENT, _LIMITER, _YAML_PATH

    _INFRA_READY = False  # fail closed until the send infra resolves
    _YAML_PATH = Path(os.environ.get("SMD_CUSTOMER_YAML_PATH") or _DEFAULT_CUSTOMER_YAML_PATH)

    # ss#2258: NO AgentMail credential is resolved here anymore. The AgentMail
    # reply goes through a broker verb, so this process holds nothing that could
    # transmit — which is the fix, not a side effect. Keeping a key here "just in
    # case" would restore exactly the reachable-credential the incident exploited.
    # The msgraph path still mints its own Graph token per send (ADR 0078); the
    # two never cross-fall.

    try:
        _CUSTOMER_SLUG = get_secret("SMD_CUSTOMER_SLUG")
        # Audit MUST go through the broker-aware factory (OP-P1-4): when the
        # broker is configured this returns a BrokerAuditClient so the agent
        # cannot write its own tamper-resistant ledger directly; otherwise it
        # falls back to a D1 client. Same ``.execute(sql, *params)`` seam.
        _D1_CLIENT = audit_client_from_env(customer_slug=_CUSTOMER_SLUG)
    except KeyError as exc:
        # Audit is observability, not a gate — the relay can still send. Run
        # without it rather than disabling a working channel for a missing binding.
        _CUSTOMER_SLUG = None
        _D1_CLIENT = None
        logger.warning(
            "hermes-smd-reply: audit binding unconfigured (%s); reply channel will "
            "send without emitting audit rows",
            exc,
        )

    _LIMITER = relay.RateLimiter()
    _start_held_release()
    _INFRA_READY = True
    ctx.register_hook("post_tool_call", on_post_tool_call)
    logger.info(
        "hermes-smd-reply registered (infra_ready=True, customer=%s); "
        "replies gated on the live organization roster (scope.inbound_allow_from)",
        _CUSTOMER_SLUG,
    )
