"""Per-customer D1 binding access with runtime namespace assertion.

Every Machine boots with a single D1 binding scoped to one customer. This module
is the gate that enforces it at runtime: every `execute` / `query` call asserts
the bound database matches the expected customer slug before issuing SQL. The
assertion runtime check ports from
ss-console/ai-employee/adapter/namespace_assertion.py in §7 of the build plan.

A namespace mismatch is treated as a fatal isolation breach: the client raises
rather than silently writing to the wrong tenant's database.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class D1Client:
    """Per-customer D1 client. Stub.

    The real implementation ports from
    ss-console/ai-employee/adapter/namespace_assertion.py and adds:
      - lazy binding resolution against the Machine's env
      - per-call assertion that binding_name maps to customer_slug
      - structured logging of namespace mismatches before raising
    """

    def __init__(self, binding_name: str, customer_slug: str) -> None:
        """Construct a D1 client pinned to a single customer.

        Args:
            binding_name: Name of the D1 binding wired into the Machine env
                (e.g. ``CUSTOMER_DB``). Resolved lazily on first call.
            customer_slug: Expected customer namespace. The runtime check
                asserts the resolved binding matches this slug.
        """
        self.binding_name = binding_name
        self.customer_slug = customer_slug

    def execute(self, sql: str, *params: Any) -> Any:
        """Execute a write statement. Stub."""
        raise NotImplementedError(
            "ported in §7 from ss-console/ai-employee/adapter/namespace_assertion.py"
        )

    def query(self, sql: str, *params: Any) -> Any:
        """Execute a read query and return rows. Stub."""
        raise NotImplementedError(
            "ported in §7 from ss-console/ai-employee/adapter/namespace_assertion.py"
        )
