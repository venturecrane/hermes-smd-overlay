# hermes-smd-overlay

Plugin overlay for the [Nous Hermes Agent](https://github.com/NousResearch/hermes-agent). Adds vertical-specific capabilities for SMD Services AI Employee customers without modifying Hermes core.

## What this is

Five plugins that attach to Hermes' documented plugin hook surface:

| Plugin | Hooks | Purpose |
|---|---|---|
| `hermes-smd-audit` | `post_tool_call`, `post_llm_call`, `subagent_stop` | Per-tool, per-LLM-call, and per-subagent audit emission to per-customer D1. Also emits `AGENT_SKILL_CREATED` when the dispatched tool is `skill_manage` (ADR 0017 §40). |
| `hermes-smd-trust` | `pre_tool_call`, `transform_tool_result` | Content-class trust ceilings + Composio per-connection isolation guard. |
| `hermes-smd-voice` | `pre_llm_call`, `post_llm_call` | Sample-driven voice transformation for customer-facing drafts. |
| `hermes-smd-memory-mirror` | `on_session_end` | Mirrors Honcho conclusions to per-customer D1 with provenance; supports Captain dismissal. |
| `hermes-smd-webhook-router` | `pre_gateway_dispatch` | Routes inbound webhook payloads to skills via `customer.yaml.webhook_triggers[]`. Emits `WEBHOOK_ROUTED` audit rows (ADR 0021 Stream E). |
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

Hermes' plugin manager clones the repo, copies the five plugins under `~/.hermes/plugins/`, and runs each plugin's `register()` on next start.

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
- `venturecrane/ss-console` (private) — SMD admin backend; consumes the audit/voice/memory data this overlay emits.

## Status

Active development. ADRs in `ss-console/docs/adr/` (0006, 0011, 0015, 0016, 0017, 0019, 0020) document the architectural decisions.
