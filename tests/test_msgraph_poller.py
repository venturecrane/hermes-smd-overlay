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


def _rid(message_id: str) -> str:
    """The poller's X-Request-ID for a message: sha256 hex (collision-safe, 64 chars)."""
    return hashlib.sha256(message_id.encode()).hexdigest()


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


def _poller(
    tmp_path, client, forwarder, *, yaml_path=None, secret=_SECRET, **kwargs
) -> MsGraphPoller:
    return MsGraphPoller(
        signing_secret=secret,
        yaml_path=yaml_path or _write_yaml(tmp_path),
        state_path=str(tmp_path / "state.json"),
        client_factory=lambda: client,
        forward_fn=forwarder,
        **kwargs,
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
    # The idempotency key is a HASH of the message id, never a prefix truncation —
    # Graph ids vary at the END, so a [:64] prefix can collide across messages.
    assert post["request_id"] == _rid("m1")
    assert len(post["request_id"]) == 64


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


# ---------------------------------------------------------------------------
# overlay#275 — a per-item failure must HOLD the cursor, never orphan the item
# ---------------------------------------------------------------------------


class _FlakyForwarder(_Forwarder):
    """Raises (or rejects) the first ``fail_first`` POSTs, then succeeds.

    Models the observed overlay#275 trigger: the poller's first cycle firing
    before the gate's HTTP server bound (connection refused)."""

    def __init__(self, fail_first: int = 1, *, reject_status: int | None = None) -> None:
        super().__init__()
        self._failures_left = fail_first
        self._reject_status = reject_status

    def __call__(self, *, body: bytes, signature: str, request_id: str):
        if self._failures_left > 0:
            self._failures_left -= 1
            if self._reject_status is not None:
                self.posts.append({"body": body, "signature": signature, "request_id": request_id})
                return self._reject_status
            raise ConnectionRefusedError("gate not up yet")
        return super().__call__(body=body, signature=signature, request_id=request_id)


def test_failed_item_holds_cursor_and_is_forwarded_exactly_once_next_cycle(tmp_path):
    # Cycle 1: forward raises (gate not bound) — the message must NOT be lost:
    # cursor stays put, so cycle 2 (old cursor re-lists the message) forwards it.
    msg = _raw("m1", "greg@wf.example")
    client = _FakeClient(
        "op@client.example",
        [([msg], "delta-1", False), ([msg], "delta-2", False)],
    )
    fwd = _FlakyForwarder(fail_first=1)
    poller = _poller(tmp_path, client, fwd)
    poller._ready()

    assert poller.poll_once() == 0  # failure swallowed, nothing forwarded
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["delta_link"] is None  # cursor HELD — not advanced past m1
    assert "m1" not in saved["seen_ids"]  # unhandled item is not "seen"
    assert client.calls == [None]

    assert poller.poll_once() == 1  # retry succeeds
    assert [json.loads(p["body"])["event_id"] for p in fwd.posts] == ["m1"]  # exactly once
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["delta_link"] == "delta-2"  # cursor advances only after success
    assert "m1" in saved["seen_ids"]
    assert client.calls == [None, None]  # cycle 2 re-polled from the held cursor


def test_partial_batch_failure_persists_handled_items_but_holds_cursor(tmp_path):
    # m1 forwards, m2 fails: the seen ledger must durably record m1 (so the retry
    # cycle dedupes it) while the cursor holds (so m2 is re-listed, not orphaned).
    m1, m2 = _raw("m1", "a@x.example"), _raw("m2", "b@x.example")

    class _FailSecond(_Forwarder):
        m2_failures = 1

        def __call__(self, *, body: bytes, signature: str, request_id: str):
            if json.loads(body)["event_id"] == "m2" and self.m2_failures > 0:
                self.m2_failures -= 1
                raise ConnectionRefusedError("blip on m2 only")
            return super().__call__(body=body, signature=signature, request_id=request_id)

    client = _FakeClient(
        "op@client.example",
        [([m1, m2], "delta-1", False), ([m1, m2], "delta-1b", False)],
    )
    fwd = _FailSecond()
    poller = _poller(tmp_path, client, fwd)
    poller._ready()

    assert poller.poll_once() == 1  # m1 through, m2 failed
    saved = json.loads((tmp_path / "state.json").read_text())
    assert "m1" in saved["seen_ids"] and "m2" not in saved["seen_ids"]
    assert saved["delta_link"] is None  # held

    assert poller.poll_once() == 1  # m2 only; m1 deduped by the seen ledger
    assert [json.loads(p["body"])["event_id"] for p in fwd.posts] == ["m1", "m2"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["delta_link"] == "delta-1b"


def test_rejected_forward_status_is_a_failure_not_a_success(tmp_path):
    # A non-2xx adapter response means the message was NOT accepted — same loss
    # class as a raised forward. Cursor holds; the item retries and lands.
    msg = _raw("m1", "greg@wf.example")
    client = _FakeClient(
        "op@client.example",
        [([msg], "delta-1", False), ([msg], "delta-2", False)],
    )
    fwd = _FlakyForwarder(fail_first=1, reject_status=500)
    poller = _poller(tmp_path, client, fwd)
    poller._ready()

    assert poller.poll_once() == 0
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["delta_link"] is None and "m1" not in saved["seen_ids"]

    assert poller.poll_once() == 1
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["delta_link"] == "delta-2" and "m1" in saved["seen_ids"]
    # Two POSTs total (the rejected one + the accepted retry) with the SAME
    # X-Request-ID — the adapter's idempotency key absorbs the duplicate.
    assert [p["request_id"] for p in fwd.posts] == [_rid("m1"), _rid("m1")]


def test_poison_item_dead_letters_at_the_retry_bound(tmp_path):
    # An item the gate rejects DETERMINISTICALLY must not wedge the cursor
    # forever (the unbounded-hold failure mode): at the retry bound the payload
    # is preserved to the dead-letter dir, the item is marked seen, and the
    # cursor advances. Bounded, loud, payload-preserved — never silent loss.
    msg = _raw("m1", "greg@wf.example")
    batches = [([msg], f"delta-{n}", False) for n in (1, 2, 3)]
    client = _FakeClient("op@client.example", batches)
    fwd = _FlakyForwarder(fail_first=99)  # never recovers
    poller = _poller(tmp_path, client, fwd, max_item_failures=3)
    poller._ready()

    assert poller.poll_once() == 0  # retry 1/3 — held
    assert poller.poll_once() == 0  # retry 2/3 — held
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["delta_link"] is None
    assert saved["failures"] == {"m1": 2}  # counts are durable across restarts

    assert poller.poll_once() == 0  # bound hit — dead-lettered, cursor released
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["delta_link"] == "delta-3"  # cursor advances past the poison item
    assert "m1" in saved["seen_ids"]  # never re-evaluated
    assert saved["failures"] == {}
    dead = list((tmp_path / "dead-letter").glob("*.json"))
    assert len(dead) == 1
    letter = json.loads(dead[0].read_text())
    assert letter["message_id"] == "m1" and letter["raw"]["subject"] == "Hi"


def test_transient_failure_count_clears_on_success(tmp_path):
    # A failure count must not accumulate across unrelated blips: once the item
    # forwards, its counter is gone (it can never creep toward the dead-letter
    # bound over weeks of occasional gate restarts).
    msg = _raw("m1", "greg@wf.example")
    client = _FakeClient(
        "op@client.example",
        [([msg], "delta-1", False), ([msg], "delta-2", False)],
    )
    fwd = _FlakyForwarder(fail_first=1)
    poller = _poller(tmp_path, client, fwd, max_item_failures=3)
    poller._ready()

    assert poller.poll_once() == 0
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["failures"] == {"m1": 1}

    assert poller.poll_once() == 1
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["failures"] == {}
