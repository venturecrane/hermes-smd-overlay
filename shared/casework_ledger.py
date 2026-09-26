"""Casework ledger: the Operator's record of what it proposed, what a person
decided, and what it then did to the firm's task list (case-manager spec,
``docs/specs/operator/case-manager-deadline-work.md``).

CANONICAL SOURCE: ``operator/workspace_broker/casework_ledger.py``. Byte-identical
copies live in the skills that read it (``operator/tests/test_casework_ledger_sync.py``)
and in the overlay at ``shared/casework_ledger.py`` (pinned by
``operator/contracts/overlay-pairs.json``). Edit here, restamp the copies.

Separate from the escalation ledger: an escalation row silences an alarm, a
casework row authorizes a WRITE to the firm's record. They share the broker
(sole writer) and the ``item_key`` arithmetic, nothing else.

Record shape (one JSON object per line)::

    {"v":1,"ts":<iso, broker>,"id":<ulid, broker>,"skill":<str>,
     "matter_id":<str>,"kind":<task|date>,"source_id":<Smokeball task/event id>,
     "item_key":<hex16>,"event":<EVENTS>,"session_id":<str|absent>,
     # raises (proposed|named|briefed):
     "n":<1..999>,"dispatch_ref":<hex32>,"thread_ref":<broker-stamped>,
     "payload":{"action","class","staff_id","to_staff_id","reason","evidence",
                "step":{"catalog_id","skill","level","params"}},
     # closed_by_record: "payload" (action close, class done, evidence required)
     # approved|held|step_started: "n","thread_ref", approved|held: "decided_by"
     # completed: "tool_call_id" (joins the update_task TOOL_CALL_COMPLETED row)
     # step_ran: "payload" (action step, level handles), "tool_call_id" (joins
     #   the create_memo TOOL_CALL_COMPLETED row the step's routine filed)
     # write_failed: "error"}

The doors ``validate_append`` keeps shut, each refused by name:

* a close proposed, or closed by record, on an item classed ``at_stake``;
* a raise (or a ``mentioned``) the broker did not witness reaching a person;
* a raise with no thread (the broker could not tie it to its own send);
* a verdict with no raise on the same thread and number;
* an outcome with no open authorization (an approved line or a
  closed_by_record), or a ``completed`` with no successful update_task call;
* a ``step_ran`` that is not a date item's step at ``handles``, or that names
  no successful create_memo call of its own session (every prep routine files
  its ``[Operator]`` memo on the matter, so a step that left none did not run);
* an ``item_key`` not derived from the row's own matter, kind and source id.

Pure stdlib with no broker imports, so vendored copies load standalone.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

SCHEMA_VERSION = 1

ITEM_KINDS: tuple[str, ...] = ("task", "date")
_NAMESPACE: dict[str, str] = {"task": "__cm_task:", "date": "__cm_date:"}

RAISING_EVENTS: tuple[str, ...] = ("proposed", "named", "briefed")
VERDICT_EVENTS: tuple[str, ...] = ("approved", "held")
OUTCOME_EVENTS: tuple[str, ...] = ("completed", "write_failed")
EVENTS: tuple[str, ...] = (
    *RAISING_EVENTS,
    *VERDICT_EVENTS,
    *OUTCOME_EVENTS,
    "closed_by_record",
    "mentioned",
    "step_started",
    "kept",
    "step_ran",
)
#: Events that claim a message reached a person, so the broker must witness a send.
WITNESSED_EVENTS: tuple[str, ...] = (*RAISING_EVENTS, "mentioned")

ACTIONS: tuple[str, ...] = ("close", "keep", "reassign", "step")
LEVELS: tuple[str, ...] = ("surfaces", "prepares", "handles")
#: The actions an approval turns into an update_task write (a step runs a skill).
WRITE_ACTIONS: tuple[str, ...] = ("close", "reassign")
CLASSES: tuple[str, ...] = ("open", "done", "stale", "at_stake")
UPDATE_TASK_TOOL = "mcp_smokeball_update_task"
#: The write every prep routine makes when it runs a step: its ``[Operator]`` memo.
STEP_WITNESS_TOOL = "mcp_smokeball_create_memo"
#: The successful tool call that witnesses each call-backed event.
WITNESS_TOOLS: dict[str, str] = {"completed": UPDATE_TASK_TOOL, "step_ran": STEP_WITNESS_TOOL}

DEFAULT_LEDGER_PATH = "/opt/data/audit/casework-ledger.jsonl"
LEDGER_PATH_ENV = "SMD_CASEWORK_LEDGER_PATH"

MAX_ITEM_NUMBER = 999
_MAX_ID_CHARS = 128
_MAX_REASON_CHARS = 300
_MAX_EVIDENCE = 20
_MAX_EVIDENCE_CHARS = 200
_MAX_ERROR_CHARS = 500
_MAX_NAME_CHARS = 120
_DISPATCH_REF_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STEP_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_SKILL_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_COMMON = frozenset(
    {"v", "ts", "id", "skill", "matter_id", "kind", "source_id", "item_key", "event", "session_id"}
)
_ALLOWED: dict[str, frozenset[str]] = {
    **{k: _COMMON | {"n", "dispatch_ref", "thread_ref", "payload"} for k in RAISING_EVENTS},
    **{k: _COMMON | {"n", "thread_ref", "decided_by"} for k in VERDICT_EVENTS},
    "step_started": _COMMON | {"n", "thread_ref"},
    "closed_by_record": _COMMON | {"payload"},
    "completed": _COMMON | {"tool_call_id"},
    "write_failed": _COMMON | {"error"},
    "mentioned": _COMMON,
    "kept": _COMMON,
    "step_ran": _COMMON | {"payload", "tool_call_id"},
}
_PAYLOAD_KEYS = frozenset(
    {"action", "class", "staff_id", "to_staff_id", "reason", "evidence", "step"}
)


def ledger_path() -> str:
    """The casework ledger path: env override, else the agent-read default."""
    return os.environ.get(LEDGER_PATH_ENV) or DEFAULT_LEDGER_PATH


def _normalize_id(value) -> str:
    return "" if value is None else str(value).strip().casefold()


def item_key(*, matter_id, kind: str, source_id) -> str:
    """Stable key for one task or court date. Keyword-only so a transposed
    argument cannot mint a different item. Equal to
    ``escalation_ledger.item_key(matter_id, "__cm_<kind>:" + source_id, None, None)``:
    same normalization, same hash, an id namespace that cannot collide with a
    raw Smokeball id."""
    if kind not in ITEM_KINDS:
        raise ValueError(f"casework kind must be one of {ITEM_KINDS}, not {kind!r}")
    if not _normalize_id(source_id) or not _normalize_id(matter_id):
        raise ValueError("casework item_key needs a matter_id and a source_id read off the record")
    namespaced = _NAMESPACE[kind] + str(source_id).strip()
    raw = "\x1f".join((_normalize_id(matter_id), _normalize_id(namespaced), ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def serialize_event(event: dict) -> str:
    """One canonical JSON line (no trailing newline)."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)


