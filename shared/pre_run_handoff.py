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

* **A projection, not a payload.** Only DATE atoms are handed over and only date
  atoms are seeded. An ACK code, a subject line, a matter caption or a sentence
  of the script's prose never reaches the register — a value the script composed
  must not verify just because the script wrote it down.
* **Bound to one session.** The file carries the instant the script started, and
  a session may take it only if that session began within
  :data:`DEFAULT_WINDOW` of it. Yesterday's file cannot certify today's draft.
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


def handoff_dir(hermes_home: str | None = None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    return Path(home) / _HANDOFF_RELDIR


def handoff_path(skill: str, hermes_home: str | None = None) -> Path:
    """Where ``skill``'s handoff lives. One file per skill, overwritten each run.

    The skill name is sanitized rather than trusted: it reaches this module from
    a script's own ``--skill`` argument, and a name containing a separator would
    otherwise choose the path. A sanitized name can only ever name a file inside
    the handoff directory.
    """
    return handoff_dir(hermes_home) / f"{_safe_skill(skill)}.json"


def consumed_path(skill: str, hermes_home: str | None = None) -> Path:
    """Where a taken handoff is renamed to. Kept (not deleted) so an operator
    reading the volume after a refusal can see whether a handoff existed at
    all — the difference between "the script wrote nothing" and "the window
    missed" is the first question anyone will ask."""
    return handoff_dir(hermes_home) / f"{_safe_skill(skill)}.consumed.json"


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


def write_handoff(
    skill: str,
    started_at: datetime,
    dates: Iterable[object] | None,
    matter_ids: Iterable[object] | None = None,
    hermes_home: str | None = None,
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
    saw, and withheld from the projection because seeding matter numbers would
    widen the gate along an axis nothing has asked for — the 2026-08-19 refusals
    were date atoms, and a control should close the hole that was measured.

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
) -> dict | None:
    """The projection this session may seed from, or ``None``.

    Returns ``{"dates": [...]}`` and nothing else, where every entry is a value
    the identifier gate itself reads as a date (:func:`_date_atoms`). Two
    narrowings, both enforced here rather than asked for at the call site,
    because a caller cannot seed what it never receives: the file's other FIELDS
    are not returned, and a non-date sitting in the ``dates`` field is not
    returned either.

    Four ways to get ``None``, and they are all the same answer to the caller —
    seed nothing, let the gate do what it does today:

    * no file (the script did not run, or wrote no handoff);
    * an unreadable or malformed file;
    * ``session_started_at`` is ``None`` (a non-cron session — an interactive
      turn must never inherit a routine's reads);
    * the session started outside ``[started_at, started_at + window]``.

    ON THE TWO CLOCK READINGS. ``session_started_at`` arrives NAIVE, parsed from
    the digits in a cron session id (``cron_..._YYYYMMDD_HHMMSS``), and nothing
    in the id says whether the scheduler formatted local time or UTC. Both
    readings are tried and either may satisfy the window.

    That is close to free rather than a widening, because the two readings differ
    by exactly the seat's UTC offset. On a seat whose offset exceeds the window —
    the pilot and A&P are both Phoenix, UTC-7 — at most one reading can ever land
    inside a twenty-minute window, so trying both picks the right clock instead
    of guessing it. On a UTC seat the two readings are the same instant and the
    question does not arise. Only a seat within twenty minutes of UTC could see
    both match, and there the two candidate sessions are minutes apart anyway.

    The alternative was to pick one clock, and picking wrong is the failure this
    module exists to end: nothing would bind, nothing would seed, and the gate
    would keep refusing in silence with no signal that the control was inert.

    On success the file is renamed to ``<skill>.consumed.json`` before the
    projection is returned, so a second caller in the same session — or a retry
    of the same turn — gets ``None``. A file that does NOT bind is left exactly
    where it is: it may still belong to a session that has not started yet.
    """
    try:
        if session_started_at is None:
            return None
        path = handoff_path(skill, hermes_home)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None  # no handoff for this skill — the ordinary case
        try:
            payload = json.loads(raw)
        except ValueError:
            logger.warning("pre_run_handoff: %s is not valid JSON; ignoring", path)
            return None
        if not isinstance(payload, dict):
            return None
        started_at = _parse_iso(payload.get("started_at"))
        if started_at is None:
            return None
        if not _in_window(started_at, session_started_at, window):
            logger.info(
                "pre_run_handoff: %s is not bound to this session (started_at=%s, "
                "session=%s); leaving it in place",
                path,
                payload.get("started_at"),
                session_started_at.isoformat(),
            )
            return None
        # The projection. NOT ``_clean_atoms``: what the file offers and what the
        # register may be seeded from are two different lists, and the difference
        # is exactly the safety property (see :func:`_date_atoms`). Keeping the
        # file's own ``dates`` verbatim leaves the two visible side by side, so
        # "my date did not seed" is a diagnosable question rather than a silent
        # one.
        dates = _date_atoms(payload.get("dates"))
        # Consume BEFORE returning. A rename that failed after the caller had the
        # projection would leave a handoff that seeds every subsequent turn, which
        # is the sticky-provenance shape this binding exists to prevent.
        try:
            os.replace(path, consumed_path(skill, hermes_home))
        except OSError as exc:
            logger.warning(
                "pre_run_handoff: could not consume %s (%s); refusing to seed from a "
                "handoff that would stay claimable",
                path,
                exc,
            )
            return None
        return {"dates": dates}
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.warning("pre_run_handoff: take failed for %r: %s", skill, exc)
        return None


def _in_window(started_at: datetime, session_started_at: datetime, window: timedelta) -> bool:
    """True iff ``session_started_at``, read on EITHER clock, falls in the window."""
    span = max(window, timedelta(0))
    if session_started_at.tzinfo is not None:
        candidates = [session_started_at.astimezone(timezone.utc)]
    else:
        candidates = [
            session_started_at.replace(tzinfo=timezone.utc),  # the id was UTC
            session_started_at.astimezone(timezone.utc),  # the id was seat-local
        ]
    return any(started_at <= candidate <= started_at + span for candidate in candidates)


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


__all__ = [
    "DEFAULT_WINDOW",
    "consumed_path",
    "handoff_dir",
    "handoff_path",
    "take_handoff",
    "write_handoff",
]
