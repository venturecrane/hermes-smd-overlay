"""Honcho conclusion poller + D1 writer.

Stub. §7 ports the implementation from ss-console/ai-employee/adapter/memory/.

Writes persona_observations rows with provenance:
- source_message_ids — pulled from Honcho's reasoning tree per conclusion.
- confidence — Honcho-assigned score carried through unchanged.
- evidence_status — computed from the source-message list at mirror time
  (e.g. "supported" | "weak" | "orphaned" if upstream messages have since
  been deleted or corrected).
- mirrored_at — timestamp of D1 write, distinct from Honcho's created_at.

Honcho remains the live store. D1 is the parallel record Captain operates on
through the admin portal.
"""


def poll_honcho_conclusions(customer_slug: str, **kwargs):
    """Fetch new/changed conclusions from the local Honcho for this customer. Stub."""
    raise NotImplementedError("ported in §7 from ss-console/ai-employee/adapter/memory/")


def write_to_d1(observations, **kwargs):
    """Insert persona_observations rows with full provenance. Stub."""
    raise NotImplementedError("ported in §7 from ss-console/ai-employee/adapter/memory/")
