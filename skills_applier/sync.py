"""Copy a skill catalog onto the agent's volume, as the hermes uid.

One module for both callers, so the boot seed and a live skills release cannot
drift apart:

* ss-console ``bootstrap.sh`` (already running as hermes) at every boot, with
  ``--source`` the committed release (``/var/lib/smd-skills/current/skills``)
  or, on a seat that has never had one, the image's ``/app/skills``.
* :mod:`skills_applier.__main__` (root) after it stages a release, through
  ``setpriv`` so this module never writes the volume as root.

SEMANTICS, replicated from bootstrap.sh step 6b (seed, retired prune,
omitted prune) so the final state is the same one the boot produced:

1. SEED. Every skill dir in ``--source`` replaces the volume entry of the same
   name. Agent-authored skills (on the volume only, ADR 0017) are never
   iterated. A seed failure is fatal (exit 1): a seat must not run on a
   half-seeded catalog. Each skill is staged beside its target and swapped in
   with renames, so a reader sees the old skill or the new one.
2. RETIRED PRUNE. Each name in a ``--retired-file`` (blank and ``#`` lines
   skipped, names outside ``[a-z0-9-]`` ignored with a warning, a name still in
   the source catalog skipped with a warning) is removed from the volume.
   Never fatal.
3. OMITTED PRUNE. A source skill no persona in ``--enabled-skills-from``
   (customer.yaml) enables is removed from the volume, and is not seeded in the
   first place. An unreadable config, or one that enables nothing, prunes
   nothing: an empty allowlist must never read as "remove every skill".

SYMLINKS ARE NEVER FOLLOWED. A symlink anywhere in the source refuses the whole
sync before anything is written. On the volume, a symlink where a skill or the
catalog root belongs is removed AS A LINK and replaced with a real directory;
nothing is ever written through one. Every file is created with
``O_NOFOLLOW | O_EXCL`` inside a staging dir this run just made.

``--refresh-profiles HERMES_HOME`` also brings each persona profile's own copy
of its skills (and the pre_run scripts staged from them) up to date, using the
same functions the boot translate uses. The boot does not pass it, because
``hermes-smd bootstrap`` runs translate right after.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import shutil
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("skills_applier.sync")

_STAGE_PREFIX = ".smd-sync-"


class SyncError(RuntimeError):
    """The sync could not complete; exit 1."""


@dataclass
class SyncResult:
    seeded: list[str] = field(default_factory=list)
    pruned_retired: list[str] = field(default_factory=list)
    pruned_omitted: list[str] = field(default_factory=list)
    profiles_refreshed: list[str] = field(default_factory=list)


def _is_link(path: Path) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _remove_entry(path: Path) -> bool:
    """Remove a volume entry without following it. True when something went."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def _scan_source(source: Path) -> list[str]:
    """Skill names in ``source``; refuses if a symlink appears anywhere in it."""
    try:
        st = os.lstat(source)
    except OSError as exc:
        raise SyncError(f"source {source} is not readable: {exc}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise SyncError(f"source {source} is not a real directory")
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        for name in dirnames + filenames:
            if _is_link(Path(dirpath) / name):
                raise SyncError(f"refusing symlink {Path(dirpath) / name} in the source catalog")
    return sorted(
        e.name
        for e in os.scandir(source)
        if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
    )


def _copy_tree(src: Path, dst: Path) -> None:
    os.mkdir(dst, 0o755)
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        child, target = Path(entry.path), dst / entry.name
        st = os.lstat(child)
        if stat.S_ISLNK(st.st_mode):
            raise SyncError(f"refusing symlink {child}")
        if stat.S_ISDIR(st.st_mode):
            _copy_tree(child, target)
        elif stat.S_ISREG(st.st_mode):
            mode = st.st_mode & 0o777
            fd_in = os.open(child, os.O_RDONLY | os.O_NOFOLLOW)
            fd_out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd_in, "rb") as fin, os.fdopen(fd_out, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            os.chmod(target, mode)
    os.chmod(dst, stat.S_IMODE(os.lstat(src).st_mode))


def _replace_skill(src: Path, dest_root: Path, name: str) -> None:
    staging = dest_root / f"{_STAGE_PREFIX}{name}-{secrets.token_hex(4)}"
    aside = dest_root / f"{_STAGE_PREFIX}old-{name}-{secrets.token_hex(4)}"
    target = dest_root / name
    try:
        _copy_tree(src, staging)
        if os.path.lexists(target):
            os.rename(target, aside)
        os.rename(staging, target)
    except (OSError, SyncError) as exc:
        _remove_entry(staging)
        raise SyncError(f"skill catalog seed failed for {name}: {exc}") from exc
    try:
        _remove_entry(aside)
    except OSError as exc:
        logger.warning("sync: could not remove replaced copy %s: %s", aside, exc)


def _prepare_dest(dest: Path) -> None:
    if _is_link(dest):
        dest.unlink()
        logger.info("sync: removed stale symlink at the catalog root %s", dest)
    dest.mkdir(parents=True, exist_ok=True)
    if _is_link(dest) or not dest.is_dir():
        raise SyncError(f"destination {dest} is not a real directory")
    # Leftovers from an interrupted run are this module's own and safe to drop.
    for entry in os.scandir(dest):
        if entry.name.startswith(_STAGE_PREFIX):
            _remove_entry(Path(entry.path))


def read_retired(paths: Iterable[str | Path]) -> list[str]:
    names: list[str] = []
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            logger.warning("sync: retired list %s absent; not pruned from it", path)
            continue
        for line in lines:
            name = "".join(line.split())
            if not name or name.startswith("#"):
                continue
            if not all(c.isascii() and (c.islower() or c.isdigit() or c == "-") for c in name):
                logger.warning("sync: retired list: ignoring malformed name %r", name)
                continue
            if name not in names:
                names.append(name)
    return names


def read_enabled(customer_yaml: str | Path) -> set[str]:
    """Names any persona enables (``enabled`` absent counts as enabled), or an
    empty set when the config cannot be read, which prunes nothing."""
    try:
        import yaml

        doc = yaml.safe_load(Path(customer_yaml).read_text(encoding="utf-8")) or {}
        names: set[str] = set()
        for persona in doc.get("personas") or []:
            for skill in (persona or {}).get("skills") or []:
                if (
                    isinstance(skill, dict)
                    and skill.get("enabled") is not False
                    and isinstance(skill.get("name"), str)
                ):
                    names.add(skill["name"].strip())
        return names
    except Exception as exc:  # noqa: BLE001 — unreadable config prunes nothing
        logger.warning("sync: could not read enabled skills from %s: %s", customer_yaml, exc)
        return set()


def sync_skills(
    source: str | Path,
    dest: str | Path,
    *,
    retired: Iterable[str] = (),
    enabled: set[str] | None = None,
) -> SyncResult:
    source, dest = Path(source), Path(dest)
    names = _scan_source(source)
    _prepare_dest(dest)
    result = SyncResult()
    allow = enabled or set()
    for name in names:
        if allow and name not in allow:
            if _remove_entry(dest / name):
                result.pruned_omitted.append(name)
                logger.info("sync: pruned omitted skill %s (not enabled in customer.yaml)", name)
            continue
        _replace_skill(source / name, dest, name)
        result.seeded.append(name)
    if not allow:
        logger.warning("sync: no enabled skill read; omitted-skill prune skipped")
    for name in retired:
        if name in names:
            logger.warning("sync: retired skill %s is still in the source catalog; kept", name)
            continue
        try:
            if _remove_entry(dest / name):
                result.pruned_retired.append(name)
                logger.info("sync: pruned retired skill %s", name)
        except OSError as exc:
            logger.warning("sync: cannot prune retired skill %s: %s", name, exc)
    return result


def refresh_profiles(hermes_home: str | Path, customer_yaml: str | Path) -> list[str]:
    """Re-install each existing persona profile's skills from the volume catalog.

    Uses translate's own functions (``_drop_unrunnable``, ``_install_persona_skills``,
    ``_refresh_staged_scripts``) so a live release lands exactly as a boot
    would land it, staged pre_run copies included. One persona failing is
    logged and does not stop the rest.
    """
    import yaml

    from bootstrap import translate

    home = Path(hermes_home)
    skills_dir = home / "skills"
    customer = yaml.safe_load(Path(customer_yaml).read_text(encoding="utf-8")) or {}
    personas, unrunnable = translate._drop_unrunnable(customer.get("personas") or [], skills_dir)
    spec_block = translate._spec_pointer_block()
    refreshed: list[str] = []
    for persona in personas:
        slug = persona.get("slug")
        profile_dir = home / "profiles" / str(slug)
        if not slug or _is_link(profile_dir) or not profile_dir.is_dir():
            continue
        try:
            _clear_profile_links(profile_dir / "skills")
            translate._install_persona_skills(
                persona=persona,
                profile_dir=profile_dir,
                skills_dir=skills_dir,
                spec_block=spec_block,
                prune={f["skill"] for f in unrunnable if f.get("persona") == slug},
            )
            translate._refresh_staged_scripts(profile_dir)
            refreshed.append(str(slug))
        except Exception as exc:  # noqa: BLE001 — one profile never blocks the rest
            logger.error("sync: refreshing profile %s failed: %s", slug, exc)
    return refreshed


def _clear_profile_links(skills_root: Path) -> None:
    """Drop any profile skill dir that is, or contains, a symlink, so the
    re-install below writes a real tree and never through a link."""
    if _is_link(skills_root):
        skills_root.unlink()
    if not skills_root.is_dir():
        return
    for entry in os.scandir(skills_root):
        path = Path(entry.path)
        if _is_link(path):
            path.unlink()
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            if any(_is_link(Path(dirpath) / n) for n in dirnames + filenames):
                shutil.rmtree(path)
                break


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m skills_applier.sync", description=__doc__)
    p.add_argument("--source", required=True, help="skill catalog to install from")
    p.add_argument("--dest", required=True, help="the volume catalog (HERMES_HOME/skills)")
    p.add_argument("--retired-file", action="append", default=[], help="repeatable")
    p.add_argument("--enabled-skills-from", help="customer.yaml for the omitted-skill prune")
    p.add_argument("--refresh-profiles", metavar="HERMES_HOME", help="also refresh profiles")
    p.add_argument(
        "--allow-root",
        action="store_true",
        help="permit uid 0 (tests only; the volume must never be written as root)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Exit 0 synced, 1 sync failed (nothing half-replaced), 2 usage or uid 0."""
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("SMD_SKILLS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if os.geteuid() == 0 and not args.allow_root:
        logger.error("sync: refusing to run as root; the volume is written as hermes")
        return 2
    enabled = read_enabled(args.enabled_skills_from) if args.enabled_skills_from else None
    try:
        result = sync_skills(
            args.source,
            args.dest,
            retired=read_retired(args.retired_file),
            enabled=enabled,
        )
    except (SyncError, OSError) as exc:
        logger.error("sync: %s", exc)
        return 1
    if args.refresh_profiles:
        if not args.enabled_skills_from:
            logger.error("sync: --refresh-profiles needs --enabled-skills-from customer.yaml")
            return 2
        result.profiles_refreshed = refresh_profiles(
            args.refresh_profiles, args.enabled_skills_from
        )
    logger.info(
        "sync: seeded %d, pruned retired %s, pruned omitted %s, profiles %s",
        len(result.seeded),
        result.pruned_retired,
        result.pruned_omitted,
        result.profiles_refreshed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
