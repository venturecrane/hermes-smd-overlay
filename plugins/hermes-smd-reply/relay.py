"""Pure logic + AgentMail send for the Operator reply channel.

Kept out of ``__init__.py`` so the hook callback stays a thin, exception-safe
wrapper (AGENTS.md: heavier logic lives in module files imported by register).
No module-level state here — the plugin owns the rate-limiter instance and the
API key; this module is a library of pure decisions plus one network call.

Design: ADR 0055 (the Operator is an employee). The reply channel sends the
agent's OWN governed draft (produced under the taint-gate + content/fabrication
floors) back to the verified inbound sender when that sender is on the
organization roster. It defeats no agent floor; it implements the employee's
"reply to a colleague" OUTSIDE the model's governed tool path, with a structural
recipient-lock and roster-membership authorization.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any

from shared import agentmail_broker, content_floor, outbound_gate
from shared import send_policy as send_policy_mod

logger = logging.getLogger(__name__)


# AgentMail REST API (NOT the MCP gateway — that uses x-api-key; the REST API
# at api.agentmail.to authenticates with a Bearer token). Verified via Context7
# /agentmail-to/agentmail-node + agentmail-skills, 2026-06-11.
AGENTMAIL_API_BASE = "https://api.agentmail.to/v0"
_SEND_TIMEOUT_S = 10.0


# Rate-limit defaults. A colleague sends a message and gets one reply; these
# bound a runaway/abusive loop without constraining the happy path. Per-sender is
# the tight bound (one address cannot be replied to more than _PER_SENDER_MAX
# times in the window); global bounds total reply volume.
_PER_SENDER_MAX = 3
_PER_SENDER_WINDOW_S = 600.0  # 10 min
_GLOBAL_MAX = 20
_GLOBAL_WINDOW_S = 3600.0  # 1 hour


# ---------------------------------------------------------------------------
# Draft extraction + recipient-lock
# ---------------------------------------------------------------------------


def _normalize_addr(value: Any) -> str:
    """Lower-cased bare email address from a ``"Name <addr>"`` or bare string."""
    if not isinstance(value, str):
        return ""
    return parseaddr(value)[1].strip().lower()


def draft_recipients(args: Any) -> set[str]:
    """Normalized recipient set from a ``create_draft`` ``to`` argument.

    Accepts a list of address strings (the AgentMail draft schema) or a single
    string (tolerant of MCP arg shapes). Each is normalized to a bare lower-
    cased address. Empty / unparseable entries are dropped — they cannot match
    the recorded sender, so dropping them only ever fails the lock CLOSED.
    """
    if not isinstance(args, dict):
        return set()
    raw = args.get("to")
    items: list[Any]
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return set()
    return {addr for addr in (_normalize_addr(x) for x in items) if addr}


def recipient_locked(args: Any, recorded_sender: str) -> bool:
    """True iff the draft addresses EXACTLY the recorded inbound sender.

    The lock is the security crux: the reply can only go back to whoever emailed
    in. It holds iff the draft's normalized recipient set is exactly the single
    recorded sender address — so an injected extra recipient ("also cc
    attacker@evil"), a substituted recipient, or an empty/missing ``to`` all
    FAIL the lock. (The actual send is additionally keyed on the recorded
    inbox+message id, so even a lock bypass would still thread only to the
    original sender — this is the intent half of a two-part structural lock.)
    """
    recorded = (recorded_sender or "").strip().lower()
    if not recorded:
        return False
    return draft_recipients(args) == {recorded}


def draft_body(args: Any) -> tuple[str, str, str]:
    """Return ``(scan_text, send_text, send_html)`` from a ``create_draft`` args.

    ``scan_text`` is what the content + fabrication floors inspect: the subject
    and the plain-text body joined, falling back to the HTML body when no plain
    text was authored — broadest signal, safest scan. ``send_text`` /
    ``send_html`` are the reply body actually transmitted (the subject is NOT
    sent — a reply threads under "Re: …"). An empty ``scan_text`` is left empty
    on purpose: the floors fail CLOSED on an uninspectable body, and the caller
    additionally refuses to send when both ``send_text`` and ``send_html`` are
    empty (a subject-only draft has nothing to relay).
    """
    if not isinstance(args, dict):
        return "", "", ""
    subject = args.get("subject") if isinstance(args.get("subject"), str) else ""
    text = args.get("text") if isinstance(args.get("text"), str) else ""
    html = args.get("html") if isinstance(args.get("html"), str) else ""
    # msgraph-mail (ADR 0078) create_draft carries the body under ``body_text``
    # (flat args, D4), not ``text``. Fold it into the plain-text body so the
    # msgraph reply relays a real body and the floors scan it — without it,
    # send_text was empty and an msgraph reply had nothing to relay.
    if not text:
        body_text = args.get("body_text")
        if isinstance(body_text, str):
            text = body_text
    # Scan subject + text + html UNCONDITIONALLY (EFF-01): a fabricated citation
    # in an html body must not slip past because a benign subject/text is present.
    parts = [p for p in (subject, text, html) if p.strip()]
    scan_text = "\n".join(parts)
    return scan_text, text, html


# ---------------------------------------------------------------------------
# Content + fabrication re-check (the relay re-applies the send-path floors)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    """Outcome of re-applying the content + fabrication floors to a draft body."""

    allowed: bool
    reason: str = ""
    categories: tuple[str, ...] = field(default_factory=tuple)


# Tool-outcome vocabulary. ``post_tool_call`` fires after EVERY dispatch,
# including one that returned an error string, so the relay must read the
# outcome rather than the tool name alone.
_FAILED_STATUSES: frozenset[str] = frozenset(
    {"error", "errored", "failed", "failure", "refused", "blocked", "denied"}
)


def draft_call_failed(*, result: Any = None, status: Any = None, error_type: Any = None) -> bool:
    """True when the draft tool call POSITIVELY reports it created no draft.

    The relay turns a governed draft into a sent email. If the draft tool did
    not actually produce a draft, there is nothing to relay — sending anyway
    puts mail in a client's inbox on the strength of an intent the tool
    rejected, and the agent's retry then sends the same answer a second time
    (leg-1 turn 2: ``create_draft`` returned "Message not found (HTTP 404)",
    the relay emailed, the retry succeeded, the relay emailed again —
    vfy_01KYTG0B88R3B5K0D7FKPACRZT).

    Detection is deliberately POSITIVE-only: an unrecognised result shape
    returns False and the reply proceeds through the normal gates. Failing
    closed on an unknown shape would silence every reply the day Hermes changes
    its tool-result envelope, and the one-reply-per-inbound guard
    (:class:`RepliedOnce`) is the structural backstop for the duplicate case.
    """
    if isinstance(status, str) and status.strip().lower() in _FAILED_STATUSES:
        return True
    if isinstance(error_type, str):
        cleaned = error_type.strip()
        if cleaned and cleaned.lower() not in {"none", "null"}:
            return True
    if not isinstance(result, str) or not result.strip():
        return False
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    if parsed.get("error"):
        return True
    if parsed.get("ok") is False or parsed.get("success") is False:
        return True
    parsed_status = parsed.get("status")
    return isinstance(parsed_status, str) and parsed_status.strip().lower() in _FAILED_STATUSES


class RepliedOnce:
    """One reply per inbound message id — bounded, thread-safe.

    ``post_tool_call`` fires once per tool CALL, not per turn, so an agent that
    retries ``create_draft`` reaches the relay twice for a single inbound email.
    Keying on the inbound message id states the invariant the way a client would:
    one message in, at most one reply out.

    ``commit`` is called only once a reply is actually SENT or durably ENQUEUED
    for release — a gated or failed reply leaves the id free, so the held-release
    path can still deliver it, and a genuine later inbound (a new message id)
    is never suppressed.
    """

    def __init__(self, max_entries: int = 512) -> None:
        self._max = max(1, int(max_entries))
        self._seen: deque[str] = deque(maxlen=self._max)
        self._index: set[str] = set()
        self._lock = threading.Lock()

    def committed(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._lock:
            return message_id in self._index

    def commit(self, message_id: str) -> None:
        if not message_id:
            return
        with self._lock:
            if message_id in self._index:
                return
            if len(self._seen) == self._max and self._seen:
                self._index.discard(self._seen[0])
            self._seen.append(message_id)
            self._index.add(message_id)

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._seen.clear()
            self._index.clear()


def gate_body(
    scan_text: str,
    *,
    vertical: str | None,
    cohort: str | None,
    internal_recipient: bool = False,
    allowed_case_names: Iterable[str] | None = None,
    allowed_money: Iterable[str] | None = None,
) -> GateResult:
    """Re-run the content-sensitivity floor + fabrication gate on the draft body.

    The relay sends OUTSIDE the model's governed tool path, so it must itself
    enforce the same floors the autonomous-send path would have applied:

    * ``content_floor.classify`` — money / contract / scope / legal content
      drops to draft (here: refuse to relay). Fails toward refuse on an
      empty / uninspectable body. **Skipped when ``internal_recipient`` is
      True** — the send path deliberately does not content-floor a send whose
      recipients classify INTERNAL (ADR 0072; enforce.py), because firm-internal
      coordination legitimately names deadlines, signatures, and attorneys.
      Flooring it here held ack confirmations in drafts (ss #1932). The caller
      owns the classification; this flag must come from the recipient
      classifier, never from a hardcoded True.
    * ``outbound_gate.evaluate`` — banned fabrication markers (Tier-1) +
      fabricated legal citations (Tier-2, law/indeterminate). Fails closed.
      Applies to EVERY reply, internal or not.

    ``allowed_case_names`` is the SAME provenance exemption the drafting path
    passes (``hermes-smd-trust/outbound.py``): case captions the agent actually
    READ from a system of record this session are quotable. Omitting it is what
    made the two channels disagree — the trust gate allowed a draft naming
    matters read from Smokeball, then this gate blocked the identical body as
    ``fabrication:tier2_citation`` and the sender got silence (leg-1 turn 5,
    vfy_01KYTG0B88R3B5K0D7FKPACRZT). An empty/omitted register grants no
    exemption, so the degradation direction is still fail-closed.

    ``allowed_money`` is the other half of that same disagreement, one path over
    (ss-console#2367). ss#2258 gave the Tier-1 ``specific-dollar-amount`` marker
    a provenance-scoped exemption on the DRAFTING path and said, deliberately,
    "no change to any other output path" — so on this path the gate still forbade
    what the skill permits. On 2026-08-13 a demand letter was filed on
    2026-PI-104 and the reply naming it was held ``fabrication:tier1_marker`` on
    two figures the agent had just read off the firm's own records (the Kaiser
    lien and the MedFin payoff), each cited to its source in the sentence that
    carried it. The firm asked for a demand letter and got silence. Same
    register, same canonical form, same all-or-nothing rule as the drafting path:
    an INVENTED figure in a reply still blocks, and an empty/omitted register
    grants no exemption at all.

    Any exception is treated as a refuse (fail closed) — a body we cannot
    certify clean does not leave.
    """
    if not internal_recipient:
        try:
            floor = content_floor.classify(scan_text)
        except Exception:  # noqa: BLE001 — uncertifiable body must not relay
            logger.exception("reply-channel: content floor raised; refusing to reply")
            return GateResult(allowed=False, reason="content_floor_error")
        if floor.sensitive:
            return GateResult(
                allowed=False, reason="content_sensitive", categories=floor.categories
            )

    try:
        decision = outbound_gate.evaluate(
            scan_text,
            cohort,
            vertical,
            allowed_case_names=allowed_case_names,
            allowed_money=allowed_money,
        )
    except Exception:  # noqa: BLE001 — fail closed on a raising gate
        logger.exception("reply-channel: outbound gate raised; refusing to reply")
        return GateResult(allowed=False, reason="outbound_gate_error")
    if not decision.allowed:
        return GateResult(allowed=False, reason=f"fabrication:{decision.tier or 'blocked'}")

    return GateResult(allowed=True)


# ---------------------------------------------------------------------------
# Rate limiter (per-sender + global, rolling window)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateDecision:
    """Outcome of a rate check: ``allowed`` plus a hold reason when refused."""

    allowed: bool
    reason: str | None = None


class RateLimiter:
    """Rolling-window per-sender + global + backstop send limiter.

    ``check(sender, internal=..., policy=...)`` evaluates the authored
    :class:`~shared.send_policy.SendPolicy` per call: the reply backstop (when
    authored) bounds every send; the per-sender and external-global windows are
    skipped for rostered-INTERNAL senders when the policy exempts them.
    Exempt sends are recorded ONLY in the backstop window, so internal
    dialogue never consumes external senders' capacity.

    ``allow(sender)`` is the legacy entry point and delegates to ``check``
    with the constructor's own values as the policy (no exemption, no
    backstop) — byte-for-byte the pre-#2070 behavior.

    Pure in-memory, bounded by the windows (old timestamps evicted on each
    call). ``clock`` is injectable for deterministic tests; defaults to
    ``time.monotonic`` (monotonic so a wall-clock adjustment cannot widen or
    collapse a window). One lock guards the whole of ``check`` — the held-
    reply sweeper (#2070 O2) calls it from a second thread, and
    evict-check-append must be atomic across ALL windows."""

    def __init__(
        self,
        *,
        per_sender_max: int = _PER_SENDER_MAX,
        per_sender_window_s: float = _PER_SENDER_WINDOW_S,
        global_max: int = _GLOBAL_MAX,
        global_window_s: float = _GLOBAL_WINDOW_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if clock is None:
            import time

            clock = time.monotonic
        self._clock = clock
        self._ctor_policy = send_policy_mod.SendPolicy(
            internal_exempt=False,
            per_sender_max=per_sender_max,
            per_sender_window_s=per_sender_window_s,
            global_max=global_max,
            global_window_s=global_window_s,
            backstop_max=0,
            backstop_window_s=global_window_s,
            held_release_enabled=False,
            held_ttl_s=send_policy_mod.DEFAULT_SEND_POLICY.held_ttl_s,
        )
        self._per_sender: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._backstop: deque[float] = deque()
        self._lock = threading.Lock()

    @staticmethod
    def _evict(window: deque[float], horizon: float) -> None:
        while window and window[0] < horizon:
            window.popleft()

    def check(
        self,
        sender: str,
        *,
        internal: bool,
        policy: send_policy_mod.SendPolicy,
    ) -> RateDecision:
        with self._lock:
            now = self._clock()

            # Reply backstop: bounds ALL classes when authored (0 = disabled).
            self._evict(self._backstop, now - policy.backstop_window_s)
            if policy.backstop_max > 0 and len(self._backstop) >= policy.backstop_max:
                return RateDecision(False, "rate_limited_backstop")

            exempt = internal and policy.internal_exempt
            if not exempt:
                self._evict(self._global, now - policy.global_window_s)
                if len(self._global) >= policy.global_max:
                    return RateDecision(False, "rate_limited_global")
                bucket = self._per_sender.get(sender)
                if bucket is None:
                    bucket = deque()
                    self._per_sender[sender] = bucket
                self._evict(bucket, now - policy.per_sender_window_s)
                if len(bucket) >= policy.per_sender_max:
                    return RateDecision(False, "rate_limited_per_sender")
                bucket.append(now)
                self._global.append(now)
            self._backstop.append(now)
            return RateDecision(True, None)

    def allow(self, sender: str) -> bool:
        return self.check(sender, internal=False, policy=self._ctor_policy).allowed


# ---------------------------------------------------------------------------
# AgentMail reply send (urllib — stdlib, matching honcho_client)
# ---------------------------------------------------------------------------


class RelaySendError(RuntimeError):
    """The AgentMail reply POST failed (HTTP error, unreachable, or timeout)."""


def send_reply(
    *,
    message_id: str,
    text: str,
    html: str,
    sender: Callable[..., Any] | None = None,
) -> str:
    """Ask the broker to send a threaded reply; return the new msg id.

    Was a direct ``POST /v0/inboxes/{id}/messages/{id}/reply`` with a Bearer
    token held in this process. It is now a broker verb, because ss#2258 showed
    that a credential living in the agent's address space is reachable by paths
    the trust hook never sees — four fabricated messages went to a real client
    principal with no audit row, which is only possible if the sending code was
    never gated.

    What moved: the broker re-fetches the source message itself and checks the
    ORIGINAL SENDER against ``inbound_allow_from`` before transmitting, and it
    writes the audit row. What stayed: the recipient is still structural (AgentMail
    threads to the original sender), so no address from the agent's draft is
    honored here — it never was, and now that guarantee is enforced by the process
    holding the key rather than by this one.

    ``sender`` is injectable for tests. Raises :class:`RelaySendError` on any
    failure — refusal or transport alike — because the caller's contract is
    unchanged: it is exception-safe and audits a failed reply.
    """
    send = sender or agentmail_broker.send_reply
    try:
        return str(send(message_id=message_id, text=text, html=html) or "")
    except agentmail_broker.BrokerError as exc:
        # The broker refused and has already recorded why. Surfacing its reason
        # keeps the operator-visible message specific ("that sender is not on
        # inbound_allow_from") rather than a generic delivery failure.
        raise RelaySendError(f"broker refused the reply: {exc}") from exc
    except agentmail_broker.AgentMailBrokerUnavailable as exc:
        raise RelaySendError(f"broker transmit unavailable: {exc}") from exc


__all__ = [
    "AGENTMAIL_API_BASE",
    "GateResult",
    "RateLimiter",
    "RelaySendError",
    "draft_body",
    "draft_recipients",
    "gate_body",
    "recipient_locked",
    "send_reply",
]
