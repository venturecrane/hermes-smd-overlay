"""Tests for the inbound webhook front-door gate (webhook_gate.py)."""

import base64
import hashlib
import hmac
import json
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


def test_stamp_source_adds_route_as_source():
    # AgentMail's payload carries event_type but no source; the gate stamps the
    # verified route slug so the router can route on (source, event_type).
    body = b'{"event_type":"message.received","message":{"message_id":"m1"}}'
    out = json.loads(gate._stamp_source(body, "agentmail"))
    assert out["source"] == "agentmail"
    assert out["event_type"] == "message.received"
    assert out["message"]["message_id"] == "m1"  # message block untouched


def test_stamp_source_overrides_body_source_with_authoritative_route():
    # The ingress provenance is authoritative and OVERRIDES a body-supplied
    # source. Smokeball's event body carries its own top-level source ("API"/
    # "UI" — the change's origin inside Smokeball), a different semantic that
    # collides on the key the router matches on. Without the override a verified
    # matter.updated would look up ("API", …) and silently no-op. The vendor's
    # original value is preserved under origin_source.
    body = b'{"source":"API","type":"matter.updated","payload":{"id":"m1"}}'
    out = json.loads(gate._stamp_source(body, "smokeball"))
    assert out["source"] == "smokeball"
    assert out["origin_source"] == "API"
    assert out["event_type"] == "matter.updated"
    assert out["payload"]["id"] == "m1"  # payload block untouched


def test_stamp_source_noop_when_source_already_equals_route():
    body = b'{"source":"agentmail","event_type":"x"}'
    assert gate._stamp_source(body, "agentmail") == body  # unchanged bytes


def test_stamp_source_fail_safe_on_non_json_or_non_object():
    # A body the gate cannot parse/stamp is forwarded unchanged (it would not
    # route anyway; a parse error must never break the forward).
    assert gate._stamp_source(b"not json", "agentmail") == b"not json"
    assert gate._stamp_source(b'["a","b"]', "agentmail") == b'["a","b"]'


def test_stamp_source_derives_event_type_from_svix_type():
    # Regression (demo-law 2026-06-13): AgentMail delivers over Svix, whose
    # envelope carries the event name under "type", not "event_type". The gate
    # must stamp event_type from "type" so the router's (source, event_type)
    # match fires — without it the route was silently skipped and the relay's
    # recipient-lock origin was never recorded.
    body = b'{"type":"message.received","data":{"message_id":"m1"}}'
    out = json.loads(gate._stamp_source(body, "agentmail"))
    assert out["source"] == "agentmail"
    assert out["event_type"] == "message.received"


def test_stamp_source_derives_event_type_from_event_field():
    body = b'{"event":"message.received","data":{}}'
    out = json.loads(gate._stamp_source(body, "agentmail"))
    assert out["event_type"] == "message.received"


def test_stamp_source_keeps_explicit_event_type_over_type():
    body = b'{"event_type":"explicit","type":"other","message":{}}'
    out = json.loads(gate._stamp_source(body, "agentmail"))
    assert out["event_type"] == "explicit"  # never overwritten


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


def test_boot_self_check_passes_under_freshness_window() -> None:
    # Regression: the v0.4.17 deploy crash-looped because the boot probe
    # signed a FIXED epoch (1700000000) that the #61 replay window rejects.
    # The self-check must sign with a current timestamp and pass against the
    # default (real-clock) freshness window.
    from webhook_gate import svix_self_check

    assert svix_self_check() is True


# --------------------------------------------------------------------------- #
# Smokeball webhook verification                                              #
# --------------------------------------------------------------------------- #

# The PUBLISHED golden vector from Smokeball's webhook docs — locks the wire
# format (raw-UTF-8 key, pipe-joined string, lowercase-hex HMAC-SHA256).
_GV_KEY = "ei7641529ue420n8b9aa"
_GV_TS = "638609288928990639"
_GV_RID = "e56f2c3f-b6de-4310-a7a2-c139d62f9711"
_GV_CID = "lou1qnn0llav95f"
_GV_SIG = "58817681863148b0e624c00f3094f99e1af31cd7b99a3c2e0655d64a2764d650"
# The instant the golden timestamp encodes (.NET ticks -> unix), used to inject
# `now` so the freshness window passes deterministically for the fixed vector.
_GV_UNIX = int(_GV_TS) // 10_000_000 - 62_135_596_800

_SB_SECRET = "raw-smokeball-key"  # raw UTF-8, NOT whsec_/base64
_SB_CID = "test-client-id"
_SB_TS = str((1700000000 + 62_135_596_800) * 10_000_000)  # 1700000000 as .NET ticks
_SB_RID = "11111111-1111-1111-1111-111111111111"


