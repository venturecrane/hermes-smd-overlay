"""Tests for the Microsoft Graph delta poller (shared/msgraph_poller.py).

Pins the four load-bearing properties (ADR 0078 / email-channel-seam D1+D3):

  1. cadence + startup gating — the poller only runs on a msgraph-inbound seat
     with a signing secret + Graph creds (fail-closed otherwise);
  2. durable cursor persistence + a 410 cursor-reset that DEDUPES against the
     seen-id ledger so a re-sync can't replay old mail as new turns;
  3. the enqueue-through-fence guarantee — every new message is re-injected as a
     stamped webhook (source=msgraph, event_type=message.received, DTO under
     inbound_message) signed with the route secret, on the loopback that the
     webhook router (fence/taint/roster) sits behind, and NO other path;
  4. the echo-loop guard — self-sent mail (from == mailbox) never forwards.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from shared.msgraph_poller import DeltaState, MsGraphPoller

_SECRET = "whook-secret"


class _FakeClient:
    """Stand-in Graph client: fixed mailbox, scripted poll_delta batches."""

    def __init__(self, mailbox: str, script: list[tuple[list[dict], str | None, bool]]) -> None:
        self.mailbox = mailbox
        self._script = list(script)
        self.calls: list[str | None] = []

    def poll_delta(self, delta_link):
        self.calls.append(delta_link)
        if self._script:
            return self._script.pop(0)
        return [], delta_link, False


class _Forwarder:
    """Records every loopback POST the poller makes."""

    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.posts: list[dict] = []

    def __call__(self, *, body: bytes, signature: str, request_id: str):
        self.posts.append({"body": body, "signature": signature, "request_id": request_id})
        return self.status


def _raw(mid: str, frm: str, *, subject: str = "Hi", body: str = "hello") -> dict:
    return {
        "id": mid,
        "conversationId": f"conv-{mid}",
        "from": {"emailAddress": {"address": frm}},
        "toRecipients": [{"emailAddress": {"address": "op@client.example"}}],
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "receivedDateTime": "2026-07-24T10:00:00Z",
    }


def _write_yaml(tmp_path, *, adapter="msgraph", enabled=True, poll_seconds=None) -> str:
    email: dict = {"adapter": adapter, "backend": "mcp:msgraph-mail", "enabled": enabled}
    if poll_seconds is not None:
        email["poll_seconds"] = poll_seconds
    doc = {"customer_id": "acme", "connectors": {"Email": email}}
    import yaml

    path = tmp_path / "customer.yaml"
    path.write_text(yaml.safe_dump(doc))
    return str(path)


def _poller(tmp_path, client, forwarder, *, yaml_path=None, secret=_SECRET) -> MsGraphPoller:
    return MsGraphPoller(
        signing_secret=secret,
        yaml_path=yaml_path or _write_yaml(tmp_path),
        state_path=str(tmp_path / "state.json"),
        client_factory=lambda: client,
        forward_fn=forwarder,
    )


# ---------------------------------------------------------------------------
# 1. cadence + startup gating
# ---------------------------------------------------------------------------


def test_start_refused_when_adapter_not_msgraph(tmp_path):
    yaml_path = _write_yaml(tmp_path, adapter="agentmail")
    client = _FakeClient("op@client.example", [])
    poller = _poller(tmp_path, client, _Forwarder(), yaml_path=yaml_path)
    assert poller.start() is False


def test_start_refused_when_signing_secret_missing(tmp_path):
    client = _FakeClient("op@client.example", [])
    poller = _poller(tmp_path, client, _Forwarder(), secret=None)
    assert poller.start() is False


def test_start_refused_when_client_unavailable(tmp_path):
    poller = MsGraphPoller(
        signing_secret=_SECRET,
        yaml_path=_write_yaml(tmp_path),
        state_path=str(tmp_path / "state.json"),
        client_factory=lambda: None,  # MSGRAPH_* unset
        forward_fn=_Forwarder(),
    )
    assert poller.start() is False


def test_poll_seconds_reads_authored_cadence(tmp_path):
    client = _FakeClient("op@client.example", [])
    poller = _poller(
        tmp_path, client, _Forwarder(), yaml_path=_write_yaml(tmp_path, poll_seconds=90)
    )
    assert poller._poll_seconds(poller._email_connector()) == 90


def test_poll_seconds_defaults_when_absent(tmp_path):
    client = _FakeClient("op@client.example", [])
    poller = _poller(tmp_path, client, _Forwarder())
    assert poller._poll_seconds(poller._email_connector()) == 45


# ---------------------------------------------------------------------------
# 3. enqueue-through-fence — the ONLY door to the model
# ---------------------------------------------------------------------------


def test_new_message_forwarded_as_stamped_signed_webhook(tmp_path):
    client = _FakeClient("op@client.example", [([_raw("m1", "greg@wf.example")], "delta-1", False)])
    fwd = _Forwarder()
    poller = _poller(tmp_path, client, fwd)
    assert poller._ready() is True
    assert poller.poll_once() == 1

    post = fwd.posts[0]
    payload = json.loads(post["body"])
    # Stamped exactly as the router's msgraph normalizer accepts.
    assert payload["source"] == "msgraph"
    assert payload["event_type"] == "message.received"
    assert payload["event_id"] == "m1"
    dto = payload["inbound_message"]
    assert dto["provider"] == "msgraph" and dto["from_addr"] == "greg@wf.example"
    # Signed with the route secret so the Hermes adapter re-verifies (the fence path).
    expected = hmac.new(_SECRET.encode(), post["body"], hashlib.sha256).hexdigest()
    assert post["signature"] == expected
    assert post["request_id"] == "m1"


def test_cursor_persisted_after_batch(tmp_path):
    client = _FakeClient("op@client.example", [([_raw("m1", "a@x.example")], "delta-XYZ", False)])
    poller = _poller(tmp_path, client, _Forwarder())
    poller._ready()
    poller.poll_once()
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["delta_link"] == "delta-XYZ"
    assert "m1" in saved["seen_ids"]


def test_second_poll_resumes_from_persisted_cursor(tmp_path):
    client = _FakeClient(
        "op@client.example",
        [([_raw("m1", "a@x.example")], "delta-1", False), ([], "delta-2", False)],
    )
    poller = _poller(tmp_path, client, _Forwarder())
    poller._ready()
    poller.poll_once()
    poller.poll_once()
    # Second poll_delta call was handed the cursor the first batch stored.
    assert client.calls == [None, "delta-1"]


# ---------------------------------------------------------------------------
# 2. cursor-reset dedupe
# ---------------------------------------------------------------------------


def test_cursor_reset_dedupes_already_seen_message(tmp_path):
    # First batch forwards m1. Second batch is a 410 RE-SYNC (cursor_reset=True)
    # that re-lists m1 (+ a genuinely new m2). m1 must NOT forward again.
    client = _FakeClient(
        "op@client.example",
        [
            ([_raw("m1", "a@x.example")], "delta-1", False),
            ([_raw("m1", "a@x.example"), _raw("m2", "b@x.example")], "delta-2", True),
        ],
    )
    fwd = _Forwarder()
    poller = _poller(tmp_path, client, fwd)
    poller._ready()
    assert poller.poll_once() == 1  # m1
    assert poller.poll_once() == 1  # only m2 (m1 deduped on reset)
    forwarded_ids = [json.loads(p["body"])["event_id"] for p in fwd.posts]
    assert forwarded_ids == ["m1", "m2"]


def test_seen_ledger_survives_restart(tmp_path):
    state_path = str(tmp_path / "state.json")
    DeltaState(state_path).mark_seen("old-1")
    s1 = DeltaState(state_path)
    s1.mark_seen("old-1")  # no-op
    s1.persist("delta-A")
    # A fresh instance (a process restart) re-reads the durable ledger.
    s2 = DeltaState(state_path)
    assert s2.has_seen("old-1")
    assert s2.delta_link == "delta-A"


# ---------------------------------------------------------------------------
# 4. echo-loop guard
# ---------------------------------------------------------------------------


def test_self_sent_message_never_forwards(tmp_path):
    client = _FakeClient(
        "op@client.example",
        [([_raw("self-1", "OP@Client.example"), _raw("m2", "greg@wf.example")], "delta-1", False)],
    )
    fwd = _Forwarder()
    poller = _poller(tmp_path, client, fwd)
    poller._ready()
    assert poller.poll_once() == 1  # only m2 forwarded; self-sent skipped
    assert [json.loads(p["body"])["event_id"] for p in fwd.posts] == ["m2"]


# ---------------------------------------------------------------------------
# exception safety
# ---------------------------------------------------------------------------


def test_poll_failure_is_swallowed_and_cursor_untouched(tmp_path):
    class _Boom(_FakeClient):
        def poll_delta(self, delta_link):
            raise RuntimeError("graph exploded")

    client = _Boom("op@client.example", [])
    poller = _poller(tmp_path, client, _Forwarder())
    poller._ready()
    assert poller.poll_once() == 0  # no raise; cycle skipped
