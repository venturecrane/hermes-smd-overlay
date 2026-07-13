"""Tests for the voice live-gate (ADR 0028 §2, issue #855).

Closes the "primed, not gated" gap: voice transformation runs on live output,
and this gate ENFORCES that an AUTONOMOUS outside send actually ran through it —
otherwise the send is downgraded to draft-for-review and audited.

Mirrors the content-floor test style (``test_content_floor.py`` /
``test_trust_enforce.py``): the gate binds only when ``voice_library`` is
authored, fires only on an allowed autonomous OUTSIDE ``external_send``, and
fails toward draft.

Matrix (team-locked):
  - authored + samples + transform-applied  → passes (send allowed)
  - authored + no samples                   → draft + audit(reason=no_samples)
  - authored + samples + transform-skipped  → draft + audit(reason=transform_not_applied)
  - unauthored voice                        → gate silent
  - gate internal error                     → draft + audit(reason=gate_error)

Plus the cross-hook marker mechanics (voice plugin side) and the exclusion of
the confirm / draft / external_send_internal paths (which have a human or are
ops traffic, not client-voice impersonation).
"""

from __future__ import annotations

import pytest

from shared.voice_status import VOICE_STATUS
from tests.conftest import load_plugin

# ---------------------------------------------------------------------------
# Module loaders + shared reset
# ---------------------------------------------------------------------------


def _load_trust(submodule: str = ""):
    plugin = load_plugin("hermes-smd-trust")
    if not submodule:
        return plugin
    if submodule == "voice_gate":
        # voice_gate is imported by enforce (not by the package __init__), so it
        # is reachable via the enforce module reference.
        return plugin.enforce.voice_gate
    return getattr(plugin, submodule)


@pytest.fixture(autouse=True)
def _reset_voice_status():
    """VOICE_STATUS is a process-wide singleton — reset before AND after each
    test so per-turn marks / the samples probe never leak between tests."""
    VOICE_STATUS.publish_samples_probe(None)
    VOICE_STATUS._applied.clear()
    yield
    VOICE_STATUS.publish_samples_probe(None)
    VOICE_STATUS._applied.clear()


@pytest.fixture
def voice_gate(monkeypatch):
    """The trust plugin's voice_gate submodule, with the audit-client cache reset
    so a stale wiring from another test can't leak in."""
    vg = _load_trust("voice_gate")
    monkeypatch.setattr(vg, "_AUDIT_WIRED", False)
    monkeypatch.setattr(vg, "_AUDIT_CLIENT", None)
    monkeypatch.setattr(vg, "_AUDIT_CUSTOMER_SLUG", None)
    return vg


