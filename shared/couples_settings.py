"""A couple's schedule and settings, set by talking to the agent (ss-console #3264).

WHY. On a couples seat nothing about the couple's rhythm is authored in
customer.yaml any more: not the check-in cadence, not the timezone, not quiet
hours, not the topics. The couple says it, over whatever channel they use
(email today, Telegram later), and the ``couples_settings_change`` tool keeps
it here. customer.yaml for a new couple carries only the people.

WHERE IT LIVES. One JSON document on the seat's persistent volume,
``/opt/data/smd-state/couples-settings.json`` (``SMD_COUPLES_SETTINGS_PATH``
overrides it), beside the two-person register (``shared.joint_acts``). The
volume survives a reprovision by design; nothing at boot rewrites this file,
and the cron store holds no copy of it, so there is nothing for a boot to wipe
or for a conversation to lose. The scheduled side reads it on every tick
(``shared.couples_gate``).

WHAT IS IN IT.

* ``timezone``: an IANA name, or ``null`` until the couple says it.
* ``checkin_topics``: what the couple chose to talk about.
* ``people.<id>``: one partner's own settings: ``quiet_hours``
  (``{"start","end"}`` or ``null``), ``dates_lead_days``, ``paused_topics``.
* ``routines``: each ``{id, kind, space, days, time, paused_until}``. ``kind``
  is the skill that runs; ``space`` is ``person:<id>`` or ``shared``, an
  authored id, never an address. Delivery follows the space, in code.
* ``log``: a bounded record of what changed, when, and who agreed.

WHO MAY CHANGE WHAT (Captain, issue #3264; ADR 0090/0091). A change that
affects only the person asking (their own reminder, their own quiet hours) is
theirs alone and applies at once. Everything else needs both partners: the
timezone, the check-in topics, ANY check-in routine, the other partner's
settings or routines, and shared routines. :func:`needs_both` decides it from
the validated change and the stored routines, never from the model's say-so;
the two-person register holds it until both have said yes.

PARSE, NEVER CAST. :func:`parse_changes` bounds every value and rejects any
key it does not know; :func:`simulate` runs the change against the current
document before anything is written or proposed.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from shared import couples_gate
from shared import space as space_mod

SCHEMA = couples_gate.SCHEMA
_PATH_ENV = "SMD_COUPLES_SETTINGS_PATH"
_DEFAULT_PATH = "/opt/data/smd-state/couples-settings.json"

#: The skills a couple can schedule, and the space kind each runs in.
#: ``person``: one routine per partner (private); ``shared``: one for both.
KINDS: dict[str, str] = {
    "couples-checkin": "person",
    "gratitude-prompts": "person",
    "dates-to-remember": "person",
    "weekly-checkin-agenda": "shared",
    "travel": "shared",
}
#: Check-ins are decided together whoever they reach (Captain).
ALWAYS_JOINT_KINDS = frozenset({"couples-checkin"})

MAX_ROUTINES = 20
MAX_TOPICS = 8
MAX_TOPIC_CHARS = 60
MAX_CHANGES = 6
MAX_LEAD_DAYS = 60
MAX_PAUSE_DAYS = 366
MAX_LOG = 100

OPS = (
    "set_timezone",
    "set_checkin_topics",
    "set_quiet_hours",
    "clear_quiet_hours",
    "set_dates_lead_days",
    "set_paused_topics",
    "add_routine",
    "change_routine",
    "pause_routine",
    "resume_routine",
    "remove_routine",
)
_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # op: (required, optional)
    "set_timezone": (frozenset({"timezone"}), frozenset()),
    "set_checkin_topics": (frozenset({"topics"}), frozenset()),
    "set_quiet_hours": (frozenset({"person", "start", "end"}), frozenset()),
    "clear_quiet_hours": (frozenset({"person"}), frozenset()),
    "set_dates_lead_days": (frozenset({"person", "lead_days"}), frozenset()),
    "set_paused_topics": (frozenset({"person", "topics"}), frozenset()),
    "add_routine": (frozenset({"kind", "for", "days", "time"}), frozenset()),
    "change_routine": (frozenset({"routine"}), frozenset({"days", "time"})),
    "pause_routine": (frozenset({"routine", "until"}), frozenset()),
    "resume_routine": (frozenset({"routine"}), frozenset()),
    "remove_routine": (frozenset({"routine"}), frozenset()),
}
_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_ROUTINE_ID_RE = re.compile(r"^r[0-9a-f]{8}$")
_ZONE_RE = re.compile(r"^(UTC|[A-Z][A-Za-z_]+(/[A-Za-z0-9_+\-]+){1,2})$")
_TOPIC_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")


class SettingsError(ValueError):
    """A change is malformed or does not fit the current settings. For the model."""


@dataclass(frozen=True)
class Change:
    op: str
    fields: dict[str, Any]


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def settings_path() -> Path:
    return Path(os.environ.get(_PATH_ENV) or _DEFAULT_PATH)


def empty() -> dict[str, Any]:
    """The defaults when nothing has been said: no routines, timezone unknown,
    no quiet hours, no topics."""
    return {
        "schema": SCHEMA,
        "timezone": None,
        "checkin_topics": [],
        "people": {},
        "routines": [],
        "log": [],
    }


def load() -> dict[str, Any]:
    """The stored document, or the defaults when there is none. An unreadable
    file raises: "could not read" must never look like "nothing set", or the
    next write would erase what the couple said."""
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as exc:
        raise SettingsError(f"the saved settings could not be read ({exc})") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise SettingsError("the saved settings are not in a shape this seat understands")
    doc = empty()
    doc.update({k: raw[k] for k in doc if k in raw})
    return doc


def save(doc: dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".couples-settings-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, sort_keys=True, indent=1)
        os.chmod(tmp, 0o644)  # the pre-run gate reads it in its own process
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _person_id(value: Any, cfg: space_mod.SpaceConfig, *, allow_shared: bool) -> str:
    if allow_shared and value == space_mod.SHARED:
        return space_mod.SHARED
    if isinstance(value, str) and cfg.person(value) is not None:
        return value
    known = ", ".join(p.id for p in cfg.people)
    extra = " or 'shared'" if allow_shared else ""
    raise SettingsError(f"who it is for must be one of the people's ids ({known}){extra}")


def _hhmm(value: Any, *, quarter: bool) -> str:
    if not isinstance(value, str) or not _HHMM_RE.match(value):
        raise SettingsError(f"{value!r} is not a time of day as HH:MM (00:00 to 23:59)")
    if quarter and int(value[3:]) % couples_gate.SLOT_MINUTES:
        raise SettingsError(f"{value} is not on the quarter hour (:00, :15, :30 or :45)")
    return value


def _days(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SettingsError("days must be a non-empty list like ['tue', 'fri']")
    out: list[str] = []
    for day in value:
        if not isinstance(day, str) or day not in couples_gate.DAYS:
            raise SettingsError(f"{day!r} is not one of {', '.join(couples_gate.DAYS)}")
        if day not in out:
            out.append(day)
    return [d for d in couples_gate.DAYS if d in out]


def _zone(value: Any) -> str:
    if not isinstance(value, str) or not _ZONE_RE.match(value):
        raise SettingsError(f"{value!r} is not an IANA timezone like America/Los_Angeles")
    if couples_gate._zone(value) is None:
        raise SettingsError(f"{value!r} is not a timezone this seat knows")
    return value


def _topics(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_TOPICS:
        raise SettingsError(f"topics must be a list of at most {MAX_TOPICS}")
    out: list[str] = []
    for topic in value:
        if not isinstance(topic, str):
            raise SettingsError("each topic must be a short phrase")
        topic = " ".join(topic.split())
        if not topic or len(topic) > MAX_TOPIC_CHARS or not _TOPIC_RE.match(topic):
            raise SettingsError(f"each topic must be 1 to {MAX_TOPIC_CHARS} characters, one line")
        if topic.lower() not in (t.lower() for t in out):
            out.append(topic)
    return out


def _until(value: Any, today: date) -> str:
    try:
        when = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        when = None
    if when is None:
        raise SettingsError(f"{value!r} is not a date as YYYY-MM-DD")
    if when < today or when > today + timedelta(days=MAX_PAUSE_DAYS):
        raise SettingsError(f"a pause runs to a date from today to {MAX_PAUSE_DAYS} days ahead")
    return when.isoformat()


def parse_changes(
    raw: Any, cfg: space_mod.SpaceConfig, *, today: date | None = None
) -> list[Change]:
    """Validate the tool's ``changes`` list. Raises :class:`SettingsError`."""
    today = today or date.today()
    if not isinstance(raw, list) or not raw or len(raw) > MAX_CHANGES:
        raise SettingsError(f"changes must be a list of 1 to {MAX_CHANGES} changes")
    out: list[Change] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("op") not in _FIELDS:
            raise SettingsError(f"each change needs an op, one of: {', '.join(OPS)}")
        op = item["op"]
        required, optional = _FIELDS[op]
        keys = set(item) - {"op"}
        if not required <= keys or keys - required - optional:
            want = ", ".join(sorted(required | optional))
            raise SettingsError(f"{op} takes exactly: {want}")
        f: dict[str, Any] = {}
        if "timezone" in item:
            f["timezone"] = _zone(item["timezone"])
        if "topics" in item:
            f["topics"] = _topics(item["topics"])
        if "person" in item:
            f["person"] = _person_id(item["person"], cfg, allow_shared=False)
        if op == "set_quiet_hours":
            f["start"] = _hhmm(item["start"], quarter=False)
            f["end"] = _hhmm(item["end"], quarter=False)
            if f["start"] == f["end"]:
                raise SettingsError("quiet hours must start and end at different times")
        if "lead_days" in item:
            lead = item["lead_days"]
            if (
                not isinstance(lead, int)
                or isinstance(lead, bool)
                or not 0 <= lead <= MAX_LEAD_DAYS
            ):
                raise SettingsError(f"lead_days must be a whole number from 0 to {MAX_LEAD_DAYS}")
            f["lead_days"] = lead
        if "days" in item:
            f["days"] = _days(item["days"])
        if "time" in item:
            f["time"] = _hhmm(item["time"], quarter=True)
        if "kind" in item:
            if item["kind"] not in KINDS:
                raise SettingsError(f"kind must be one of: {', '.join(KINDS)}")
            f["kind"] = item["kind"]
        if "for" in item:
            f["for"] = _person_id(item["for"], cfg, allow_shared=True)
        if "routine" in item:
            if not isinstance(item["routine"], str) or not _ROUTINE_ID_RE.match(item["routine"]):
                raise SettingsError(f"{item['routine']!r} is not a routine id")
            f["routine"] = item["routine"]
        if "until" in item:
            f["until"] = _until(item["until"], today)
        if op == "change_routine" and not ({"days", "time"} & keys):
            raise SettingsError("change_routine needs new days, a new time, or both")
        out.append(Change(op=op, fields=f))
    return out


