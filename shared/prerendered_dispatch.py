"""Out-of-turn dispatch of a routine's pre-rendered outbound (WS-RENDER).

THE GAP THIS CLOSES (the 2026-08-24..31 outbound review). A cron routine's
``pre_run.py`` already computed every value its morning alert carries, and the
model still re-composed the WORDS each run — format drift, field names
reaching readers, run-on bodies, recipient flapping. The durable fix: the
pre_run renders EVERYTHING (recipients, subject, full body, an authored
identifier-free skeleton, the ledger appends a successful send earns) into a
consume-once envelope, and THIS module dispatches it at ``pre_llm_call`` —
out of turn, deterministic, ``templated=True``, through the FULL gate. The
model composes nothing; its turn is reduced to the residual duties the
injected context note names.

THE ENVELOPE: ``$HERMES_HOME/.smd/pre_run/<skill>.dispatch.json``, written by
the skill's pre_run beside the provenance handoff. Binding, freshness and
consume-once semantics are the handoff's (:mod:`shared.pre_run_handoff`):
file recency on the reader's clock (never the cron session stamp — the
2026-08-24 defect-B lesson), persona-home-first root walk (defect A), renamed
to ``<skill>.dispatch.consumed.json`` BEFORE dispatch so a retry or a peer
thread finds nothing. The ``.smd`` fence makes it unwritable from inside a
turn — an envelope the model can author is not an envelope.

IT DOES NOT BYPASS ANYTHING. Every dispatch goes through
:func:`shared.send_dispatch.dispatch` — the trust plugin's published sender,
which re-authorizes through the same ``evaluate_tool_call`` a model's own
send faces (ceiling, taint gate, content floor, fabrication scan, identifier
gate). The fallback ladder per dispatch:

1. full body refused by a gate -> dispatch the authored skeleton (still
   ``templated=True``, still the full gate);
2. skeleton also refused, or transport failure -> NOTHING sends; the context
   note tells the turn to follow its skill's failure instruction, and the
   heartbeat's no-send pager is the backstop.

Two authored postures are DISPOSITIONS, not failures, and they are distinct:

* **Confirm-withheld** ("withheld pending ..."): the send is captured pending
  the owner's approval — no skeleton, no appends, the note says it is held.
  Two posture-dependent gaps, stated rather than papered over (both dormant on
  the pilot, whose internal sends are autonomous): the pending slot is SINGLE
  (``PENDING_SEND.capture`` supersedes), so with several recipient sets only
  the last stays queued and the note says so; and an approved-after-withhold
  delivery goes down ``_dispatch_approved_send``, which knows nothing of this
  envelope's append plan — the raise rows are NOT written for that delivery.
  Recovery is the ledger's own re-fire property (the items surface again next
  run and the next successful full dispatch records them); persisting the
  append plan alongside the pending send is the structural fix if a seat ever
  authors ``external_send_internal: confirm``.
* **Draft-routed** ("routing to draft ..."): under a review posture the
  reviewed DRAFT is the delivery — nothing sends and nothing is queued. No
  skeleton, no failure note, no appends; the note tells the turn to compose
  the one draft for review from its Script Output.

APPENDS ARE POST-DISPATCH AND FULL-BODY ONLY. After a successful FULL send,
this module writes the envelope's ``fired``/``chased``/``handed_off`` events
through the broker's ``escalation_event_append`` verb with the SAME resolved
session id the send row carries, so the broker's send witness joins them
deterministically. A skeleton delivery appends NOTHING — the per-item codes
never reached a person, and the items re-fire next run by the ledger's own
re-fire property (annoying, never dangerous). The derive-handle discipline
(ss #2304) guards MODEL-supplied identity; this module is deterministic code
replaying keys the pre_run computed into a tamper-fenced file, which is the
identity source that discipline exists to force.

Context strings injected into the model NEVER name a gate or a rule
(corrective action only): a refusal message is machinery vocabulary the model
must not learn to negotiate with.

Exception-safe throughout: every failure degrades to "nothing dispatched,
note says so (or no note at all)", never a broken hook.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
from collections import OrderedDict

from shared import cron_attribution, escalation_ledger, pre_run_handoff, provenance, send_dispatch

logger = logging.getLogger(__name__)


def canonical_body_sha256(text: str) -> str:
    """The ONE body hash of the send-render conformance chain: sha256 over
    utf-8 of (CRLF -> LF, per-line trailing whitespace stripped, trailing
    newlines stripped). Stamped by the skills' pre_run (envelope +
    EMITTED_WAKE), by the trust plugin's dispatch (CONFIRM row, pre-mutation),
    and recomputed by the console verifier. The arbiter every implementation
    is tested against: tests/fixtures/body-canon-vectors.json (mirrored
    verbatim from ss-console operator/contracts/fixtures/)."""
    normalized = text.replace("\r\n", "\n")
    lines = [line.rstrip(" \t") for line in normalized.split("\n")]
    return hashlib.sha256("\n".join(lines).rstrip("\n").encode("utf-8")).hexdigest()


_SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"
_APPEND_TIMEOUT_SECONDS = 10

# Validation bounds: the envelope is trusted state, but a bug (or a probe)
# must not turn the hook into an unbounded sender.
_MAX_DISPATCHES = 10
_MAX_APPENDS = 200
_MAX_RECIPIENTS = 20
_MAX_BODY_CHARS = 131_072
_MAX_SUBJECT_CHARS = 500

_ALLOWED_EVENTS = frozenset({"fired", "chased", "handed_off"})

#: In-turn template declarations from consumed envelopes, keyed by resolved
#: session id, for :mod:`shared.rendered_body_gate`. Bounded like the trust
#: plugin's handoff-seeded set.
_IN_TURN: OrderedDict[str, dict] = OrderedDict()
_MAX_IN_TURN = 64


def envelope_path(skill: str, hermes_home: str | None = None, persona: str | None = None):
    """Where ``skill``'s dispatch envelope lives (beside its handoff)."""
    return pre_run_handoff.handoff_dir(hermes_home, persona) / (f"{_safe(skill)}.dispatch.json")


