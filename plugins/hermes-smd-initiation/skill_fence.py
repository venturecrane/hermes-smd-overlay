"""The omitted-skill read fence: a skill this seat does not enable cannot be run
by reading it.

THE INCIDENT (2026-10-06). A seat's drafting lane is fail-closed by OMISSION:
the firm's customer.yaml does not list the drafting skills, so the seat "does
not carry them". But the image ships every skill under ``/app/skills/``, and the
router told the model to ``read_file`` a drafting skill's SKILL.md and carry it
out. It did: an omitted skill ran inline in a gateway turn, because omission
removed the skill from the index and nothing from the disk.

THE FENCE. A ``pre_tool_call`` block on every tool that reads a file the model
names: ``read_file``, ``search_files``, ``skill_view`` and ``terminal`` (and
``execute_code``, whose source can read too). Any path that RESOLVES (symlinks
and ``..`` collapsed) into a skills root (``/app/skills``, ``/opt/data/skills``)
must name a skill the LIVE customer.yaml enables; a skill root itself (a search
across every skill) is refused too. The refusal is the router's own sentence:
"could not load" the skill, said plainly, never approximated.

HARDENED after review (2026-10-06): a path a skills root sits UNDER (``/``,
``/app``, ``/opt/data``, ``.`` from there) is a search of every skill and is
refused; ``~`` is expanded and a relative path resolves against the tool's own
working directory; a ``workdir`` in or above a root counts; ``/./`` and ``//``
are collapsed; a shell or code cell that names ``skills`` with a glob, a find,
or a root and no readable slug is refused; ``skill_view`` takes a bare slug.
A regex over shell text cannot be complete (string concatenation defeats it),
so this is DEFENSE IN DEPTH: the structural fix is the boot reconciler in
ss-console's bootstrap.sh, which removes omitted skills from the volume.

FAIL-CLOSED. An unreadable customer.yaml enables nothing, so every skills read
is refused until it reads again; a hook fault refuses. Reads outside the skills
roots are untouched.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from typing import Any

from shared.customer_config import CustomerConfig

logger = logging.getLogger(__name__)

SKILL_ROOTS: tuple[str, ...] = ("/app/skills", "/opt/data/skills")
FENCED_TOOLS = frozenset({"read_file", "search_files", "skill_view", "terminal", "execute_code"})
_PATH_KEYS = ("path", "file_path", "filename", "directory", "dir", "target", "pattern", "glob")
_WORKDIR_KEYS = ("workdir", "cwd", "working_directory")
_COMMAND_KEYS = ("command", "cmd", "code", "script")
_CODE_TOOLS = frozenset({"terminal", "execute_code"})
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
#: Any mention of a skills root inside a command or a search pattern.
_ROOT_IN_TEXT = re.compile(r"(?:/app|/opt/data)/+skills(?:/+([^/\s'\"`;|&)]*))?")
_RELATIVE_IN_COMMAND = re.compile(r"(?:^|[\s'\"=:(])(?:\./)?skills/+([a-z0-9][a-z0-9-]{0,63})")
#: A command that could reach a skills root without spelling it: a root's
#: parent, a glob, a find, or the bare word next to a path separator.
_ROOT_PARENT_IN_TEXT = re.compile(
    r"(?:^|[\s'\"=:(])(?:/|/app/?|/opt/?|/opt/data/?|~/?|\.\.?/?)(?=$|[\s'\"*;|&)])"
)
_GLOB = re.compile(r"[*?\[]")


def refusal(slug: str) -> str:
    if not slug:
        return (
            "could not read there: that path includes the skills directory, and only the skills "
            "this seat enables can be loaded. Read or search a narrower path; if a skill was "
            "wanted, say plainly that it could not be loaded, and never approximate its output."
        )
    return (
        f"could not load the {slug} skill: it is not enabled on this seat, so its procedure "
        "cannot be run here. Say so plainly to the person who asked, and never approximate its output."
    )


def enabled_skills(config: CustomerConfig | None) -> frozenset[str]:
    """Every skill the live config lists and does not disable. None: nothing."""
    if config is None:
        return frozenset()
    names: set[str] = set()
    for persona in config.personas:
        skills = persona.get("skills") if isinstance(persona, dict) else None
        for skill in skills if isinstance(skills, list) else []:
            if (
                isinstance(skill, dict)
                and skill.get("enabled") is not False
                and isinstance(skill.get("name"), str)
            ):
                names.add(skill["name"].strip())
    return frozenset(names)


def _resolve(path: str, base: str) -> str:
    expanded = os.path.expanduser(path)
    return os.path.realpath(expanded if os.path.isabs(expanded) else os.path.join(base, expanded))


def _slug_under_root(path: str, base: str | None = None) -> str | None:
    """The skill a path resolves into; ``""`` for a skills root itself OR any
    directory a skills root sits under (a search of ``/``, ``/app``, ``.``);
    None when the path is outside every skills root and above none."""
    resolved = _resolve(path, base or os.getcwd())
    for root in SKILL_ROOTS:
        for candidate_root in {root, os.path.realpath(root)}:
            if resolved == candidate_root:
                return ""
            if resolved.startswith(candidate_root + os.sep):
                return resolved[len(candidate_root) + 1 :].split(os.sep, 1)[0]
            if candidate_root.startswith(resolved.rstrip(os.sep) + os.sep):
                return ""
    return None


def _normalize(text: str) -> str:
    """Collapse the spellings a regex would otherwise miss (``/./``, ``//``)."""
    previous = None
    while previous != text:
        previous = text
        text = text.replace("/./", "/").replace("//", "/")
    return text


def _slugs_in_text(text: str) -> Iterable[str]:
    for m in _ROOT_IN_TEXT.finditer(_normalize(text)):
        tail = (m.group(1) or "").strip()
        yield tail if _SLUG.match(tail) else ""


def _slugs_in_code(text: str, workdir_hit: bool) -> list[str]:
    """What a shell command or a code cell would read. Heuristic by nature
    (concatenation defeats any regex), so it leans to refusal: a command that
    names ``skills`` at all and also a root, a root's parent, a glob, or a
    workdir in or above a root is treated as reading the skills root unless
    every slug it names can be read off it."""
    norm = _normalize(text)
    out = list(_slugs_in_text(norm))
    out.extend(m.group(1) for m in _RELATIVE_IN_COMMAND.finditer(norm))
    if "skills" in norm and (
        workdir_hit
        or _ROOT_PARENT_IN_TEXT.search(norm)
        or _GLOB.search(norm)
        or "find " in norm
        or "/app" in norm
        or "/opt/data" in norm
    ):
        named = [m.group(1) for m in _RELATIVE_IN_COMMAND.finditer(norm)] + [
            s for s in _slugs_in_text(norm) if s
        ]
        if not named or _GLOB.search(norm) or "find " in norm:
            out.append("")
    return out


def touched_skills(tool_name: str, args: Any) -> list[str]:
    """Every skill slug this call would read (``""`` = a skills root)."""
    if not isinstance(args, dict):
        return []
    out: list[str] = []
    if tool_name == "skill_view":
        for key in ("name", "skill", "slug"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                # A bare slug only: "x/../y", "../y" or a path is refused.
                out.append(name if _SLUG.match(name) else "")
    base = os.getcwd()
    workdir_hit = False
    for key in _WORKDIR_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            base = _resolve(value.strip(), os.getcwd())
            slug = _slug_under_root(value.strip())
            if slug is not None:
                workdir_hit = True
                if slug:
                    out.append(slug)
    if tool_name == "search_files" and not any(
        isinstance(args.get(k), str) and args.get(k, "").strip()
        for k in ("path", "directory", "dir", "target")
    ):
        # A search with no directory searches the working directory.
        slug = _slug_under_root(base)
        if slug is not None:
            out.append(slug)
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            slug = _slug_under_root(value.strip(), base)
            if slug is not None:
                out.append(slug)
            out.extend(_slugs_in_text(value))
    if tool_name in _CODE_TOOLS:
        for key in _COMMAND_KEYS:
            value = args.get(key)
            if isinstance(value, str):
                out.extend(_slugs_in_code(value, workdir_hit))
    return out


def check(tool_name: str, args: Any, enabled: frozenset[str]) -> str | None:
    """The refusal sentence, or None when the call may proceed."""
    if tool_name not in FENCED_TOOLS:
        return None
    for slug in touched_skills(tool_name, args):
        if slug not in enabled or not slug:
            return refusal(slug)
    return None


def on_pre_tool_call(**kwargs: Any) -> dict[str, Any] | None:
    tool_name = str(kwargs.get("tool_name") or "")
    if tool_name not in FENCED_TOOLS:
        return None
    try:
        args = kwargs.get("args")
        if not touched_skills(tool_name, args):
            return None
        try:
            config: CustomerConfig | None = CustomerConfig.from_volume()
        except Exception:  # noqa: BLE001 - an unreadable config enables nothing
            config = None
        message = check(tool_name, args, enabled_skills(config))
    except Exception:  # noqa: BLE001 - a fence that cannot decide refuses
        logger.exception(
            "hermes-smd-initiation: skill fence could not decide; refusing %s", tool_name
        )
        return {"action": "block", "message": refusal("")}
    if message is None:
        return None
    logger.info("hermes-smd-initiation: refused %s of a skill this seat does not enable", tool_name)
    return {"action": "block", "message": message}


__all__ = [
    "FENCED_TOOLS",
    "SKILL_ROOTS",
    "check",
    "enabled_skills",
    "on_pre_tool_call",
    "refusal",
    "touched_skills",
]
