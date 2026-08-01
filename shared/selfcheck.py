"""The boot self-check's identity, shared by the probe and its observers.

The ``gateway:startup`` activation gate proves the audit WRITE path is live by
driving a REAL ``post_llm_call`` dispatch and confirming a row lands in
``audit_log`` (ss-console#1285 Q2). Driving the real hook is the whole point —
a fake that skipped the fan-out would prove nothing about the live process.

But the real fan-out has more consumers than the one being tested. The audit
plugin's ``post_llm_call`` also meters the turn into the interactive cost meter
(ADR 0062 §4), and the probe's synthetic ``model="boot-selfcheck"`` is not in
the pricing table — so every boot alarmed ``model_unpriced`` and wrote an
``INVARIANT_VIOLATION`` row. All 61 such rows on the pilot seat were that alarm.
A safety signal that fires on a known-good boot is a signal nobody reads, which
is the real cost: ``INVARIANT_VIOLATION`` has to keep meaning "a genuine breach".

The probe is not a turn, so it is not metered. It already carried a distinct
session id declared for exactly this purpose ("trivially distinguishable from
real-turn rows"); this module promotes that id from a private constant in the
handler to the contract both sides read, so the probe and the observers that
must ignore it can never drift apart.

Scope note: this suppresses METERING of the probe, never the alarm itself. A
real turn on an unpriced model still alarms — that is a genuine "this seat's
spend is uncapped" finding and the only reason the alarm exists.
"""

from __future__ import annotations

#: Session id the activation handler stamps on its boot audit self-check
#: dispatch. Read by ``hooks/smd-overlay-activation/handler.py`` (the producer)
#: and ``shared.interactive_cost_meter`` (which must not score it).
SELFCHECK_SESSION_ID = "smd-activation-selfcheck"


def is_selfcheck_session(session_id: str | None) -> bool:
    """True iff this dispatch is the boot self-check probe, not a real turn."""
    return session_id == SELFCHECK_SESSION_ID


__all__ = ["SELFCHECK_SESSION_ID", "is_selfcheck_session"]
