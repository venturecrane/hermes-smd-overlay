"""Root-side establishment intake (ss ADR 0085, ss-console #2160/#2161/#2162).

The trust-boundary core of conversational establishment. The agent's
``establish_*`` tools (plugins/hermes-smd-establishment) reach the workspace
broker, which validates and writes broker-authored files into a spool the agent
uid cannot open. THIS package is the root daemon on the other side of that
spool: it verifies the files' provenance (uid + hashes), runs the distillation
compilers as subprocess write gates, and — only on pass — merge-writes the
customer's vault object (``vaults/<slug>/output-classes.json``), which the
fail-static ``spec_applier`` then installs root-owned.

Module map:

* ``gates``   — subprocess wrappers for the compilers + per-gate dispositions.
* ``intake``  — verify → gate-run → R2 previous-copy + merge-put → converge-wait
  → result write → purge. The spool contract with the broker (C0, ss-console)
  is documented in ``intake``'s module docstring.
* ``__main__`` — env wiring, the LOUD boot line + heartbeat file, poll loop.

Trust posture, restated where it is enforced: the agent never touches the
spool, R2, or the spec tree; the broker never holds R2 creds; root's input
surface is broker-authored files only, and every one is verified before use.
"""

from establish_intake.intake import EstablishIntake

__all__ = ["EstablishIntake"]
