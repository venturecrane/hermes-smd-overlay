"""Spec-read observer (ss ADR 0083, ss-console #2084).

The PRODUCER half of ``shared.spec_status``. Called from the trust plugin's
EXISTING ``pre_tool_call`` — no new hook, and no change to
``contracts/overlay-hook-surface.json``. When the agent is about to read a file
that the ROOT-OWNED manifest names as an installed spec, and the bytes on disk
still hash to what root recorded, the read is marked for this turn.

OBSERVE, NEVER BLOCK. ``read_file`` is READ-class: enforcement always allows it,
and this module must not change that. It returns nothing and swallows every
error. The only thing that can refuse anything on account of a spec is the gate
at the send site, and it refuses a SEND, never a read.

WHY VERIFICATION LIVES HERE AND NOT AT THE STAMP. The stamp in
``<profile>/skills/*/SKILL.md`` is hermes-owned and therefore forgeable by the
agent that reads it. Marking on the strength of a path that merely looks like a
spec path would let the agent satisfy its own gate by reading a file it wrote.
The manifest is root-owned, the hash is recomputed from disk at read time, and a
file the manifest does not name marks nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from shared import spec_manifest
from shared.spec_status import SPEC_STATUS

logger = logging.getLogger(__name__)

#: Tool names whose argument names a local file the agent is about to read.
#: Deliberately narrow — one tool. A wider net would invite marking on a tool
#: whose "path" argument means something else.
_READ_TOOLS = frozenset({"read_file"})

#: Argument keys that can carry the path, across Hermes versions and MCP
#: shims. Being permissive on the KEY is safe: nothing is marked until the
#: manifest has resolved and verified the resulting path.
_PATH_KEYS = ("path", "file_path", "filename", "file")


def observe_read(tool_name: str, args: dict[str, Any] | None, session_id: str) -> None:
    """Mark a verified read of an installed spec. Never raises, never blocks."""
    try:
        if tool_name not in _READ_TOOLS or not session_id:
            return
        path = _extract_path(args)
        if not path:
            return
        entry = spec_manifest.entry_for_path(path)
        if entry is None:
            return  # not under the spec dir, or not a manifest-named spec
        if not spec_manifest.verify(entry):
            logger.warning(
                "spec_read: %s is named by the manifest but its bytes no longer match "
                "the recorded digest; NOT marking the read (the gate stays closed)",
                entry.rel_path,
            )
            return
        SPEC_STATUS.mark_read(session_id, entry.output_class, entry.prop)
        logger.debug(
            "spec_read: marked %s/%s read for session %s",
            entry.output_class,
            entry.prop,
            session_id,
        )
    except Exception:  # noqa: BLE001 — observation must never perturb the tool path
        logger.debug("spec_read: observation failed", exc_info=True)


def _extract_path(args: dict[str, Any] | None) -> str:
    if not isinstance(args, dict):
        return ""
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


__all__ = ["observe_read"]
