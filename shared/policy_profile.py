"""Per-seat policy profile: what a seat's role loosens, over floors no role can touch.

WHY THIS EXISTS. Every seat runs the same trust gates, and the gates were written
for a law firm. A couple's seat was refused a birthday reminder because the date
of this year's birthday is in no record (only the month and day are), and its
household wording ("can't wait!") was refused by a paralegal's checklist. The
only way out was an operator env var, ``SMD_IDENTIFIER_GATE_MODE=report``, set
on that one seat, which turned off ALL identifier blocking there: SSNs and case
numbers included, invisible to review. At a hundred seats, a global edit for
one client changes every client, and a per-seat secret is a loosening nobody
reviews.

THE MODEL (ss-console ADR 0093, docs/adr/0093-seat-policy-profiles.md). Three
layers, two of them here:

* **Floors, in code.** SSN, A-number, USCIS receipt number, case number and a
  (case number, date) PAIR always block on every seat, under any policy. Nothing
  authorable can name them: :data:`RELAXABLE_KINDS` is the closed set a policy
  may touch, and it is DATE alone. The floor never keys on the free-text
  ``vertical``, so ``mixed`` or a typo cannot lose it.
* **Role defaults, in the image.** A seat's ``seat.role`` names a role package
  the image carries at ``${ROLES_DIR:-/app/roles}/<role>/role.yaml``; its
  ``policy:`` block may loosen three narrow knobs (schema below). A loosening must
  carry ``rationale`` and ``adr``; one without is refused. The role file ships
  in the same image as this reader, so the two cannot skew.
* **Platform default** is exactly the behavior before profiles existed: dates
  block, the paralegal checklist applies. No role, an unknown role, a missing or
  unparseable file, an unknown key or a bad value all resolve here, with one
  warning logged per distinct cause. A broken policy can only make a seat
  STRICTER.

Schema (role.yaml)::

    policy:
      identifier_gate:
        recurring_dates: verify_by_month_day   # or block (default)
        provenance_sources: [calendar_invite_list, couples_settings_read]  # default []
      output_checklist:
        profile: household                     # or paralegal (default)
      rationale: "<why this role needs it>"
      adr: "<ss-console ADR that approved it>"

``provenance_sources``: read tools over stores the seat OWNS (closed list,
:data:`SEAT_OWNED_SOURCE_TOOLS`) whose results seed the session's register on
this role, as a tenant read does. Naming ``calendar_invite_list`` also lets an
invitation's own dates (its start, its end, its repeat rule's last day) verify
in the one email that carries that invitation: the body describes the act, it
is not a claim about the world. Set only by the calendar plugin's code, for that
one send (``provenance.self_described_dates``); dates in free text stay checked.

``verify_by_month_day``: a written date (with a year) verifies when its month
and day equal a recurring date read THIS session from the seat's record store
(a year-less ``06-14``, a month-name date with no year, or a dated record from
an earlier year: a birthday, an anniversary), and its year is this year or next.
That is the computed occurrence of an authored yearly date. An invented trip
date verifies nothing: it is in no record, and a dated record for this year or
later is not treated as recurring.

The rollback lever ``SMD_IDENTIFIER_GATE_MODE=report`` keeps its full meaning as
a FLEET-WIDE incident rollback (it outranks policy and downgrades every kind to
report). It is not per-seat posture. :func:`lever_set` reports it so seat facts
and the boot smoke can flag any seat where it is set; ``python3 -m
shared.policy_profile`` prints the stable marker line (see :func:`marker`).

Read per call (the ``shared.send_policy.live_send_policy`` pattern): nothing is
cached, so a re-provisioned role takes effect on the next gate decision.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from shared import identifier_filter

logger = logging.getLogger(__name__)

DEFAULT_ROLES_DIR = "/app/roles"

RECURRING_BLOCK = "block"
RECURRING_VERIFY = "verify_by_month_day"
RECURRING_VALUES: frozenset[str] = frozenset({RECURRING_BLOCK, RECURRING_VERIFY})

PROFILE_PARALEGAL = "paralegal"
PROFILE_HOUSEHOLD = "household"
PROFILE_VALUES: frozenset[str] = frozenset({PROFILE_PARALEGAL, PROFILE_HOUSEHOLD})

#: What seat facts and the audit row call the identifier posture.
MODE_BLOCK = "block"
MODE_VERIFY_RECURRING = "verify_recurring"
MODE_REPORT_LEVER = "report"

LEVER_ENV = "SMD_IDENTIFIER_GATE_MODE"

#: The only identifier kinds a policy may relax. Everything else that blocks is
#: a floor. Defined by inclusion so a new kind added to the vendored filter is a
#: floor by default.
RELAXABLE_KINDS: frozenset[identifier_filter.IdKind] = frozenset({identifier_filter.IdKind.DATE})

#: Kinds that block on every seat whatever its policy says.
FLOOR_KINDS: frozenset[identifier_filter.IdKind] = (
    frozenset(identifier_filter.IdKind) - {identifier_filter.IdKind.NAME} - RELAXABLE_KINDS
)

_REQUIRED_FLOORS = frozenset(
    {
        identifier_filter.IdKind.SSN,
        identifier_filter.IdKind.A_NUMBER,
        identifier_filter.IdKind.RECEIPT_NUMBER,
        identifier_filter.IdKind.CASE_NUMBER,
        identifier_filter.IdKind.PAIR,
    }
)
if not _REQUIRED_FLOORS <= FLOOR_KINDS:  # pragma: no cover - an import-time tripwire
    raise RuntimeError("policy_profile: a required identifier floor is relaxable")

#: Read tools over stores the SEAT OWNS that a role may name as provenance
#: sources (``identifier_gate.provenance_sources``). Code-side and closed: a
#: role names from this list or its policy is refused. Fleet-wide these are
#: pinned NON-sources (tests/test_provenance_sources.py), because what they list
#: is mostly what the seat itself wrote; a role whose job is that store (a
#: couple's own calendar and schedule) opts in, with rationale and adr.
SEAT_OWNED_SOURCE_TOOLS: frozenset[str] = frozenset(
    {"calendar_invite_list", "couples_settings_read"}
)

_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_POLICY_KEYS = frozenset({"identifier_gate", "output_checklist", "rationale", "adr"})
_IDENTIFIER_KEYS = frozenset({"recurring_dates", "provenance_sources"})
_CHECKLIST_KEYS = frozenset({"profile"})


@dataclass(frozen=True)
class Policy:
    """The resolved posture of one seat. Frozen; compute a new one per decision."""

    role: str | None = None
    recurring_dates: str = RECURRING_BLOCK
    checklist_profile: str = PROFILE_PARALEGAL
    #: ``platform_default``, ``role:<role>``, or ``fallback:<cause>``.
    source: str = "platform_default"
    rationale: str | None = None
    adr: str | None = None
    lever: bool = False
    #: Seat-owned read tools this role treats as provenance sources.
    provenance_sources: frozenset[str] = frozenset()

    @property
    def loosened(self) -> bool:
        """True when anything is looser than the platform default (lever aside)."""
        return (
            self.recurring_dates != RECURRING_BLOCK
            or self.checklist_profile != PROFILE_PARALEGAL
            or bool(self.provenance_sources)
        )

    @property
    def own_calendar_is_source(self) -> bool:
        """The role treats its own calendar as a record: its reads seed, and an
        invitation's own dates verify in the body that sends it."""
        return "calendar_invite_list" in self.provenance_sources

    @property
    def identifier_mode(self) -> str:
        """``report`` (the fleet lever), ``verify_recurring`` or ``block``."""
        if self.lever:
            return MODE_REPORT_LEVER
        if self.recurring_dates == RECURRING_VERIFY:
            return MODE_VERIFY_RECURRING
        return MODE_BLOCK


