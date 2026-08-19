"""Tests for the dead-letter replay tool (shared/msgraph_replay.py, overlay#275).

Pins the two load-bearing properties: byte-parity with the poller's own forward
(same envelope bytes, same HMAC, same idempotency key — the replay enters the
model through the SAME fenced door as live mail), and file-lifecycle honesty
(rename only on acceptance; anything else leaves the dead letter in place)."""

from __future__ import annotations

import hashlib
import hmac
import json

from shared import msgraph_replay
from shared.msgraph_poller import MsGraphPoller

_SECRET = "whook-secret"
_MAILBOX = "op@client.example"


def _raw(mid: str, frm: str) -> dict:
    return {
        "id": mid,
        "conversationId": f"conv-{mid}",
        "from": {"emailAddress": {"address": frm}},
        "toRecipients": [{"emailAddress": {"address": _MAILBOX}}],
        "subject": "Hi",
        "body": {"contentType": "Text", "content": "hello"},
        "receivedDateTime": "2026-07-24T10:00:00Z",
    }


def _letter(tmp_path, raw: dict):
    path = tmp_path / "dead-letter" / "abc123.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"reason": "boom", "message_id": raw.get("id", ""), "raw": raw}))
    return path


class _Recorder:
    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.posts: list[dict] = []

    def __call__(self, *, body: bytes, signature: str, request_id: str):
        self.posts.append({"body": body, "signature": signature, "request_id": request_id})
        return self.status


def test_envelope_byte_parity_with_the_pollers_forward(tmp_path):
    # The replayed bytes must be indistinguishable from what the poller itself
    # would have POSTed for the same raw item — same body, HMAC, request id.
    raw = _raw("m1", "greg@wf.example")

    class _Client:
        mailbox = _MAILBOX

        def poll_delta(self, delta_link):
            return [raw], "delta-1", False

    import yaml

    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "customer_id": "acme",
                "connectors": {
                    "Email": {"adapter": "msgraph", "backend": "mcp:msgraph-mail", "enabled": True}
                },
            }
        )
    )
    poller_fwd = _Recorder()
    poller = MsGraphPoller(
        signing_secret=_SECRET,
        yaml_path=str(yaml_path),
        state_path=str(tmp_path / "state.json"),
        client_factory=lambda: _Client(),
        forward_fn=poller_fwd,
    )
    poller._ready()
    poller.poll_once()

    replay_fwd = _Recorder()
    letter = _letter(tmp_path, raw)
    assert (
        msgraph_replay.replay(
            str(letter), mailbox=_MAILBOX, signing_secret=_SECRET, forward_fn=replay_fwd
        )
        == 0
    )
    assert replay_fwd.posts[0]["body"] == poller_fwd.posts[0]["body"]
    assert replay_fwd.posts[0]["signature"] == poller_fwd.posts[0]["signature"]
    assert replay_fwd.posts[0]["request_id"] == poller_fwd.posts[0]["request_id"]
    expected_sig = hmac.new(
        _SECRET.encode(), replay_fwd.posts[0]["body"], hashlib.sha256
    ).hexdigest()
    assert replay_fwd.posts[0]["signature"] == expected_sig


def test_replay_renames_on_acceptance(tmp_path):
    letter = _letter(tmp_path, _raw("m1", "greg@wf.example"))
    assert (
        msgraph_replay.replay(
            str(letter), mailbox=_MAILBOX, signing_secret=_SECRET, forward_fn=_Recorder(202)
        )
        == 0
    )
    assert not letter.exists()
    assert letter.with_suffix(".json.replayed").exists()


def test_replay_leaves_file_on_rejection(tmp_path):
    letter = _letter(tmp_path, _raw("m1", "greg@wf.example"))
    assert (
        msgraph_replay.replay(
            str(letter), mailbox=_MAILBOX, signing_secret=_SECRET, forward_fn=_Recorder(500)
        )
        == 1
    )
    assert letter.exists()  # never destroy the only copy on a rejection


def test_replay_refuses_bad_input(tmp_path):
    missing = str(tmp_path / "nope.json")
    assert (
        msgraph_replay.replay(
            missing, mailbox=_MAILBOX, signing_secret=_SECRET, forward_fn=_Recorder()
        )
        == 2
    )
    idless = _raw("", "greg@wf.example")
    letter = _letter(tmp_path, idless)
    assert (
        msgraph_replay.replay(
            str(letter), mailbox=_MAILBOX, signing_secret=_SECRET, forward_fn=_Recorder()
        )
        == 2
    )
    assert letter.exists()
