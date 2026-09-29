"""The record-store trio and the fence around it (ss-console#2793).

THE DEFECT (live, 2026-09-25). An inbound-email turn tried to keep a dictated
open-house note with ``write_file``; the webhook platform is never offered the
file toolset, Hermes rewrote the call to ``read_file``, and the record was never
written. The fix is a write that cannot take a path: store name plus record
name, resolved inside a directory the engagement authored. Each test here pins
one rule and the direction that would be a defect if it flipped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from shared import record_store
from shared.customer_config import CustomerConfig
from tests.conftest import load_plugin


@pytest.fixture()
def stores(tmp_path: Path) -> dict[str, Path]:
    return {"open-house-visitors": tmp_path / "open-house" / "visitors"}


# ---------------------------------------------------------------------------
# the authored block
# ---------------------------------------------------------------------------


def test_authored_stores_reads_name_and_path_from_the_config() -> None:
    cfg = CustomerConfig(
        {
            "customer_id": "scott",
            "record_stores": [
                {"name": "open-house-visitors", "path": "/opt/data/open-house/visitors"}
            ],
        }
    )
    assert record_store.authored_stores(cfg) == {
        "open-house-visitors": Path("/opt/data/open-house/visitors")
    }


def test_a_seat_that_authors_nothing_has_no_stores() -> None:
    assert record_store.authored_stores(CustomerConfig({"customer_id": "scott"})) == {}


@pytest.mark.parametrize(
    "path, fragment",
    [
        ("/opt/data", "not /opt/data itself"),
        ("/opt/data/profiles/agent-crane/skills", "owned by the runtime"),
        ("/opt/data/attachment-spool/x", "owned by the runtime"),
        ("/opt/data/.smd/records", "owned by the runtime"),
        ("/tmp/records", "must live under /opt/data"),
        ("/opt/data/open-house/../customer", "normalized"),
        ("relative/path", "absolute"),
    ],
)
def test_a_store_path_outside_the_agent_owned_volume_is_refused(path: str, fragment: str) -> None:
    cfg = CustomerConfig({"customer_id": "s", "record_stores": [{"name": "a", "path": path}]})
    with pytest.raises(record_store.RecordStoreError, match=fragment):
        record_store.authored_stores(cfg)


@pytest.mark.parametrize("name", ["Open House", "open_house", "-lead", "a" * 65, ""])
def test_a_store_name_that_is_not_kebab_case_is_refused(name: str) -> None:
    cfg = CustomerConfig(
        {"customer_id": "s", "record_stores": [{"name": name, "path": "/opt/data/x"}]}
    )
    with pytest.raises(record_store.RecordStoreError, match="kebab-case"):
        record_store.authored_stores(cfg)


def test_the_same_store_authored_twice_is_refused() -> None:
    cfg = CustomerConfig(
        {
            "customer_id": "s",
            "record_stores": [
                {"name": "a", "path": "/opt/data/a"},
                {"name": "a", "path": "/opt/data/b"},
            ],
        }
    )
    with pytest.raises(record_store.RecordStoreError, match="authored twice"):
        record_store.authored_stores(cfg)


# ---------------------------------------------------------------------------
# write, read, list
# ---------------------------------------------------------------------------


def test_a_write_lands_inside_the_store_and_reads_back(stores: dict[str, Path]) -> None:
    receipt = record_store.write_record(
        "open-house-visitors",
        "2026-09-13_1420-e-4th-st-tempe_nguyen.md",
        "---\nvisit_date: 2026-09-13\n---\n\nnotes",
        stores=stores,
    )
    assert receipt == {
        "store": "open-house-visitors",
        "name": "2026-09-13_1420-e-4th-st-tempe_nguyen.md",
        "bytes": len(b"---\nvisit_date: 2026-09-13\n---\n\nnotes"),
        "created": True,
    }
    path = stores["open-house-visitors"] / "2026-09-13_1420-e-4th-st-tempe_nguyen.md"
    assert path.read_text(encoding="utf-8").startswith("---\nvisit_date")
    # readable by the process, not world-readable
    assert oct(path.stat().st_mode & 0o777) == "0o640"
    assert (
        record_store.read_record(
            "open-house-visitors", "2026-09-13_1420-e-4th-st-tempe_nguyen.md", stores=stores
        )
        == "---\nvisit_date: 2026-09-13\n---\n\nnotes"
    )
    listed = record_store.list_records("open-house-visitors", stores=stores)
    assert [r["name"] for r in listed] == ["2026-09-13_1420-e-4th-st-tempe_nguyen.md"]
    assert listed[0]["bytes" if "bytes" in listed[0] else "size"] > 0


def test_a_second_write_refuses_to_replace_unless_told(stores: dict[str, Path]) -> None:
    record_store.write_record("open-house-visitors", "a.md", "first", stores=stores)
    with pytest.raises(record_store.RecordStoreError, match="already exists"):
        record_store.write_record("open-house-visitors", "a.md", "second", stores=stores)
    assert (stores["open-house-visitors"] / "a.md").read_text() == "first"
    receipt = record_store.write_record(
        "open-house-visitors", "a.md", "second", overwrite=True, stores=stores
    )
    assert receipt["created"] is False
    assert (stores["open-house-visitors"] / "a.md").read_text() == "second"


@pytest.mark.parametrize(
    "name",
    [
        "../escape.md",
        "sub/dir.md",
        "..",
        ".hidden.md",
        "no-extension",
        "shell.sh",
        "/opt/data/customer.yaml",
        "a\x00b.md",
        "x" * 200 + ".md",
    ],
)
def test_a_record_name_that_is_not_a_single_text_file_is_refused(
    stores: dict[str, Path], name: str
) -> None:
    with pytest.raises(record_store.RecordStoreError, match="record name"):
        record_store.write_record("open-house-visitors", name, "x", stores=stores)
    with pytest.raises(record_store.RecordStoreError, match="record name"):
        record_store.read_record("open-house-visitors", name, stores=stores)
    assert not (stores["open-house-visitors"]).exists() or not any(
        stores["open-house-visitors"].iterdir()
    )


def test_a_symlinked_record_that_points_out_of_the_store_is_refused(
    stores: dict[str, Path], tmp_path: Path
) -> None:
    root = stores["open-house-visitors"]
    root.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    os.symlink(outside, root / "link.md")
    with pytest.raises(record_store.RecordStoreError, match="outside its store"):
        record_store.read_record("open-house-visitors", "link.md", stores=stores)
    with pytest.raises(record_store.RecordStoreError, match="outside its store"):
        record_store.write_record(
            "open-house-visitors", "link.md", "x", overwrite=True, stores=stores
        )
    assert outside.read_text() == "secret"


def test_an_unauthored_store_is_refused_by_name(stores: dict[str, Path]) -> None:
    with pytest.raises(record_store.RecordStoreError, match="no record store named 'leads'"):
        record_store.write_record("leads", "a.md", "x", stores=stores)
    with pytest.raises(record_store.RecordStoreError, match="no record store named"):
        record_store.list_records("leads", stores=stores)


def test_empty_and_oversized_content_are_refused(stores: dict[str, Path]) -> None:
    with pytest.raises(record_store.RecordStoreError, match="empty"):
        record_store.write_record("open-house-visitors", "a.md", "  \n", stores=stores)
    with pytest.raises(record_store.RecordStoreError, match="larger than a record"):
        record_store.write_record(
            "open-house-visitors", "a.md", "x" * (record_store.MAX_RECORD_BYTES + 1), stores=stores
        )
    assert not (stores["open-house-visitors"] / "a.md").exists()


def test_listing_skips_dotfiles_temp_files_and_directories(stores: dict[str, Path]) -> None:
    root = stores["open-house-visitors"]
    root.mkdir(parents=True)
    (root / "keep.md").write_text("k")
    (root / ".write-abc.tmp").write_text("partial")
    (root / "sub").mkdir()
    (root / "notes.bin").write_bytes(b"\x00")
    assert [r["name"] for r in record_store.list_records("open-house-visitors", stores=stores)] == [
        "keep.md"
    ]


def test_listing_a_store_that_does_not_exist_yet_is_empty_not_an_error(
    stores: dict[str, Path],
) -> None:
    assert record_store.list_records("open-house-visitors", stores=stores) == []


# ---------------------------------------------------------------------------
# the plugin
# ---------------------------------------------------------------------------


@pytest.fixture()
def store_seat(monkeypatch: pytest.MonkeyPatch, stores: dict[str, Path]) -> dict[str, Path]:
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: dict(stores))
    monkeypatch.setattr(
        record_store,
        "authored_policies",
        lambda *_, **__: {n: record_store.StorePolicy(root=p) for n, p in stores.items()},
    )
    return stores


def test_the_plugin_registers_the_trio_on_a_seat_that_authors_a_store(
    fake_ctx: Any, store_seat: dict[str, Path]
) -> None:
    plugin = load_plugin("hermes-smd-record-store")
    plugin.register(fake_ctx)
    assert set(fake_ctx.tools) == {"record_store_list", "record_store_read", "record_store_write"}
    for entry in fake_ctx.tools.values():
        assert not entry.get("requires_env")
        assert entry["toolset"] == "record_store"
        assert "parameters" in entry["schema"]
        assert entry["schema"]["parameters"]["type"] == "object"
        assert entry["schema"]["description"]
    assert plugin.on_pre_tool_call in fake_ctx.registered["pre_tool_call"]


def test_a_seat_with_no_store_gets_no_tools_and_no_hook(
    fake_ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: {})
    plugin = load_plugin("hermes-smd-record-store")
    plugin.register(fake_ctx)
    assert fake_ctx.tools == {}
    assert fake_ctx.registered == {}


def test_an_unreadable_record_stores_block_registers_nothing(
    fake_ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> dict[str, Path]:
        raise record_store.RecordStoreError("record_stores[0].path: must live under /opt/data")

    monkeypatch.setattr(record_store, "authored_stores", boom)
    plugin = load_plugin("hermes-smd-record-store")
    plugin.register(fake_ctx)
    assert fake_ctx.tools == {}


def test_the_handlers_round_trip_through_json(fake_ctx: Any, store_seat: dict[str, Path]) -> None:
    plugin = load_plugin("hermes-smd-record-store")
    plugin.register(fake_ctx)
    write = fake_ctx.tools["record_store_write"]["handler"]
    read = fake_ctx.tools["record_store_read"]["handler"]
    listing = fake_ctx.tools["record_store_list"]["handler"]
    receipt = json.loads(
        write({"store": "open-house-visitors", "name": "v.md", "content": "notes"})
    )
    assert receipt["created"] is True
    assert json.loads(read({"store": "open-house-visitors", "name": "v.md"}))["content"] == "notes"
    assert json.loads(listing({"store": "open-house-visitors"}))["count"] == 1


def test_a_handler_failure_reaches_the_model_as_a_reason(
    fake_ctx: Any, store_seat: dict[str, Path]
) -> None:
    plugin = load_plugin("hermes-smd-record-store")
    plugin.register(fake_ctx)
    write = fake_ctx.tools["record_store_write"]["handler"]
    with pytest.raises(RuntimeError, match="record name must be a single file name"):
        write({"store": "open-house-visitors", "name": "../x.md", "content": "notes"})


# ---------------------------------------------------------------------------
# the taint fence on the write
# ---------------------------------------------------------------------------


def test_the_write_is_blocked_on_a_tainted_session(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = load_plugin("hermes-smd-record-store")
    monkeypatch.setattr(plugin.SESSION_TAINT, "is_tainted", lambda sid: sid == "tainted")
    blocked = plugin.on_pre_tool_call(tool_name="record_store_write", session_id="tainted", args={})
    assert blocked is not None and blocked["action"] == "block"
    assert "outside the firm" in blocked["message"]
    assert (
        plugin.on_pre_tool_call(tool_name="record_store_write", session_id="clean", args={}) is None
    )
    # reads and lists are never the hook's business
    assert (
        plugin.on_pre_tool_call(tool_name="record_store_read", session_id="tainted", args={})
        is None
    )


def test_an_unresolvable_taint_state_refuses_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = load_plugin("hermes-smd-record-store")

    def boom(_sid: str) -> bool:
        raise RuntimeError("register gone")

    monkeypatch.setattr(plugin.SESSION_TAINT, "is_tainted", boom)
    blocked = plugin.on_pre_tool_call(tool_name="record_store_write", session_id="s", args={})
    assert blocked is not None and blocked["action"] == "block"


# ---------------------------------------------------------------------------
# the owner fence (ss-console#2793 follow-on: the broker view)
#
# Two agents at one brokerage write into the same store. Each opens only their
# own records; the broker (a reader) opens every record and rewrites none. The
# viewer is the VERIFIED inbound sender bound to the session, never a name in
# the prompt. Each test names the direction that would be a defect if flipped.
# ---------------------------------------------------------------------------

AGENT_A = "scott@example.com"
AGENT_B = "tim@example.com"
BROKER = "broker@example.com"


def _private(root: Path) -> record_store.StorePolicy:
    return record_store.StorePolicy(
        root=root,
        owner_field="agent",
        readers=frozenset({BROKER}),
        index=("visitor", "property", "visit_date", "contact"),
    )


def _record(owner: str, visitor: str = "Priya Patel", contact: str = "480-555-0177") -> str:
    return (
        "---\n"
        f"visit_date: 2026-09-27\n"
        f"property: 1420 E 4th St, Tempe\n"
        f"agent: {owner}\n"
        f"visitor: {visitor}\n"
        f"contact: {contact}\n"
        "stated_intent: wants a big yard\n"
        "follow_ups:\n"
        "  - { step: 1, due: 2026-09-29, drafted: null }\n"
        "---\n\n"
        "Agent's notes, verbatim:\n\nPRIVATE NOTES\n"
    )


def test_policy_keys_are_parsed_and_readers_are_lower_cased() -> None:
    cfg = CustomerConfig(
        {
            "customer_id": "scott",
            "record_stores": [
                {
                    "name": "open-house-visitors",
                    "path": "/opt/data/open-house/visitors",
                    "owner_field": "agent",
                    "readers": ["Tim@TheBrokery.com"],
                    "index": ["visitor", "property"],
                }
            ],
        }
    )
    policy = record_store.authored_policies(cfg)["open-house-visitors"]
    assert policy.private and policy.owner_field == "agent"
    assert policy.readers == frozenset({"tim@thebrokery.com"})
    assert policy.index == ("visitor", "property")
    # a store with no owner field is shared, and authored_stores still answers
    shared = record_store.authored_policies(
        CustomerConfig(
            {"customer_id": "s", "record_stores": [{"name": "notes", "path": "/opt/data/n"}]}
        )
    )["notes"]
    assert not shared.private and shared.readers == frozenset()


@pytest.mark.parametrize(
    "entry, fragment",
    [
        ({"owner_field": "Agent Name"}, "owner_field: must be a frontmatter key"),
        ({"readers": ["tim@x.example"]}, "readers: needs owner_field"),
        ({"owner_field": "agent", "readers": ["not-an-address"]}, "is not an email address"),
        ({"owner_field": "agent", "readers": "tim@x.example"}, "readers: must be a list"),
        ({"index": ["Visitor Name"]}, "index: 'Visitor Name' is not a frontmatter key"),
    ],
)
def test_a_bad_policy_key_is_refused_by_name(entry: dict, fragment: str) -> None:
    faults = record_store.policy_problems({"name": "s", "path": "/opt/data/s", **entry})
    assert any(fragment in f for f in faults), faults
    with pytest.raises(record_store.RecordStoreError, match="record_stores\\[0\\]"):
        record_store.authored_policies(
            CustomerConfig(
                {
                    "customer_id": "s",
                    "record_stores": [{"name": "s", "path": "/opt/data/s", **entry}],
                }
            )
        )


def test_frontmatter_reads_scalars_only_and_stops_at_the_second_rule() -> None:
    fm = record_store.read_frontmatter(_record(AGENT_A))
    assert fm["agent"] == AGENT_A and fm["visitor"] == "Priya Patel"
    assert "follow_ups" in fm and fm["follow_ups"] == ""  # a list key carries no scalar
    assert record_store.read_frontmatter("no frontmatter\n---\nagent: x\n") == {}
    assert record_store.read_frontmatter('---\nagent: "Q@X.com"  # note\n---\n') == {
        "agent": "Q@X.com"
    }


def test_the_listing_carries_owner_and_index_for_every_record_but_never_the_notes(
    stores: dict[str, Path],
) -> None:
    policy = _private(stores["open-house-visitors"])
    record_store.write_record("open-house-visitors", "a.md", _record(AGENT_A), stores=stores)
    record_store.write_record(
        "open-house-visitors", "b.md", _record(AGENT_B, "Derek", "480-555-0100"), stores=stores
    )
    rows = {r["name"]: r for r in record_store.list_records("open-house-visitors", policy=policy)}
    assert rows["a.md"]["owner"] == AGENT_A
    assert rows["a.md"]["index"] == {
        "visitor": "Priya Patel",
        "property": "1420 E 4th St, Tempe",
        "visit_date": "2026-09-27",
        "contact": "480-555-0177",
    }
    assert rows["b.md"]["owner"] == AGENT_B and rows["b.md"]["index"]["visitor"] == "Derek"
    assert "PRIVATE NOTES" not in json.dumps(rows)
    # a shared store (no policy) lists as before: name, size, modified only
    plain = record_store.list_records("open-house-visitors", stores=stores)
    assert set(plain[0]) == {"name", "size", "modified"}


def test_the_fence_lets_an_owner_read_their_own_and_refuses_a_colleagues(tmp_path: Path) -> None:
    policy = _private(tmp_path)
    mine = _record(AGENT_A)
    assert record_store.access_problem(policy, action="read", viewer=AGENT_A, existing=mine) is None
    problem = record_store.access_problem(policy, action="read", viewer=AGENT_B, existing=mine)
    assert problem and "belongs to another person" in problem and AGENT_A in problem
    # a reader (the broker) opens anyone's record
    assert record_store.access_problem(policy, action="read", viewer=BROKER, existing=mine) is None
    # case does not decide ownership
    assert (
        record_store.access_problem(policy, action="read", viewer=AGENT_A.upper(), existing=mine)
        is None
    )


def test_the_fence_never_lets_anyone_rewrite_another_persons_record_reader_included(
    tmp_path: Path,
) -> None:
    policy = _private(tmp_path)
    mine = _record(AGENT_A)
    for viewer in (AGENT_B, BROKER):
        problem = record_store.access_problem(
            policy, action="write", viewer=viewer, existing=mine, content=_record(viewer)
        )
        assert problem and "only they may change it" in problem, viewer
    # the owner rewrites their own, stamped with their own address
    assert (
        record_store.access_problem(
            policy, action="write", viewer=AGENT_A, existing=mine, content=_record(AGENT_A)
        )
        is None
    )


def test_a_new_record_must_be_stamped_with_the_writers_own_address(tmp_path: Path) -> None:
    policy = _private(tmp_path)
    forged = record_store.access_problem(
        policy, action="write", viewer=AGENT_B, existing=None, content=_record(AGENT_A)
    )
    assert forged and "must carry agent: tim@example.com" in forged
    unstamped = record_store.access_problem(
        policy, action="write", viewer=AGENT_B, existing=None, content="---\nvisitor: x\n---\nn"
    )
    assert unstamped and "must carry agent:" in unstamped
    assert (
        record_store.access_problem(
            policy, action="write", viewer=AGENT_B, existing=None, content=_record(AGENT_B)
        )
        is None
    )


def test_a_turn_with_no_inbound_origin_and_a_shared_store_are_not_fenced(tmp_path: Path) -> None:
    private = _private(tmp_path)
    mine = _record(AGENT_A)
    # the scheduled follow-up turn reads every agent's records and stamps them
    assert record_store.access_problem(private, action="read", viewer=None, existing=mine) is None
    assert (
        record_store.access_problem(
            private, action="write", viewer=None, existing=mine, content=_record(AGENT_A)
        )
        is None
    )
    shared = record_store.StorePolicy(root=tmp_path)
    assert record_store.access_problem(shared, action="read", viewer=AGENT_B, existing=mine) is None


def _fenced_seat(monkeypatch: pytest.MonkeyPatch, root: Path, *, sender: str | None) -> Any:
    plugin = load_plugin("hermes-smd-record-store")
    policy = _private(root)
    monkeypatch.setattr(record_store, "authored_policies", lambda *_, **__: {"ohv": policy})
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: {"ohv": root})
    monkeypatch.setattr(plugin.SESSION_TAINT, "is_tainted", lambda sid: False)

    class _Origin:
        sender_address = sender or ""

    monkeypatch.setattr(
        plugin.SESSION_INBOUND_ORIGIN, "get", lambda sid: _Origin() if sender else None
    )
    return plugin


def test_the_hook_refuses_a_colleagues_record_by_the_verified_sender(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record_store.write_record("ohv", "a.md", _record(AGENT_A), stores={"ohv": tmp_path})
    plugin = _fenced_seat(monkeypatch, tmp_path, sender=AGENT_B)
    blocked = plugin.on_pre_tool_call(
        tool_name="record_store_read", session_id="s", args={"store": "ohv", "name": "a.md"}
    )
    assert blocked is not None and "belongs to another person" in blocked["message"]
    blocked = plugin.on_pre_tool_call(
        tool_name="record_store_write",
        session_id="s",
        args={"store": "ohv", "name": "a.md", "content": _record(AGENT_B), "overwrite": True},
    )
    assert blocked is not None and "only they may change it" in blocked["message"]
    # Tim's own new record, stamped with his address, passes
    assert (
        plugin.on_pre_tool_call(
            tool_name="record_store_write",
            session_id="s",
            args={"store": "ohv", "name": "b.md", "content": _record(AGENT_B)},
        )
        is None
    )
    # the broker reads Scott's record and is refused the rewrite
    plugin = _fenced_seat(monkeypatch, tmp_path, sender=BROKER)
    assert (
        plugin.on_pre_tool_call(
            tool_name="record_store_read", session_id="s", args={"store": "ohv", "name": "a.md"}
        )
        is None
    )
    blocked = plugin.on_pre_tool_call(
        tool_name="record_store_write",
        session_id="s",
        args={"store": "ohv", "name": "a.md", "content": _record(BROKER), "overwrite": True},
    )
    assert blocked is not None and "only they may change it" in blocked["message"]


def test_the_hook_is_open_on_the_seats_own_turns_and_on_unknown_stores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record_store.write_record("ohv", "a.md", _record(AGENT_A), stores={"ohv": tmp_path})
    plugin = _fenced_seat(monkeypatch, tmp_path, sender=None)  # a scheduled wake
    assert (
        plugin.on_pre_tool_call(
            tool_name="record_store_read", session_id="cron", args={"store": "ohv", "name": "a.md"}
        )
        is None
    )
    plugin = _fenced_seat(monkeypatch, tmp_path, sender=AGENT_B)
    # no such store: the handler's refusal, not the fence's
    assert (
        plugin.on_pre_tool_call(
            tool_name="record_store_read", session_id="s", args={"store": "nope", "name": "a.md"}
        )
        is None
    )
    # listing is never the fence's business
    assert (
        plugin.on_pre_tool_call(
            tool_name="record_store_list", session_id="s", args={"store": "ohv"}
        )
        is None
    )


def test_an_unreadable_policy_refuses_rather_than_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin = load_plugin("hermes-smd-record-store")
    monkeypatch.setattr(plugin.SESSION_TAINT, "is_tainted", lambda sid: False)

    def boom(*_: Any, **__: Any) -> dict:
        raise record_store.RecordStoreError("customer.yaml unreadable")

    monkeypatch.setattr(record_store, "authored_policies", boom)
    blocked = plugin.on_pre_tool_call(
        tool_name="record_store_read", session_id="s", args={"store": "ohv", "name": "a.md"}
    )
    assert blocked is not None and "could not be resolved" in blocked["message"]


def test_the_list_handler_carries_the_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record_store.write_record("ohv", "a.md", _record(AGENT_A), stores={"ohv": tmp_path})
    plugin = _fenced_seat(monkeypatch, tmp_path, sender=AGENT_B)
    out = json.loads(plugin._HANDLERS["record_store_list"]({"store": "ohv"}))
    assert out["policy"] == {
        "owner_field": "agent",
        "readers": [BROKER],
        "index": ["visitor", "property", "visit_date", "contact"],
    }
    assert out["records"][0]["owner"] == AGENT_A
    assert out["records"][0]["index"]["visitor"] == "Priya Patel"
    assert "PRIVATE NOTES" not in json.dumps(out)