PLATFORM_DEFAULT = Policy()

_warned: set[str] = set()
_warned_lock = threading.Lock()


def _warn_once(cause: str, detail: str) -> None:
    with _warned_lock:
        if cause in _warned:
            return
        _warned.add(cause)
    logger.warning("policy_profile: %s; applying the platform default (%s)", detail, cause)


def lever_set() -> bool:
    """True when the fleet rollback lever downgrades the identifier gate to report."""
    return os.environ.get("SMD_IDENTIFIER_GATE_MODE", "").strip().lower() == "report"


def roles_dir(explicit: str | None = None) -> Path:
    """The explicit dir, else ``$ROLES_DIR``, else the image's ``/app/roles``."""
    return Path(explicit or os.environ.get("ROLES_DIR") or DEFAULT_ROLES_DIR)


def _seat_block(cfg: Any) -> Any:
    """``seat`` from a parsed dict or a ``CustomerConfig`` (its raw document)."""
    raw = cfg if isinstance(cfg, dict) else cfg.raw
    if not isinstance(raw, dict):
        raise TypeError("customer.yaml root is not a mapping")
    seat = raw.get("seat")
    if seat is not None and not isinstance(seat, dict):
        raise TypeError("customer.yaml seat is not a mapping")
    return seat


def _load_cfg() -> Any:
    from shared.customer_config import CustomerConfig

    return CustomerConfig.from_volume()


