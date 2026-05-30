# Hermes Plugin Hook Surface

**Pinned ref:** `NousResearch/hermes-agent` at tag `v2026.5.16` (commit `a91a57fa5a13d516c38b07a141a9ce8a3daabeb0`)
**License:** MIT
**Verified:** 2026-05-24

This document captures the exact upstream surface that the four `hermes-smd-overlay` plugins (`hermes-smd-audit`, `hermes-smd-trust`, `hermes-smd-voice`, `hermes-smd-memory-mirror`) attach to. Every claim below is grounded in a file:line citation at the pinned ref. Re-verify at every Hermes rebase by re-running the citation grep and the `hermes-smd-hook-probe` smoke plugin.

## Registration API

Plugins are Python packages under `~/.hermes/plugins/<name>/` with a `plugin.yaml` manifest and an `__init__.py` that exposes a single entry point:

```python
def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    # ...
```

The `ctx` object is a `PluginContext` instance (`hermes_cli/plugins.py:287`). Its `register_hook(name, callback)` method (`hermes_cli/plugins.py:669`) appends the callback to the manager's internal hook list. Unknown hook names log a warning but are still stored — forward-compatible plugins do not break.

The canonical list of valid hook names lives at `hermes_cli/plugins.py:128-168` (`VALID_HOOKS` set). The six hooks the overlay depends on are all members.

The dispatcher is `PluginManager.invoke_hook(name, **kwargs)` at `hermes_cli/plugins.py:1264`. Each callback is wrapped in its own try/except — a misbehaving plugin cannot break the agent loop (`hermes_cli/plugins.py:1287-1297`). Non-`None` return values are collected and returned to the firing site; per-hook semantics determine how the list is interpreted.

Plugin manifest schema (verified against `plugins/observability/langfuse/plugin.yaml`):

```yaml
name: <plugin-name>
version: "<semver>"
description: "<one-line>"
author: <author>
requires_env:
  - <ENV_VAR>          # gates loading
hooks:
  - pre_tool_call      # advertised hook list (PluginManifest.provides_hooks)
  - post_tool_call
```

## Hook citations

### 1. `pre_tool_call`

**Purpose (overlay):** trust-ceiling enforcement (`hermes-smd-trust`). Blocks a tool before execution by returning `{"action": "block", "message": "<reason>"}`.

**Firing site:** `hermes_cli/plugins.py:1419-1426` (inside `get_pre_tool_call_block_message`, declared at line 1396). The helper is called once per tool execution from `model_tools.py:778`.

**Single-fire contract:** The helper is the only production path. `model_tools.py:763-789` documents the contract: "pre_tool_call fires exactly once per tool execution"; callers that already fired it pass `skip_pre_tool_call_hook=True` to avoid double-firing.

**Kwargs:**

| kwarg | type | source |
|---|---|---|
| `tool_name` | `str` | function being dispatched |
| `args` | `dict` | tool arguments (`{}` if non-dict) |
| `task_id` | `str` | per-task identifier (`""` when absent) |
| `session_id` | `str` | per-session identifier |
| `tool_call_id` | `str` | per-call identifier |

**Return-value semantics:** First callback returning `{"action": "block", "message": "<non-empty str>"}` wins; the tool short-circuits with a JSON error. Other return shapes are ignored — observer plugins remain safe. (`hermes_cli/plugins.py:1428-1437`)

### 2. `post_tool_call`

**Purpose (overlay):** audit emission (`hermes-smd-audit` — one D1 row per tool invocation with duration).

**Firing site:** `model_tools.py:826-836`.

**Ordering invariant:** Fires after `registry.dispatch()` returns. Always fires regardless of result (success or error). `transform_tool_result` fires immediately after `post_tool_call` on the same execution path; see hook #5 below.

**Kwargs:**

| kwarg | type | source |
|---|---|---|
| `tool_name` | `str` | function dispatched |
| `args` | `dict` | tool arguments |
| `result` | `str` | tool output (usually JSON) |
| `task_id` | `str` | task identifier |
| `session_id` | `str` | session identifier |
| `tool_call_id` | `str` | call identifier |
| `duration_ms` | `int` | `time.monotonic()` delta in milliseconds (`model_tools.py:823`) |

**Return-value semantics:** Observer only. Returns are collected but not interpreted by the firing site.

