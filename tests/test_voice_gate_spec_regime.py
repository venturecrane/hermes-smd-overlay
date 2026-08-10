"""Behavioral tests for the voice gate's spec regime (ss#2086 step 1).

The repointed gate resolves its binding per (seat × output class): a class
declared ``voice_spec: expected`` is governed by the authored-spec binding
(installed + hash-matches-root-manifest + read this turn, via the SAME
``spec_read`` → ``SPEC_STATUS`` machinery the spec gate uses); every other
class keeps the original Mechanism-B binding. ``test_voice_live_gate.py``
continues to cover the fallback matrix untouched — everything here is the new
half and the seams between the two.

Matrix:
  - declared class, spec installed+verified+read this turn      → passes
  - declared class, spec installed but NOT read this turn        → draft(spec_not_read)
  - declared class, spec installed but bytes tampered            → draft(spec_hash_mismatch)
  - declared class, no voice spec installed at all               → draft(no_spec)
  - declared class, spec-register explodes                       → draft(gate_error)
  - UNDECLARED class on a DECLARING seat                         → Mechanism-B fallback
  - output_classes unreadable on a voice-authored seat           → Mechanism-B fallback
  - audit: spec-regime rows ride the unchanged VOICE_GATE_TRIGGERED shape
"""

from __future__ import annotations

import hashlib
import json

import pytest

from shared import customer_config
from shared.spec_status import SPEC_STATUS
from shared.voice_status import VOICE_STATUS
from tests.conftest import load_plugin

SESSION = "sess-spec"


def _load_trust(submodule: str = ""):
    plugin = load_plugin("hermes-smd-trust")
    if not submodule:
        return plugin
    if submodule == "voice_gate":
        return plugin.enforce.voice_gate
    return getattr(plugin, submodule)


@pytest.fixture(autouse=True)
def _reset_registers():
    SPEC_STATUS._reset_for_tests()
    VOICE_STATUS.publish_samples_probe(None)
    VOICE_STATUS._applied.clear()
    yield
    SPEC_STATUS._reset_for_tests()
    VOICE_STATUS.publish_samples_probe(None)
    VOICE_STATUS._applied.clear()


@pytest.fixture
def voice_gate(monkeypatch):
    vg = _load_trust("voice_gate")
    monkeypatch.setattr(vg, "_AUDIT_WIRED", False)
    monkeypatch.setattr(vg, "_AUDIT_CLIENT", None)
    monkeypatch.setattr(vg, "_AUDIT_CUSTOMER_SLUG", None)
    return vg


def _capture_audit(monkeypatch, vg) -> list[dict]:
    calls: list[dict] = []

    def _rec(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(vg, "_emit_voice_gate_audit", _rec)
    return calls


def _set_config(monkeypatch, *, voice_library=None, output_classes=None):
    """Install a fake seat config behind ``CustomerConfig.from_volume``."""

    class _Cfg:
        # enforce._resolve_vertical reads this on the same path; empty ⇒ no
        # vertical floors, keeping the authored exposure in charge of the test.
        vertical = ""

    cfg = _Cfg()
    cfg.voice_library = voice_library if voice_library is not None else {}
    cfg.output_classes = output_classes if output_classes is not None else {}
    monkeypatch.setattr(customer_config.CustomerConfig, "from_volume", classmethod(lambda cls: cfg))
    return cfg


@pytest.fixture
def spec_tree(tmp_path, monkeypatch):
    """An installed, root-manifested voice spec for ``outbound_client``."""
    from shared import spec_manifest

    body = "Lead with the answer. One idea per sentence.\n"
    rel = "classes/outbound_client/voice.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(body)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "customer": "ashton-price",
                "source_digest": "deadbeef",
                "specs": {
                    rel: {
                        "class": "outbound_client",
                        "property": "voice",
                        "sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "bytes": len(body),
                    }
                },
            }
        )
    )
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tmp_path))
    return tmp_path


#: A seat that authors voice AND declares the client class — the overlap case
#: where the spec regime must govern the declared class.
_DECLARING = {"outbound_client": {"voice_spec": "expected"}}


def _gate(vg, *, action_class_value="external_send_client", session_id=SESSION):
    return vg.check_voice_gate(
        tool_name="agentmail:send_message",
        action_class_value=action_class_value,
        session_id=session_id,
    )


# ===========================================================================
# The spec regime — pass and each fail reason
# ===========================================================================


