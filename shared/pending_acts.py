"""Pending-act approval register: the commitment half of read-back-and-confirm.

WHAT THIS IS FOR (ss-console operator-own-matter, 2026-08-21). A COMMITMENT
tool call never fires on the Operator's own initiative. Until now the only
current-turn approval the seat could obtain was for a SEND, over Telegram
(:mod:`shared.pending_send`), so a commitment on an email seat was simply
unreachable and the Operator's reply told the firm to go do the thing by hand.
The firm's administrators direct their employee, and an administrator's written
instruction is authority they already hold. This register is the in-process half
of turning that instruction into an approval:

* the trust gate withholds the commitment, asks the broker to mint a proposal,
  and records the read-back sentence here (:meth:`note_proposed`);
* the administrator answers "yes, create it" by email, and the establishment
  plugin, which is the one seam that sees the message and the verified sender
  together, records the confirmation here (:meth:`mark_confirmed`);
* the trust gate replays the STORED payload over the live arguments on the next
  call of that tool, so what executes is the act the administrator read, not
  whatever the model composed the second time.

THE INVARIANTS, and why each exists:

- **Session-keyed, unlike PENDING_SEND.** The send register is a process
  singleton because a Telegram approval is out of band and there is only ever
  one outstanding send. An act is confirmed by a reply INSIDE a conversation, so
  the approval belongs to that conversation and must not leak into another one
  running on the same Machine.
- **Single outstanding act per session.** A bare "yes" is worth nothing if two
  acts are waiting on it, and :meth:`note_proposed` refuses rather than
  superseding: an act the firm has already been asked about is not something a
  later proposal may quietly replace.
- **Stored-payload replay.** :meth:`peek_confirmed` hands back a deep copy of
  the payload captured at proposal time. Re-composition drift, a changed matter
  number, an added field: none of it can change what executes.
- **Consumed on allow, not on confirm.** :meth:`take_in_flight` marks the record
  spent as an approval while keeping it readable by :meth:`finish`, which the
  post-call hook needs in order to commit the broker row against the outcome. A
  second call of the same tool in the same turn therefore finds no approval.
- **Short in-process TTL.** Ten minutes. The broker row is the durable memory
  (24 hours); this register only has to survive the gap between the proposal and
  the answer inside one live conversation, and a shorter life means a stale
  approval cannot attach to a later turn.
"""

from __future__ import annotations

import copy
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

#: Ten minutes. See the module docstring: the broker row is the durable memory.
_DEFAULT_TTL_SECONDS: float = 600.0

#: Bound on tracked sessions, FIFO by recency. Same posture as the establishment
#: plugin's read-back bound: an unbounded per-session dict in a long-lived
#: gateway process is a leak.
_MAX_SESSIONS = 64

#: Mirrors ``plugins/hermes-smd-establishment._FAILED_STATUSES`` and
#: ``plugins/hermes-smd-reply/relay.py``. Copied rather than imported because
#: hyphenated plugin directories are not importable module paths; the three must
#: move together.
_FAILED_STATUSES: frozenset[str] = frozenset(
    {"error", "errored", "failed", "failure", "refused", "blocked", "denied"}
)


def tool_call_failed(status: Any, error_type: Any) -> bool:
    """True when a tool call POSITIVELY reported that it failed.

    Positive detection, deliberately: an allow-list of success words would
    silently start committing failed acts the day Hermes renames its status
    vocabulary, and a commitment committed on a failure is a broker row claiming
    the firm's system of record holds something it does not.
    """
    if isinstance(status, str) and status.strip().lower() in _FAILED_STATUSES:
        return True
    return isinstance(error_type, str) and error_type.strip().lower() not in ("", "none", "null")


@dataclass(frozen=True)
class ConfirmedAct:
    """One commitment an administrator confirmed, with who confirmed it and where.

    ``payload`` is the AUTHORED block the broker minted the proposal from, never
    the model's arguments. ``confirmed_message_id`` is the inbound message the
    answer arrived on, which is what makes the audit row joinable back to
    something a person actually wrote.
    """

    proposal_id: str
    tool: str
    payload: dict[str, Any]
    instructed_by: str
    confirmed_by: str
    confirmed_message_id: str
    confirmed_at: float


