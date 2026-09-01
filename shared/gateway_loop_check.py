"""Gateway loop-liveness check for the heartbeat (ss-console#2488 part 2).

The defect. On 2026-08-20 the paying client's gateway event loop wedged for 33
minutes. Every liveness signal a human could see stayed green the whole time:
Fly's ``/health`` is a literal constant, and both the control-plane heartbeat and
the healthchecks.io ping are emitted by THIS process -- the webhook gate -- which
runs beside the gateway and does not share its loop. The gate's immunity to a
wedge is the reason nothing noticed, and it is also the fix: it is the one
process guaranteed to still be able to report one.

What the seat already has. Hermes rewrites
``$HERMES_HOME/profiles/<profile>/state/gateway.heartbeat`` every 30s from an
asyncio task ON the gateway loop (``gateway.shutdown_watchdog.loop_heartbeat_forever``),
so the file's mtime goes stale the instant the loop freezes. ss-console#2488
part 1 added a root-side supervisor in the Machine entrypoint that watches that
mtime and restarts the seat, and publishes its own state to
``/run/smd-gateway-liveness/state`` (one word) plus a kill ledger at
``/opt/data/gateway-liveness/kills`` (one line per restart). Both root-written,
world-readable, so this gate-uid process can read them.

What this module ships, four fields, all tri-state by construction:

``gateway_loop_ok``            1 the check could look / 0 it could not (OUR
                               blindness, paged separately, never a verdict on
                               the loop) / absent = nothing to say.
``gateway_loop_age_seconds``   seconds since the loop last beat. The wedge
                               signal. Absent while the arming latch below is
                               closed, and on a pin with no heartbeat at all.
``gateway_supervisor_state``   the supervisor's word: armed / not-armed /
                               starting / inert / not-watching / never-healthy /
                               refusing. Absent on a pin without the supervisor,
                               and also absent for any word this module does not
                               recognise -- see ``SUPERVISOR_STATES``.
``gateway_restarts_last_hour`` kill-ledger lines inside the last 3600s. Absent
                               without a ledger. The one field a restart cannot
                               race: the line is on the volume before the
                               container dies, so the first beat after reboot
                               carries it.

Why two signals and not one. A restart that part 1 performs is ALSO the thing
that refreshes the heartbeat -- the very success of part 1 can overwrite the
stale age before the console's 2-minute cron samples it, and a wedge that was
fixed goes unreported. The age catches the wedge the supervisor cannot fix; the
ledger catches the one it did.

The arming latch. The volume persists, so a stale heartbeat from the PREVIOUS
boot is on disk at every cold start, and this gate is forked before the gateway
exec. Reporting age before the file has been seen fresh ONCE in this process's
lifetime would page on every deploy. Part 1's supervisor has the same latch for
the same reason (there it would KILL every boot). Belt-and-braces, the age is
also suppressed inside ``scheduler_check.BOOT_SUPPRESS_SECONDS`` of process
uptime, the overlay's existing post-boot quiet window.

Doctrine carried from ``scheduler_check``: never ``path.exists()`` under a 0700
directory -- ``exists()`` swallows ``PermissionError`` and reads as "legitimately
absent, green". ``os.stat`` and classify: ``FileNotFoundError`` (file OR parent)
is a hold; any other ``OSError`` is ``ok=False``. Everything is read-only and
fail-soft; the emitter wraps the call and debounces a crash before reporting it.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_HOME = "/opt/data"
_DEFAULT_RUN_DIR = "/run/smd-gateway-liveness"
_DEFAULT_LEDGER_DIR = "/opt/data/gateway-liveness"
LEDGER_WINDOW_SECONDS = 3600
# Mirrors scheduler_check.BOOT_SUPPRESS_SECONDS; imported lazily below so a
# refactor there cannot break this module's import.
_FALLBACK_BOOT_SUPPRESS_SECONDS = 900

# A CLOSED vocabulary, and the closure is load-bearing: `_read_supervisor_state`
# forwards nothing it does not recognise, so a word the entrypoint writes and
# this set does not carry is dropped to None -- a NULL, which the console holds
# rather than pages on. That is the right default for an unknown writer and the
# wrong outcome for a new state we meant to ship, so this set and the entrypoint's
# `gateway_liveness_state` calls move together, in the same change.
#
# `starting` and `never-healthy` were added 2026-09-01 after the pilot-smokeball
# crash loop (ss-console docs/runbooks/operator/incidents/
# 2026-09-01-gateway-startup-watchdog-collision.md), which the supervisor could
# describe only as `inert` (a page, emitted on every healthy boot's first minutes)
# or `not-armed` (no page at all, including for a gateway that wedged during
# startup and would never come up).
#
#   starting       argv does not name hermes yet -- entrypoint has exec'd
#                  bootstrap.sh and bootstrap has not yet exec'd the gateway.
#                  Normal, bounded by the seat's startup grace, NOT a page.
#   never-healthy  no fresh loop beat in the whole startup grace. The gateway is
#                  wedged during startup. This one IS a page: the supervisor
#                  deliberately does not kill a slow-starting gateway, so a human
#                  is the only recovery path.
SUPERVISOR_STATES = frozenset(
    {
        "armed",
        "not-armed",
        "starting",
        "inert",
        "not-watching",
        "never-healthy",
        "refusing",
    }
)


@dataclass(frozen=True)
class GatewayLoopCheck:
    ok: bool
    age_seconds: int | None
    supervisor_state: str | None
    restarts_last_hour: int | None
    reason: str = ""


def heartbeat_path(profile: str, home: str | None = None) -> Path:
    root = home or os.environ.get("HERMES_HOME") or _DEFAULT_HOME
    return Path(root) / "profiles" / profile / "state" / "gateway.heartbeat"


def _boot_suppress_seconds() -> int:
    try:
        from shared.scheduler_check import BOOT_SUPPRESS_SECONDS

        return int(BOOT_SUPPRESS_SECONDS)
    except Exception:  # noqa: BLE001 - a refactor there must not break this read
        return _FALLBACK_BOOT_SUPPRESS_SECONDS


class GatewayLoopChecker:
    """Holds the arming latch across ticks. One instance per gate process.

    ``profile`` defaults to ``$HERMES_ACTIVE_PROFILE``, which bootstrap exports
    before this process is forked. Unset is reported as ``ok=False``: a check
    that does not know which file to read cannot say the loop is fine.
    """

    def __init__(
        self,
        *,
        profile: str | None = None,
        home: str | None = None,
        run_dir: str | None = None,
        ledger_dir: str | None = None,
    ) -> None:
        self._profile = profile
        self._home = home
        self._run_dir = Path(run_dir or _DEFAULT_RUN_DIR)
        self._ledger_dir = Path(ledger_dir or _DEFAULT_LEDGER_DIR)
        self._armed = False

    @property
    def armed(self) -> bool:
        return self._armed

    def check(
        self,
        *,
        now: float | None = None,
        uptime_seconds: int | None = None,
    ) -> GatewayLoopCheck:
        now_s = time.time() if now is None else now
        state = self._read_supervisor_state()
        restarts = self._count_restarts(now_s)

        profile = self._profile or os.environ.get("HERMES_ACTIVE_PROFILE") or ""
        if not profile:
            return GatewayLoopCheck(
                ok=False,
                age_seconds=None,
                supervisor_state=state,
                restarts_last_hour=restarts,
                reason="HERMES_ACTIVE_PROFILE unset; cannot resolve the gateway heartbeat",
            )

        path = heartbeat_path(profile, self._home)
        try:
            mtime = os.stat(path).st_mtime
        except FileNotFoundError:
            # A pin without the heartbeat (Hermes < 0.19) or the pre-first-beat
            # window. Nothing to say about the loop; the supervisor fields still
            # travel, because they are independent facts.
            return GatewayLoopCheck(
                ok=True,
                age_seconds=None,
                supervisor_state=state,
                restarts_last_hour=restarts,
                reason="no heartbeat file",
            )
        except OSError as exc:
            logger.warning("gateway_loop_check: cannot stat %s (%s)", path, exc)
            return GatewayLoopCheck(
                ok=False,
                age_seconds=None,
                supervisor_state=state,
                restarts_last_hour=restarts,
                reason=f"cannot read heartbeat: {exc.__class__.__name__}",
            )

        # int() because the console's parseNonNegInt rejects floats and would
        # NULL the column forever; max(0, ...) because clock skew after a Fly
        # resume can put mtime in the future and a negative would hold silently.
        age = max(0, int(now_s - mtime))

        boot_suppress = _boot_suppress_seconds()
        if not self._armed:
            # Arm only on a beat seen FRESH: inside one boot-suppress window and
            # well under anything a supervisor would act on. A 2-hour-old file
            # from the previous boot must not arm this.
            if age <= 120:
                self._armed = True
                logger.info("gateway_loop_check: ARMED (heartbeat %ss fresh)", age)
        if not self._armed:
            return GatewayLoopCheck(
                ok=True,
                age_seconds=None,
                supervisor_state=state,
                restarts_last_hour=restarts,
                reason=f"not armed: heartbeat {age}s stale has not been seen fresh this process",
            )
        if uptime_seconds is not None and uptime_seconds < boot_suppress:
            return GatewayLoopCheck(
                ok=True,
                age_seconds=None,
                supervisor_state=state,
                restarts_last_hour=restarts,
                reason=f"boot suppression ({uptime_seconds}s < {boot_suppress}s)",
            )
        return GatewayLoopCheck(
            ok=True,
            age_seconds=age,
            supervisor_state=state,
            restarts_last_hour=restarts,
        )

    # -- supervisor artefacts (part 1) -------------------------------------

    def _read_supervisor_state(self) -> str | None:
        try:
            text = (self._run_dir / "state").read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("gateway_loop_check: cannot read supervisor state (%s)", exc)
            return None
        word = text.strip().split("\n", 1)[0].strip()
        # A closed vocabulary. Anything else is a writer we do not understand and
        # must not be forwarded as a state the console will act on.
        return word if word in SUPERVISOR_STATES else None

    def _count_restarts(self, now_s: float) -> int | None:
        try:
            text = (self._ledger_dir / "kills").read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            # No ledger means no supervisor on this pin, OR a supervisor that
            # has never had to kill. The former must hold, and the two cannot be
            # told apart from here -- the state file above separates them.
            try:
                if not self._ledger_dir.is_dir():
                    return None
            except OSError:
                return None
            return 0
        except OSError as exc:
            logger.warning("gateway_loop_check: cannot read kill ledger (%s)", exc)
            return None
        cutoff = now_s - LEDGER_WINDOW_SECONDS
        count = 0
        for line in text.splitlines():
            head = line.split(" ", 1)[0]
            if not head.isdigit():
                continue
            if int(head) >= cutoff:
                count += 1
        return count


__all__ = [
    "GatewayLoopCheck",
    "GatewayLoopChecker",
    "LEDGER_WINDOW_SECONDS",
    "SUPERVISOR_STATES",
    "heartbeat_path",
]
