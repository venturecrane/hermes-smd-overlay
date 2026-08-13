"""``operator_seat_facts`` — grounded facts about this seat, read not remembered.

THE DEFECT THIS CLOSES (ss-console#2222, card rows 1 + 7). ``operator-introduce``
answers "introduce yourself and tell me what you can see" and "walk me through
what you'll do each day and week". On the client's ONLY channel — inbound email —
neither ask can reach the skill's procedure:

* the depth-2 phrasing appears nowhere in ``matter-inbox-router``'s body, and the
  router body is the only skill text core pre-loads on an email turn (the skills
  index is absent);
* the depth-1 route instructs ``skill_view``, which is NOT on the webhook tool
  surface (live probe, pilot-smokeball, 15 tools) — an unexecutable instruction,
  so the model improvised a fluent roster from memory.

A remembered roster is the failure mode that looks exactly like success. The
pattern that works on this channel is the establishment plugin's: make the act a
REGISTERED TOOL, carry the procedure in the tool description, nudge once, and let
the audit row make fired-vs-improvised decidable from the ledger instead of
arguable from the prose.

WHAT THIS MODULE READS, AND WHAT IT REFUSES TO.

Config-derived and seat-local facts only:

* identity, declared connections, the routine roster and its scheduler pairing,
  installed-voice status, cohort discrepancies, and the counts line.

Deliberately NOT read here (they stay model-driven MCP calls):

* live connector auth (``mcp_smokeball_auth_status``) and the open-matter count;
* the unread-inbox count.

Those are the only claims in the reply that are *observed this turn*. Folding
them in would make "observed live" something this tool ASSERTS rather than
something the audit ledger RECORDS — and the card's falsifier is precisely "a
capability named that was not observed this turn". They are also network calls:
a config read must not block on a vendor timeout. So ``matters`` and ``inbox``
ship present-but-unread with an instruction marker, never a fabricated number.

THREE INVARIANTS, each enforced by construction rather than by instruction:

1. **Counts only.** Nothing here reads or constructs a matter name, a matter
   number, or a client name. There is no source in this module that carries one,
   so there is nothing for a downstream gate to strip. Two authored-label fields
   DO pass through verbatim — ``routine_names`` values and a job's
   ``paused_reason`` — because the firm authored them as operational labels and
   the skill is required to name them; they are pinned by test, not accidental.

2. **No run history.** ``last_run_at``, ``last_status`` and ``next_run_at`` are
   dropped at the READ boundary (:func:`_read_jobs`), not filtered later. The
   introduce skill forbids every run-history claim, and a rule enforced by not
   putting the field in front of the model is stronger than the same rule written
   in prose the model may not be reading on this channel.

3. **Truthful or degraded, never fabricated.** Every section is always present
   and carries a ``read`` flag; a section that could not be read carries
   ``read: false`` and an entry in ``unreadable[]`` using the same vocabulary as
   ``shared.config_snapshot.degraded[]``. An omitted section reads as "nothing
   there", which is the one thing an honest self-description may never say by
   accident. A section fault never raises out of :func:`build_facts`.

WHY VOICE STATUS IS THREE-STATE. ``spec_manifest.load_entries`` collapses every
failure into ``{}`` because its own consumers all fail closed on empty. Rendering
that collapse here would report "the firm never established this voice" and "this
seat cannot see its own spec tree" identically — the exact conflation
``spec_manifest.manifest_state()`` exists to break (ss-console#2234). So each
declared class reports ``installed`` / ``not_installed`` / ``unreadable``, and
``unreadable`` is never a synonym for absence.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Envelope identity. Consumers pin this; a shape change bumps the version.
SCHEMA = "operator.seat.facts/v1"

DEPTH_INTRODUCTION = "introduction"
DEPTH_WALKTHROUGH = "walkthrough"
DEPTHS = (DEPTH_INTRODUCTION, DEPTH_WALKTHROUGH)

#: Every section key the envelope always carries. An absent key would read as
#: "nothing there"; a present key with ``read: false`` reads as "I could not
#: look". Asserted in test — the distinction is the whole honesty contract.
SECTIONS = (
    "identity",
    "connections",
    "matters",
    "inbox",
    "routines",
    "voice",
    "cohort_discrepancies",
    "counts",
    "working_rules",
)

#: Default Hermes home (overlay convention; ``HERMES_HOME`` overrides at
#: runtime). Mirrors ``shared.config_snapshot._DEFAULT_HERMES_HOME`` — never a
#: literal at a call site, because the runtime customer.yaml path
#: (``/var/lib/smd-config/customer.yaml``) and this one deliberately differ.
_DEFAULT_HERMES_HOME = "/opt/data"

#: The marker ``matters`` / ``inbox`` carry. Not a failure: an instruction.
_OBSERVE_YOURSELF = "observe this yourself with your own connector tools this turn"

#: Firm-legible words for the output classes we can translate. A slug we cannot
#: translate is reported as the slug, labelled internal — never guessed at.
_CLASS_FIRM_WORDS = {
    "work_product": "your work product",
    "staff": "your staff-facing writing",
}

#: Voice status vocabulary. Closed, and three-valued for the reason in the
#: module docstring.
VOICE_INSTALLED = "installed"
VOICE_NOT_INSTALLED = "not_installed"
VOICE_UNREADABLE = "unreadable"

#: Routine state vocabulary. The first five mirror the introduce skill's own
#: pairing table verbatim. ``not_scheduled`` covers an enabled routine with
#: neither an authored ``cron:`` entry nor a live job (the skill's table has no
#: row for it because its table is about the scheduled ones). ``authored_layer_only``
#: is what every routine reports when the scheduler store itself would not read:
#: the authored layer is still true and must not be discarded just because the
#: other half is unknown.
STATE_SCHEDULED = "scheduled"
STATE_PAUSED = "paused"
STATE_SWITCHED_OFF = "switched_off"
STATE_AUTHORED_NO_JOB = "authored_no_job"
STATE_JOB_NOT_AUTHORED = "job_not_authored"
STATE_NOT_SCHEDULED = "not_scheduled"
STATE_AUTHORED_LAYER_ONLY = "authored_layer_only"

GROUP_SCHEDULE = "schedule"
GROUP_EVENT = "event"
GROUP_REQUEST = "request"

_DAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


# --------------------------------------------------------------------------- #
# Schedule prose — a closed set of three shapes, computed here, never by the
# model. A wrong translation is fabrication; an untranslated one is just less
# polish, so anything outside the set returns None and the model prints the raw
# expression labelled as raw.
# --------------------------------------------------------------------------- #


def _clock(minute: int, hour: int) -> str:
    """12-hour clock, two-digit minutes. 0 and 12 both render as 12."""
    suffix = "a.m." if hour < 12 else "p.m."
    display = hour % 12
    if display == 0:
        display = 12
    return f"{display}:{minute:02d} {suffix}"


def schedule_prose(expr: object) -> str | None:
    """Plain language for one cron expression, or ``None`` outside the set.

    The three shapes the introduce skill authorizes, and only those::

        M H * * *        -> "Daily at 7:00 a.m."
        M H * * 1-5      -> "Weekdays at 7:00 a.m."
        M H * * <0-6>    -> "Weekly on Tuesday at 7:00 a.m."

    Times are the seat's own clock, so no zone arithmetic happens here and none
    is described to the reader.
    """
    if not isinstance(expr, str):
        return None
    fields = expr.split()
    if len(fields) != 5:
        return None
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    if dom_f != "*" or month_f != "*":
        return None
    try:
        minute = int(minute_f)
        hour = int(hour_f)
    except ValueError:
        return None
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        return None
    when = _clock(minute, hour)
    if dow_f == "*":
        return f"Daily at {when}"
    if dow_f == "1-5":
        return f"Weekdays at {when}"
    if len(dow_f) == 1 and dow_f.isdigit():
        return f"Weekly on {_DAY_NAMES[int(dow_f)]} at {when}"
    return None


# --------------------------------------------------------------------------- #
# Path resolution. Inside the handler, never at import and never as a literal —
# a missing env var must degrade ONE section, not delete the tool from the
# surface (the vision_analyze failure shape) or hardcode the wrong volume path.
# --------------------------------------------------------------------------- #


def hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME


def _jobs_path(persona_slug: str, home: str | None = None) -> Path:
    return Path(home or hermes_home()) / "profiles" / persona_slug / "cron" / "jobs.json"


def _cohort_root(home: str | None = None) -> Path:
    return Path(home or hermes_home()) / "voice" / "cohort"


# --------------------------------------------------------------------------- #
# Per-source readers. Each returns its data or raises; ``build_facts`` converts
# a raise into ``read: false`` + one ``unreadable[]`` entry for that section and
# nothing else.
# --------------------------------------------------------------------------- #


def _read_jobs(path: Path) -> list[dict[str, Any]]:
    """The live scheduler store, with run history dropped at the boundary.

    ``last_run_at`` / ``last_status`` / ``next_run_at`` are never copied into the
    return value: the introduce skill forbids every run-history claim, and the
    strongest way to enforce that is for the field to never reach the model. See
    invariant 2 in the module docstring.

    Tolerates both scheduler shapes for the expression — ``schedule`` as a bare
    string and ``schedule: {expr: ...}`` — because the store has carried both.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(raw_jobs, list):
        raise ValueError("jobs.json carries no jobs list")
    out: list[dict[str, Any]] = []
    for job in raw_jobs:
        if not isinstance(job, dict):
            continue
        schedule = job.get("schedule")
        expr = schedule.get("expr") if isinstance(schedule, dict) else schedule
        out.append(
            {
                "name": job.get("name"),
                "skill": job.get("skill"),
                "schedule_expr": expr if isinstance(expr, str) else None,
                "enabled": job.get("enabled"),
                "state": job.get("state"),
                "paused_at": job.get("paused_at"),
                "paused_reason": job.get("paused_reason"),
            }
        )
    return out


