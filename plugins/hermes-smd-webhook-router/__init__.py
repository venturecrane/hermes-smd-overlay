"""hermes-smd-webhook-router - route inbound webhook payloads to skills.

Attaches to one hook at the pinned Hermes ref (v2026.5.16):

- ``pre_gateway_dispatch`` (``hermes_cli/plugins.py:128-168`` lists it
  in VALID_HOOKS) - the gateway-side hook that runs before each
  inbound message is dispatched into the agent loop. The plugin's
  return value can rewrite the dispatch.

ADR 0021 Stream E. The router reads
``customer.yaml.webhook_triggers[]`` live on each dispatch (ADR 0044
WS2) and builds the ``(source, event_type)`` -> skill mapping fresh,
so editing the triggers takes effect on the next inbound webhook with
no restart. The inbound payload is inspected for webhook markers;
matches are rewritten to invoke the configured skill, non-matches
pass through unchanged.

Per ADR 0016 mirror-don't-gate: a successful route emits one
``WEBHOOK_ROUTED`` audit row directly via the shared D1Client. The
router is observation + dispatch-rewrite only; it never blocks the
dispatch on audit failure.

ADR 0027 inbound convergence: on a verified route the router also attaches
an :class:`shared.inbound.InboundEnvelope` (item_id, trust_class=
unknown_external, source, surface, ingested_at, verification, content_digest)
to the dispatch directive, emits an ``INBOUND_RECEIVED`` audit row, and
records the item in :data:`shared.inbound.PENDING` so the ``hermes-smd-inbound``
plugin's ``pre_llm_call`` chokepoint can wrap it in a nonce-fenced quarantine
block before the engine reasons over it. Envelope/enqueue failures never break
routing — the trust gate (``hermes-smd-trust`` refusing injected sends) remains
the enforcing wall; the envelope + fence are defense-in-depth + provenance.

Hook callbacks are exception-safe per AGENTS.md hard rule #3.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from shared import inbound, inbound_message, read_volume
from shared.audit_client import audit_client_from_env
from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.audit_contract import agent_event_params, sender_key
from shared.customer_config import CustomerConfig
from shared.secrets import get_secret, require
from shared.webhook_read_surface import (
    WEBHOOK_READ_TOOLSET,
    register_webhook_read_toolset,
)

from . import router, verify  # noqa: F401 - surface for tests

logger = logging.getLogger(__name__)


# Default path; overridden at register time from SMD_CUSTOMER_YAML_PATH.
_DEFAULT_CUSTOMER_YAML_PATH = "/opt/data/customer.yaml"

# Module-level state - populated by ``register()``.
#
# The routing table is NOT cached here: ``on_pre_gateway_dispatch`` rebuilds it
# live from customer.yaml on every dispatch (ADR 0044 WS2) so editing
# ``webhook_triggers`` takes effect on the next inbound webhook with no restart.
# ``_YAML_PATH`` is the authored-config path the live build reads. The env
# secrets below (slug, audit binding, signing secret) DO bind at register —
# they change only on a restart.
_YAML_PATH: Path = Path(_DEFAULT_CUSTOMER_YAML_PATH)
_CUSTOMER_SLUG: str | None = None
_D1_CLIENT: Any | None = None
# Per-customer webhook signing secret (issue #13). None disables routing:
# an unverifiable webhook must not drive skill actions.
_SIGNING_SECRET: str | None = None
# Per-process replay cache for event-ID de-duplication.
_REPLAY = verify.ReplayCache()

# Inbound header names carrying the provider signature material. Lower-cased
# for case-insensitive lookup. Provider-specific; defaults are generic.
_SIGNATURE_HEADER = "x-webhook-signature"
_TIMESTAMP_HEADER = "x-webhook-timestamp"
_EVENT_ID_HEADER = "x-webhook-id"


# ULID, ISO-Z timestamps, and the audit_log INSERT contract are single-sourced
# in shared.ids / shared.audit_contract (imported above). The row params are
# built via agent_event_params so this writer's column order can never drift
# from hermes-smd-audit/emit.py.


def _emit_webhook_routed(
    *,
    client: Any,
    customer: str,
    trigger: router.WebhookTrigger,
) -> None:
    """Write one WEBHOOK_ROUTED row directly via D1Client.

    Sidesteps the dynamic-import dance of pulling AuditLogWriter from
    the sibling audit plugin, but shares its row contract
    (``shared.audit_contract``) so the two can never desync.
    """
    metadata = {
        "per_webhook_route": True,
        "customer": customer,
        "source": trigger.source,
        "event_type": trigger.event_type,
        "persona": trigger.persona,
        "skill": trigger.skill,
    }
    params = agent_event_params(
        action_type="WEBHOOK_ROUTED",
        skill_name=trigger.skill,
        metadata=metadata,
    )
    client.execute(_INSERT_SQL, *params)


def _emit_inbound_received(
    *,
    client: Any,
    customer: str,
    envelope: inbound.InboundEnvelope,
    dto: Any = None,
    session_id: str = "",
) -> None:
    """Write one INBOUND_RECEIVED row directly via D1Client (ADR 0027).

    Carries the provenance envelope via ``envelope.audit_metadata()`` (source,
    surface, ingested_at, trust_class, verification, verification_detail,
    content_digest, item_id) — NEVER the inbound content itself, only its
    digest. ``INBOUND_RECEIVED`` is added to ACCEPTED_ACTION_TYPES ss-console-
    side by PR-B; the direct-INSERT path does not validate action_type, so the
    row writes through regardless. If the audit writer ever rejects the type,
    coordinate with team-lead (the schema is the canonical source).

    WHO SENT IT, AND WHICH MESSAGE (ss-console#2497). Until this, the row's only
    identifier was ``item_id`` — ``secrets.token_hex(16)``, minted here
    (``shared/inbound.py``), belonging to nothing outside this process. So the
    row said a message arrived and could name neither the person who sent it nor
    the message in the mailbox it came from: naming the sender meant opening the
    mailbox and matching by timestamp and digest, which is what an auditor does
    when there is no record. Two facts fix it, both taken from the normalized
    seam DTO rather than re-parsed from the provider payload:

    * ``sender_key`` — ``sha256`` of the canonical address
      (``shared.audit_contract.sender_key``). A HASH, not the address: an audit
      export is a file that leaves the Machine, and the issue's non-goal is
      explicit that no raw address may be in a row. The firm holds its own
      people's addresses and can reproduce the key.
    * ``vendor_message_id`` — ``dto.message_id``: the AgentMail message id, or
      the Graph message id the msgraph connector normalized onto the DTO. Both
      are OBSERVED fields of the seam contract (``shared/inbound_message.py``:
      ``message_id`` is required for msgraph and parsed from ``message_id`` for
      AgentMail), which is why this reads the DTO and does not reach for
      ``internetMessageId`` — nothing in this repo has seen that field on the
      wire, and a guessed key is a key no query will ever match.

    ``session_id`` rides along under the same argument the send rows use: it is
    the only thing that ties this row to the reply it produced. It is routinely
    EMPTY on the live email path (core carries none at dispatch, observed
    2026-07-15) and is then omitted rather than written as "".
    """
    metadata = {
        "per_inbound_received": True,
        "customer": customer,
        **envelope.audit_metadata(),
    }
    if dto is not None:
        key = sender_key(getattr(dto, "from_addr", None))
        if key:
            metadata["sender_key"] = key
        vendor_message_id = getattr(dto, "message_id", "")
        if isinstance(vendor_message_id, str) and vendor_message_id:
            metadata["vendor_message_id"] = vendor_message_id
    # input_digest stays NULL — inbound content is never persisted, only its
    # digest (carried inside metadata via envelope.audit_metadata()).
    params = agent_event_params(
        action_type="INBOUND_RECEIVED",
        metadata=metadata,
        session_id=session_id or None,
    )
    client.execute(_INSERT_SQL, *params)


def _inbound_content_for(payload: Any) -> str:
    """Best-effort extraction of the untrusted text body from an inbound payload.

    Used for the envelope's content_digest and the quarantine fence. Tries the
    common body keys (top-level then nested under ``data``/``body``); falls back
    to a canonical JSON serialization of the whole payload so SOMETHING is
    always quarantined (fail-closed: we never hand un-fenced inbound to the
    engine just because we didn't recognize the body shape).
    """
    if isinstance(payload, dict):
        for key in ("body", "body_plain", "text", "content", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("body", "body_plain", "text", "content", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _origin_from_dto(
    dto: inbound_message.InboundMessage | None, *, content: str
) -> inbound.InboundOrigin | None:
    """Derive the recipient-lock origin from the normalized seam DTO (spec D2).

    The origin fields — the verified sender (roster input), the reply
    ``message_id``, and the AgentMail ``inbox_id`` the reply threads into — are
    read FROM the :class:`shared.inbound_message.InboundMessage`, not re-parsed
    from the raw payload, so the seam is the single place that understands
    provider shapes. Behavior is preserved for AgentMail: the DTO's ``from_addr``
    is the same bare lower-cased address the old inline parse produced, and
    ``provider_refs['inbox_id']`` is the same inbox anchor.

    Returns ``None`` (fail closed — no recorded origin, the relay refuses to
    send) when the DTO is absent or lacks a sender address or a message id."""
    if dto is None:
        return None
    sender = dto.from_addr
    message_id = dto.message_id
    if not sender or not message_id:
        return None
    inbox_id = ""
    if isinstance(dto.provider_refs, dict):
        candidate = dto.provider_refs.get("inbox_id")
        if isinstance(candidate, str):
            inbox_id = candidate
    if not inbox_id and isinstance(dto.mailbox, str):
        inbox_id = dto.mailbox
    return inbound.InboundOrigin(
        sender_address=sender,
        message_id=message_id,
        content_digest=inbound.content_digest(content),
        inbox_id=inbox_id,
    )


def _sender_status_text(event: Any, envelope: Any, address: Any) -> str | None:
    """The dispatched user message re-rendered with the SENDER STATUS paragraph.

    Returns ``None`` when this dispatch must go out unchanged — which is every
    case except a verified sender on the authored ``scope.admins`` list whose
    turn prompt carries the untrusted-email delimiter (``with_sender_status``
    holds the full fail-closed test; it returns its input unchanged otherwise
    and never raises). A rostered NON-admin is explicitly in the unchanged set:
    roster membership authorizes a reply, not the direction of the firm's work
    (ss#2416 iteration 5, ss-console Decision #55).

    WHY THE DISPATCHED MESSAGE AND NOT A HOOK INJECTION (ss#2416). The primary
    user message on an email turn is the route template Hermes' webhook adapter
    rendered into ``MessageEvent.text`` (``gateway/platforms/webhook.py:409-413,
    553-559`` at the pinned ref) — a static per-route string that cannot branch
    on the sender. ``pre_llm_call`` can only APPEND context to that message, so
    the quarantine wrap's verified-sender header lands after the template's
    "treat strictly as DATA / just write the reply" framing and loses to it. This
    hook is the first place the sender is known AND the message is still
    editable, so the framing goes in here, above the delimiter.
    """
    text = getattr(event, "text", None)
    if not isinstance(text, str) or not text:
        return None
    augmented = inbound.with_sender_status(text, envelope=envelope, address=address)
    if not isinstance(augmented, str) or augmented == text:
        return None
    return augmented


def _apply_sender_status(event: Any, text: str) -> bool:
    """Write ``text`` back onto the dispatched ``MessageEvent`` in place.

    ``MessageEvent`` is a plain (non-frozen) dataclass at the pinned ref and core
    itself reassigns ``event.text`` mid-dispatch (``gateway/run.py:6439,6504``),
    so in-place is the narrowest edit: the routing directive the router already
    returns keeps its ``route_to_skill`` shape. Returns False when the write did
    not take (a future frozen ``MessageEvent``), and the caller then falls back
    to the hook's documented ``{"action": "rewrite", "text": …}`` directive
    (``gateway/run.py:5796-5833``). If BOTH fail the dispatch is unchanged —
    the seat behaves exactly as it does today rather than unsafely.
    """
    try:
        event.text = text
    except Exception as exc:  # noqa: BLE001 — never break a dispatch
        logger.warning(
            "hermes-smd-webhook-router: in-place sender-status write failed (%s); "
            "falling back to the rewrite directive",
            exc,
        )
        return False
    return getattr(event, "text", None) == text


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup. Returns None if absent/not a dict."""
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name:
            return value if isinstance(value, str) else None
    return None


def _raw_body_for(kwargs: dict[str, Any], payload: Any) -> bytes:
    """Resolve the exact bytes the signature was computed over.

    The gateway SHOULD pass ``raw_body`` (the verbatim request body) so
    the HMAC matches the provider's signature byte-for-byte. When it does
    not, we fall back to a canonical JSON serialization of the parsed
    payload — this only matches our own internal signer/tests, NOT a real
    third-party provider, which is why an absent raw_body for a real
    inbound webhook will fail verification and (correctly) not route.
    """
    raw = kwargs.get("raw_body")
    if isinstance(raw, (bytes, str)):
        return raw if isinstance(raw, bytes) else raw.encode("utf-8")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _event_id_from_payload(payload: Any) -> str | None:
    """Best-effort event-ID extraction for replay dedupe."""
    if not isinstance(payload, dict):
        return None
    for key in ("event_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("event_id", "id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _verification_failure(kwargs: dict[str, Any], payload: Any) -> str | None:
    """Return a human-readable reason the inbound webhook is unverified,
    or ``None`` when it verifies. Routing proceeds only on ``None``.

    Refusing to route on any failure is the safe default (issue #13): a
    forged or replayed event must not drive skill actions. The dispatch
    is not blocked — it simply passes through as an ordinary message that
    the agent loop (and the trust ceiling) still governs.
    """
    if _SIGNING_SECRET is None:
        return (
            "SMD_WEBHOOK_SIGNING_SECRET not configured; webhook routing is "
            "disabled until a per-customer signing secret is provisioned"
        )
    headers = kwargs.get("headers")
    # Header-bearing invocation (legacy / tests): do the full signature check.
    # Header-less invocation is the real Hermes hook contract — the SMD gate
    # (Svix) and Hermes' webhook adapter (X-Webhook-Signature) BOTH verified the
    # delivery upstream before the MessageEvent was built, and the hook carries
    # no headers to re-verify. Trust that upstream verification and fall through
    # to replay-protection (which the payload's event_id still anchors). An
    # attacker cannot reach this hook without first clearing both upstream
    # verifiers, so this is not a bypass.
    if headers is not None:
        signature = _header(headers, _SIGNATURE_HEADER)
        timestamp = _header(headers, _TIMESTAMP_HEADER)
        raw_body = _raw_body_for(kwargs, payload)
        try:
            verify.verify_signature(
                secret=_SIGNING_SECRET,
                raw_body=raw_body,
                signature=signature,
                timestamp=timestamp,
            )
        except verify.WebhookVerificationError as exc:
            return f"signature verification failed: {exc}"

    event_id = _header(headers, _EVENT_ID_HEADER) or _event_id_from_payload(payload)
    if not event_id:
        return "missing event id required for replay protection"
    if not _REPLAY.check_and_record(event_id):
        return f"replayed event id {event_id!r}"
    return None


def _webhook_payload(kwargs: dict[str, Any]) -> Any:
    """Resolve the parsed webhook body from the hook kwargs.

    Hermes' ``pre_gateway_dispatch`` hook (gateway/run.py) is invoked as
    ``invoke_hook("pre_gateway_dispatch", event=<MessageEvent>, gateway=...,
    session_store=...)`` — there is NO ``payload`` kwarg. The webhook adapter
    (gateway/platforms/webhook.py) parses the POST body and stores it on
    ``MessageEvent.raw_message``. So the parsed dict lives at
    ``kwargs["event"].raw_message``. The router originally read
    ``kwargs["payload"]`` (an assumed contract that never existed at runtime),
    so the route NEVER matched and no webhook was ever auto-routed — the demo
    relay's recipient-lock origin was therefore never recorded (2026-06-14).

    Back-compat: a direct ``payload`` kwarg (the in-tree tests, and any future
    invocation that passes one) is still honored. JSON strings are parsed."""
    event = kwargs.get("event")
    raw = getattr(event, "raw_message", None)
    if raw is None:
        raw = kwargs.get("payload")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


# Source label the MCP gate stamps on a conversational ask_operator turn
# (webhook_gate.py forwards ``{"source": "mcp", ...}``).
_MCP_SOURCE = "mcp"


def _quarantine_inbound_mcp(kwargs: dict[str, Any], payload: Any) -> None:
    """Fence + taint a conversational MCP turn, regardless of skill routing.

    The ask_operator channel routes to no skill, so it bypasses the matched-route
    enqueue in :func:`on_pre_gateway_dispatch`. This records the operator's
    message in :data:`shared.inbound.PENDING` so the ``hermes-smd-inbound``
    pre_llm_call chokepoint fences it and marks the session tainted — the same
    untrusted-origin treatment inbound email gets. The reply still flows (the
    synchronous return rides the result store, not a send tool), and reads/drafts
    are unaffected; only autonomous sensitive third-party actions are withheld for
    the tainted turn. Trust class stays ``unknown_external`` — a verified channel
    is still untrusted third-party data. Never raises (provenance must not break
    dispatch)."""
    try:
        content = _inbound_content_for(payload)
        envelope = inbound.make_envelope(
            content=content,
            source=_MCP_SOURCE,
            surface="webhook",
            verification="verified",
            trust_class=inbound.TRUST_CLASS_UNKNOWN_EXTERNAL,
        )
        session_id = kwargs.get("session_id")
        if not isinstance(session_id, str):
            session_id = ""
        inbound.PENDING.enqueue(
            inbound.InboundItem(session_id=session_id, content=content, envelope=envelope)
        )
    except Exception as exc:  # noqa: BLE001 — provenance must not break dispatch
        logger.warning(
            "hermes-smd-webhook-router: mcp inbound quarantine failed (%s); "
            "route still passed through (the trust gate remains the enforcing wall)",
            exc,
        )


def on_pre_gateway_dispatch(**kwargs: Any) -> dict | None:
    """Inspect each inbound dispatch for webhook markers and rewrite if matched.

    Hermes invokes this hook with ``event`` (a ``MessageEvent``), ``gateway``,
    and ``session_store`` — NOT a ``payload``/``headers``/``raw_body`` kwarg set.
    The parsed webhook body is read from ``event.raw_message`` via
    ``_webhook_payload``. The SMD gate (Svix) and Hermes' webhook adapter
    (``X-Webhook-Signature``) verify the delivery UPSTREAM before the
    ``MessageEvent`` is built, so when the hook carries no ``headers`` the router
    trusts that upstream verification and applies replay-protection only (a
    header-bearing invocation still gets the full signature check).

    Returns a rewrite directive on a matched AND verified webhook, or
    ``None`` to pass through unchanged. The rewrite directive shape:

        {"action": "route_to_skill",
         "persona": "<persona-slug>",
         "skill": "<skill-name>",
         "payload": <original payload>}

    Hermes' gateway dispatcher consumes this contract to invoke the
    named skill on the named persona.

    On a VERIFIED sender who is on the authored ``scope.admins`` list, the
    dispatched ``MessageEvent.text`` also gains the SENDER STATUS paragraph
    above the untrusted-body delimiter (ss#2416 — see ``_sender_status_text``).
    That edit is applied in place; only if the in-place write cannot take does
    the directive downgrade to Hermes' own ``{"action": "rewrite", "text": …}``
    contract (``gateway/run.py:5796-5833`` at the pinned ref). Every other
    sender dispatches byte-identically to before.

    A matched route is verified (HMAC signature + timestamp freshness +
    event-ID dedupe) before it fires (issue #13). Verification failure
    does NOT block the dispatch — it declines to auto-invoke the skill,
    so a forged or replayed event cannot drive skill actions.

    Exception-safe: any failure logs at warning level and returns
    ``None``. Per AGENTS.md hard rule #3, the callback never raises.
    """
    payload = _webhook_payload(kwargs)

    # Conversational MCP channel (ask_operator): "just talk" routes to NO skill,
    # so it never reaches the matched-route enqueue below. But its message is
    # untrusted external input — on the Claude connector it is literally another
    # model relaying arbitrary text — so it MUST fence + taint the session exactly
    # as inbound email does, or an instruction smuggled inside a conversational
    # message would slip past the taint-gate. Quarantine here and short-circuit:
    # the conversational channel is always the generic worker prompt, never a
    # skill. Over-tainting is fail-safe; the delivery is HMAC-verified upstream by
    # the webhook adapter before this hook runs.
    if isinstance(payload, dict) and payload.get("source") == _MCP_SOURCE:
        _quarantine_inbound_mcp(kwargs, payload)
        return None

    # Rebuild the routing table live from customer.yaml (ADR 0044 WS2): editing
    # webhook_triggers applies on the next dispatch with no restart. The build is
    # lenient (returns an empty table on any read/parse failure), so a transient
    # bad file fails safe to "no routing" rather than raising.
    table = router.build_routing_table(_YAML_PATH)
    if table.size() == 0:
        return None

    try:
        decision = router.decide_route(table, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes-smd-webhook-router: decide_route failed (%s); passing through",
            exc,
        )
        return None

    if decision.trigger is None:
        return None

    # Verify the inbound webhook before routing (issue #13). An attacker
    # who learns the dispatch URL must not be able to drive skill actions
    # with forged or replayed events.
    try:
        failure = _verification_failure(kwargs, payload)
    except Exception as exc:  # noqa: BLE001 - never let verification raise
        logger.warning(
            "hermes-smd-webhook-router: verification raised (%s); refusing to route",
            exc,
        )
        return None
    if failure is not None:
        logger.warning(
            "hermes-smd-webhook-router: refusing to route %s — %s",
            decision.matched_key,
            failure,
        )
        return None

    # ADR 0027 — attach an inbound provenance envelope to the dispatched
    # content and record it for the pre_llm_call quarantine chokepoint. The
    # router only reaches here on a VERIFIED route (the verification gate
    # above already declined forged/replayed events), so verification is
    # "verified". Trust class (ss #1943): a sender on the organization roster
    # (scope.inbound_allow_from — the SAME authored list that already
    # authorizes autonomous recipient-locked REPLIES to them, a strictly
    # stronger action) classifies ``internal``: their email is the firm's own
    # instruction channel, not third-party data, so it neither fences nor
    # taints. Everything else — strangers, payloads with no resolvable
    # sender, roster-read failures — stays ``unknown_external`` (fail
    # closed) and is fenced + tainted. Building the envelope must never
    # break the route, so it is wrapped in its own try/except.
    envelope: inbound.InboundEnvelope | None = None
    dto: inbound_message.InboundMessage | None = None
    # Set only when the in-place sender-status write could not be applied; the
    # directive below then carries the rewrite for Hermes to apply instead.
    rewrite_text: str | None = None
    # Bound BEFORE the try so the audit emission below can read it even when the
    # envelope block raised partway (ss-console#2497). It is also routinely "" on
    # the live email path, which the writer omits rather than records.
    session_id: str = ""
    try:
        content = _inbound_content_for(payload)
        # Normalize at the seam (spec D2): one InboundMessage shape regardless of
        # provider. The recipient-lock origin is derived FROM the DTO, and the DTO
        # is attached to the dispatch below — nothing downstream re-parses the raw
        # provider payload. normalize_inbound is fail-closed (unknown source →
        # None) and never raises.
        dto = inbound_message.normalize_inbound(decision.trigger.source, payload)
        origin = _origin_from_dto(dto, content=content)
        trust_class = inbound.TRUST_CLASS_UNKNOWN_EXTERNAL
        # SEPARATE FACT, SEPARATE LIST (ss#2416 iteration 5). Roster membership
        # (``scope.inbound_allow_from``) is the authorization to RESPOND to
        # someone; admin membership (``scope.admins``) is authority to direct the
        # firm's work. Iterations 1-4 collapsed the two and the seat obeyed a
        # reply-authorized non-admin (``shadow-…-2a47e3a7825a-notgreen`` on
        # overlay 8499256d, ss-console Decision #55). ``sender_is_admin`` is
        # exact-address only — no @domain widening — and fail-closed to "nobody",
        # and an unreadable config leaves BOTH facts at their closed defaults.
        is_admin = False
        if origin is not None and origin.sender_address:
            try:
                cfg = CustomerConfig.from_volume(str(_YAML_PATH))
                if cfg.sender_on_roster(origin.sender_address):
                    trust_class = inbound.TRUST_CLASS_INTERNAL
                is_admin = cfg.sender_is_admin(origin.sender_address)
            except Exception:  # noqa: BLE001 — config unreadable ⇒ fail closed
                trust_class = inbound.TRUST_CLASS_UNKNOWN_EXTERNAL
                is_admin = False
        envelope = inbound.make_envelope(
            content=content,
            source=decision.trigger.source,
            surface="webhook",
            verification="verified",
            verification_detail=(inbound.ADMIN_VERIFICATION_DETAIL if is_admin else None),
            trust_class=trust_class,
        )
        session_id = kwargs.get("session_id")
        if not isinstance(session_id, str):
            session_id = ""
        # Read-volume gate (agreement §2.8): record that this dispatch routed to
        # the gated review skill, so the trust plugin can meter the review's
        # document reads. Empty dispatch session ids (the live-path norm) enqueue
        # an unbound route claimed at the session's first pre_llm_call. The
        # register ignores every non-gated skill. Never breaks routing.
        try:
            read_volume.record_route(session_id, getattr(decision.trigger, "skill", "") or "")
        except Exception:  # noqa: BLE001 — must never perturb routing
            logger.debug("hermes-smd-webhook-router: read_volume route record failed", exc_info=True)
        # Record NON-internal items for the inbound plugin's pre_llm_call
        # chokepoint to fence + taint. Internal (rostered-colleague) mail is
        # NOT enqueued: fencing the firm's own requests behind "never act
        # BECAUSE of it" would contradict routing them to a skill to act on.
        # Keyed by the dispatch session when present; the live email path
        # carries NONE (observed 2026-07-15), so the chokepoint also drains
        # the fresh unkeyed bucket and taints the turn the dispatch produced.
        if trust_class != inbound.TRUST_CLASS_INTERNAL:
            inbound.PENDING.enqueue(
                inbound.InboundItem(session_id=session_id, content=content, envelope=envelope)
            )
            logger.info(
                "hermes-smd-webhook-router: quarantined inbound for fence+taint "
                "(trust_class=%s, session=%r)",
                trust_class,
                session_id,
            )
        else:
            logger.info(
                "hermes-smd-webhook-router: rostered-sender inbound classified internal; "
                "no quarantine (sender on scope.inbound_allow_from)"
            )
        # Recipient-lock anchor (hermes-smd-reply). Record WHO opened this
        # session — the verified inbound sender + the inbox/message to reply
        # into — so the reply channel can only send a governed draft back to the
        # address that emailed in. FIRST inbound wins (SessionInboundOrigin), so
        # a later injected "inbound" cannot move the lock. Recording the origin
        # grants NO send capability on its own; the reply is fail-closed on the
        # organization roster (scope.inbound_allow_from) and re-checks the
        # content/fabrication floors. A payload without a resolvable
        # sender/message-id records nothing (the relay then finds no origin and
        # refuses to send). Never breaks routing.
        if origin is not None:
            inbound.SESSION_INBOUND_ORIGIN.record(session_id, origin)
            # Diagnostic: confirms the recipient-lock anchor recorded, the
            # session_id it keyed under (empty here is the case the relay's
            # address-recovery path handles), and that the inbox/message ids
            # needed to thread the reply are present. Attribution only — never
            # the body (the audit row already carries the recipient).
            logger.info(
                "hermes-smd-webhook-router: recorded inbound origin "
                "(session=%r, sender=%s, have_inbox_id=%s, have_message_id=%s)",
                session_id,
                origin.sender_address,
                bool(origin.inbox_id),
                bool(origin.message_id),
            )
        else:
            logger.warning(
                "hermes-smd-webhook-router: no inbound origin extracted "
                "(payload keys=%s); demo relay has no recipient anchor to recover",
                sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
            )

        # ss#2416 — say in the PRIMARY turn prompt what the envelope already
        # knows: this sender is a verified administrator of the firm and this is
        # a work request, not third-party data to reason about. Gated on the SAME
        # fail-closed test the quarantine header uses (internal AND verified AND
        # on scope.admins) plus the presence of the untrusted-email delimiter, so
        # a rostered NON-admin, a stranger's email, and a vendor webhook all
        # dispatch byte-identically. The wrap header selection shares the gate.
        if envelope is not None and origin is not None:
            event = kwargs.get("event")
            candidate = _sender_status_text(event, envelope, origin.sender_address)
            if candidate is not None:
                if _apply_sender_status(event, candidate):
                    logger.info(
                        "hermes-smd-webhook-router: dispatched prompt carries the "
                        "verified-admin work-request paragraph (in place)"
                    )
                else:
                    rewrite_text = candidate
    except Exception as exc:  # noqa: BLE001 — provenance must not break routing
        logger.warning(
            "hermes-smd-webhook-router: inbound envelope/enqueue failed (%s); "
            "route still applied (the trust gate remains the enforcing wall)",
            exc,
        )

    # Emit audit rows. Failure is logged but does NOT block the route -
    # the dispatch rewrite is the load-bearing action; the audit rows
    # are observability.
    if _D1_CLIENT is not None and _CUSTOMER_SLUG is not None:
        try:
            _emit_webhook_routed(
                client=_D1_CLIENT,
                customer=_CUSTOMER_SLUG,
                trigger=decision.trigger,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes-smd-webhook-router: WEBHOOK_ROUTED emission failed (%s); "
                "route still applied",
                exc,
            )
        if envelope is not None:
            try:
                _emit_inbound_received(
                    client=_D1_CLIENT,
                    customer=_CUSTOMER_SLUG,
                    envelope=envelope,
                    dto=dto,
                    session_id=session_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes-smd-webhook-router: INBOUND_RECEIVED emission failed (%s); "
                    "route still applied",
                    exc,
                )

    directive: dict[str, Any] = {
        "action": "route_to_skill",
        "persona": decision.trigger.persona,
        "skill": decision.trigger.skill,
        "payload": payload,
    }
    if envelope is not None:
        # Attach provenance to the dispatch directive so downstream consumers
        # (and the dashboard) can trace the action back to the inbound item.
        directive["inbound_envelope"] = envelope.as_dict()
    if dto is not None:
        # Additive (spec D2): the normalized seam DTO rides alongside the raw
        # ``payload`` and ``inbound_envelope``. Live authored skills still read
        # the raw ``{message.*}`` paths; downstream seam consumers read this.
        directive["inbound_message"] = dto.to_dict()
    if rewrite_text is not None:
        # Fallback only (see _apply_sender_status): the in-place write did not
        # take, so ask Hermes to replace the dispatched text via the hook's own
        # documented rewrite contract. ``route_to_skill`` is not consumed by core
        # at the pinned ref (routing comes from the webhook route's ``skills:``
        # list), so swapping the action here costs no routing behavior; the
        # persona/skill/provenance keys still ride along for overlay consumers.
        directive["action"] = "rewrite"
        directive["text"] = rewrite_text
    return directive


def register(ctx) -> None:
    """Plugin entry point. Wires pre_gateway_dispatch.

    Binds the env secrets (customer slug, audit binding, signing secret) and
    records the authored-config path. The routing table itself is NOT built
    here — ``on_pre_gateway_dispatch`` rebuilds it live from customer.yaml on
    every dispatch (ADR 0044 WS2), so editing ``webhook_triggers`` takes effect
    on the next inbound webhook with no restart. A one-shot build runs here only
    to log the table size at boot; it is not retained.

    Also creates the read-only webhook toolset (ss-console#2145). This is the
    RUNTIME half of a two-process fix whose config half lives in
    ``bootstrap/translate.py``; it belongs here because this plugin already owns
    the webhook platform's concerns, and it runs at plugin LOAD rather than on
    first dispatch because ``get_tool_definitions`` memoizes on
    ``registry._generation`` and creating a toolset does not bump it — a late
    registration can land behind a stale memo and never be seen.
    """
    global _YAML_PATH, _CUSTOMER_SLUG, _D1_CLIENT, _SIGNING_SECRET

    # Before anything env-dependent: a seat whose secrets are unset still needs
    # the toolset to exist, or the activation gate refuses the boot for the
    # wrong reason. Exception-safe — the gate is what turns a miss into a
    # refusal to serve, and it reads the RESOLVED surface, so a failure here
    # cannot pass silently.
    try:
        register_webhook_read_toolset()
    except Exception:  # noqa: BLE001
        logger.warning(
            "hermes-smd-webhook-router: could not create the %r toolset; read_file "
            "will be absent from webhook turns and the activation gate will refuse "
            "to serve (ss-console#2145)",
            WEBHOOK_READ_TOOLSET,
            exc_info=True,
        )

    try:
        secrets_map = require("SMD_CUSTOMER_SLUG", "SMD_D1_AUDIT_BINDING")
        _CUSTOMER_SLUG = secrets_map["SMD_CUSTOMER_SLUG"]
        # Broker-aware audit transport (OP-P1-4): routes WEBHOOK_ROUTED /
        # INBOUND_RECEIVED rows through the append-only broker when
        # SMD_AUDIT_BROKER_SOCKET is set; direct D1Client otherwise.
        _D1_CLIENT = audit_client_from_env(customer_slug=_CUSTOMER_SLUG)
    except KeyError as exc:
        _CUSTOMER_SLUG = None
        _D1_CLIENT = None
        logger.warning(
            "hermes-smd-webhook-router: env not configured (%s); routing will work "
            "without audit emission",
            exc,
        )

    # Per-customer webhook signing secret (issue #13). Optional at register
    # time: when absent, the router still registers but refuses to route any
    # webhook (it cannot verify it). Provisioned alongside the webhook
    # subscription that captures the provider's signing secret.
    try:
        _SIGNING_SECRET = get_secret("SMD_WEBHOOK_SIGNING_SECRET")
    except KeyError:
        _SIGNING_SECRET = None
        logger.warning(
            "hermes-smd-webhook-router: SMD_WEBHOOK_SIGNING_SECRET not set; inbound "
            "webhooks cannot be verified and will NOT be routed until it is provisioned"
        )

    _YAML_PATH = Path(os.environ.get("SMD_CUSTOMER_YAML_PATH") or _DEFAULT_CUSTOMER_YAML_PATH)
    logger.info(
        "hermes-smd-webhook-router registered (customer=%s, routing_table_size=%d, "
        "table read live per dispatch)",
        _CUSTOMER_SLUG,
        router.build_routing_table(_YAML_PATH).size(),
    )

    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
