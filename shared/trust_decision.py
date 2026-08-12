"""Per-tool-call trust-decision handoff (ss-console #2122).

The gate decides; the ledger records. They are two different plugins, and until
this module existed nothing carried the decision from one to the other.

``hermes-smd-trust`` computes the whole authorization trail on ``pre_tool_call``
— the resolved action class, the authored ceiling, the vertical floor, the
effective ceiling, and the allow / draft / refuse / await_approval verdict
(``enforce.EnforcementDecision``, whose docstring already calls those fields
"the full trust trail"). ``hermes-smd-audit`` writes the audit row on
``post_tool_call``. Nothing joined the two, so the trail was computed and
dropped: on the pilot seat ``ceiling_level`` was null on 100% of 4130 live rows
because the one caller passed a literal ``None``, and the typed send classes the
entitlements actually govern (``external_send_client`` / ``_vendor`` /
``_internal``) never reached a row at all — the row carried only the coarse
class ``classify_tool`` returns BEFORE recipient resolution. Every row said what
the agent did; none said what authorized it.

Two plugins cannot import each other (the plugin directories are hyphenated and
therefore not dotted module paths), so ``shared/`` is the only seam. Same
producer/consumer split and the same process-singleton shape as
``shared.pending_send``, ``shared.spec_status`` and ``shared.inbound.SESSION_TAINT``,
for the same reason. One tenant per Machine (AGENTS.md #5).

This module holds PLAIN DATA — strings and bools, never ``ActionClass`` or
``Ceiling``. ``shared/`` is the lower layer and must not import upward into a
plugin, the same rule ``audit_contract.ACTOR_AGENT`` is pinned under.

MATCHING, AND WHY THE ROW SAYS HOW IT MATCHED
---------------------------------------------

``tool_call_id`` is the documented per-call key at both hooks
(``docs/hook-surface.md``), and it is the primary key here. But the pre-hook's
kwargs are not trustworthy on that point: core drops ``session_id`` on the
``pre_tool_call`` path (overlay #141 — the fire sites in ``run_agent.py`` pass
``task_id`` only), and those are the same fire sites that would have carried
``tool_call_id``. A join that silently never matches is precisely the failure
this module exists to end, so it does not depend on that kwarg being present.

The fallback is the last decision made ON THIS THREAD, and it is sound because
the two hooks bracket one dispatch with nothing in between. Core states the
contract itself at ``/opt/hermes/model_tools.py:1053`` — "Single-fire contract:
pre_tool_call fires exactly once per tool execution" — and the bracket is
visible in one function body: the pre-hook fires at 1059-1074,
``registry.dispatch`` at 1143/1151, ``_emit_post_tool_call_hook`` at 1178. So at
post-time the last decision recorded on that thread IS this call's. It is
guarded anyway — the tool name must agree and every take is single-use, so a
disagreement yields NO decision rather than the wrong one.

(Line numbers above are from the Hermes that RUNS ON THE SEAT, not from the ref
``docs/hook-surface.md`` pins. That document's citations for these sites are
older and no longer address this file; the structural contract they describe is
unchanged and confirmed.)

WHY THE FALLBACK IS PER-THREAD AND NOT PROCESS-GLOBAL
------------------------------------------------------

A process-global "last decision" is wrong here, and dangerously so. Core keeps a
long-lived event loop per worker thread in thread-local storage —
``/opt/hermes/model_tools.py:66-80``, ``_get_worker_loop()``: "Each worker
thread (e.g., delegate_task's ThreadPoolExecutor threads) gets its own
long-lived loop stored in thread-local storage." ADR 0021 has us using
``delegate_task`` as a native primitive, so concurrent agent threads in one
process are a live configuration, not a hypothetical.

Two threads can therefore sit inside their own pre→dispatch→post brackets at the
same time, and a global slot would hand thread A's decision to thread B's row.
The tool-name guard does not close that: two concurrent calls to the SAME tool
(parallel reads are routine) pass it. For a legal ledger a mis-attributed
ceiling is worse than a null one — the row asserts that something authorized a
call it did not authorize. Within one thread the bracket genuinely is
sequential, so the slot is thread-local, mirroring core's own idiom.

The keyed map stays process-wide, because a ``tool_call_id`` is unique per call
and so cannot mis-attribute across threads; it carries a lock only so concurrent
records cannot corrupt the eviction bookkeeping. If a pre/post pair ever DID
split across threads, the keyed path still resolves and the sequential path
degrades to no decision — never to someone else's.

And the match is DECLARED. The audit row records which way it matched
(``trust_decision_match``: ``tool_call_id`` | ``sequential`` | ``none``). A
compliance ledger may not present an inferred join as a keyed one, and a row
with no trust provenance must say so rather than look like a row that simply
predates the field.

THE SESSION RESOLUTION RIDES THE SAME RAIL (ss-console #2288)
--------------------------------------------------------------

``shared.provenance.resolve_session`` had the process-global slot this module
argues against, keying every per-session safety register off it. It is now
thread-local with a declared mode, and the decision carries that mode
(``session_match`` / ``session_resolved``) so the row can state it. The ride is
not incidental: the resolution that gates a call happens in the PRE-hook, this
is the only structure that already carries a pre-hook fact to a post-hook row,
and it is single-use and per-thread — the properties a resolution record needs
for exactly the reasons set out above.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

#: How a row's decision was joined to its tool call. Stamped into every per-tool
#: audit row so an auditor never has to guess which of the three it was.
MATCH_KEYED = "tool_call_id"
MATCH_SEQUENTIAL = "sequential"
MATCH_NONE = "none"

#: The action-class label recorded for a permanently-banned tool. Not an
#: ``ActionClass`` member — a banned tool is refused by name before any class is
#: resolved, and the existing trust-decision log line already says
#: ``action_class=banned`` for it.
ACTION_CLASS_BANNED = "banned"


@dataclass(frozen=True)
class TrustDecision:
    """One ``pre_tool_call`` authorization decision, as plain strings.

    Mirrors the six fields the trust plugin already logs on every decision
    (``enforce._audit_decision``): the resolved action class, the authored
    ceiling, the vertical floor, the effective ceiling, the verdict, and the
    reason — plus the persona the exposure was resolved for.

    ``authored_ceiling`` / ``vertical_floor`` / ``effective_ceiling`` are
    ``None`` when unauthored, absent, or indeterminate; the audit row keeps that
    distinction rather than coercing it to a string, because "no ceiling was
    authored for this class" and "the ceiling could not be resolved" are
    different facts and only one of them is fail-closed by design.
    """

    action_class: str
    audit_action: str  # "allow" | "draft" | "refuse" | "await_approval"
    allowed: bool
    authored_ceiling: str | None = None
    vertical_floor: str | None = None
    effective_ceiling: str | None = None
    persona: str = ""
    reason: str = ""
    #: HOW the session this call was gated under was resolved — one of
    #: ``shared.provenance``'s ``MODE_*`` values, and the session it landed on
    #: (ss-console #2288). Carried here rather than read at post-time because
    #: the resolution that MATTERS happened in the pre-hook: core drops
    #: session_id there (#141), so that is the only point where a fallback can
    #: have keyed the trust gate, the matter gate's party set, and the spec and
    #: voice marks. By post_tool_call core supplies the real id again, and a
    #: resolution taken then would report ``keyed`` for a call an inference
    #: gated. Same reason ``TrustDecision`` exists at all.
    session_match: str = ""
    session_resolved: str = ""


@dataclass
class TrustDecisionRegister:
    """Pre→post handoff for the current tool call.

    Two stores with deliberately different scopes:

    * ``_by_call`` — process-wide, keyed by the per-call ``tool_call_id``. Safe
      to share because the key is unique per call. Bounded FIFO so a long-lived
      Machine whose pre-hook decisions are never collected (a refused call never
      dispatches, so it never reaches ``post_tool_call``) cannot grow it without
      limit. Guarded by a lock: the eviction bookkeeping is a read-then-mutate
      sequence, which concurrent recorders would otherwise interleave.
    * ``_local.last`` — THREAD-LOCAL. See the module docstring: concurrent
      ``delegate_task`` worker threads each run their own pre→dispatch→post
      bracket, so a shared last-decision slot would cross-attribute one thread's
      ceiling onto another thread's row.
    """

    max_calls: int = 256
    #: tool_call_id -> (tool_name, decision). Process-wide; lock-guarded.
    _by_call: OrderedDict[str, tuple[str, TrustDecision]] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    #: Thread-local holder for ``last``: (tool_call_id, tool_name, decision).
    _local: threading.local = field(default_factory=threading.local)

    # ------------------------------------------------------------------
    # Producer — the trust plugin's pre_tool_call
    # ------------------------------------------------------------------

    def record(self, tool_call_id: str, tool_name: str, decision: TrustDecision) -> None:
        """Record the decision for the tool call about to dispatch.

        A later record supersedes the sequential slot FOR THIS THREAD, so the
        slot holds this call's decision by the time its ``post_tool_call`` fires
        on the same thread. No-op on an empty tool name (a malformed pre-hook
        kwarg, never a real call).
        """
        if not tool_name:
            return
        key = tool_call_id or ""
        if key:
            with self._lock:
                self._by_call[key] = (tool_name, decision)
                self._by_call.move_to_end(key)
                while len(self._by_call) > self.max_calls:
                    self._by_call.popitem(last=False)
        self._local.last = (key, tool_name, decision)

    # ------------------------------------------------------------------
    # Consumer — the audit plugin's post_tool_call
    # ------------------------------------------------------------------

    def take(self, tool_call_id: str, tool_name: str) -> tuple[TrustDecision | None, str]:
        """CONSUME the decision for this tool call. Returns ``(decision, match)``.

        Single-use on both paths: a taken decision can never be attributed to a
        second row. The tool name must agree on either path — a disagreement
        returns ``(None, MATCH_NONE)``, because a row with no trust provenance is
        honest and a row with someone else's is not. The fallback reads only
        THIS thread's slot, so a concurrent worker's decision is unreachable
        from here by construction, not by a guard that could be defeated by two
        calls to the same tool.
        """
        if not tool_name:
            return None, MATCH_NONE
        if tool_call_id:
            with self._lock:
                entry = self._by_call.pop(tool_call_id, None)
            if entry is not None and entry[0] == tool_name:
                last = getattr(self._local, "last", None)
                if last is not None and last[0] == tool_call_id:
                    self._local.last = None
                return entry[1], MATCH_KEYED
        last = getattr(self._local, "last", None)
        if last is not None and last[1] == tool_name:
            self._local.last = None
            if last[0]:
                with self._lock:
                    self._by_call.pop(last[0], None)
            return last[2], MATCH_SEQUENTIAL
        return None, MATCH_NONE

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Drop the shared keyed map and THIS thread's sequential slot.

        Another thread's slot is unreachable from here — that is the property
        that makes the slot safe, and it means ``clear()`` is a per-thread
        reset for the fallback. Test fixtures should clear on the thread they
        recorded on.
        """
        with self._lock:
            self._by_call.clear()
        self._local.last = None

    def _reset_for_tests(self) -> None:
        self.clear()


#: Process-wide singleton — the trust gate records, the audit plugin takes.
TRUST_DECISIONS = TrustDecisionRegister()


__all__ = [
    "ACTION_CLASS_BANNED",
    "MATCH_KEYED",
    "MATCH_NONE",
    "MATCH_SEQUENTIAL",
    "TRUST_DECISIONS",
    "TrustDecision",
    "TrustDecisionRegister",
]
