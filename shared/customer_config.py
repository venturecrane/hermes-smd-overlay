"""customer.yaml + per-profile config loader.

Reads the customer's authored ``customer.yaml`` from the Fly volume at
``/opt/data/customer.yaml`` and the materialized per-profile Hermes config at
``$HERMES_HOME/profiles/<slug>/config.yaml``. The bootstrap CLI is responsible
for translating the former into the latter (see ``bootstrap/translate.py``);
this module is the read path used by plugins at runtime.

Structural-vs-non-structural change rule (ADR 0019)
---------------------------------------------------
A ``customer.yaml`` field is **structural** when changing it requires the
Machine to re-provision: adding or removing a persona, swapping a connector
backend, adding or revoking an OAuth scope, changing the trust ceiling
schema. Structural changes go through Captain re-provision — the bootstrap
CLI rewrites profile directories and the Machine restarts.

A field is **non-structural** when it can be hot-reloaded: tone tweaks,
review thresholds, voice samples, skill pin bumps within the same catalog,
content policy adjustments. The ``customer-sync`` sidecar polls R2 for these
and signals SIGHUP to reload without restart.

Real loader logic ports from
ss-console/ai-employee/adapter/validate_customer_yaml.py in §7.
"""

import logging

logger = logging.getLogger(__name__)


class CustomerConfig:
    """In-memory view of ``customer.yaml`` plus resolved per-profile config. Stub.

    The real implementation:
      - parses YAML with safe_load
      - validates against the schema in ss-console
      - resolves skill pins
      - exposes typed accessors for personas, connectors, trust ceilings, and
        non-structural reload-eligible fields
    """

    @classmethod
    def from_volume(cls, path: str = "/opt/data/customer.yaml") -> "CustomerConfig":
        """Load a customer config from the Fly volume.

        Args:
            path: Absolute path to ``customer.yaml`` on the Machine's volume.
                Defaults to the standard mount point.

        Returns:
            A parsed and validated ``CustomerConfig``.

        Raises:
            NotImplementedError: Until §7 of the build plan lands.
        """
        raise NotImplementedError("ported in §7")
