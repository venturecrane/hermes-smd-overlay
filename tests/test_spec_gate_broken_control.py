"""Broken-control dispositions in the authored-spec gate (ss-console #2234).

A class declares a spec and none was ever installed. Before 2026-08-10 every
class refused, which on ``pilot-smokeball`` meant six days of internal mail
failing silently with a remedy the model could not perform — there was no spec
to read (ss-console #2228). The Captain's ruling changed what a broken control
COSTS, and made it depend on who is waiting:

* ``staff`` — a person inside the firm waiting on ops mail → the send proceeds
  in the persona's own authored register, and an alert is raised.
* ``outbound_*`` — the firm's voice to someone outside it, where the persona's
  register is the wrong voice rather than a neutral one → draft for a human.
* ``work_product`` / ``record`` — artifacts nobody is blocked on → still refuse.

Half of this file exists to stop the permissive branch from spreading past the
one class that earned it. Three tests in particular are load-bearing:
``spec_dir`` unresolvable must NOT read as "nothing installed", a tampered spec
must not become an escape hatch, and ``work_product`` must keep refusing —
ss-console's runtime-controls registry marks that behaviour ``enforced`` on a
live observation, so inverting it would falsify a certified control.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from shared import spec_gate, spec_manifest
from shared.spec_status import SPEC_STATUS

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "hermes-smd-trust"))

SESSION = "sess-broken-control"


@pytest.fixture(autouse=True)
def _clean():
    SPEC_STATUS._reset_for_tests()
    spec_gate._AUDIT_WIRED = True  # skip audit wiring; the disposition is the subject
    spec_gate._AUDIT_CLIENT = None
    spec_gate._AUDIT_CUSTOMER_SLUG = None
    yield
    SPEC_STATUS._reset_for_tests()


def _declare(monkeypatch, declaration):
    class FakeConfig:
        output_classes = declaration

        @classmethod
        def from_volume(cls):
            return cls()

    monkeypatch.setattr(spec_gate, "CustomerConfig", FakeConfig)


def _tree(tmp_path, specs, *, manifest=None, assertions=None):
    """Build an installed spec tree.

    ``specs`` maps rel_path -> (output_class, property); each file gets real
    bytes and a real digest so ``verify`` passes. ``{}`` writes a valid manifest
    naming nothing (the ordinary nothing-installed state). ``manifest=`` writes
    raw text instead, for the unparseable case.

    Deliberately a real on-disk tree rather than a monkeypatch of
    ``entries_for_class``: patching that helper bypasses ``spec_dir`` and
    ``load_entries``, which is precisely the seam these tests exist to pin.
    """
    if manifest is not None:
        (tmp_path / "manifest.json").write_text(manifest)
        return tmp_path
    entries = {}
    for rel, (output_class, prop) in specs.items():
        body = f"authored {output_class} {prop}\n"
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        entries[rel] = {
            "class": output_class,
            "property": prop,
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
            "bytes": len(body),
            "assertions": (assertions or {}) if prop == "format" else {},
        }
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "customer": "pilot-smokeball",
                "source_digest": "deadbeef",
                "specs": entries,
            }
        )
    )
    return tmp_path


def _staff_expected(monkeypatch):
    _declare(monkeypatch, {"staff": {"voice_spec": "expected", "format_spec": "none"}})


def _staff_send(**kwargs):
    return spec_gate.check_spec_gate(
        tool_name="mcp_agentmail_send_message",
        action_class_value="external_send_internal",
        session_id=SESSION,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# manifest_state — the distinction everything below rests on
# ---------------------------------------------------------------------------


def test_manifest_state_separates_cannot_look_from_nothing_installed(monkeypatch, tmp_path):
    """``load_entries`` returns {} for both, and every one of its callers reads
    that as fail-closed. This is the one question it cannot answer."""
    monkeypatch.delenv(spec_manifest.SPEC_DIR_ENV, raising=False)
    assert spec_manifest.manifest_state() == spec_manifest.STATE_UNREADABLE

    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tmp_path))
    assert spec_manifest.manifest_state() == spec_manifest.STATE_ABSENT

    _tree(tmp_path, {}, manifest="{not json")
    assert spec_manifest.manifest_state() == spec_manifest.STATE_UNREADABLE

    _tree(tmp_path, {"classes/staff/voice.md": ("staff", "voice")})
    assert spec_manifest.manifest_state() == spec_manifest.STATE_OK


def test_manifest_state_rejects_a_parsed_document_with_no_specs_map(monkeypatch, tmp_path):
    """Parsed JSON is not a parsed MANIFEST. A document without a ``specs``
    mapping tells us nothing about what is installed."""
    monkeypatch.setenv(
        spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {}, manifest=json.dumps({"specs": []})))
    )
    assert spec_manifest.manifest_state() == spec_manifest.STATE_UNREADABLE


# ---------------------------------------------------------------------------
# The waiver — exactly one class
# ---------------------------------------------------------------------------


def test_staff_send_proceeds_when_its_voice_spec_was_never_installed(monkeypatch, tmp_path):
    """The Captain's ruling. ADR 0083's decision sentence already said each
    property is "authored by the customer or fails closed to the persona's own
    authored judgment"; the runtime refused instead."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {})))
    _staff_expected(monkeypatch)
    assert _staff_send() is None


