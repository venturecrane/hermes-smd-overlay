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
