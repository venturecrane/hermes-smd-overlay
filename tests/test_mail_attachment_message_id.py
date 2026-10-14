"""The attachment tools take the turn's own email id, not a retyped one.

THE DEFECT (A&P, 2026-10-14 17:57Z). The turn read a message with its full
Graph id, then retyped the id into ``mail_list_attachments`` with one middle run
missing, and Graph answered ``ErrorInvalidIdMalformed``. A Graph id is ~150
characters of base64 with long repeated runs, which is exactly the string a
model drops a piece of. These tests pin the repair and, for each direction, the
case that would be a defect if it flipped: a DIFFERENT message must never be
rewritten into the turn's own.
"""

from __future__ import annotations

from typing import Any

import pytest

from shared import email_adapter, msgraph_attachments, send_anchor
from tests.conftest import load_plugin

# Shaped like the A&P ids. MANGLED is OWN_ID with one middle run dropped, as the
# 17:57Z call sent it.
OWN_ID = "AAMkADEyOWU0MzEzLWZmMjctAAAAAAEMAABMDdTCAa5HR7muUjCUH-vUAAAp6AuqAAA="
MANGLED = "AAMkADEyOWU0MzEzLWZmMjctAAAp6AuqAAA="
OTHER_ID = "AAMkADEyOWU0MzEzLWZmMjctBBBBBBEMAABMDdTCAa5HR7muUjCUH-vUAAAp6AuoAAA="
# Shorter, same opening and closing, but NOT an in-order subset of OWN_ID ("XX").
NEAR_MISS = "AAMkADEyOWU0MzEzLWZmMjctXXp6AuqAAA="


@pytest.fixture
def graph_tools(fake_ctx: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """A Graph seat whose turn is answering OWN_ID; returns every backend call."""
    monkeypatch.setattr(email_adapter, "email_connector_enabled", lambda *_, **__: True)
    monkeypatch.setattr(
        email_adapter, "email_adapter", lambda *_, **__: email_adapter.ADAPTER_MSGRAPH
    )
    monkeypatch.setattr(
        send_anchor,
        "message_origin",
        lambda *_, **__: {"kind": send_anchor.GRAPH_MESSAGE, "graph_message_id": OWN_ID},
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        msgraph_attachments, "list_attachments", lambda mid: calls.append(("list", mid)) or []
    )
    monkeypatch.setattr(
        msgraph_attachments,
        "spool_attachment",
        lambda mid, aid: calls.append(("spool", mid, aid)) or {"spool_token": "t"},
    )
    monkeypatch.setattr(
        msgraph_attachments,
        "spool_message",
        lambda mid, mailbox=None: calls.append(("eml", mid, str(mailbox))) or {"spool_token": "t"},
    )
    load_plugin("hermes-smd-mail-attachments").register(fake_ctx)
    return calls


def _call(fake_ctx: Any, tool: str, args: dict[str, Any]) -> None:
    fake_ctx.tools[tool]["handler"](args)


def test_a_mangled_copy_of_the_turns_own_id_is_corrected(fake_ctx: Any, graph_tools) -> None:
    _call(fake_ctx, "mail_list_attachments", {"message_id": MANGLED})
    _call(fake_ctx, "mail_spool_attachment", {"message_id": MANGLED, "attachment_id": "a1"})
    _call(fake_ctx, "mail_spool_message", {"message_id": MANGLED})
    assert graph_tools == [("list", OWN_ID), ("spool", OWN_ID, "a1"), ("eml", OWN_ID, "None")]


def test_an_empty_id_means_the_turns_own_email(fake_ctx: Any, graph_tools) -> None:
    _call(fake_ctx, "mail_list_attachments", {})
    _call(fake_ctx, "mail_spool_attachment", {"attachment_id": "a1"})
    assert graph_tools == [("list", OWN_ID), ("spool", OWN_ID, "a1")]


def test_a_different_message_is_never_rewritten(fake_ctx: Any, graph_tools) -> None:
    """A real, different id in the same mailbox passes through, and so does a
    short id that is not an in-order subset of the turn's own."""
    _call(fake_ctx, "mail_list_attachments", {"message_id": OTHER_ID})
    _call(fake_ctx, "mail_list_attachments", {"message_id": NEAR_MISS})
    assert graph_tools == [("list", OTHER_ID), ("list", NEAR_MISS)]


def test_a_staff_mailbox_id_is_never_rewritten(fake_ctx: Any, graph_tools) -> None:
    _call(fake_ctx, "mail_spool_message", {"message_id": MANGLED, "mailbox": "staff@firm.example"})
    assert graph_tools == [("eml", MANGLED, "staff@firm.example")]


def test_no_turn_email_leaves_the_id_as_given(fake_ctx: Any, graph_tools, monkeypatch) -> None:
    """A wake or cron turn has no email of its own: nothing is defaulted or repaired."""
    monkeypatch.setattr(send_anchor, "message_origin", lambda *_, **__: None)
    _call(fake_ctx, "mail_list_attachments", {"message_id": MANGLED})
    _call(fake_ctx, "mail_list_attachments", {})
    assert graph_tools == [("list", MANGLED), ("list", "")]


def test_a_failing_anchor_lookup_is_no_default_not_a_crash(
    fake_ctx: Any, graph_tools, monkeypatch
) -> None:
    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("provenance unavailable")

    monkeypatch.setattr(send_anchor, "message_origin", boom)
    _call(fake_ctx, "mail_list_attachments", {"message_id": MANGLED})
    assert graph_tools == [("list", MANGLED)]


def test_message_id_is_optional_in_every_schema(fake_ctx: Any, graph_tools) -> None:
    for name in ("mail_list_attachments", "mail_spool_attachment", "mail_spool_message"):
        schema = fake_ctx.tools[name]["schema"]
        params = schema.get("parameters", schema)
        assert "message_id" not in params.get("required", []), name
