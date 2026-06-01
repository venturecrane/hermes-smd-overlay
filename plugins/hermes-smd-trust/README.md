# hermes-smd-trust

Content-class trust ceiling enforcement.

## Hooks

| Hook | Firing site (Hermes v2026.5.16) | Behavior |
|---|---|---|
| `pre_tool_call` | `model_tools.py:778` (via `get_pre_tool_call_block_message` at `hermes_cli/plugins.py:1396`) | Returns `{"action": "block", "message": "..."}` to refuse a tool that exceeds the per-customer trust ceiling. |

## Status

Stub. Real implementation ports from `ss-console/operator/adapter/trust_ceiling.py` in §7 of the build plan.

## Trust ceilings (planned, not yet implemented)

Three content classes per ADR 0005 (reviewer-as-sender):

- `autonomous` — agent sends/posts/files directly.
- `draft-for-review` — agent prepares the artifact in a customer-visible queue; reviewer-as-sender pattern handles final dispatch.
- `refused` — agent does not produce the artifact.

The ceiling for each tool is derived from `customer.yaml.scope` at provisioning and stored alongside the per-profile config.
