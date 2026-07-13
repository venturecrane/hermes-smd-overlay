"""Pending-send approval register (ADR 0071 / #1806).

The ``confirm`` ceiling (``external_send`` authored at ``confirm``) WITHHOLDS a
proactive send until the human explicitly approves it over a trusted channel (a
Telegram DM from the allowlisted owner). This module is the process-wide handoff
between the two hooks that implement that round-trip:

* the trust gate (``pre_tool_call``) CAPTURES the full send payload when the
  ceiling returns ``await_approval``, and later CONSUMES an approved record —
  replaying the STORED payload verbatim so the content that ships is exactly what
  was withheld;
* the approval-capture step (``pre_llm_call``) MARKS the single pending record
  approved when an allowlisted Telegram sender replies with a bare affirmative.

Design invariants (see the #1806 plan + critique):

- **Single outstanding pending.** At most one record awaits approval; a new
  compose SUPERSEDES the prior (and resets approval). A bare "yes" is therefore
  never ambiguous — there is only ever one thing to approve.
- **Stored-payload replay.** The record holds the full args captured at withhold;
  the gate overwrites the re-invoked call's args with them. LLM re-compose drift
  (reflowed whitespace, a reworded body, an injected cc/bcc/attachment on the
  second turn) cannot change what ships — the approved payload is definitional,
  not a probabilistic hash match.
- **Content-bound consume.** Consume requires the re-invoked call to match the
  stored ``(tool_name, recipient-set)``; a changed recipient → no match →
  re-withheld (the different send needs its own approval).
- **Single-use + TTL.** Consumed on the matching send; expires after
  ``ttl_seconds`` (15 min). A fresh capture always resets approval, so a stale
  approval can never attach to newly-composed content.

Single tenant per Machine (AGENTS.md #5), so a process singleton is correct — no
session keying is needed. The fingerprint is the ``(tool, recipients)`` of the one
outstanding send; the approval source is the allowlisted channel sender.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# 15 minutes (ADR 0071 #1806): long enough for a human to read and approve, short
# enough that a stale approval window cannot be replayed later in the session.
_DEFAULT_TTL_SECONDS: float = 15 * 60


@dataclass
class PendingSend:
    """One withheld send awaiting a trusted current-turn approval.

    ``args`` is a deep copy of the send call captured at withhold — it is the
    payload REPLAYED verbatim on approval, so what ships is exactly what was
    reviewed. ``recipients`` is the normalized ``to`` set, the consume-match key.
    """

    tool_name: str
    args: dict[str, Any]
    recipients: frozenset[str]
    created_at: float
    approved: bool = False
    approval_source: str | None = None
    consumed: bool = False


@dataclass
class PendingSendRegister:
    """Process-wide single-outstanding-pending register (single tenant/Machine)."""

    ttl_seconds: float = _DEFAULT_TTL_SECONDS
    _record: PendingSend | None = None

    # -- time ---------------------------------------------------------------
    def _now(self) -> float:
        return time.time()

    def _expired(self, rec: PendingSend, now: float | None = None) -> bool:
        now = self._now() if now is None else now
        return (now - rec.created_at) > self.ttl_seconds

    # -- capture (pre_tool_call, on await_approval) -------------------------
    def capture(self, tool_name: str, args: Any, recipients: Iterable[str] | None) -> None:
        """Record — or SUPERSEDE — the single outstanding pending send.

        A fresh capture always resets approval: a stale approval must never attach
        to newly-composed content. ``args`` is deep-copied so later mutation of the
        live tool-args dict (SEC-36 strip, broker grant, the eventual overwrite)
        cannot corrupt the stored payload.
        """
        self._record = PendingSend(
            tool_name=tool_name,
            args=copy.deepcopy(args) if isinstance(args, dict) else {},
            recipients=frozenset(recipients or ()),
            created_at=self._now(),
        )

    # -- approve (pre_llm_call, allowlisted-sender affirmative) -------------
    def mark_approved(self, source: str) -> bool:
        """Mark the single pending record approved. Idempotent.

        Returns True iff a fresh (unconsumed, unexpired) record was marked; a
        no-op returning False when nothing is pending / it already expired. The
        caller supplies ``source`` (e.g. ``"telegram:7367659986"``) for audit.
        """
        rec = self._record
        if rec is None or rec.consumed or self._expired(rec):
            return False
        rec.approved = True
        rec.approval_source = source
        return True

    # -- match / consume (pre_tool_call, on the re-invoked send) ------------
    def _match(self, tool_name: str, recipients: Iterable[str] | None) -> PendingSend | None:
        """The pending record iff approved, unexpired, unconsumed, and matching
        ``(tool_name, recipient-set)``. Non-consuming. A stale (expired) record is
        cleared; a recipient/tool MISMATCH leaves the record intact (the correct
        re-invoke can still consume it)."""
        rec = self._record
        if rec is None or rec.consumed or not rec.approved:
            return None
        if self._expired(rec):
            self._record = None
            return None
        if rec.tool_name != tool_name:
            return None
        if rec.recipients != frozenset(recipients or ()):
            return None
        return rec

    def has_approved_match(self, tool_name: str, recipients: Iterable[str] | None) -> bool:
        """True iff an approved record matches this send — WITHOUT consuming it.

        The gate calls this to resolve ``current_turn_approval`` before enforcing;
        it only consumes (``take_for_send``) once the send has cleared every other
        gate and is actually about to ship."""
        return self._match(tool_name, recipients) is not None

    def take_for_send(self, tool_name: str, recipients: Iterable[str] | None) -> PendingSend | None:
        """CONSUME and return the approved matching record (single-use), or ``None``.

        Same match as :meth:`has_approved_match`; on match the record is consumed
        and cleared so it can never authorize a second send. Returned so the caller
        can replay ``record.args``."""
        rec = self._match(tool_name, recipients)
        if rec is None:
            return None
        rec.consumed = True
        self._record = None  # single-use
        return rec

    # -- introspection / lifecycle -----------------------------------------
    def peek(self) -> PendingSend | None:
        """The current pending record (approved or not), or None. For tests/audit."""
        return self._record

    def clear(self) -> None:
        self._record = None


# Process-wide singleton — the trust gate captures/consumes, the approval-capture
# step marks. Both import THIS instance (single tenant per Machine, AGENTS.md #5).
PENDING_SEND = PendingSendRegister()
