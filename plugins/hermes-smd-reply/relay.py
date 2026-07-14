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
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any

from shared import content_floor, outbound_gate

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


def gate_body(scan_text: str, *, vertical: str | None, cohort: str | None) -> GateResult:
    """Re-run the content-sensitivity floor + fabrication gate on the draft body.

    The relay sends OUTSIDE the model's governed tool path, so it must itself
    enforce the same two floors the autonomous-send path would have applied:

    * ``content_floor.classify`` — money / contract / scope / legal content
      drops to draft (here: refuse to relay). Fails toward refuse on an
      empty / uninspectable body.
    * ``outbound_gate.evaluate`` — banned fabrication markers (Tier-1) +
      fabricated legal citations (Tier-2, law/indeterminate). Fails closed.

    Any exception is treated as a refuse (fail closed) — a body we cannot
    certify clean does not leave.
    """
    try:
        floor = content_floor.classify(scan_text)
    except Exception:  # noqa: BLE001 — uncertifiable body must not relay
        logger.exception("reply-channel: content floor raised; refusing to reply")
        return GateResult(allowed=False, reason="content_floor_error")
    if floor.sensitive:
        return GateResult(allowed=False, reason="content_sensitive", categories=floor.categories)

    try:
        decision = outbound_gate.evaluate(scan_text, cohort, vertical)
    except Exception:  # noqa: BLE001 — fail closed on a raising gate
        logger.exception("reply-channel: outbound gate raised; refusing to reply")
        return GateResult(allowed=False, reason="outbound_gate_error")
    if not decision.allowed:
        return GateResult(allowed=False, reason=f"fabrication:{decision.tier or 'blocked'}")

    return GateResult(allowed=True)


# ---------------------------------------------------------------------------
# Proactive outbound relay (ADR 0075 / #1868)
#
# The inbound reply path (above) delivers the agent's governed draft back to a
# verified INBOUND sender. This section is the mirror for PROACTIVE sends: a
# scheduled chase draft addressed to the firm's own rostered CLIENT / RECORDS
# VENDOR (``scope.outbound_roster``). The model, on a cron turn, reliably drafts
# and does not choose ``send_message`` (three weeks of audit: create_draft dozens
# of times, send_message zero). So — exactly as the reply relay does for inbound —
# this trusted code turns that governed draft into a sent message when, and only
# when, the SAME authorization the gate's ``send_message`` would require holds:
# the recipients classify to a typed outbound-roster class, that class's authored
# ceiling is ``autonomous``, and the turn is not tainted. Otherwise it holds (the
# draft simply stays a draft — the day-one posture).
#
# It re-applies the content + fabrication floors (``gate_body`` above) before
# sending, mirrors the gate's recipient classification via the SAME shared
# ``recipient_classifier`` primitives, and reads the authored ceiling from the
# SAME ``customer.yaml`` — so a proactive send can never be authorized where the
# model-path gate's ``send_message`` would be held. It defeats no floor.
# ---------------------------------------------------------------------------


# The canonical "autonomous" ceiling string — mirrors ``Ceiling.AUTONOMOUS.value``
# in ``plugins/hermes-smd-trust/enforce.py`` (the gate). ``test_proactive_relay``
# pins the equality against the gate via ``load_plugin`` so a rename on either
# side fails CI rather than silently diverging.
CEILING_AUTONOMOUS = "autonomous"


@dataclass(frozen=True)
class ProactiveDecision:
    """Whether a proactive draft may be autonomously delivered, plus the audit trail.

    ``send`` is the single bit the hook acts on. ``action_class`` / ``ceiling`` /
    ``reason`` are for the audit row (never the body). A held decision (``send``
    False) always carries a legible ``reason``.
    """

    send: bool
    action_class: str = ""
    ceiling: str = ""
    reason: str = ""


