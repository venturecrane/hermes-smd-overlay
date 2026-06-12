"""Tests for the inbound webhook front-door gate (webhook_gate.py)."""

import base64
import hashlib
import hmac
import time

import webhook_gate as gate

_SECRET = "whsec_" + base64.b64encode(b"a-test-signing-key").decode()

# Fixed signing epoch for deterministic tests; freshness is exercised by
# passing an explicit `now` reference relative to this instant.
_TS = 1700000000
_TS_STR = str(_TS)


def _svix_sig(body: bytes, svix_id: str, ts: str, secret: str) -> str:
    key = base64.b64decode(secret.split("_", 1)[1])
    signed = svix_id.encode() + b"." + ts.encode() + b"." + body
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def test_verify_accepts_a_correct_svix_signature():
    body = b'{"event_type":"message.received","message":{"message_id":"<a@b>"}}'
    sig = _svix_sig(body, "msg_1", _TS_STR, _SECRET)
    assert gate.verify_svix_signature(body, "msg_1", _TS_STR, sig, _SECRET, now=_TS)


def test_verify_accepts_one_of_multiple_space_delimited_signatures():
    body = b'{"x":1}'
    good = _svix_sig(body, "id", _TS_STR, _SECRET)
    header = "v1,deadbeef " + good  # rotated/old sig first, real one second
    assert gate.verify_svix_signature(body, "id", _TS_STR, header, _SECRET, now=_TS)


def test_verify_rejects_wrong_secret_body_id_and_missing_fields():
    body = b'{"x":1}'
    good = _svix_sig(body, "id", _TS_STR, _SECRET)
    other = "whsec_" + base64.b64encode(b"different-key").decode()
    assert not gate.verify_svix_signature(body, "id", _TS_STR, good, other, now=_TS)
    assert not gate.verify_svix_signature(b'{"x":2}', "id", _TS_STR, good, _SECRET, now=_TS)
    assert not gate.verify_svix_signature(body, "WRONG", _TS_STR, good, _SECRET, now=_TS)
    assert not gate.verify_svix_signature(body, "", _TS_STR, good, _SECRET, now=_TS)
    assert not gate.verify_svix_signature(body, "id", _TS_STR, "", _SECRET, now=_TS)
    assert not gate.verify_svix_signature(body, "id", _TS_STR, good, "", now=_TS)


def test_verify_rejects_stale_timestamp_replay():
    """A captured delivery with a valid signature must die past the
    tolerance window (threat model OP-P2-3 — replay closure)."""
    body = b'{"event_type":"message.received"}'
    sig = _svix_sig(body, "msg_replay", _TS_STR, _SECRET)
    stale_now = _TS + gate.SVIX_TIMESTAMP_TOLERANCE_SECONDS + 1
    assert not gate.verify_svix_signature(body, "msg_replay", _TS_STR, sig, _SECRET, now=stale_now)


def test_verify_rejects_future_skew_beyond_tolerance():
    body = b'{"x":1}'
    sig = _svix_sig(body, "id", _TS_STR, _SECRET)
    early_now = _TS - gate.SVIX_TIMESTAMP_TOLERANCE_SECONDS - 1
    assert not gate.verify_svix_signature(body, "id", _TS_STR, sig, _SECRET, now=early_now)


def test_verify_accepts_within_tolerance_both_directions():
    body = b'{"x":1}'
    sig = _svix_sig(body, "id", _TS_STR, _SECRET)
    inside = gate.SVIX_TIMESTAMP_TOLERANCE_SECONDS - 1
    assert gate.verify_svix_signature(body, "id", _TS_STR, sig, _SECRET, now=_TS + inside)
    assert gate.verify_svix_signature(body, "id", _TS_STR, sig, _SECRET, now=_TS - inside)


def test_verify_rejects_non_numeric_timestamp():
    """The timestamp is part of the signed content; a real Svix delivery
    always carries a parseable epoch. Garbage is a fail-closed reject."""
    body = b'{"x":1}'
    sig = _svix_sig(body, "id", "not-a-number", _SECRET)
    assert not gate.verify_svix_signature(body, "id", "not-a-number", sig, _SECRET, now=_TS)


def test_verify_defaults_to_wall_clock():
    """Without an injected reference the gate uses time.time() — a freshly
    signed delivery passes, the fixed 2023 epoch does not."""
    body = b'{"x":1}'
    fresh_ts = str(int(time.time()))
    fresh_sig = _svix_sig(body, "id", fresh_ts, _SECRET)
    assert gate.verify_svix_signature(body, "id", fresh_ts, fresh_sig, _SECRET)
    old_sig = _svix_sig(body, "id", _TS_STR, _SECRET)
    assert not gate.verify_svix_signature(body, "id", _TS_STR, old_sig, _SECRET)


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


def test_audit_db_path_handles_direct_path_varname_and_fallback(monkeypatch):
    # Direct filesystem path (how the live Machine sets it).
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "/opt/data/audit.db")
    assert gate._audit_db_path() == "/opt/data/audit.db"
    # Var-name indirection (the documented form): binding names the path var.
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    monkeypatch.setenv("CUSTOMER_DB", "/data/c.db")
    assert gate._audit_db_path() == "/data/c.db"
    # No binding → fall back to CUSTOMER_DB directly.
    monkeypatch.delenv("SMD_D1_AUDIT_BINDING", raising=False)
    assert gate._audit_db_path() == "/data/c.db"
    monkeypatch.delenv("CUSTOMER_DB", raising=False)
    assert gate._audit_db_path() is None
    assert gate._message_id(b"not json") is None
    assert gate._message_id(b'{"message":{}}') is None