def _capture_audit(monkeypatch, vg) -> list[dict]:
    """Replace ``_emit_voice_gate_audit`` with a recorder; return the record list."""
    calls: list[dict] = []

    def _rec(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(vg, "_emit_voice_gate_audit", _rec)
    return calls


# ===========================================================================
# check_voice_gate — the gate decision (matrix)
# ===========================================================================


def test_gate_silent_when_voice_not_authored(voice_gate, monkeypatch):
    """Unauthored voice ⇒ gate does not fire (ADR 0035 — not an imposed default)."""
    monkeypatch.setattr(voice_gate, "_voice_authored", lambda: False)
    calls = _capture_audit(monkeypatch, voice_gate)
    # No mark, no probe — but it must not matter: the gate is unbound.
    assert voice_gate.check_voice_gate(tool_name="agentmail:send_message", session_id="s1") is None
    assert calls == []


def test_pass_when_authored_samples_and_transform_applied(voice_gate, monkeypatch):
    """authored + transform-applied ⇒ send allowed (both pass conditions hold —
    a successful transform implies samples were retrieved)."""
    monkeypatch.setattr(voice_gate, "_voice_authored", lambda: True)
    calls = _capture_audit(monkeypatch, voice_gate)
    VOICE_STATUS.mark_applied("s1")
    assert voice_gate.check_voice_gate(tool_name="agentmail:send_message", session_id="s1") is None
    assert calls == []


def test_draft_and_audit_when_authored_no_samples(voice_gate, monkeypatch):
    """authored + no samples ⇒ draft + audit(reason=no_samples)."""
    monkeypatch.setattr(voice_gate, "_voice_authored", lambda: True)
    calls = _capture_audit(monkeypatch, voice_gate)
    # No mark; no probe published ⇒ samples_available() is False.
    result = voice_gate.check_voice_gate(tool_name="agentmail:send_message", session_id="s1")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "draft" in result["message"].lower()
    assert len(calls) == 1
    assert calls[0]["reason"] == "no_samples"


def test_draft_and_audit_when_samples_but_transform_skipped(voice_gate, monkeypatch):
    """authored + samples + transform-skipped ⇒ draft + audit(reason=transform_not_applied)."""
    monkeypatch.setattr(voice_gate, "_voice_authored", lambda: True)
    calls = _capture_audit(monkeypatch, voice_gate)
    VOICE_STATUS.publish_samples_probe(lambda: True)  # samples ARE retrievable
    # ... but no per-turn mark ⇒ the transform did not apply this turn.
    result = voice_gate.check_voice_gate(tool_name="agentmail:send_message", session_id="s1")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "draft" in result["message"].lower()
    assert len(calls) == 1
    assert calls[0]["reason"] == "transform_not_applied"


def test_draft_and_audit_on_gate_internal_error(voice_gate, monkeypatch):
    """Any internal error in a BOUND gate ⇒ draft + audit(reason=gate_error) (ADR 0028 §4)."""
    monkeypatch.setattr(voice_gate, "_voice_authored", lambda: True)
    calls = _capture_audit(monkeypatch, voice_gate)

    class _Boom:
        def was_applied(self, _sid):
            raise RuntimeError("register exploded")

    monkeypatch.setattr(voice_gate, "VOICE_STATUS", _Boom())
    result = voice_gate.check_voice_gate(tool_name="agentmail:send_message", session_id="s1")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert len(calls) == 1
    assert calls[0]["reason"] == "gate_error"


def test_downgrade_without_audit_env_does_not_crash(voice_gate, monkeypatch):
    """The real audit emitter (no SMD_D1_AUDIT_BINDING configured) skips the row
    but the draft downgrade still stands — the emitter must never raise."""
    monkeypatch.setattr(voice_gate, "_voice_authored", lambda: True)
    for key in ("SMD_CUSTOMER_SLUG", "SMD_D1_AUDIT_BINDING", "SMD_AUDIT_BROKER_SOCKET"):
        monkeypatch.delenv(key, raising=False)
    result = voice_gate.check_voice_gate(tool_name="agentmail:send_message", session_id="s1")
    assert isinstance(result, dict)
    assert result["action"] == "block"


def test_empty_session_id_fails_closed_to_draft(voice_gate, monkeypatch):
    """An untracked turn (empty session id) cannot certify voice ran ⇒ draft."""
    monkeypatch.setattr(voice_gate, "_voice_authored", lambda: True)
    _capture_audit(monkeypatch, voice_gate)
    # Even if a mark exists for some OTHER session, an empty id must not pass.
    VOICE_STATUS.mark_applied("other")
    result = voice_gate.check_voice_gate(tool_name="agentmail:send_message", session_id="")
    assert isinstance(result, dict)
    assert result["action"] == "block"


# ===========================================================================
# check_voice_gate — binding condition reads voice_library
# ===========================================================================


def test_voice_authored_true_when_voice_library_present(voice_gate, monkeypatch):
    from shared import customer_config

    class _Cfg:
        voice_library = {"samples_path": "r2://vaults/smd/voice/cohort/"}

    monkeypatch.setattr(
        customer_config.CustomerConfig, "from_volume", classmethod(lambda cls: _Cfg())
    )
    assert voice_gate._voice_authored() is True


def test_voice_authored_false_when_voice_library_absent(voice_gate, monkeypatch):
    from shared import customer_config

    class _Cfg:
        voice_library: dict = {}

    monkeypatch.setattr(
        customer_config.CustomerConfig, "from_volume", classmethod(lambda cls: _Cfg())
    )
    assert voice_gate._voice_authored() is False


def test_voice_authored_false_when_config_unresolved(voice_gate, monkeypatch):
    """Missing/unreadable config ⇒ not authored ⇒ gate silent (never fail-open here;
    the ceiling resolver already fail-closes an unresolvable config upstream)."""
    from shared import customer_config

    def _boom(cls):
        raise customer_config.CustomerConfigMissingError("no volume")

    monkeypatch.setattr(customer_config.CustomerConfig, "from_volume", classmethod(_boom))
    assert voice_gate._voice_authored() is False


# ===========================================================================
# Integration through enforce.evaluate_tool_call — fires only on the
# allowed autonomous OUTSIDE external_send path
# ===========================================================================


def _set_exposure(monkeypatch, enforce, mapping, *, persona="marcus"):
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", lambda slug="": dict(mapping))
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", persona)


def test_autonomous_outside_send_downgraded_when_voice_authored_and_no_transform(monkeypatch):
    """End-to-end: a clean autonomous outside send on a voice-authored seat is
    downgraded to draft when the transform did not run this turn."""
    enforce = _load_trust("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    monkeypatch.setattr(enforce.voice_gate, "_voice_authored", lambda: True)
    _capture_audit(monkeypatch, enforce.voice_gate)
    # Clean body clears the content floor; no per-turn mark for this session.
    args = {"subject": "Saw your note", "text": "Got it, that works on my end. Talk soon."}
    result = enforce.evaluate_tool_call("agentmail:send_message", args, "smd", session_id="sess-x")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "draft" in result["message"].lower()


def test_autonomous_outside_send_allowed_when_transform_marked(monkeypatch):
    """The same send is allowed once the per-turn transform mark is set."""
    enforce = _load_trust("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    monkeypatch.setattr(enforce.voice_gate, "_voice_authored", lambda: True)
    VOICE_STATUS.mark_applied("sess-x")
    args = {"subject": "Saw your note", "text": "Got it, that works on my end. Talk soon."}
    assert (
        enforce.evaluate_tool_call("agentmail:send_message", args, "smd", session_id="sess-x")
        is None
    )


def test_autonomous_outside_send_not_gated_when_voice_unauthored(monkeypatch):
    """Non-voice seat: an autonomous clean outside send still sends (gate silent)."""
    enforce = _load_trust("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    monkeypatch.setattr(enforce.voice_gate, "_voice_authored", lambda: False)
    args = {"subject": "Saw your note", "text": "Got it, that works on my end. Talk soon."}
    assert (
        enforce.evaluate_tool_call("agentmail:send_message", args, "smd", session_id="sess-x")
        is None
    )


def test_confirm_ceiling_send_is_not_voice_gated(monkeypatch):
    """A CONFIRM-ceiling send with an explicit current-turn approval has a human
    in the loop, so voice is NOT gated even with voice authored + no mark."""
    enforce = _load_trust("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.CONFIRM}
    )
    monkeypatch.setattr(enforce.voice_gate, "_voice_authored", lambda: True)
    args = {"text": "Got it, that works on my end.", "_current_turn_approval": True}
    assert (
        enforce.evaluate_tool_call("agentmail:send_message", args, "smd", session_id="sess-x")
        is None
    )


def test_internal_send_is_not_voice_gated(monkeypatch):
    """external_send_internal (ops traffic to rostered staff) is not client-voice
    impersonation, so the voice gate does not apply even at autonomous."""
    enforce = _load_trust("enforce")
    _set_exposure(
        monkeypatch,
        enforce,
        {enforce.ActionClass.EXTERNAL_SEND_INTERNAL: enforce.Ceiling.AUTONOMOUS},
    )
    monkeypatch.setattr(enforce.voice_gate, "_voice_authored", lambda: True)
    # Force the recipient reclassification to INTERNAL.
    monkeypatch.setattr(
        enforce,
        "_reclassify_send",
        lambda *a, **k: enforce.ActionClass.EXTERNAL_SEND_INTERNAL,
    )
    args = {"text": "Heads up: the Miller matter needs a call today."}
    assert (
        enforce.evaluate_tool_call("agentmail:send_message", args, "smd", session_id="sess-x")
        is None
    )


def test_content_floor_precedes_voice_gate_on_sensitive_body(monkeypatch):
    """A money/contract body is downgraded by the content floor before the voice
    gate is consulted — either way the outcome is draft, but the content floor
    is the first gate on the path."""
    enforce = _load_trust("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    monkeypatch.setattr(enforce.voice_gate, "_voice_authored", lambda: True)
    # Voice would ALSO downgrade (no mark), so assert the message is the content
    # floor's, proving order.
    args = {"text": "Please remit payment of $500 by Friday."}
    result = enforce.evaluate_tool_call("agentmail:send_message", args, "smd", session_id="sess-x")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    # content floor message names the sensitive category; voice message names "voice"
    assert "voice live-gate" not in result["message"]


# ===========================================================================
# Cross-hook marker mechanics (voice plugin side)
# ===========================================================================


class _FakeResult:
    def __init__(self, status, draft="reshaped"):
        self.status = status
        self.transformed_draft = draft
        self.changes_applied = 1
        self.profile_sample_count = 6


@pytest.fixture
def voice():
    mod = load_plugin("hermes-smd-voice")
    mod._R2_READER = None
    mod._CUSTOMER_SLUG = None
    mod._VOICE_BUNDLE = None
    yield mod
    mod._R2_READER = None
    mod._CUSTOMER_SLUG = None
    mod._VOICE_BUNDLE = None


def test_transform_hook_sets_marker_on_transformed(voice, monkeypatch):
    voice._R2_READER = object()
    voice._CUSTOMER_SLUG = "acme"
    monkeypatch.setattr(voice, "_get_cached_bundle", lambda: object())
    monkeypatch.setattr(
        voice,
        "transform_draft",
        lambda draft, profile: _FakeResult(voice.TransformStatus.TRANSFORMED),
    )
    out = voice.on_transform_llm_output(
        response_text="hi there", session_id="sess-x", model="m", platform="p"
    )
    assert out == "reshaped"
    assert VOICE_STATUS.was_applied("sess-x") is True


def test_transform_hook_no_marker_on_passthrough(voice, monkeypatch):
    voice._R2_READER = object()
    voice._CUSTOMER_SLUG = "acme"
    monkeypatch.setattr(voice, "_get_cached_bundle", lambda: object())
    monkeypatch.setattr(
        voice,
        "transform_draft",
        lambda draft, profile: _FakeResult(voice.TransformStatus.PASSTHROUGH_NO_CHANGE_NEEDED),
    )
    out = voice.on_transform_llm_output(
        response_text="hi there", session_id="sess-x", model="m", platform="p"
    )
    assert out is None
    assert VOICE_STATUS.was_applied("sess-x") is False


def test_transform_hook_no_marker_on_exception(voice, monkeypatch):
    voice._R2_READER = object()
    voice._CUSTOMER_SLUG = "acme"
    monkeypatch.setattr(voice, "_get_cached_bundle", lambda: object())

    def _boom(draft, profile):
        raise RuntimeError("transform blew up")

    monkeypatch.setattr(voice, "transform_draft", _boom)
    out = voice.on_transform_llm_output(
        response_text="hi there", session_id="sess-x", model="m", platform="p"
    )
    assert out is None
    assert VOICE_STATUS.was_applied("sess-x") is False


def test_pre_llm_call_clears_marker(voice):
    VOICE_STATUS.mark_applied("sess-x")
    assert VOICE_STATUS.was_applied("sess-x") is True
    # Unbound is fine — the clear runs before the unbound check.
    voice.on_pre_llm_call(session_id="sess-x", user_message="hello")
    assert VOICE_STATUS.was_applied("sess-x") is False


def test_marker_is_per_turn_not_sticky(voice, monkeypatch):
    """Turn 1 transforms (marker set); turn 2 does not — the pre_llm_call clear at
    the start of turn 2 means turn 2's send cannot ride turn 1's mark."""
    voice._R2_READER = object()
    voice._CUSTOMER_SLUG = "acme"
    monkeypatch.setattr(voice, "_get_cached_bundle", lambda: object())
    monkeypatch.setattr(
        voice,
        "transform_draft",
        lambda draft, profile: _FakeResult(voice.TransformStatus.TRANSFORMED),
    )
    # Turn 1
    voice.on_pre_llm_call(session_id="sess-x", user_message="hello")
    voice.on_transform_llm_output(response_text="a", session_id="sess-x", model="m", platform="p")
    assert VOICE_STATUS.was_applied("sess-x") is True
    # Turn 2 — new turn starts (clear), transform does NOT run this time
    voice.on_pre_llm_call(session_id="sess-x", user_message="again")
    assert VOICE_STATUS.was_applied("sess-x") is False


# ---------------------------------------------------------------------------
# Samples probe published at bind
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, keys):
        self._keys = list(keys)

    def list_keys(self, prefix):
        return [k for k in self._keys if k.startswith(prefix)]

    def get(self, key):
        import json

        return json.dumps({"schema_version": 1, "greeting_style": "first_name"}).encode()


def test_bind_publishes_samples_probe_true_when_samples(voice):
    reader = _FakeReader(["acme/voice/cohort/general/s1.json"])
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)
    assert VOICE_STATUS.samples_available() is True


def test_bind_publishes_samples_probe_false_when_empty(voice):
    reader = _FakeReader([])  # empty vault
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)
    assert VOICE_STATUS.samples_available() is False


def test_samples_available_false_when_unbound():
    """No probe published (voice runtime never bound) ⇒ fail-closed False."""
    VOICE_STATUS.publish_samples_probe(None)
    assert VOICE_STATUS.samples_available() is False
