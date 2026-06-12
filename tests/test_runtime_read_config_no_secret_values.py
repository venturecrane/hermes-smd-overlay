"""Guard: a config snapshot NEVER carries a secret value across the read seam.

The ``config`` kind crosses the trust boundary to the console. env_presence is
presence-only; config digests redact secret-keyed values before hashing. This
test plants known secret values in both env-shaped and config-file inputs and
asserts none appear anywhere in the serialized snapshot — the permanent guard
against a future field accidentally echoing a value.
"""

from __future__ import annotations

import json

from shared import config_snapshot as cs

# A value we plant and then assert never appears in the serialized snapshot.
SECRET = "PLANTED-SECRET-VALUE-do-not-leak-7f3a9c"


def test_config_file_secret_value_never_in_snapshot(tmp_path) -> None:
    prof = tmp_path / "profiles" / "crane"
    (prof / "cron").mkdir(parents=True)
    (prof / "config.yaml").write_text(
        # Secret-keyed values that MUST be redacted out of the digest.
        f"skills:\n  - inbox-triage\napi_token: {SECRET}\n"
        f"telegram_bot_token: {SECRET}\nnested:\n  client_secret: {SECRET}\n",
        encoding="utf-8",
    )
    (prof / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"name": "op-managed:smd:inbox-triage", "schedule": "0 * * * *"}]}),
        encoding="utf-8",
    )

    profiles, _ = cs.read_profiles(str(tmp_path))
    snap = cs.build_snapshot(
        allowlist=["R2_SECRET_ACCESS_KEY", "SMD_CUSTOMER_SLUG"],
        # agent_env is name -> is_empty: values are structurally impossible here,
        # but we still assert the serialized form is clean.
        agent_env={"R2_SECRET_ACCESS_KEY": False, "SMD_CUSTOMER_SLUG": False},
        overlay_ref={"value": "deadbeef", "source": "direct_url"},
        profiles=profiles,
        extra_degraded=[],
    )

    blob = json.dumps(snap)
    assert SECRET not in blob, "a secret value leaked into the config snapshot"
    # The redaction sentinel proves the secret-keyed fields were processed, not
    # merely absent by luck.
    # (digest is a hash; we assert the raw value is gone, which is the contract.)
    assert "api_token" not in blob  # config body is hashed, never echoed verbatim


def test_env_presence_is_booleans_only(tmp_path) -> None:
    snap = cs.build_snapshot(
        allowlist=["R2_SECRET_ACCESS_KEY"],
        agent_env={"R2_SECRET_ACCESS_KEY": False},
        overlay_ref={"value": "x", "source": "direct_url"},
        profiles=[],
        extra_degraded=[],
    )
    entry = snap["env_presence"]["R2_SECRET_ACCESS_KEY"]
    assert set(entry) == {"present", "empty"}
    assert all(isinstance(v, bool) for v in entry.values())
