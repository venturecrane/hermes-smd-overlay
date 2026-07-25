"""Tests for the normalized inbound-mail seam (ADR 0078 / email-channel-seam D2).

Covers the ``shared.inbound_message`` DTO + provider normalizer registry:

  * AgentMail normalization from a Svix-shaped payload (adapter #1) — the same
    field extraction the live router used, now producing the DTO.
  * msgraph acceptance of an already-DTO-shaped dict + strict rejection.
  * Unknown source → None (fail-closed: a channel with no normalizer has no door).
  * Address lowercasing/bareing; missing fields DEGRADE (''/None/[]), never invented.
"""

from __future__ import annotations

from shared import inbound_message as im

# ---------------------------------------------------------------------------
# AgentMail adapter (#1)
# ---------------------------------------------------------------------------


def test_agentmail_normalizes_svix_data_envelope() -> None:
    """The Svix envelope (gate-stamped): message fields sit under ``data``."""
    payload = {
        "source": "agentmail",
        "event_type": "message.received",
        "data": {
            "inbox_id": "inbox_abc",
            "message_id": "msg_777",
            "thread_id": "thr_1",
            "from": "Greg Whitfield <greg@whitfield.example>",
            "subject": "New matter",
            "text": "I'd like to discuss a new matter.",
            "to": ["ops@firm.agents.smd.services"],
            "cc": [],
            "timestamp": "2026-07-24T12:00:00.000Z",
        },
    }
    dto = im.normalize_inbound("agentmail", payload)
    assert dto is not None
    assert dto.provider == "agentmail"
    assert dto.from_addr == "greg@whitfield.example"
    assert dto.message_id == "msg_777"
    assert dto.mailbox == "inbox_abc"
    assert dto.thread_ref == "thr_1"
    assert dto.subject == "New matter"
    assert dto.body_text == "I'd like to discuss a new matter."
    assert dto.to == ["ops@firm.agents.smd.services"]
    assert dto.cc == []
    assert dto.received_at == "2026-07-24T12:00:00.000Z"
    # The reply anchor the AgentMail transport threads on rides provider_refs.
    assert dto.provider_refs["inbox_id"] == "inbox_abc"
    assert dto.provider_refs["message_id"] == "msg_777"


def test_agentmail_normalizes_message_block() -> None:
    """The vendor webhook shape: fields under ``message`` rather than ``data``."""
    payload = {
        "source": "agentmail",
        "event_type": "message.received",
        "message": {
            "inbox_id": "inbox_9",
            "message_id": "msg_1",
            "from": "jane@example.com",
        },
    }
    dto = im.normalize_inbound("agentmail", payload)
    assert dto is not None
    assert dto.from_addr == "jane@example.com"
    assert dto.message_id == "msg_1"


def test_agentmail_display_name_bared_and_lowercased() -> None:
    payload = {"data": {"from": "Greg Whitfield <GREG@Whitfield.Example>", "message_id": "m"}}
    dto = im.normalize_inbound("agentmail", payload)
    assert dto is not None
    assert dto.from_addr == "greg@whitfield.example"


def test_agentmail_missing_fields_degrade_not_invent() -> None:
    """A sparse message block degrades every absent field to an empty value —
    never a placeholder, never a guess."""
    payload = {"data": {"from": "solo@example.com"}}
    dto = im.normalize_inbound("agentmail", payload)
    assert dto is not None
    assert dto.from_addr == "solo@example.com"
    assert dto.message_id == ""
    assert dto.mailbox == ""
    assert dto.thread_ref is None
    assert dto.subject == ""
    assert dto.body_text == ""
    assert dto.to == []
    assert dto.cc == []
    assert dto.received_at == ""


def test_agentmail_unparseable_payload_returns_none() -> None:
    # No message/data block resolves ⇒ None (fail toward quarantine).
    assert im.normalize_inbound("agentmail", {"source": "agentmail", "body": "x"}) is None
    assert im.normalize_inbound("agentmail", {"event_type": "message.received"}) is None


def test_agentmail_to_dict_carries_every_field() -> None:
    payload = {"data": {"from": "a@b.com", "message_id": "m", "inbox_id": "i", "text": "hi"}}
    dto = im.normalize_inbound("agentmail", payload)
    assert dto is not None
    d = dto.to_dict()
    assert set(d) == {
        "provider",
        "mailbox",
        "message_id",
        "thread_ref",
        "from_addr",
        "to",
        "cc",
        "subject",
        "body_text",
        "received_at",
        "provider_refs",
    }
    assert d["provider"] == "agentmail"
    assert d["from_addr"] == "a@b.com"
    assert d["provider_refs"]["inbox_id"] == "i"


