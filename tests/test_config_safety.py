"""Unit tests for ``config_applier.safety`` — pure live-apply decision logic.

Covers the ceiling-direction classifier, vertical/content floor preservation,
the live-writability allow-list (including the rebuild-class never-list), the
diff walker, and the monotonic config-epoch counter. Every fail-closed branch
is exercised: unknown ceilings read as widening, unexpected shapes read as
floor violations / non-writable.
"""

import pytest

from config_applier import safety
from config_applier.safety import (
    CEILING_ORDER,
    Direction,
    changed_paths,
    classify_direction,
    floor_preserving,
    live_writable,
    next_epoch,
    non_live_writable_changes,
    vertical_floors,
)

# ---------------------------------------------------------------------------
# classify_direction
# ---------------------------------------------------------------------------


def test_ceiling_order_is_least_to_most_permissive():
    assert CEILING_ORDER == ("refused", "draft_for_review", "autonomous")


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("autonomous", "draft_for_review", Direction.TIGHTENING),
        ("draft_for_review", "refused", Direction.TIGHTENING),
        ("autonomous", "refused", Direction.TIGHTENING),
        ("refused", "draft_for_review", Direction.WIDENING),
        ("draft_for_review", "autonomous", Direction.WIDENING),
        ("refused", "autonomous", Direction.WIDENING),
        ("autonomous", "autonomous", Direction.SAME),
        ("refused", "refused", Direction.SAME),
    ],
)
def test_classify_direction(old, new, expected):
    assert classify_direction(old, new) == expected


def test_classify_direction_is_case_and_space_tolerant():
    assert classify_direction("AUTONOMOUS", " refused ") == Direction.TIGHTENING


def test_classify_direction_unknown_new_reads_as_widening():
    # Fail closed: an unparseable new ceiling must not slip through as a
    # tightening or a no-op. It reads as maximally permissive → widening.
    assert classify_direction("draft_for_review", "yolo") == Direction.WIDENING
    assert classify_direction("autonomous", None) == Direction.WIDENING


def test_classify_direction_unknown_old_reads_as_tightening_to_known():
    # An unknown old (maximally permissive) → a known new reads as tightening.
    assert classify_direction("garbage", "draft_for_review") == Direction.TIGHTENING


# ---------------------------------------------------------------------------
# vertical_floors
# ---------------------------------------------------------------------------


def test_vertical_floors_law_firm_external_send():
    assert vertical_floors("law-firm") == {"external_send": "draft_for_review"}


def test_vertical_floors_unknown_vertical_is_empty():
    assert vertical_floors("mixed") == {}
    assert vertical_floors("home-services") == {}


def test_vertical_floors_non_string_is_empty():
    assert vertical_floors(None) == {}
    assert vertical_floors(42) == {}


# ---------------------------------------------------------------------------
# floor_preserving
# ---------------------------------------------------------------------------


def _law_cfg(external_send: str | None) -> dict:
    # ADR 0056: exposure is authored PER persona at entitlements.exposure.
    exposure: dict = {}
    if external_send is not None:
        exposure["external_send"] = external_send
    return {
        "vertical": "law-firm",
        "personas": [{"slug": "marcus", "entitlements": {"exposure": exposure}}],
    }


def test_floor_preserving_no_vertical_floor_always_true():
    # mixed has no declared floor — any authored exposure is fine.
    cfg = {
        "vertical": "mixed",
        "personas": [{"slug": "m", "entitlements": {"exposure": {"external_send": "autonomous"}}}],
    }
    assert floor_preserving({}, cfg) is True


def test_floor_preserving_rejects_when_any_persona_exceeds_floor():
    # A multi-persona law config where ONE persona raises external_send above the
    # floor is rejected even though the other is clean (exposure is per-persona).
    cfg = {
        "vertical": "law-firm",
        "personas": [
            {"slug": "clean", "entitlements": {"exposure": {"external_send": "draft_for_review"}}},
            {"slug": "loud", "entitlements": {"exposure": {"external_send": "autonomous"}}},
        ],
    }
    assert floor_preserving({}, cfg) is False


def test_floor_preserving_authored_at_floor_is_ok():
    assert floor_preserving({}, _law_cfg("draft_for_review")) is True


def test_floor_preserving_authored_below_floor_is_ok():
    # refused is MORE restrictive than the draft_for_review floor — narrowing is
    # always allowed.
    assert floor_preserving({}, _law_cfg("refused")) is True


