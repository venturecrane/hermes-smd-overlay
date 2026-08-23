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

THE FOURTH OUTCOME (ss-console#2546 follow-up). A rule an administrator APPLIED
is news the person who asked is owed just as much as a decline, and it was the
one outcome that reached nobody: the seat's live path observed the install and
then failed to send, and there was no pass afterwards that could notice. So an
installed row now travels this loop too. It does NOT go through ``notify``: the
install note answers a different question (was anybody waiting on this rule) and
carries its own once-only claim, so it is handed to ``notify_install``, which
does both and returns whether the person was told.
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


#: The kind an operations request is stored under. Mirrors ``OPS_REQUEST_KIND``
#: in ss-console ``operator/workspace_broker/establishment.py``.
OPS_REQUEST_KIND = "ops_request"


def outcome_kind(row: dict[str, Any]) -> str:
    """Which outcome this row is, in the vocabulary the notes are written in.

    ``""`` for a row that has not ended, which is every row that must be left
    alone. The broker speaks of a committed rule as ``state="committed"`` plus an
    ``installed`` flag, and the two are deliberately separate over there: a rule
    can be committed and still converging, and only the flag says a run's result
    was read and the word seen. This collapses the pair into the one word the
    note is keyed on, and a committed row WITHOUT the flag collapses to nothing.

    AN OPERATIONS ROW SPEAKS ITS OWN THREE WORDS (ss-console#2546). ``done``
    rather than ``installed``, and the difference is not cosmetic: ``installed``
    routes to :func:`notify_install`, which asks "was anybody at the firm waiting
    on an administrator" and sends the rule letter. Nobody at the firm applied an
    operations change — SMD did — so an ops row that came back as ``installed``
    would tell the requester one of their colleagues had put a rule in force. All
    three ops outcomes go through ``notify``, and the caller dispatches on the
    row's stored kind from there.

    THE KIND IS READ FROM THE ROW, never from the caller, for the same reason the
    broker reads its TTL from the stored kind: nothing on the wire may decide
    which letter a person gets.
    """
    if not isinstance(row, dict):
        return ""
    state = str(row.get("state") or "")
    if str(row.get("kind") or "rule") == OPS_REQUEST_KIND:
        if state in ("lapsed", "declined"):
            return state
        # ``ops_resolve`` with ``done`` stamps consumed_at AND installed_at
        # together, so the broker's view says committed+installed; there is no
        # converge window on a change SMD made by hand, and the ``installed``
        # flag is therefore not a second condition here.
        return "done" if state == "committed" else ""
    if state in ("lapsed", "declined"):
        return state
    if state == "committed" and row.get("installed"):
        return "installed"
    return ""


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
    notify_install: Callable[[str, str], bool] | None = None,
) -> SweepResult:
    """One reporting pass. Pure with respect to I/O injection.

    ``fetch`` returns the seat's unreported outcomes (and, as a side effect of
    being a broker verb, marks whatever has just expired). ``notify`` sends one
    row's note and returns whether it went. ``mark`` tells the broker the person
    has been told. ``notify_install`` sends AND marks an install note, because
    that one is once-only against the broker rather than against this pass; a
    sweeper built without it simply leaves installed rows for the next caller.

    A row this cannot send is left UNMARKED and comes back next pass. There is
    no failure counter and no give-up, on purpose: the failure modes are a
    refused gate and an unreachable transport, and both are conditions that
    clear on their own. A row that never clears them is bounded anyway, because
    the broker deletes it once it is old enough.

    ``failed`` ALSO COUNTS A ROW ANOTHER OBSERVER IS SENDING RIGHT NOW
    (ss-console#2546). Since the duplicate on 2026-08-23 every outcome letter is
    claimed by proposal id before dispatch, and ``notify`` returns false to
    whichever observer did not get the claim. That is not a distinct outcome
    worth a fourth counter: if the holder's send goes, the row is marked and this
    pass never sees it again; if it does not, the claim is released and the next
    pass retries -- which is exactly what ``failed`` already means here.
    """
    reported = 0
    failed = 0
    skipped = 0
    for row in fetch()[:max_per_pass]:
        if not isinstance(row, dict):
            skipped += 1
            continue
        proposal_id = str(row.get("proposal_id") or "")
        kind = outcome_kind(row)
        if not proposal_id or not kind:
            skipped += 1
            continue
        if kind == "installed":
            if notify_install is None:
                skipped += 1
            elif notify_install("", proposal_id):
                reported += 1
            else:
                failed += 1
            continue
        if notify(kind=kind, row=row, by=str(row.get("declined_by") or "")):
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
    "OPS_REQUEST_KIND",
    "SweepResult",
    "outcome_kind",
    "run_sweep_once",
    "start_sweeper_thread",
]
