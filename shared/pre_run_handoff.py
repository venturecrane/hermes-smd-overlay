"""What a routine's pre-run script READ, handed to the one session it ran for.

THE GAP THIS CLOSES (ss-console#2547). The identifier-integrity gate refuses an
outbound identifier the Operator did not read this session
(``shared.provenance`` + ``shared.identifier_filter``). A cron routine's
``pre_run.py`` reads the firm's records through the broker BEFORE the session
exists — the deadline escalator pulls each matter's authored due date out of
Smokeball and hands it to the model as prompt text, verbatim, doing no date
arithmetic of its own — and nothing carried that read into the register. So on
2026-08-19 the escalator woke with a court date seven days out, tried five times
to tell a human, and was refused five times on the very dates it had just read.
The gate was right about what it could see: from inside the session those dates
had no source. What was missing was the source, not a looser rule.

So the script's read becomes a source, and the whole design is about keeping it
that and nothing more:

* **A projection, not a payload.** Only DATE atoms and validated
  ``(matterNumber, dates)`` records are handed over and seeded. An ACK code, a
  subject line, a matter caption or a sentence of the script's prose never
  reaches the register — a value the script composed must not verify just
  because the script wrote it down. Records seed as ASSOCIATIONS (pairs), so a
  seeded number cannot certify a date from a different matter.
* **Bound to one session.** The file carries the instant the script started,
  and a session may take it only while the file is younger than
  :data:`DEFAULT_WINDOW` (recency on the reader's own clock — see
  :func:`take_handoff` for why the session-stamp window this shipped with was
  inert in production). Yesterday's file cannot certify today's draft.
* **Consumed once.** Taking renames the file to ``<skill>.consumed.json``, so a
  second turn — a retry, a peer thread, an interactive session that happens to
  resolve to the same routine — finds nothing.
* **Unwritable from inside a turn.** ``$HERMES_HOME/.smd/`` is fenced against
  every write-class tool by ``hermes-smd-trust``'s ``pre_tool_call``
  (``_smd_dir_fence``), so the agent cannot author its own provenance. A
  sentinel the agent can write is not a sentinel.

WHY A FILE. The writer is a separate short-lived PROCESS — ``pre_run.py``, run
by the scheduler before the turn — and the reader is the agent process's
``pre_llm_call`` hook. Nothing else crosses that boundary; this is the same
crossing (and the same directory, on the same Fly volume) that
``shared.audit_status``, ``shared.audit_failure_counter`` and
``shared.webhook_surface_check`` already make.

Every entry point here is best-effort and returns rather than raises. A handoff
that cannot be written or read costs exactly what today costs: the register is
not seeded, the gate refuses, and a human re-reads the source. Failing the other
way — raising into the pre-run or into a hook — would take down a routine to
protect a convenience.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_HERMES_HOME = "/opt/data"

#: Relative to ``$HERMES_HOME``, beside ``audit_status.json`` and the write-
#: failure tally — same directory, same volume, same process crossing.
_HANDOFF_RELDIR = Path(".smd") / "pre_run"

#: How long after the script started a session may still claim its handoff.
#:
#: The scheduler runs ``pre_run.py`` and then starts the turn, so the real gap is
#: seconds to a couple of minutes (the script's own broker reads dominate it).
#: Twenty minutes is generous enough to survive a slow Smokeball page and a
#: cold model, and short enough that the NEXT day's run of the same daily
#: routine — the only other session that could plausibly find this file — is
#: never in range.
DEFAULT_WINDOW = timedelta(minutes=20)

#: Cap on how many date atoms one handoff may carry. A routine's digest names a
#: handful of deadlines; a file naming thousands is a bug or a probe, and either
#: way seeding it would be seeding a haystack.
_MAX_DATES = 200

#: Cap on the length of a single atom. Real dates are ten characters.
_MAX_ATOM_CHARS = 64

#: Cap on structured records one handoff may carry. A digest names tens of
#: matters, not hundreds; past this the file is a bug or a probe.
_MAX_RECORDS = 100

#: A bare-digit matter number (ss#2458). Some firms number matters with plain
#: digit runs ("201537", "4853") that no identifier-gate pattern matches — and
#: before this branch existed, :func:`_record_entries` dropped every such
#: record AT SEEDING, so the handoff's pair seeding delivered nothing for that
#: firm and the digest gate refused numbers the script had just read. Three to
#: ten digits: two digits is an ordinal or a day, eleven-plus is a GUID
#: fragment or an account number, and neither is a matter number anywhere.
_BARE_MATTER_NUMBER_RE = re.compile(r"\d{3,10}")

#: How far in the FUTURE a file's ``started_at`` may sit and still bind.
#: Recency binding compares the writer's stamp against the reader's clock;
#: both are the same Machine, but two processes can disagree by scheduler
#: latency and coarse clock steps. Two minutes absorbs that without letting a
#: stamp meaningfully from the future (a corrupt or forged value) bind.
_MAX_CLOCK_SKEW = timedelta(minutes=2)


def handoff_dir(hermes_home: str | None = None, persona: str | None = None) -> Path:
    """The handoff directory — under the PERSONA home when ``persona`` is given.

    WHY TWO ROOTS (the 2026-08-24 pilot probe, ss-console#2547 defect A). The
    scheduler runs a persona's ``pre_run.py`` with ``HERMES_HOME`` set to the
    persona home (``/opt/data/profiles/operator``), so the writer lands its file
    under that root. The agent process reading in ``pre_llm_call`` has
    ``HERMES_HOME=/opt/data``. The merged seeding shipped, ran, wrote a perfect
    file — and the reader looked one root up and found nothing, every day. A
    reader that knows the routine's persona can look where the writer actually
    wrote; the plain root stays as the fallback for seats and tests where the
    two processes share one ``HERMES_HOME``.
    """
    home = hermes_home or os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    base = Path(home)
    if persona:
        base = base / "profiles" / _safe_skill(persona)
    return base / _HANDOFF_RELDIR


def handoff_path(skill: str, hermes_home: str | None = None, persona: str | None = None) -> Path:
    """Where ``skill``'s handoff lives. One file per skill, overwritten each run.

    The skill name is sanitized rather than trusted: it reaches this module from
    a script's own ``--skill`` argument, and a name containing a separator would
    otherwise choose the path. A sanitized name can only ever name a file inside
    the handoff directory. The persona name gets the same treatment for the same
    reason.
    """
    return handoff_dir(hermes_home, persona) / f"{_safe_skill(skill)}.json"


def consumed_path(skill: str, hermes_home: str | None = None, persona: str | None = None) -> Path:
    """Where a taken handoff is renamed to. Kept (not deleted) so an operator
    reading the volume after a refusal can see whether a handoff existed at
    all — the difference between "the script wrote nothing" and "the window
    missed" is the first question anyone will ask."""
    return handoff_dir(hermes_home, persona) / f"{_safe_skill(skill)}.consumed.json"


def _safe_skill(skill: str) -> str:
    """A file-name-safe form of a skill name. Never empty, never a path."""
    raw = skill if isinstance(skill, str) else ""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in raw).strip("-")
    return cleaned or "unnamed"


