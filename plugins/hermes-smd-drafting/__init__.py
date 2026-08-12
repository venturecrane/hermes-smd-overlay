"""The drafting lane's declared exit (ss ADR 0083, ss-console #2094).

WHY A TOOL EXISTS AT ALL, when a gate would be cheaper
------------------------------------------------------
``spec_gate`` can refuse an output that was composed without reading its class's
authored spec — but only where it can tell which class the output belongs to.
For a SEND that works, because the recipient is in the tool call and the trust
decision already resolved it. For an internal artifact it does not, and probing
established that it cannot (ss-console ``vfy_01KYZF6CYFRQ9SJDWQF0FDNX7W``):
``pre_tool_call`` carries ``tool_name, args, task_id, session_id,
tool_call_id`` and nothing about what is being produced. ``create_memo``
carrying a demand letter is byte-indistinguishable, at the gate, from the same
call carrying a chronology row, and ``work_product`` and ``record`` share every
seam they deliver through.

Two ways out of that. Bind the gate to EVERY internal write on a seat that
declares a firm-voice class — which reaches 38 of the 51 skills, fires for
``record`` whose voice provenance is ``none``, and turns a missing spec into a
seat that cannot write anything. Or stop inferring the class and let the caller
declare it, by giving the drafting lane its own exit. This is that exit. The
class is no longer guessed from a generic write; it is a property of which tool
was called.

WHAT THIS TOOL DOES NOT DO, and why
-----------------------------------
It does not perform the write. The mediated-tool precedent in this overlay
(``hermes-smd-workspace``) reaches its backend through a dedicated broker
socket; Smokeball has no such broker — it is an MCP server — so a tool that
delivered would have to reimplement a connector that already exists. So this
authorizes and the skill writes: the gate decision happens here, at the moment
the lane declares what it has, and the existing connector performs the seam.

THE LIMIT, STATED RATHER THAN BURIED
------------------------------------
A drafter could skip this tool and call ``mcp_smokeball_create_memo`` directly,
delivering an ungated artifact. That path is visible in the audit log and is not
prevented here.

That is the right bar, and the reasoning is worth keeping next to the code. The
failure this gate exists to catch is a model that FORGOT to read the spec, not
an adversary routing around a control. The adversary case in this subsystem —
an agent-writable spec acting as a persistent, untainted, self-authored
injection channel — is already closed, by root ownership of the spec tree
(#2084) rather than by anything here. Holding this row to an anti-adversary bar
is what made it look unclosable; holding it to the real one makes it a tool.

WHAT IT REFUSES ON
------------------
Whatever ``check_spec_gate`` refuses on for the named class: the voice spec not
read this turn, an authored format assertion the body breaks, or an evaluation
fault. All three fail toward NOT DELIVERING. A refusal returns the gate's own
message, so the model is told which rule it broke and what to do, rather than
being handed a bare error it will retry verbatim.

WHAT IT DOES NOT VALIDATE
-------------------------
The record. Zero-invention, citation, privilege hold-out and marker integrity
belong to the drafting discipline's ten mechanical gates
(``operator/templates/drafting/drafting_gate_check.py`` in ss-console), which
run against the draft with its sources in reach. This tool has the body and the
class and nothing else, and a second, weaker authority over those rules is the
drift the output-class registry exists to end.
"""

from __future__ import annotations

import logging
from typing import Any

from shared.tool_registration import register_wrapped_tool

logger = logging.getLogger(__name__)

#: Classes this tool may be asked to deliver. Deliberately NOT the whole
#: registry: the four outbound classes resolve from their recipient at the send
#: site with better evidence than any caller's assertion, and letting a caller
#: name one here would create a second, weaker resolver for a question already
#: answered. Only the two classes nothing can derive are declarable.
DELIVERABLE_CLASSES = frozenset({"work_product", "record"})

_TOOL_NAME = "smd_deliver_draft"