def read_ledger(path: str | None = None) -> list[dict]:
    """Every parseable row, oldest first. Unparseable lines are skipped, never
    guessed at: a lost approval stays unapproved, so nothing is written on it. A
    missing file is an empty ledger."""
    try:
        with open(path or ledger_path(), encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in raw_lines:
        try:
            obj = json.loads(line) if line.strip() else None
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("event") in EVENTS and obj.get("item_key"):
            events.append(obj)
    return events


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """One numbered line on one message: a raise and what a person said to it."""

    thread_ref: str
    n: int
    raise_event: str
    payload: dict
    dispatch_ref: str | None = None
    raised_ts: str | None = None
    verdict: str | None = None
    verdict_ts: str | None = None
    decided_by: dict | None = None
    started: bool = False


@dataclass
class ItemState:
    """Ledger-derived state for one item_key.

    ``authorization`` is the open license to write ("approved" or
    "closed_by_record"); an outcome consumes it, so one approval authorizes one
    write. ``completed_via`` names which license the last completed write used;
    Job 3 mentions an item once when that is "closed_by_record".
    ``unmentioned_steps`` holds the date-prep steps the Operator ran itself
    (``step_ran``) that no message has told anyone about yet, each
    ``{"step": <payload.step>, "day": <date|None>}``; a ``mentioned`` clears it.
    """

    item_key: str
    matter_id: str | None = None
    kind: str | None = None
    source_id: str | None = None
    decisions: dict[tuple[str, int], Decision] = field(default_factory=dict)
    last_raise: Decision | None = None
    named: bool = False
    authorization: str | None = None
    authorized_decision: Decision | None = None
    record_payload: dict | None = None
    completed: bool = False
    completed_via: str | None = None
    last_completed_date: date | None = None
    write_failed: bool = False
    mentioned: bool = False
    kept: bool = False
    last_kept_date: date | None = None
    unmentioned_steps: list[dict] = field(default_factory=list)


def _ts_date(ts) -> date | None:
    try:
        return date.fromisoformat(str(ts)[:10])
    except ValueError:
        return None


def _slot(event: dict) -> tuple[str, int]:
    """(thread_ref, n) for a row; an unparseable n is 0, which no raise holds."""
    n = event.get("n")
    return (str(event.get("thread_ref") or ""), n if isinstance(n, int) and _valid_n(n) else 0)


def _fold(state: ItemState, event: dict) -> None:
    kind = event.get("event")
    ts = event.get("ts") if isinstance(event.get("ts"), str) else None
    slot = _slot(event)
    for attr in ("matter_id", "kind", "source_id"):
        if event.get(attr) is not None:
            setattr(state, attr, str(event.get(attr)))
    if kind in RAISING_EVENTS:
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        dispatch_ref = event.get("dispatch_ref")
        dispatch_ref = dispatch_ref if isinstance(dispatch_ref, str) else None
        decision = Decision(slot[0], slot[1], str(kind), payload, dispatch_ref, ts)
        state.decisions[slot] = decision
        state.last_raise = decision
        state.named = state.named or kind == "named"
        return
    decision = state.decisions.get(slot)
    if kind in VERDICT_EVENTS and decision is not None:
        decision.verdict, decision.verdict_ts = kind, ts
        decision.decided_by = (
            event.get("decided_by") if isinstance(event.get("decided_by"), dict) else None
        )
        if kind == "approved" and decision.payload.get("action") in WRITE_ACTIONS:
            state.authorization, state.authorized_decision = "approved", decision
    elif kind == "step_started" and decision is not None:
        decision.started = True
    elif kind == "closed_by_record":
        state.authorization, state.authorized_decision = "closed_by_record", None
        state.record_payload = (
            event.get("payload") if isinstance(event.get("payload"), dict) else None
        )
    elif kind in OUTCOME_EVENTS:
        if kind == "completed":
            state.completed, state.completed_via = True, state.authorization
            state.last_completed_date = _ts_date(ts)
            state.mentioned = False
        state.write_failed = kind == "write_failed"
        state.authorization, state.authorized_decision = None, None
    elif kind == "mentioned":
        state.mentioned = True
        state.unmentioned_steps = []
    elif kind == "step_ran":
        ran = event.get("payload")
        ran_step = ran.get("step") if isinstance(ran, dict) else None
        step = ran_step if isinstance(ran_step, dict) else {}
        state.unmentioned_steps.append({"step": step, "day": _ts_date(ts)})
    elif kind == "kept":
        state.kept, state.last_kept_date = True, _ts_date(ts)


def derive_state(events) -> dict[str, ItemState]:
    """Fold rows into per-item state in ``(ts, file order)`` order (stable sort)."""
    states: dict[str, ItemState] = {}
    for event in sorted(events, key=lambda e: str(e.get("ts") or "")):
        key = str(event.get("item_key") or "")
        if key:
            _fold(states.setdefault(key, ItemState(item_key=key)), event)
    return states


def pending_decisions(state: ItemState | None) -> list[Decision]:
    """Raised lines nobody has answered yet."""
    return [] if state is None else [d for d in state.decisions.values() if d.verdict is None]


def needs_mention(state: ItemState | None) -> bool:
    """A task the Operator closed on the record's evidence, or a date-prep step
    it ran itself, that it has not yet mentioned to anyone (Job 3: one line in
    the next message, never its own)."""
    if state is not None and state.unmentioned_steps:
        return True
    return bool(
        state
        and state.completed
        and state.completed_via == "closed_by_record"
        and not state.mentioned
    )


def is_kept_quiet(state: ItemState | None, today: date, keep_quiet_days: int) -> bool:
    """True while a person's "leave it" still holds."""
    if state is None or not state.kept or state.last_kept_date is None:
        return False
    return today < state.last_kept_date + timedelta(days=max(0, keep_quiet_days))


# ---------------------------------------------------------------------------
# Validation (broker-side)
# ---------------------------------------------------------------------------


def _valid_n(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_ITEM_NUMBER


def _short_str(value, limit: int, *, allow_none: bool = False) -> bool:
    if value is None:
        return allow_none
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _validate_step(step) -> None:
    """A step names one catalog entry the pre_run built: the skill that runs it,
    the level the firm set when it was offered, and bounded string parameters."""
    params = step.get("params", {}) if isinstance(step, dict) else None
    if (
        not isinstance(step, dict)
        or set(step) - {"catalog_id", "skill", "level", "params"}
        or not (isinstance(step.get("catalog_id"), str) and _STEP_RE.fullmatch(step["catalog_id"]))
        or not (isinstance(step.get("skill"), str) and _SKILL_RE.fullmatch(step["skill"]))
        or step.get("level") not in LEVELS
        or not isinstance(params, dict)
        or len(params) > 10
        or not all(isinstance(k, str) and _SKILL_RE.fullmatch(k) for k in params)
        or not all(_short_str(v, _MAX_EVIDENCE_CHARS) for v in params.values())
    ):
        raise ValueError(
            "payload.step is {catalog_id, skill, level, params}: catalog_id "
            "[A-Za-z0-9_.:-]{1,120}, "
            f"skill [a-z0-9_-]{{1,64}}, level one of {LEVELS}, params at most 10 string values"
        )


def _validate_payload(kind: str, payload, item: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"a {kind} row requires a payload object")
    unknown = sorted(set(payload) - _PAYLOAD_KEYS)
    if unknown:
        raise ValueError(
            f"payload carries unknown fields {unknown}; it holds only {sorted(_PAYLOAD_KEYS)}"
        )
    action, klass = payload.get("action"), payload.get("class")
    if action not in ACTIONS:
        raise ValueError(f"payload.action must be one of {ACTIONS}")
    if klass not in CLASSES:
        raise ValueError(f"payload.class must be one of {CLASSES}")
    if action == "close" and klass == "at_stake":
        raise ValueError(
            "refusing a close on an item classed at_stake: money or a court date is riding on it, "
            "so it is never closed without a person doing it in the record. Offer keep or "
            "reassign, "
            "or leave it with the escalator. Retrying will fail identically."
        )
    for key in ("staff_id", "to_staff_id"):
        if not _short_str(payload.get(key), _MAX_ID_CHARS, allow_none=True):
            raise ValueError(f"payload.{key} must be a staff id read off the record, or null")
    if action == "reassign" and payload.get("to_staff_id") is None:
        raise ValueError("a reassign names payload.to_staff_id, the staff member the task moves to")
    if (action == "step") != (payload.get("step") is not None):
        raise ValueError("payload.step is required when action is step, and only then")
    if payload.get("step") is not None:
        _validate_step(payload["step"])
    if not _short_str(payload.get("reason"), _MAX_REASON_CHARS, allow_none=True):
        raise ValueError(f"payload.reason must be 1..{_MAX_REASON_CHARS} characters, or null")
    evidence = payload.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > _MAX_EVIDENCE:
        raise ValueError(f"payload.evidence must be a list of at most {_MAX_EVIDENCE} atoms")
    if not all(_short_str(atom, _MAX_EVIDENCE_CHARS) for atom in evidence):
        raise ValueError(f"each evidence atom must be 1..{_MAX_EVIDENCE_CHARS} characters")
    _check_kind_shape(kind, payload, item)


def _check_kind_shape(kind: str, payload: dict, item: str) -> None:
    """The two row kinds whose payload has one legal shape: a step the
    Operator ran itself, and a task the record shows done."""
    action, klass = payload.get("action"), payload.get("class")
    evidence = payload.get("evidence", [])
    if kind == "step_ran" and (
        action != "step"
        or klass != "open"
        or item != "date"
        or payload["step"].get("level") != "handles"
    ):
        raise ValueError(
            "step_ran records a date-prep step the Operator ran itself: kind date, action step, "
            "class open, and a step at level handles. A step at any other level waits for a person."
        )
    if kind == "closed_by_record" and (
        action != "close" or klass != "done" or not evidence or item != "task"
    ):
        raise ValueError(
            "closed_by_record means the record shows a task is done: kind task, action close, "
            "class done, "
            "and the evidence atoms that show it. Anything else goes to a person as a proposal."
        )


def _validate_decided_by(decided_by) -> None:
    if decided_by is None:
        return
    if not isinstance(decided_by, dict) or set(decided_by) != {"name", "key"}:
        raise ValueError("decided_by holds exactly name and key")
    if not _short_str(decided_by.get("name"), _MAX_NAME_CHARS):
        raise ValueError(
            "decided_by.name is the firm's authored users[].full_name, 1..120 characters"
        )
    if not (isinstance(decided_by.get("key"), str) and _SHA256_RE.fullmatch(decided_by["key"])):
        raise ValueError("decided_by.key is the sha256 of the verified sender's canonical address")


def _validate_identity(event: dict) -> None:
    kind = event.get("event")
    if kind not in EVENTS:
        raise ValueError(f"unknown casework event {kind!r}; expected one of {EVENTS}")
    unknown = sorted(set(event) - _ALLOWED[kind])
    if unknown:
        raise ValueError(
            f"a {kind} row carries unknown fields {unknown}; drop them from this append"
        )
    if not _short_str(event.get("skill"), _MAX_ID_CHARS):
        raise ValueError("casework event requires a skill")
    if event.get("kind") not in ITEM_KINDS:
        raise ValueError(f"casework event requires kind, one of {ITEM_KINDS}")
    for key in ("matter_id", "source_id"):
        if not _short_str(event.get(key), _MAX_ID_CHARS):
            raise ValueError(f"casework event requires {key}, read off the record")
    derived = item_key(
        matter_id=event["matter_id"], kind=event["kind"], source_id=event["source_id"]
    )
    if event.get("item_key") != derived:
        raise ValueError("item_key must be derived from this row's matter_id, kind and source_id")


def _witnessed(kind: str, send_witness, event: dict) -> None:
    if not callable(send_witness) or not send_witness(event):
        raise ValueError(
            f"refusing to record a {kind}: the broker dispatched no message to a person in this "
            "session. Record it only after a send succeeds; a memo, a task or a draft is not a "
            "delivery. Retrying will fail identically until a send succeeds."
        )


def _check_raise(kind: str, event: dict, state: ItemState | None) -> None:
    dispatch_ref = event.get("dispatch_ref")
    if (
        not _valid_n(event.get("n"))
        or not (isinstance(dispatch_ref, str) and _DISPATCH_REF_RE.fullmatch(dispatch_ref))
        or not _short_str(event.get("thread_ref"), 512)
    ):
        raise ValueError(
            f"a {kind} must carry its line number n, the send's dispatch_ref, and the thread "
            "the broker stamped from its own send record. The broker could not tie this raise "
            "to a message it sent in this session, so a reply could never find it. Write nothing."
        )
    if kind == "named" and state is not None and state.named:
        raise ValueError("this item was already handed over once; a handover is never repeated")


def _check_answer(kind: str, event: dict, state: ItemState | None) -> None:
    slot = _slot(event)
    decision = state.decisions.get(slot) if state is not None and slot[0] and slot[1] else None
    if decision is None:
        raise ValueError(
            f"refusing a {kind}: no line {event.get('n')!r} was raised for this item on that "
            "thread, so the reply answers nothing this item was asked. Write nothing; ask the "
            "person."
        )
    if kind == "step_started":
        if decision.verdict != "approved" or decision.payload.get("action") != "step":
            raise ValueError("a step starts only on an approved step line")
        return
    _validate_decided_by(event.get("decided_by"))
    if decision.verdict is not None:
        raise ValueError(f"line {slot[1]} on this thread was already answered ({decision.verdict})")


def _check_call(kind: str, event: dict, existing_events, audit_witness) -> None:
    """A call-backed row names one successful tool call of its own session, and
    no call backs two rows."""
    tool = WITNESS_TOOLS[kind]
    call_id = event.get("tool_call_id")
    if not _short_str(call_id, _MAX_ID_CHARS):
        raise ValueError(f"{kind} carries tool_call_id, the id of the {tool} call")
    if any(e.get("tool_call_id") == call_id for e in existing_events):
        raise ValueError(f"that {tool} call already backs another casework row")
    if not callable(audit_witness) or not audit_witness(event):
        raise ValueError(
            f"refusing {kind}: the audit log holds no successful {tool} call with that id in "
            "this session. Record write_failed if a write did not land; record no step that "
            "cannot be shown to have run."
        )


def _check_outcome(kind, event, state, existing_events, audit_witness) -> None:
    if state is None or state.authorization is None:
        raise ValueError(
            f"refusing a {kind}: nothing authorized a write on this item (no approved line and "
            "no closed_by_record open). A write happens only on an authorization. Write nothing."
        )
    if kind == "write_failed":
        if not _short_str(event.get("error"), _MAX_ERROR_CHARS):
            raise ValueError(f"write_failed carries error, 1..{_MAX_ERROR_CHARS} characters")
        return
    _check_call(kind, event, existing_events, audit_witness)


def _check_followup(kind: str, state: ItemState | None) -> None:
    if kind == "kept" and (
        state is None or not any(d.verdict == "held" for d in state.decisions.values())
    ):
        raise ValueError("kept records a person's hold; this item has no held line")
    if kind == "mentioned" and (state is None or not (state.completed or state.unmentioned_steps)):
        raise ValueError(
            "mentioned records telling someone a task was closed or a step was run; "
            "this item has neither"
        )


def validate_append(existing_events, new_event: dict, *, send_witness, audit_witness) -> None:
    """Raise ValueError unless ``new_event`` may be appended.

    ``send_witness(event) -> bool``: the broker itself dispatched to a person in
    the event's session. ``audit_witness(event) -> bool``: ``event["tool_call_id"]``
    names this session's successful ``WITNESS_TOOLS[event["event"]]`` call in the
    audit log. Both
    keyword-only with no default, so a caller that forgets one gets a TypeError,
    not an open door.
    """
    if not isinstance(new_event, dict):
        raise ValueError("casework event must be an object")
    _validate_identity(new_event)
    kind = new_event["event"]
    if "payload" in _ALLOWED[kind]:
        _validate_payload(kind, new_event.get("payload"), new_event["kind"])
    if kind in WITNESSED_EVENTS:
        _witnessed(kind, send_witness, new_event)
    key = new_event["item_key"]
    state = derive_state([e for e in existing_events if e.get("item_key") == key]).get(key)
    if kind in RAISING_EVENTS:
        _check_raise(kind, new_event, state)
    elif kind in VERDICT_EVENTS or kind == "step_started":
        _check_answer(kind, new_event, state)
    elif kind in OUTCOME_EVENTS:
        _check_outcome(kind, new_event, state, existing_events, audit_witness)
    elif kind == "step_ran":
        _check_call(kind, new_event, existing_events, audit_witness)
    else:
        _check_followup(kind, state)


# ---------------------------------------------------------------------------
# Append (broker-side write path)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _ulid() -> str:
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    out = []
    for _ in range(26):
        n, rem = divmod(n, 32)
        out.append(_CROCKFORD[rem])
    return "".join(reversed(out))


def stamp_event(event: dict) -> dict:
    """A copy with a server-stamped ``ts``, a fresh ``id`` and ``v``. The caller
    can neither backdate nor choose the id."""
    stamped = dict(event)
    stamped["ts"] = _now_iso()
    stamped["id"] = _ulid()
    stamped["v"] = SCHEMA_VERSION
    return stamped


def append_line(path: str, event: dict) -> None:
    """Append one serialized row, creating the file 0644 so the agent uid can
    read it. The broker serializes writers; this function takes no lock."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    newly_created = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(serialize_event(event) + "\n")
    if newly_created:
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
