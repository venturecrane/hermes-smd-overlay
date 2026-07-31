"""Per-turn authored-spec read register (ss ADR 0083, ss-console #2084).

The runtime half of the spec loader. Delivery puts a POINTER to the seat's
authored spec in front of the model; this register records whether the model
actually followed it, so a class that declares a spec cannot ship an output
composed without one.

You cannot make a model read something. You can make not-reading-it fail the
send. That asymmetry is the entire design: the register is written by the trust
plugin when a spec file is read, and consulted by the spec gate at the moment a
send would leave. No read this turn, no send this turn.

Same producer/consumer split and the same module-level-singleton shape as
``shared.voice_status`` and ``shared.inbound.SESSION_TAINT``, for the same
reason: two plugins cannot import each other (hyphenated package dirs are not
dotted module paths), so ``shared/`` is the only seam. Process-wide singleton;
one tenant per Machine (AGENTS.md #5).

CLEARED EVERY TURN. Marks are set from ``pre_tool_call`` and cleared at the
start of every turn from ``pre_llm_call``, keyed by
``shared.provenance.resolve_session`` so producer and consumer agree on the key.
A read three turns ago must not certify this turn's send: the spec governs the
composition that is happening now, and a sticky mark would certify a draft the
spec never touched. Bounded FIFO so a long-lived Machine with churning session
ids cannot leak the register.

WHAT A MARK MEANS. It is set ONLY after ``shared.spec_manifest`` has verified
the file's bytes against the ROOT-OWNED manifest. It therefore means "the agent
read the spec root installed", not "the agent read a file at a path that looked
like a spec". A file under the spec dir that the manifest does not name, or one
whose bytes no longer hash to the recorded digest, sets nothing.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SpecReadStatus:
    """Which authored specs the agent read on each session's CURRENT turn."""

    max_sessions: int = 512
    _read: OrderedDict[str, set[tuple[str, str]]] = field(default_factory=OrderedDict)

    # ------------------------------------------------------------------
    # Producer — the trust plugin's pre_tool_call
    # ------------------------------------------------------------------

    def mark_read(self, session_id: str, output_class: str, prop: str) -> None:
        """Record a verified read of ``output_class``'s ``prop`` spec.

        No-op for an empty session id or empty identifiers. The caller must have
        verified the file against the root-owned manifest first — this register
        stores the claim, it does not check it.
        """
        if not session_id or not output_class or not prop:
            return
        marks = self._read.get(session_id)
        if marks is None:
            marks = set()
            self._read[session_id] = marks
        marks.add((output_class, prop))
        self._read.move_to_end(session_id)
        while len(self._read) > self.max_sessions:
            self._read.popitem(last=False)

    def clear_turn(self, session_id: str) -> None:
        """Drop this session's marks (start of every turn). Idempotent."""
        if not session_id:
            return
        self._read.pop(session_id, None)

    # ------------------------------------------------------------------
    # Consumer — the spec gate
    # ------------------------------------------------------------------

    def was_read(self, session_id: str, output_class: str, prop: str) -> bool:
        """True iff that exact spec was read, and verified, on this turn.

        Empty id ⇒ ``False``. An untracked turn cannot certify anything, and the
        gate reads False as a refusal — fail-closed, matching every other
        can't-confirm path in the trust plugin.
        """
        if not session_id or not output_class or not prop:
            return False
        return (output_class, prop) in self._read.get(session_id, set())

    def read_this_turn(self, session_id: str) -> frozenset[tuple[str, str]]:
        """Every ``(class, property)`` read this turn. Diagnostics and audit."""
        if not session_id:
            return frozenset()
        return frozenset(self._read.get(session_id, set()))

    def _reset_for_tests(self) -> None:
        self._read.clear()


#: Process-wide singleton — the trust plugin marks, the spec gate reads.
SPEC_STATUS = SpecReadStatus()


__all__ = ["SPEC_STATUS", "SpecReadStatus"]
