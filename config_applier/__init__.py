"""Root-owned live config applier (ADR 0044 WS3).

A ROOT-owned process on the customer Machine pulls a freshly-authored
``customer.yaml`` from R2 (the source of truth), validates it with the
parity-hardened :func:`bootstrap.validate.validate_customer_yaml`, runs the
live-apply safety checks in :mod:`config_applier.safety`, atomically writes it
to the volume, and emits a ``CONFIG_WRITE`` audit row through the broker-aware
audit client. The gateway is then signalled to reload.

This package holds only the *logic* — every side effect (R2 read, filesystem
write, audit emission) is injected so the module is fully unit-testable with no
network, no real volume, and no broker socket. The boot script wires the real
clients and owns the SIGUSR1 reload signal; that wiring is out of scope here.

Two layers:

* :mod:`config_applier.safety` — pure decision functions: ceiling direction
  classification, vertical/content floor preservation, the live-writable
  allow-list (rebuild-class fields are rejected on the live path), and the
  monotonic config-epoch counter.
* :mod:`config_applier.applier` — orchestration: pull → validate → safety →
  atomic write → audit, returning a structured :class:`ApplyResult`.
"""

from config_applier.applier import (
    ApplyOutcome,
    ApplyResult,
    ConfigApplyError,
    apply,
    atomic_write,
    pull_config,
)
from config_applier.safety import (
    CEILING_ORDER,
    Direction,
    classify_direction,
    floor_preserving,
    live_writable,
    next_epoch,
)

__all__ = [
    "CEILING_ORDER",
    "ApplyOutcome",
    "ApplyResult",
    "ConfigApplyError",
    "Direction",
    "apply",
    "atomic_write",
    "classify_direction",
    "floor_preserving",
    "live_writable",
    "next_epoch",
    "pull_config",
]