def _persona(cfg: Any) -> dict[str, Any]:
    personas = cfg.personas
    if not personas:
        raise ValueError("config authors no personas")
    first = personas[0]
    if not isinstance(first, dict):
        raise ValueError("persona entry is not a mapping")
    return first


def _raw_block(cfg: Any, key: str) -> Any:
    raw = cfg.raw
    return raw.get(key) if isinstance(raw, dict) else None


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #


def _identity_section(cfg: Any) -> dict[str, Any]:
    persona = _persona(cfg)
    connectors = cfg.connectors or {}
    email = connectors.get("Email") if isinstance(connectors, dict) else None
    # Only msgraph-custody seats author their own address (``mailbox``);
    # AgentMail seats do not carry one in customer.yaml at all. ``None`` here is
    # "not authored", and the tool description tells the model the address it
    # replies from is the one this exchange arrived on — never invented.
    mailbox = email.get("mailbox") if isinstance(email, dict) else None
    return {
        "read": True,
        "persona_name": persona.get("name"),
        "persona_title": persona.get("title"),
        "persona_slug": persona.get("slug"),
        "firm_display_name": cfg.customer_name,
        "email_address": mailbox if isinstance(mailbox, str) and mailbox else None,
    }


def _connections_section(cfg: Any) -> dict[str, Any]:
    """What the config DECLARES. Live auth is the model's own MCP call.

    The key is ``declared`` and not ``observed`` deliberately: this list is
    authored state, and labelling authored state "observed" is the precise shape
    of the fabrication the whole tool exists to prevent.
    """
    connectors = cfg.connectors or {}
    declared: list[dict[str, Any]] = []
    if isinstance(connectors, dict):
        for capability, record in sorted(connectors.items()):
            if not isinstance(record, dict) or not record.get("enabled"):
                continue
            declared.append(
                {
                    "capability": str(capability),
                    "adapter": record.get("adapter"),
                    "auth": "unknown",
                }
            )
    return {
        "read": True,
        "declared": declared,
        "note": (
            "Authored roster, not a live probe. Call your own connector "
            "auth-status tool this turn and report only what you observed."
        ),
    }


