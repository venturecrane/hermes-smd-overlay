"""customer.yaml → per-profile Hermes config translation.

For each persona in ``customer.yaml.personas[]`` the bootstrap CLI writes:

  $HERMES_HOME/profiles/<persona-slug>/config.yaml   (Hermes-native config shape)
  $HERMES_HOME/profiles/<persona-slug>/SOUL.md       (per-persona identity)

The Hermes-native config consumes the multi-persona pattern documented in
ADR 0011; per-persona SOUL.md is what Hermes loads as identity at profile
boot.

Structural-vs-non-structural change rule (ADR 0019)
---------------------------------------------------
``bootstrap`` is the structural path: persona add/remove, connector backend
swap, OAuth scope change, trust ceiling schema change. It rewrites profile
directories and is followed by a Machine restart so Hermes re-reads identity
and connector wiring from scratch.

``start_customer_sync`` is the non-structural path: tone tweaks, review
thresholds, voice samples, skill pin bumps within the same catalog. The
sidecar polls R2, applies the diff in place, and signals the Hermes process
with SIGHUP to reload without restart.

Real translation logic ports from
ss-console/ai-employee/adapter/validate_customer_yaml.py and
ss-console/ai-employee/adapter/resolve_skill_pins.py in §7 of the build plan.
"""

import logging

logger = logging.getLogger(__name__)


def translate_customer_yaml(customer_yaml_path: str, hermes_home: str) -> list[str]:
    """Translate ``customer.yaml`` into per-profile Hermes config. Stub.

    Args:
        customer_yaml_path: Absolute path to the authored ``customer.yaml``
            (typically ``/opt/data/customer.yaml`` on the Fly volume).
        hermes_home: Hermes home directory under which profile directories
            live (typically ``~/.hermes`` or ``$HERMES_HOME``).

    Returns:
        List of profile slugs written. Each slug corresponds to a persona in
        ``customer.yaml.personas[]`` and a directory under
        ``$HERMES_HOME/profiles/``.

    Raises:
        NotImplementedError: Until §7 of the build plan lands.
    """
    raise NotImplementedError(
        "ported in §7 from ss-console/ai-employee/adapter/validate_customer_yaml.py "
        "+ resolve_skill_pins.py"
    )


def start_customer_sync(customer_yaml_path: str, r2_bucket: str, interval: int) -> None:
    """Long-running sidecar that polls R2 for non-structural ``customer.yaml`` changes. Stub.

    Loops every ``interval`` seconds, fetches the latest authored
    ``customer.yaml`` from R2, diffs it against the on-disk copy, and (for
    non-structural fields only) writes the update in place and signals the
    Hermes process with SIGHUP. Structural diffs are rejected with a logged
    warning — those require Captain re-provision via ``bootstrap``.

    Args:
        customer_yaml_path: Absolute path to the on-disk ``customer.yaml`` to
            keep in sync.
        r2_bucket: R2 source identifier (URL or bucket reference) for the
            authoring tree.
        interval: Poll interval in seconds.

    Raises:
        NotImplementedError: Until §7 of the build plan lands.
    """
    raise NotImplementedError("ported in §7")