# ---------------------------------------------------------------------------
# Who decides
# ---------------------------------------------------------------------------


def _space_of(routine: dict[str, Any]) -> str:
    return str(routine.get("space") or "")


def routine_visible(routine: dict[str, Any], space: space_mod.Space) -> bool:
    """A partner sees shared routines, check-ins (decided together), and their
    own. The other partner's private routines are theirs."""
    if routine.get("kind") in ALWAYS_JOINT_KINDS or _space_of(routine) == space_mod.SHARED:
        return True
    return space.private and _space_of(routine) == space.key


def needs_both(changes: list[Change], doc: dict[str, Any], speaker: str) -> bool:
    """True unless every change affects the speaker alone."""
    own = space_mod.person_key(speaker)
    by_id = {r.get("id"): r for r in doc.get("routines") or [] if isinstance(r, dict)}
    for change in changes:
        f = change.fields
        if change.op in ("set_timezone", "set_checkin_topics"):
            return True
        if "person" in f:
            if f["person"] != speaker:
                return True
            continue
        if change.op == "add_routine":
            target = space_mod.SHARED if f["for"] == space_mod.SHARED else f"person:{f['for']}"
            kind = f["kind"]
        else:
            routine = by_id.get(f["routine"])
            if routine is None:
                return True
            target, kind = _space_of(routine), routine.get("kind")
        if kind in ALWAYS_JOINT_KINDS or target != own:
            return True
    return False


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _routine(doc: dict[str, Any], rid: str, space: space_mod.Space | None) -> dict[str, Any]:
    for routine in doc["routines"]:
        if routine.get("id") == rid and (space is None or routine_visible(routine, space)):
            return routine
    raise SettingsError(f"there is no routine {rid}")