def proactive_disposition(
    *,
    recipients: set[str],
    internal_roster: Iterable[str],
    typed_roster: Sequence[tuple[str, str]],
    persona_exposure: Mapping[str, str],
    tainted: bool,
) -> ProactiveDecision:
    """Decide whether a proactive draft to ``recipients`` may be autonomously sent.

    Composes the SAME shared primitives the trust gate uses on the model send path
    — :func:`shared.recipient_classifier.classify_recipients_typed` +
    :func:`shared.recipient_classifier.send_action_class` + the authored per-class
    ceiling — so a proactive relay send can never be authorized where the gate's
    ``send_message`` would be held. Fail-closed at every branch:

      * no resolvable recipient → hold;
      * recipients do not classify to a typed outbound-roster class (CLIENT /
        VENDOR) → hold with no ``action_class`` (OUTSIDE — incl. an injected extra
        or heterogeneous recipient — / INTERNAL / UNKNOWN are owned by the model's
        own governed path or the inbound reply relay, not this one);
      * tainted turn → hold (a turn that read untrusted external content never
        fires an autonomous proactive send — the same net decision the gate reaches
        by reclassifying a tainted send to the outside/draft ceiling);
      * the class's authored ceiling is absent or not ``autonomous``
        (day-one ``draft_for_review``, ``confirm``, or unauthored) → hold.

    Recipients are classified with ``from_tainted=False`` so a client/vendor-aimed
    chase held purely by taint still resolves its intended ``action_class`` for a
    legible audit reason; the taint gate is then applied explicitly. The send/hold
    DECISION is identical to the gate either way — only the audit reason is sharper.
    """
    from shared.recipient_classifier import (
        RecipientClass,
        classify_recipients_typed,
        send_action_class,
    )

    if not recipients:
        return ProactiveDecision(False, reason="no_recipient")

    cls = classify_recipients_typed(
        sorted(recipients), internal_roster, typed_roster, from_tainted=False
    )
    if cls not in (RecipientClass.CLIENT, RecipientClass.VENDOR):
        # OUTSIDE (incl. an injected extra/heterogeneous recipient), INTERNAL, or
        # UNKNOWN — not a graduatable typed outbound send. Held as a draft, no
        # action_class (the caller stays silent: not a proactive-relay event).
        return ProactiveDecision(False, reason=f"not_typed_roster:{cls.value}")

    action = send_action_class(cls)
    if tainted:
        return ProactiveDecision(False, action_class=action, ceiling="", reason="tainted_turn")
    ceiling = persona_exposure.get(action, "")
    if ceiling != CEILING_AUTONOMOUS:
        return ProactiveDecision(
            False, action_class=action, ceiling=ceiling, reason="ceiling_not_autonomous"
        )
    return ProactiveDecision(True, action_class=action, ceiling=ceiling, reason="autonomous")


def parse_created_draft(result: Any) -> tuple[str, str]:
    """Return ``(inbox_id, draft_id)`` from a ``create_draft`` tool result.

    The ``post_tool_call`` hook receives the tool output as ``result`` (a JSON
    string, per the AgentMail MCP Draft object). Tolerant of shape: accepts a
    JSON string or an already-parsed mapping, camelCase (``draftId`` / ``inboxId``,
    the node SDK) or snake_case (``draft_id`` / ``inbox_id``, the REST envelope),
    a bare ``id`` for the draft, and one level of ``{"draft": {...}}`` nesting.
    Returns ``("", "")`` when either id is absent — the caller fails closed (a
    draft whose ids we cannot read is not sent).
    """
    obj: Any = result
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("utf-8", "replace")
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except (json.JSONDecodeError, ValueError):
            return "", ""
    if not isinstance(obj, dict):
        return "", ""
    inner = obj.get("draft") if isinstance(obj.get("draft"), dict) else obj
    draft_id = inner.get("draftId") or inner.get("draft_id") or inner.get("id") or ""
    inbox_id = inner.get("inboxId") or inner.get("inbox_id") or ""
    if not isinstance(draft_id, str) or not isinstance(inbox_id, str):
        return "", ""
    draft_id, inbox_id = draft_id.strip(), inbox_id.strip()
    if not (draft_id and inbox_id):
        # Both ids are required to send; a partial result fails closed so the
        # caller never attempts a send it cannot address.
        return "", ""
    return inbox_id, draft_id


# ---------------------------------------------------------------------------
# Rate limiter (per-sender + global, rolling window)
# ---------------------------------------------------------------------------


class RateLimiter:
    """Rolling-window per-sender + global send limiter.

    ``allow(sender)`` returns True and records the send iff neither the
    per-sender nor the global window is full. Pure in-memory, bounded by the
    window (old timestamps are evicted on each call). ``clock`` is injectable
    for deterministic tests; defaults to ``time.monotonic`` (monotonic so a
    wall-clock adjustment cannot widen or collapse a window)."""

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
        self._per_sender_max = per_sender_max
        self._per_sender_window_s = per_sender_window_s
        self._global_max = global_max
        self._global_window_s = global_window_s
        self._per_sender: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()

    @staticmethod
    def _evict(window: deque[float], horizon: float) -> None:
        while window and window[0] < horizon:
            window.popleft()

    def allow(self, sender: str) -> bool:
        now = self._clock()
        self._evict(self._global, now - self._global_window_s)
        if len(self._global) >= self._global_max:
            return False
        bucket = self._per_sender.get(sender)
        if bucket is None:
            bucket = deque()
            self._per_sender[sender] = bucket
        self._evict(bucket, now - self._per_sender_window_s)
        if len(bucket) >= self._per_sender_max:
            return False
        bucket.append(now)
        self._global.append(now)
        return True


