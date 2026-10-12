"""Tests for the dead-letter replay tool (shared/msgraph_replay.py, overlay#275).

Pins the two load-bearing properties: byte-parity with the poller's own forward
(same envelope bytes, the same HMAC V2 scheme under the route secret, same idempotency key — the replay enters the
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
    """Replay-side forward double: returns (status, body) like ``_post_forward``."""

    def __init__(self, status: int = 202, payload: bytes | None = None) -> None:
        self.status = status
        self.payload = (
            payload
            if payload is not None
            else json.dumps({"status": "accepted", "route": "msgraph"}).encode()
        )
        self.posts: list[dict] = []

    def __call__(self, *, body: bytes, signature: str, timestamp: str, request_id: str):
        self.posts.append(
            {"body": body, "signature": signature, "timestamp": timestamp, "request_id": request_id}
        )
        return self.status, self.payload


class _PollerRecorder(_Recorder):
    """Poller-side forward double: the poller's forward contract is status-only."""

    def __call__(self, **kw):
        return super().__call__(**kw)[0]


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
    poller_fwd = _PollerRecorder()
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
    assert replay_fwd.posts[0]["request_id"] == poller_fwd.posts[0]["request_id"]
    # The signature is the adapter's HMAC V2 over "<timestamp>.<bytes>", so it is
    # bound to each POST's own timestamp rather than byte-equal across the two
    # forwards; parity is the body and the idempotency key, and the replay's own
    # signature must verify for the timestamp it sent.
    replayed = replay_fwd.posts[0]
    expected_sig = hmac.new(
        _SECRET.encode(), replayed["timestamp"].encode() + b"." + replayed["body"], hashlib.sha256
    ).hexdigest()
    assert replayed["signature"] == expected_sig


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


# ---------------------------------------------------------------------------
# A 2xx is not a delivery: the Hermes adapter's idempotency cache answers
# HTTP 200 {"status":"duplicate","delivery_id":...} (hermes
# gateway/platforms/webhook.py, pinned v2026.9.14@345cd2b0, line ~611) and
# starts NO turn. Before this fix the replay renamed the file to .replayed and
# exited 0 on that answer: a dropped replay reported as delivered.
# ---------------------------------------------------------------------------

_DUPLICATE_BODY = b'{"status": "duplicate", "delivery_id": "x"}'


def _serve_once(status: int, payload: bytes):
    """A real loopback HTTP server standing in for the adapter (one answer)."""
    import http.server
    import threading

    seen: list[dict] = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — stdlib hook name
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({"path": self.path, "body": self.rfile.read(length)})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return srv, seen


def test_replay_duplicate_200_from_the_real_forward_is_not_a_delivery(
    tmp_path, monkeypatch, caplog
):
    # End to end through the DEFAULT forward (no injected double): a real HTTP
    # 200 duplicate from the loopback must exit 1 and leave the dead letter.
    from shared import msgraph_poller

    srv, seen = _serve_once(200, _DUPLICATE_BODY)
    monkeypatch.setattr(msgraph_poller, "_GATEWAY_PORT", srv.server_address[1])
    letter = _letter(tmp_path, _raw("m1", "greg@wf.example"))
    try:
        with caplog.at_level("ERROR", logger="hermes-smd-msgraph-replay"):
            rc = msgraph_replay.replay(str(letter), mailbox=_MAILBOX, signing_secret=_SECRET)
    finally:
        srv.server_close()
    assert seen and seen[0]["path"] == "/webhooks/msgraph"
    assert rc == 1
    assert letter.exists()
    assert not letter.with_suffix(".json.replayed").exists()
    assert "duplicate" in caplog.text and "NOT delivered" in caplog.text
    assert "3600s" in caplog.text  # says when the cache entry expires


def test_replay_duplicate_from_a_double_leaves_file(tmp_path):
    letter = _letter(tmp_path, _raw("m1", "greg@wf.example"))
    rc = msgraph_replay.replay(
        str(letter),
        mailbox=_MAILBOX,
        signing_secret=_SECRET,
        forward_fn=_Recorder(200, _DUPLICATE_BODY),
    )
    assert rc == 1
    assert letter.exists()


def test_replay_ignored_200_is_not_a_delivery(tmp_path):
    letter = _letter(tmp_path, _raw("m1", "greg@wf.example"))
    rc = msgraph_replay.replay(
        str(letter),
        mailbox=_MAILBOX,
        signing_secret=_SECRET,
        forward_fn=_Recorder(200, b'{"status": "ignored", "reason": "filter"}'),
    )
    assert rc == 1
    assert letter.exists()


def test_post_forward_surfaces_the_adapter_body(monkeypatch):
    from shared import msgraph_poller

    srv, _ = _serve_once(202, b'{"status": "accepted"}')
    monkeypatch.setattr(msgraph_poller, "_GATEWAY_PORT", srv.server_address[1])
    try:
        status, payload = msgraph_poller._post_forward(
            body=b"{}", signature="s", timestamp="1", request_id="r"
        )
    finally:
        srv.server_close()
    assert status == 202
    assert json.loads(payload) == {"status": "accepted"}


def test_poller_default_forward_contract_is_unchanged(monkeypatch):
    # The poller keeps reading the adapter's duplicate 200 as accepted (see
    # _default_forward's docstring): its forward still returns the bare status.
    from shared import msgraph_poller

    srv, _ = _serve_once(200, _DUPLICATE_BODY)
    monkeypatch.setattr(msgraph_poller, "_GATEWAY_PORT", srv.server_address[1])
    try:
        status = msgraph_poller._default_forward(
            body=b"{}", signature="s", timestamp="1", request_id="r"
        )
    finally:
        srv.server_close()
    assert status == 200