### 3. `pre_llm_call`

**Purpose (overlay):** voice-sample injection (`hermes-smd-voice` — adds per-customer voice samples to the user message before the model sees it) AND untrusted-inbound quarantine (`hermes-smd-inbound`, ADR 0027 — drains the per-session pending inbound register and injects each item wrapped in a nonce-fenced quarantine block). Both are observers that contribute injected context; returns are merged (no "first wins"), so the two plugins coexist on this hook. This is the SINGLE chokepoint for inbound quarantine — it also fires for skill-triggered LLM calls, so no per-skill duplication is needed.

**Firing site:** `run_agent.py:12447-12457`.

**Ordering invariant:** Fires once per turn, before the model API request. Context returned from plugins is injected into the user message, **not** the system prompt — this preserves prompt-cache prefix stability across turns (`hermes_cli/plugins.py:1278-1282`). All injected context is ephemeral and is not persisted to the session DB.

**Kwargs:**

| kwarg | type | source |
|---|---|---|
| `session_id` | `str` | session identifier |
| `user_message` | `str` | the user's input for this turn |
| `conversation_history` | `list` | full message list (shallow copy) |
| `is_first_turn` | `bool` | `True` when there is no prior history |
| `model` | `str` | model identifier |
| `platform` | `str` | `cli`, `tui`, gateway adapter, etc. |
| `sender_id` | `str` | user identifier (`""` when unavailable) |

**Return-value semantics:** Each callback may return `{"context": "<text>"}` or a plain string. Non-empty strings are joined with `\n\n` and injected into the user message (`run_agent.py:12458-12465`). All return values are merged (no "first wins" — every plugin contributes).

### 4. `post_llm_call`

**Purpose (overlay):** audit emission for the LLM call (`hermes-smd-audit` — per-turn LLM cost/timing row).

**Firing site:** `run_agent.py:15901-15910`.

**Ordering invariant:** Fires once per turn, after the tool-calling loop completes (`run_agent.py:15895-15898`). Only fires when `final_response` is non-empty AND the turn was not interrupted (`run_agent.py:15899`). Interrupted turns do not fire `post_llm_call`.

**Kwargs:**

| kwarg | type | source |
|---|---|---|
| `session_id` | `str` | session identifier |
| `user_message` | `str` | the turn's original user input |
| `assistant_response` | `str` | final response text |
| `conversation_history` | `list` | full message list |
| `model` | `str` | model identifier |
| `platform` | `str` | platform string |

**Return-value semantics:** Observer only. Returns collected but not interpreted.

### 5. `transform_tool_result`

**Purpose (overlay):** Composio per-connection isolation guard (`hermes-smd-trust` — refuses a Composio tool result that doesn't match the customer's expected `connection_id`).

**Firing site:** `model_tools.py:847-857`.

**Ordering invariant:** Fires **after** `post_tool_call` on the same execution path (`model_tools.py:840-846` documents the seam). Fires before the result is appended back into conversation context — a returned string replaces the result for the agent.

**Kwargs:** identical to `post_tool_call` (tool_name, args, result, task_id, session_id, tool_call_id, duration_ms).

**Return-value semantics:** Fail-open. The first callback returning a `str` wins and replaces the tool result. Non-string returns are ignored (`model_tools.py:858-859` — only `isinstance(hook_result, str)` is accepted).

### 6. `on_session_end`

**Purpose (overlay):** Honcho conclusion mirror trigger (`hermes-smd-memory-mirror` — poll Honcho for new conclusions and write them to per-customer D1 with provenance).

**Primary firing site:** `run_agent.py:16016-16024`.

**Safety-net firing site:** `cli.py:13831-13839` — only fires when an interrupted exit occurs while the agent is mid-turn (i.e., `run_conversation()`'s hook didn't fire because the turn was incomplete). `cli.py:13826-13828` documents the rule: "run_conversation() already fires this per-turn on normal completion".

**Firing cadence:** **Per-turn, not per-conversation.** `on_session_end` fires at the end of every `run_conversation()` call, which is once per agent turn — not once per chat session. The overlay's memory-mirror plugin runs on every turn; this is the correct cadence given Honcho's `writeFrequency: session` (per ADR 0016) which produces new conclusions at the per-turn boundary.

