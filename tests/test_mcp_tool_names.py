"""The v0.19 MCP rename must not un-map a single policy table (ss-console#2444).

WHAT THIS GUARDS. Hermes v0.19 renamed MCP tools ``mcp_<server>_<tool>`` ->
``mcp__<server>__<tool>``. The overlay classifies by exact name and fails
CLOSED, so on the first v0.20.4 seat every connector tool was refused and the
agent told the firm "all connector tools are failing closed"
(hermes-pilot-smokeball, 2026-08-20, vfy_01M0E9XW8MBR2G1P9XK81K5B34). These
tests drive the WIRE form through the same entry points the runtime uses.

WHY IT CAN FAIL. Every assertion below is false against the pre-fix tree: the
canonical helper did not exist, the fan-out passed ctx straight through, and
``classify_tool("mcp__smokeball__list_matters")`` returned REFUSED/unmapped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import __init__ as umbrella  # noqa: E402  (the fan-out module under test)
from shared.action_classes import ActionClass, BannedToolError, classify_tool  # noqa: E402
from shared.matter_binding import is_content_read  # noqa: E402
from shared.mcp_tool_names import canonical_tool_name, is_wire_mcp_name  # noqa: E402
from shared.outbound_recipient import DIRECT_TO_SEND_TOOLS  # noqa: E402


class TestCanonicalToolName:
    @pytest.mark.parametrize(
        ("wire", "canonical"),
        [
            ("mcp__smokeball__list_matters", "mcp_smokeball_list_matters"),
            ("mcp__agentmail__send_message", "mcp_agentmail_send_message"),
            # Server component carrying its own underscore (the msgraph-mail
            # connector sanitizes to ``msgraph_mail``) — the FIRST ``__`` is
            # the boundary, so the server survives intact.
            ("mcp__msgraph_mail__send_message", "mcp_msgraph_mail_send_message"),
            # Tool component carrying an underscore.
            ("mcp__smokeball__get_memos_on_matter", "mcp_smokeball_get_memos_on_matter"),
        ],
    )
    def test_wire_names_rewrite_to_the_tables_spelling(self, wire, canonical):
        assert canonical_tool_name(wire) == canonical
        assert is_wire_mcp_name(wire) is True

    @pytest.mark.parametrize(
        "unchanged",
        [
            "mcp_smokeball_list_matters",  # already canonical (pre-v0.19 seat)
            "terminal",
            "execute_code",
            "read_file",
            "workspace_gmail_send",
            "",
        ],
    )
    def test_non_wire_names_pass_through_untouched(self, unchanged):
        assert canonical_tool_name(unchanged) == unchanged
        assert is_wire_mcp_name(unchanged) is False

    def test_none_ish_input_does_not_raise(self):
        assert canonical_tool_name(None) is None  # type: ignore[arg-type]


class TestPolicyTablesSeeTheCanonicalName:
    """The tables themselves, driven with the wire spelling."""

    def test_read_tool_classifies_instead_of_refusing(self):
        wire = "mcp__smokeball__list_matters"
        assert classify_tool(wire).unmapped is True  # raw name: still unknown
        result = classify_tool(canonical_tool_name(wire))
        assert result.unmapped is False
        assert result.action_class is not ActionClass.REFUSED

    def test_external_send_still_classifies_as_external_send(self):
        result = classify_tool(canonical_tool_name("mcp__agentmail__send_message"))
        assert result.action_class is ActionClass.EXTERNAL_SEND
        assert "mcp_agentmail_send_message" in DIRECT_TO_SEND_TOOLS

    def test_destructive_tool_is_still_BANNED_not_merely_unmapped(self):
        """The rename must not downgrade a banned tool to 'unknown'.

        Both outcomes block the call, but only the ban carries the categorical
        reason into the audit row — losing it would make a funds-touching tool
        look like an unrecognized read in the ledger.
        """
        with pytest.raises(BannedToolError) as excinfo:
            classify_tool(canonical_tool_name("mcp__smokeball__create_transaction"))
        assert excinfo.value.reason == "banned_tool_destructive"

    def test_matter_binding_still_sees_a_content_read(self):
        assert is_content_read(canonical_tool_name("mcp__smokeball__read_document"))
        assert is_content_read(canonical_tool_name("mcp__smokeball__get_memos_on_matter"))


class _FakeCtx:
    """Minimal PluginContext stand-in: records what got registered."""

    def __init__(self):
        self.registered: list[tuple[str, object]] = []
        self.other_calls: list[str] = []

    def register_hook(self, hook_name, callback):
        self.registered.append((hook_name, callback))
        return object()

    def register_system_prompt_section(self, *a, **k):
        self.other_calls.append("register_system_prompt_section")
        return object()


class TestFanOutBoundary:
    """The translation happens once, at the fan-out, for tool hooks only."""

    def _wrap(self):
        return umbrella._CanonicalizingCtx(_FakeCtx())

    def test_tool_hooks_receive_the_canonical_name_and_the_wire_name(self):
        seen: dict = {}

        def cb(**kwargs):
            seen.update(kwargs)
            return "sentinel-return"

        ctx = self._wrap()
        ctx.register_hook("pre_tool_call", cb)
        wrapped = ctx._inner.registered[0][1]
        out = wrapped(tool_name="mcp__smokeball__list_matters", args={})

        assert seen["tool_name"] == "mcp_smokeball_list_matters"
        assert seen["tool_name_wire"] == "mcp__smokeball__list_matters"
        # transform_tool_result depends on the return value surviving.
        assert out == "sentinel-return"

    def test_non_tool_hooks_are_not_wrapped(self):
        def cb(**kwargs):
            return None

        ctx = self._wrap()
        ctx.register_hook("pre_llm_call", cb)
        assert ctx._inner.registered[0][1] is cb

    def test_a_pre_v019_name_is_left_alone_and_gains_no_wire_key(self):
        seen: dict = {}

        def cb(**kwargs):
            seen.update(kwargs)

        ctx = self._wrap()
        ctx.register_hook("post_tool_call", cb)
        ctx._inner.registered[0][1](tool_name="mcp_smokeball_list_matters")

        assert seen["tool_name"] == "mcp_smokeball_list_matters"
        assert "tool_name_wire" not in seen

    def test_core_tool_names_are_untouched(self):
        seen: dict = {}

        def cb(**kwargs):
            seen.update(kwargs)

        ctx = self._wrap()
        ctx.register_hook("transform_tool_result", cb)
        ctx._inner.registered[0][1](tool_name="terminal", result="x")
        assert seen["tool_name"] == "terminal"

    def test_the_proxy_forwards_everything_else_untouched(self):
        ctx = self._wrap()
        ctx.register_system_prompt_section("body")
        assert ctx._inner.other_calls == ["register_system_prompt_section"]

    def test_a_callback_that_raises_still_raises_through_the_wrapper(self):
        """The wrapper guards its own rewrite, never the plugin's behavior."""

        def cb(**kwargs):
            raise RuntimeError("plugin said no")

        ctx = self._wrap()
        ctx.register_hook("pre_tool_call", cb)
        with pytest.raises(RuntimeError, match="plugin said no"):
            ctx._inner.registered[0][1](tool_name="mcp__smokeball__list_matters")
