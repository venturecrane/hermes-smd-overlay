"""Pending-send approval register (ADR 0071 / #1806).

The ``confirm`` ceiling (``external_send`` authored at ``confirm``) WITHHOLDS a
proactive send until the human explicitly approves it over a trusted channel (a
Telegram DM from the allowlisted owner). This module is the process-wide handoff
between the two hooks that implement that round-trip:

* the trust gate (``pre_tool_call``) CAPTURES the full send payload when the
  ceiling returns ``await_approval``, and later CONSUMES an approved record —
  replaying the STORED payload verbatim so the content that ships is exactly what
  was withheld;
* the approval-capture step (``pre_llm_call``) APPROVES the pending record its
  sender may approve when an allowlisted Telegram sender replies with a bare
  affirmative.

Design invariants (see the #1806 plan + critique):

- **One outstanding pending per approver.** A new compose SUPERSEDES every
  record it overlaps (and resets approval). For any one approver there is only
  ever one thing a bare "yes" could mean.
- **Bound to its requester (2026-10-17).** Each record carries the person and
  chat it was raised in (``shared.turn_principal``), and only that person in
  that chat can approve it: one allowlisted partner on a two-principal seat can
  no longer release the other's send, and a "yes" in a group never releases a
  send raised in a DM, nor the reverse. Records whose approvers cannot overlap
  coexist, so each partner may have one outstanding. On a single-principal seat
  every record overlaps every other, which is exactly the old single slot.
- **Stored-payload replay.** The record holds the full args captured at withhold;
  the gate overwrites the re-invoked call's args with them. LLM re-compose drift
  (reflowed whitespace, a reworded body, an injected cc/bcc/attachment on the
  second turn) cannot change what ships — the approved payload is definitional,
  not a probabilistic hash match.
- **Content-bound consume.** Consume requires the re-invoked call to match the
  stored ``(tool_name, recipient-set)``; a changed recipient → no match →
  re-withheld (the different send needs its own approval). A person-raised
  record is released only on that person's turn in that chat.
- **Single-use + TTL.** Consumed on the matching send; expires after
  ``ttl_seconds`` (15 min). A fresh capture always resets approval, so a stale
  approval can never attach to newly-composed content.

Single tenant per Machine (AGENTS.md #5), so a process singleton is correct. The
fingerprint is the ``(tool, recipients)`` of a send, the binding is who raised
it, and the approval source is the allowlisted channel sender.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from shared import turn_principal
from shared.turn_principal import UNBOUND, Principal

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
    #: The participant fence (shared.send_anchor): the request this send
    #: answered and the lane that sent it, captured WHEN IT WAS HELD. The
    #: approval turn is a different turn; replaying under its anchor would let
    #: an approver's email vouch for people who were on someone else's request.
    #: The broker re-reads the anchor at replay.
    anchor: dict[str, str] | None = None
    lane: str | None = None
    #: Who raised it and in which chat (``shared.turn_principal.requester``),
    #: captured WHEN IT WAS HELD. ``UNBOUND`` when the holding turn had no chat
    #: identity (no session context, an email turn). See :func:`_may_approve`.
    requester: Principal = UNBOUND
    #: The session that COMPOSED the send, captured WHEN IT WAS HELD. The
    #: outbound scans (identifier gate, matter gate) judge a body against what
    #: that session read; the approval turn read nothing, so the out-of-band
    #: replay scans under this id, never the approver's. ``None`` when the
    #: holding call carried no session id.
    session_id: str | None = None


def _overlaps(a: Principal, b: Principal) -> bool:
    """Could one approver satisfy both bindings? An unknown field matches anything."""
    users = not a.user_id or not b.user_id or a.user_id == b.user_id
    chats = not a.chat_id or not b.chat_id or a.chat_id == b.chat_id
    return users and chats


def _same_chat(bound: Principal, other: Principal | None) -> bool:
    if other is None or not bound.chat_id or bound.chat_id != other.chat_id:
        return False
    # Telegram chat ids already differ between a DM and a group; the type is a
    # second, independent check whenever both sides know it.
    return not (bound.chat_type and other.chat_type and bound.chat_type != other.chat_type)


def _may_approve(req: Principal, approver: Principal | None, sole_principal: bool) -> bool:
    """Whether ``approver`` may approve a record raised under ``req``.

    * Raised by a person: only that person, in that same chat. No exception for a
      single-principal seat: the owner's DM-to-DM flow is unchanged, and a group
      "yes" must not release a DM-raised send there either.
    * Raised by a routine into a chat (cron): anyone allowlisted in that chat;
      on a single-principal seat, the one principal from anywhere, as before.
    * Raised with no binding: only on a single-principal seat, as before. With
      two or more principals nothing says whose it is, so it fails closed.
    """
    if req.user_id:
        return (
            approver is not None and approver.user_id == req.user_id and _same_chat(req, approver)
        )
    if sole_principal:
        return True
    if req.chat_id:
        return _same_chat(req, approver)
    return False


def _may_consume(req: Principal, turn: Principal) -> bool:
    """A person-raised approved send is released only on that person's turn in
    that chat, so a partner's turn re-invoking the same send cannot spend it."""
    if not req.user_id:
        return True
    return turn.user_id == req.user_id and _same_chat(req, turn)


