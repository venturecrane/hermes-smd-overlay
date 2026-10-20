"""skills_applier: root install, capability gate, commit ordering, poll loop.

Every R2 call goes to an in-memory fake; every disk write to tmp_path. The
falsifiers that matter: a corrupt object, a manifest whose bytes disagree with
the pointer, an oversize body, and a skill the image cannot run must each leave
``current`` exactly where it was.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import stat
from pathlib import Path

import pytest

from skills_applier import __main__ as entry
from skills_applier import applier as ap
from skills_applier import manifest as mf

SLUG = "acme"
BUCKET = "cfg"


class _Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.gets: list[str] = []

    def put(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    def get_object(self, Bucket: str, Key: str):  # noqa: N803 — boto3 shape
        self.gets.append(Key)
        if Key not in self.objects:
            raise _Missing(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket: str, Key: str):  # noqa: N803
        if Key not in self.objects:
            raise _Missing(Key)
        return {"ETag": '"' + hashlib.md5(self.objects[Key]).hexdigest() + '"'}  # noqa: S324


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def publish(
    s3: FakeS3, version: str, files: dict[str, bytes], *, retired=(), needs=None, modes=None
):
    """Upload objects + manifest + pointer the way CI and `fleet release` do."""
    entries = {}
    for rel, data in files.items():
        s3.put(mf.object_key(_sha(data)), data)
        entries[rel] = {"sha256": _sha(data), "size": len(data)}
        if modes and rel in modes:
            entries[rel]["mode"] = modes[rel]
    manifest = {
        "version": version,
        "source_sha": "abc",
        "created_at": "2026-10-19T00:00:00Z",
        "files": entries,
        "retired": list(retired),
        "needs": needs or {},
    }
    raw = json.dumps(manifest).encode()
    key = f"fleet/skills/{version}.json"
    s3.put(key, raw)
    s3.put(
        mf.pointer_key(SLUG),
        json.dumps(
            {"version": version, "manifest_key": key, "manifest_sha256": _sha(raw)}
        ).encode(),
    )
    return raw


def _stage(s3, root, **kw):
    kw.setdefault("image_skills", root.parent / "image-skills")
    kw.setdefault("capabilities_path", root.parent / "caps.json")
    return ap.stage(s3_client=s3, bucket=BUCKET, slug=SLUG, root=root, **kw)


def _apply(s3, root, **kw):
    result = _stage(s3, root, **kw)
    if result.outcome is ap.Outcome.APPLIED:
        ap.commit(root, result.version)
    return result


V1 = {
    "alpha/SKILL.md": b"alpha v1\n",
    "beta/SKILL.md": b"beta v1\n",
    "beta/tools/run.py": b"print(1)\n",
}


@pytest.fixture
def root(tmp_path):
    return tmp_path / "smd-skills"


# ---------------------------------------------------------------------------
# Happy path + layout
# ---------------------------------------------------------------------------


def test_happy_path_installs_and_commits(root):
    s3 = FakeS3()
    raw = publish(s3, "v1", V1, retired=["gamma"], modes={"beta/tools/run.py": "0755"})
    result = _apply(s3, root)
    assert result.outcome is ap.Outcome.APPLIED
    assert os.readlink(root / "current") == "v1"
    cur = root / "current"
    assert (cur / "VERSION").read_text() == "v1\n"
    assert (cur / "skills/alpha/SKILL.md").read_bytes() == b"alpha v1\n"
    assert (cur / "manifest.json").read_bytes() == raw
    assert (cur / "retired.txt").read_text() == "gamma\n"
    assert json.loads((cur / "REFUSED.json").read_text())["refused"] == {}
    assert stat.S_IMODE(os.stat(cur / "skills/beta/tools/run.py").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(cur / "skills/alpha/SKILL.md").st_mode) == 0o644
    assert stat.S_IMODE(os.stat(cur / "skills/beta/tools").st_mode) == 0o755
    assert ap.read_current_version(root) == "v1"


def test_same_version_is_unchanged_and_fetches_nothing_more(root):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    _apply(s3, root)
    s3.gets.clear()
    assert _stage(s3, root).outcome is ap.Outcome.UNCHANGED
    assert s3.gets == [mf.pointer_key(SLUG)]


def test_unchanged_files_are_reused_not_downloaded(root):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    _apply(s3, root)
    s3.gets.clear()
    publish(s3, "v2", {**V1, "alpha/SKILL.md": b"alpha v2\n"})
    assert _apply(s3, root).outcome is ap.Outcome.APPLIED
    object_gets = [k for k in s3.gets if k.startswith(mf.OBJECT_PREFIX)]
    assert object_gets == [mf.object_key(_sha(b"alpha v2\n"))]


# ---------------------------------------------------------------------------
# Fail-static
# ---------------------------------------------------------------------------


def _assert_still_v1(root):
    assert os.readlink(root / "current") == "v1"
    assert (root / "current/skills/alpha/SKILL.md").read_bytes() == b"alpha v1\n"
    assert not [p for p in root.iterdir() if p.name.startswith(".staging-")]


def test_corrupt_object_keeps_previous(root):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    _apply(s3, root)
    publish(s3, "v2", {**V1, "alpha/SKILL.md": b"alpha v2\n"})
    s3.put(mf.object_key(_sha(b"alpha v2\n")), b"alpha vX\n")  # same size, wrong bytes
    result = _apply(s3, root)
    assert result.outcome is ap.Outcome.REJECTED
    assert any("does not match its sha256" in r for r in result.reasons)
    _assert_still_v1(root)
    assert not (root / "v2").exists()


def test_missing_object_keeps_previous(root):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    _apply(s3, root)
    publish(s3, "v2", {**V1, "alpha/SKILL.md": b"alpha v2\n"})
    del s3.objects[mf.object_key(_sha(b"alpha v2\n"))]
    assert _apply(s3, root).outcome is ap.Outcome.REJECTED
    _assert_still_v1(root)


def test_manifest_sha_mismatch_rejected(root):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    _apply(s3, root)
    publish(s3, "v2", V1)
    key = "fleet/skills/v2.json"
    s3.put(key, s3.objects[key] + b" ")
    result = _apply(s3, root)
    assert result.outcome is ap.Outcome.REJECTED
    assert "manifest_sha256" in result.reasons[0]
    _assert_still_v1(root)


def test_oversize_object_rejected_without_reading_past_its_size(root):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    _apply(s3, root)
    publish(s3, "v2", {**V1, "alpha/SKILL.md": b"alpha v2\n"})
    s3.put(mf.object_key(_sha(b"alpha v2\n")), b"alpha v2\n" + b"x" * 1000)
    result = _apply(s3, root)
    assert result.outcome is ap.Outcome.REJECTED
    assert any("larger than its declared" in r for r in result.reasons)
    _assert_still_v1(root)


def test_size_cap_refuses_the_manifest(root):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    _apply(s3, root)
    raw = publish(s3, "v2", V1)
    doc = json.loads(raw)
    doc["files"]["alpha/SKILL.md"]["size"] = mf.MAX_FILE_BYTES + 1
    raw = json.dumps(doc).encode()
    s3.put("fleet/skills/v2.json", raw)
    s3.put(
        mf.pointer_key(SLUG),
        json.dumps(
            {"version": "v2", "manifest_key": "fleet/skills/v2.json", "manifest_sha256": _sha(raw)}
        ).encode(),
    )
    result = _apply(s3, root)
    assert result.outcome is ap.Outcome.REJECTED
    assert any("cap" in r for r in result.reasons)
    _assert_still_v1(root)


@pytest.mark.parametrize(
    "rel",
    ["../escape/SKILL.md", "/abs/SKILL.md", "Bad/SKILL.md", "alpha/../x", "alpha/.hidden", "alpha"],
)
def test_unsafe_paths_refuse_the_whole_manifest(root, rel):
    s3 = FakeS3()
    publish(s3, "v1", {**V1, rel: b"x"})
    result = _apply(s3, root)
    assert result.outcome is ap.Outcome.REJECTED
    assert not (root / "current").exists()


def test_bad_pointer_rejected(root):
    s3 = FakeS3()
    s3.put(
        mf.pointer_key(SLUG),
        json.dumps(
            {"version": "v1", "manifest_key": "vaults/x.json", "manifest_sha256": "0" * 64}
        ).encode(),
    )
    assert _stage(s3, root).outcome is ap.Outcome.REJECTED


def test_missing_pointer_raises_pointer_missing(root):
    with pytest.raises(ap.PointerMissing):
        _stage(FakeS3(), root)


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------


def test_capability_refusal_keeps_previous_skill_version(root, tmp_path):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    _apply(s3, root)
    (tmp_path / "caps.json").write_text(
        json.dumps({"tools": ["mcp_x"], "verbs": [], "action_classes": {"send": 1}})
    )
    publish(
        s3,
        "v2",
        {**V1, "alpha/SKILL.md": b"alpha v2\n", "beta/SKILL.md": b"beta v2\n"},
        needs={
            "alpha": {"tools": ["mcp_missing"]},
            "beta": {"tools": ["mcp_x"], "action_classes": ["send"]},
        },
    )
    result = _apply(s3, root)
    assert result.outcome is ap.Outcome.APPLIED
    assert os.readlink(root / "current") == "v2"
    cur = root / "current/skills"
    assert (cur / "alpha/SKILL.md").read_bytes() == b"alpha v1\n"  # held back
    assert (cur / "beta/SKILL.md").read_bytes() == b"beta v2\n"  # went ahead
    refused = json.loads((root / "current/REFUSED.json").read_text())["refused"]
    assert refused["alpha"]["missing"] == {"tools": ["mcp_missing"]}
    assert "beta" not in refused


def test_missing_path_need_refuses_with_no_previous_copy(root, tmp_path):
    s3 = FakeS3()
    publish(s3, "v1", V1, needs={"alpha": {"paths": [str(tmp_path / "not-on-this-image")]}})
    result = _apply(s3, root)
    assert result.outcome is ap.Outcome.APPLIED
    assert not (root / "current/skills/alpha").exists()
    assert result.refused["alpha"]["kept_previous_from"] is None


def test_previous_copy_falls_back_to_image_skills(root, tmp_path):
    img = tmp_path / "image-skills/alpha"
    img.mkdir(parents=True)
    (img / "SKILL.md").write_bytes(b"alpha baked\n")
    s3 = FakeS3()
    publish(s3, "v1", V1, needs={"alpha": {"paths": ["/definitely/absent/path"]}})
    _apply(s3, root)
    assert (root / "current/skills/alpha/SKILL.md").read_bytes() == b"alpha baked\n"


def test_without_capability_file_only_paths_are_checked(root, tmp_path):
    s3 = FakeS3()
    publish(s3, "v1", V1, needs={"alpha": {"tools": ["mcp_unknown"], "paths": [str(tmp_path)]}})
    result = _apply(s3, root)
    assert result.refused == {}
    assert result.unchecked == ("alpha:tools:mcp_unknown",)
    assert (root / "current/skills/alpha/SKILL.md").read_bytes() == b"alpha v1\n"


# ---------------------------------------------------------------------------
# Commit ordering + retention
# ---------------------------------------------------------------------------


def test_stage_does_not_move_current_and_version_is_written_last(root, monkeypatch):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    _apply(s3, root)
    publish(s3, "v2", {**V1, "alpha/SKILL.md": b"alpha v2\n"})
    order: list[str] = []
    real = ap._write_new

    def spy(path, data, mode):
        order.append(Path(path).name)
        real(path, data, mode)

    monkeypatch.setattr(ap, "_write_new", spy)
    result = _stage(s3, root)
    assert result.outcome is ap.Outcome.APPLIED
    assert order[-1] == "VERSION"
    assert os.readlink(root / "current") == "v1"  # staged, not committed
    ap.commit(root, "v2")
    assert os.readlink(root / "current") == "v2"


def test_commit_refuses_an_incomplete_version_dir(root):
    (root / "v9/skills").mkdir(parents=True)
    with pytest.raises(ap.SkillsApplyError):
        ap.commit(root, "v9")


def test_crash_before_version_leaves_no_installed_dir(root, monkeypatch):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    real = ap._write_new

    def fail_on_version(path, data, mode):
        if Path(path).name == "VERSION":
            raise OSError("disk full")
        real(path, data, mode)

    monkeypatch.setattr(ap, "_write_new", fail_on_version)
    with pytest.raises(ap.SkillsApplyError):
        _stage(s3, root)
    assert not (root / "v1").exists()
    assert not (root / "current").exists()
    assert not [p for p in root.iterdir() if p.name.startswith(".staging-")]


def test_prune_keeps_current_and_previous(root):
    s3 = FakeS3()
    for v in ("v1", "v2", "v3"):
        publish(s3, v, {**V1, "alpha/SKILL.md": f"alpha {v}\n".encode()})
        result = _apply(s3, root)
        ap.prune(root, {result.version, result.previous or result.version})
    assert sorted(p.name for p in root.iterdir()) == ["current", "v2", "v3"]


# ---------------------------------------------------------------------------
# Poll loop + entry point
# ---------------------------------------------------------------------------


def _loop(s3, root, tmp_path, runner):
    return entry.SkillsPollLoop(
        s3_client=s3,
        bucket=BUCKET,
        slug=SLUG,
        root=root,
        hermes_home=str(tmp_path / "home"),
        customer_yaml=str(tmp_path / "customer.yaml"),
        image_skills=tmp_path / "image-skills",
        capabilities_path=tmp_path / "caps.json",
        sync_runner=runner,
        poll_seconds=1,
    )


def test_loop_syncs_before_committing(root, tmp_path):
    s3 = FakeS3()
    publish(s3, "v1", V1, retired=["gamma"])
    seen = {}

    def runner(args):
        seen["args"] = args
        seen["current_at_sync"] = ap.read_current_version(root)
        return 0

    loop = _loop(s3, root, tmp_path, runner)
    assert loop.run_once() is ap.Outcome.APPLIED
    assert seen["current_at_sync"] is None  # sync ran before the commit
    assert os.readlink(root / "current") == "v1"
    args = seen["args"]
    assert args[args.index("--source") + 1] == str(root / "v1/skills")
    assert args[args.index("--dest") + 1] == str(tmp_path / "home/skills")
    assert str(root / "v1/retired.txt") in args
    assert args[args.index("--refresh-profiles") + 1] == str(tmp_path / "home")
    assert loop.run_once() is None  # ETag cached


def test_loop_failed_sync_leaves_current_and_retries(root, tmp_path):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    calls = []

    def runner(args):
        calls.append(1)
        return 1 if len(calls) == 1 else 0

    loop = _loop(s3, root, tmp_path, runner)
    assert loop.run_once() is None
    assert not (root / "current").exists()
    assert loop.run_once() is ap.Outcome.APPLIED
    assert os.readlink(root / "current") == "v1"


def test_loop_rejection_is_cached_until_a_new_publish(root, tmp_path):
    s3 = FakeS3()
    publish(s3, "v1", V1)
    s3.put("fleet/skills/v1.json", b"{}")
    loop = _loop(s3, root, tmp_path, lambda a: 0)
    assert loop.run_once() is ap.Outcome.REJECTED
    assert loop.run_once() is None
    publish(s3, "v2", V1)  # a corrected publish moves the pointer
    assert loop.run_once() is ap.Outcome.APPLIED


def test_boot_once_commits_without_sync_and_never_fails(root, tmp_path):
    s3 = FakeS3()
    publish(s3, "v1", V1)

    def runner(args):
        raise AssertionError("boot must not sync; bootstrap.sh does")

    loop = _loop(s3, root, tmp_path, runner)
    assert entry.run_boot_once(loop) == 0
    assert os.readlink(root / "current") == "v1"
    assert entry.run_boot_once(_loop(FakeS3(), tmp_path / "other", tmp_path, runner)) == 0


def test_sync_child_env_carries_no_r2_credentials(monkeypatch):
    captured = {}

    def fake_run(cmd, env, cwd, timeout, check):
        captured.update(cmd=cmd, env=env, cwd=cwd)

        class R:
            returncode = 0

        return R()

    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "never-in-the-child")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "never-either")
    monkeypatch.setattr(entry.subprocess, "run", fake_run)
    assert entry.run_sync_as_hermes(["--source", "s", "--dest", "d"]) == 0
    assert not any(k.startswith("R2_") for k in captured["env"])
    assert "--reuid=hermes" in captured["cmd"] and "--regid=hermes" in captured["cmd"]
    assert captured["cmd"][-6:-4] == ["-m", "skills_applier.sync"]
    assert captured["cwd"] == "/"


def test_main_exits_130_on_sigterm(root, tmp_path):
    class StopLoop:
        def run_forever(self, stop):
            os.kill(os.getpid(), signal.SIGTERM)
            while not stop():
                pass

    assert entry.main([], loop_factory=lambda with_audit: StopLoop()) == entry.EXIT_STOPPED


def test_main_once_returns_zero_even_when_startup_fails():
    def broken(with_audit):
        raise SystemExit("no env")

    assert entry.main(["--once"], loop_factory=broken) == 0
    assert entry.main([], loop_factory=broken) == 2


def test_emit_skills_write_uses_accepted_action_type():
    rows = []

    class Audit:
        def execute(self, sql, *params):
            rows.append(params)

    result = ap.SkillsApplyResult(
        ap.Outcome.APPLIED, version="v2", previous="v1", refused={"alpha": {}}
    )
    assert ap.emit_skills_write(audit_client=Audit(), slug=SLUG, result=result)
    assert rows[0][2] == "CONFIG_WRITE"
    meta = json.loads(rows[0][-1])
    assert meta["skills_apply"] is True and meta["version"] == "v2" and meta["refused"] == ["alpha"]


def test_emit_skills_write_prefers_the_root_verb():
    """A root poller's generic append is refused by the broker (gateway only)."""
    rows = []

    class BrokerAudit:
        def execute(self, sql, *params):
            raise AssertionError("refused for a root poller")

        def execute_config_write(self, sql, *params):
            rows.append(params)

    result = ap.SkillsApplyResult(ap.Outcome.APPLIED, version="v2", previous="v1")
    assert ap.emit_skills_write(audit_client=BrokerAudit(), slug=SLUG, result=result)
    assert rows[0][2] == "CONFIG_WRITE"
