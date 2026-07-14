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
def _reset_inbox_cache():
    mod = _load()
    mod._INBOX_ID = None
    yield
    mod._INBOX_ID = None


# ---------------------------------------------------------------------------
# inbox resolution
# ---------------------------------------------------------------------------


def test_resolve_inbox_id_parses_and_caches():
    mod = _load()
    calls = []
    inbox = mod.resolve_inbox_id("am_key", opener=_opener(calls, {"inboxes": [{"inbox_id": "crane@x.agentmail.to"}]}))
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
        api_key="am_key", inbox_id="inbox1", payload=payload, opener=_opener(calls, {"message_id": "msg_123"})
    )
    assert mid == "msg_123"
    sent = json.loads(calls[0].data.decode("utf-8"))
    assert sent == {"to": ["bob@acme.com"], "subject": "Report", "text": "the reviewed body"}
    assert calls[0].get_header("Authorization") == "Bearer am_key"
    assert "/inboxes/inbox1/messages/send" in calls[0].full_url


def test_send_message_refuses_without_recipient():
    mod = _load()
    with pytest.raises(mod.AgentMailSendError):
        mod.send_message(api_key="am_key", inbox_id="inbox1", payload={"subject": "x", "text": "y"}, opener=_opener([], {}))


def test_send_message_tolerates_missing_message_id():
    mod = _load()
    mid = mod.send_message(
        api_key="am_key", inbox_id="inbox1", payload={"to": "a@b.com", "text": "hi"}, opener=_opener([], {})
    )
    assert "sent" in mid


def test_http_error_maps_to_agentmail_send_error():
    import urllib.error

    mod = _load()

    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b""))

    with pytest.raises(mod.AgentMailSendError):
        mod.send_message(api_key="am_key", inbox_id="inbox1", payload={"to": "a@b.com", "text": "hi"}, opener=_boom)
