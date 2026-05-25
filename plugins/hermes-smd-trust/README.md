# hermes-smd-trust

Content-class trust ceiling enforcement + Composio per-connection isolation guard.

## Hooks

| Hook | Firing site (Hermes v2026.5.16) | Behavior |
|---|---|---|
| `pre_tool_call` | `model_tools.py:778` (via `get_pre_tool_call_block_message` at `hermes_cli/plugins.py:1396`) | Returns `{"action": "block", "message": "..."}` to refuse a tool that exceeds the per-customer trust ceiling. |
| `transform_tool_result` | `model_tools.py:847-857` | Returns a replacement result string when a Composio response is missing or has a mismatched `connection_id`. |

## Status

Stub. Real implementation ports from `ss-console/ai-employee/adapter/trust_ceiling.py` and `ss-console/ai-employee/adapter/connectors/composio_assertion.py` in §7 of the build plan.

## Trust ceilings (planned, not yet implemented)

Three content classes per ADR 0005 (reviewer-as-sender):

- `autonomous` — agent sends/posts/files directly.
- `draft-for-review` — agent prepares the artifact in a customer-visible queue; reviewer-as-sender pattern handles final dispatch.
- `refused` — agent does not produce the artifact.

The ceiling for each tool is derived from `customer.yaml.scope` at provisioning and stored alongside the per-profile config.

## Composio guard

Composio uses a single API key per account. Per-customer isolation requires that every tool response carry the expected `connection_id` from `customer.yaml.connectors{}`. Missing or mismatched values trigger a replacement result that surfaces the violation to the audit log and refuses to feed the contaminated data back into the conversation.

The guard runs even when the Composio backend appears healthy — defense in depth against misconfiguration.
