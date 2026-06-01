"""Tests for the inbound webhook front-door gate (webhook_gate.py)."""

import base64
import hashlib
import hmac

import webhook_gate as gate

_SECRET = "whsec_" + base64.b64encode(b"a-test-signing-key").decode()


def _svix_sig(body: bytes, svix_id: str, ts: str, secret: str) -> str:
    key = base64.b64decode(secret.split("_", 1)[1])
    signed = svix_id.encode() + b"." + ts.encode() + b"." + body
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def test_verify_accepts_a_correct_svix_signature():
    body = b'{"event_type":"message.received","message":{"message_id":"<a@b>"}}'
    sig = _svix_sig(body, "msg_1", "1700000000", _SECRET)
    assert gate.verify_svix_signature(body, "msg_1", "1700000000", sig, _SECRET)


def test_verify_accepts_one_of_multiple_space_delimited_signatures():
    body = b'{"x":1}'
    good = _svix_sig(body, "id", "1700000000", _SECRET)
    header = "v1,deadbeef " + good  # rotated/old sig first, real one second
    assert gate.verify_svix_signature(body, "id", "1700000000", header, _SECRET)


def test_verify_rejects_wrong_secret_body_id_and_missing_fields():
    body = b'{"x":1}'
    good = _svix_sig(body, "id", "1700000000", _SECRET)
    other = "whsec_" + base64.b64encode(b"different-key").decode()
    assert not gate.verify_svix_signature(body, "id", "1700000000", good, other)
    assert not gate.verify_svix_signature(b'{"x":2}', "id", "1700000000", good, _SECRET)
    assert not gate.verify_svix_signature(body, "WRONG", "1700000000", good, _SECRET)
    assert not gate.verify_svix_signature(body, "", "1700000000", good, _SECRET)
    assert not gate.verify_svix_signature(body, "id", "1700000000", "", _SECRET)
    assert not gate.verify_svix_signature(body, "id", "1700000000", good, "")


def test_route_regex_allows_slugs_and_blocks_traversal_and_scheme_tricks():
    assert gate._ROUTE_RE.match("agentmail")
    assert gate._ROUTE_RE.match("filevine-events")
    assert not gate._ROUTE_RE.match("../etc/passwd")
    assert not gate._ROUTE_RE.match("a/b")
    assert not gate._ROUTE_RE.match("")
    assert not gate._ROUTE_RE.match("UPPER")


def test_route_secret_reads_per_vendor_env(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_AGENTMAIL", "abc")
    assert gate._route_secret("agentmail") == "abc"
    monkeypatch.delenv("WEBHOOK_SECRET_AGENTMAIL", raising=False)
    assert gate._route_secret("agentmail") is None


def test_message_id_extracted_from_agentmail_payload():
    body = b'{"event_type":"message.received","message":{"message_id":"<x@y>"}}'
    assert gate._message_id(body) == "<x@y>"
    assert gate._message_id(b"not json") is None
    assert gate._message_id(b'{"message":{}}') is None
