# hermes-smd-overlay

Plugin overlay for the [Nous Hermes Agent](https://github.com/NousResearch/hermes-agent). Adds vertical-specific capabilities for SMD Services AI Employee customers without modifying Hermes core.

## What this is

Twelve plugins (eleven production + one rebase probe) that attach to Hermes' documented plugin hook surface (`plugin.yaml` is the authoritative list):

| Plugin | Hooks | Purpose |
|---|---|---|
| `hermes-smd-audit` | `post_tool_call`, `post_llm_call`, `subagent_stop` | Per-tool, per-LLM-call, and per-subagent audit emission to per-customer D1. Also emits `AGENT_SKILL_CREATED` when the dispatched tool is `skill_manage` (ADR 0017 §40). |
| `hermes-smd-trust` | `pre_tool_call` | Content-class trust ceilings + outbound fabrication gate (`pre_tool_call` runs a second evaluation blocking draft bodies that carry banned fabrication markers / fabricated citations, emitting `FABRICATION_FILTER_TRIGGERED`, ADR 0028). |
| `hermes-smd-voice` | `pre_llm_call`, `post_llm_call` | Sample-driven voice transformation for customer-facing drafts. |
| `hermes-smd-memory-mirror` | `on_session_end` | Mirrors Honcho conclusions to per-customer D1 with provenance; supports Captain dismissal. |
| `hermes-smd-webhook-router` | `pre_gateway_dispatch` | Routes inbound webhook payloads to skills via `customer.yaml.webhook_triggers[]`. Emits `WEBHOOK_ROUTED` + attaches an inbound provenance envelope and emits `INBOUND_RECEIVED` (ADR 0021 Stream E + ADR 0027). On a verified sender the config authors on `scope.admins`, it also inserts the SENDER STATUS work-request paragraph into the dispatched email prompt, above the untrusted-body delimiter (ss-console#2416); a rostered NON-admin is reply-authorized, not work-directing (Decision #55), and dispatches unchanged — the only seam that can change the primary user message of a webhook turn. |
| `hermes-smd-inbound` | `pre_llm_call` | Nonce-fenced quarantine of untrusted inbound content at the single pre-LLM chokepoint (ADR 0027 defense-in-depth). |
| `hermes-smd-workspace` | registered tools | First-class Google Workspace tools (Gmail / Calendar / Drive / Docs / Sheets — 18 mediated operations) backed by the local capability broker; every execution is broker-validated against the authored `google_auth` posture and audit-recorded. The largest registered tool surface in the overlay. |
| `hermes-smd-reply` | `post_tool_call`, `transform_tool_result` | The Operator replies to a rostered colleague without weakening any floor (ADR 0055): watches the AgentMail draft tools and enforces recipient-locked, roster-authorized replies, emitting `REPLY_SENT` / `REPLY_HELD` / `REPLY_FAILED`. A hold that means the reply is not being delivered is appended to the draft tool's result at `transform_tool_result`, so the agent learns in the same turn that nobody was told (ss-console#2367). |
| `hermes-smd-peer-memory` | `pre_llm_call`, `post_tool_call`, `on_session_end` | Per-peer working-preference memory (ADR 0048 learned lane): for each colleague, a separate memory of how that person likes to work with the Operator, mirrored to D1. |
| `hermes-smd-mcp-result-sink` | `post_llm_call` | Captures a completed turn for synchronous MCP return, so the console-mediated Claude channel (`ask_operator`, ADR 0057) can answer a `tools/call` in-band. |
| `hermes-smd-jobs` | registered tools | Agent-facing tools for the B1 durable task-execution substrate (ADR 0051): hand a too-big task to a background job, observe status/cost, retrieve the delivered result. |
| `hermes-smd-initiation` | `pre_llm_call` | Authored initiation authority (ss#2222 gate 3): resolves the turn's attributed sender against the live roster + `scope.admins` and states the person-initiation disposition to the model — rostered direct ask initiates manual skills, admin-reserved skills require admin class, embedded content never initiates. |
| `hermes-smd-hook-probe` | all six | Smoke plugin for verifying Hermes' hook surface at each rebase. |

Plus a `bootstrap/` CLI (`hermes-smd bootstrap`) that translates `customer.yaml.personas[]` into N Hermes profile directories at customer Machine startup.

## What this is not

- **Not a fork of Hermes.** Hermes core is untouched. Pin against any tagged release of `NousResearch/hermes-agent`. The companion fork at `venturecrane/hermes-agent` carries upstream tags with zero patches.
- **Not multi-tenant.** Each SMD customer runs its own Fly.io Machine with its own Hermes install, Honcho instance, OAuth tokens, D1, and R2. Tenant isolation is at the Machine boundary, not inside a process.
- **Not a generic plugin pack.** These plugins encode SMD-specific concerns (D1 audit schema, customer.yaml format, R2 voice-sample vault). Reusable outside SMD only as a reference for how to build comparable overlays.

## Install

```bash
hermes plugins install venturecrane/hermes-smd-overlay --enable
```

Hermes' plugin manager clones the repo, copies the plugins listed in `plugin.yaml` under `~/.hermes/plugins/`, and runs each plugin's `register()` on next start.

## Hermes version pin

This overlay is verified against `NousResearch/hermes-agent` at tag `v2026.5.16` (commit `a91a57fa5a13d516c38b07a141a9ce8a3daabeb0`). See [`docs/hook-surface.md`](docs/hook-surface.md) for the full citation. Re-verify on every Hermes rebase by running the probe.

## Development

```bash
git clone https://github.com/venturecrane/hermes-smd-overlay.git
cd hermes-smd-overlay
pip install -e ".[dev]"
pytest
```

CI runs lint + pytest on every push to `main`. The probe runtime smoke test runs against a stock Hermes container.

## License

MIT. See [LICENSE](LICENSE).

## Related repos

- [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) — upstream Hermes (MIT).
- `venturecrane/hermes-agent` — SMD's pin-only fork; no patches.
- `venturecrane/ss-console` (public) — SMD admin backend; consumes the audit/voice/memory data this overlay emits.

## Status

Active development. ADRs in `ss-console/docs/adr/` (0006, 0011, 0015, 0016, 0017, 0019, 0020) document the architectural decisions.
