"""The case-manager tools' shared rules: envelope shapes, ledger rows, rendering.

Split from ``casework.py`` so each file stays readable. Everything here is pure or
file-local: the envelope a pre_run writes and how it is validated and taken, the
casework ledger rows as they are read back, and the code that renders every
sentence a firm receives. Nothing here reaches the model.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from shared import cron_attribution, digest_reply_ref, pre_run_handoff

logger = logging.getLogger(__name__)

DEFAULT_LEDGER_PATH = "/opt/data/audit/casework-ledger.jsonl"

CASEWORK_SUFFIX = "casework"
BRIEF_SUFFIX = "brief"

VERDICT_EVENTS = frozenset({"approved", "held"})

_MAX_MESSAGES = 10
_MAX_RECIPIENTS = 20
_MAX_ITEMS = 30
_MAX_CLOSES = 30
_MAX_DONE_SINCE = 20
_MAX_SUBJECT = 500
_MAX_LEAD = 600
_MAX_LINE = 240
_MAX_SHORT_LINE = 160
_MAX_SUBJECT_LABEL = 200
_MAX_CATALOG = 20
_MAX_BRIEF_DONE = 8
_MAX_BRIEF_DONE_CHARS = 120
_MAX_QUESTION = 300
_MAX_DECISIONS = 2
_EM_DASH = "\u2014"

ACTIONS = frozenset({"close", "keep", "reassign", "step"})
CLASSES = frozenset({"open", "done", "stale", "at_stake"})
LEVELS = frozenset({"surfaces", "prepares", "handles"})

DEFAULT_FOOTER = 'Reply here in words, for example "yes to all", "all except 3" or "leave 2".'
BRIEF_FOOTER = "Reply here and I'll take it from there."

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class _Bounded:
    """A small per-session map; a long-lived gateway must not grow one forever."""

    def __init__(self, cap: int = 64) -> None:
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._cap = cap
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._cap:
                self._data.popitem(last=False)

    def pop(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


_FINISH = _Bounded()
_BRIEFED = _Bounded()
_REPLIES = _Bounded()


def _text(value: object, limit: int, *, required: bool = True) -> bool:
    if value is None and not required:
        return True
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= limit
        and _EM_DASH not in value
    )


def _id(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


def _optional_id(value: object) -> bool:
    return value is None or _id(value)


def _addresses(value: object, *, required: bool) -> bool:
    if value is None and not required:
        return True
    if not isinstance(value, list) or len(value) > _MAX_RECIPIENTS:
        return False
    if required and not value:
        return False
    return all(isinstance(a, str) and "@" in a and a.strip() == a for a in value)


def _join(parts: list[str], conjunction: str = "and") -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} {conjunction} {parts[-1]}"


def _numbers(values: list[int], conjunction: str = "and") -> str:
    return _join([str(n) for n in sorted(values)], conjunction)


def _queued_note(count: int, tool: str) -> str:
    """What the turn is told while replayed task writes are waiting."""
    calls = "once" if count == 1 else f"{count} times"
    return (
        f"Call mcp_smokeball_update_task {calls}. Its arguments are filled in for you, "
        f"so pass the task_id you see and nothing else matters. Then call {tool} again."
    )


def ledger_path() -> str:
    return os.environ.get("SMD_CASEWORK_LEDGER_PATH") or DEFAULT_LEDGER_PATH


def read_rows(path: str) -> list[dict]:
    """Every well-formed row in the casework ledger twin; a missing file is empty."""
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("event"), str):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _event(
    kind: str,
    *,
    skill: str,
    matter_id: object,
    item_key: str,
    session_id: str,
    **fields: Any,
) -> dict[str, Any]:
    """One row bound for the broker's ``casework_event_append`` verb. The broker
    stamps ts and id; nobody here can backdate."""
    event: dict[str, Any] = {
        "ts": None,
        "skill": skill,
        "matter_id": matter_id,
        "item_key": item_key,
        "event": kind,
        "session_id": session_id,
    }
    event.update({k: v for k, v in fields.items() if v is not None})
    return event


def _ok(response: object) -> bool:
    return isinstance(response, dict) and bool(response.get("ok"))


def _append(append: Callable[[dict], Any], event: dict[str, Any]) -> bool:
    try:
        response = append(event)
    except Exception as exc:  # noqa: BLE001 — one lost row must not lose the rest
        logger.warning("casework: %s row not written (%s)", event.get("event"), exc)
        return False
    if not _ok(response):
        logger.warning("casework: %s row refused (%s)", event.get("event"), response)
        return False
    return True


# ---------------------------------------------------------------------------
# Envelopes (tamper-fenced, consume once)
# ---------------------------------------------------------------------------


def envelope_path(skill: str, suffix: str, hermes_home: str | None = None, persona=None):
    safe = pre_run_handoff._safe_skill(skill)
    return pre_run_handoff.handoff_dir(hermes_home, persona) / f"{safe}.{suffix}.json"


def take_envelope(
    skill: str,
    suffix: str,
    validator: Callable[[dict], bool],
    *,
    persona: str | None = None,
    hermes_home: str | None = None,
    now: datetime | None = None,
) -> dict | None:
    """The validated envelope for ``skill``, renamed to ``.consumed.json`` BEFORE
    use, or None. Same binding as the dispatch envelope: fresh on the reader's
    clock, persona home first, and a malformed field refuses the whole file."""
    try:
        for candidate_persona in [persona, None] if persona else [None]:
            path = envelope_path(skill, suffix, hermes_home, candidate_persona)
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                payload = json.loads(raw)
            except ValueError:
                logger.warning("casework: %s is not valid JSON; ignoring", path)
                return None
            if not isinstance(payload, dict) or payload.get("skill") != skill:
                return None
            started_at = pre_run_handoff._parse_iso_aware(payload.get("started_at"))
            if started_at is None:
                return None
            moment = now or datetime.now(timezone.utc)
            age = moment - started_at
            if age > pre_run_handoff.DEFAULT_WINDOW or age < -pre_run_handoff._MAX_CLOCK_SKEW:
                logger.info("casework: %s is not fresh; leaving it in place", path)
                return None
            if not validator(payload):
                logger.warning("casework: %s is malformed; refusing the whole envelope", path)
                return None
            os.replace(path, path.with_name(path.name[: -len(".json")] + ".consumed.json"))
            return payload
        return None
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.warning("casework: take failed for %r (%s)", skill, exc)
        return None


def _valid_done_since(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, list) or len(value) > _MAX_DONE_SINCE:
        return False
    return all(
        isinstance(row, dict)
        and _id(row.get("item_key"))
        and _optional_id(row.get("matter_id"))
        and _text(row.get("line"), _MAX_SHORT_LINE)
        for row in value
    )


def _valid_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("action") not in ACTIONS or payload.get("class") not in CLASSES:
        return False
    if payload.get("action") == "close" and payload.get("class") == "at_stake":
        return False  # money or court at stake is never closed without a person
    if not _id(payload.get("staff_id")):
        return False
    if payload.get("action") == "reassign" and not _id(payload.get("to_staff_id")):
        return False
    evidence = payload.get("evidence", [])
    return isinstance(evidence, list) and all(isinstance(e, str) for e in evidence)


def _valid_item(item: object) -> bool:
    return (
        isinstance(item, dict)
        and digest_reply_ref.valid_digest_number(item.get("n"))
        and _text(item.get("group"), _MAX_SHORT_LINE, required=False)
        and _text(item.get("line"), _MAX_LINE)
        and item.get("event") in ("proposed", "named")
        and _id(item.get("item_key"))
        and _optional_id(item.get("matter_id"))
        and _id(item.get("task_id"))
        and _valid_payload(item.get("payload"))
    )


def _valid_close(close: object) -> bool:
    return (
        isinstance(close, dict)
        and _id(close.get("item_key"))
        and _optional_id(close.get("matter_id"))
        and _id(close.get("task_id"))
        and _id(close.get("staff_id"))
        and _text(close.get("line"), _MAX_SHORT_LINE)
    )


def _valid_message(message: object) -> bool:
    if not isinstance(message, dict):
        return False
    items = message.get("items", [])
    closes = message.get("closes", [])
    if not (isinstance(items, list) and len(items) <= _MAX_ITEMS):
        return False
    if not (isinstance(closes, list) and len(closes) <= _MAX_CLOSES):
        return False
    numbers = [item.get("n") for item in items if isinstance(item, dict)]
    return (
        _addresses(message.get("recipients"), required=True)
        and _addresses(message.get("cc"), required=False)
        and _text(message.get("subject"), _MAX_SUBJECT)
        and _text(message.get("lead"), _MAX_LEAD, required=False)
        and _text(message.get("footer"), _MAX_LEAD, required=False)
        and _optional_id(message.get("routing_leg"))
        and all(_valid_item(item) for item in items)
        and len(set(numbers)) == len(numbers)
        and all(_valid_close(close) for close in closes)
        and _valid_done_since(message.get("done_since"))
    )


def valid_casework_envelope(payload: dict) -> bool:
    messages = payload.get("messages")
    return (
        isinstance(messages, list)
        and len(messages) <= _MAX_MESSAGES
        and all(_valid_message(m) for m in messages)
    )


def _valid_catalog_entry(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and _id(entry.get("catalog_id"))
        and _id(entry.get("skill"))
        and entry.get("level") in ("prepares", "handles")
        and isinstance(entry.get("params", {}), dict)
    )


def valid_brief_envelope(payload: dict) -> bool:
    catalog = payload.get("catalog")
    if not isinstance(catalog, list) or not catalog or len(catalog) > _MAX_CATALOG:
        return False
    ids = [entry.get("catalog_id") for entry in catalog if isinstance(entry, dict)]
    return (
        all(_valid_catalog_entry(entry) for entry in catalog)
        and len(set(ids)) == len(ids)
        and _id(payload.get("matter_id"))
        and _id(payload.get("item_key"))
        and _optional_id(payload.get("event_id"))
        and _text(payload.get("subject_label"), _MAX_SUBJECT_LABEL)
        and _addresses(payload.get("recipients"), required=True)
        and _addresses(payload.get("cc"), required=False)
        and _optional_id(payload.get("routing_leg"))
        and _valid_done_since(payload.get("done_since"))
    )


def _routine(session_id: str) -> tuple[str, str | None] | None:
    routine = cron_attribution.resolve_routine(session_id)
    if routine is None or not routine.skill:
        return None
    return routine.skill, routine.persona


# ---------------------------------------------------------------------------
# Rendering (code, never the model)
# ---------------------------------------------------------------------------


def _done_since_line(rows: list[dict]) -> str:
    lines = [str(row["line"]).rstrip(". ") for row in rows]
    return "Done since last time: " + "; ".join(lines) + "." if lines else ""


def render_review(message: dict, closed: list[dict], failed: list[dict]) -> str:
    """The task-review body: lead, done since last time, closed just now, what
    could not be updated, the numbered lines, the footer. Blank-line separated.
    ``closed`` and ``failed`` are this message's closes by recorded outcome, so
    "Closed just now" can only list what actually closed."""
    blocks: list[str] = []
    if message.get("lead"):
        blocks.append(str(message["lead"]).strip())
    since = _done_since_line(message.get("done_since") or [])
    if since:
        blocks.append(since)
    if closed:
        blocks.append("\n".join(["Closed just now:", *(f"- {c['line']}" for c in closed)]))
    if failed:
        lines = "; ".join(str(c["line"]).rstrip(". ") for c in failed)
        blocks.append(f"I couldn't update these in Smokeball just now: {lines}.")
    listing: list[str] = []
    group = None
    for item in sorted(message.get("items") or [], key=lambda i: i["n"]):
        if item.get("group") and item["group"] != group:
            group = item["group"]
            if listing:
                listing.append("")
            listing.append(str(group))
        listing.append(f"{item['n']}. {item['line']}")
    if listing:
        blocks.append("\n".join(listing))
    blocks.append(str(message.get("footer") or DEFAULT_FOOTER))
    return "\n\n".join(blocks)


_COUNT_WORDS = {1: "one question", 2: "two questions"}


def render_brief(envelope: dict, done: list[str], decisions: list[dict]) -> tuple[str, str]:
    """``(subject, body)`` for a date-prep brief."""
    subject = f"{envelope['subject_label']}, {_COUNT_WORDS[len(decisions)]} for you"
    blocks: list[str] = []
    if done:
        blocks.append("\n".join(["Done:", *(f"- {line.strip()}" for line in done)]))
    since = _done_since_line(envelope.get("done_since") or [])
    if since:
        blocks.append(since)
    needs = ["Needs you:"]
    needs.extend(f"{n}. {d['question'].strip()}" for n, d in enumerate(decisions, start=1))
    blocks.append("\n".join(needs))
    blocks.append(BRIEF_FOOTER)
    return subject, "\n\n".join(blocks)