def consumed_path(skill: str, hermes_home: str | None = None, persona: str | None = None):
    return pre_run_handoff.handoff_dir(hermes_home, persona) / (
        f"{_safe(skill)}.dispatch.consumed.json"
    )


def _safe(skill: str) -> str:
    return pre_run_handoff._safe_skill(skill)


def in_turn_templates(session_id: str) -> dict | None:
    """The consumed envelope's in-turn declaration for this session, or None.

    ``{"enforce": bool, "templates": [{"name", "template", "slots"}...]}`` —
    consulted by the rendered-body check on gated send tools."""
    return _IN_TURN.get(session_id)


def _valid_dispatch(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    recipients = entry.get("recipients")
    if not isinstance(recipients, list) or not recipients or len(recipients) > _MAX_RECIPIENTS:
        return False
    if not all(isinstance(r, str) and r.strip() for r in recipients):
        return False
    subject = entry.get("subject")
    full = entry.get("full_body")
    skeleton = entry.get("skeleton_body")
    if not (isinstance(subject, str) and subject.strip() and len(subject) <= _MAX_SUBJECT_CHARS):
        return False
    if not (isinstance(full, str) and full.strip() and len(full) <= _MAX_BODY_CHARS):
        return False
    if skeleton is not None and not (
        isinstance(skeleton, str) and len(skeleton) <= _MAX_BODY_CHARS
    ):
        return False
    appends = entry.get("appends", [])
    if not isinstance(appends, list) or len(appends) > _MAX_APPENDS:
        return False
    for append in appends:
        if not isinstance(append, dict):
            return False
        if append.get("event") not in _ALLOWED_EVENTS:
            return False
        if not isinstance(append.get("item_key"), str) or not append["item_key"]:
            return False
    return True


def take_envelope(
    skill: str,
    session_started_at,
    hermes_home: str | None = None,
    persona: str | None = None,
    now=None,
) -> dict | None:
    """The validated envelope this session may dispatch, or None — same
    binding rules as :func:`pre_run_handoff.take_handoff` (cron session only,
    recency window, persona-first roots, consume BEFORE use)."""
    try:
        if session_started_at is None:
            return None
        path = raw = None
        found_persona = None
        for candidate_persona in [persona, None] if persona else [None]:
            candidate = envelope_path(skill, hermes_home, candidate_persona)
            try:
                raw = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            path = candidate
            found_persona = candidate_persona
            break
        if path is None or raw is None:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            logger.warning("prerendered_dispatch: %s is not valid JSON; ignoring", path)
            return None
        if not isinstance(payload, dict) or payload.get("skill") != skill:
            return None
        started_at = pre_run_handoff._parse_iso_aware(payload.get("started_at"))
        if started_at is None:
            return None
        from datetime import datetime, timezone

        moment = now if now is not None else datetime.now(timezone.utc)
        age = moment - started_at
        if age > pre_run_handoff.DEFAULT_WINDOW or age < -pre_run_handoff._MAX_CLOCK_SKEW:
            logger.info(
                "prerendered_dispatch: %s is not fresh (started_at=%s); leaving it in place",
                path,
                payload.get("started_at"),
            )
            return None
        dispatches = payload.get("dispatches")
        if not isinstance(dispatches, list) or len(dispatches) > _MAX_DISPATCHES:
            return None
        if not all(_valid_dispatch(d) for d in dispatches):
            logger.warning(
                "prerendered_dispatch: %s carries a malformed dispatch entry; refusing the whole envelope",
                path,
            )
            return None
        # Consume BEFORE dispatch: an envelope that stayed claimable after a
        # partial dispatch would double-send on the next turn.
        try:
            os.replace(path, consumed_path(skill, hermes_home, found_persona))
        except OSError as exc:
            logger.warning(
                "prerendered_dispatch: could not consume %s (%s); refusing to dispatch from a "
                "still-claimable envelope",
                path,
                exc,
            )
            return None
        return payload
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.warning("prerendered_dispatch: take failed for %r: %s", skill, exc)
        return None


# ---------------------------------------------------------------------------
# The ledger appends (post-dispatch, full-body only)
# ---------------------------------------------------------------------------


def _broker_request(payload: dict) -> dict:
    socket_path = os.environ.get(_SOCKET_ENV, "")
    if not socket_path:
        raise RuntimeError("broker socket unset")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_APPEND_TIMEOUT_SECONDS)
        sock.connect(socket_path)
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        raw = b""
        while not raw.endswith(b"\n"):
            chunk = sock.recv(65_536)
            if not chunk:
                break
            raw += chunk
    return json.loads(raw.decode("utf-8"))