**Abnormal-exit risk:** Neither firing site handles `kill -9`, container OOM, or unhandled SIGSEGV. The plan §6 calls for a "periodic backup poller for sessions that end abnormally" — this risk justifies that work; the hook alone is not sufficient.

**Kwargs (primary site):**

| kwarg | type | source |
|---|---|---|
| `session_id` | `str` | session identifier |
| `completed` | `bool` | whether the turn finished cleanly |
| `interrupted` | `bool` | whether the turn was interrupted |
| `model` | `str` | model identifier |
| `platform` | `str` | platform string |

**Kwargs (safety-net site):** same shape; `completed=False`, `interrupted=True` always.

**Return-value semantics:** Observer only.

## Findings — no plan branches required

All six hooks the overlay design depends on exist in upstream Hermes at the pinned ref. No hook is missing; no hook is misordered relative to the overlay's needs.

- The single-fire contract on `pre_tool_call` is documented (`model_tools.py:763-789`) — the overlay's trust-ceiling enforcement runs once per tool execution.
- The post-tool seam (`post_tool_call` → `transform_tool_result`) is the correct location for audit + Composio guard.
- `pre_llm_call` writes into the user message (not the system prompt) — preserves prompt-cache stability.
- `post_llm_call` only fires on completed, non-interrupted turns. Interrupted turns will need to be captured by an alternative signal (e.g., `on_session_end` with `completed=False, interrupted=True`); the audit plugin can compensate.
- `on_session_end` fires per-turn, not per-conversation. The memory-mirror plugin's mirror cadence aligns naturally (Honcho's `writeFrequency: session` produces new conclusions at the same per-turn boundary).

The plan's three branch conditions ((a) revise plugin design, (b) propose upstream contribution, (c) accept degraded feature) are **not triggered**. Proceed to §4 (create the overlay repo) as planned.

## Re-verification at rebase

The set of valid hook names and their kwargs may evolve in future Hermes releases. The probe plugin (`hermes-smd-hook-probe`, scaffolded in §4 of the build plan) and this document are co-maintained:

1. On any pinned-ref bump in `customer.yaml.hermes_ref` or the Machine image build, re-run the grep that produced this document:

   ```bash
   grep -rn 'invoke_hook(' --include="*.py" /path/to/hermes-agent | \
     grep -v "^tests/"
   ```

   Verify file:line for each of the six hooks. If any have moved, update the citations.

2. Run the `hermes-smd-hook-probe` smoke plugin against a stock Hermes container at the new ref. Capture probe logs. Verify hook ordering matches the invariants documented above.

3. Update `VALID_HOOKS` reference (file:line) if upstream restructures the constant.

4. The CI assertion in the Machine image build (`§5` of the plan: "assert the installed Hermes commit SHA matches the pinned upstream commit SHA") guarantees that this document's pinned-ref citations remain accurate for any deployed Machine.

## Appendix — full `VALID_HOOKS` enumeration

For reference, the complete set of hook names Hermes accepts at the pinned ref (`hermes_cli/plugins.py:128-168`):

| Hook | Used by overlay? | Notes |
|---|---|---|
| `pre_tool_call` | yes | trust ceiling |
| `post_tool_call` | yes | audit |
| `transform_terminal_output` | no | terminal-output canonicalization (not relevant) |
| `transform_tool_result` | yes | Composio guard |
| `transform_llm_output` | no | vocabulary/personality transformation (potential future use) |
| `pre_llm_call` | yes | voice sample injection + inbound quarantine (ADR 0027) |
| `post_llm_call` | yes | LLM audit |
| `pre_api_request` | no | gateway-level API request (not relevant) |
| `post_api_request` | no | gateway-level API response (not relevant) |
| `on_session_start` | no | per-turn session-start (not currently used; available if needed) |
| `on_session_end` | yes | memory-mirror trigger |
| `on_session_finalize` | no | session-end finalization |
| `on_session_reset` | no | session reset |
| `subagent_stop` | no | subagent lifecycle |
| `pre_gateway_dispatch` | yes | webhook routing + inbound envelope attach (ADR 0021 Stream E / ADR 0027) |
| `pre_approval_request` | no | approval lifecycle observer |
| `post_approval_response` | no | approval lifecycle observer |

The "no" rows are documented for future agents — these hooks exist and are stable; the overlay can attach to them if a new feature requires it.
