"""Hand-off of routine enable/disable events from bootstrap to the ledger (#2498).

WHY A SPOOL AND NOT A DIRECT WRITE. A routine is turned on or off by editing
``personas[].cron[]`` in customer.yaml; the change lands at boot, when
``bootstrap/cron_materialize.py`` reconciles the authored set into the Hermes
cron store. That runs inside ``hermes-smd bootstrap`` — a short-lived process
that is NOT the gateway. The broker's generic ``audit_append`` verb is gated on
``peer_pid == SMD_GATEWAY_PID`` (``operator/workspace_broker/server.py``), and
the broker holds the only RW handle on the ledger (OP-P1-4), so bootstrap
cannot write the row itself and must not be given a verb that lets it.

So bootstrap records WHAT changed, and the audit plugin — which registers
inside the gateway process moments later, where ``audit_append`` is reachable —
drains the spool and writes the rows. Same crossing, same directory, and the
same reason as ``shared.audit_status`` and ``shared.webhook_surface_check``:
``$HERMES_HOME/.smd/`` is on the Fly volume, written by one process, read by
another.

DRAIN, NOT READ. The file is renamed aside before it is parsed, so a row is
emitted at most once even if the plugin registers twice, and a spool that
cannot be parsed is discarded rather than replayed at every boot forever. If
the gateway dies between the rename and the write, those events are lost — an
accepted loss: a lost row is a gap, and a duplicated row is a lie about how
many times the firm changed its mind.

The spool holds no client content: a persona slug, a skill name, a boolean, and
a cron expression. Nothing here can carry a matter reference or a recipient.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = "smd.routine_change/1"

_SPOOL_RELPATH = Path(".smd") / "routine_changes.jsonl"
_DRAINING_SUFFIX = ".draining"

_DEFAULT_HERMES_HOME = "/opt/data"

#: A seat has tens of routines, not thousands. A spool longer than this is a
#: broken writer, not a busy firm; the excess is dropped rather than replayed.
_MAX_EVENTS = 500


def spool_path(hermes_home: str | None = None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    return Path(home) / _SPOOL_RELPATH


def append_routine_change(
    *,
    persona_slug: str,
    skill: str,
    enabled: bool,
    schedule: str | None,
    hermes_home: str | None = None,
) -> bool:
    """Record one routine change for the gateway to turn into an audit row.

    Best-effort; never raises. Returns True when the line was appended. The
    directory IS created here (unlike the failure tally): bootstrap runs before
    the audit plugin has had a chance to create ``.smd``, so a routine change on
    a first boot would otherwise have nowhere to land.
    """
    path = spool_path(hermes_home)
    line = json.dumps(
        {
            "schema": SCHEMA,
            "persona_slug": persona_slug,
            "skill": skill,
            "enabled": bool(enabled),
            "schedule": schedule,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except OSError as exc:
        logger.warning("routine_change_spool: append failed (%s): %s", path, exc)
        return False


def drain_routine_changes(hermes_home: str | None = None) -> list[dict[str, Any]]:
    """Take every spooled change and remove the spool. Never raises.

    Returns the parsed events in the order they were recorded. An empty list
    means nothing was spooled — the ordinary case on a boot that changed no
    routine, and the reason this is cheap to call at every registration.
    """
    path = spool_path(hermes_home)
    staged = path.with_name(path.name + _DRAINING_SUFFIX)
    try:
        # Rename first: whatever happens next, these events are not replayed.
        path.replace(staged)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("routine_change_spool: cannot stage spool (%s): %s", path, exc)
        return []
    try:
        raw = staged.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("routine_change_spool: cannot read staged spool: %s", exc)
        raw = ""
    finally:
        try:
            staged.unlink()
        except OSError:
            logger.warning("routine_change_spool: could not remove staged spool %s", staged)

    events: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if len(events) >= _MAX_EVENTS:
            logger.warning(
                "routine_change_spool: more than %d events spooled; dropping the rest",
                _MAX_EVENTS,
            )
            break
        try:
            event = json.loads(line)
        except ValueError:
            logger.warning("routine_change_spool: unparseable line %d discarded", lineno)
            continue
        if not isinstance(event, dict) or event.get("schema") != SCHEMA:
            logger.warning("routine_change_spool: line %d has no known schema; discarded", lineno)
            continue
        if not isinstance(event.get("skill"), str) or not event["skill"]:
            logger.warning("routine_change_spool: line %d names no skill; discarded", lineno)
            continue
        if not isinstance(event.get("enabled"), bool):
            logger.warning("routine_change_spool: line %d has no enable verdict; discarded", lineno)
            continue
        events.append(event)
    return events


__all__ = ["SCHEMA", "spool_path", "append_routine_change", "drain_routine_changes"]
