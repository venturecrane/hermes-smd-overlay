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
"""

from __future__ import annotations

import logging
import os
import stat
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

_DEFAULT_HERMES_HOME = "/opt/data"


def tally_path(hermes_home: str | None = None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    return Path(home) / _TALLY_RELPATH


def record_audit_write_failure(context: str, *, hermes_home: str | None = None) -> bool:
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
    a count cannot leak a matter reference or a recipient.
    """
    path = tally_path(hermes_home)
    if not path.parent.is_dir():
        logger.debug("audit_failure_counter: no tally dir at %s; not counted", path.parent)
        return False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
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
    _WARNER.warn(logger, f"audit row lost ({context}); tallied in {path}")
    return True


def read_audit_write_failures(hermes_home: str | None = None) -> int | None:
    """Total lost rows, or ``None`` when the seat cannot answer.

    Three states, and the difference between the last two is the point:

    * ``None``  — ``.smd`` does not exist. The audit plugin has never
      registered on this Machine, so the seat has no opinion; the console holds
      whatever it last knew rather than reporting a reassuring zero.
    * ``0``     — ``.smd`` exists and no tally file does. A REAL zero: the
      writer has been up and has lost nothing. This is the value that lets a
      recovered seat stop alerting.
    * ``n > 0`` — that many rows have been lost since the volume was created.
    """
    path = tally_path(hermes_home)
    if not path.parent.is_dir():
        return None
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
        logger.warning("audit_failure_counter: tally path %s is not a regular file", path)
        return None
    return st.st_size


__all__ = ["tally_path", "record_audit_write_failure", "read_audit_write_failures"]
