"""Unit tests for the operator.runtime.config/v1 facts snapshot.

Covers the pure assembly (degraded[] is load-bearing — a missing introspection
is *unknown*, never *absent*) and the filesystem/secret-redaction adapters. The
no-secret-value guard lives in test_runtime_read_config_no_secret_values.py.
"""

from __future__ import annotations

import json

from shared import config_snapshot as cs


def _overlay_ref(value="abc123", source="direct_url"):
    return {"value": value, "source": source}


# --------------------------------------------------------------------------- #
# build_snapshot — pure assembly + degraded[] semantics
# --------------------------------------------------------------------------- #


def test_env_presence_maps_present_and_empty() -> None:
    snap = cs.build_snapshot(
        allowlist=["FOO", "BAR", "BAZ"],
        agent_env={"FOO": False, "BAR": True},  # BAR present-but-empty
        overlay_ref=_overlay_ref(),
        profiles=[],
        extra_degraded=[],
    )
    ep = snap["env_presence"]
    assert ep["FOO"] == {"present": True, "empty": False}
    assert ep["BAR"] == {"present": True, "empty": True}
    assert ep["BAZ"] == {"present": False, "empty": False}
    # Only allow-listed vars appear — never an environment dump.
    assert set(ep) == {"FOO", "BAR", "BAZ"}


def test_unknown_allowlist_degrades_env_presence() -> None:
    snap = cs.build_snapshot(
        allowlist=None,
        agent_env={"FOO": False},
        overlay_ref=_overlay_ref(),
        profiles=[],
        extra_degraded=[],
    )
    assert snap["env_presence"] is None
    assert any(d["field"] == "env_presence" for d in snap["degraded"])


def test_missing_agent_process_degrades_env_presence_not_gate_env() -> None:
    # The strip-violation check is only meaningful against the agent's env. If
    # the agent process isn't found, env_presence must degrade — NEVER fall back
    # to the gate's own env (which would be false-positive/negative prone).
    snap = cs.build_snapshot(
        allowlist=["FOO"],
        agent_env=None,
        overlay_ref=_overlay_ref(),
        profiles=[],
        extra_degraded=[],
    )
    assert snap["env_presence"] is None
    reasons = [d["reason"] for d in snap["degraded"] if d["field"] == "env_presence"]
    assert reasons == ["agent process not introspectable"]


def test_missing_overlay_ref_degrades() -> None:
    snap = cs.build_snapshot(
        allowlist=["FOO"],
        agent_env={"FOO": False},
        overlay_ref={"value": None, "source": None},
        profiles=[],
        extra_degraded=[],
    )
    assert any(d["field"] == "overlay_ref" for d in snap["degraded"])


def test_registry_always_degraded_and_schema_stamped() -> None:
    snap = cs.build_snapshot(
        allowlist=["FOO"],
        agent_env={"FOO": False},
        overlay_ref=_overlay_ref(),
        profiles=[{"slug": "crane"}],
        extra_degraded=[{"field": "materialized.x", "reason": "y"}],
    )
    assert snap["schema"] == cs.SCHEMA
    # registry lives in the agent's memory — not gate-readable, honestly degraded.
    assert any(d["field"] == "registry" for d in snap["degraded"])
    # extra_degraded (from the profile reader) is carried through.
    assert any(d["field"] == "materialized.x" for d in snap["degraded"])
    assert snap["materialized"]["profiles"] == [{"slug": "crane"}]


# --------------------------------------------------------------------------- #
# read_profiles — filesystem adapter
# --------------------------------------------------------------------------- #


def test_read_profiles_absent_dir_degrades(tmp_path) -> None:
    profiles, degraded = cs.read_profiles(str(tmp_path))
    assert profiles == []
    assert degraded and degraded[0]["field"] == "materialized.profiles"


def test_read_profiles_reads_config_skills_and_cron(tmp_path) -> None:
    prof = tmp_path / "profiles" / "crane"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text(
        "skills:\n  - inbox-triage\n  - workspace\nmodel: claude-opus-4-8\n",
        encoding="utf-8",
    )
    cron = prof / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "op-managed:smd:inbox-triage",
                        "schedule": "0 7-19 * * *",
                        "skill": "inbox-triage",
                        "last_status": "ok",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    profiles, degraded = cs.read_profiles(str(tmp_path))
    assert len(profiles) == 1
    p = profiles[0]
    assert p["slug"] == "crane"
    assert p["config_present"] is True
    assert p["config_sha256"] and len(p["config_sha256"]) == 64
    assert p["skills_enabled"] == ["inbox-triage", "workspace"]
    assert p["cron"]["available"] is True
    assert p["cron"]["jobs"][0]["name"] == "op-managed:smd:inbox-triage"
    assert p["cron"]["jobs"][0]["last_status"] == "ok"
    assert degraded == []


def test_read_profiles_no_cron_is_determinable_not_degraded(tmp_path) -> None:
    # A profile with no jobs.json is a real "no cron materialized" fact (the diff
    # engine compares to authored cron) — NOT a degraded read.
    prof = tmp_path / "profiles" / "crane"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("skills: []\n", encoding="utf-8")
    profiles, degraded = cs.read_profiles(str(tmp_path))
    assert profiles[0]["cron"] == {"available": True, "jobs": []}
    assert degraded == []


def test_read_profiles_unparseable_config_degrades(tmp_path) -> None:
    prof = tmp_path / "profiles" / "crane"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("{ this: is: not: valid: yaml", encoding="utf-8")
    profiles, degraded = cs.read_profiles(str(tmp_path))
    assert profiles[0]["config_present"] is True
    assert profiles[0]["config_sha256"] is None
    assert any("config_sha256" in d["field"] for d in degraded)


def test_unparseable_cron_degrades_that_profile(tmp_path) -> None:
    prof = tmp_path / "profiles" / "crane"
    (prof / "cron").mkdir(parents=True)
    (prof / "config.yaml").write_text("skills: []\n", encoding="utf-8")
    (prof / "cron" / "jobs.json").write_text("{ not json", encoding="utf-8")
    profiles, degraded = cs.read_profiles(str(tmp_path))
    assert profiles[0]["cron"] == {"available": False, "jobs": []}
    assert any(d["field"].endswith(".cron") for d in degraded)


# --------------------------------------------------------------------------- #
# secret redaction in the config digest
# --------------------------------------------------------------------------- #


def test_config_digest_is_secret_value_independent(tmp_path) -> None:
    prof_a = tmp_path / "a" / "profiles" / "crane"
    prof_b = tmp_path / "b" / "profiles" / "crane"
    prof_a.mkdir(parents=True)
    prof_b.mkdir(parents=True)
    # Identical except a secret-keyed value — the digest must NOT differ.
    (prof_a / "config.yaml").write_text("skills: []\napi_token: SECRET-AAAA\n", encoding="utf-8")
    (prof_b / "config.yaml").write_text("skills: []\napi_token: SECRET-BBBB\n", encoding="utf-8")
    pa, _ = cs.read_profiles(str(tmp_path / "a"))
    pb, _ = cs.read_profiles(str(tmp_path / "b"))
    assert pa[0]["config_sha256"] == pb[0]["config_sha256"]


def test_extract_skill_names_unknown_shape_returns_none() -> None:
    assert cs._extract_skill_names([{"no_name": 1}]) is None
    assert cs._extract_skill_names("notalist") is None
    assert cs._extract_skill_names(None) == []
    assert cs._extract_skill_names(["a", {"name": "b"}]) == ["a", "b"]
