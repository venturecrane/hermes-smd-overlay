"""The file-tool path fence on a seat that keeps records (record-store/path_fence.py).

THE DEFECT (proven offline 2026-10-17). The owner fence guarded only the
``record_store_*`` tools. ``read_file`` is READ-class, on the webhook (email)
surface, and Hermes' own read guard does not know a record store exists, so an
agent's email turn that named a colleague's record by path got the colleague's
private notes while ``record_store_read`` of the same record refused.

The first test is the falsifier: it fails on the pre-fix plugin (no hook
refuses the read, and the real Hermes ``read_file_tool`` returns the notes) and
passes once the fence is wired. The rest pin the allowlist, the legitimate
flows that must still work, and the seats that must not change.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from shared import record_store
from shared.webhook_read_surface import WEBHOOK_READ_TOOLS
from tests.conftest import FakePluginContext, load_plugin

ALICE = "alice@example.com"
BOB = "bob@example.com"
BROKER = "broker@example.com"

_HERMES_SRC = os.environ.get("HERMES_AGENT_SRC", "")


def _record(owner: str) -> str:
    return (
        "---\n"
        f"agent: {owner}\n"
        "visitor: Priya Patel\n"
        "contact: 480-555-0177\n"
        "---\n\n"
        "Agent's notes, verbatim:\n\nPRIVATE NOTES\n"
    )


@pytest.fixture()
def seat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """A Brokery-shaped seat: a private open-house store plus a shared,
    writer-gated reference store, a profile home, a spec dir, a skills root."""
    volume = tmp_path / "opt-data"
    home = volume / "profiles" / "brokery-agent"
    visitors = volume / "open-house" / "visitors"
    reference = volume / "reference"
    skills = tmp_path / "app-skills"
    specs = tmp_path / "smd-config" / "specs"
    for d in (
        home / "skills" / "open-house-visitor-capture",
        skills / "open-house-visitor-capture",
        specs,
    ):
        d.mkdir(parents=True)
    (home / "skills" / "open-house-visitor-capture" / "SKILL.md").write_text("profile skill\n")
    (skills / "open-house-visitor-capture" / "SKILL.md").write_text("image skill\n")
    (specs / "voice.md").write_text("voice spec\n")
    policies = {
        "open-house-visitors": record_store.StorePolicy(
            root=visitors,
            owner_field="agent",
            readers=frozenset({BROKER}),
            index=("visitor", "property", "visit_date", "contact"),
        ),
        "seat-reference": record_store.StorePolicy(root=reference, writers=frozenset({BROKER})),
    }
    stores = {name: p.root for name, p in policies.items()}
    monkeypatch.setattr(record_store, "authored_policies", lambda *_, **__: dict(policies))
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: dict(stores))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("SMD_SPEC_DIR", str(specs))
    record_store.write_record("open-house-visitors", "alice.md", _record(ALICE), stores=stores)
    record_store.write_record("open-house-visitors", "bob.md", _record(BOB), stores=stores)
    plugin = load_plugin("hermes-smd-record-store")
    monkeypatch.setattr(plugin.path_fence, "SKILL_ROOTS", (str(skills),))
    monkeypatch.setattr(plugin.path_fence, "_hermes_spillover", lambda: None)
    monkeypatch.setattr(plugin.SESSION_TAINT, "is_tainted", lambda sid: False)
    plugin.path_fence.SPILLOVER._reset_for_tests()
    ctx = FakePluginContext()
    plugin.register(ctx)
    return {
        "plugin": plugin,
        "ctx": ctx,
        "home": home,
        "volume": volume,
        "visitors": visitors,
        "skills": skills,
        "specs": specs,
        "stores": stores,
        "monkeypatch": monkeypatch,
    }


def _as_sender(seat: dict[str, Any], sender: str | None) -> None:
    class _Origin:
        sender_address = sender or ""

    seat["monkeypatch"].setattr(
        seat["plugin"].SESSION_INBOUND_ORIGIN, "get", lambda sid: _Origin() if sender else None
    )


def _pre(seat: dict[str, Any], tool_name: str, args: dict[str, Any], **kw: Any) -> dict | None:
    """Every pre_tool_call hook the plugin wired, in order; the first block wins
    (Hermes' ``_get_pre_tool_call_directive_details``)."""
    for hook in seat["ctx"].registered.get("pre_tool_call", []):
        out = hook(
            tool_name=tool_name,
            args=args,
            session_id=kw.get("session_id", "s1"),
            **{k: v for k, v in kw.items() if k != "session_id"},
        )
        if out and out.get("action") == "block":
            return out
    return None


def _blocked(seat: dict[str, Any], tool_name: str, args: dict[str, Any], **kw: Any) -> bool:
    return _pre(seat, tool_name, args, **kw) is not None


# ---------------------------------------------------------------------------
# the falsifier
# ---------------------------------------------------------------------------


def _hermes_read_file() -> Any:
    """The real Hermes read_file_tool, or a skip outside CI when the pinned
    Hermes is not importable here. CI must run it."""
    if not _HERMES_SRC:
        if os.environ.get("CI"):
            pytest.fail("CI must check out the pinned Hermes and set HERMES_AGENT_SRC")
        pytest.skip("HERMES_AGENT_SRC not set")
    if _HERMES_SRC not in sys.path:
        sys.path.insert(0, _HERMES_SRC)
    try:
        from tools.file_tools import read_file_tool
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("CI"):
            raise
        pytest.skip(f"pinned Hermes not importable here: {exc}")
    return read_file_tool


def test_a_members_email_turn_cannot_read_a_colleagues_record_by_path(seat: dict[str, Any]) -> None:
    _as_sender(seat, BOB)
    path = str(seat["visitors"] / "alice.md")
    # read_file is on the email surface: the turn can ask for it.
    assert "read_file" in WEBHOOK_READ_TOOLS
    # control: the record tool refuses Bob his colleague's record
    control = _pre(seat, "record_store_read", {"store": "open-house-visitors", "name": "alice.md"})
    assert control is not None and "belongs to another person" in control["message"]
    # the same record by path is refused too (pre-fix: nothing refused this)
    blocked = _pre(seat, "read_file", {"path": path})
    assert blocked is not None and "record_store_read" in blocked["message"]
    # and what the fence stands in front of is real: Hermes' own read_file
    # would hand the notes over (its guard knows nothing of record stores)
    served = json.loads(_hermes_read_file()(path))
    assert "PRIVATE NOTES" in served.get("content", ""), served


def test_the_broker_and_the_owner_cannot_read_records_by_path_either(seat: dict[str, Any]) -> None:
    for sender in (ALICE, BROKER, None):  # owner, reader, a scheduled wake
        _as_sender(seat, sender)
        assert _blocked(seat, "read_file", {"path": str(seat["visitors"] / "alice.md")})


# ---------------------------------------------------------------------------
# what the allowlist refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "cron/output/job-1/2026-10-16.md",
        "cron/jobs.json",
        "state.db",
        "sessions/sess-1.jsonl",
        "memories/MEMORY.md",
        "logs/agent.log",
        "config.yaml",
    ],
)
def test_the_runtimes_own_records_of_other_turns_are_not_readable(
    seat: dict[str, Any], rel: str
) -> None:
    target = seat["home"] / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("another person's turn\n")
    _as_sender(seat, BOB)
    assert _blocked(seat, "read_file", {"path": str(target)})
    assert _blocked(seat, "search_files", {"pattern": "turn", "path": str(target.parent)})


