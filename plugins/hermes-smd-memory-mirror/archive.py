"""TTL archival of aged Honcho conclusions.

Stub. §7 ports the implementation from ss-console/ai-employee/adapter/memory/.

Conclusions older than `archive_after_days` (default 180, configured per
customer.yaml) move from Honcho into D1's persona_observations_archive table
and are then physically deleted from Honcho. Captain's admin portal can
restore an archived observation from D1 back into the live Honcho store.

This keeps the Honcho working set bounded without losing operator-visible
history. The D1 archive is the durable long-tail.
"""


def archive_aged_conclusions(customer_slug: str, archive_after_days: int = 180, **kwargs):
    """Move conclusions older than the TTL from Honcho to D1 archive, then delete from Honcho. Stub."""
    raise NotImplementedError("ported in §7 from ss-console/ai-employee/adapter/memory/")


def restore_from_archive(observation_id: str, **kwargs):
    """Restore an archived observation from D1 back into the live Honcho store. Stub."""
    raise NotImplementedError("ported in §7 from ss-console/ai-employee/adapter/memory/")