def test_staff_waiver_also_applies_when_no_manifest_was_ever_written(monkeypatch, tmp_path):
    """A spec dir that exists and holds no manifest is the ordinary
    nothing-was-ever-installed state — the applier writes manifest.json last as
    its commit point, and entrypoint.sh creates the dir on every boot."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tmp_path))
    _staff_expected(monkeypatch)
    assert _staff_send() is None


@pytest.mark.parametrize("output_class", ["work_product", "record"])
def test_internal_artifacts_still_refuse(monkeypatch, tmp_path, output_class):
    """The classes the waiver must NOT reach.

    ``draft_delivery_gate`` in ss-console's runtime-controls registry is
    ``status: enforced`` on exactly this state, observed live at
    vfy_01KYZNTJAEST5HEVJATYFY9ED3 (work_product declared + empty spec dir =
    REFUSED). Nobody is blocked on an artifact, so refusing costs nothing. Note
    that ``_INTERNAL_ARTIFACT_CLASSES`` in the gate means work_product/record —
    a change that read the word "internal" loosely would invert this silently.
    """
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {})))
    _declare(monkeypatch, {output_class: {"voice_spec": "expected", "format_spec": "none"}})
    block = spec_gate.check_spec_gate(
        tool_name="smd_deliver_draft",
        action_class_value="",
        session_id=SESSION,
        output_class=output_class,
    )
    assert block is not None and block["action"] == "block"


@pytest.mark.parametrize(
    ("action", "output_class"),
    [
        ("external_send_client", "outbound_client"),
        ("external_send_vendor", "outbound_vendor"),
        ("external_send", "outbound_external"),
    ],
)
def test_outbound_drafts_and_never_names_an_unfollowable_remedy(
    monkeypatch, tmp_path, action, output_class
):
    """Outbound still routes to a human — but the message must not tell the
    model to read a spec that does not exist. That instruction is what the seat
    spent six days attempting."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {})))
    _declare(monkeypatch, {output_class: {"voice_spec": "expected", "format_spec": "none"}})
    block = spec_gate.check_spec_gate(
        tool_name="mcp_agentmail_send_message",
        action_class_value=action,
        session_id=SESSION,
    )
    assert block is not None and block["action"] == "block"
    assert "no such spec is installed" in block["message"]
    assert "Read the spec named in your skill" not in block["message"]


# ---------------------------------------------------------------------------
# The states that are NOT broken controls
# ---------------------------------------------------------------------------


def test_staff_send_blocks_when_the_spec_dir_cannot_be_resolved(monkeypatch):
    """The fail-open guard, and the whole reason ``manifest_state`` exists.

    ``SMD_SPEC_DIR`` unset is not evidence that nothing is installed — it is
    evidence this process cannot look. An ABSENT spec dir PASSES the boot gate
    (operator/safety-substrate/invariants/spec_dir_ownership.py), and the
    heartbeat that reports seat health runs in the gateway process while this
    gate runs in the agent process, so the two can disagree about the env while
    every health signal reads green. If this returned None, a lost env var would
    silently unlock autonomous sending.
    """
    monkeypatch.delenv(spec_manifest.SPEC_DIR_ENV, raising=False)
    _staff_expected(monkeypatch)
    block = _staff_send()
    assert block is not None and block["action"] == "block"