def test_declared_class_with_spec_read_this_turn_passes(voice_gate, monkeypatch, spec_tree):
    """Declared + installed + verified + read ⇒ the autonomous send proceeds,
    with NO dependency on Mechanism B (no samples, no transform mark)."""
    _set_config(monkeypatch, voice_library={"samples_path": "r2://x/"}, output_classes=_DECLARING)
    calls = _capture_audit(monkeypatch, voice_gate)
    SPEC_STATUS.mark_read(SESSION, "outbound_client", "voice")
    assert _gate(voice_gate) is None
    assert calls == []


def test_declared_class_spec_not_read_downgrades(voice_gate, monkeypatch, spec_tree):
    """Installed and verifiable but unread this turn ⇒ draft(spec_not_read).
    A transform mark cannot substitute: the spec regime governs this class."""
    _set_config(monkeypatch, voice_library={"samples_path": "r2://x/"}, output_classes=_DECLARING)
    calls = _capture_audit(monkeypatch, voice_gate)
    VOICE_STATUS.mark_applied(SESSION)  # Mechanism B ran — must NOT certify a declared class
    result = _gate(voice_gate)
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "draft" in result["message"].lower()
    assert len(calls) == 1
    assert calls[0]["reason"] == "spec_not_read"


def test_declared_class_tampered_spec_downgrades_hash_mismatch(voice_gate, monkeypatch, spec_tree):
    """Bytes on disk no longer hash to what root recorded ⇒ draft(spec_hash_mismatch).
    (A tampered spec also cannot produce a read mark — spec_read refuses to mark it.)"""
    _set_config(monkeypatch, voice_library={"samples_path": "r2://x/"}, output_classes=_DECLARING)
    calls = _capture_audit(monkeypatch, voice_gate)
    (spec_tree / "classes/outbound_client/voice.md").write_text("attacker rewrote this\n")
    result = _gate(voice_gate)
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert len(calls) == 1
    assert calls[0]["reason"] == "spec_hash_mismatch"


def test_declared_class_with_no_installed_spec_downgrades_no_spec(
    voice_gate, monkeypatch, tmp_path
):
    """Declared ``expected`` and the manifest names no voice spec ⇒
    draft(no_spec). An outbound class refuses on a broken control either way,
    but the REASON has to be right: it is what tells an operator whether the
    firm has authoring left to do.

    The spec dir is a real, readable, empty tree — not an unset env var. Those
    were the same thing to this gate until ss-console #2234; now an unset
    ``SMD_SPEC_DIR`` reports ``spec_unprovable``, because a process that cannot
    look has not established that anything is absent.
    """
    from shared import spec_manifest

    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "customer": "t", "specs": {}})
    )
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tmp_path))
    _set_config(monkeypatch, voice_library={"samples_path": "r2://x/"}, output_classes=_DECLARING)
    calls = _capture_audit(monkeypatch, voice_gate)
    result = _gate(voice_gate)
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert len(calls) == 1
    assert calls[0]["reason"] == "no_spec"


def test_an_unresolvable_spec_dir_downgrades_spec_unprovable_not_no_spec(voice_gate, monkeypatch):
    """The distinction #2234 introduced, pinned from the outbound side.

    Reporting ``no_spec`` here would blame the firm for our own blindness — and
    the two want opposite responses: one is authoring work, the other is an
    env-propagation bug on the seat.
    """
    from shared import spec_manifest

    monkeypatch.delenv(spec_manifest.SPEC_DIR_ENV, raising=False)
    _set_config(monkeypatch, voice_library={"samples_path": "r2://x/"}, output_classes=_DECLARING)
    calls = _capture_audit(monkeypatch, voice_gate)
    result = _gate(voice_gate)
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert calls[0]["reason"] == "spec_unprovable"


def test_declared_class_register_error_downgrades_gate_error(voice_gate, monkeypatch, spec_tree):
    """A bound spec-regime evaluation fault fails closed (ADR 0028 §4)."""
    _set_config(monkeypatch, voice_library={"samples_path": "r2://x/"}, output_classes=_DECLARING)
    calls = _capture_audit(monkeypatch, voice_gate)

    class _Boom:
        def was_read(self, *_args):
            raise RuntimeError("register exploded")

    monkeypatch.setattr(voice_gate, "SPEC_STATUS", _Boom())
    result = _gate(voice_gate)
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert len(calls) == 1
    assert calls[0]["reason"] == "gate_error"


