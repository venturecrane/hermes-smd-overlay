"""Cross-process tally of audit rows that could not be persisted (ss-console #2498).

THE GAP THIS CLOSES. Every audit writer on the Machine swallows
``AuditWriteError`` — by design: the ledger is observability, and a decision
already enforced is never rolled back because its row failed to persist. The
cost is that a failed write leaves a GAP, and a gap is exactly what a quiet
seat also leaves. Reading the ledger, "the routines are off" and "the writer
is broken" are the same picture. Off-Machine, nothing learned of it at all:
``fleet_status`` carried ``last_audit_ts`` and nothing else, so a ledger that
stopped being written looked identical to a ledger with nothing to write.

WHY A FILE AND NOT A COUNTER IN MEMORY. The failures happen in the AGENT
process (hooks), in cron ``pre_run`` children, and in the config applier. The
heartbeat that reports them runs in the GATE process and opens the ledger
read-only in its own process (:func:`shared.heartbeat.read_audit_timestamps`).
A process variable cannot cross that boundary. This is the same crossing
``shared.audit_status`` and ``shared.webhook_surface_check`` already make, and
it uses the same directory for the same reason: ``$HERMES_HOME/.smd/`` is on
the Fly volume, written by the agent, read by the gate.

WHY A BYTE TALLY AND NOT A JSON COUNTER. Several processes bump this
concurrently and none of them may block or fail. A JSON read-modify-write
loses increments under exactly the burst a broker outage produces — the case
the counter exists for. Appending ONE BYTE per failure is atomic for every
writer under POSIX ``O_APPEND`` with no lock, and the count is then
``st_size``: O(1) to read, impossible to race, and readable by a process that
holds no write permission at all.

Deliberately NOT pid-guarded and deliberately NOT reset at boot, unlike the
sentinels next to it. Those answer "what is true THIS boot"; this answers "how
many rows has this seat ever lost", which is the question a console-side delta
can act on. A restart that reset the tally would re-alert on every reboot and
would hide failures that happened just before a crash.

Growth: one byte per lost row, on a volume, forever. A seat that loses ten
thousand rows has a ten-kilobyte file and a much larger problem.

WHY A REASON FILE BESIDE IT (2026-10-20). The count says THAT rows were lost and
nothing else. The reason was a rate-limited WARNING in the losing process's own
log: on a seat that is a file nobody may read (it carries client content), at
most once per five minutes per process, and Sentry saw it only as a breadcrumb.
So "captain-mark lost 2 rows after each release" was countable and could not be
explained. Each loss now also appends one line to
``audit_write_failures.reasons``: a reason CLASS from a closed vocabulary
(:data:`REASONS`) and, when known, the row's ``action_type``. Both are
constants of the code, never content: no recipient, no matter, no message
text, no metadata. The heartbeat reports the histogram and the console names
the classes in its warning. The tally stays the count of record; the reason
file explains it and is never used to compute it.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections import Counter
from pathlib import Path

from shared.audit_status import NoAuditWarner

logger = logging.getLogger(__name__)

#: A broker outage produces a BURST of failures, and every one of them must be
#: counted while at most one of them says so in the log. The tally is the
#: signal; the log line is the human-readable echo, rate-limited by the same
#: mechanism the other no-audit path already uses.
_WARNER = NoAuditWarner()

#: Relative to HERMES_HOME, alongside ``audit_status.json`` and
#: ``webhook_surface.json`` — same directory, same volume, same crossing.
_TALLY_RELPATH = Path(".smd") / "audit_write_failures.tally"
_REASONS_RELPATH = Path(".smd") / "audit_write_failures.reasons"

_DEFAULT_HERMES_HOME = "/opt/data"

#: Why a row was lost. Closed: a class outside this set is recorded as
#: ``unclassified``, so a caller can never put free text in the file.
REASONS = frozenset(
    {
        "socket_unset",  # SMD_AUDIT_BROKER_SOCKET unset on a broker seat
        "bad_row",  # the row did not have the contract's column count
        "broker_unreachable",  # nothing listening, and this writer may not hold
        "broker_io",  # the socket failed after connect: the row may have landed
        "broker_bad_reply",  # the broker answered something that is not JSON
        "refused_not_gateway",  # generic audit_append from a pid that is not the gateway
        "broker_refused",  # any other broker refusal
        "gap_full",  # the gateway's gap buffer was full
        "gap_deadline",  # the broker stayed unreachable past the retry deadline
        "gap_exit",  # rows still held when the gateway's interpreter exited
        "gap_dead_gateway",  # rows a killed gateway held, counted at the next boot
        "gap_flush_error",  # the gap retry thread hit an unexpected error
        "direct_write_failed",  # the direct (non-broker) D1 write failed
        "unclassified",
    }
)
_UNCLASSIFIED = "unclassified"
_ACTION_TYPE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_KEY_RE = re.compile(r"([a-z][a-z_]{0,39})(?::([A-Z][A-Z0-9_]{0,63}))?")
#: A reasons file is read whole on every beat; past this it is read from the end
#: (about 130,000 losses at ~30 bytes a line). The console keeps a per-key
#: high-water mark, so a key whose count falls out of the window is never
#: re-reported as new.
_REASONS_READ_CAP_BYTES = 4 * 1024 * 1024
#: Distinct keys reported per beat. More than this is not a pattern to read.
_MAX_REASON_KEYS = 32


def loss_key(reason: str | None, action_type: str | None = None) -> str:
    """``reason[:ACTION_TYPE]``, both checked against closed shapes."""
    cls = reason if reason in REASONS else _UNCLASSIFIED
    if isinstance(action_type, str) and _ACTION_TYPE_RE.fullmatch(action_type):
        return f"{cls}:{action_type}"
    return cls


def tally_path(hermes_home: str | None = None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    return Path(home) / _TALLY_RELPATH


def record_audit_write_failure(
    context: str,
    *,
    reason: str | None = None,
    action_type: str | None = None,
    hermes_home: str | None = None,
) -> bool:
    """Tally one lost audit row. Best-effort; NEVER raises.

    Returns True when the byte was appended. Returns False — silently as far
    as control flow is concerned — when the tally directory does not exist or
    the append fails.

    The directory is NEVER created here. On a real Machine ``.smd`` is created
    by :func:`shared.audit_status.write_audit_status` at plugin registration,
    before any hook can fire, so the tally always has a home when it matters.
    Off-Machine (CI, a dev shell, a unit test that constructs an
    ``AuditWriteError``) the directory is absent and this is a no-op, so
    importing the audit stack can never write to a developer's filesystem.

    ``context`` is logged, never written to the file: the tally is a count, and
    a count cannot leak a matter reference or a recipient. ``reason`` and
    ``action_type`` go to the sibling reasons file as one :func:`loss_key`
    line, after the byte, so a reason write that fails never costs the count.
    """
    path = tally_path(hermes_home)
    if not path.parent.is_dir():
        logger.debug("audit_failure_counter: no tally dir at %s; not counted", path.parent)
        return False
    try:
        fd = _open_append(path)
    except OSError as exc:
        logger.warning("audit_failure_counter: cannot open tally (%s): %s", path, exc)
        return False
    try:
        # One byte, one lost row. A single small write to an O_APPEND fd is
        # atomic across processes, so concurrent writers cannot lose a count.
        os.write(fd, b"\x01")
    except OSError as exc:
        logger.warning("audit_failure_counter: tally append failed (%s): %s", path, exc)
        return False
    finally:
        os.close(fd)
    key = loss_key(reason, action_type)
    _append_reason(path.parent / _REASONS_RELPATH.name, key)
    _WARNER.warn(logger, f"audit row lost [{key}] ({context}); tallied in {path}")
    return True


def _open_append(path: Path) -> int:
    """O_APPEND fd on ``path``, owned like its directory when root writes it.

    Root pollers (the config and skills appliers) share ``/opt/data/.smd`` with
    the agent-uid gateway and gate. A 0600 file root created first would lock
    them out of appending and the heartbeat out of reading the reasons, until
    the next boot's chown (review 2026-10-20). Best-effort: a failed chown
    leaves the file as it was, never costs the count.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if os.geteuid() == 0:
        try:
            st = os.stat(path.parent)
            os.fchown(fd, st.st_uid, st.st_gid)
        except OSError as exc:
            logger.debug("audit_failure_counter: could not hand %s to its dir owner: %s", path, exc)
    return fd