def test_searches_that_hold_a_store_are_refused(seat: dict[str, Any]) -> None:
    _as_sender(seat, BOB)
    for where in (seat["visitors"], seat["visitors"].parent, seat["volume"], "/"):
        assert _blocked(seat, "search_files", {"pattern": "PRIVATE", "path": str(where)})
    # no path: Hermes searches the working directory, which is not listed
    assert _blocked(seat, "search_files", {"pattern": "PRIVATE"})
    # a glob that names a directory reaches past the path
    assert _blocked(
        seat,
        "search_files",
        {"pattern": "../../open-house/**", "target": "files", "path": str(seat["skills"])},
    )


def test_spellings_that_resolve_into_a_store_are_refused(seat: dict[str, Any]) -> None:
    _as_sender(seat, BOB)
    skills = seat["skills"]
    escape = str(skills / ".." / "opt-data" / "open-house" / "visitors" / "alice.md")
    assert _blocked(seat, "read_file", {"path": escape})
    # a symlink that sits under an allowed root and points into a store
    link = skills / "open-house-visitor-capture" / "notes.md"
    link.symlink_to(seat["visitors"] / "alice.md")
    assert _blocked(seat, "read_file", {"path": str(link)})
    # a relative path resolves against the working directory, which is not listed
    os_cwd = os.getcwd()
    try:
        os.chdir(seat["visitors"])
        assert _blocked(seat, "read_file", {"path": "alice.md"})
    finally:
        os.chdir(os_cwd)