def _write_appends(skill: str, appends: list, session_id: str) -> tuple[int, int]:
    """Append each event through the broker's validated verb. Returns
    (written, attempted). A refused or failed append is logged and skipped —
    the item re-fires next run; never a raised exception into the hook."""
    written = 0
    attempted = 0
    for entry in appends[:_MAX_APPENDS]:
        attempted += 1
        try:
            event = escalation_ledger.make_event(
                skill=skill,
                matter_id=entry.get("matter_id"),
                item_key=str(entry["item_key"]),
                event=str(entry["event"]),
                attempt=int(entry.get("attempt") or 1),
                token=entry.get("token"),
            )
            # The witness key: the broker joins the raise to the send row it
            # just wrote on this same resolved session id (ss#2603).
            event["session_id"] = session_id
            response = _broker_request({"action": "escalation_event_append", "event": event})
            if isinstance(response, dict) and response.get("ok"):
                written += 1
            else:
                logger.warning(
                    "prerendered_dispatch: append refused for %s (%s)",
                    entry.get("item_key"),
                    response,
                )
        except Exception as exc:  # noqa: BLE001 — one bad append must not lose the rest
            logger.warning(
                "prerendered_dispatch: append failed for %s (%s)", entry.get("item_key"), exc
            )
    return written, attempted


# ---------------------------------------------------------------------------
# The dispatch pass
# ---------------------------------------------------------------------------


