"""A couples seat boots with read_file on email, fenced, and no write tools (ss-console #3264).

THE INCIDENT (captain-mark, 2026-10-18, the first couples seat to boot): the
activation gate died with "WEBHOOK TURNS CANNOT READ FILES". The couples
persona disabled Hermes' whole ``file`` toolset, and Hermes subtracts disabled
toolsets LAST (pinned v2026.9.14 ``model_tools._select_tool_names``), so
``read_file`` left the ``smd_webhook_read`` toolset too.

The Hermes resolution is modelled here faithfully (enable the platform's
toolsets, then subtract every disabled toolset's tools), because CI has no
Hermes. Each falsifier names the line whose removal makes it fail.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from shared import couples_surface, spec_manifest, webhook_read_surface
from shared.spec_status import SPEC_STATUS
from tests.conftest import load_plugin
from tests.test_space_translate import PEOPLE, VALID_YAML, _config

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "hermes-smd-trust"))

import spec_read  # noqa: E402

#: The toolsets at the pinned Hermes ref that matter here (toolsets.py:100-146),
#: plus the two the overlay creates at runtime.
TOOLSETS = {
    "file": ["read_file", "write_file", "patch", "search_files"],
    "terminal": ["terminal", "process_manage"],
    "code_execution": ["execute_code"],
    "delegation": ["delegate_task"],
    "skills": ["skills_list", "skill_view", "skill_manage"],
    "web": ["web_search", "web_extract"],
    "vision": ["vision_analyze"],
    "clarify": ["clarify"],
    "memory": ["memory"],
    webhook_read_surface.WEBHOOK_READ_TOOLSET: ["read_file"],
    couples_surface.COUPLES_NO_FILE_WRITES: list(couples_surface.COUPLES_NO_FILE_WRITES_TOOLS),
}
#: Hermes' default Telegram composite (core toolsets).
TELEGRAM_DEFAULT = ["file", "terminal", "code_execution", "delegation", "skills", "web", "memory"]


def resolve(config: dict, platform: str) -> set[str]:
    """Hermes' resolution: enable, then subtract every disabled toolset LAST."""
    if platform == "webhook":
        enabled = (config.get("platform_toolsets") or {}).get("webhook") or (
            webhook_read_surface.webhook_platform_toolsets()
        )
    else:
        enabled = TELEGRAM_DEFAULT
    tools = {t for ts in enabled for t in TOOLSETS.get(ts, [])}
    for ts in (config.get("agent") or {}).get("disabled_toolsets") or []:
        tools -= set(TOOLSETS.get(ts, []))  # an unknown toolset name is a no-op
    return tools


COUPLES = VALID_YAML + PEOPLE.replace(
    "product: hosted-agent", "product: hosted-agent\n  role: couples"
)


@pytest.fixture
def couples_config(tmp_path, monkeypatch):
    monkeypatch.setattr(webhook_read_surface, "resolve_platform_tool_names", resolve)
    monkeypatch.setattr(
        webhook_read_surface, "resolve_webhook_tool_names", lambda c: resolve(c, "webhook")
    )
    return _config(tmp_path, COUPLES)


def test_a_couples_seat_offers_read_file_on_email(couples_config):
    """FALSIFIER: put ``file`` back in _COUPLES_REFUSED_TOOLSETS (translate.py)."""
    webhook_read_surface.assert_read_file_on_webhook(couples_config)
    assert "read_file" in resolve(couples_config, "webhook")


def test_the_old_config_is_the_boot_failure(couples_config):
    """Pins the incident: with ``file`` disabled the gate refuses."""
    broken = json.loads(json.dumps(couples_config))
    broken["agent"]["disabled_toolsets"].append("file")
    with pytest.raises(webhook_read_surface.WebhookReadSurfaceError):
        webhook_read_surface.assert_read_file_on_webhook(broken)


def test_no_write_search_or_code_tool_on_any_couples_surface(couples_config):
    """FALSIFIER: drop smd_couples_no_file_writes from the couples toolsets."""
    assert couples_surface.is_couples_config(couples_config)
    couples_surface.assert_no_forbidden_tools(couples_config)
    for platform in couples_surface.CHECKED_PLATFORMS:
        offered = resolve(couples_config, platform)
        assert not set(couples_surface.FORBIDDEN_TOOLS) & offered, platform


def test_an_unregistered_toolset_refuses_the_boot(couples_config, monkeypatch):
    """If the runtime toolset never got created Hermes ignores the name and the
    write tools come back; the activation check must catch it."""
    monkeypatch.delitem(TOOLSETS, couples_surface.COUPLES_NO_FILE_WRITES)
    with pytest.raises(couples_surface.CouplesSurfaceError, match="write_file"):
        couples_surface.assert_no_forbidden_tools(couples_config)