def test_spec_regime_binds_even_without_voice_library(voice_gate, monkeypatch, spec_tree):
    """A declared class binds the spec regime on its own — additive means the
    declaration can ADD a gate to a seat the old predicate left silent."""
    _set_config(monkeypatch, voice_library={}, output_classes=_DECLARING)
    calls = _capture_audit(monkeypatch, voice_gate)
    result = _gate(voice_gate)
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert calls[0]["reason"] == "spec_not_read"


# ===========================================================================
# The additive seams — fallback preserved where the spec regime does not govern
# ===========================================================================


def test_undeclared_class_on_declaring_seat_keeps_fallback(voice_gate, monkeypatch):
    """A seat declaring ONLY work_product keeps the B fallback on its
    outbound_client sends: no samples ⇒ draft(no_samples), exactly as today."""
    _set_config(
        monkeypatch,
        voice_library={"samples_path": "r2://x/"},
        output_classes={"work_product": {"voice_spec": "expected"}},
    )
    calls = _capture_audit(monkeypatch, voice_gate)
    result = _gate(voice_gate)
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert len(calls) == 1
    assert calls[0]["reason"] == "no_samples"


def test_voice_spec_none_keeps_fallback(voice_gate, monkeypatch):
    """``voice_spec: none`` is an authored choice, not a spec declaration — the
    fallback still governs the class on a voice-authored seat."""
    _set_config(
        monkeypatch,
        voice_library={"samples_path": "r2://x/"},
        output_classes={"outbound_client": {"voice_spec": "none"}},
    )
    calls = _capture_audit(monkeypatch, voice_gate)
    result = _gate(voice_gate)
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert calls[0]["reason"] == "no_samples"


def test_unresolvable_action_class_keeps_fallback(voice_gate, monkeypatch):
    """No resolvable output class (empty action_class_value) ⇒ the ORIGINAL
    binding, verbatim — the pre-repoint call shape keeps its behavior."""
    _set_config(monkeypatch, voice_library={"samples_path": "r2://x/"}, output_classes=_DECLARING)
    calls = _capture_audit(monkeypatch, voice_gate)
    result = _gate(voice_gate, action_class_value="")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert calls[0]["reason"] == "no_samples"


def test_malformed_output_classes_falls_back_to_old_regime(voice_gate, monkeypatch):
    """``output_classes`` unreadable on a voice-authored seat ⇒ the B fallback
    binds (fail-closed to the OLD regime) — never a silently unbound gate, and
    never a spec binding the config could not confirm."""

    class _Cfg:
        voice_library = {"samples_path": "r2://x/"}

        @property
        def output_classes(self):
            raise ValueError("customer.yaml: output_classes must be a mapping; got str")

    monkeypatch.setattr(
        customer_config.CustomerConfig, "from_volume", classmethod(lambda cls: _Cfg())
    )
    calls = _capture_audit(monkeypatch, voice_gate)
    result = _gate(voice_gate)
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert calls[0]["reason"] == "no_samples"


def test_wholly_unreadable_config_keeps_gate_silent(voice_gate, monkeypatch):
    """A config that cannot be read at all keeps the ORIGINAL posture: nothing
    is positively authored, so the gate stays silent (ADR 0035; the ceiling
    resolver upstream already fail-closes an unresolvable config)."""

    def _boom(cls):
        raise customer_config.CustomerConfigMissingError("no volume")

    monkeypatch.setattr(customer_config.CustomerConfig, "from_volume", classmethod(_boom))
    calls = _capture_audit(monkeypatch, voice_gate)
    assert _gate(voice_gate) is None
    assert calls == []


# ===========================================================================
# Audit shape — unchanged action_type, unchanged row, new reasons ride along
# ===========================================================================