#: The identifier gate's operator-only rollback lever (hermes-smd-trust
#: outbound.py). Unset or any value other than ``report`` = blocking. Read here
#: rather than assumed, because "I refuse identifiers I cannot verify" is FALSE
#: on a seat running the gate in report mode, and that is exactly the kind of
#: sentence a firm would act on.
_IDENTIFIER_MODE_ENV = "SMD_IDENTIFIER_GATE_MODE"

#: The one working rule with no readable mechanism behind it. It is authored
#: skill policy, so it is carried under its own key and labelled as policy —
#: never mixed in with the three the seat can prove.
_POLICY_RULES = (
    {
        "rule": "no_legal_advice",
        "says": "I don't give legal advice or opinions on the merits.",
        "basis": "authored skill policy — no runtime gate enforces this one",
    },
)


def _working_rules_section(cfg: Any) -> dict[str, Any]:
    """The rules a firm hears in an introduction — three of them READ (ss#2338).

    WHY THIS IS A SECTION AND NOT A SENTENCE IN THE SKILL. The four rules were
    already authored, in ``operator-introduce``'s fixed shape, before the
    2026-08-12 rehearsal — and the rehearsal's introduction still stated none
    of them. That skill's own body explains why (:64-68): on an email turn the
    file is not in front of the model, so a rule living only there is a rule
    nobody reads. The reply that came back mirrored THIS envelope's sections
    exactly — identity, connections, matters, inbox, routines, voice, counts —
    which is the evidence that the envelope, not the skill body, is what shapes
    an email introduction. So the rules move to where the reading happens.

    WHY THEY ARE READ AND NOT RECITED. Two of the four are seat-variable, and
    stating them as constants would be the fabrication this whole tool exists
    to prevent:

    * **review-before-send** is per-class ``exposure``. On a seat authoring
      ``external_send: autonomous`` the sentence "I don't send externally on my
      own" is simply false — and ADR 0073 proved an authored autonomous send
      really does send. So the posture is reported per class, as authored.
    * **unverified identifiers** and **reads-never-computes** are the same A1
      gate, and it carries an operator rollback lever
      (``SMD_IDENTIFIER_GATE_MODE=report``). In report mode it observes and
      does not refuse, so a seat running it there must not tell a firm it
      refuses.

    Only ``no_legal_advice`` has no mechanism to read; it is carried separately
    and labelled as policy rather than dressed up as an observation.
    """
    posture: list[dict[str, Any]] = []
    try:
        persona = _persona(cfg)
        entitlements = persona.get("entitlements")
        exposure = entitlements.get("exposure") if isinstance(entitlements, dict) else None
        if isinstance(exposure, dict):
            for action, value in sorted(exposure.items()):
                if not str(action).startswith("external_send"):
                    continue
                posture.append({"action_class": str(action), "exposure": str(value)})
    except Exception:  # noqa: BLE001 — an unreadable persona degrades this section only
        logger.warning("operator_seat_facts: exposure unreadable for working_rules", exc_info=True)
        return _empty("working_rules")

    mode = str(os.environ.get(_IDENTIFIER_MODE_ENV, "") or "").strip().lower()
    refusing = mode != "report"
    return {
        "read": True,
        "send_posture": posture,
        "send_posture_note": (
            (
                "As authored on this seat, per outbound class. Say what these "
                "values mean in the firm's own words — 'draft_for_review' is 'a "
                "person reviews it before it goes', 'autonomous' is 'I send it "
                "myself'. Never state a review promise for a class authored "
                "autonomous, and never claim a blanket posture when the classes "
                "disagree."
            )
            if posture
            # An empty list is NOT "no restrictions" — it is the fail-closed
            # state (ADR 0037 tenet 3: unconfigured is a safety state, never an
            # identity). Saying so is the difference between "I send nothing
            # outward" and the far worse "nothing stops me".
            else (
                "No outbound class is authored on this seat, which means outward "
                "sending is refused rather than unrestricted. Say you cannot "
                "send outward here — never that you are free to."
            )
        ),
        "identifier_gate": {
            "refusing": refusing,
            "says": (
                "I won't use a case number, date, or identifier I haven't read "
                "from your records. If I can't verify it, I say so instead."
                if refusing
                else "I flag an identifier I could not verify, but I do not "
                "refuse on it — this seat's identifier gate is in report mode."
            ),
            "also_covers": (
                "The same gate is the backstop for never computing a legal "
                "deadline: a computed date is in no source, so it is not in the "
                "session's provenance register."
            ),
        },
        "policy_rules": [dict(rule) for rule in _POLICY_RULES],
    }