# ---------------------------------------------------------------------------
# AgentMail reply send (urllib — stdlib, matching honcho_client)
# ---------------------------------------------------------------------------


class RelaySendError(RuntimeError):
    """The AgentMail reply POST failed (HTTP error, unreachable, or timeout)."""


def send_reply(
    *,
    api_key: str,
    inbox_id: str,
    message_id: str,
    text: str,
    html: str,
    base_url: str = AGENTMAIL_API_BASE,
    timeout_s: float = _SEND_TIMEOUT_S,
    opener: Callable[..., Any] | None = None,
) -> str:
    """POST a threaded reply via the AgentMail REST API; return the new msg id.

    Endpoint: ``POST /v0/inboxes/{inbox_id}/messages/{message_id}/reply`` with a
    ``{text, html}`` body and a ``Bearer`` token. The reply is keyed on the
    recorded inbox + message, so AgentMail threads it to the original sender —
    the recipient is structurally the inbound sender, independent of any address
    the agent's draft named. ``opener`` is injectable for tests (defaults to
    ``urllib.request.urlopen``). Raises :class:`RelaySendError` on any failure;
    the caller is exception-safe and audits the failure.
    """
    path = (
        f"/inboxes/{urllib.parse.quote(inbox_id, safe='')}"
        f"/messages/{urllib.parse.quote(message_id, safe='')}/reply"
    )
    url = base_url + path
    body: dict[str, str] = {}
    if text:
        body["text"] = text
    if html:
        body["html"] = html
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    _open = opener or urllib.request.urlopen
    try:
        with _open(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise RelaySendError(f"agentmail reply returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RelaySendError(f"agentmail reply unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RelaySendError(f"agentmail reply timed out after {timeout_s}s") from exc
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = {}
    # AgentMail returns {"messageId": "..."} (SendMessageResponse). Surface it
    # for the audit row; absence is non-fatal (the send succeeded — 2xx).
    return str(parsed.get("messageId") or parsed.get("message_id") or "")


def send_draft(
    *,
    api_key: str,
    inbox_id: str,
    draft_id: str,
    base_url: str = AGENTMAIL_API_BASE,
    timeout_s: float = _SEND_TIMEOUT_S,
    opener: Callable[..., Any] | None = None,
) -> str:
    """Send an EXISTING draft via the AgentMail REST API; return the new msg id.

    Endpoint: ``POST /v0/inboxes/{inbox_id}/drafts/{draft_id}/send`` with a
    ``Bearer`` token and an empty body (no scheduling). This is the proactive
    outbound relay's delivery step: the model already composed and created the
    draft (``create_draft``), the floors have been re-applied to that draft's
    body, and this sends *that exact draft* — so the content inspected is byte-for-
    byte the content transmitted, with no re-composition. ``opener`` is injectable
    for tests. Raises :class:`RelaySendError` on any failure; the caller is
    exception-safe and audits the failure.
    """
    path = (
        f"/inboxes/{urllib.parse.quote(inbox_id, safe='')}"
        f"/drafts/{urllib.parse.quote(draft_id, safe='')}/send"
    )
    url = base_url + path
    data = b"{}"
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    _open = opener or urllib.request.urlopen
    try:
        with _open(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise RelaySendError(f"agentmail send-draft returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RelaySendError(f"agentmail send-draft unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RelaySendError(f"agentmail send-draft timed out after {timeout_s}s") from exc
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = {}
    return str(parsed.get("messageId") or parsed.get("message_id") or "")


__all__ = [
    "AGENTMAIL_API_BASE",
    "CEILING_AUTONOMOUS",
    "GateResult",
    "ProactiveDecision",
    "RateLimiter",
    "RelaySendError",
    "draft_body",
    "draft_recipients",
    "gate_body",
    "parse_created_draft",
    "proactive_disposition",
    "recipient_locked",
    "send_draft",
    "send_reply",
]
