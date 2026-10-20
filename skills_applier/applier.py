"""Root install of a skills release into ``/var/lib/smd-skills/<version>/``.

    pointer → manifest (sha-verified) → objects (sha + size verified, capped)
            → staged root-owned → capability check → VERSION → rename → repoint

INSTALLED LAYOUT::

    /var/lib/smd-skills/
      current -> <version>          (symlink; the commit point, swapped atomically)
      <version>/
        skills/<skill>/...          (0755 dirs, 0644 or 0755 files, root-owned)
        manifest.json               (the verified manifest bytes)
        retired.txt                 (manifest.retired, one name per line)
        REFUSED.json                (skills held at their previous version, and why)
        VERSION                     (written last inside the version dir)

FAIL-STATIC. A pointer, manifest, or object that does not verify installs
NOTHING: the staging dir is removed and ``current`` still names what it named.
A seat keeps the skills it had; a bad publish costs it the update, never its
skills.

THE CAPABILITY GATE. Each skill may declare what it needs from the image
(``needs``). A skill whose needs this image cannot meet is not installed at the
new version: its previous files are carried into the new version dir instead
(from ``current``, else from the image's baked ``/app/skills``) and it is named
in ``REFUSED.json``. The rest of the release goes ahead, so one skill written
ahead of the image never holds back every other skill.

WHY ROOT. The install tree must not be writable by the hermes uid, for the same
reason ``spec_applier``'s is not: a skill body is read into the agent's prompt,
and an agent-writable copy of it would be a self-authored injection channel
that survives restarts. The copy onto the agent's volume is a separate step,
run as hermes (:mod:`skills_applier.sync`).
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.ids import iso_utc
from skills_applier.manifest import (
    MAX_MANIFEST_BYTES,
    MAX_POINTER_BYTES,
    MODE_EXEC,
    MODE_FILE,
    Manifest,
    ManifestError,
    Needs,
    object_key,
    parse_manifest,
    parse_pointer,
    pointer_key,
    valid_version,
)

logger = logging.getLogger(__name__)

DEFAULT_ROOT = "/var/lib/smd-skills"
DEFAULT_IMAGE_SKILLS = "/app/skills"
DEFAULT_CAPABILITIES = "/app/image-capabilities.json"

CURRENT = "current"
SKILLS_SUBDIR = "skills"
VERSION_FILE = "VERSION"
MANIFEST_FILE = "manifest.json"
RETIRED_FILE = "retired.txt"
REFUSED_FILE = "REFUSED.json"

_DIR_MODE = 0o755
_CHUNK = 1024 * 1024


class SkillsApplyError(RuntimeError):
    """An R2 or disk fault. The installed tree is untouched; retry next tick."""


class PointerMissing(SkillsApplyError):
    """No pointer for this seat: the ordinary state before its first release."""


class Outcome(str, enum.Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class SkillsApplyResult:
    outcome: Outcome
    version: str | None = None
    previous: str | None = None
    reasons: tuple[str, ...] = ()
    refused: dict[str, Any] = field(default_factory=dict)
    unchecked: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()
    path: Path | None = None


# ---------------------------------------------------------------------------
# R2
# ---------------------------------------------------------------------------


def _is_missing(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict) and str(error.get("Code", "")) in {
            "NoSuchKey",
            "404",
            "NotFound",
        }:
            return True
    return type(exc).__name__ in {"NoSuchKey", "NoSuchKeyError"}


def fetch(s3_client: Any, bucket: str, key: str, cap: int) -> bytes:
    """GET ``key``, refusing a body over ``cap`` bytes without reading past it."""
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 — classified, then wrapped
        if _is_missing(exc):
            raise PointerMissing(f"no object at s3://{bucket}/{key}") from exc
        raise SkillsApplyError(f"could not read s3://{bucket}/{key}: {exc}") from exc
    body = response.get("Body") if isinstance(response, dict) else getattr(response, "Body", None)
    if body is None:
        raise SkillsApplyError(f"response for {key} has no Body")
    try:
        data = body.read(cap + 1)
    except Exception as exc:  # noqa: BLE001
        raise SkillsApplyError(f"reading {key} failed: {exc}") from exc
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)):
        raise SkillsApplyError(f"body of {key} is not bytes")
    if len(data) > cap:
        raise ManifestError([f"{key} exceeds its {cap}-byte cap"])
    return bytes(data)


# ---------------------------------------------------------------------------
# Disk helpers (no symlink is ever followed)
# ---------------------------------------------------------------------------


def _harden(path: Path, mode: int) -> None:
    try:
        os.chown(path, 0, 0, follow_symlinks=False)
    except OSError as exc:
        logger.debug("skills_applier: could not chown %s to root (%s)", path, exc)
    os.chmod(path, mode)


def _mkdirs(base: Path, rel_dir: str) -> Path:
    current = base
    for part in [p for p in rel_dir.split("/") if p]:
        current = current / part
        try:
            os.mkdir(current, _DIR_MODE)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                raise OSError(f"{current} exists and is not a directory") from None
        _harden(current, _DIR_MODE)
    return current


def _write_new(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _harden(path, mode)


def _read_nofollow(path: Path, limit: int) -> bytes | None:
    """Bytes of a regular file, or None (absent, a symlink, not a file, too big)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(limit + 1)
    finally:
        if fd != -1:
            os.close(fd)
    return data if len(data) <= limit else None


