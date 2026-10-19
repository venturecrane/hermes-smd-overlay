"""Root-owned skills applier (ss seat fleet scaling).

The third root applier, beside :mod:`config_applier` and :mod:`spec_applier`.
It delivers SKILLS as a release artifact, so a skills-only change reaches a
seat through the rollout waves with no image build and no restart.

* :mod:`skills_applier.manifest` — the pointer/manifest contract and its caps.
* :mod:`skills_applier.applier` — root: verify all-or-nothing into
  ``/var/lib/smd-skills/<version>/``, the capability gate, the atomic
  ``current`` swap, retention, the audit row.
* :mod:`skills_applier.sync` — hermes: copy a catalog onto the volume with the
  boot's seed / retired / omitted-prune semantics, never following a symlink.
  bootstrap.sh calls the same module at boot.
* :mod:`skills_applier.__main__` — the poll loop and the boot ``--once``.
"""

from skills_applier.applier import (
    Outcome,
    PointerMissing,
    SkillsApplyError,
    SkillsApplyResult,
    commit,
    stage,
)
from skills_applier.manifest import Manifest, ManifestError, Pointer

__all__ = [
    "Manifest",
    "ManifestError",
    "Outcome",
    "Pointer",
    "PointerMissing",
    "SkillsApplyError",
    "SkillsApplyResult",
    "commit",
    "stage",
]
