# hermes-smd-audit

Per-tool and per-LLM-call audit emission for SMD AI Employee customer Machines.

## Hooks

| Hook | Firing site (Hermes v2026.5.16) | Emits |
|---|---|---|
| `post_tool_call` | `model_tools.py:826-836` | One row per tool invocation with `duration_ms`. |
| `post_llm_call` | `run_agent.py:15901-15910` | One row per completed turn (does NOT fire on interrupted turns). |

## Status

Stub. Real implementation ports from `ss-console/operator/adapter/audit_log.py` in §7 of the build plan.

## Env requirements

- `SMD_CUSTOMER_SLUG` — per-customer namespace identifier.
- `SMD_D1_AUDIT_BINDING` — Cloudflare D1 binding name for the audit_log database.

## Notes

- Interrupted turns are not captured by `post_llm_call`. The `on_session_end` hook (handled by `hermes-smd-memory-mirror`) covers that case with `interrupted=True`. Cross-correlate via `session_id`.
- `transform_tool_result` fires after `post_tool_call`; audit is observer-only and does not interact with that seam.
- GEPA-related action types (ADR 0018, superseded) are not carried forward.