def test_staff_send_blocks_when_the_manifest_is_unparseable(monkeypatch, tmp_path):
    """Same rule from the other side: a corrupt manifest proves nothing."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {}, manifest="{not json")))
    _staff_expected(monkeypatch)
    block = _staff_send()
    assert block is not None and block["action"] == "block"


def test_staff_send_blocks_when_the_installed_spec_no_longer_matches(monkeypatch, tmp_path):
    """Tamper must never become the escape hatch. If damaged bytes took the
    broken-control path, the permissive branch would be reachable by rewriting a
    file — the adversary root ownership exists to exclude."""
    tree = _tree(tmp_path, {"classes/staff/voice.md": ("staff", "voice")})
    (tree / "classes/staff/voice.md").write_text("rewritten after root recorded it\n")
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tree))
    _staff_expected(monkeypatch)
    block = _staff_send()
    assert block is not None and block["action"] == "block"


def test_staff_send_still_blocks_when_an_installed_spec_was_not_read(monkeypatch, tmp_path):
    """Unchanged, and the distinction the whole change rests on: a spec that
    EXISTS and was not read is the model's failure, with a remedy it can
    perform. Only a spec that does not exist gets the waiver."""
    monkeypatch.setenv(
        spec_manifest.SPEC_DIR_ENV,
        str(_tree(tmp_path, {"classes/staff/voice.md": ("staff", "voice")})),
    )
    _staff_expected(monkeypatch)
    block = _staff_send()
    assert block is not None and "did not read it" in block["message"]


# ---------------------------------------------------------------------------
# Per-property, and the format half
# ---------------------------------------------------------------------------


def test_a_missing_voice_spec_does_not_excuse_a_real_format_violation(monkeypatch, tmp_path):
    """Present controls keep binding. The waiver is per-property, never a
    blanket pass for the class."""
    monkeypatch.setenv(
        spec_manifest.SPEC_DIR_ENV,
        str(
            _tree(
                tmp_path,
                {"classes/staff/format.md": ("staff", "format")},
                assertions={"forbid_substrings": ["utilize"]},
            )
        ),
    )
    _declare(monkeypatch, {"staff": {"voice_spec": "expected", "format_spec": "expected"}})
    block = _staff_send(body="We will utilize the process.")
    assert block is not None and "does not have the shape" in block["message"]


def test_a_format_bound_send_with_an_uninspectable_body_blocks(monkeypatch, tmp_path):
    """``_extract_send_body`` returns None to mean INDETERMINATE and the content
    floor fails toward draft on it. This gate used to coerce that None to "" and
    skip the format check entirely — one value, two adjacent call sites,
    opposite dispositions."""
    monkeypatch.setenv(
        spec_manifest.SPEC_DIR_ENV,
        str(_tree(tmp_path, {"classes/staff/format.md": ("staff", "format")})),
    )
    _declare(monkeypatch, {"staff": {"voice_spec": "none", "format_spec": "expected"}})
    block = _staff_send(body=None)
    assert block is not None and block["action"] == "block"


def test_a_missing_format_spec_is_a_broken_control_not_a_silent_pass(monkeypatch, tmp_path):
    """The mirror-image bug (Captain, 2026-08-10). A declared format spec that
    was never installed used to yield no violations and PASS — the opposite
    failure direction from voice, inside the same function."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {})))
    _declare(monkeypatch, {"outbound_client": {"voice_spec": "none", "format_spec": "expected"}})
    block = spec_gate.check_spec_gate(
        tool_name="mcp_agentmail_send_message",
        action_class_value="external_send_client",
        session_id=SESSION,
        body="a body the absent spec cannot be checked against",
    )
    assert block is not None and "no such spec is installed" in block["message"]


def test_an_undeclared_seat_is_untouched_by_all_of_this(monkeypatch, tmp_path):
    """The binding condition is unchanged: a seat that declares nothing is
    silent, whatever the spec tree looks like."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {})))
    _declare(monkeypatch, {})
    assert _staff_send() is None
