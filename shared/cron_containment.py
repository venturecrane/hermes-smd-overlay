"""Cron containment sentinel (ss-console#2276).

During the ss-console#2258 incident, containment flipped 13 cron jobs to
``enabled=False`` in the runtime cron store — and that flip does not survive
a boot, because :mod:`bootstrap.cron_materialize` deliberately removes all
managed jobs and recreates exactly the authored set from ``customer.yaml``
on every bootstrap ("the store always matches customer.yaml", ADR 0047).
A restart silently re-armed the very jobs containment had switched off.

This module defines the durable lever: a sentinel file on the persistent
volume (``$HERMES_HOME/CRON_CONTAINMENT``, default ``/opt/data`` — the same
volume that keeps profile homes and cron stores across reprovisions BY
DESIGN). While the sentinel exists:

  * bootstrap materializes ZERO managed cron jobs and removes any that
    exist (``bootstrap/translate.py`` passes ``containment=True`` through to
    ``materialize_cron``), and logs the state at WARNING; and
  * every heartbeat carries ``cron_containment: 1``
    (:mod:`shared.heartbeat`), so the console sees "crons deliberately
    disabled" — a contained seat must never be indistinguishable from a
    quietly broken one (Law 12: silence is never success). The field is
    tri-state on the wire: ``1`` contained, ``0`` genuinely not contained,
    ABSENT when the volume could not be read at all (ss-console#2291) —
    absent is never a verdict, and the console holds it as unknown.

Placing and removing the sentinel are deliberate operator acts::

    fly ssh console -a <app> -C 'sh -c "echo <reason> > /opt/data/CRON_CONTAINMENT"'
    fly ssh console -a <app> -C 'rm /opt/data/CRON_CONTAINMENT'

The file's content (optional) is a one-line human reason, surfaced in the
bootstrap log. Removal takes effect at the next bootstrap, which
re-materializes the authored set as usual.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SENTINEL_BASENAME = "CRON_CONTAINMENT"
_DEFAULT_HOME = "/opt/data"


def sentinel_path(home: str | None = None) -> Path:
    """Where the sentinel lives: ``<home>/CRON_CONTAINMENT``.

    ``home`` defaults to ``$HERMES_HOME`` and then ``/opt/data``, matching
    the volume-root convention used across the overlay (``shared.audit_status``,
    ``shared.customer_config``)."""
    root = home or os.environ.get("HERMES_HOME") or _DEFAULT_HOME
    return Path(root) / SENTINEL_BASENAME


def containment_state(home: str | None = None) -> bool | None:
    """Tri-state sentinel read (ss-console#2291).

    ``True`` contained, ``False`` genuinely not contained, ``None`` when the
    state cannot be determined at all — the volume is unreadable, or the home
    that would hold the sentinel is not there to read. Callers that report
    containment outward (the heartbeat) must distinguish the third case:
    collapsing it into ``False`` publishes a contained seat as a normal one,
    which is the exact blindness ss-console#2276 exists to remove.

    Known limit: a mount point that exists but is empty (volume failed to
    attach) is indistinguishable from an uncontained seat and reads ``False``.
    Nothing readable from this process separates those two.
    """
    path = sentinel_path(home)
    try:
        if path.is_file():
            return True
        # No sentinel. That is "not contained" only if the directory that
        # would hold one is actually present and readable.
        return False if path.parent.is_dir() else None
    except OSError as exc:
        logger.warning(
            "cron_containment: cannot read sentinel %s (%s); containment state UNKNOWN",
            path,
            exc,
        )
        return None


def containment_active(home: str | None = None) -> bool:
    """True when the sentinel file exists. Never raises, and treats an
    unknown state as not-contained: bootstrap deliberately fails open here,
    because its own volume checks fail loudly long before this is consulted.

    Callers that publish containment state — the heartbeat — must use
    :func:`containment_state` instead, so "could not tell" stays distinct from
    "not contained" (ss-console#2291)."""
    return containment_state(home) is True


def containment_reason(home: str | None = None) -> str:
    """First line of the sentinel file, for the bootstrap log. Empty string
    when the file is empty or unreadable."""
    try:
        text = sentinel_path(home).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text.strip().splitlines()[0].strip() if text.strip() else ""