@dataclass
class PendingAct:
    """The single act outstanding for one session, at whatever stage it has reached."""

    proposal_id: str
    tool: str
    readback: str
    created_at: float
    delivered: bool = False
    confirmed: ConfirmedAct | None = None
    in_flight: bool = False


@dataclass
class PendingActRegister:
    """Session-keyed single-outstanding-act register."""

    ttl_seconds: float = _DEFAULT_TTL_SECONDS
    max_sessions: int = _MAX_SESSIONS
    _records: OrderedDict[str, PendingAct] = field(default_factory=OrderedDict)

    # -- time ---------------------------------------------------------------
    def _now(self) -> float:
        return time.time()

    def _live(self, session_id: Any) -> PendingAct | None:
        """The session's record if it exists and has not expired, else None.

        Expiry is evaluated on read rather than swept on a timer: this process
        has no scheduler, and a record nobody reads costs nothing.
        """
        if not isinstance(session_id, str) or not session_id:
            return None
        rec = self._records.get(session_id)
        if rec is None:
            return None
        if (self._now() - rec.created_at) > self.ttl_seconds:
            self._records.pop(session_id, None)
            return None
        self._records.move_to_end(session_id)
        return rec

    # -- propose (trust gate, on await_approval) ----------------------------
    def has_open(self, session_id: Any) -> bool:
        """True when this session already has an act at any stage.

        The cross-register invariant reads this before proposing: one thing at a
        time is what makes "yes" unambiguous.
        """
        return self._live(session_id) is not None

    def note_proposed(self, session_id: Any, proposal_id: str, tool: str, readback: str) -> bool:
        """Record a proposed act. Refuses (returns False) while one is open.

        Never supersedes. An act the administrator has already been asked about
        stays the one they are answering; replacing it would mean their "yes"
        lands on a sentence they were never shown.
        """
        if not isinstance(session_id, str) or not session_id:
            return False
        if not proposal_id or not tool or not readback:
            return False
        if self.has_open(session_id):
            return False
        self._records[session_id] = PendingAct(
            proposal_id=str(proposal_id),
            tool=str(tool),
            readback=str(readback),
            created_at=self._now(),
        )
        self._records.move_to_end(session_id)
        while len(self._records) > self.max_sessions:
            self._records.popitem(last=False)
        return True

    def proposed(self, session_id: Any) -> list[str]:
        """Read-back sentences this session still owes the person, verbatim.

        The establishment plugin's send gate unions this with its own rule
        read-backs: a reply that follows a proposal has to carry the sentence the
        broker rendered, or the person is confirming something they never saw.
        """
        rec = self._live(session_id)
        if rec is None or rec.delivered or rec.confirmed is not None:
            return []
        return [rec.readback]

    def mark_delivered(self, session_id: Any, readback: str) -> bool:
        """Note that the read-back shipped, so it is no longer owed."""
        rec = self._live(session_id)
        if rec is None or rec.readback != readback:
            return False
        rec.delivered = True
        return True

    # -- confirm (establishment pre_llm_call, the admin's own words) --------
    def mark_confirmed(self, session_id: Any, act: ConfirmedAct) -> bool:
        """Attach the administrator's confirmation to an act, and refuse to invent one.

        Two cases, and the difference is which turn is speaking:

        * this session proposed the act and is now hearing the answer. The id and
          the tool must match what it proposed, because the seat decides what was
          confirmed from what it asked, never from an id quoted at it;
        * the answer arrives on a LATER turn, which is the ordinary case: the
          administrator reads the read-back in their mail client and replies, and
          that reply is a fresh inbound with its own session. There is nothing in
          process to match against, so the record is ADOPTED from the caller.

        Adoption is safe because of where the caller's row comes from. Only the
        trust gate can mint an act row, only on an authenticated turn an
        administrator opened, and only from the block the firm authored in the
        root-owned customer.yaml, which the broker re-reads and re-checks. The
        caller has just re-fetched that row from the broker and verified that the
        person answering is an administrator. Nothing here is the model's word.
        """
        if not isinstance(session_id, str) or not session_id:
            return False
        if not act.proposal_id or not act.tool:
            return False
        rec = self._live(session_id)
        if rec is None:
            rec = PendingAct(
                proposal_id=act.proposal_id,
                tool=act.tool,
                readback="",
                created_at=self._now(),
                delivered=True,
            )
            self._records[session_id] = rec
            self._records.move_to_end(session_id)
            while len(self._records) > self.max_sessions:
                self._records.popitem(last=False)
        if rec.proposal_id != act.proposal_id or rec.tool != act.tool:
            return False
        if rec.confirmed is not None or rec.in_flight:
            return False
        rec.confirmed = ConfirmedAct(
            proposal_id=act.proposal_id,
            tool=act.tool,
            payload=copy.deepcopy(act.payload),
            instructed_by=act.instructed_by,
            confirmed_by=act.confirmed_by,
            confirmed_message_id=act.confirmed_message_id,
            confirmed_at=act.confirmed_at,
        )
        return True

    # -- replay / consume (trust gate, on the re-invoked call) --------------
    def peek_confirmed(self, session_id: Any, tool: str) -> ConfirmedAct | None:
        """The confirmed act for THIS tool, without consuming it, or None.

        Tool-bound: a confirmation of one commitment never authorizes a
        different one, however similar the call looks.
        """
        rec = self._live(session_id)
        if rec is None or rec.in_flight or rec.confirmed is None:
            return None
        if rec.tool != tool:
            return None
        return ConfirmedAct(
            proposal_id=rec.confirmed.proposal_id,
            tool=rec.confirmed.tool,
            payload=copy.deepcopy(rec.confirmed.payload),
            instructed_by=rec.confirmed.instructed_by,
            confirmed_by=rec.confirmed.confirmed_by,
            confirmed_message_id=rec.confirmed.confirmed_message_id,
            confirmed_at=rec.confirmed.confirmed_at,
        )

    def take_in_flight(self, session_id: Any, tool: str) -> ConfirmedAct | None:
        """Spend the approval and hold the record for the outcome hook.

        Called once the call has cleared every gate and is about to execute. The
        record survives so :meth:`finish` can commit the broker row against the
        result, but it is no longer an approval: a second call of the same tool
        on the same turn is withheld again.
        """
        act = self.peek_confirmed(session_id, tool)
        if act is None:
            return None
        rec = self._records.get(str(session_id))
        if rec is not None:
            rec.in_flight = True
        return act

    def finish(self, session_id: Any, tool: str) -> ConfirmedAct | None:
        """Pop the in-flight record so its outcome can be committed, or None.

        Popping on both outcomes is deliberate. On success the broker row is
        committed; on failure nothing is committed and the seat holds no stale
        approval, so a retry has to be proposed and confirmed again. The broker
        row simply stays open until its own TTL sweeps it.
        """
        rec = self._live(session_id)
        if rec is None or not rec.in_flight or rec.confirmed is None or rec.tool != tool:
            return None
        self._records.pop(str(session_id), None)
        return rec.confirmed

    # -- introspection / lifecycle -----------------------------------------
    def peek(self, session_id: Any) -> PendingAct | None:
        """The session's record at whatever stage it is in. For tests and audit."""
        return self._live(session_id)

    def clear(self, session_id: Any = None) -> None:
        """Drop one session's record, or every record when called with no id."""
        if session_id is None:
            self._records.clear()
            return
        self._records.pop(str(session_id), None)


#: Process-wide register, keyed by session. The trust gate proposes, replays and
#: consumes; the establishment plugin confirms. Both import THIS instance.
PENDING_ACTS = PendingActRegister()

__all__ = [
    "PENDING_ACTS",
    "ConfirmedAct",
    "PendingAct",
    "PendingActRegister",
    "tool_call_failed",
]
