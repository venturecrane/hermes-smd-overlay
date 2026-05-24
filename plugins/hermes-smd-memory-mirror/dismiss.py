"""Captain dismissal of active Honcho conclusions.

Stub. §7 ports the implementation from ss-console/ai-employee/adapter/memory/.

Exposes a CLI/HTTP entry point invoked when Captain dismisses an active
conclusion in the admin portal. The dismissal path calls
`DELETE /conclusions/{id}` against the local Honcho instance — a physical
delete, not a soft flag.

The physical-delete posture works around Honcho upstream bug #658
(corrections do not propagate through the reasoning tree). Until that lands,
the only reliable way to remove a wrong conclusion from agent behavior is to
remove the row. The D1 mirror retains a dismissed-with-reason record for
auditability.
"""


def dismiss_conclusion(observation_id: str, reason: str, **kwargs):
    """Physical-delete the conclusion in Honcho; record the dismissal in D1. Stub."""
    raise NotImplementedError("ported in §7 from ss-console/ai-employee/adapter/memory/")