def _append_reason(path: Path, key: str) -> None:
    """One line, one O_APPEND write: atomic across writers like the tally byte."""
    try:
        fd = _open_append(path)
    except OSError as exc:
        logger.warning("audit_failure_counter: cannot open reasons (%s): %s", path, exc)
        return
    try:
        os.write(fd, key.encode("ascii") + b"\n")
    except OSError as exc:
        logger.warning("audit_failure_counter: reason append failed (%s): %s", path, exc)
    finally:
        os.close(fd)


def seat_home(hermes_home: str | None = None) -> Path:
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME)
    # A caller running inside a profile home still answers for the whole seat.
    if home.parent.name == "profiles":
        home = home.parent.parent
    return home


def read_audit_write_failures(hermes_home: str | None = None) -> int | None:
    """Total lost rows, or ``None`` when the seat cannot answer.

    Three states, and the difference between the last two is the point:

    * ``None``  — ``.smd`` does not exist. The audit plugin has never
      registered on this Machine, so the seat has no opinion; the console holds
      whatever it last knew rather than reporting a reassuring zero.
    * ``0``     — ``.smd`` exists and no tally file does. A REAL zero: the
      writer has been up and has lost nothing. This is the value that lets a
      recovered seat stop alerting.
    * ``n > 0`` — that many rows have been lost since the volume was created,
      summed across the seat home AND every profile home that still exists.

    WHY THE SUM. The tally lives under ``$HERMES_HOME``, and that is not one
    directory on a seat. The gateway runs as ``hermes -p operator gateway run``,
    so inside the process ``HERMES_HOME`` is ``/opt/data/profiles/operator`` and
    its losses land in ``profiles/operator/.smd``; the gate process that reports
    them runs with ``/opt/data``. Reading only the caller's own home made every
    row the GATEWAY lost invisible to the console: on pilot-smokeball 2026-09-26
    the heartbeat said 13 while the gateway's own file held 238 (its
    ``audit_status.json`` names pid 650, the gateway). Summing on read counts that
    history without moving a byte, and a writer in any profile is counted the
    moment it writes.
    """
    home = seat_home(hermes_home)
    root = tally_path(str(home))
    if not root.parent.is_dir():
        return None
    root_count = _tally_size(root, required=True)
    if root_count is None:
        return None
    total = root_count
    for profile_tally in sorted((home / "profiles").glob(f"*/{_TALLY_RELPATH}")):
        # A broken PROFILE tally is skipped, not fatal: the seat can still give an
        # honest lower bound from the files it can read, and the warning names it.
        total += _tally_size(profile_tally, required=False) or 0
    return total