def test_floor_preserving_authored_above_floor_is_rejected():
    # autonomous is wider than the draft_for_review floor — a live apply would
    # widen past the compliance floor. Reject.
    assert floor_preserving({}, _law_cfg("autonomous")) is False


def test_floor_preserving_unauthored_action_is_ok():
    # Unauthored external_send: the runtime applies the pack floor itself, so it
    # is floor-preserving by construction.
    assert floor_preserving({}, _law_cfg(None)) is True


def test_floor_preserving_non_mapping_new_fails_closed():
    assert floor_preserving({}, None) is False
    assert floor_preserving({}, "not-a-config") is False


def test_floor_preserving_garbled_ceiling_fails_closed():
    # An unparseable authored ceiling ranks as maximally permissive → above the
    # floor → rejected.
    assert floor_preserving({}, _law_cfg("yolo")) is False


# ---------------------------------------------------------------------------
# live_writable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "personas.0.entitlements.exposure",
        "personas.0.entitlements.exposure.external_send",
        "escalation",
        "escalation.red_flag_recipients",
        "webhook_triggers",
        "webhook_triggers.0",
        "scope.inbound_allow_from",
        "scope.inbound_allow_from.0",
        "personas.0.skills.2.enabled",
        "personas.1.skills.0.initiation",
        "personas.1.skills.0.initiation.scheduled",
    ],
)
def test_live_writable_allows_allow_list(path):
    assert live_writable(path) is True


@pytest.mark.parametrize(
    "path",
    [
        # The retired entitlement paths are NOT live-writable (ADR 0056): a diff
        # touching them forces a re-provision / is rejected by the applier.
        "scope.trust_ceiling",
        "scope.action_ceilings",
        "scope.action_ceilings.external_send",
        "personas.1.skills.0.trust_ceiling",
    ],
)
def test_live_writable_rejects_legacy_entitlement_paths(path):
    assert live_writable(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "vertical",
        "model",
        "memory",
        "memory.d1_namespace",
        "hermes_ref",
        "customer_id",
        "fly_region",
        "connectors",
        "connectors.Email.backend",
        "personas.0.google_auth",
        "personas.0.oauth.scopes",
        "personas.0.slug",
        "personas.0.status",
    ],
)
def test_live_writable_rejects_rebuild_class(path):
    assert live_writable(path) is False


def test_live_writable_rejects_persona_name_change():
    # A persona name is not on the allow-list and not on the never-list; the
    # default is reject (allow-list is exhaustive).
    assert live_writable("personas.0.name") is False


def test_live_writable_rejects_unknown_path():
    assert live_writable("some.random.path") is False


def test_live_writable_rejects_non_string_or_empty():
    assert live_writable(None) is False
    assert live_writable("") is False
    assert live_writable("   ") is False
    assert live_writable(42) is False


def test_live_writable_prefix_match_respects_segment_boundary():
    # ``escalation_extra`` must NOT match the ``escalation`` prefix.
    assert live_writable("escalation_extra") is False


def test_live_writable_never_list_beats_allow_list_for_persona_oauth():
    # personas.*.google_auth is rebuild-class even though personas.* skill leaves
    # are writable — the never-list wins.
    assert live_writable("personas.0.google_auth.subject") is False


# ---------------------------------------------------------------------------
# changed_paths / non_live_writable_changes
# ---------------------------------------------------------------------------


def test_changed_paths_detects_scalar_change():
    old = {"vertical": "law-firm", "model": "claude-opus-4-7"}
    new = {"vertical": "law-firm", "model": "claude-opus-4-8"}
    assert changed_paths(old, new) == ["model"]


def test_changed_paths_detects_nested_change():
    old = {"scope": {"trust_ceiling": "draft_for_review"}}
    new = {"scope": {"trust_ceiling": "autonomous"}}
    assert changed_paths(old, new) == ["scope.trust_ceiling"]


def test_changed_paths_detects_added_and_removed_keys():
    old = {"a": 1}
    new = {"b": 2}
    assert changed_paths(old, new) == ["a", "b"]


def test_changed_paths_addresses_list_elements_by_index():
    old = {"personas": [{"skills": [{"enabled": False}]}]}
    new = {"personas": [{"skills": [{"enabled": True}]}]}
    assert changed_paths(old, new) == ["personas.0.skills.0.enabled"]


def test_changed_paths_reports_list_length_change():
    old = {"webhook_triggers": [{"source": "a"}]}
    new = {"webhook_triggers": [{"source": "a"}, {"source": "b"}]}
    assert changed_paths(old, new) == ["webhook_triggers.1"]


