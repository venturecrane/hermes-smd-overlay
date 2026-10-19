"""The skills release contract: pointer, manifest, and the limits on both.

Pure parsing. Nothing here touches R2 or the disk, so every refusal rule is
testable on bytes alone, and the installer can rely on a parsed
:class:`Manifest` naming only safe paths, well-formed digests and sizes inside
the caps.

R2 LAYOUT (written by ss-console CI ``publish-skills`` and ``fleet release
--skills``):

* ``vaults/<slug>/skills-pointer.json`` — the ONE object a seat polls::

      {"version": "<v>", "manifest_key": "fleet/skills/<v>.json",
       "manifest_sha256": "<hex>"}

* ``fleet/skills/<v>.json`` — the candidate manifest::

      {"version": "<v>", "source_sha": "<git sha>", "created_at": "<iso>",
       "files": {"<skill>/<relpath>": {"sha256": "<hex>", "size": <int>,
                                       "mode": "0644" | "0755"}},
       "retired": ["<skill>", ...],
       "needs": {"<skill>": {"paths": [], "tools": [], "verbs": [],
                             "action_classes": []}}}

  ``mode`` is optional and defaults to ``0644``; only ``0644`` and ``0755``
  exist, because nothing in a skill needs more and an installer that accepted
  arbitrary modes would accept setuid.

* ``fleet/skills/objects/<sha256>`` — content-addressed file bodies.

ALL OR NOTHING. :func:`parse_manifest` raises with EVERY reason it found, and a
manifest with one bad entry is refused whole. Adopting the good half would
install a release nobody published.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

POINTER_KEY_TEMPLATE = "vaults/{slug}/skills-pointer.json"
MANIFEST_PREFIX = "fleet/skills/"
OBJECT_PREFIX = "fleet/skills/objects/"

#: Caps. A skill is prose plus small scripts; anything past these is a mistake
#: or an attempt to fill the root volume, and either way the release is refused.
MAX_POINTER_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 10_000

MODE_FILE = 0o644
MODE_EXEC = 0o755
_MODES = {"0644": MODE_FILE, "644": MODE_FILE, "0755": MODE_EXEC, "755": MODE_EXEC}

_VERSION = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
#: Same alphabet bootstrap.sh's retired-skill prune accepts.
_SKILL = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
#: No leading dot: no hidden files, no ``.``/``..``, and nothing that could
#: collide with the installer's own ``.staging-*`` names.
_SEGMENT = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_NEED_KINDS = ("paths", "tools", "verbs", "action_classes")


class ManifestError(ValueError):
    """A pointer or manifest that must not be adopted. ``reasons`` lists every
    problem found, so one publish fault does not hide the next."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


@dataclass(frozen=True)
class Pointer:
    version: str
    manifest_key: str
    manifest_sha256: str


@dataclass(frozen=True)
class FileEntry:
    rel: str
    skill: str
    sha256: str
    size: int
    mode: int = MODE_FILE


@dataclass(frozen=True)
class Needs:
    paths: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    verbs: tuple[str, ...] = ()
    action_classes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    version: str
    source_sha: str | None
    created_at: str | None
    files: dict[str, FileEntry]
    retired: tuple[str, ...] = ()
    needs: dict[str, Needs] = field(default_factory=dict)

    @property
    def skills(self) -> tuple[str, ...]:
        return tuple(sorted({entry.skill for entry in self.files.values()}))


def pointer_key(slug: str) -> str:
    if not isinstance(slug, str) or not slug.strip() or "/" in slug:
        raise ManifestError(["customer slug is required and must be one path segment"])
    return POINTER_KEY_TEMPLATE.format(slug=slug.strip())


def object_key(sha: str) -> str:
    return f"{OBJECT_PREFIX}{sha}"


def valid_version(value: object) -> bool:
    return isinstance(value, str) and bool(_VERSION.match(value))


def valid_skill_name(value: object) -> bool:
    return isinstance(value, str) and bool(_SKILL.match(value))


def _load_object(data: bytes, what: str, cap: int) -> dict[str, Any]:
    if len(data) > cap:
        raise ManifestError([f"{what} is {len(data)} bytes, over the {cap}-byte cap"])
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError([f"{what} is not valid UTF-8 JSON: {exc}"]) from exc
    if not isinstance(doc, dict):
        raise ManifestError([f"{what} must be a JSON object"])
    return doc


def parse_pointer(data: bytes) -> Pointer:
    doc = _load_object(data, "skills pointer", MAX_POINTER_BYTES)
    reasons: list[str] = []
    version = doc.get("version")
    key = doc.get("manifest_key")
    sha = doc.get("manifest_sha256")
    if not valid_version(version):
        reasons.append(f"pointer.version {version!r} is not a valid version")
    if (
        not isinstance(key, str)
        or not key.startswith(MANIFEST_PREFIX)
        or not key.endswith(".json")
        or key.startswith(OBJECT_PREFIX)
        or ".." in key
        or "//" in key
    ):
        reasons.append(f"pointer.manifest_key {key!r} is not a {MANIFEST_PREFIX}<v>.json key")
    if not isinstance(sha, str) or not _SHA256.match(sha):
        reasons.append("pointer.manifest_sha256 must be 64 lowercase hex characters")
    if reasons:
        raise ManifestError(reasons)
    return Pointer(version=version, manifest_key=key, manifest_sha256=sha)