def _clean_atoms(values: Iterable[object] | None) -> list[str]:
    """Bounded, de-duplicated, order-preserving list of short non-empty strings."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or len(text) > _MAX_ATOM_CHARS or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= _MAX_DATES:
            break
    return out


def _date_atoms(values: Iterable[object] | None) -> list[str]:
    """The values a DATE, and only a DATE, can be read out of.

    THE PROJECTION IS ENFORCED HERE, ON THE READ SIDE, and that is not belt and
    braces. The writer is an inline copy living in another repository — one
    twelve-line block per routine, in ``ss-console``'s ``pre_run.py`` files — so
    what lands in the ``dates`` field is authored by code this module cannot see
    or test. Trusting the field NAME would make "only dates seed" a promise about
    somebody else's script instead of a property of this gate, and a script that
    printed an ACK code into that list would be laundering it.

    The predicate is the identifier gate's OWN extractor, run against an empty
    register so every identifier in the string comes back as a hit. A value
    qualifies when it yields at least one hit and every hit is a DATE. Same code
    deciding "is this a date" as decides "is this hit a date" — a second opinion
    here would be a second opinion about the one question both sides must answer
    identically.
    """
    from shared.identifier_filter import IdKind, ProvenanceRegister, unverified_identifiers

    out: list[str] = []
    for text in _clean_atoms(values):
        try:
            hits = unverified_identifiers(text, ProvenanceRegister())
        except Exception:  # noqa: BLE001 — an unscannable atom is not a date
            continue
        if hits and all(hit.kind is IdKind.DATE for hit in hits):
            out.append(text)
    return out


def _record_entries(values: object) -> list[dict]:
    """The structured records a session may seed associations from.

    Same read-side enforcement posture as :func:`_date_atoms`, one level up: the
    writer is code in another repository, so the field SHAPE is not trusted. A
    record qualifies when its ``matterNumber`` reads as a case number and
    nothing else under the identifier gate's own extractor, and its ``dates``
    survive :func:`_date_atoms`. Anything the extractor reads differently — an
    ACK code, a GUID, a sentence — is dropped whole, so a script cannot launder
    a composed value into the register by wrapping it in a record.

    WHY RECORDS AT ALL (2026-08-24, the degraded-digest incident). The original
    projection carried dates only, and matter ids were deliberately withheld —
    at that point nothing had asked for them. Then the pilot's escalator shipped
    a digest where every item read "matter number unavailable", and the fix the
    Captain directed projects real matter numbers from the firm's records into
    the digest by code. Those numbers must verify, and they must verify AS
    ASSOCIATIONS — ``(number, date)`` pairs per record — because a bare-atom
    seeding would let the model pair any seeded number with any seeded date,
    which is the exact mispairing the register's pair check exists to catch.
    """
    from shared.identifier_filter import IdKind, ProvenanceRegister, unverified_identifiers

    out: list[dict] = []
    if not isinstance(values, list):
        return out
    for entry in values:
        if len(out) >= _MAX_RECORDS:
            break
        if not isinstance(entry, dict):
            continue
        number = entry.get("matterNumber")
        if not isinstance(number, str):
            continue
        number = number.strip()
        if not number or len(number) > _MAX_ATOM_CHARS:
            continue
        try:
            hits = unverified_identifiers(number, ProvenanceRegister())
        except Exception:  # noqa: BLE001 — an unscannable value is not a number
            continue
        if not hits or not all(hit.kind is IdKind.CASE_NUMBER for hit in hits):
            # Second acceptance branch (ss#2458): a bare digit run. Safe at
            # THIS seam and no other, because both sides of it are code: the
            # value was produced by the gate's own connector pull (the writer
            # projects `matterNumber` off resolved records) and is consumed by
            # structured add_record seeding, so the shape check here guards
            # against junk, not collision — and an exact code-read value is
            # not junk. This widens no extraction pattern: a bare number still
            # extracts from nothing; it only stops being DROPPED when the
            # firm's own record spells it that way.
            if not _BARE_MATTER_NUMBER_RE.fullmatch(number):
                continue
        dates = _date_atoms(entry.get("dates"))
        if not dates:
            continue
        out.append({"matterNumber": number, "dates": dates})
    return out


def write_handoff(
    skill: str,
    started_at: datetime,
    dates: Iterable[object] | None,
    matter_ids: Iterable[object] | None = None,
    hermes_home: str | None = None,
    records: list[dict] | None = None,
) -> Path | None:
    """Record what this run of ``skill`` read, for the session about to start.

    ``started_at`` is when the SCRIPT began — not when it finished. The binding
    window opens there because that is the instant a session can be compared
    against: the scheduler starts the turn after the script exits, so a window
    anchored at the script's start covers its whole run plus the turn's own
    start-up, while an end-anchored window would shift with how slow the firm's
    connector happened to be that morning.

    ``matter_ids`` are recorded but NOT projected by :func:`take_handoff`. They
    are written because the file is also a forensic record of what the script
    saw. (They were originally withheld from the projection because nothing had
    asked for matter numbers; since the 2026-08-24 degraded-digest incident the
    projection DOES carry matter numbers — but only through ``records``, whose
    read-side validation is :func:`_record_entries`. Bare GUID matter ids still
    never seed anything.)

    ``records`` is the structured half: ``[{"matterNumber": …, "dates": […]}]``,
    one entry per matter the script's pull resolved, associations known in code
    rather than inferred. Stored lightly here; validated on the read side.

    Atomic by temp-file + rename inside the same directory, so a reader can only
    ever see a whole file. 0600 on the file, 0700 on the directory: the handoff
    is provenance, and provenance another local process can rewrite is not
    provenance.

    Returns the path written, or ``None`` on any failure. Never raises — a
    pre-run must not die because an optimization could not be recorded.
    """
    try:
        directory = handoff_dir(hermes_home)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "skill": str(skill),
            "started_at": _as_utc(started_at).isoformat(),
            "dates": _clean_atoms(dates),
            "matter_ids": _clean_atoms(matter_ids),
        }
        if records:
            payload["records"] = [
                {
                    "matterNumber": str(entry.get("matterNumber") or ""),
                    "dates": _clean_atoms(entry.get("dates")),
                }
                for entry in records[:_MAX_RECORDS]
                if isinstance(entry, dict)
            ]
        target = handoff_path(skill, hermes_home)
        fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".handoff-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, target)
        except BaseException:
            # Leave no torn temp file behind on any exit path, including a
            # KeyboardInterrupt during a scheduler shutdown.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return target
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.warning("pre_run_handoff: could not write handoff for %r: %s", skill, exc)
        return None


def take_handoff(
    skill: str,
    session_started_at: datetime | None,
    hermes_home: str | None = None,
    window: timedelta = DEFAULT_WINDOW,
    persona: str | None = None,
    now: datetime | None = None,
) -> dict | None:
    """The projection this session may seed from, or ``None``.

    Returns ``{"dates": [...], "records": [...]}`` and nothing else: dates that
    the identifier gate itself reads as dates (:func:`_date_atoms`), and records
    that survive :func:`_record_entries` — the file's other fields are not
    returned, and a non-date in ``dates`` or a non-case-number in a record is
    not returned either. Enforced here rather than asked for at the call site,
    because a caller cannot seed what it never receives.

    Ways to get ``None``, all the same answer to the caller — seed nothing, let
    the gate do what it does today:

    * no file (the script did not run, or wrote no handoff);
    * an unreadable or malformed file;
    * ``session_started_at`` is ``None`` (a non-cron session — an interactive
      turn must never inherit a routine's reads);
    * a ``started_at`` that is missing, unparseable, or NAIVE — the writer
      stamps an explicit UTC offset, and a stamp without one cannot be compared
      against any clock honestly;
    * a file older than ``window``, or stamped further than the skew allowance
      into the future.

    BINDING IS BY FILE RECENCY, NOT BY THE SESSION STAMP (2026-08-24, defect B).
    The first shipped version parsed the digits out of the cron session id and
    window-matched them against the file, trying the naive stamp on both the UTC
    and the process-local clock — on the theory that those two readings differ by
    the seat's UTC offset. The pilot falsified the theory the first morning it
    ran: the scheduler stamps the id with the fire time in the ROUTINE'S cron
    timezone (Phoenix, ``…_070026`` for a 14:00Z fire) while the container's
    local clock is UTC, so both readings collapsed to the same wrong instant,
    seven hours outside a twenty-minute window. Nothing bound, nothing seeded,
    and the gate kept refusing in silence — the exact inert-control failure this
    module's own docstring warned about.

    Recency needs no theory about a third clock this module cannot see. The
    scheduler runs the script and then starts the turn, so at the only moment a
    legitimate reader asks, the file is seconds-to-minutes old. ``started_at``
    (writer's clock, explicit UTC) against ``now`` (reader's clock, same
    Machine) spans one process boundary on one host. The cron-session guard
    (``session_started_at`` must parse at all) still keeps interactive sessions
    out; ``skill`` still names the one file; consume-once still bounds a claim
    to a single session.

    On success the file is renamed to ``<skill>.consumed.json`` before the
    projection is returned, so a second caller in the same session — or a retry
    of the same turn — gets ``None``. A file that does NOT bind is left exactly
    where it is, so an operator reading the volume after a refusal can see
    whether a handoff existed at all.

    ``persona`` names the routine's persona; the persona-home directory is
    tried FIRST (that is where a scheduler-run writer's ``HERMES_HOME`` points —
    defect A), then the plain root. ``now`` is a test seam; production callers
    leave it unset.
    """
    try:
        if session_started_at is None:
            return None
        path = None
        raw = None
        roots: list[str | None] = [persona, None] if persona else [None]
        found_persona: str | None = None
        for candidate_persona in roots:
            candidate = handoff_path(skill, hermes_home, candidate_persona)
            try:
                raw = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            path = candidate
            found_persona = candidate_persona
            break
        if path is None or raw is None:
            return None  # no handoff for this skill — the ordinary case
        try:
            payload = json.loads(raw)
        except ValueError:
            logger.warning("pre_run_handoff: %s is not valid JSON; ignoring", path)
            return None
        if not isinstance(payload, dict):
            return None
        started_at = _parse_iso_aware(payload.get("started_at"))
        if started_at is None:
            return None
        moment = _as_utc(now) if now is not None else datetime.now(timezone.utc)
        age = moment - started_at
        if age > max(window, timedelta(0)) or age < -_MAX_CLOCK_SKEW:
            logger.info(
                "pre_run_handoff: %s is not fresh (started_at=%s, now=%s); leaving it in place",
                path,
                payload.get("started_at"),
                moment.isoformat(),
            )
            return None
        # The projection. NOT ``_clean_atoms``: what the file offers and what the
        # register may be seeded from are two different lists, and the difference
        # is exactly the safety property (see :func:`_date_atoms`). Keeping the
        # file's own fields verbatim leaves the two visible side by side, so
        # "my date did not seed" is a diagnosable question rather than a silent
        # one.
        dates = _date_atoms(payload.get("dates"))
        records = _record_entries(payload.get("records"))
        # Consume BEFORE returning. A rename that failed after the caller had the
        # projection would leave a handoff that seeds every subsequent turn, which
        # is the sticky-provenance shape this binding exists to prevent.
        try:
            os.replace(path, consumed_path(skill, hermes_home, found_persona))
        except OSError as exc:
            logger.warning(
                "pre_run_handoff: could not consume %s (%s); refusing to seed from a "
                "handoff that would stay claimable",
                path,
                exc,
            )
            return None
        return {"dates": dates, "records": records}
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.warning("pre_run_handoff: take failed for %r: %s", skill, exc)
        return None


def _as_utc(value: datetime) -> datetime:
    """A timezone-aware UTC instant. A naive value is read on the local clock —
    the same reading ``datetime.astimezone`` gives it, and the clock a script
    calling ``datetime.now()`` was on."""
    if value.tzinfo is None:
        return value.astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _parse_iso_aware(value: object) -> datetime | None:
    """Like :func:`_parse_iso`, but a NAIVE stamp is rejected rather than read
    on the local clock. Recency binding compares the writer's stamp against the
    reader's clock; a stamp that does not say which clock it was on cannot make
    that comparison honest, and the real writer always stamps UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


__all__ = [
    "DEFAULT_WINDOW",
    "consumed_path",
    "handoff_dir",
    "handoff_path",
    "take_handoff",
    "write_handoff",
]