def _fallback(role: str | None, cause: str, detail: str, lever: bool) -> Policy:
    _warn_once(cause, detail)
    return Policy(role=role, source=f"fallback:{cause}", lever=lever)


def resolve_role_policy(role: str | None, raw: Any, *, lever: bool = False) -> Policy:
    """Validate a role's ``policy:`` block. Anything malformed resolves strict.

    Pure: the matrix test calls this with the authored block straight from the
    ss-console projection, so the CI answer and the seat's answer come from the
    same code.
    """
    if raw is None:
        return Policy(role=role, source=f"role:{role}" if role else "platform_default", lever=lever)
    if not isinstance(raw, dict):
        return _fallback(role, f"{role}:not_a_mapping", "role policy is not a mapping", lever)
    unknown = set(raw) - _POLICY_KEYS
    if unknown:
        return _fallback(
            role, f"{role}:unknown_key", f"role policy has unknown keys {sorted(unknown)}", lever
        )
    recurring = RECURRING_BLOCK
    ident = raw.get("identifier_gate")
    if ident is not None:
        if not isinstance(ident, dict) or set(ident) - _IDENTIFIER_KEYS:
            return _fallback(role, f"{role}:identifier_gate", "identifier_gate is malformed", lever)
        recurring = ident.get("recurring_dates", RECURRING_BLOCK)
        if recurring not in RECURRING_VALUES:
            return _fallback(
                role, f"{role}:recurring_dates", "identifier_gate.recurring_dates is unknown", lever
            )
        named = ident.get("provenance_sources", [])
        if (
            not isinstance(named, list)
            or not all(isinstance(n, str) for n in named)
            or len(set(named)) != len(named)
            or set(named) - SEAT_OWNED_SOURCE_TOOLS
        ):
            return _fallback(
                role,
                f"{role}:provenance_sources",
                "identifier_gate.provenance_sources names a tool off the seat-owned list",
                lever,
            )
        sources = frozenset(named)
    else:
        sources = frozenset()
    profile = PROFILE_PARALEGAL
    checklist = raw.get("output_checklist")
    if checklist is not None:
        if not isinstance(checklist, dict) or set(checklist) - _CHECKLIST_KEYS:
            return _fallback(
                role, f"{role}:output_checklist", "output_checklist is malformed", lever
            )
        profile = checklist.get("profile", PROFILE_PARALEGAL)
        if profile not in PROFILE_VALUES:
            return _fallback(role, f"{role}:profile", "output_checklist.profile is unknown", lever)
    rationale = raw.get("rationale")
    adr = raw.get("adr")
    for name, value in (("rationale", rationale), ("adr", adr)):
        if value is not None and not isinstance(value, str):
            return _fallback(role, f"{role}:{name}", f"{name} is not a string", lever)
    resolved = Policy(
        role=role,
        recurring_dates=recurring,
        checklist_profile=profile,
        source=f"role:{role}",
        rationale=(rationale or "").strip() or None,
        adr=(adr or "").strip() or None,
        lever=lever,
        provenance_sources=sources,
    )
    if resolved.loosened and not (resolved.rationale and resolved.adr):
        return _fallback(
            role, f"{role}:uncited", "a loosening without rationale and adr is refused", lever
        )
    return resolved


