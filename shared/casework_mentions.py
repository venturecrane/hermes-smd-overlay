"""The ``mentioned`` rows an out-of-turn digest earns (case-manager Job 3).

On a case-manager seat the daily deadline digest carries a "Done since last
time" line: work the Operator finished without asking (a task closed on the
record's evidence, a date-prep step it ran itself) that nobody has been told
about. The escalator's pre_run renders the line and lists, per dispatch, the
casework items it names (``casework_mentions``). After a successful FULL send
:mod:`shared.prerendered_dispatch` hands them here, and each becomes one
``mentioned`` row through the broker's ``casework_event_append`` verb, so the
line is told exactly once.

The broker witnesses every ``mentioned`` row the way it witnesses a raise: the
row's session must hold a send this broker dispatched to a person, so the rows
are written with the SAME resolved session id the send row carries. A skeleton
delivery carries no done line and writes nothing here; a refused row is logged
and the line is simply told again next time (annoying, never dangerous).

The list is validated with the envelope: a malformed entry refuses the whole
envelope, the same posture as the digest's own appends, and every ``item_key``
must be the one the ledger derives from its matter, kind and id.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from shared import casework_ledger
from shared.casework_acts import broker_append

logger = logging.getLogger(__name__)

MAX_MENTIONS = 50
_KEYS = frozenset({"item_key", "matter_id", "kind", "source_id"})


def _valid_one(row: object) -> bool:
    if not isinstance(row, dict) or set(row) != _KEYS:
        return False
    if row.get("kind") not in casework_ledger.ITEM_KINDS:
        return False
    if not all(isinstance(row.get(k), str) and 0 < len(row[k]) <= 200 for k in _KEYS):
        return False
    try:
        derived = casework_ledger.item_key(
            matter_id=row["matter_id"], kind=row["kind"], source_id=row["source_id"]
        )
    except ValueError:
        return False
    return derived == row["item_key"]


def valid(value: object) -> bool:
    """``casework_mentions`` absent, or a bounded list of well-keyed items."""
    if value is None:
        return True
    return (
        isinstance(value, list)
        and len(value) <= MAX_MENTIONS
        and all(_valid_one(row) for row in value)
    )


def write(
    skill: str,
    mentions: list,
    session_id: str,
    append: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[int, int]:
    """One ``mentioned`` row per item. Returns (written, attempted); never raises."""
    writer = append or broker_append
    written = attempted = 0
    for row in (mentions or [])[:MAX_MENTIONS]:
        attempted += 1
        event = {
            "ts": None,
            "skill": skill,
            "matter_id": row["matter_id"],
            "kind": row["kind"],
            "source_id": row["source_id"],
            "item_key": row["item_key"],
            "event": "mentioned",
            "session_id": session_id,
        }
        try:
            response = writer(event)
        except Exception as exc:  # noqa: BLE001 — one lost row must not lose the rest
            logger.warning("casework_mentions: %s not written (%s)", row.get("item_key"), exc)
            continue
        if isinstance(response, dict) and response.get("ok"):
            written += 1
        else:
            logger.warning("casework_mentions: %s refused (%s)", row.get("item_key"), response)
    return written, attempted


__all__ = ["MAX_MENTIONS", "valid", "write"]
