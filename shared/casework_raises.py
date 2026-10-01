"""The ``proposed`` rows an out-of-turn deadline digest earns, one per task line
a person may answer "done with 1" to.

A digest reply used to be an acknowledgement only: it quieted an item for the
firm's snooze window, and only completion in Smokeball closed it. The person
the Operator works for could not tell it to close a task it had just reminded
them about. Now the escalator's pre_run lists, per dispatch, the numbered
needs-you lines that are tasks with a resolvable owner (``casework_raises``),
and after a successful FULL send :mod:`shared.prerendered_dispatch` hands them
here. Each becomes one casework ``proposed`` row carrying payload action
``complete`` (a person's word, never the record's evidence), the line number
``n`` and the send's ``dispatch_ref``; the broker joins them to its own send
row and stamps the thread. A plain-word reply then finds the raise by thread
and number, and an ``approved`` row under the replier's authored name
authorizes the one Smokeball write (``plugins/hermes-smd-escalation/
digest_reply.py``).

The broker witnesses every raise the way it witnesses a review's proposal:
the row's session must hold a send this broker dispatched to a person, so the
rows are written with the SAME resolved session id the send row carries. A
skeleton delivery carries no numbered line and writes nothing here. A refused
row is logged, and that line is then answered by ack only: never a close
nobody authorized.

The list is validated with the envelope: a malformed entry refuses the whole
envelope, the same posture as the digest's own appends, and every ``item_key``
must be the one the ledger derives from its matter, kind and id.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from shared import casework_ledger, digest_reply_ref
from shared.casework_acts import broker_append

logger = logging.getLogger(__name__)

MAX_RAISES = 60
_KEYS = frozenset({"item_key", "matter_id", "kind", "source_id", "n", "payload"})
_PAYLOAD_KEYS = frozenset({"action", "class", "staff_id", "reason", "evidence"})
_MAX_ID = 200
_MAX_REASON = 300


def _valid_payload(payload: object) -> bool:
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        return False
    if payload.get("action") != "complete" or payload.get("class") not in casework_ledger.CLASSES:
        return False
    staff_id = payload.get("staff_id")
    if not (isinstance(staff_id, str) and 0 < len(staff_id) <= _MAX_ID):
        return False
    reason = payload.get("reason")
    if not (isinstance(reason, str) and 0 < len(reason) <= _MAX_REASON):
        return False
    return payload.get("evidence") == []


def _valid_one(row: object) -> bool:
    if not isinstance(row, dict) or set(row) != _KEYS:
        return False
    if row.get("kind") != "task":
        return False
    for key in ("item_key", "matter_id", "source_id"):
        if not (isinstance(row.get(key), str) and 0 < len(row[key]) <= _MAX_ID):
            return False
    if not digest_reply_ref.valid_digest_number(row.get("n")):
        return False
    if not _valid_payload(row.get("payload")):
        return False
    try:
        derived = casework_ledger.item_key(
            matter_id=row["matter_id"], kind=row["kind"], source_id=row["source_id"]
        )
    except ValueError:
        return False
    return derived == row["item_key"]


def valid(value: object) -> bool:
    """``casework_raises`` absent, or a bounded list of well-keyed task lines."""
    if value is None:
        return True
    return (
        isinstance(value, list)
        and len(value) <= MAX_RAISES
        and all(_valid_one(row) for row in value)
    )


def write(
    skill: str,
    raises: list,
    session_id: str,
    dispatch_ref: str,
    append: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[int, int]:
    """One ``proposed`` row per task line. Returns (written, attempted); never raises."""
    writer = append or broker_append
    written = attempted = 0
    for row in (raises or [])[:MAX_RAISES]:
        attempted += 1
        event = {
            "ts": None,
            "skill": skill,
            "matter_id": row["matter_id"],
            "kind": row["kind"],
            "source_id": row["source_id"],
            "item_key": row["item_key"],
            "event": "proposed",
            "session_id": session_id,
            "payload": {
                **row["payload"],
                "to_staff_id": None,
            },
        }
        digest_reply_ref.stamp_casework_raise(event, row.get("n"), dispatch_ref)
        try:
            response = writer(event)
        except Exception as exc:  # noqa: BLE001 — one lost row must not lose the rest
            logger.warning("casework_raises: %s not written (%s)", row.get("item_key"), exc)
            continue
        if isinstance(response, dict) and response.get("ok"):
            written += 1
        else:
            logger.warning("casework_raises: %s refused (%s)", row.get("item_key"), response)
    return written, attempted


__all__ = ["MAX_RAISES", "valid", "write"]