def _sb_sig(ts: str, rid: str, cid: str, secret: str) -> str:
    signed = f"{ts}|{rid}|{cid}".encode()
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def test_smokeball_accepts_published_golden_vector():
    # Byte-for-byte against Smokeball's documented example. If this fails, our
    # wire format diverged from theirs (the highest-value guard).
    assert gate.verify_smokeball_signature(
        b"any-body-ignored", _GV_TS, _GV_RID, _GV_CID, _GV_SIG, _GV_KEY, now=_GV_UNIX
    )


def test_smokeball_key_is_raw_bytes_not_base64_or_whsec():
    # The golden vector only verifies if the key is used as raw UTF-8 bytes.
    # Guards against cloning the Svix path (whsec_ strip + base64 decode).
    assert _sb_sig(_GV_TS, _GV_RID, _GV_CID, _GV_KEY) == _GV_SIG


def test_smokeball_body_is_not_part_of_signature():
    # Smokeball signs metadata only; the body must not affect verification.
    sig = _sb_sig(_SB_TS, _SB_RID, _SB_CID, _SB_SECRET)
    ok_a = gate.verify_smokeball_signature(
        b'{"a":1}', _SB_TS, _SB_RID, _SB_CID, sig, _SB_SECRET, now=1700000000
    )
    ok_b = gate.verify_smokeball_signature(
        b'{"b":2}', _SB_TS, _SB_RID, _SB_CID, sig, _SB_SECRET, now=1700000000
    )
    assert ok_a and ok_b


def test_smokeball_accepts_within_tolerance_both_directions():
    sig = _sb_sig(_SB_TS, _SB_RID, _SB_CID, _SB_SECRET)
    inside = gate.SMOKEBALL_TIMESTAMP_TOLERANCE_SECONDS - 1
    assert gate.verify_smokeball_signature(
        b"", _SB_TS, _SB_RID, _SB_CID, sig, _SB_SECRET, now=1700000000 + inside
    )
    assert gate.verify_smokeball_signature(
        b"", _SB_TS, _SB_RID, _SB_CID, sig, _SB_SECRET, now=1700000000 - inside
    )


def test_smokeball_rejects_stale_and_future_skew():
    sig = _sb_sig(_SB_TS, _SB_RID, _SB_CID, _SB_SECRET)
    past = gate.SMOKEBALL_TIMESTAMP_TOLERANCE_SECONDS + 1
    assert not gate.verify_smokeball_signature(
        b"", _SB_TS, _SB_RID, _SB_CID, sig, _SB_SECRET, now=1700000000 + past
    )
    assert not gate.verify_smokeball_signature(
        b"", _SB_TS, _SB_RID, _SB_CID, sig, _SB_SECRET, now=1700000000 - past
    )


def test_smokeball_rejects_wrong_secret_clientid_sig_and_missing_fields():
    sig = _sb_sig(_SB_TS, _SB_RID, _SB_CID, _SB_SECRET)
    v = gate.verify_smokeball_signature
    # wrong secret
    assert not v(b"", _SB_TS, _SB_RID, _SB_CID, sig, "other-key", now=1700000000)
    # wrong client id (tenant binding — a different configured ClientId fails)
    assert not v(b"", _SB_TS, _SB_RID, "wrong-client", sig, _SB_SECRET, now=1700000000)
    # tampered signature
    assert not v(b"", _SB_TS, _SB_RID, _SB_CID, "00" + sig[2:], _SB_SECRET, now=1700000000)
    # each missing field is fail-closed
    assert not v(b"", "", _SB_RID, _SB_CID, sig, _SB_SECRET, now=1700000000)
    assert not v(b"", _SB_TS, "", _SB_CID, sig, _SB_SECRET, now=1700000000)
    assert not v(b"", _SB_TS, _SB_RID, "", sig, _SB_SECRET, now=1700000000)
    assert not v(b"", _SB_TS, _SB_RID, _SB_CID, "", _SB_SECRET, now=1700000000)
    assert not v(b"", _SB_TS, _SB_RID, _SB_CID, sig, "", now=1700000000)


def test_smokeball_rejects_non_numeric_ticks_and_strips_whitespace():
    # Garbage ticks -> fail-closed. Surrounding whitespace on the (already
    # OWS-trimmed) header must not break the integer parse.
    assert not gate.verify_smokeball_signature(
        b"", "not-ticks", _SB_RID, _SB_CID, "x", _SB_SECRET, now=1700000000
    )
    sig = _sb_sig(_SB_TS, _SB_RID, _SB_CID, _SB_SECRET)
    assert gate.verify_smokeball_signature(
        b"", f"  {_SB_TS}  ", _SB_RID, _SB_CID, sig, _SB_SECRET, now=1700000000
    )