def test_changed_paths_type_change_reports_parent():
    old = {"scope": {"trust_ceiling": "autonomous"}}
    new = {"scope": "autonomous"}
    assert changed_paths(old, new) == ["scope"]


def test_changed_paths_identical_is_empty():
    cfg = {"a": {"b": [1, 2, 3]}}
    assert changed_paths(cfg, dict(cfg)) == []


def test_changed_paths_root_type_change_reports_dot():
    assert changed_paths({"a": 1}, ["a"]) == ["."]


def test_non_live_writable_changes_empty_when_all_writable():
    old = {
        "personas": [{"entitlements": {"exposure": {"external_send": "autonomous"}}}],
        "escalation": {"to": ["a@x"]},
    }
    new = {
        "personas": [{"entitlements": {"exposure": {"external_send": "draft_for_review"}}}],
        "escalation": {"to": ["b@x"]},
    }
    assert non_live_writable_changes(old, new) == []


def test_non_live_writable_changes_flags_rebuild_class():
    old = {
        "model": "claude-opus-4-7",
        "personas": [{"entitlements": {"exposure": {"external_send": "autonomous"}}}],
    }
    new = {
        "model": "claude-opus-4-8",
        "personas": [{"entitlements": {"exposure": {"external_send": "refused"}}}],
    }
    # model is rebuild-class; personas.*.entitlements.exposure is live-writable.
    assert non_live_writable_changes(old, new) == ["model"]


def test_non_live_writable_changes_flags_connector_backend_swap():
    old = {"connectors": {"PM": {"backend": "mcp:clio"}}}
    new = {"connectors": {"PM": {"backend": "build:filevine"}}}
    assert non_live_writable_changes(old, new) == ["connectors.PM.backend"]


# ---------------------------------------------------------------------------
# next_epoch
# ---------------------------------------------------------------------------


def test_next_epoch_increments():
    assert next_epoch(0) == 1
    assert next_epoch(1) == 2
    assert next_epoch(41) == 42


def test_next_epoch_missing_resets_to_one():
    assert next_epoch(None) == 1


def test_next_epoch_non_integer_resets_to_one():
    assert next_epoch("5") == 1
    assert next_epoch(3.5) == 1


def test_next_epoch_negative_resets_to_one():
    assert next_epoch(-7) == 1


def test_next_epoch_rejects_bool():
    # True is an int subclass but is never a real epoch.
    assert next_epoch(True) == 1
    assert next_epoch(False) == 1


def test_safety_public_surface():
    # Guard the __all__ surface the applier + boot script import.
    for name in (
        "CEILING_ORDER",
        "Direction",
        "classify_direction",
        "floor_preserving",
        "live_writable",
        "next_epoch",
        "non_live_writable_changes",
    ):
        assert hasattr(safety, name)


# ---------------------------------------------------------------------------
# Derive-don't-duplicate: the apply-time floor map MUST track enforce.py
#
# Both surfaces derive from the single source ``shared.action_classes.
# VERTICAL_FLOORS``. These tests pin that contract so a floor added to the shared
# map (or to enforce.py's runtime realization) can never silently diverge from
# the apply-time gate. Reviewed concern, overlay PR #81 (2026-06-15).
# ---------------------------------------------------------------------------


def test_vertical_floors_reads_shared_source_of_truth():
    from shared.action_classes import VERTICAL_FLOORS

    # safety.vertical_floors() must return exactly what the shared map declares
    # for every vertical it knows, by string key.
    for vertical, floors in VERTICAL_FLOORS.items():
        assert vertical_floors(vertical) == dict(floors)


def test_apply_floor_map_matches_enforce_runtime_map():
    # enforce.py builds an enum-keyed runtime map from the same shared source;
    # convert it back to strings and assert byte-for-byte agreement with the
    # apply-time string map for law-firm. A hand-copy in either place would fail
    # this the moment the two drifted.
    from tests.conftest import load_plugin

    enforce = load_plugin("hermes-smd-trust").enforce
    enforce_law = {
        ac.value: ceiling.value for ac, ceiling in enforce._VERTICAL_FLOORS["law-firm"].items()
    }
    assert vertical_floors("law-firm") == enforce_law
    # And the concrete contract value the law-firm pack pins.
    assert vertical_floors("law-firm") == {"external_send": "draft_for_review"}
