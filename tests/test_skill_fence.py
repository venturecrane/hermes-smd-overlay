"""The omitted-skill read fence (hermes-smd-initiation/skill_fence.py).

A seat's skill set is what its live customer.yaml enables; the image carries
every skill. These pin that no file-reading tool can run an omitted skill by
reading it, that an enabled one still reads, and that a broken config refuses.
"""

from __future__ import annotations

import pytest

from shared.customer_config import CustomerConfig
from tests.conftest import load_plugin

ENABLED = "matter-inbox-router"
OMITTED = "demand-letter-drafter"

_YAML = f"""
customer_id: acme
vertical: law-firm
personas:
  - slug: operator
    skills:
      - name: {ENABLED}
        enabled: true
      - name: medical-chronology-maintainer
        enabled: true
      - name: discovery-response-drafter
        enabled: false
"""


@pytest.fixture
def fence(monkeypatch, tmp_path):
    path = tmp_path / "customer.yaml"
    path.write_text(_YAML)
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(path))
    return load_plugin("hermes-smd-initiation").skill_fence, path


def _call(mod, tool, args):
    return mod.on_pre_tool_call(tool_name=tool, args=args, session_id="s")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_file", {"path": f"/app/skills/{OMITTED}/SKILL.md"}),
        ("read_file", {"path": f"/opt/data/skills/{OMITTED}/references/skeleton.md"}),
        ("read_file", {"path": f"/app/skills/{ENABLED}/../{OMITTED}/SKILL.md"}),
        ("read_file", {"path": f"/app//skills/./{OMITTED}/SKILL.md"}),
        ("search_files", {"pattern": "demand", "path": f"/app/skills/{OMITTED}"}),
        ("search_files", {"pattern": "demand", "path": "/app/skills"}),
        ("skill_view", {"name": OMITTED}),
        ("skill_view", {"name": "discovery-response-drafter"}),
        ("terminal", {"command": f"cat /app/skills/{OMITTED}/SKILL.md"}),
        ("terminal", {"command": f"cd /app && cat skills/{OMITTED}/SKILL.md"}),
        ("execute_code", {"code": f"open('/opt/data/skills/{OMITTED}/SKILL.md').read()"}),
    ],
)
def test_an_omitted_skill_cannot_be_read(fence, tool, args) -> None:
    mod, _ = fence
    verdict = _call(mod, tool, args)
    assert verdict is not None and verdict["action"] == "block"
    assert verdict["message"].startswith("could not load")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_file", {"path": f"/app/skills/{ENABLED}/SKILL.md"}),
        ("read_file", {"path": f"/app/skills/{ENABLED}/references/routing-rubric.md"}),
        ("skill_view", {"name": ENABLED}),
        ("search_files", {"pattern": "x", "path": f"/opt/data/skills/{ENABLED}"}),
        ("read_file", {"path": "/tmp/notes.txt"}),
        ("terminal", {"command": "ls /var/lib"}),
        ("write_file", {"path": f"/app/skills/{OMITTED}/SKILL.md"}),
    ],
)
def test_an_enabled_skill_or_another_path_reads(fence, tool, args) -> None:
    mod, _ = fence
    assert _call(mod, tool, args) is None


def test_an_unreadable_config_enables_nothing(fence, monkeypatch) -> None:
    mod, _ = fence

    def boom(cls, p=None):
        raise OSError("gone")

    monkeypatch.setattr(CustomerConfig, "from_volume", classmethod(boom))
    verdict = _call(mod, "read_file", {"path": f"/app/skills/{ENABLED}/SKILL.md"})
    assert verdict is not None and verdict["action"] == "block"


def test_the_allowlist_is_read_live(fence) -> None:
    mod, path = fence
    assert _call(mod, "skill_view", {"name": OMITTED})["action"] == "block"
    path.write_text(_YAML + f"      - name: {OMITTED}\n        enabled: true\n")
    assert _call(mod, "skill_view", {"name": OMITTED}) is None


def test_the_plugin_registers_the_fence() -> None:
    class Ctx:
        def __init__(self) -> None:
            self.hooks: list[str] = []

        def register_tool(self, **_):
            pass

        def register_hook(self, name, fn):
            self.hooks.append(name)

    ctx = Ctx()
    load_plugin("hermes-smd-initiation").register(ctx)
    assert "pre_tool_call" in ctx.hooks
