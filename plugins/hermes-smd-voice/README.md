# hermes-smd-voice

Sample-driven voice transformation for SMD AI Employee customer drafts.

## Hooks

| Hook | Firing site (Hermes v2026.5.16) | Behavior |
|---|---|---|
| `pre_llm_call` | `run_agent.py:12447-12457` | Returns `{"context": "<sample block>"}` to inject customer voice samples into the user-message context. Preserves system-prompt cache. |
| `post_llm_call` | `run_agent.py:15901-15910` | Evaluates draft fidelity against samples. Mostly observational. |

## Status

Stub. Real implementation ports from `ss-console/operator/adapter/voice/` in §7 of the build plan.

## Approach

Sample-driven, not rule-based. The agent is shown examples of the customer's own writing (from `vaults/<slug>/voice/samples/` in R2) and matches the style. Rule-based "always use 'we' not 'I'" prescriptions tend to over-correct and read inauthentic; samples capture register, vocabulary, and rhythm holistically.

The companion `operator/voice-gate/` harness in `ss-console` blind-tests draft fidelity before launch (target: 80% reviewer-panel indistinguishability). This plugin is the runtime transformer; voice-gate is the pre-deployment evaluator. They share the *concept* of voice-fidelity-as-measurable but do not import each other.

## Env requirements

- `SMD_CUSTOMER_SLUG` — per-customer namespace identifier.
- `SMD_R2_VOICE_BINDING` — Cloudflare R2 binding for the voice samples vault.