# The two authored-posture dispositions, told apart from a refusal by the
# ceiling's own reason phrases. Substrings because the decision object's
# ``audit_action`` (await_approval vs draft) does not cross the block-dict
# boundary — ``evaluate_tool_call`` returns only ``{"action", "message"}`` —
# and these two phrases are the ceiling's stable vocabulary for exactly these
# two decisions (enforce._await_approval / enforce._draft + the content
# floor's draft routing). Tested against the real reason texts.
_CONFIRM_WITHHELD_MARK = "withheld pending"
_DRAFT_ROUTED_MARK = "routing to draft"


def _looks_confirm_withheld(reason: str) -> bool:
    """The CONFIRM ceiling: the send is captured pending the owner's approval —
    a real round-trip. No skeleton, no appends; approval delivers it."""
    return _CONFIRM_WITHHELD_MARK in (reason or "").lower()


def _looks_draft_routed(reason: str) -> bool:
    """The DRAFT_FOR_REVIEW posture (or a content-floor draft routing): under
    this posture the reviewed DRAFT is the delivery — nothing is captured and
    nothing sends autonomously. Its own disposition: no skeleton, no failure
    note, no appends; the turn composes the one draft for review."""
    return _DRAFT_ROUTED_MARK in (reason or "").lower()


def _recipients_phrase(recipients) -> str:
    return ", ".join(recipients) if recipients else "(no recipient)"


