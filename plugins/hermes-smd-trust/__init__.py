"""hermes-smd-trust — content-class trust ceilings.

Attaches to one hook at the pinned Hermes ref (v2026.5.16):

- ``pre_tool_call`` (model_tools.py:778 via ``get_pre_tool_call_block_message``
  at hermes_cli/plugins.py:1396) — blocks tools that exceed the per-customer
  trust ceiling by returning ``{"action": "block", "message": "<reason>"}``.

Per AGENTS.md hard rule #3 the callback is exception-safe: a raise from
the policy module is caught at the hook boundary so a faulty plugin cannot
break the agent loop. Audit observation of refusals happens downstream via
the audit plugin's ``post_tool_call`` hook on the resulting error result;
this plugin does not cross-import the audit plugin.
"""

import logging
from typing import Any

from shared.secrets import get_secret

from . import enforce, outbound

logger = logging.getLogger(__name__)


def on_pre_tool_call(**kwargs: Any) -> dict | None:
    """Block a tool call that exceeds the per-customer trust ceiling.

    Returns ``{"action": "block", "message": "<reason>"}`` to refuse, or
    ``None`` to allow the call.

    FAIL CLOSED (issue #12): if the policy path raises unexpectedly, this
    hook returns a block directive rather than ``None``. Safety must not
    degrade to "allow" on error — a transient or config-induced fault in
    the trust path must never silently let a sensitive action through on
    the live per-customer Machine. ``evaluate_tool_call`` already handles
    its own resolution failures (allowing low-risk READs, refusing
    sensitive actions); this handler is the backstop for anything it
    cannot catch. The pre_tool_call helper's contract
    (hermes_cli/plugins.py:1428-1437) honors the block-directive shape.

    TWO evaluations run in this one callback (ADR 0028). First the
    trust-ceiling check (``evaluate_tool_call``). If — and ONLY if — that
    allows the call AND the tool is a body-bearing draft-creating tool, a
    SECOND evaluation runs: the outbound provenance gate
    (``outbound.check_outbound_draft``) scans the draft body for banned
    fabrication markers / fabricated citations and blocks before the draft
    tool executes. ``pre_tool_call`` is the only hook that can block via
    return value, and send tools are permanently banned by the ceiling
    layer, so "drafted OR sent" reduces to "drafted" here. The gate is a
    second check in the same callback, not a new plugin or hook.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, task_id, session_id, tool_call_id
    """
    try:
        tool_name = kwargs.get("tool_name") or ""
        args = kwargs.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        customer_slug = kwargs.get("customer_slug")
        if not isinstance(customer_slug, str) or not customer_slug:
            # Fall back to the env var the Machine boots with. The slug is
            # not load-bearing for the policy decision itself (the customer
            # ceiling resolves from customer.yaml / env), but downstream
            # audit observation expects it.
            try:
                customer_slug = get_secret("SMD_CUSTOMER_SLUG")
            except KeyError:
                customer_slug = ""

        ceiling_block = enforce.evaluate_tool_call(tool_name, args, customer_slug)
        if ceiling_block is not None:
            # The trust ceiling already refuses this call; no need to scan a
            # draft body that will never be written.
            return ceiling_block

        # Ceiling allowed the call. Run the outbound provenance gate as a
        # SECOND evaluation — it no-ops for non-draft tools and blocks a draft
        # whose body carries a banned fabrication marker / fabricated citation.
        return outbound.check_outbound_draft(
            tool_name=tool_name,
            args=args,
            session_id=kwargs.get("session_id") or "",
            tool_call_id=kwargs.get("tool_call_id") or "",
        )
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.exception(
            "hermes-smd-trust: pre_tool_call raised; FAILING CLOSED — blocking "
            "the tool call (safety: an indeterminate trust decision must not "
            "allow a sensitive action; issue #12)"
        )
        return {
            "action": "block",
            "message": (
                "Refused: trust-ceiling evaluation failed; failing closed "
                "(indeterminate decision blocked for safety)"
            ),
        }


def register(ctx) -> None:
    """Plugin entry point. Wires the pre_tool_call hook."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    logger.info("hermes-smd-trust registered: pre_tool_call")