def test_smokeball_signature_is_case_insensitive_hex():
    sig = _sb_sig(_SB_TS, _SB_RID, _SB_CID, _SB_SECRET)
    assert gate.verify_smokeball_signature(
        b"", _SB_TS, _SB_RID, _SB_CID, sig.upper(), _SB_SECRET, now=1700000000
    )


def test_smokeball_replay_cache_rejects_duplicate_request_id():
    gate._replay_seen.clear()
    now = 1700000000
    assert gate._replay_check_and_record("rid-A", now=now) is True
    assert gate._replay_check_and_record("rid-A", now=now) is False  # dup
    # A different id is fresh.
    assert gate._replay_check_and_record("rid-B", now=now) is True
    # Past the TTL the entry is pruned and the id is fresh again.
    later = now + gate._REPLAY_TTL_SECONDS + 1
    assert gate._replay_check_and_record("rid-A", now=later) is True
    # An empty id is never deduped (signature already validated it).
    assert gate._replay_check_and_record("", now=now) is True
    gate._replay_seen.clear()


def test_smokeball_route_verify_uses_configured_clientid_and_returns_rid(monkeypatch):
    gate._replay_seen.clear()
    monkeypatch.setenv("WEBHOOK_SMOKEBALL_CLIENT_ID", _SB_CID)
    # The adapter uses the wall clock, so sign with current ticks.
    now = int(time.time())
    ticks = str((now + 62_135_596_800) * 10_000_000)
    rid = "route-rid-1"
    sig = _sb_sig(ticks, rid, _SB_CID, _SB_SECRET)
    headers = {"Timestamp": ticks, "RequestId": rid, "Signature": sig}
    ok, out_rid = gate._smokeball_route_verify(headers, b'{"x":1}', _SB_SECRET)
    assert ok and out_rid == rid
    gate._replay_seen.clear()


def test_smokeball_route_verify_fails_closed_without_configured_clientid(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SMOKEBALL_CLIENT_ID", raising=False)
    now = int(time.time())
    ticks = str((now + 62_135_596_800) * 10_000_000)
    sig = _sb_sig(ticks, "rid", _SB_CID, _SB_SECRET)
    headers = {"Timestamp": ticks, "RequestId": "rid", "Signature": sig}
    ok, _ = gate._smokeball_route_verify(headers, b"", _SB_SECRET)
    assert ok is False


def test_unknown_route_has_no_verifier_registered():
    # A route with no registered verifier must be rejected (fail-closed),
    # closing the prior hole where any secret-bearing route fell through to Svix.
    assert gate._VERIFIERS.get("agentmail") is not None
    assert gate._VERIFIERS.get("smokeball") is not None
    assert gate._VERIFIERS.get("github") is None


def test_smokeball_stamp_routes_on_authoritative_source_and_type():
    # End-to-end stamp on a realistic Smokeball event body: the body's own
    # source ("API") is overridden by the route, event_type comes from "type",
    # and the router's (smokeball, matter.updated) lookup will fire.
    body = (
        b'{"accountId":"a","subscriptionId":"s","type":"matter.updated",'
        b'"source":"API","payload":{"id":"68df","status":"Open"}}'
    )
    out = json.loads(gate._stamp_source(body, "smokeball"))
    assert (out["source"], out["event_type"]) == ("smokeball", "matter.updated")
    assert out["origin_source"] == "API"


def test_stamp_source_stamps_event_id_for_replay_when_absent():
    # The header-less router reads its replay key from the body; Smokeball carries
    # no top-level event_id/id, so the gate stamps the verified RequestId.
    body = b'{"type":"matter.updated","source":"API","payload":{"id":"68df"}}'
    out = json.loads(gate._stamp_source(body, "smokeball", event_id="req-123"))
    assert out["event_id"] == "req-123"


def test_stamp_source_never_overrides_an_existing_event_id():
    # A vendor that DOES supply event_id keeps it (AgentMail unaffected).
    body = b'{"type":"message.received","event_id":"vendor-own","data":{}}'
    out = json.loads(gate._stamp_source(body, "agentmail", event_id="req-xyz"))
    assert out["event_id"] == "vendor-own"


def test_stamp_source_no_event_id_when_none_provided():
    # The handoff/legacy path passes no event_id → no event_id key is added.
    body = b'{"type":"matter.updated","source":"API"}'
    out = json.loads(gate._stamp_source(body, "smokeball"))
    assert "event_id" not in out


def test_smokeball_boot_self_check_passes_under_freshness_window():
    from webhook_gate import smokeball_self_check

    assert smokeball_self_check() is True