_DESCRIPTION = (
    "Deliver a completed internal draft. Call this INSTEAD of writing the draft "
    "straight to a memo, file, or task: it checks the draft against the authored "
    "spec for its output class and tells you whether it may be delivered. On "
    "'authorized' write the body to the seam you named. On 'refused' do NOT "
    "write it anywhere — fix what the message says and call this again."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output_class": {
            "type": "string",
            "enum": sorted(DELIVERABLE_CLASSES),
            "description": (
                "What this artifact IS. 'work_product' for attorney work product "
                "drafted for review (a demand letter, discovery responses, a "
                "brief). 'record' for the internal record consumed as data (a "
                "chronology row, a ledger line, a receipt log)."
            ),
        },
        "body": {
            "type": "string",
            "description": "The complete draft text, exactly as it will be written.",
        },
        "seam": {
            "type": "string",
            "description": (
                "Where it will be written: smokeball_memo, smokeball_task, "
                "smokeball_file, or session_output."
            ),
        },
    },
    "required": ["output_class", "body"],
}


def _handle(args: dict[str, Any], **kwargs: Any) -> str:
    """Run the spec gate for a declared internal-artifact class.

    Returns a human-readable line the model acts on. A refusal is a RETURN, not
    a raise: the gate's message names the rule broken and the remedy, and an
    exception would strip that down to a stack trace the model retries blind.
    """
    output_class = str(args.get("output_class") or "").strip()
    body = args.get("body")
    seam = str(args.get("seam") or "").strip() or "(unnamed seam)"

    if output_class not in DELIVERABLE_CLASSES:
        return (
            f"Refused: '{output_class}' is not a class this tool delivers. Use one of "
            f"{sorted(DELIVERABLE_CLASSES)}. Outbound classes resolve from their "
            "recipient at the send site and are never declared here."
        )
    if not isinstance(body, str) or not body.strip():
        return "Refused: body is empty. There is nothing to deliver."

    session_id = _resolve_session(kwargs)

    # ONE implementation of the gate, reached through the only seam two plugins
    # share. Hyphenated plugin dirs are not dotted module paths, so this cannot
    # import the trust plugin — which is exactly why `check_spec_gate` lives in
    # `shared/` rather than beside its first caller. A second implementation
    # here would be a second authority over one decision, and the two would
    # disagree the first time either changed.
    from shared.spec_gate import check_spec_gate

    block = check_spec_gate(
        tool_name=_TOOL_NAME,
        action_class_value="",  # no recipient to resolve; the class is declared below
        session_id=session_id,
        body=body,
        output_class=output_class,
    )

    if block is not None:
        message = block.get("message") or "Refused by the authored-spec gate."
        logger.info(
            "smd_deliver_draft REFUSED class=%s seam=%s chars=%d",
            output_class,
            seam,
            len(body),
        )
        return str(message)

    logger.info(
        "smd_deliver_draft AUTHORIZED class=%s seam=%s chars=%d",
        output_class,
        seam,
        len(body),
    )
    return (
        f"Authorized: this '{output_class}' draft satisfies the authored spec for its "
        f"class. Write it to {seam} now, unchanged. Editing the body after this point "
        "delivers something the gate did not see."
    )


def _resolve_session(kwargs: dict[str, Any]) -> str:
    """Resolve the session id under the SAME key the read marks were set under.

    Overlay #141: core drops ``session_id`` at some tool fire sites and passes
    only ``task_id``. ``shared.provenance.resolve_session`` is the one place that
    reconciles them, and the spec read register is keyed by its answer — so
    consulting it by any other key would look up an empty set and refuse a turn
    that did read.

    A non-keyed resolution is LOGGED (ss-console #2288). This lane refuses on a
    missing spec mark and writes no audit row of its own, so without the line an
    operator seeing "you did not read the spec" on a turn that did read has
    nothing to look at. The gated tool path records the same fact on its per-tool
    row as ``session_resolution``.
    """
    try:
        from shared import provenance

        resolved, mode = provenance.resolve_session_with_mode(str(kwargs.get("session_id") or ""))
        if mode != provenance.MODE_KEYED:
            logger.info("smd_deliver_draft session resolved by %s", mode)
        return resolved
    except Exception:  # noqa: BLE001 — an unresolvable session is not a delivery fault
        logger.debug("smd_deliver_draft: session resolution failed", exc_info=True)
        return str(kwargs.get("session_id") or "")


def register(ctx: Any) -> None:
    """Register the drafting lane's declared exit."""
    register_wrapped_tool(
        ctx,
        name=_TOOL_NAME,
        toolset="drafting",
        schema=_SCHEMA,
        handler=_handle,
        description=_DESCRIPTION,
        emoji="",
    )
    logger.info("hermes-smd-drafting registered: %s", _TOOL_NAME)