def test_a_store_authored_under_an_allowed_root_is_still_refused(
    seat: dict[str, Any],
) -> None:
    inside = seat["skills"] / "kept-records"
    seat["monkeypatch"].setattr(record_store, "authored_stores", lambda *_, **__: {"odd": inside})
    inside.mkdir()
    (inside / "r.md").write_text("PRIVATE\n")
    assert _blocked(seat, "read_file", {"path": str(inside / "r.md")})
    assert _blocked(seat, "search_files", {"pattern": "x", "path": str(seat["skills"])})


def test_code_tools_are_refused_on_a_seat_that_keeps_records(seat: dict[str, Any]) -> None:
    for sender in (BOB, None):
        _as_sender(seat, sender)
        out = _pre(seat, "terminal", {"command": "ls"})
        assert out is not None and "terminal and execute_code" in out["message"]
        assert _blocked(seat, "execute_code", {"code": "print(1)"})


def test_a_read_that_names_nothing_and_an_undecidable_fence_refuse(seat: dict[str, Any]) -> None:
    assert _blocked(seat, "read_file", {})
    assert _blocked(seat, "read_file", "not-a-dict")  # type: ignore[arg-type]

    def boom(*_: Any, **__: Any) -> dict:
        raise record_store.RecordStoreError("customer.yaml unreadable: gone")

    seat["monkeypatch"].setattr(record_store, "authored_stores", boom)
    assert _blocked(seat, "read_file", {"path": str(seat["specs"] / "voice.md")})


# ---------------------------------------------------------------------------
# what still works
# ---------------------------------------------------------------------------


def test_skills_and_specs_still_read(seat: dict[str, Any]) -> None:
    _as_sender(seat, BOB)
    skill = "open-house-visitor-capture"
    assert not _blocked(seat, "read_file", {"path": str(seat["skills"] / skill / "SKILL.md")})
    assert not _blocked(
        seat, "read_file", {"path": str(seat["home"] / "skills" / skill / "SKILL.md")}
    )
    assert not _blocked(seat, "read_file", {"path": str(seat["specs"] / "voice.md")})
    assert not _blocked(
        seat, "search_files", {"pattern": "skill", "path": str(seat["skills"] / skill)}
    )
    assert not _blocked(seat, "skill_view", {"name": skill})
    assert not _blocked(seat, "skill_view", {"name": skill, "file_path": "references/policy.md"})
    assert _blocked(
        seat, "skill_view", {"name": skill, "file_path": "../../open-house/visitors/alice.md"}
    )
    assert _blocked(
        seat, "skill_view", {"name": skill, "file_path": str(seat["visitors"] / "alice.md")}
    )
    assert _blocked(seat, "skill_view", {"name": "../open-house"})


def test_a_spillover_file_reads_only_in_the_session_that_produced_it(
    seat: dict[str, Any],
) -> None:
    spill = seat["home"] / "cache" / "spillover"
    spill.mkdir(parents=True)
    (spill / "toolu_1.txt").write_text("a long listing\n")
    seat["monkeypatch"].setattr(
        seat["plugin"].path_fence,
        "_hermes_spillover",
        lambda: (os.path.realpath(spill), lambda call_id: f"{call_id}.txt"),
    )
    path = str(spill / "toolu_1.txt")
    assert _blocked(seat, "read_file", {"path": path}, session_id="s1")  # nothing noted yet
    for hook in seat["ctx"].registered["post_tool_call"]:
        hook(
            tool_name="record_store_list",
            args={},
            session_id="s1",
            tool_call_id="toolu_1",
            result="x",
        )
    assert not _blocked(seat, "read_file", {"path": path}, session_id="s1")
    assert _blocked(seat, "read_file", {"path": path}, session_id="s2")  # another person's turn
    assert _blocked(seat, "search_files", {"pattern": "x", "path": str(spill)}, session_id="s1")