def read_current_version(root: Path) -> str | None:
    data = _read_nofollow(root / CURRENT / VERSION_FILE, 1024)
    if data is None:
        return None
    text = data.decode("utf-8", errors="replace").strip()
    return text if valid_version(text) else None


def _current_target(root: Path) -> str | None:
    try:
        target = os.readlink(root / CURRENT)
    except OSError:
        return None
    return target if valid_version(target) else None


def _remove(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------


def load_capabilities(path: str | Path) -> dict[str, set[str]] | None:
    """The image capability file, or None when this image ships none.

    ``action_classes`` may be a list or a map (class -> anything); membership is
    by name either way.
    """
    data = _read_nofollow(Path(path), 8 * 1024 * 1024)
    if data is None:
        return None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("skills_applier: %s is not valid JSON; treating as absent", path)
        return None
    if not isinstance(doc, dict):
        return None
    caps: dict[str, set[str]] = {}
    for kind in ("tools", "verbs", "action_classes"):
        raw = doc.get(kind)
        if isinstance(raw, dict):
            caps[kind] = {str(k) for k in raw}
        elif isinstance(raw, list):
            caps[kind] = {str(v) for v in raw}
        else:
            caps[kind] = set()
    return caps


def check_needs(
    needs: Needs,
    capabilities: dict[str, set[str]] | None,
    path_exists: Callable[[str], bool] = os.path.exists,
) -> tuple[dict[str, list[str]], list[str]]:
    """``(missing, unchecked)``: what this image lacks, and what it cannot judge.

    Paths are always checked against the running image. Without a capability
    file the other three kinds cannot be judged; they are recorded as
    unchecked rather than assumed present or absent.
    """
    missing: dict[str, list[str]] = {}
    gone = [p for p in needs.paths if not path_exists(p)]
    if gone:
        missing["paths"] = gone
    unchecked: list[str] = []
    for kind in ("tools", "verbs", "action_classes"):
        wanted = getattr(needs, kind)
        if not wanted:
            continue
        if capabilities is None:
            unchecked.extend(f"{kind}:{name}" for name in wanted)
            continue
        lacking = [name for name in wanted if name not in capabilities.get(kind, set())]
        if lacking:
            missing[kind] = lacking
    return missing, unchecked


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local_body(candidates: list[Path], rel: str, sha: str, size: int) -> bytes | None:
    """A body already on this box with the right digest (skips the download)."""
    for base in candidates:
        data = _read_nofollow(base / rel, size)
        if data is not None and len(data) == size and _sha256(data) == sha:
            return data
    return None


def _copy_tree_nofollow(src: Path, dst: Path) -> None:
    """Copy a skill dir from a previous install, refusing any symlink in it."""
    st = os.lstat(src)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{src} is not a directory")
    os.mkdir(dst, _DIR_MODE)
    _harden(dst, _DIR_MODE)
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        child = Path(entry.path)
        est = os.lstat(child)
        if stat.S_ISLNK(est.st_mode):
            raise OSError(f"refusing symlink {child} in a previous skill copy")
        if stat.S_ISDIR(est.st_mode):
            _copy_tree_nofollow(child, dst / entry.name)
        elif stat.S_ISREG(est.st_mode):
            data = _read_nofollow(child, 64 * 1024 * 1024)
            if data is None:
                raise OSError(f"could not read {child}")
            mode = MODE_EXEC if est.st_mode & 0o111 else MODE_FILE
            _write_new(dst / entry.name, data, mode)


def _download_all(
    s3_client: Any,
    bucket: str,
    manifest: Manifest,
    skills_dir: Path,
    candidates: list[Path],
) -> list[str]:
    """Lay down every file; return integrity failures (non-empty = refuse all)."""
    failures: list[str] = []
    fetched: dict[str, bytes] = {}
    for rel, entry in sorted(manifest.files.items()):
        data = fetched.get(entry.sha256) or _local_body(candidates, rel, entry.sha256, entry.size)
        if data is None:
            try:
                data = fetch(s3_client, bucket, object_key(entry.sha256), entry.size)
            except PointerMissing:
                failures.append(f"{rel}: object {entry.sha256} is missing")
                continue
            except ManifestError:
                failures.append(f"{rel}: object is larger than its declared {entry.size} bytes")
                continue
        if len(data) != entry.size:
            failures.append(f"{rel}: {len(data)} bytes, manifest says {entry.size}")
            continue
        if _sha256(data) != entry.sha256:
            failures.append(f"{rel}: content does not match its sha256")
            continue
        fetched[entry.sha256] = data
        parent, _, name = rel.rpartition("/")
        _write_new(_mkdirs(skills_dir, parent) / name, data, entry.mode)
    return failures


def _hold_back(
    manifest: Manifest,
    skills_dir: Path,
    previous_dirs: list[Path],
    capabilities: dict[str, set[str]] | None,
    path_exists: Callable[[str], bool],
) -> tuple[dict[str, Any], list[str]]:
    """Apply the capability gate; replace each refused skill with its previous copy."""
    refused: dict[str, Any] = {}
    unchecked_all: list[str] = []
    for skill, needs in sorted(manifest.needs.items()):
        missing, unchecked = check_needs(needs, capabilities, path_exists)
        unchecked_all.extend(f"{skill}:{item}" for item in unchecked)
        if not missing:
            continue
        shutil.rmtree(skills_dir / skill)
        kept_from = None
        for base in previous_dirs:
            if os.path.lexists(base / skill):
                _copy_tree_nofollow(base / skill, skills_dir / skill)
                kept_from = str(base)
                break
        refused[skill] = {"missing": missing, "kept_previous_from": kept_from}
        logger.warning(
            "skills_applier: %s needs what this image lacks (%s); kept %s",
            skill,
            missing,
            kept_from or "no previous copy",
        )
    return refused, unchecked_all


def stage(
    *,
    s3_client: Any,
    bucket: str,
    slug: str,
    root: str | Path = DEFAULT_ROOT,
    image_skills: str | Path = DEFAULT_IMAGE_SKILLS,
    capabilities_path: str | Path = DEFAULT_CAPABILITIES,
    path_exists: Callable[[str], bool] = os.path.exists,
) -> SkillsApplyResult:
    """Verify and install the pointed-at release into ``root/<version>``.

    Does NOT repoint ``current`` — call :func:`commit` for that, after the
    volume copy has succeeded. Returns UNCHANGED when ``current`` already names
    the pointed-at version, REJECTED (nothing installed) when anything fails to
    verify, APPLIED with ``path`` set when the version dir is complete.
    """
    root = Path(root)
    current = read_current_version(root)
    try:
        pointer = parse_pointer(fetch(s3_client, bucket, pointer_key(slug), MAX_POINTER_BYTES))
    except ManifestError as exc:
        return _rejected(None, current, list(exc.reasons))
    if current == pointer.version:
        return SkillsApplyResult(Outcome.UNCHANGED, version=current, previous=current)
    try:
        raw = fetch(s3_client, bucket, pointer.manifest_key, MAX_MANIFEST_BYTES)
    except PointerMissing:
        return _rejected(pointer.version, current, [f"manifest {pointer.manifest_key} is missing"])
    except ManifestError as exc:
        return _rejected(pointer.version, current, list(exc.reasons))
    if _sha256(raw) != pointer.manifest_sha256:
        return _rejected(
            pointer.version, current, ["manifest bytes do not match pointer.manifest_sha256"]
        )
    try:
        manifest = parse_manifest(raw, expected_version=pointer.version)
    except ManifestError as exc:
        return _rejected(pointer.version, current, list(exc.reasons))

    root.mkdir(parents=True, exist_ok=True)
    _harden(root, _DIR_MODE)
    staging = root / f".staging-{pointer.version}-{secrets.token_hex(4)}"
    final = root / pointer.version
    current_skills = root / CURRENT / SKILLS_SUBDIR
    candidates = [final / SKILLS_SUBDIR, current_skills, Path(image_skills)]
    try:
        os.mkdir(staging, _DIR_MODE)
        _harden(staging, _DIR_MODE)
        skills_dir = _mkdirs(staging, SKILLS_SUBDIR)
        failures = _download_all(s3_client, bucket, manifest, skills_dir, candidates)
        if failures:
            _remove(staging)
            return _rejected(pointer.version, current, failures)
        refused, unchecked = _hold_back(
            manifest,
            skills_dir,
            [current_skills, Path(image_skills)],
            load_capabilities(capabilities_path),
            path_exists,
        )
        _write_new(staging / MANIFEST_FILE, raw, MODE_FILE)
        _write_new(
            staging / RETIRED_FILE,
            "".join(f"{name}\n" for name in manifest.retired).encode(),
            MODE_FILE,
        )
        report = {
            "version": pointer.version,
            "installed_at": iso_utc(),
            "refused": refused,
            "unchecked": unchecked,
        }
        _write_new(
            staging / REFUSED_FILE,
            json.dumps(report, indent=2, sort_keys=True).encode() + b"\n",
            MODE_FILE,
        )
        # VERSION last: a version dir without it is incomplete by definition.
        _write_new(staging / VERSION_FILE, f"{pointer.version}\n".encode(), MODE_FILE)
        if os.path.lexists(final):
            aside = root / f".old-{pointer.version}-{secrets.token_hex(4)}"
            os.rename(final, aside)
            _remove(aside)
        os.rename(staging, final)
    except OSError as exc:
        try:
            _remove(staging)
        except OSError:
            logger.warning("skills_applier: could not remove staging dir %s", staging)
        raise SkillsApplyError(f"installing {pointer.version} failed: {exc}") from exc
    return SkillsApplyResult(
        Outcome.APPLIED,
        version=pointer.version,
        previous=current,
        refused=refused,
        unchecked=tuple(unchecked),
        retired=manifest.retired,
        path=final,
    )


def _rejected(version: str | None, previous: str | None, reasons: list[str]) -> SkillsApplyResult:
    return SkillsApplyResult(
        Outcome.REJECTED, version=version, previous=previous, reasons=tuple(reasons)
    )


# ---------------------------------------------------------------------------
# Commit + retention
# ---------------------------------------------------------------------------


def commit(root: str | Path, version: str) -> None:
    """Atomically point ``root/current`` at ``version`` (the commit point)."""
    root = Path(root)
    if not valid_version(version) or read_version_file(root / version) != version:
        raise SkillsApplyError(f"refusing to commit {version!r}: version dir incomplete")
    link = root / CURRENT
    if os.path.lexists(link) and not os.path.islink(link):
        # A real directory where the link belongs (never written by this code):
        # move it aside so the swap below can land.
        os.rename(link, root / f".old-current-{secrets.token_hex(4)}")
    tmp = root / f".current-{secrets.token_hex(4)}"
    os.symlink(version, tmp)
    try:
        os.replace(tmp, link)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    try:
        fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        logger.debug("skills_applier: directory fsync of %s not supported", root)


def read_version_file(version_dir: Path) -> str | None:
    data = _read_nofollow(version_dir / VERSION_FILE, 1024)
    return data.decode("utf-8", errors="replace").strip() if data is not None else None


def prune(root: str | Path, keep: set[str]) -> list[str]:
    """Remove version dirs not in ``keep`` (plus leftover staging dirs).

    ``current``'s target is always kept, whatever the caller passed.
    """
    root = Path(root)
    target = _current_target(root)
    if target:
        keep = keep | {target}
    removed: list[str] = []
    if not root.is_dir():
        return removed
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        name = entry.name
        if name == CURRENT or name in keep:
            continue
        if not (name.startswith((".staging-", ".old-", ".current-")) or valid_version(name)):
            continue
        try:
            _remove(Path(entry.path))
            removed.append(name)
        except OSError as exc:
            logger.warning("skills_applier: could not prune %s: %s", entry.path, exc)
    return removed


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def emit_skills_write(*, audit_client: Any, slug: str, result: SkillsApplyResult) -> bool:
    """One CONFIG_WRITE row per committed skills release, best-effort.

    CONFIG_WRITE (not a new type) because it is already in both repos' accepted
    vocabularies; ``skills_apply`` in the metadata tells the two apart.
    """
    from shared.audit_contract import INSERT_SQL, agent_event_params

    metadata = {
        "skills_apply": True,
        "customer": slug,
        "version": result.version,
        "previous": result.previous,
        "refused": sorted(result.refused)[:64],
        "retired": list(result.retired[:64]),
    }
    params = agent_event_params(action_type="CONFIG_WRITE", metadata=metadata)
    # The root skills applier, not the agent (agent_event_params' default).
    params[3] = "skills-applier"
    params[4] = "operator"
    try:
        # Root poller: the generic append admits only the gateway (see
        # config_applier/applier.py _emit_config_write).
        write = getattr(audit_client, "execute_config_write", None) or audit_client.execute
        write(INSERT_SQL, *params)
        return True
    except Exception as exc:  # noqa: BLE001 — audit never undoes an install
        logger.warning("skills_applier: CONFIG_WRITE audit emission failed (%s)", exc)
        return False


__all__ = [
    "CURRENT",
    "DEFAULT_CAPABILITIES",
    "DEFAULT_IMAGE_SKILLS",
    "DEFAULT_ROOT",
    "REFUSED_FILE",
    "RETIRED_FILE",
    "SKILLS_SUBDIR",
    "VERSION_FILE",
    "Outcome",
    "PointerMissing",
    "SkillsApplyError",
    "SkillsApplyResult",
    "check_needs",
    "commit",
    "emit_skills_write",
    "fetch",
    "load_capabilities",
    "prune",
    "read_current_version",
    "stage",
]
