"""skills_applier.sync: the volume copy bootstrap.sh and the applier share.

Pins bootstrap.sh step 6b's semantics (seed replaces per skill, agent-authored
skills untouched, retired prune, omitted prune with the empty-allowlist guard)
and the symlink rule: nothing is ever written through a link.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from skills_applier import sync


def _skill(root: Path, name: str, body: str = "x", extra: dict | None = None) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body)
    for rel, text in (extra or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)


def _customer(path: Path, enabled: list[str], disabled: list[str] = ()) -> Path:
    skills = [{"name": n} for n in enabled] + [{"name": n, "enabled": False} for n in disabled]
    path.write_text(yaml.safe_dump({"personas": [{"slug": "p", "skills": skills}]}))
    return path


@pytest.fixture
def src(tmp_path):
    s = tmp_path / "src"
    _skill(s, "alpha", "alpha new", {"tools/run.py": "print(1)"})
    _skill(s, "beta", "beta new")
    return s


def test_seed_replaces_repo_skills_and_keeps_agent_skills(src, tmp_path):
    dest = tmp_path / "dest"
    _skill(dest, "alpha", "alpha old", {"stale.md": "gone after replace"})
    _skill(dest, "my-own", "agent authored")  # ADR 0017
    result = sync.sync_skills(src, dest)
    assert result.seeded == ["alpha", "beta"]
    assert (dest / "alpha/SKILL.md").read_text() == "alpha new"
    assert not (dest / "alpha/stale.md").exists()
    assert (dest / "alpha/tools/run.py").read_text() == "print(1)"
    assert (dest / "my-own/SKILL.md").read_text() == "agent authored"
    assert not [p for p in dest.iterdir() if p.name.startswith(".smd-sync-")]


def test_file_modes_follow_the_source(src, tmp_path):
    # A script's exec bit must survive the copy (a staged pre_run is executed).
    exec_mode = os.stat(src / "alpha/tools/run.py").st_mode | stat.S_IXUSR
    os.chmod(src / "alpha/tools/run.py", exec_mode)
    dest = tmp_path / "dest"
    sync.sync_skills(src, dest)
    copied = os.stat(dest / "alpha/tools/run.py").st_mode
    assert stat.S_IMODE(copied) == stat.S_IMODE(exec_mode)
    assert stat.S_IMODE(os.stat(dest / "alpha/SKILL.md").st_mode) == stat.S_IMODE(
        os.stat(src / "alpha/SKILL.md").st_mode
    )


def test_retired_prune_semantics(src, tmp_path):
    dest = tmp_path / "dest"
    _skill(dest, "old-one")
    _skill(dest, "my-own")
    retired = tmp_path / "retired.txt"
    retired.write_text("# comment\n\n old-one \nBad_Name\nalpha\nnever-there\n")
    names = sync.read_retired([retired, tmp_path / "absent.txt"])
    assert names == ["old-one", "alpha", "never-there"]
    result = sync.sync_skills(src, dest, retired=names)
    assert result.pruned_retired == ["old-one"]
    assert (dest / "alpha").is_dir()  # still in the source catalog: kept
    assert (dest / "my-own").is_dir()


def test_omitted_prune_removes_unenabled_repo_skills_only(src, tmp_path):
    dest = tmp_path / "dest"
    _skill(dest, "beta", "beta old")
    _skill(dest, "my-own")
    cfg = _customer(tmp_path / "customer.yaml", ["alpha"], disabled=["beta"])
    result = sync.sync_skills(src, dest, enabled=sync.read_enabled(cfg))
    assert result.seeded == ["alpha"]
    assert result.pruned_omitted == ["beta"]
    assert not (dest / "beta").exists()
    assert (dest / "my-own").is_dir()


def test_unreadable_config_prunes_nothing(src, tmp_path):
    dest = tmp_path / "dest"
    assert sync.read_enabled(tmp_path / "missing.yaml") == set()
    result = sync.sync_skills(src, dest, enabled=set())
    assert result.seeded == ["alpha", "beta"] and result.pruned_omitted == []


def test_symlink_in_source_refuses_before_writing(src, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("secret")
    os.symlink(victim, src / "beta/leak.md")
    dest = tmp_path / "dest"
    _skill(dest, "alpha", "alpha old")
    with pytest.raises(sync.SyncError):
        sync.sync_skills(src, dest)
    assert (dest / "alpha/SKILL.md").read_text() == "alpha old"


def test_symlinked_skill_in_dest_is_replaced_never_followed(src, tmp_path):
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    (victim_dir / "SKILL.md").write_text("victim")
    dest = tmp_path / "dest"
    dest.mkdir()
    os.symlink(victim_dir, dest / "alpha")
    sync.sync_skills(src, dest)
    assert not (dest / "alpha").is_symlink()
    assert (dest / "alpha/SKILL.md").read_text() == "alpha new"
    assert (victim_dir / "SKILL.md").read_text() == "victim"


def test_symlink_inside_dest_skill_is_not_written_through(src, tmp_path):
    victim = tmp_path / "victim.md"
    victim.write_text("victim")
    dest = tmp_path / "dest"
    (dest / "alpha").mkdir(parents=True)
    os.symlink(victim, dest / "alpha/SKILL.md")
    sync.sync_skills(src, dest)
    assert victim.read_text() == "victim"
    assert not (dest / "alpha/SKILL.md").is_symlink()


def test_symlinked_catalog_root_is_dropped_as_a_link(src, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    dest = tmp_path / "dest"
    os.symlink(elsewhere, dest)
    sync.sync_skills(src, dest)
    assert not dest.is_symlink() and (dest / "alpha").is_dir()
    assert list(elsewhere.iterdir()) == []


def test_retired_symlink_removed_as_link(src, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.md").write_text("k")
    dest = tmp_path / "dest"
    dest.mkdir()
    os.symlink(target, dest / "old-one")
    sync.sync_skills(src, dest, retired=["old-one"])
    assert not os.path.lexists(dest / "old-one")
    assert (target / "keep.md").exists()


def test_cli_happy_path_and_root_refusal(src, tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    retired = tmp_path / "r.txt"
    retired.write_text("gone\n")
    _skill(dest, "gone")
    cfg = _customer(tmp_path / "c.yaml", ["alpha", "beta"])
    argv = [
        "--source",
        str(src),
        "--dest",
        str(dest),
        "--retired-file",
        str(retired),
        "--enabled-skills-from",
        str(cfg),
    ]
    assert sync.main(argv) == 0
    assert (dest / "beta").is_dir() and not (dest / "gone").exists()
    monkeypatch.setattr(sync.os, "geteuid", lambda: 0)
    assert sync.main(argv) == 2
    assert sync.main([*argv, "--allow-root"]) == 0


def test_cli_returns_1_on_refusal(src, tmp_path):
    os.symlink(tmp_path, src / "alpha/loop")
    assert sync.main(["--source", str(src), "--dest", str(tmp_path / "d")]) == 1


def test_refresh_profiles_updates_profile_copies_and_staged_scripts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    catalog = home / "skills"
    _skill(catalog, "alpha", "alpha new", {"pre_run.py": "new"})
    _skill(catalog, "beta", "beta new")
    profile = home / "profiles/p"
    _skill(profile / "skills", "alpha", "alpha old", {"pre_run.py": "old"})
    _skill(profile / "skills", "beta", "beta old")
    (profile / "scripts/alpha").mkdir(parents=True)
    (profile / "scripts/alpha/pre_run.py").write_text("old")
    victim = tmp_path / "victim.md"
    victim.write_text("victim")
    os.symlink(victim, profile / "skills/alpha/linked.md")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"personas": [{"slug": "p", "skills": [{"name": "alpha", "enabled": True}]}]}
        )
    )
    monkeypatch.delenv("SMD_SPEC_DIR", raising=False)
    assert sync.refresh_profiles(home, cfg) == ["p"]
    assert (profile / "skills/alpha/SKILL.md").read_text().startswith("alpha new")
    assert (profile / "scripts/alpha/pre_run.py").read_text() == "new"
    assert not (profile / "skills/beta").exists()  # not enabled for this persona
    assert victim.read_text() == "victim"
    assert not os.path.lexists(profile / "skills/alpha/linked.md")
