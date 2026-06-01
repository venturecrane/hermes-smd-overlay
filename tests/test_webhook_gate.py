"""Tests for the inbound webhook front-door gate (webhook_gate.py)."""

import hashlib
import hmac

import webhook_gate as gate


def _sig(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_accepts_a_correct_agentmail_signature():
    body = b'{"event_type":"message.received","message":{"message_id":"<a@b>"}}'
    assert gate.verify_agentmail_signature(body, _sig(body, "s3cr3t"), "s3cr3t")


def test_verify_rejects_wrong_secret_wrong_body_and_missing_header():
    body = b'{"x":1}'
    assert not gate.verify_agentmail_signature(body, _sig(body, "right"), "wrong")
    assert not gate.verify_agentmail_signature(b'{"x":2}', _sig(body, "k"), "k")
    assert not gate.verify_agentmail_signature(body, "", "k")
    assert not gate.verify_agentmail_signature(body, _sig(body, "k"), "")


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
