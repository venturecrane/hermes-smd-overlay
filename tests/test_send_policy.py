"""shared.send_policy resolver + bootstrap validator coverage (ss-console #2070).

The resolver is whole-block fail-closed: ANY fault resolves the entire block to
the platform defaults (today's exact caps, no exemption, no backstop, no held
release). The validator surfaces the same faults at authoring time so a typo is
caught before it silently tightens a seat.
"""

from __future__ import annotations

import pytest

from shared.send_policy import (
    DEFAULT_SEND_POLICY,
    live_send_policy,
    resolve_send_policy,
)

FULL_BLOCK = {
    "reply": {
        "internal_exempt": True,
        "per_sender_max": 5,
        "per_sender_window_seconds": 300,
        "global_max": 40,
        "global_window_seconds": 1800,
        "backstop_max": 60,
        "backstop_window_seconds": 3600,
    },
    "held_release": {"enabled": True, "ttl_seconds": 7200},
}


def test_absent_block_is_default() -> None:
    assert resolve_send_policy(None) == DEFAULT_SEND_POLICY


def test_default_matches_pre_change_constants() -> None:
    d = DEFAULT_SEND_POLICY
    assert (d.per_sender_max, d.per_sender_window_s) == (3, 600.0)
    assert (d.global_max, d.global_window_s) == (20, 3600.0)
    assert d.internal_exempt is False
    assert d.backstop_max == 0  # disabled
    assert d.held_release_enabled is False


def test_full_valid_block_resolves() -> None:
    p = resolve_send_policy(FULL_BLOCK)
    assert p.internal_exempt is True
    assert (p.per_sender_max, p.per_sender_window_s) == (5, 300.0)
    assert (p.global_max, p.global_window_s) == (40, 1800.0)
    assert (p.backstop_max, p.backstop_window_s) == (60, 3600.0)
    assert p.held_release_enabled is True and p.held_ttl_s == 7200.0


def test_partial_block_fills_defaults() -> None:
    p = resolve_send_policy({"reply": {"internal_exempt": True}})
    assert p.internal_exempt is True
    assert p.per_sender_max == DEFAULT_SEND_POLICY.per_sender_max
    assert p.backstop_max == 0
    assert p.held_release_enabled is False


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-mapping",
        {"unknown_top": {}},
        {"reply": "nope"},
        {"reply": {"unknown_key": 1}},
        {"reply": {"internal_exempt": "yes"}},
        {"reply": {"per_sender_max": -1}},
        {"reply": {"per_sender_max": True}},  # bool is not a count
        {"reply": {"per_sender_window_seconds": 0}},
        {"reply": {"global_window_seconds": -3}},
        {"held_release": {"enabled": "true"}},
        {"held_release": {"ttl_seconds": 0}},
        {"held_release": {"bogus": 1}},
        # One bad field poisons the WHOLE block — the exemption is dropped too.
        {"reply": {"internal_exempt": True, "backstop_max": -5}},
    ],
    ids=lambda r: str(r)[:60],
)
def test_any_fault_resolves_whole_block_to_default(raw) -> None:
    assert resolve_send_policy(raw) == DEFAULT_SEND_POLICY


def test_live_read_missing_file_is_default(tmp_path) -> None:
    assert live_send_policy(str(tmp_path / "missing.yaml")) == DEFAULT_SEND_POLICY


def test_live_read_authored(tmp_path) -> None:
    path = tmp_path / "customer.yaml"
    path.write_text(
        "customer_id: acme\n"
        "send_policy:\n"
        "  reply:\n"
        "    internal_exempt: true\n"
        "    backstop_max: 60\n"
    )
    p = live_send_policy(str(path))
    assert p.internal_exempt is True and p.backstop_max == 60


# ---------------------------------------------------------------------------
# bootstrap.validate — authoring-time surface for the same faults
# ---------------------------------------------------------------------------


def _validate(tmp_path, yaml_text: str) -> list[str]:
    from bootstrap.validate import validate_customer_yaml

    path = tmp_path / "customer.yaml"
    path.write_text(yaml_text)
    return [e for e in validate_customer_yaml(path) if "send_policy" in e]


_BASE = "customer_id: acme\nvertical: law-firm\npersonas:\n  - slug: ops\n    display_name: Ops\n"


def test_validator_accepts_full_block(tmp_path) -> None:
    yaml_text = _BASE + (
        "send_policy:\n"
        "  reply:\n"
        "    internal_exempt: true\n"
        "    per_sender_max: 3\n"
        "    per_sender_window_seconds: 600\n"
        "    global_max: 20\n"
        "    global_window_seconds: 3600\n"
        "    backstop_max: 60\n"
        "    backstop_window_seconds: 3600\n"
        "  held_release:\n"
        "    enabled: true\n"
        "    ttl_seconds: 86400\n"
    )
    assert _validate(tmp_path, yaml_text) == []


def test_validator_accepts_absent_block(tmp_path) -> None:
    assert _validate(tmp_path, _BASE) == []


@pytest.mark.parametrize(
    ("snippet", "needle"),
    [
        ("send_policy: []\n", "must be a mapping"),
        ("send_policy:\n  bogus: {}\n", "unknown key"),
        ("send_policy:\n  reply:\n    internal_exempt: 1\n", "must be a boolean"),
        ("send_policy:\n  reply:\n    per_sender_max: -2\n", "non-negative integer"),
        ("send_policy:\n  reply:\n    backstop_window_seconds: 0\n", "positive number"),
        ("send_policy:\n  reply:\n    mystery: 1\n", "unknown key"),
        ("send_policy:\n  held_release:\n    ttl_seconds: 0\n", "positive integer"),
        ("send_policy:\n  held_release:\n    enabled: nope\n", "must be a boolean"),
    ],
    ids=lambda v: v.replace("\n", " ")[:50],
)
def test_validator_rejects_malformed(tmp_path, snippet, needle) -> None:
    errors = _validate(tmp_path, _BASE + snippet)
    assert errors and any(needle in e for e in errors), errors