def _parse_rel(rel: object) -> tuple[str, str] | None:
    """``(skill, rel)`` for a safe ``<skill>/<relpath>`` key, else None."""
    if not isinstance(rel, str) or rel.startswith("/") or "\\" in rel:
        return None
    parts = rel.split("/")
    if len(parts) < 2 or not _SKILL.match(parts[0]):
        return None
    if not all(_SEGMENT.match(part) for part in parts[1:]):
        return None
    return parts[0], rel


def _parse_mode(raw: object) -> int | None:
    if raw is None:
        return MODE_FILE
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw in (MODE_FILE, MODE_EXEC) else None
    if isinstance(raw, str):
        return _MODES.get(raw.strip())
    return None


def _parse_files(raw: object, reasons: list[str]) -> dict[str, FileEntry]:
    if not isinstance(raw, dict) or not raw:
        reasons.append("manifest.files must be a non-empty object")
        return {}
    if len(raw) > MAX_FILES:
        reasons.append(f"manifest.files has {len(raw)} entries, over the {MAX_FILES} cap")
        return {}
    files: dict[str, FileEntry] = {}
    total = 0
    for key, entry in sorted(raw.items()):
        parsed = _parse_rel(key)
        if parsed is None:
            reasons.append(f"files[{key!r}]: not a safe <skill>/<relpath> path")
            continue
        if not isinstance(entry, dict):
            reasons.append(f"files[{key!r}]: must be an object")
            continue
        sha, size = entry.get("sha256"), entry.get("size")
        mode = _parse_mode(entry.get("mode"))
        if not isinstance(sha, str) or not _SHA256.match(sha):
            reasons.append(f"files[{key!r}].sha256 must be 64 lowercase hex characters")
            continue
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            reasons.append(f"files[{key!r}].size must be a non-negative integer")
            continue
        if size > MAX_FILE_BYTES:
            reasons.append(f"files[{key!r}] is {size} bytes, over the {MAX_FILE_BYTES} cap")
            continue
        if mode is None:
            reasons.append(f"files[{key!r}].mode must be 0644 or 0755")
            continue
        total += size
        files[key] = FileEntry(rel=key, skill=parsed[0], sha256=sha, size=size, mode=mode)
    if total > MAX_TOTAL_BYTES:
        reasons.append(f"manifest totals {total} bytes, over the {MAX_TOTAL_BYTES} cap")
    # A file key that is also a directory prefix of another key cannot be laid out.
    dirs = {key.rsplit("/", n)[0] for key in files for n in range(1, key.count("/") + 1)}
    for key in sorted(dirs & set(files)):
        reasons.append(f"files[{key!r}] is both a file and a directory")
    return files


def _parse_str_list(raw: object, where: str, reasons: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw):
        reasons.append(f"{where} must be a list of non-empty strings")
        return ()
    return tuple(v.strip() for v in raw)


def _parse_needs(raw: object, skills: set[str], reasons: list[str]) -> dict[str, Needs]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        reasons.append("manifest.needs must be an object")
        return {}
    needs: dict[str, Needs] = {}
    for skill, entry in sorted(raw.items()):
        if skill not in skills:
            reasons.append(f"needs[{skill!r}]: names a skill the manifest does not ship")
            continue
        if not isinstance(entry, dict):
            reasons.append(f"needs[{skill!r}] must be an object")
            continue
        values = {
            kind: _parse_str_list(entry.get(kind), f"needs[{skill!r}].{kind}", reasons)
            for kind in _NEED_KINDS
        }
        for path in values["paths"]:
            if not path.startswith("/"):
                reasons.append(f"needs[{skill!r}].paths: {path!r} is not absolute")
        needs[skill] = Needs(**values)
    return needs


def parse_manifest(data: bytes, *, expected_version: str) -> Manifest:
    doc = _load_object(data, "skills manifest", MAX_MANIFEST_BYTES)
    reasons: list[str] = []
    version = doc.get("version")
    if version != expected_version:
        reasons.append(
            f"manifest.version {version!r} does not match the pointer's {expected_version!r}"
        )
    files = _parse_files(doc.get("files"), reasons)
    skills = {entry.skill for entry in files.values()}
    retired = _parse_str_list(doc.get("retired"), "manifest.retired", reasons)
    for name in retired:
        if not _SKILL.match(name):
            reasons.append(f"manifest.retired: {name!r} is not a skill name")
        elif name in skills:
            reasons.append(f"manifest.retired: {name!r} is also shipped by this manifest")
    needs = _parse_needs(doc.get("needs"), skills, reasons)
    source_sha = doc.get("source_sha")
    created_at = doc.get("created_at")
    if reasons:
        raise ManifestError(reasons)
    return Manifest(
        version=expected_version,
        source_sha=source_sha if isinstance(source_sha, str) else None,
        created_at=created_at if isinstance(created_at, str) else None,
        files=files,
        retired=retired,
        needs=needs,
    )


__all__ = [
    "MANIFEST_PREFIX",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_POINTER_BYTES",
    "MAX_TOTAL_BYTES",
    "MODE_EXEC",
    "MODE_FILE",
    "OBJECT_PREFIX",
    "FileEntry",
    "Manifest",
    "ManifestError",
    "Needs",
    "Pointer",
    "object_key",
    "parse_manifest",
    "parse_pointer",
    "pointer_key",
    "valid_skill_name",
    "valid_version",
]
