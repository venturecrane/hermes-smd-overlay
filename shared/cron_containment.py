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
    quietly broken one (Law 12: silence is never success).

Placing and removing the sentinel are deliberate operator acts::

    fly ssh console -a <app> -C 'sh -c "echo <reason> > /opt/data/CRON_CONTAINMENT"'
    fly ssh console -a <app> -C 'rm /opt/data/CRON_CONTAINMENT'

The file's content (optional) is a one-line human reason, surfaced in the
bootstrap log. Removal takes effect at the next bootstrap, which
re-materializes the authored set as usual.
"""

from __future__ import annotations

import os
from pathlib import Path

SENTINEL_BASENAME = "CRON_CONTAINMENT"
_DEFAULT_HOME = "/opt/data"


def sentinel_path(home: str | None = None) -> Path:
    """Where the sentinel lives: ``<home>/CRON_CONTAINMENT``.

    ``home`` defaults to ``$HERMES_HOME`` and then ``/opt/data``, matching
    the volume-root convention used across the overlay (``shared.audit_status``,
    ``shared.customer_config``)."""
    root = home or os.environ.get("HERMES_HOME") or _DEFAULT_HOME
    return Path(root) / SENTINEL_BASENAME


def containment_active(home: str | None = None) -> bool:
    """True when the sentinel file exists. Never raises: an unreadable
    volume reads as not-contained here, and bootstrap's own volume checks
    fail loudly long before this is consulted."""
    try:
        return sentinel_path(home).is_file()
    except OSError:
        return False


def containment_reason(home: str | None = None) -> str:
    """First line of the sentinel file, for the bootstrap log. Empty string
    when the file is empty or unreadable."""
    try:
        text = sentinel_path(home).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text.strip().splitlines()[0].strip() if text.strip() else ""