def test_the_brokery_flows_pass_every_hook(seat: dict[str, Any]) -> None:
    """Capture, listing, own-record read, broker read, writer edit: every hook
    the plugin wires lets them through, and the handlers serve them."""
    _as_sender(seat, BOB)
    tools = seat["ctx"].tools
    new = _record(BOB).replace("Priya Patel", "Sam Lee")
    write_args = {"store": "open-house-visitors", "name": "bob-2.md", "content": new}
    assert _pre(seat, "record_store_write", write_args) is None
    assert json.loads(tools["record_store_write"]["handler"](write_args))["created"] is True
    assert _pre(seat, "record_store_list", {"store": "open-house-visitors"}) is None
    listing = json.loads(tools["record_store_list"]["handler"]({"store": "open-house-visitors"}))
    assert listing["count"] == 3
    own = {"store": "open-house-visitors", "name": "bob-2.md"}
    assert _pre(seat, "record_store_read", own) is None
    assert "Sam Lee" in json.loads(tools["record_store_read"]["handler"](own))["content"]
    _as_sender(seat, BROKER)
    assert (
        _pre(seat, "record_store_read", {"store": "open-house-visitors", "name": "alice.md"})
        is None
    )
    edit = {"store": "seat-reference", "name": "program.md", "content": "# Program\n"}
    assert _pre(seat, "record_store_write", edit) is None
    # a scheduled wake still opens every record through the tool, as before
    _as_sender(seat, None)
    assert (
        _pre(
            seat,
            "record_store_read",
            {"store": "open-house-visitors", "name": "alice.md"},
            session_id="cron",
        )
        is None
    )


# ---------------------------------------------------------------------------
# seats that must not change
# ---------------------------------------------------------------------------


def test_a_seat_with_no_store_wires_no_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: {})
    ctx = FakePluginContext()
    load_plugin("hermes-smd-record-store").register(ctx)
    assert ctx.registered == {} and ctx.tools == {}


def test_an_unreadable_customer_yaml_wires_nothing_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.customer_config import CustomerConfigError

    def boom(*_: Any, **__: Any) -> dict:
        try:
            raise CustomerConfigError("customer.yaml not found")
        except CustomerConfigError as exc:
            raise record_store.RecordStoreError(f"customer.yaml unreadable: {exc}") from exc

    monkeypatch.setattr(record_store, "authored_stores", boom)
    ctx = FakePluginContext()
    load_plugin("hermes-smd-record-store").register(ctx)
    assert ctx.registered == {} and ctx.tools == {}


def test_a_malformed_record_stores_block_wires_the_fence_and_no_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*_: Any, **__: Any) -> dict:
        raise record_store.RecordStoreError("record_stores[0].path: must live under /opt/data")

    monkeypatch.setattr(record_store, "authored_stores", boom)
    ctx = FakePluginContext()
    load_plugin("hermes-smd-record-store").register(ctx)
    assert ctx.tools == {}
    hooks = ctx.registered["pre_tool_call"]
    target = tmp_path / "r.md"
    target.write_text("x\n")
    assert any(h(tool_name="read_file", args={"path": str(target)}, session_id="s") for h in hooks)


# ---------------------------------------------------------------------------
# the write side: write_file and patch
# ---------------------------------------------------------------------------


def _hermes_write_file() -> Any:
    _hermes_read_file()  # same import gate (skip locally, required in CI)
    from tools.file_tools import write_file_tool

    return write_file_tool