def dispatch_prerendered(session_id: str) -> str | None:
    """Dispatch this cron session's pre-rendered envelope, if one binds.

    Returns the context note to inject (so the turn knows what happened and
    what remains), or None when there is nothing to say (not a cron session,
    no envelope, window missed). Never raises."""
    try:
        routine = cron_attribution.resolve_routine(session_id)
        if routine is None or not routine.skill:
            return None
        started_at = cron_attribution.parse_cron_session_started_at(session_id)
        if started_at is None:
            return None
        envelope = take_envelope(routine.skill, started_at, persona=routine.persona)
        if envelope is None:
            return None
        resolved = provenance.resolve_session(session_id)

        in_turn = envelope.get("in_turn")
        if isinstance(in_turn, list) and in_turn:
            _IN_TURN[resolved] = {
                "enforce": bool(envelope.get("in_turn_enforce", True)),
                "templates": [t for t in in_turn if isinstance(t, dict)],
            }
            while len(_IN_TURN) > _MAX_IN_TURN:
                _IN_TURN.popitem(last=False)

        lines: list[str] = []
        appended_total = 0
        confirm_withheld = 0
        for entry in envelope.get("dispatches") or []:
            recipients = [str(r) for r in entry["recipients"]]
            # ``skill_name`` (ss-console claims review 2026-09-04, B3): the
            # routine this session IS, resolved above from the cron session id
            # -- a scheduler fact, not something the agent asserted. It rides
            # audit_extra through the broker's closed allowlist and lands on the
            # CONFIRM row's skill_name COLUMN (the broker moves it there), which
            # is the half of the console's wake<->confirm join that was NULL on
            # every live row: EMITTED_WAKE carried the column, the dispatch did
            # not, and `declares.get("")` graded nothing. Stamped on the full
            # send and the skeleton fallback alike -- both are this routine's.
            # Deploy order: the broker's allowlist is a SILENT closed list, so
            # the ss-console half lands first; on an older broker this key is
            # dropped without error and the row is what it was.
            audit_base = {"skill_name": routine.skill}
            if isinstance(entry.get("routing_leg"), str) and entry["routing_leg"]:
                audit_base["routing_leg"] = entry["routing_leg"]
            result = send_dispatch.dispatch(
                to=recipients,
                subject=str(entry["subject"]),
                text=str(entry["full_body"]),
                session_id=session_id,
                cc=[str(c) for c in (entry.get("cc") or []) if isinstance(c, str)],
                templated=True,
                audit_extra={**audit_base, "body_variant": "full"},
            )
            who = _recipients_phrase(result.recipients or tuple(recipients))
            if result.sent:
                written, attempted = _write_appends(
                    routine.skill, entry.get("appends") or [], resolved
                )
                appended_total += written
                note = (
                    f"Your {routine.skill} alert was already delivered to {who} "
                    f"(message {result.message_id})"
                )
                if attempted:
                    note += f"; {written} of {attempted} item record(s) written"
                lines.append(note + ". Do not send this alert and do not record these items again.")
                continue
            if _looks_confirm_withheld(result.reason):
                # Held for the owner's approval — a legitimate authored
                # posture. No skeleton, no appends; the approval round-trip
                # owns delivery from here. (Two honesty limits below the loop:
                # the pending slot is single, and an approved delivery writes
                # no item records — see the module docstring.)
                confirm_withheld += 1
                lines.append(
                    f"Your {routine.skill} alert to {who} is being held for the owner's "
                    "approval. Do not compose, resend, or record anything for it."
                )
                continue
            if _looks_draft_routed(result.reason):
                # This seat's review posture: the reviewed draft IS the
                # delivery. Nothing was sent and nothing is queued — the turn
                # composes the ONE draft for a person to review. No skeleton,
                # no failure note, no item records (nothing reached a person;
                # the items re-fire next run if the draft never sends).
                lines.append(
                    f"Your {routine.skill} alert to {who} was not sent and is to be "
                    "routed to a draft for review instead: compose ONE draft of this "
                    "alert from your Script Output's digest, following your skill's "
                    "format reference exactly, for a person to review and send. Do "
                    "not send it yourself and do not record any items."
                )
                continue
            skeleton = entry.get("skeleton_body")
            if isinstance(skeleton, str) and skeleton.strip():
                fallback = send_dispatch.dispatch(
                    to=recipients,
                    subject=str(entry["subject"]),
                    text=skeleton,
                    session_id=session_id,
                    cc=[str(c) for c in (entry.get("cc") or []) if isinstance(c, str)],
                    templated=True,
                    audit_extra={**audit_base, "body_variant": "skeleton"},
                )
                if fallback.sent:
                    # No appends: the per-item codes never reached a person;
                    # the items re-fire next run by design.
                    lines.append(
                        f"A reduced {routine.skill} alert was delivered to {who} "
                        f"(message {fallback.message_id}); the full details will be "
                        "retried on the next run. Do not compose or resend anything, "
                        "and do not record any items."
                    )
                    continue
            lines.append(
                f"Your {routine.skill} alert to {who} could not be delivered this run. "
                "Follow your skill's failure instruction: report the failure in one "
                "plain line, and send nothing else."
            )
        if confirm_withheld > 1:
            # HONESTY (single pending slot): PENDING_SEND keeps ONE send — a
            # later capture supersedes the earlier — so of N withheld alerts
            # only the last is actually queued for approval. Say so; the
            # superseded sets re-fire next run by the ledger's own property.
            lines.append(
                "Only the most recently held alert remains queued for approval; "
                "the earlier held alerts were superseded and their items will "
                "surface again on the next run."
            )
        memo_matters = [
            str(m) for m in (envelope.get("memo_matters") or []) if isinstance(m, str) and m
        ]
        if memo_matters:
            lines.append(
                "Flag each of these matters in place with a memo naming the alert and "
                "the unassigned state (no direct delivery is pending for them): "
                + ", ".join(memo_matters)
                + "."
            )
        unroutable = envelope.get("unroutable") or []
        if unroutable and not memo_matters:
            names = [
                str(u.get("matter_number") or u.get("matter_id") or "")
                for u in unroutable
                if isinstance(u, dict)
            ]
            names = [n for n in names if n]
            if names:
                lines.append(
                    "These matters had no reachable recipient; flag each in place with "
                    "a memo: " + ", ".join(names) + "."
                )
        if not lines:
            return None
        logger.info(
            "prerendered_dispatch: %s — %d dispatch(es) processed, %d append(s) written",
            routine.skill,
            len(envelope.get("dispatches") or []),
            appended_total,
        )
        return "[" + " ".join(lines) + "]"
    except Exception:  # noqa: BLE001 — hook callers must never see a raise
        logger.warning("prerendered_dispatch: dispatch pass failed", exc_info=True)
        return None


__all__ = [
    "consumed_path",
    "dispatch_prerendered",
    "envelope_path",
    "in_turn_templates",
    "take_envelope",
]