def current(cfg: Any = None, *, roles_dir_override: str | None = None) -> Policy:
    """This seat's policy, read now. Never raises; any fault is the platform default."""
    lever = lever_set()
    try:
        if cfg is None:
            cfg = _load_cfg()
        seat = _seat_block(cfg)
    except Exception as exc:  # noqa: BLE001 - an unreadable config is a strict seat
        return _fallback(None, "config_unreadable", f"customer.yaml unreadable ({exc})", lever)
    role = seat.get("role") if isinstance(seat, dict) else None
    if role is None or role == "":
        return Policy(lever=lever)
    if not isinstance(role, str) or not _ROLE_RE.match(role):
        return _fallback(None, "bad_role_slug", "seat.role is not a role slug", lever)
    path = roles_dir(roles_dir_override) / role / "role.yaml"
    try:
        manifest = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return _fallback(role, f"{role}:no_role_file", f"{path} does not exist", lever)
    except Exception as exc:  # noqa: BLE001 - unreadable or invalid YAML
        return _fallback(role, f"{role}:unreadable", f"{path} unreadable ({exc})", lever)
    if not isinstance(manifest, dict) or manifest.get("role") != role:
        return _fallback(role, f"{role}:wrong_manifest", f"{path} does not declare role", lever)
    return resolve_role_policy(role, manifest.get("policy"), lever=lever)


# ---------------------------------------------------------------------------
# verify_by_month_day
# ---------------------------------------------------------------------------

_MONTHS = (
    r"January|February|March|April|May|June|July|August|September|October|November|"
    r"December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
#: A field whose WHOLE value is a month and day: ``date: 06-14`` (the
#: dates-to-remember record shape), ``birthday: 6/14``.
_FIELD_MONTH_DAY = re.compile(
    r"(?:^|\n)[ \t]*[A-Za-z][A-Za-z _-]{0,30}:[ \t]*(\d{1,2})[-/](\d{1,2})[ \t]*(?=\n|$)"
)
#: A month-name date that carries no year: "June 14", "14th June".
_NAME_MONTH_DAY = re.compile(
    rf"\b(?:(?P<m1>{_MONTHS})\.?\s+(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"|(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<m2>{_MONTHS})\.?)\b"
    r"(?!,?\s+\d{4})",
    re.IGNORECASE,
)


def _month_number(name: str) -> int | None:
    key = name.strip(".").lower()[:3]
    for i, month in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    ):
        if key == month:
            return i
    return None


def _month_day(month: int | None, day: int) -> str | None:
    if month is None or not 1 <= month <= 12:
        return None
    try:
        datetime.date(2024, month, day)  # a leap year, so 02-29 is a real date
    except ValueError:
        return None
    return f"{month:02d}-{day:02d}"


def _flatten(payload: Any, depth: int = 0) -> list[str]:
    """Render a JSON tool result as ``key: value`` lines, strings verbatim."""
    if depth > 6:
        return []
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        out: list[str] = []
        for key, value in payload.items():
            if isinstance(value, str):
                # As a field (``date: 06-14`` in a listing) and as text (a
                # record's ``content``, whose own first line is a field).
                out.append(f"{key}: {value}")
                out.append(value)
            else:
                out.extend(_flatten(value, depth + 1))
        return out
    if isinstance(payload, list):
        out = []
        for item in payload[:500]:
            out.extend(_flatten(item, depth + 1))
        return out
    return []