def _tally_size(path: Path, *, required: bool) -> int | None:
    """Byte count of one tally file; 0 when absent; None when it cannot answer."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.warning("audit_failure_counter: cannot stat tally (%s): %s", path, exc)
        return None
    if not stat.S_ISREG(st.st_mode):
        # A directory's st_size is a real number and a meaningless count. Report
        # "cannot answer" rather than a fabricated figure — a wrong number here
        # would page someone about failures that never happened.
        logger.warning(
            "audit_failure_counter: tally path %s is not a regular file%s",
            path,
            "" if required else "; not counted",
        )
        return None
    return st.st_size


def read_audit_write_failure_reasons(hermes_home: str | None = None) -> dict[str, int] | None:
    """Lost rows by :func:`loss_key`, summed over the seat and profile homes.

    ``None`` exactly when :func:`read_audit_write_failures` would say the seat
    cannot answer (no ``.smd``); ``{}`` when nothing explained has been lost.
    Rows lost before the reasons file existed have a tally byte and no line, so
    the histogram's total can be below the tally; it is never above it except
    by a write the tally itself dropped. Lines that do not have the key shape
    are skipped, so a damaged file reports less, never something invented.
    """
    home = seat_home(hermes_home)
    if not (home / ".smd").is_dir():
        return None
    counts: Counter[str] = Counter()
    paths = [home / _REASONS_RELPATH, *sorted((home / "profiles").glob(f"*/{_REASONS_RELPATH}"))]
    for path in paths:
        counts.update(_read_reason_keys(path))
    return dict(counts.most_common(_MAX_REASON_KEYS))


def _read_reason_keys(path: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > _REASONS_READ_CAP_BYTES:
                fh.seek(size - _REASONS_READ_CAP_BYTES)
                fh.readline()  # drop the partial first line
            raw = fh.read()
    except FileNotFoundError:
        return counts
    except OSError as exc:
        logger.warning("audit_failure_counter: cannot read reasons (%s): %s", path, exc)
        return counts
    for line in raw.decode("ascii", errors="replace").splitlines():
        match = _KEY_RE.fullmatch(line.strip())
        if match and match.group(1) in REASONS:
            counts[match.group(0)] += 1
    return counts


__all__ = [
    "REASONS",
    "loss_key",
    "tally_path",
    "record_audit_write_failure",
    "read_audit_write_failures",
    "read_audit_write_failure_reasons",
    "seat_home",
]
