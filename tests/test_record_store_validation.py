"""``record_stores`` validates as a closed shape at provisioning time.

The runtime rule (``shared.record_store``) and this one are the same function,
so a store that provisioning accepts is one the tool will serve, and a store
the tool would refuse never reaches a volume. Each case names the way a store
could otherwise reach past its own directory.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from bootstrap.validate import validate_customer_yaml

_BASE = dedent(
    """\
    schema_version: 1
    customer_id: scott
    customer_name: Scott Durgan
    vertical: mixed
    fly_region: lax
    model: claude-sonnet-4-6
    hermes_ref: v2026.9.14
    personas:
      - slug: agent-crane
        status: active
        name: Agent Crane
        title: Chief of Staff
        entitlements:
          exposure:
            internal_write: autonomous
    scope:
      inbound_allow_from:
        - smdurgan@icloud.com
    """
)


def _validate(tmp_path: Path, block: str) -> list[str]:
    path = tmp_path / "customer.yaml"
    path.write_text(_BASE + block)
    return [e for e in validate_customer_yaml(path) if "record_stores" in e]


def test_an_authored_store_under_the_volume_validates(tmp_path: Path) -> None:
    block = (
        "record_stores:\n  - name: open-house-visitors\n    path: /opt/data/open-house/visitors\n"
    )
    assert _validate(tmp_path, block) == []


def test_absent_is_valid(tmp_path: Path) -> None:
    assert _validate(tmp_path, "") == []


@pytest.mark.parametrize(
    ("block", "fragment"),
    [
        ("record_stores: /opt/data/x\n", "must be a list"),
        ("record_stores:\n  - /opt/data/x\n", "must be a mapping"),
        ("record_stores:\n  - name: Open House\n    path: /opt/data/x\n", "kebab-case"),
        ("record_stores:\n  - name: a\n    path: relative/x\n", "absolute path"),
        ("record_stores:\n  - name: a\n    path: /tmp/x\n", "must live under /opt/data"),
        ("record_stores:\n  - name: a\n    path: /opt/data\n", "not /opt/data itself"),
        (
            "record_stores:\n  - name: a\n    path: /opt/data/profiles/agent-crane/cron\n",
            "owned by the runtime",
        ),
        ("record_stores:\n  - name: a\n    path: /opt/data/.smd/x\n", "owned by the runtime"),
        ("record_stores:\n  - name: a\n    path: /opt/data/x/../y\n", "normalized"),
        (
            "record_stores:\n  - name: a\n    path: /opt/data/x\n    mode: rw\n",
            "unknown key",
        ),
        (
            "record_stores:\n  - name: a\n    path: /opt/data/x\n  - name: a\n    path: /opt/data/y\n",
            "authored twice",
        ),
    ],
)
def test_a_store_cannot_be_authored_past_its_own_directory(
    tmp_path: Path, block: str, fragment: str
) -> None:
    errors = _validate(tmp_path, block)
    assert errors, "expected a record_stores error"
    assert any(fragment in e for e in errors), errors