def test_a_seat_that_is_not_couples_is_not_checked(tmp_path):
    assert not couples_surface.is_couples_config(_config(tmp_path, VALID_YAML))


def test_the_router_creates_the_toolset_at_load(monkeypatch):
    created: dict[str, list[str]] = {}
    fake = type(sys)("toolsets")
    fake.create_custom_toolset = lambda name, desc, tools=None, includes=None: created.update(
        {name: tools}
    )
    monkeypatch.setitem(sys.modules, "toolsets", fake)
    couples_surface.register_couples_toolset()
    assert created[couples_surface.COUPLES_NO_FILE_WRITES] == [
        "write_file",
        "patch",
        "search_files",
        "skill_manage",
    ]
    router = Path(__file__).parent.parent / "plugins" / "hermes-smd-webhook-router" / "__init__.py"
    assert "register_couples_toolset()" in router.read_text()


# ---------------------------------------------------------------------------
# What that read_file may open: the record-store path fence
# ---------------------------------------------------------------------------


@pytest.fixture
def fenced(tmp_path, monkeypatch):
    body = "Warm and brief.\n"
    rel = "classes/staff/voice.md"
    spec_dir = tmp_path / "spec"
    (spec_dir / rel).parent.mkdir(parents=True)
    (spec_dir / rel).write_text(body)
    (spec_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "customer": "captain-mark",
                "source_digest": "x",
                "specs": {
                    rel: {
                        "class": "staff",
                        "property": "voice",
                        "sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "bytes": len(body),
                    }
                },
            }
        )
    )
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(spec_dir))
    plugin = load_plugin("hermes-smd-record-store")
    monkeypatch.setattr(plugin.path_fence, "SKILL_ROOTS", (str(tmp_path / "skills"),))
    monkeypatch.setattr(plugin.path_fence, "_hermes_spillover", lambda: None)
    store = tmp_path / "couples" / "checkin"
    store.mkdir(parents=True)
    (store / "answer-2026-10-20-p1.md").write_text("---\nowner: p1\n---\nprivate\n")
    SPEC_STATUS._reset_for_tests()
    yield {"fence": plugin.path_fence, "store": store, "spec": spec_dir / rel, "tmp": tmp_path}
    SPEC_STATUS._reset_for_tests()


def test_partner_bs_email_turn_cannot_read_a_p1_record_by_path(fenced):
    """FALSIFIER: drop the store-first denial in path_fence._path_problem."""
    record = str(fenced["store"] / "answer-2026-10-20-p1.md")
    refusal = fenced["fence"].check(
        "read_file", {"path": record}, [str(fenced["store"])], {"session_id": "b"}
    )
    assert refusal == fenced["fence"].REFUSAL


@pytest.mark.parametrize(
    "path",
    ["memories/MEMORY.md", "state.db", "sessions/x.json", "logs/agent.log", "cron/output/j1/x.md"],
)
def test_nothing_else_on_the_volume_is_readable(fenced, path):
    target = str(fenced["tmp"] / path)
    assert (
        fenced["fence"].check("read_file", {"path": target}, [str(fenced["store"])], {}) is not None
    )


def test_the_spec_read_mark_still_sets(fenced):
    """The reason read_file is on the email surface at all."""
    args = {"path": str(fenced["spec"])}
    assert (
        fenced["fence"].check("read_file", args, [str(fenced["store"])], {"session_id": "b"})
        is None
    )
    spec_read.observe_read("read_file", args, "b")
    assert SPEC_STATUS.was_read("b", "staff", "voice")


@pytest.mark.parametrize("tool", ["write_file", "patch", "terminal", "execute_code"])
def test_the_fence_still_refuses_writes_and_code_if_one_were_offered(fenced, tool):
    args = {"path": str(fenced["tmp"] / "x.md"), "content": "x"}
    assert fenced["fence"].check(tool, args, [str(fenced["store"])], {}) is not None


def test_a_couples_seat_without_record_stores_refuses_to_boot(monkeypatch):
    """No store, no path fence, an unfenced read_file. FALSIFIER: drop
    assert_read_fence_armed from the activation check."""
    from shared import record_store

    monkeypatch.setattr(record_store, "authored_stores", lambda *a, **k: {})
    with pytest.raises(couples_surface.CouplesSurfaceError, match="record_stores"):
        couples_surface.assert_read_fence_armed()
    monkeypatch.setattr(record_store, "authored_stores", lambda *a, **k: {"lists": Path("/x")})
    couples_surface.assert_read_fence_armed()


def test_the_activation_gate_runs_the_couples_check():
    handler = Path(__file__).parent.parent / "hooks" / "smd-overlay-activation" / "handler.py"
    text = handler.read_text()
    assert "_couples_surface_check()" in text and "assert_read_fence_armed()" in text
