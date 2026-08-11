"""Tests for the out-of-band AgentMail send transport (ADR 0071 / #1806 harden).

Transport only — the REST call, inbox resolution, body allowlisting, and error
mapping. No network: the ``opener`` is injected. Authorization (the gate) is
tested separately; this module never decides whether a send is allowed.
"""

from __future__ import annotations

import io
import json

import pytest

from tests.conftest import load_plugin


def _load():
    return load_plugin("hermes-smd-trust").outbound_send


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8") if payload is not None else b""

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(captured, payload):
    """An injectable urlopen that records the request and returns ``payload``."""

    def _open(req, timeout=None):
        captured.append(req)
        return _Resp(payload)

    return _open


@pytest.fixture(autouse=True)
def _reset_inbox_cache(monkeypatch):
    mod = _load()
    mod._INBOX_ID_BY_ADDRESS.clear()
    # Pin the seat's identity for every test. Without this the module would fall
    # back to the <slug>@agentmail.to convention off the ambient environment, and
    # the suite's verdict would depend on the developer's shell (ss#2258).
    monkeypatch.setenv("AGENTMAIL_INBOX_ADDRESS", "crane@x.agentmail.to")
    yield
    mod._INBOX_ID_BY_ADDRESS.clear()


# ---------------------------------------------------------------------------
# inbox resolution
# ---------------------------------------------------------------------------


def test_resolve_inbox_id_parses_and_caches():
    mod = _load()
    calls = []
    inbox = mod.resolve_inbox_id(
        "am_key", opener=_opener(calls, {"inboxes": [{"inbox_id": "crane@x.agentmail.to"}]})
    )
    assert inbox == "crane@x.agentmail.to"
    # second call is cached — no second HTTP request.
    inbox2 = mod.resolve_inbox_id("am_key", opener=_opener(calls, {"inboxes": []}))
    assert inbox2 == "crane@x.agentmail.to"
    assert len(calls) == 1


def test_resolve_inbox_id_errors_on_no_inbox():
    mod = _load()
    with pytest.raises(mod.AgentMailSendError):
        mod.resolve_inbox_id("am_key", opener=_opener([], {"inboxes": []}))


# ---------------------------------------------------------------------------
# ss#2258 — the seat sends from ITS OWN inbox, or not at all
# ---------------------------------------------------------------------------


def test_resolve_picks_its_own_inbox_not_the_first():
    """The regression. The account listing is newest-first and account-wide: on
    2026-08-11 it held 8 inboxes with the pilot seat's own at index 6. Taking
    inboxes[0] meant sending from whichever inbox was created most recently."""
    mod = _load()
    listing = {
        "inboxes": [
            {"inbox_id": "ss-probe-admin@agentmail.to"},
            {"inbox_id": "sim-opposing-counsel@agentmail.to"},
            {"inbox_id": "crane@x.agentmail.to"},
            {"inbox_id": "another-firm@agentmail.to"},
        ]
    }
    assert mod.resolve_inbox_id("am_key", opener=_opener([], listing)) == "crane@x.agentmail.to"


def test_resolve_refuses_when_its_own_inbox_is_absent():
    """Fail closed. Sending from another firm's mailbox is worse than not sending,
    so a listing without this seat's address raises instead of falling back."""
    mod = _load()
    listing = {
        "inboxes": [
            {"inbox_id": "ss-probe-admin@agentmail.to"},
            {"inbox_id": "another-firm@agentmail.to"},
        ]
    }
    with pytest.raises(mod.AgentMailSendError, match="not in the AgentMail account listing"):
        mod.resolve_inbox_id("am_key", opener=_opener([], listing))


def test_resolve_refuses_when_identity_is_unknowable(monkeypatch):
    """No authored address and no slug ⇒ the seat cannot know which mailbox is
    its own, so it must not pick one."""
    mod = _load()
    monkeypatch.delenv("AGENTMAIL_INBOX_ADDRESS", raising=False)
    monkeypatch.delenv("SMD_CUSTOMER_SLUG", raising=False)
    monkeypatch.delenv("CUSTOMER_SLUG", raising=False)
    with pytest.raises(mod.AgentMailSendError, match="Refusing to guess"):
        mod.resolve_inbox_id("am_key", opener=_opener([], {"inboxes": [{"inbox_id": "x@y"}]}))


def test_seat_address_falls_back_to_slug_convention(monkeypatch):
    mod = _load()
    monkeypatch.delenv("AGENTMAIL_INBOX_ADDRESS", raising=False)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "ashton-price")
    assert mod.seat_inbox_address() == "ashton-price@agentmail.to"


def test_authored_address_wins_over_the_convention(monkeypatch):
    """A seat whose inbox does not follow the convention can still be pinned
    without a code change."""
    mod = _load()
    monkeypatch.setenv("AGENTMAIL_INBOX_ADDRESS", "odd-name@agentmail.to")
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "ashton-price")
    assert mod.seat_inbox_address() == "odd-name@agentmail.to"


def test_cache_is_keyed_by_address(monkeypatch):
    """A cache hit must never answer for a different address than was asked."""
    mod = _load()
    calls = []
    first = {"inboxes": [{"inbox_id": "crane@x.agentmail.to"}]}
    assert mod.resolve_inbox_id("am_key", opener=_opener(calls, first)) == "crane@x.agentmail.to"
    monkeypatch.setenv("AGENTMAIL_INBOX_ADDRESS", "other@agentmail.to")
    second = {"inboxes": [{"inbox_id": "other@agentmail.to"}]}
    assert mod.resolve_inbox_id("am_key", opener=_opener(calls, second)) == "other@agentmail.to"
    assert len(calls) == 2  # the second address did NOT read the first one's cache


# ---------------------------------------------------------------------------
# send body allowlisting + POST
# ---------------------------------------------------------------------------


def test_send_message_forwards_only_allowlisted_fields():
    mod = _load()
    calls = []
    payload = {
        "to": ["bob@acme.com"],
        "subject": "Report",
        "text": "the reviewed body",
        "_current_turn_approval": True,  # internal flag — must NOT be forwarded
        "_workspace_grant": "xyz",  # broker grant — must NOT be forwarded
        "tool_call_id": "abc",  # noise — must NOT be forwarded
    }
    mid = mod.send_message(
        api_key="am_key",
        inbox_id="inbox1",
        payload=payload,
        opener=_opener(calls, {"message_id": "msg_123"}),
    )
    assert mid == "msg_123"
    sent = json.loads(calls[0].data.decode("utf-8"))
    assert sent == {"to": ["bob@acme.com"], "subject": "Report", "text": "the reviewed body"}
    assert calls[0].get_header("Authorization") == "Bearer am_key"
    assert "/inboxes/inbox1/messages/send" in calls[0].full_url


def test_send_message_refuses_without_recipient():
    mod = _load()
    with pytest.raises(mod.AgentMailSendError):
        mod.send_message(
            api_key="am_key",
            inbox_id="inbox1",
            payload={"subject": "x", "text": "y"},
            opener=_opener([], {}),
        )


def test_send_message_tolerates_missing_message_id():
    mod = _load()
    mid = mod.send_message(
        api_key="am_key",
        inbox_id="inbox1",
        payload={"to": "a@b.com", "text": "hi"},
        opener=_opener([], {}),
    )
    assert "sent" in mid


def test_http_error_maps_to_agentmail_send_error():
    import urllib.error

    mod = _load()

    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b""))

    with pytest.raises(mod.AgentMailSendError):
        mod.send_message(
            api_key="am_key",
            inbox_id="inbox1",
            payload={"to": "a@b.com", "text": "hi"},
            opener=_boom,
        )
