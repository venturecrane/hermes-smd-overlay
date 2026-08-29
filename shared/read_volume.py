"""Read-volume gate for the opposing-response review routine (agreement §2.8).

WHAT THIS CLOSES. Routine 5 ("Opposing responses reviewed") is webhook-fired
when opposing counsel serves discovery responses: nobody at the firm requests
it, and the volume is set by the adversary. The service agreement caps the
review at an authored page threshold — past it, the Operator surfaces the set
with the observed size and does not keep reviewing. A sentence in SKILL.md
cannot hold that line (an instruction to the model is not a control), so the
stop lives here: the trust plugin refuses the document read that would carry a
review past the threshold, and the refusal directive tells the model the one
thing it can still do — write the surface-only internal note.

SCOPE — which sessions are "a review". Two triggers mark a session, either is
sufficient, both are mechanical observations rather than model claims:

* the webhook router dispatched the session to the gated skill (the direct
  route), recorded at dispatch — with the same unbound claim-once handoff
  ``shared.inbound`` uses, because dispatch-time session ids are often empty;
* the session READ the gated skill's procedure — ``skill_view`` of the gated
  skill, or ``read_file`` of its SKILL.md, observed at post_tool_call. This is
  the spine path: ``matter-inbox-router`` executes other skills' procedures
  inside its own session and reads the procedure first, so the read is the
  earliest mechanical footprint of a review on the path a live seat actually
  uses. BOTH tools are watched because the live 2026-08-28 rehearsal showed
  the model reaches skills through the gateway-native ``skill_view``, never
  ``read_file`` — a marker watching only the tool the docs named was inert on
  the real turn.

Ambiguity posture INVERTS ``claim_unbound``'s exactly-one rule: with two fresh
unclaimed routes, ALL fresh claimant sessions are marked. Over-applying a read
gate costs at worst one recoverable refusal on a non-review session;
under-applying breaches a signed commitment. A route that expires unclaimed is
logged at warning so "the gate never attached" is observable, not silent.

COUNTING. Distinct documents only: the connector reports the WHOLE document's
``pageCount`` on every windowed read, so a document counts once no matter how
many windows the model pulls. Non-PDF reads (no ``pageCount``) estimate pages
as ``ceil(total_chars / 3000)``. A read carrying neither signal counts zero
and is tracked as unmeasured — the envelope comes from the connector, not the
model, so absence is a defect to surface, never a talk-past vector. The
accumulated figure is "pages of documents read in the course of this review"
(the agreement's Exhibit A wording is aligned to exactly this measure); it is
NOT a claim about the served set's total size, and the refusal directive says
"at least N pages" for that reason.

PERSISTENCE — per-session, in memory, decided deliberately (v1): a seat
restart or a webhook redelivery grants a fresh budget. Compensating controls:
the webhook throttle, one audit row per crossing, and §2.7 usage monitoring.
If rehearsal or production shows retry amplification, the named follow-up is
an SQLite accumulator keyed (matter, window) beside the sticky-stop store.

FAIL POSTURES. Threshold unauthored → inert (a contract term, not a safety
floor — pilot and unconfigured seats run ungated). Customer config unreadable
→ inert plus a warning. ``SMD_READ_VOLUME_GATE_MODE``: ``off`` | ``report`` |
``block``; unset or garbage = ``block`` (matching ``matter_gate`` mode
parsing). ``report`` accumulates and records ONE report-class audit row at the
crossing read — an artifact rehearsal can cite, not a log line — and never
blocks.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: The one skill this gate governs. Deliberately a constant, not config: the
#: agreement names the routine, and a config-driven skill list would let a
#: config slip widen or void a contract control.
GATED_SKILL = "opposing-response-deficiency-review"

#: The per-skill settings key authored in customer.yaml
#: (``personas[].skills[].settings.review_threshold_pages``).
SETTING_KEY = "review_threshold_pages"

#: The document-content read this gate counts and, past the threshold, refuses.
COUNTED_TOOL = "mcp_smokeball_read_document"

#: Estimated characters per page for reads carrying no pageCount (docx/plain).
FALLBACK_CHARS_PER_PAGE = 3000

_MAX_SESSIONS = 256
_UNBOUND_TTL_SECONDS = 180.0
_UNBOUND_MAX = 8


def mode() -> str:
    """``off`` | ``report`` | ``block``. Default ``block``, fail-closed on
    garbage — same parsing posture as ``matter_gate.multi_matter_mode``."""
    raw = (os.environ.get("SMD_READ_VOLUME_GATE_MODE") or "").strip().lower()
    if raw == "off":
        return "off"
    return "report" if raw == "report" else "block"


@dataclass
class _SessionVolume:
    review: bool = False
    pages_by_file: dict[str, int] = field(default_factory=dict)
    unmeasured: set[str] = field(default_factory=set)
    names_by_file: dict[str, str] = field(default_factory=dict)
    report_recorded: bool = False


_sessions: OrderedDict[str, _SessionVolume] = OrderedDict()

#: Dispatch-time routes whose session id was empty, awaiting claim at the next
#: pre_llm_call. Entries are (monotonic_ts,) — the skill is always GATED_SKILL
#: (non-gated routes are never enqueued), so the timestamp is the whole record.
_unbound_routes: deque[float] = deque()


def _state(session_id: str) -> _SessionVolume:
    existing = _sessions.get(session_id)
    if existing is None:
        existing = _SessionVolume()
        _sessions[session_id] = existing
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)
    else:
        _sessions.move_to_end(session_id)
    return existing


def reset() -> None:
    """Test seam. Clears every register."""
    _sessions.clear()
    _unbound_routes.clear()


#: Shape-only trace journal. WHY: three rehearsal rounds produced a silent gate
#: with no way to tell WHICH stage went dark (marking vs accumulation vs
#: evaluation) — the gateway logs to stdout this venture cannot read, and the
#: registers are process-internal. Every entry is structural (tool names, arg
#: KEYS, value TYPES, counts) and never document content, capped per process,
#: on tmpfs so a restart clears it. Permanent by design: the next silent-gate
#: diagnosis starts from this file instead of four blind deploy cycles.
_TRACE_PATH = "/tmp/read_volume_trace.jsonl"
_TRACE_CAP = 120
_trace_count = 0


def _trace(event: str, **fields: Any) -> None:
    global _trace_count
    try:
        if _trace_count >= _TRACE_CAP:
            return
        _trace_count += 1
        import json as _json

        with open(_TRACE_PATH, "a") as f:
            f.write(_json.dumps({"event": event, **fields}) + "\n")
    except Exception:  # noqa: BLE001 — tracing must never perturb the gate
        pass


def record_route(session_id: str, skill: str) -> None:
    """Webhook router: record that a dispatch routed to ``skill``.

    Only the gated skill is recorded. An empty session id (the common
    dispatch-time case) enqueues an unbound route claimed at the session's
    first pre_llm_call. Never raises."""
    try:
        if (skill or "").strip() != GATED_SKILL:
            return
        if session_id:
            _state(session_id).review = True
            return
        _unbound_routes.append(time.monotonic())
        while len(_unbound_routes) > _UNBOUND_MAX:
            _unbound_routes.popleft()
    except Exception:  # noqa: BLE001 — must never perturb routing
        logger.debug("read_volume: record_route failed", exc_info=True)


def claim_unbound_routes(session_id: str, now: float | None = None) -> None:
    """pre_llm_call: attach fresh unclaimed routes to this session.

    Inverts ``inbound.claim_unbound``'s exactly-one rule on purpose: EVERY
    fresh claimant session is marked while any fresh route is pending, because
    over-applying a read gate is recoverable and under-applying breaches the
    agreement. Expired routes are dropped WITH A WARNING — a route nobody
    claimed means the gate never attached to the review it was for, and that
    must be observable. Never raises."""
    try:
        ts = time.monotonic() if now is None else now
        while _unbound_routes and (ts - _unbound_routes[0]) > _UNBOUND_TTL_SECONDS:
            _unbound_routes.popleft()
            logger.warning(
                "read_volume: a %s route expired unclaimed — the read-volume "
                "gate did not attach to that review's session",
                GATED_SKILL,
            )
        if not session_id or not _unbound_routes:
            return
        # Mark, don't consume: the deque drains by TTL so a burst of routes can
        # mark a burst of sessions. One route marking two sessions over-applies,
        # which is the chosen direction.
        _state(session_id).review = True
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.debug("read_volume: claim_unbound_routes failed", exc_info=True)


def _as_payload(result: Any) -> Any:
    if isinstance(result, (dict, list)):
        return result
    if isinstance(result, str):
        text = result.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                import json

                return json.loads(text)
            except Exception:  # noqa: BLE001
                return None
    return None


def _walk_read_fields(payload: Any) -> dict[str, Any] | None:
    """Find the read envelope's fields at whatever wrapping depth they sit.

    LIVE-CAUGHT (pilot rehearsal round 3, 2026-08-28; the same trap
    hermes-smd-establishment documented on 2026-08-11): the hook's ``result``
    is ``{"result": "<the connector's JSON, as a string>"}`` — the fields live
    inside a NESTED JSON STRING. A walker that only descends dicts and lists
    finds nothing, records nothing, and the gate stays silent while the model
    genuinely reads past the threshold. So string values that look like JSON
    are parsed and descended too, bounded by the same node budget."""
    stack = [payload]
    depth = 0
    while stack and depth < 200:
        depth += 1
        node = stack.pop()
        if isinstance(node, str):
            text = node.strip()
            if text.startswith("{") or text.startswith("["):
                try:
                    import json

                    stack.append(json.loads(text))
                except Exception:  # noqa: BLE001
                    pass
            continue
        if isinstance(node, dict):
            if "fileId" in node or "file_id" in node:
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def note_read(session_id: str, tool_name: str, args: Any, result: Any) -> None:
    """post_tool_call: the two mechanical observations this gate rides.

    1. A ``read_file`` of the gated skill's SKILL.md marks the session as a
       review (the spine path).
    2. A counted document read accumulates pages for a marked-or-unmarked
       session (accumulation is unconditional so a session marked AFTER its
       first read still carries the full count). Never raises."""
    try:
        if not session_id:
            if tool_name in ("read_file", "skill_view", COUNTED_TOOL):
                _trace("no_session", tool=tool_name)
            return
        if tool_name in ("read_file", "skill_view"):
            blob = ""
            if isinstance(args, dict):
                blob = " ".join(str(v) for v in args.values())
            elif isinstance(args, str):
                blob = args
            marked = GATED_SKILL in blob and (tool_name == "skill_view" or "SKILL.md" in blob)
            if marked:
                _state(session_id).review = True
            _trace(
                "skill_read",
                tool=tool_name,
                marked=marked,
                arg_keys=sorted(args.keys()) if isinstance(args, dict) else type(args).__name__,
                gated_in_blob=GATED_SKILL in blob,
                session=session_id[:24],
            )
            return
        if tool_name != COUNTED_TOOL:
            return
        # Strip the inbound quarantine fence FIRST. Hermes v0.20.4 inverted the
        # hook order (ss#2444: transform_tool_result now fires BEFORE
        # post_tool_call), so this consumer receives the FENCED text — round-5
        # rehearsal trace: result_head "[UNTRUSTED INBOUND DATA…". unwrap is
        # pass-through on unfenced input, so this is correct under both orders.
        if isinstance(result, str):
            from shared.inbound import unwrap_inbound

            result = unwrap_inbound(result)
        payload = _as_payload(result)
        fields = _walk_read_fields(payload) if payload is not None else None
        if fields is None:
            _trace(
                "counted_read_unparsed",
                result_type=type(result).__name__,
                result_head=(
                    result[:60]
                    if isinstance(result, str)
                    else sorted(result.keys())[:8]
                    if isinstance(result, dict)
                    else None
                ),
                session=session_id[:24],
            )
            return
        file_id = str(fields.get("fileId") or fields.get("file_id") or "")
        if not file_id:
            _trace(
                "counted_read_no_fileid",
                field_keys=sorted(fields.keys())[:10],
                session=session_id[:24],
            )
            return
        state = _state(session_id)
        if file_id in state.pages_by_file or file_id in state.unmeasured:
            return  # distinct documents count once; windows re-report the whole doc
        pages = fields.get("pageCount")
        if pages is None:
            pages = fields.get("page_count")
        if isinstance(pages, (int, float)) and pages > 0:
            state.pages_by_file[file_id] = int(pages)
        else:
            total_chars = fields.get("total_chars")
            if total_chars is None:
                total_chars = fields.get("totalChars")
            if isinstance(total_chars, (int, float)) and total_chars > 0:
                state.pages_by_file[file_id] = max(
                    1, math.ceil(float(total_chars) / FALLBACK_CHARS_PER_PAGE)
                )
            else:
                # No volume signal at all: count zero, remember the defect.
                state.unmeasured.add(file_id)
                logger.warning(
                    "read_volume: document %s carried neither pageCount nor "
                    "total_chars; counted as zero pages (unmeasured)",
                    file_id,
                )
        name = fields.get("name")
        if isinstance(name, str) and name:
            state.names_by_file[file_id] = name
        _trace(
            "counted_read",
            file_id=file_id[:12],
            pages=state.pages_by_file.get(file_id),
            total=sum(state.pages_by_file.values()),
            review=state.review,
            session=session_id[:24],
        )
    except Exception as exc:  # noqa: BLE001 — hook callbacks must be exception-safe
        _trace("note_read_error", error=str(exc)[:120])
        logger.debug("read_volume: note_read failed", exc_info=True)


def _threshold() -> int | None:
    """The authored threshold for the gated skill, or ``None`` (gate inert).

    Read live from the volume config on every evaluation (ADR 0044). Inert on
    an unreadable config WITH a warning: this is a billing-scope contract term,
    not a safety floor — failing closed would brick the review routine on any
    config hiccup, the asymmetry the matter-gate's fail-closed posture does not
    share."""
    try:
        from shared.customer_config import CustomerConfig

        for persona in CustomerConfig.from_volume().personas:
            for skill in persona.get("skills") or []:
                if not isinstance(skill, dict):
                    continue
                if (skill.get("name") or "").strip() != GATED_SKILL:
                    continue
                settings = skill.get("settings")
                if not isinstance(settings, dict):
                    return None
                raw = settings.get(SETTING_KEY)
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    return None
                value = int(raw)
                return value if value > 0 else None
        return None
    except Exception:  # noqa: BLE001
        logger.warning(
            "read_volume: customer config unreadable; gate inert this call",
            exc_info=True,
        )
        return None


@dataclass(frozen=True)
class Verdict:
    """What the fence should do with this read. At most one of the two message
    fields is set. ``report`` is returned ONCE per session (the crossing)."""

    refusal: str | None = None
    report: str | None = None


def _observed(state: _SessionVolume) -> tuple[int, int, str]:
    total = sum(state.pages_by_file.values())
    docs = len(state.pages_by_file) + len(state.unmeasured)
    names = [state.names_by_file.get(f, f) for f in list(state.pages_by_file)[:8]]
    listing = "; ".join(names)
    if state.unmeasured:
        listing += f" (+{len(state.unmeasured)} document(s) with no volume signal)"
    return total, docs, listing


def evaluate_read(session_id: str, tool_name: str) -> Verdict:
    """pre_tool_call: the gate. Evaluate always; the caller acts on the verdict.

    Refuses the counted read that would carry a REVIEW session past the
    authored threshold. The directive names only observed numbers ("at least
    N pages") and the one remedy the model can perform — the surface-only
    internal note. Fail-open on an unresolvable session id (no key, no
    accumulator; same documented hole as the matter-mixing fence)."""
    try:
        gate_mode = mode()
        if gate_mode == "off":
            return Verdict()
        if not session_id or tool_name != COUNTED_TOOL:
            return Verdict()
        state = _sessions.get(session_id)
        if state is None or not state.review:
            _trace(
                "evaluate_pass",
                why="no_state" if state is None else "not_review",
                session=session_id[:24],
                known_sessions=len(_sessions),
            )
            return Verdict()
        threshold = _threshold()
        if threshold is None:
            _trace("evaluate_pass", why="no_threshold", session=session_id[:24])
            return Verdict()
        total, docs, listing = _observed(state)
        if total < threshold:
            _trace(
                "evaluate_pass",
                why="under",
                total=total,
                threshold=threshold,
                session=session_id[:24],
            )
            return Verdict()
        _trace(
            "evaluate_crossing",
            total=total,
            threshold=threshold,
            mode=gate_mode,
            session=session_id[:24],
        )
        message = (
            f"this review has read at least {total} pages across {docs} "
            f"document(s) ({listing}), reaching the firm's authored review "
            f"threshold of {threshold} pages. Do not read further documents "
            "for this review. Write the internal note instead: name the served "
            f"set, state that at least {total} pages across {docs} documents "
            "were observed, and state that the review was suspended at the "
            "authored threshold rather than completed, so the assigned person "
            "can decide how to proceed."
        )
        if gate_mode == "report":
            if state.report_recorded:
                return Verdict()
            state.report_recorded = True
            return Verdict(report=message)
        return Verdict(refusal=message)
    except Exception:  # noqa: BLE001 — must never perturb the read path
        logger.debug("read_volume: evaluate_read failed", exc_info=True)
        return Verdict()