def test_a_scheduled_wake_cannot_write_a_record_by_path(seat: dict[str, Any]) -> None:
    """The write falsifier: a cron turn (no inbound sender) writing straight
    into the store would skip the owner stamp, the writers list and the
    no-overwrite rule. Pre-fix nothing refused it."""
    _as_sender(seat, None)
    target = seat["visitors"] / "alice.md"
    forged = _record(BOB)  # Bob's stamp over Alice's record, no overwrite flag
    blocked = _pre(seat, "write_file", {"path": str(target), "content": forged}, session_id="cron")
    assert blocked is not None and "record_store_write" in blocked["message"]
    new = seat["visitors"] / "planted.md"
    assert _blocked(seat, "write_file", {"path": str(new), "content": forged}, session_id="cron")
    assert _blocked(
        seat,
        "patch",
        {"mode": "replace", "path": str(target), "old_string": ALICE, "new_string": BOB},
        session_id="cron",
    )
    # what the fence stands in front of: Hermes' own write_file lands it
    scratch = seat["visitors"] / "hermes-would-write.md"
    out = json.loads(_hermes_write_file()(str(scratch), forged))
    assert "error" not in out, out
    assert scratch.read_text() == forged


@pytest.mark.parametrize(
    "rel",
    [
        "cron/jobs.json",
        "cron/output/job-1/2026-10-16.md",
        "state.db",
        "sessions/sess-1.jsonl",
        "memories/MEMORY.md",
        "logs/agent.log",
        "config.yaml",
        "skills/open-house-visitor-capture/SKILL.md",
    ],
)
def test_no_file_tool_writes_anywhere_on_a_seat_that_keeps_records(
    seat: dict[str, Any], rel: str
) -> None:
    for sender in (None, BOB, BROKER):
        _as_sender(seat, sender)
        target = str(seat["home"] / rel)
        assert _blocked(seat, "write_file", {"path": target, "content": "x"})
        assert _blocked(
            seat, "patch", {"mode": "replace", "path": target, "old_string": "a", "new_string": "b"}
        )


def test_writes_that_cannot_be_judged_refuse(seat: dict[str, Any]) -> None:
    v4a = "*** Begin Patch\n*** Update File: /opt/data/open-house/visitors/a.md\n*** End Patch\n"
    assert _blocked(seat, "patch", {"mode": "patch", "patch": v4a})
    assert _blocked(seat, "patch", {"patch": v4a})  # mode defaulted, text still present
    assert _blocked(seat, "write_file", {"content": "x"})  # no path
    os_cwd = os.getcwd()
    try:
        os.chdir(seat["visitors"])
        assert _blocked(seat, "write_file", {"path": "rel.md", "content": "x"})
    finally:
        os.chdir(os_cwd)


def test_a_writable_root_admits_writes_but_never_into_a_store(
    seat: dict[str, Any], tmp_path: Path
) -> None:
    """The allowlist is a real mechanism, not a blanket refusal: a listed root
    admits a write, and a store under it, or a symlink into one, still refuses."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    seat["monkeypatch"].setattr(seat["plugin"].path_fence, "WRITABLE_ROOTS", (str(scratch),))
    assert not _blocked(seat, "write_file", {"path": str(scratch / "note.txt"), "content": "x"})
    assert not _blocked(
        seat,
        "patch",
        {
            "mode": "replace",
            "path": str(scratch / "note.txt"),
            "old_string": "x",
            "new_string": "y",
        },
    )
    (scratch / "into-store").symlink_to(seat["visitors"])
    assert _blocked(
        seat, "write_file", {"path": str(scratch / "into-store" / "a.md"), "content": "x"}
    )
    seat["monkeypatch"].setattr(
        record_store, "authored_stores", lambda *_, **__: {"odd": scratch / "kept"}
    )
    assert _blocked(seat, "write_file", {"path": str(scratch / "kept" / "r.md"), "content": "x"})


def test_a_seat_with_no_store_is_not_fenced_for_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a seat that authors no store wires no hook, so write_file and
    patch (and every read) are judged exactly as before this plugin's fence."""
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: {})
    ctx = FakePluginContext()
    plugin = load_plugin("hermes-smd-record-store")
    plugin.register(ctx)
    assert ctx.registered == {} and ctx.tools == {}
    # and the fence itself, consulted directly, never judges a non-fenced tool
    assert plugin.path_fence.check("web_search", {"query": "x"}, [], {}) is None
