"""A rule nobody answered has to lapse, and somebody has to be told (ss#2546).

WHY A THREAD AND NOT A CRON. The lapse is the one outcome with nobody in front
of it: it happens because no administrator replied, and the person who asked is
not necessarily writing to the seat either. Every other trigger the platform has
depends on somebody being there. A scheduled routine would have been the obvious
answer and is not available: Ashton and Price's crons are all off (customer.yaml,
2026-08-12), so a routine authored for this would never fire on the one seat the
feature exists for.

So one daemon thread, the ``hermes-smd-reply`` sweeper shape, started
unconditionally at register and no-op on a seat with nothing outstanding.

WHAT ONE PASS DOES, and the ordering is the whole design:

1. asks the broker what has ended unreported. That single call ALSO performs the
   marking, because the broker sweeps expired rows on every establishment verb;
   asking is therefore not a read that races the marking, it is the thing that
   causes it. Nothing here computes an expiry, and nothing here holds a clock:
   the broker's clock is the only one, which is why a caller cannot expire
   somebody else's rule by asking at the wrong moment;
2. sends each row's author their note, through the seat's own gate;
3. marks the row reported ONLY after the send returns sent. That ordering trades
   a possible retry for a possible silence, deliberately: a person who is told
   twice is mildly annoyed, and a person who is never told is the defect this
   whole issue exists to fix.

WHAT IT NEVER DOES. It does not confirm, commit, decline, or release anything.
The broker's senderless listing returns TERMINAL rows only, so there is nothing
here that could be acted on even by a caller trying to.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Thirty seconds. Short enough that a lapse is reported while the person who
#: asked still remembers asking, long enough that an idle seat is not making a
#: broker round trip worth talking about. Same interval the held-reply sweeper
#: settled on, for the same reason.
DEFAULT_SWEEP_INTERVAL_S = 30.0

#: Rows reported in one pass. A backlog drains over several passes rather than
#: emitting a burst of mail on the first tick after a restart, which is what an
#: unbounded pass would do on a seat that had been down for a week.
MAX_PER_PASS = 20


@dataclass(frozen=True)
class SweepResult:
    """What one pass did. Returned for the log line and for the tests."""

    reported: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def touched(self) -> int:
        return self.reported + self.failed


def run_sweep_once(
    *,
    fetch: Callable[[], list[dict[str, Any]]],
    notify: Callable[..., bool],
    mark: Callable[[str], None],
    max_per_pass: int = MAX_PER_PASS,
) -> SweepResult:
    """One reporting pass. Pure with respect to I/O injection.

    ``fetch`` returns the seat's unreported outcomes (and, as a side effect of
    being a broker verb, marks whatever has just expired). ``notify`` sends one
    row's note and returns whether it went. ``mark`` tells the broker the person
    has been told.

    A row this cannot send is left UNMARKED and comes back next pass. There is
    no failure counter and no give-up, on purpose: the failure modes are a
    refused gate and an unreachable transport, and both are conditions that
    clear on their own. A row that never clears them is bounded anyway, because
    the broker deletes it once it is old enough.
    """
    reported = 0
    failed = 0
    skipped = 0
    for row in fetch()[:max_per_pass]:
        if not isinstance(row, dict):
            skipped += 1
            continue
        proposal_id = str(row.get("proposal_id") or "")
        state = str(row.get("state") or "")
        if not proposal_id or state not in ("lapsed", "declined"):
            skipped += 1
            continue
        if notify(kind=state, row=row, by=str(row.get("declined_by") or "")):
            mark(proposal_id)
            reported += 1
        else:
            failed += 1
    return SweepResult(reported=reported, failed=failed, skipped=skipped)


def start_sweeper_thread(
    *,
    sweep: Callable[[], SweepResult],
    interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """Start the daemon loop. Never raises, and never dies of one bad pass.

    Started unconditionally at register: whether anything is outstanding is a
    per-pass question, not a boot-time one, and a seat that authored nothing
    simply gets an empty list every thirty seconds.
    """
    stop = stop_event or threading.Event()

    def _loop() -> None:
        while not stop.wait(interval_s):
            try:
                result = sweep()
            except Exception as exc:  # noqa: BLE001 -- the loop must survive anything
                logger.warning("hermes-smd-establishment: lapse sweep failed (%s)", exc)
                continue
            if result.touched:
                logger.info(
                    "hermes-smd-establishment: lapse sweep reported=%d failed=%d skipped=%d",
                    result.reported,
                    result.failed,
                    result.skipped,
                )

    thread = threading.Thread(target=_loop, name="smd-rule-lapse-sweeper", daemon=True)
    thread.start()
    return thread


__all__ = [
    "DEFAULT_SWEEP_INTERVAL_S",
    "MAX_PER_PASS",
    "SweepResult",
    "run_sweep_once",
    "start_sweeper_thread",
]
