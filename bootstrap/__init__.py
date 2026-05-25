"""hermes-smd bootstrap CLI — customer.yaml → per-profile Hermes config translation.

See ADR 0019. The ``hermes-smd`` console script (declared in pyproject.toml)
dispatches to ``bootstrap.cli:main``. Subcommands:

- ``bootstrap``     — one-shot translation at Machine boot
- ``customer-sync`` — long-running sidecar polling R2 for non-structural updates
"""
