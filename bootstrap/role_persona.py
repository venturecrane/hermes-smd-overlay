"""Load a hosted-agent role's persona prompt for SOUL.md (ss-console ADR 0090).

A seat selects a role with ``seat.role: <role>`` in its customer.yaml. The role
package lives in ss-console at ``operator/roles/<role>/`` and the image carries
it at ``/app/roles/<role>/`` (Dockerfile ``COPY operator/roles/ /app/roles/``),
the same image-baked delivery the skill library uses. The package's
``role.yaml`` names the persona file; its text is rendered VERBATIM into every
profile's SOUL.md, because the persona carries the role's safety rules and a
paraphrase of a safety rule is a different rule.

Fail closed. A seat that names a role whose package, manifest or persona file is
missing, malformed or empty raises :class:`RolePersonaError` and the boot stops:
a role seat running without its safety rules is worse than a seat that is down.
A seat with no ``seat.role`` never reads the roles dir, so its SOUL.md is
byte-identical to what it was before roles existed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ROLES_DIR = "/app/roles"

# A role slug and a persona filename are each a single path segment, so neither
# can climb out of the roles dir.
_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PERSONA_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RolePersonaError(RuntimeError):
    """The seat names a role whose persona cannot be loaded."""


def seat_role(customer: dict[str, Any]) -> str | None:
    """Return ``seat.role`` or ``None`` when the seat has no role."""
    seat = customer.get("seat")
    if not isinstance(seat, dict):
        return None
    role = seat.get("role")
    if role is None or role == "":
        return None
    if not isinstance(role, str) or not _ROLE_RE.match(role):
        raise RolePersonaError(f"seat.role {role!r} is not a role slug")
    return role


def resolve_roles_dir(roles_dir: str | None) -> Path:
    """The explicit dir, else ``$ROLES_DIR``, else the image's ``/app/roles``."""
    return Path(roles_dir or os.environ.get("ROLES_DIR") or DEFAULT_ROLES_DIR)


def load_role_persona(customer: dict[str, Any], roles_dir: str | None = None) -> str:
    """Return the persona text for ``seat.role``, or ``""`` for a seat with no role."""
    role = seat_role(customer)
    if role is None:
        return ""
    package = resolve_roles_dir(roles_dir) / role
    manifest_path = package / "role.yaml"
    if not manifest_path.is_file():
        raise RolePersonaError(
            f"seat.role is {role!r} but {manifest_path} does not exist; "
            "refusing to boot a role seat without its persona"
        )
    try:
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise RolePersonaError(f"{manifest_path} is not valid YAML: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("role") != role:
        raise RolePersonaError(f"{manifest_path} does not declare role: {role}")
    persona_file = manifest.get("persona")
    if not isinstance(persona_file, str) or not _PERSONA_FILE_RE.match(persona_file):
        raise RolePersonaError(
            f"{manifest_path} persona must be a bare filename, got {persona_file!r}"
        )
    persona_path = package / persona_file
    if not persona_path.is_file():
        raise RolePersonaError(
            f"seat.role is {role!r} but its persona {persona_path} does not exist; "
            "refusing to boot a role seat without its persona"
        )
    text = persona_path.read_text()
    if not text.strip():
        raise RolePersonaError(f"persona {persona_path} is empty")
    return text if text.endswith("\n") else text + "\n"