def _apply_one(
    doc: dict[str, Any],
    change: Change,
    kinds: frozenset[str],
    space: space_mod.Space | None,
    new_id: Any,
) -> None:
    f = change.fields
    people = doc["people"]
    if change.op == "set_timezone":
        doc["timezone"] = f["timezone"]
    elif change.op == "set_checkin_topics":
        doc["checkin_topics"] = f["topics"]
    elif "person" in f:
        mine = people.setdefault(f["person"], {})
        if change.op == "set_quiet_hours":
            mine["quiet_hours"] = {"start": f["start"], "end": f["end"]}
        elif change.op == "clear_quiet_hours":
            mine["quiet_hours"] = None
        elif change.op == "set_dates_lead_days":
            mine["dates_lead_days"] = f["lead_days"]
        else:
            mine["paused_topics"] = f["topics"]
    elif change.op == "add_routine":
        kind, who = f["kind"], f["for"]
        if kind not in kinds:
            raise SettingsError(f"{kind} is not turned on for this seat")
        if (KINDS[kind] == "shared") != (who == space_mod.SHARED):
            where = "for both of you together" if KINDS[kind] == "shared" else "for one person"
            raise SettingsError(f"a {kind} routine is set {where}")
        if not doc["timezone"]:
            raise SettingsError("the timezone is not set yet; ask which timezone they are in")
        if len(doc["routines"]) >= MAX_ROUTINES:
            raise SettingsError(f"this seat keeps at most {MAX_ROUTINES} routines")
        target = space_mod.SHARED if who == space_mod.SHARED else space_mod.person_key(who)
        doc["routines"].append(
            {
                "id": new_id(),
                "kind": kind,
                "space": target,
                "days": f["days"],
                "time": f["time"],
                "paused_until": None,
            }
        )
    else:
        routine = _routine(doc, f["routine"], space)
        if change.op == "change_routine":
            routine.update({k: f[k] for k in ("days", "time") if k in f})
        elif change.op == "pause_routine":
            routine["paused_until"] = f["until"]
        elif change.op == "resume_routine":
            routine["paused_until"] = None
        else:
            doc["routines"].remove(routine)
    _check_unique(doc)