def _voice_section(cfg: Any, spec_manifest: Any) -> dict[str, Any]:
    """Per-class installed-voice status, three-valued.

    ``installed`` requires BOTH a manifest entry for the class's ``voice``
    property AND that the file on disk still hashes to what root recorded — the
    same pairing ``shared.spec_control_check`` makes. A file that no longer
    matches root's record is reported ``not_installed``: the seat cannot claim it
    learned a voice it cannot verify, and the safe direction is to under-claim.
    """
    declared = cfg.output_classes
    if not isinstance(declared, dict):
        raise ValueError("output_classes is not a mapping")

    state = spec_manifest.manifest_state()
    unreadable = state == spec_manifest.STATE_UNREADABLE

    classes: list[dict[str, Any]] = []
    for output_class, block in sorted(declared.items()):
        if not isinstance(output_class, str) or not isinstance(block, dict):
            continue
        voice_spec = str(block.get("voice_spec", "none")).strip().lower()
        if unreadable:
            status = VOICE_UNREADABLE
        else:
            installed = any(
                entry.prop == "voice" and spec_manifest.verify(entry)
                for entry in spec_manifest.entries_for_class(output_class)
            )
            status = VOICE_INSTALLED if installed else VOICE_NOT_INSTALLED
        classes.append(
            {
                "class": output_class,
                "firm_words": _CLASS_FIRM_WORDS.get(output_class),
                "voice_spec": voice_spec,
                "status": status,
            }
        )
    return {"read": True, "manifest_state": state, "classes": classes}