def recurring_month_days(text: str, *, today: datetime.date | None = None) -> frozenset[str]:
    """``MM-DD`` of every recurring date in a record-store read result.

    Recurring means: a year-less month and day, or a full date from a year
    before this one (a birthday, an anniversary). A full date this year or later
    is a one-off (a trip, a reservation) and contributes nothing.
    """
    if not isinstance(text, str) or not text:
        return frozenset()
    year = (today or datetime.datetime.now(datetime.timezone.utc).date()).year
    try:
        chunks = _flatten(json.loads(text))
    except (ValueError, TypeError):
        chunks = [text]
    found: set[str] = set()
    for chunk in chunks:
        for match in _FIELD_MONTH_DAY.finditer(chunk):
            md = _month_day(int(match.group(1)), int(match.group(2)))
            if md:
                found.add(md)
        for match in _NAME_MONTH_DAY.finditer(chunk):
            month = _month_number(match.group("m1") or match.group("m2"))
            md = _month_day(month, int(match.group("d1") or match.group("d2")))
            if md:
                found.add(md)
        for pattern in identifier_filter._DATE_RES:
            for match in pattern.finditer(chunk):
                canonical = identifier_filter._canon_date(match.group(0))
                if canonical and int(canonical[:4]) < year:
                    found.add(canonical[5:])
    return frozenset(found)


def occurrence_years(today: datetime.date | None = None) -> frozenset[int]:
    """Years an occurrence may carry: this year or next, on UTC today or yesterday.

    The same two-day window as the gate's ambient dates, so a US-local "today"
    on New Year's Eve still finds its year.
    """
    now = today or datetime.datetime.now(datetime.timezone.utc).date()
    years = {now.year, (now - datetime.timedelta(days=1)).year}
    return frozenset(years | {y + 1 for y in years})


def recurring_occurrence_verifies(
    canonical: str, month_days: frozenset[str], *, today: datetime.date | None = None
) -> bool:
    """True when ``canonical`` (``YYYY-MM-DD``) is this or next year's occurrence
    of a recurring date the session read."""
    if not month_days or not isinstance(canonical, str) or len(canonical) != 10:
        return False
    try:
        written = datetime.date.fromisoformat(canonical)
    except ValueError:
        return False
    return canonical[5:] in month_days and written.year in occurrence_years(today)


# ---------------------------------------------------------------------------
# The stable marker the boot smoke greps
# ---------------------------------------------------------------------------


def marker(policy: Policy | None = None) -> str:
    """One value-free line: ``policy_profile role=<r> identifier=<mode>
    checklist=<profile> sources=<tools|-> source=<source> lever=<set|unset>``.

    ``lever=set`` on any seat is a fleet incident rollback in force; the boot
    smoke flags it.
    """
    p = policy or current()
    return (
        f"policy_profile role={p.role or '-'} identifier={p.identifier_mode} "
        f"checklist={p.checklist_profile} sources={','.join(sorted(p.provenance_sources)) or '-'} "
        f"source={p.source} "
        f"lever={'set' if p.lever else 'unset'}"
    )


def main(argv: list[str] | None = None) -> int:
    """``python3 -m shared.policy_profile [--customer-yaml PATH] [--check-lever]``.

    Prints :func:`marker`. With ``--check-lever`` exits 1 when the lever is set.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    policy: Policy | None = None
    cfg = None
    if "--customer-yaml" in args:
        from shared.customer_config import CustomerConfig

        index = args.index("--customer-yaml")
        try:
            cfg = CustomerConfig.from_volume(args[index + 1])
        except Exception as exc:  # noqa: BLE001 - reported, then strict
            policy = _fallback(
                None, "config_unreadable", f"customer.yaml unreadable ({exc})", lever_set()
            )
    if policy is None:
        policy = current(cfg)
    print(marker(policy))
    if "--check-lever" in args and policy.lever:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "FLOOR_KINDS",
    "LEVER_ENV",
    "MODE_BLOCK",
    "MODE_REPORT_LEVER",
    "MODE_VERIFY_RECURRING",
    "PLATFORM_DEFAULT",
    "PROFILE_HOUSEHOLD",
    "PROFILE_PARALEGAL",
    "RECURRING_BLOCK",
    "RECURRING_VERIFY",
    "RELAXABLE_KINDS",
    "Policy",
    "current",
    "lever_set",
    "marker",
    "occurrence_years",
    "recurring_month_days",
    "recurring_occurrence_verifies",
    "resolve_role_policy",
]