def test_spec_regime_audit_row_shape_is_unchanged(voice_gate, monkeypatch, tmp_path):
    """A spec-regime downgrade emits through the SAME emitter: action_type
    VOICE_GATE_TRIGGERED and the same metadata keys the fallback emits — the
    new reason strings ride the existing ``reason`` field."""
    from shared import spec_manifest

    # A real, readable, empty spec tree so the reason under test is `no_spec`.
    # Leaving SMD_SPEC_DIR to the ambient environment made this assert on
    # whatever the machine happened to have; since #2234 an unset var reports
    # `spec_unprovable`, which is a different claim about a different fault.
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "customer": "t", "specs": {}})
    )
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tmp_path))
    _set_config(monkeypatch, voice_library={"samples_path": "r2://x/"}, output_classes=_DECLARING)

    executed: list[tuple] = []

    class _Client:
        def execute(self, sql, *params):
            executed.append((sql, params))

    monkeypatch.setattr(voice_gate, "_AUDIT_WIRED", True)
    monkeypatch.setattr(voice_gate, "_AUDIT_CLIENT", _Client())
    monkeypatch.setattr(voice_gate, "_AUDIT_CUSTOMER_SLUG", "ashton-price")

    result = voice_gate.check_voice_gate(
        tool_name="agentmail:send_message",
        action_class_value="external_send_client",
        session_id=SESSION,
        tool_call_id="tc-1",
    )
    assert isinstance(result, dict)
    assert len(executed) == 1
    _sql, params = executed[0]
    from shared.audit_contract import COLUMNS

    row = dict(zip(COLUMNS, params, strict=True))
    assert row["action_type"] == "VOICE_GATE_TRIGGERED"
    metadata = json.loads(row["metadata"])
    assert set(metadata) == {
        "voice_gate",
        "customer",
        "tool",
        "reason",
        "session_id",
        "tool_call_id",
    }
    assert metadata["reason"] == "no_spec"


# ===========================================================================
# Integration through enforce.evaluate_tool_call
# ===========================================================================


def _set_exposure(monkeypatch, enforce, mapping, *, persona="marcus"):
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", lambda slug="": dict(mapping))
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", persona)


def test_enforce_passes_resolved_class_and_spec_read_send_goes_out(monkeypatch, spec_tree):
    """End-to-end: an autonomous CLIENT send on a seat declaring outbound_client
    passes once the spec was read this turn — no Mechanism-B mark involved."""
    enforce = _load_trust("enforce")
    _set_exposure(
        monkeypatch,
        enforce,
        {enforce.ActionClass.EXTERNAL_SEND_CLIENT: enforce.Ceiling.AUTONOMOUS},
    )
    monkeypatch.setattr(
        enforce, "_reclassify_send", lambda *a, **k: enforce.ActionClass.EXTERNAL_SEND_CLIENT
    )
    _set_config(monkeypatch, voice_library={"samples_path": "r2://x/"}, output_classes=_DECLARING)
    _capture_audit(monkeypatch, enforce.voice_gate)
    SPEC_STATUS.mark_read("sess-x", "outbound_client", "voice")
    args = {"subject": "Saw your note", "text": "Got it, that works on my end. Talk soon."}
    assert (
        enforce.evaluate_tool_call("agentmail:send_message", args, "smd", session_id="sess-x")
        is None
    )


def test_enforce_declared_class_unread_spec_downgrades(monkeypatch, spec_tree):
    """The same send with the spec unread downgrades at the voice gate with the
    spec-regime reason — before the separate spec gate is ever consulted."""
    enforce = _load_trust("enforce")
    _set_exposure(
        monkeypatch,
        enforce,
        {enforce.ActionClass.EXTERNAL_SEND_CLIENT: enforce.Ceiling.AUTONOMOUS},
    )
    monkeypatch.setattr(
        enforce, "_reclassify_send", lambda *a, **k: enforce.ActionClass.EXTERNAL_SEND_CLIENT
    )
    _set_config(monkeypatch, voice_library={"samples_path": "r2://x/"}, output_classes=_DECLARING)
    calls = _capture_audit(monkeypatch, enforce.voice_gate)
    args = {"subject": "Saw your note", "text": "Got it, that works on my end. Talk soon."}
    result = enforce.evaluate_tool_call("agentmail:send_message", args, "smd", session_id="sess-x")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert calls[0]["reason"] == "spec_not_read"


def test_enforce_undeclared_outside_send_keeps_fallback_downgrade(monkeypatch):
    """An autonomous OUTSIDE send on a seat declaring only work_product keeps
    the fallback downgrade — the enforce seam preserves the additive rule."""
    enforce = _load_trust("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    _set_config(
        monkeypatch,
        voice_library={"samples_path": "r2://x/"},
        output_classes={"work_product": {"voice_spec": "expected"}},
    )
    calls = _capture_audit(monkeypatch, enforce.voice_gate)
    args = {"subject": "Saw your note", "text": "Got it, that works on my end. Talk soon."}
    result = enforce.evaluate_tool_call("agentmail:send_message", args, "smd", session_id="sess-x")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert calls[0]["reason"] == "no_samples"