def _cohort_section(cfg: Any, home: str | None = None) -> dict[str, Any]:
    """Cohort directories on disk that the authored vocabulary does not authorize.

    An unreadable root is NOT an empty result: "I could not look" must never
    render as "there was nothing there", so the caller degrades this section
    rather than reporting no discrepancies.
    """
    block = _raw_block(cfg, "voice_cohorts")
    authored = block.get("cohorts") if isinstance(block, dict) else None
    authorized = {str(c) for c in authored} if isinstance(authored, list) else set()
    root = _cohort_root(home)
    if not root.is_dir():
        # A seat with no cohort tree has nothing on disk to be unauthorized —
        # a determinable fact, not a failed read.
        return {"read": True, "unauthorized_dirs": []}
    found = sorted(entry.name for entry in root.iterdir() if entry.is_dir())
    return {
        "read": True,
        "unauthorized_dirs": [name for name in found if name not in authorized],
    }


def _routine_items(
    cfg: Any,
    persona: dict[str, Any],
    jobs: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Pair the authored layer against the scheduler layer, per routine.

    The two layers are never reconciled to whichever reads better: a
    disagreement IS the finding, and it ships as its own state so the reply can
    report it in plain words.
    """
    skills = persona.get("skills")
    skills = skills if isinstance(skills, list) else []
    cron = persona.get("cron")
    cron = cron if isinstance(cron, list) else []

    authored_cron: dict[str, str | None] = {}
    for entry in cron:
        if isinstance(entry, dict) and isinstance(entry.get("skill"), str):
            expr = entry.get("schedule")
            authored_cron[entry["skill"]] = expr if isinstance(expr, str) else None

    triggers = _raw_block(cfg, "webhook_triggers")
    events: dict[str, str] = {}
    if isinstance(triggers, list):
        for trig in triggers:
            if not isinstance(trig, dict) or not isinstance(trig.get("skill"), str):
                continue
            source = str(trig.get("source", "")).strip()
            event_type = str(trig.get("event_type", "")).strip()
            label = " ".join(part for part in (source, event_type) if part)
            if label:
                events.setdefault(trig["skill"], label)

    names_block = _raw_block(cfg, "routine_names")
    firm_names = names_block if isinstance(names_block, dict) else {}

    jobs_by_skill: dict[str, dict[str, Any]] = {}
    if jobs is not None:
        for job in jobs:
            skill = job.get("skill")
            if isinstance(skill, str):
                jobs_by_skill.setdefault(skill, job)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for entry in skills:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        slug = entry["name"]
        seen.add(slug)
        initiation = entry.get("initiation")
        initiation = initiation if isinstance(initiation, dict) else {}
        job = jobs_by_skill.get(slug)
        authored_expr = authored_cron.get(slug)
        is_authored = slug in authored_cron

        if jobs is None:
            state = STATE_AUTHORED_LAYER_ONLY
        elif entry.get("enabled") is False:
            state = STATE_SWITCHED_OFF
        elif job is not None and (job.get("enabled") is False or job.get("paused_at")):
            state = STATE_PAUSED
        elif is_authored and job is not None:
            state = STATE_SCHEDULED
        elif is_authored:
            state = STATE_AUTHORED_NO_JOB
        elif job is not None:
            state = STATE_JOB_NOT_AUTHORED
        else:
            state = STATE_NOT_SCHEDULED

        if is_authored:
            group = GROUP_SCHEDULE
        elif initiation.get("webhook") is True:
            group = GROUP_EVENT
        elif initiation.get("manual") is True:
            group = GROUP_REQUEST
        else:
            group = None

        expr = authored_expr or (job.get("schedule_expr") if job else None)
        firm_name = firm_names.get(slug)
        items.append(
            {
                "skill": slug,
                "firm_name": firm_name if isinstance(firm_name, str) else None,
                "group": group,
                "schedule_expr": expr,
                "schedule_prose": schedule_prose(expr),
                "state": state,
                "paused_reason": job.get("paused_reason") if job else None,
                "event": events.get(slug) if group == GROUP_EVENT else None,
                "also_on_request": bool(
                    initiation.get("manual") is True and group != GROUP_REQUEST
                ),
            }
        )

    # A live job whose skill the config does not author at all is a discrepancy
    # the config-side walk above cannot see. Reporting it is the point.
    for job in jobs or []:
        slug = job.get("skill")
        if not isinstance(slug, str) or slug in seen:
            continue
        seen.add(slug)
        expr = job.get("schedule_expr")
        firm_name = firm_names.get(slug)
        items.append(
            {
                "skill": slug,
                "firm_name": firm_name if isinstance(firm_name, str) else None,
                "group": GROUP_SCHEDULE,
                "schedule_expr": expr,
                "schedule_prose": schedule_prose(expr),
                "state": STATE_JOB_NOT_AUTHORED,
                "paused_reason": job.get("paused_reason"),
                "event": None,
                "also_on_request": False,
            }
        )

    return items


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def _empty(section: str) -> dict[str, Any]:
    """The ``read: false`` shell for a section that would not read."""
    shells: dict[str, dict[str, Any]] = {
        "identity": {
            "persona_name": None,
            "persona_title": None,
            "persona_slug": None,
            "firm_display_name": None,
            "email_address": None,
        },
        "connections": {"declared": [], "note": None},
        "routines": {"items": []},
        "voice": {"manifest_state": None, "classes": []},
        "cohort_discrepancies": {"unauthorized_dirs": []},
        "working_rules": {
            "send_posture": [],
            "send_posture_note": None,
            "identifier_gate": None,
            "policy_rules": [],
        },
        "counts": {
            "skill_entries": None,
            "enabled": None,
            "scheduled": None,
            "live_jobs": None,
        },
    }
    return {"read": False, **shells.get(section, {})}


def build_facts(
    *,
    depth: str = DEPTH_INTRODUCTION,
    config_loader: Any = None,
    spec_manifest_module: Any = None,
    home: str | None = None,
) -> dict[str, Any]:
    """Assemble ``operator.seat.facts/v1``. Never raises.

    Fail-open PER SECTION: one unreadable source degrades its own section and
    nothing else. A total fault returns the envelope with every section
    ``read: false``, which lets the model say honestly that it could not read
    itself — a better client outcome than a tool error it would paraphrase.

    ``config_loader`` / ``spec_manifest_module`` / ``home`` are injection seams
    for tests only; production passes none of them.
    """
    if depth not in DEPTHS:
        depth = DEPTH_INTRODUCTION

    unreadable: list[dict[str, str]] = []
    facts: dict[str, Any] = {"schema": SCHEMA, "depth": depth}

    if config_loader is None:
        from shared.customer_config import CustomerConfig

        config_loader = CustomerConfig.from_volume
    if spec_manifest_module is None:
        from shared import spec_manifest as spec_manifest_module

    try:
        cfg = config_loader()
    except Exception as exc:  # noqa: BLE001 — an unreadable config degrades, never raises
        logger.warning("operator_seat_facts: customer config unreadable (%s)", exc)
        cfg = None

    if cfg is None:
        for section in SECTIONS:
            facts[section] = _empty(section)
        facts["matters"] = _observe_marker("open_count")
        facts["inbox"] = _observe_marker("unread_count")
        unreadable.append({"section": "config", "reason": "customer.yaml unreadable"})
        facts["unreadable"] = unreadable
        return facts

    # identity ---------------------------------------------------------------
    persona_slug: str | None = None
    try:
        facts["identity"] = _identity_section(cfg)
        slug = facts["identity"].get("persona_slug")
        persona_slug = slug if isinstance(slug, str) and slug else None
    except Exception as exc:  # noqa: BLE001
        facts["identity"] = _empty("identity")
        unreadable.append({"section": "identity", "reason": f"persona unreadable ({exc})"})

    # connections ------------------------------------------------------------
    try:
        facts["connections"] = _connections_section(cfg)
    except Exception as exc:  # noqa: BLE001
        facts["connections"] = _empty("connections")
        unreadable.append({"section": "connections", "reason": f"connectors unreadable ({exc})"})

    # live observations the tool deliberately does not make -------------------
    facts["matters"] = _observe_marker("open_count")
    facts["inbox"] = _observe_marker("unread_count")

    # routines + counts ------------------------------------------------------
    jobs: list[dict[str, Any]] | None = None
    jobs_readable = False
    if persona_slug:
        path = _jobs_path(persona_slug, home)
        if path.is_file():
            try:
                jobs = _read_jobs(path)
                jobs_readable = True
            except Exception as exc:  # noqa: BLE001
                jobs = None
                unreadable.append(
                    {"section": "scheduler", "reason": f"jobs.json unreadable ({exc})"}
                )
        else:
            # No jobs file is a determinable fact (nothing materialized), not a
            # failed read — the same call ``config_snapshot.read_profiles`` makes.
            jobs = []
            jobs_readable = True
    else:
        unreadable.append(
            {"section": "scheduler", "reason": "persona slug unknown; scheduler not located"}
        )

    try:
        persona = _persona(cfg)
        items = _routine_items(cfg, persona, jobs)
        facts["routines"] = {"read": True, "items": items}
        skills = persona.get("skills")
        skills = skills if isinstance(skills, list) else []
        cron = persona.get("cron")
        cron = cron if isinstance(cron, list) else []
        facts["counts"] = {
            "read": True,
            "skill_entries": len(skills),
            "enabled": sum(
                1 for s in skills if isinstance(s, dict) and s.get("enabled") is not False
            ),
            "scheduled": len(cron),
            "live_jobs": len(jobs) if jobs_readable and jobs is not None else None,
        }
    except Exception as exc:  # noqa: BLE001
        facts["routines"] = _empty("routines")
        facts["counts"] = _empty("counts")
        unreadable.append({"section": "routines", "reason": f"routine roster unreadable ({exc})"})

    # voice ------------------------------------------------------------------
    try:
        facts["voice"] = _voice_section(cfg, spec_manifest_module)
        if facts["voice"].get("manifest_state") == spec_manifest_module.STATE_UNREADABLE:
            unreadable.append(
                {
                    "section": "voice",
                    "reason": "spec manifest unreadable; installed-ness unprovable",
                }
            )
    except Exception as exc:  # noqa: BLE001
        facts["voice"] = _empty("voice")
        unreadable.append({"section": "voice", "reason": f"output_classes unreadable ({exc})"})

    # cohorts ----------------------------------------------------------------
    try:
        facts["cohort_discrepancies"] = _cohort_section(cfg, home)
    except Exception as exc:  # noqa: BLE001
        facts["cohort_discrepancies"] = _empty("cohort_discrepancies")
        unreadable.append({"section": "cohorts", "reason": f"cohort tree unreadable ({exc})"})

    # working rules ----------------------------------------------------------
    try:
        facts["working_rules"] = _working_rules_section(cfg)
        if not facts["working_rules"].get("read"):
            unreadable.append(
                {"section": "working_rules", "reason": "exposure unreadable; posture unprovable"}
            )
    except Exception as exc:  # noqa: BLE001
        facts["working_rules"] = _empty("working_rules")
        unreadable.append({"section": "working_rules", "reason": f"exposure unreadable ({exc})"})

    facts["unreadable"] = unreadable
    return facts


def _observe_marker(field: str) -> dict[str, Any]:
    """``matters`` / ``inbox``: present, unread, and instructed — not failed."""
    return {"read": False, field: None, "reason": _OBSERVE_YOURSELF}


__all__ = [
    "DEPTHS",
    "DEPTH_INTRODUCTION",
    "DEPTH_WALKTHROUGH",
    "GROUP_EVENT",
    "GROUP_REQUEST",
    "GROUP_SCHEDULE",
    "SCHEMA",
    "SECTIONS",
    "STATE_AUTHORED_LAYER_ONLY",
    "STATE_AUTHORED_NO_JOB",
    "STATE_JOB_NOT_AUTHORED",
    "STATE_NOT_SCHEDULED",
    "STATE_PAUSED",
    "STATE_SCHEDULED",
    "STATE_SWITCHED_OFF",
    "VOICE_INSTALLED",
    "VOICE_NOT_INSTALLED",
    "VOICE_UNREADABLE",
    "build_facts",
    "hermes_home",
    "schedule_prose",
]