def test_agentmail_multiple_recipients_bared() -> None:
    payload = {
        "data": {
            "from": "a@b.com",
            "message_id": "m",
            "to": ["First Last <ONE@X.com>", "two@y.com", "not-an-address"],
            "cc": "CC Person <cc@z.com>",
        }
    }
    dto = im.normalize_inbound("agentmail", payload)
    assert dto is not None
    assert dto.to == ["one@x.com", "two@y.com"]  # unparseable entry dropped
    assert dto.cc == ["cc@z.com"]


# ---------------------------------------------------------------------------
# Microsoft Graph adapter (#2) — accepts the connector's DTO-shaped dict
# ---------------------------------------------------------------------------


def _msgraph_dto_dict() -> dict:
    return {
        "provider": "msgraph",
        "mailbox": "operator@clientdomain.com",
        "message_id": "AAMkAGI2...",
        "thread_ref": "AAQkAGI2...",
        "from_addr": "client@theirfirm.com",
        "to": ["operator@clientdomain.com"],
        "cc": [],
        "subject": "Re: intake",
        "body_text": "Thanks for the update.",
        "received_at": "2026-07-24T18:30:00Z",
        "provider_refs": {"conversation_id": "AAQkAGI2...", "internet_message_id": "<abc@x>"},
    }


def test_msgraph_accepts_dto_at_root() -> None:
    dto = im.normalize_inbound("msgraph", _msgraph_dto_dict())
    assert dto is not None
    assert dto.provider == "msgraph"
    assert dto.from_addr == "client@theirfirm.com"
    assert dto.message_id == "AAMkAGI2..."
    assert dto.thread_ref == "AAQkAGI2..."
    assert dto.provider_refs["conversation_id"] == "AAQkAGI2..."


def test_msgraph_accepts_dto_nested_under_inbound_message() -> None:
    payload = {"source": "msgraph", "event_type": "message.received"}
    payload["inbound_message"] = _msgraph_dto_dict()
    dto = im.normalize_inbound("msgraph", payload)
    assert dto is not None
    assert dto.message_id == "AAMkAGI2..."


def test_msgraph_strict_rejects_missing_provider() -> None:
    d = _msgraph_dto_dict()
    del d["provider"]
    assert im.normalize_inbound("msgraph", d) is None


def test_msgraph_strict_rejects_wrong_provider() -> None:
    d = _msgraph_dto_dict()
    d["provider"] = "agentmail"
    assert im.normalize_inbound("msgraph", d) is None


def test_msgraph_strict_rejects_missing_message_id() -> None:
    d = _msgraph_dto_dict()
    d["message_id"] = ""
    assert im.normalize_inbound("msgraph", d) is None


def test_msgraph_strict_rejects_missing_from_addr() -> None:
    d = _msgraph_dto_dict()
    del d["from_addr"]
    assert im.normalize_inbound("msgraph", d) is None


def test_msgraph_degrades_optional_fields() -> None:
    dto = im.normalize_inbound(
        "msgraph",
        {"provider": "msgraph", "message_id": "m", "from_addr": "CLIENT@X.com"},
    )
    assert dto is not None
    assert dto.from_addr == "client@x.com"
    assert dto.subject == ""
    assert dto.body_text == ""
    assert dto.to == []
    assert dto.thread_ref is None
    assert dto.provider_refs == {}


# ---------------------------------------------------------------------------
# Registry / entry point
# ---------------------------------------------------------------------------


def test_normalize_inbound_unknown_source_returns_none() -> None:
    # Fail-closed: a source with no normalizer has no seam door.
    assert im.normalize_inbound("smokeball", {"data": {"from": "a@b.com"}}) is None
    assert im.normalize_inbound("", {}) is None


def test_normalize_inbound_dispatches_by_source() -> None:
    # The agentmail payload shape is NOT a valid msgraph DTO, and vice-versa —
    # each source routes to its own normalizer.
    agentmail_payload = {"data": {"from": "a@b.com", "message_id": "m"}}
    assert im.normalize_inbound("agentmail", agentmail_payload) is not None
    assert im.normalize_inbound("msgraph", agentmail_payload) is None


def test_has_normalizer() -> None:
    assert im.has_normalizer("agentmail")
    assert im.has_normalizer("msgraph")
    assert not im.has_normalizer("smokeball")
    assert not im.has_normalizer(None)


def test_normalizer_never_raises_on_weird_payload() -> None:
    # normalize_inbound wraps the normalizer in try/except: an unexpected shape
    # fails toward None, never a crash into the dispatch path.
    for weird in (None, 42, "string", [], {"data": 5}, {"message": ["x"]}):
        assert im.normalize_inbound("agentmail", weird) is None
        assert im.normalize_inbound("msgraph", weird) is None


def test_accepted_providers_match_registry() -> None:
    # The closed provider vocabulary and the normalizer registry stay in lock-step.
    assert set(im.NORMALIZERS) == im.ACCEPTED_PROVIDERS