@dataclass
class PendingSendRegister:
    """Process-wide pending-send register (single tenant/Machine): one record per
    non-overlapping requester binding (see the module invariants)."""

    ttl_seconds: float = _DEFAULT_TTL_SECONDS
    _records: list[PendingSend] = field(default_factory=list)  # oldest first

    # -- time ---------------------------------------------------------------
    def _now(self) -> float:
        return time.time()

    def _expired(self, rec: PendingSend, now: float | None = None) -> bool:
        now = self._now() if now is None else now
        return (now - rec.created_at) > self.ttl_seconds

    def _live(self) -> list[PendingSend]:
        """Unconsumed, unexpired records, newest first. Prunes the expired."""
        now = self._now()
        self._records = [r for r in self._records if not r.consumed and not self._expired(r, now)]
        return list(reversed(self._records))

    # -- capture (pre_tool_call, on await_approval) -------------------------
    def capture(
        self,
        tool_name: str,
        args: Any,
        recipients: Iterable[str] | None,
        *,
        anchor: dict[str, str] | None = None,
        lane: str | None = None,
        requester: Principal | None = None,
        session_id: str | None = None,
    ) -> None:
        """Record a pending send, SUPERSEDING every record it overlaps.

        A fresh capture always resets approval: a stale approval must never attach
        to newly-composed content. ``args`` is deep-copied so later mutation of the
        live tool-args dict (SEC-36 strip, broker grant, the eventual overwrite)
        cannot corrupt the stored payload. ``requester`` defaults to ``UNBOUND``,
        which overlaps every record: the old single slot.
        """
        req = requester or UNBOUND
        self._live()
        self._records = [r for r in self._records if not _overlaps(r.requester, req)]
        self._records.append(
            PendingSend(
                tool_name=tool_name,
                args=copy.deepcopy(args) if isinstance(args, dict) else {},
                recipients=frozenset(recipients or ()),
                created_at=self._now(),
                anchor=dict(anchor) if anchor else None,
                lane=lane,
                requester=req,
                session_id=session_id or None,
            )
        )

    # -- approve (pre_llm_call, allowlisted-sender affirmative) -------------
    def approve(
        self, source: str, approver: Principal | None = None, *, sole_principal: bool = True
    ) -> PendingSend | None:
        """Mark approved the newest fresh record ``approver`` may approve; return it.

        ``None`` when nothing fresh is pending that this approver may approve.
        Idempotent. ``source`` (e.g. ``"telegram:7367659986"``) is kept for audit.
        ``sole_principal`` is whether the seat allowlists exactly one approver; the
        production caller (``approval.py``) always computes it and always names
        the approver.
        """
        for rec in self._live():
            if _may_approve(rec.requester, approver, sole_principal):
                rec.approved = True
                rec.approval_source = source
                return rec
        return None

    def mark_approved(
        self, source: str, approver: Principal | None = None, *, sole_principal: bool = True
    ) -> bool:
        """:meth:`approve`, as a bool."""
        return self.approve(source, approver, sole_principal=sole_principal) is not None

    # -- match / consume (pre_tool_call, on the re-invoked send) ------------
    def approved_match(
        self, tool_name: str, recipients: Iterable[str] | None, turn: Principal | None = None
    ) -> PendingSend | None:
        """The newest approved, unexpired, unconsumed record matching
        ``(tool_name, recipient-set)`` that THIS turn may release. Non-consuming.
        A mismatch leaves every record intact (the correct re-invoke can still
        consume it). ``turn`` defaults to the running turn's principal."""
        recips = frozenset(recipients or ())
        approved = [r for r in self._live() if r.approved]
        if not approved:
            return None
        turn = turn_principal.current() if turn is None else turn
        for rec in approved:
            if rec.tool_name != tool_name or rec.recipients != recips:
                continue
            if _may_consume(rec.requester, turn):
                return rec
        return None

    def has_approved_match(
        self, tool_name: str, recipients: Iterable[str] | None, turn: Principal | None = None
    ) -> bool:
        """True iff an approved record matches this send — WITHOUT consuming it.

        The gate calls this to resolve ``current_turn_approval`` before enforcing;
        it only consumes (``take_for_send``) once the send has cleared every other
        gate and is actually about to ship."""
        return self.approved_match(tool_name, recipients, turn) is not None

    def take_for_send(
        self, tool_name: str, recipients: Iterable[str] | None, turn: Principal | None = None
    ) -> PendingSend | None:
        """CONSUME and return the approved matching record (single-use), or ``None``.

        Same match as :meth:`approved_match`; on match the record is consumed and
        removed so it can never authorize a second send. Returned so the caller
        can replay ``record.args``."""
        rec = self.approved_match(tool_name, recipients, turn)
        if rec is None:
            return None
        rec.consumed = True
        self._records = [r for r in self._records if r is not rec]  # single-use
        return rec

    # -- introspection / lifecycle -----------------------------------------
    def peek(self) -> PendingSend | None:
        """The newest pending record (approved or not), or None. For tests/audit
        and for "is anything outstanding at all"."""
        return self._records[-1] if self._records else None

    def pending(self) -> list[PendingSend]:
        """Every live record, newest first."""
        return self._live()

    def clear(self) -> None:
        self._records = []


# Process-wide singleton — the trust gate captures/consumes, the approval-capture
# step approves. Both import THIS instance (single tenant per Machine, AGENTS.md #5).
PENDING_SEND = PendingSendRegister()
