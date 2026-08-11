"""Webhook expected-tool self-check — is the warn-tier surface intact this boot?

ss-console#2222. The sibling of :mod:`shared.spec_control_check` and
:mod:`shared.connector_check`, deliberately shaped alike: an operator should not
have to learn each health check's moods.

WHAT IT ANSWERS. ``shared.webhook_read_surface.WEBHOOK_EXPECTED_TOOLS`` names
tools a webhook turn must be offered but whose absence degrades one class of
answer rather than the seat — today, ``operator_seat_facts``. Their absence is
NOT boot-fatal (see that module's docstring for the harm judgment), so something
that runs whether or not the seat is busy has to report it. That is a heartbeat.

WHY IT READS A SENTINEL INSTEAD OF RESOLVING THE SURFACE ITSELF. The resolved
tool surface only exists in the AGENT (gateway) process; this check runs inside
the GATE's heartbeat emitter, which cannot see the agent's registry. So the
activation gate resolves it once at ``gateway:startup`` and writes the outcome
to ``$HERMES_HOME/.smd/webhook_surface.json``; this reads it back. Identical
crossing to ``shared.audit_status``, including its staleness key: a handler
cannot sentinel its own non-execution, so a file whose writing pid is gone is a
PREVIOUS boot's answer and is held, never served as current.

THREE OUTCOMES, and the differences are the point:

* ``None`` — no usable sentinel (absent, unparseable, or written by a dead pid),
  or this seat does not serve the webhook platform at all. Nothing to conclude;
  the console holds whatever it last knew rather than resolving an open alert on
  an absence.
* ``ok=False, tools=None`` — the check itself could not run. OUR blindness, not
  a missing tool, and it pages on its own.
* ``ok=True, tools={...}`` — the check ran. An entry with ``offered: False`` is
  the alertable gap, and each entry carries both sides so a recovery can say
  which way it recovered.

No consecutive-failure debounce, unlike the scheduler / connector / spec checks.
Those probe live subsystems that can be transiently down; this reads one small
local file written once per boot, which has no transient-failure mode a debounce
would smooth. Adding one would only delay a real signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from shared import webhook_read_surface

logger = logging.getLogger("hermes_smd.webhook_surface_check")


@dataclass(frozen=True)
class WebhookSurfaceCheck:
    """Outcome of one warn-tier surface read.

    ``ok`` is the health of the CHECK, not of any tool. ``tools`` maps tool name
    → ``{"expected": True, "offered": bool}``; ``None`` when the check is broken.
    """

    ok: bool
    tools: dict[str, dict] | None


def _pid_alive(pid: int) -> bool:
    """Liveness via /proc — no signals sent, works for non-child processes."""
    return Path(f"/proc/{pid}").is_dir()


def check(hermes_home: str | None = None, *, pid_alive=_pid_alive) -> WebhookSurfaceCheck | None:
    """Read this boot's warn-tier outcome. Never raises."""
    try:
        status = webhook_read_surface.read_webhook_surface_status(hermes_home)
        if status is None:
            return None

        pid = status.get("pid")
        if not isinstance(pid, int) or pid <= 0 or not pid_alive(pid):
            # A previous boot's answer. Holding is correct: reporting it would
            # let a stale green resolve an alert about the current process.
            logger.debug("webhook_surface_check: sentinel pid %r not live; holding", pid)
            return None

        if status.get("ok") is not True:
            return WebhookSurfaceCheck(ok=False, tools=None)

        tools = status.get("tools")
        if not isinstance(tools, dict):
            return WebhookSurfaceCheck(ok=False, tools=None)
        return WebhookSurfaceCheck(ok=True, tools=tools)
    except Exception:  # noqa: BLE001 — a broken check pages, never goes dark
        logger.exception("webhook_surface_check: evaluation failed")
        return WebhookSurfaceCheck(ok=False, tools=None)


__all__ = ["WebhookSurfaceCheck", "check"]
