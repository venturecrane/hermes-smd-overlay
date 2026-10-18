"""The fixed slot jobs that run a couple's conversation-set routines (ss-console #3264).

On a couples seat (``seat.role: couples`` with ``people:`` authored) the boot
materializer (``bootstrap/cron_materialize.py``) registers ONE job per
(schedulable skill, space): ``op-managed:<persona>:<skill>@<space>``, every
quarter hour, gated by a pre-run script. The routines themselves (which days,
what time, paused or not) are conversation-set state in
``shared.couples_settings`` and are decided per tick by ``shared.couples_gate``.

Why fixed slots rather than one cron job per routine:

* **Nothing for a boot to wipe.** The materializer owns every ``op-managed:``
  job and recreates the authored set on every boot. A routine that lived only
  in the cron store would have to survive that reconcile; a slot IS the
  authored set, so the reconcile and the conversation never touch the same
  thing. The routine survives a reprovision because it lives on the volume.
* **A change needs no cron write.** The tool runs inside the gateway, where
  rewriting the cron store would mean pointing the process-global
  ``HERMES_HOME`` at a profile and reloading Hermes' ``cron.jobs`` under a live
  scheduler. Writing the settings document is enough: the next tick reads it.
* **Delivery stays structural.** The slot's name carries its space, the space
  resolver and the email delivery read it from there (``shared.cron_space``),
  exactly as for an authored routine. Nothing the couple says names an address.

Persons' routines run in ``person:<id>`` slots, the couple's shared ones in the
``shared`` slot (``couples_settings.KINDS``). A skill gets slots only when the
persona enables it with ``initiation.scheduled`` (ADR 0056), the same rule
every cron entry meets.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from shared import couples_gate
from shared import couples_settings as cs
from shared import space as space_mod

COUPLES_ROLE = "couples"
SLOT_SCHEDULE = f"*/{couples_gate.SLOT_MINUTES} * * * *"
#: Under ``<profile>/scripts/``: Hermes runs only scripts inside it. Dot-prefixed
#: so the boot refresh of skill-body scripts (``_refresh_staged_scripts``),
#: which owns ``scripts/<skill>/``, leaves it alone.
GATE_DIR = ".smd-couples"
GATE_LIB = "couples_gate.py"


def is_couples_seat(customer: dict[str, Any]) -> bool:
    seat = customer.get("seat")
    role = seat.get("role") if isinstance(seat, dict) else None
    return (
        isinstance(role, str)
        and role.strip() == COUPLES_ROLE
        and customer.get("people") is not None
    )


def slot_specs(
    customer: dict[str, Any], persona: dict[str, Any], cfg: space_mod.SpaceConfig | None
) -> list[tuple[str, str]]:
    """``[(skill, space_key)]`` for this persona's slots; ``[]`` off a couples seat."""
    if cfg is None or not is_couples_seat(customer):
        return []
    kinds = cs.available_kinds({"personas": [persona]})
    out: list[tuple[str, str]] = []
    for skill in cs.KINDS:
        if skill not in kinds:
            continue
        if cs.KINDS[skill] == "shared":
            out.append((skill, space_mod.SHARED))
        else:
            out.extend((skill, space_mod.person_key(p.id)) for p in cfg.people)
    return out


def stub_name(skill: str, space_key: str) -> str:
    return f"{skill}@{space_key.replace(':', '-')}.py"


def gate_stub(state_path: str, skill: str, space_key: str, people: list[str]) -> str:
    """The per-slot pre-run script: the slot's identity, baked in at boot from
    authored values, handed to the shared gate."""
    return (
        "# Generated at boot by hermes-smd (shared.couples_slots). Do not edit.\n"
        "import os\nimport sys\n\n"
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
        "import couples_gate  # noqa: E402\n\n"
        f"couples_gate.main({state_path!r}, {skill!r}, {space_key!r}, {list(people)!r})\n"
    )


def _write(target: Path, content: bytes) -> None:
    if target.is_file() and target.read_bytes() == content:
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def stage_gate(profile_dir: Path, skill: str, space_key: str, people: list[str]) -> str:
    """Stage the gate library and this slot's stub under the profile's scripts
    dir. Returns the ref Hermes resolves under ``scripts/``."""
    scripts = profile_dir / "scripts" / GATE_DIR
    _write(scripts / GATE_LIB, Path(couples_gate.__file__).read_bytes())
    name = stub_name(skill, space_key)
    stub = gate_stub(str(cs.settings_path()), skill, space_key, people)
    _write(scripts / name, stub.encode("utf-8"))
    return f"{GATE_DIR}/{name}"


__all__ = [
    "COUPLES_ROLE",
    "GATE_DIR",
    "SLOT_SCHEDULE",
    "gate_stub",
    "is_couples_seat",
    "slot_specs",
    "stage_gate",
    "stub_name",
]
