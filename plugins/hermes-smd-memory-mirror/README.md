# hermes-smd-memory-mirror

Mirror Honcho conclusions to per-customer D1 with provenance, plus Captain dismissal and TTL archival.

## Hooks

| Hook | Firing site (Hermes v2026.5.16) | Emits |
|---|---|---|
| `on_session_end` | `run_agent.py:16016-16024` | Fires per-turn at end of `run_conversation()`. Kwargs: `session_id, completed, interrupted, model, platform`. |
| `on_session_end` (safety net) | `cli.py:13831-13839` | Fires only on interrupted CLI exit while the agent was mid-turn (the `run_conversation` path did not fire). |

## Status

Stub. Real implementation ports from `ss-console/operator/adapter/memory/` in §7 of the build plan.

## Approach

Mirror-don't-gate per [ADR 0016](../../docs/adr/0016-honcho-disposition.md). Honcho is the live store; D1 holds a parallel record with provenance so Captain operates on it through the admin portal without standing between the agent and its working memory.

- **`mirror.py`** — polls new/changed Honcho conclusions and writes `persona_observations` rows with `source_message_ids`, `confidence`, `evidence_status`, and `mirrored_at`.
- **`dismiss.py`** — when Captain dismisses a conclusion in the admin portal, calls `DELETE /conclusions/{id}` against the local Honcho. Physical delete; works around Honcho upstream bug #658 (corrections don't propagate).
- **`archive.py`** — conclusions older than `archive_after_days` (default 180) move from Honcho into `persona_observations_archive` and are physically deleted from Honcho. Restorable from D1.

## Env requirements

- `SMD_CUSTOMER_SLUG` — per-customer namespace identifier.
- `SMD_D1_OBSERVATIONS_BINDING` — Cloudflare D1 binding name for the persona observations database.
- `HONCHO_BASE_URL` — local Honcho instance URL.
- `HONCHO_API_KEY` — Honcho auth token.

## Risk: abnormal session exit

Neither `on_session_end` firing site catches `kill -9`, OOM, or `SIGSEGV`. Conclusions generated during a turn that ends abnormally will not be mirrored on that turn boundary. The build plan §6 calls for a periodic backup poller for abnormal session-end recovery — out of scope for this stub, tracked as a follow-on.