def _check_unique(doc: dict[str, Any]) -> None:
    seen: set[tuple[str, str, str, str]] = set()
    for r in doc["routines"]:
        for day in r["days"]:
            key = (r["kind"], r["space"], day, r["time"])
            if key in seen:
                raise SettingsError("that would set the same routine twice at the same time")
            seen.add(key)


def simulate(
    changes: list[Change],
    doc: dict[str, Any],
    kinds: frozenset[str],
    space: space_mod.Space | None,
) -> dict[str, Any]:
    """The document after ``changes``, without writing. Raises SettingsError."""
    out = copy.deepcopy(doc)
    for change in changes:
        _apply_one(out, change, kinds, space, lambda: f"r{uuid.uuid4().hex[:8]}")
    return out


def apply_changes(
    changes: list[Change],
    *,
    kinds: frozenset[str],
    agreed_by: list[str],
    summary: str,
    space: space_mod.Space | None = None,
) -> dict[str, Any]:
    """Validate against the stored document, write, and log. Returns the new document."""
    doc = simulate(changes, load(), kinds, space)
    doc["log"] = (
        doc["log"]
        + [{"at": int(time.time()), "agreed_by": sorted(set(agreed_by)), "summary": summary}]
    )[-MAX_LOG:]
    save(doc)
    return doc


def available_kinds(customer: dict[str, Any]) -> frozenset[str]:
    """The schedulable skills this seat's persona has enabled with
    ``initiation.scheduled`` (the same rule the cron materializer applies)."""
    out: set[str] = set()
    for persona in customer.get("personas") or []:
        if not isinstance(persona, dict):
            continue
        for skill in persona.get("skills") or []:
            if not isinstance(skill, dict) or not skill.get("enabled"):
                continue
            name, initiation = skill.get("name"), skill.get("initiation")
            if name in KINDS and isinstance(initiation, dict) and initiation.get("scheduled"):
                out.add(name)
    return frozenset(out)


def changes_from_args(args: dict[str, Any]) -> list[dict[str, Any]]:
    """The raw change list stored on a held act (re-parsed when it applies)."""
    raw = args.get("changes")
    return copy.deepcopy(raw) if isinstance(raw, list) else []


__all__ = [
    "ALWAYS_JOINT_KINDS",
    "KINDS",
    "MAX_ROUTINES",
    "SCHEMA",
    "Change",
    "SettingsError",
    "apply_changes",
    "available_kinds",
    "changes_from_args",
    "empty",
    "load",
    "needs_both",
    "parse_changes",
    "routine_visible",
    "save",
    "settings_path",
    "simulate",
]
