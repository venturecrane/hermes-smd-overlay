"""The trace a date-prep step leaves: the memo its routine filed, seen by code.

THE PROBLEM (ss-console ``docs/specs/operator/case-manager-deadline-work.md``,
Jobs 2 and 3). A date-prep step the firm set to "Handles it" is run by the
date-prep turn itself, and when no decision is left there is no brief, so the
work shows up nowhere a person reads. The fix records the step in the casework
ledger (``step_ran``) so the next message that person gets says it was done.
A row that says work happened must not rest on the model's word that it did.

THE WITNESS. Every prep routine a step runs files its own ``[Operator]`` memo on
the matter (``mcp_smokeball_create_memo``); that memo is how the next brief
knows the routine ran. This register watches the post-tool hook for successful
create_memo calls and keeps, per session, the ones nobody has claimed yet: the
call id (the key of that call's ``TOOL_CALL_COMPLETED`` audit row), the RAW
session id the audit row carries, and the matter the memo went on. The
``casework_step_done`` tool claims one per recorded step, and the broker checks
the claimed call against its own audit log before it writes the row, so a step
is recorded only on a memo that really landed, on the brief's matter, in this
session, and never twice on one memo.

Exception-safe: a hook never raises.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from shared import casework_ledger
from shared.casework_acts import _result_reports_error
from shared.pending_acts import tool_call_failed

logger = logging.getLogger(__name__)

MEMO_TOOL = casework_ledger.STEP_WITNESS_TOOL
_TTL_SECONDS = 1800.0
_MAX_SESSIONS = 64
_MAX_PER_SESSION = 20


@dataclass(frozen=True)
class Memo:
    """One successful create_memo call: what a ``step_ran`` row may claim."""

    call_id: str
    raw_session: str
    matter_id: str


@dataclass
class _Seen:
    memos: list[Memo] = field(default_factory=list)
    claimed: set[str] = field(default_factory=set)
    touched: float = field(default_factory=time.monotonic)


def _args(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


class StepWitness:
    """Per-session record of the memos the turn filed (see the module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, _Seen] = OrderedDict()

    def _fresh(self, session_id: str, *, create: bool = False) -> _Seen | None:
        seen = self._sessions.get(session_id)
        if seen is not None and time.monotonic() - seen.touched > _TTL_SECONDS:
            self._sessions.pop(session_id, None)
            seen = None
        if seen is None and create:
            seen = self._sessions[session_id] = _Seen()
            while len(self._sessions) > _MAX_SESSIONS:
                self._sessions.popitem(last=False)
        return seen

    def on_post_tool(self, tool_name: str, session_id: str, kwargs: dict[str, Any]) -> None:
        """Keep a successful create_memo call. Never raises."""
        try:
            if tool_name != MEMO_TOOL or not session_id:
                return
            call_id = str(kwargs.get("tool_call_id") or "")
            matter_id = str(_args(kwargs.get("args")).get("matter_id") or "").strip()
            if not call_id or not matter_id:
                return
            if tool_call_failed(kwargs.get("status"), kwargs.get("error_type")):
                return
            if _result_reports_error(kwargs.get("result")):
                return
            memo = Memo(call_id, str(kwargs.get("session_id") or ""), matter_id)
            with self._lock:
                seen = self._fresh(session_id, create=True)
                if seen is not None and len(seen.memos) < _MAX_PER_SESSION:
                    seen.memos.append(memo)
                    seen.touched = time.monotonic()
        except Exception:  # noqa: BLE001 — a hook must never raise
            logger.warning("casework_steps: memo not recorded", exc_info=True)

    def unclaimed(self, session_id: str, matter_id: str) -> list[Memo]:
        """This session's memos on ``matter_id`` no step has claimed, oldest first."""
        with self._lock:
            seen = self._fresh(session_id)
            if seen is None:
                return []
            return [
                m for m in seen.memos if m.matter_id == matter_id and m.call_id not in seen.claimed
            ]

    def claim(self, session_id: str, call_id: str) -> None:
        with self._lock:
            seen = self._fresh(session_id)
            if seen is not None:
                seen.claimed.add(call_id)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


#: The process singleton the trust plugin's post-tool hook feeds and the
#: escalation plugin's ``casework_step_done`` reads.
STEP_WITNESS = StepWitness()


__all__ = ["MEMO_TOOL", "Memo", "STEP_WITNESS", "StepWitness"]
