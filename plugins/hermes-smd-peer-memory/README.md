# hermes-smd-peer-memory

Per-peer working-preference memory — the **learned lane** of the Operator
relationship model (ADR 0048). The Operator's personality is its relationships,
applied: for each colleague it works with, a separate memory of how *that*
person likes to work with it, captured from the content of their requests on any
channel and surfaced before each turn.

Hermes' native `memory` tool is per-profile and identity-blind (one
`MEMORY.md`/`USER.md`, no per-peer keying, no capture nudge). This plugin builds
the per-peer layer on top of it — zero changes to Hermes core.

## How it works

| Hook | Role |
| --- | --- |
| `pre_llm_call` | Carries the only per-peer id Hermes threads (`sender_id`). Stashes it by `session_id`, and injects that peer's active preferences as turn context. |
| `post_tool_call` | When the agent called `record_peer_preference`, resolves the peer from the stash (server-side — the agent never names them), checks the session taint-gate, and writes the row. |
| `on_session_end` | Evicts the session's stashed sender. |

Capture is **explicit**, via the agent-callable `record_peer_preference` tool:
the agent records a concrete *stated* or *demonstrated* preference. There is no
inference path — trait/psychological labelling is rejected by construction (no
trait column; `source` is the only provenance). This is the line that keeps the
learned lane from becoming Honcho-by-the-back-door.

## Trust contract (ADR 0048)

- **Stated or demonstrated only. Never a trait label.** "Wants bullet
  summaries" ✓. "Is impatient" ✗.
- **Server-side attribution.** The peer is the turn's sender, resolved from the
  stash; the agent cannot record a preference for someone else.
- **Taint-aware.** A session fed untrusted content cannot write a preference —
  injected instructions must not plant durable per-peer memory. Reads/drafts on
  a tainted turn are unaffected.
- **Reversible + inspectable.** Recency wins (an identical restatement
  supersedes the prior copy via `superseded_by`); Captain reads and overrides
  through the admin Learned lane (ss-console `memory_export?table=peer_preferences`).

## Storage

`peer_preferences` on the per-customer **agent-state** D1 binding
(`SMD_D1_AGENT_STATE_BINDING`, falling back to `SMD_D1_AUDIT_BINDING`) — the
same hermes-writable file as `agent_skills_inventory`, created idempotently at
register time. Not the broker-owned audit ledger; not the Honcho mirror. See
`schemas.py` for the table.

## Authored seed

The **authored** relationship lane (ADR 0048 — `customer.yaml relationship:` →
`SOUL.md`) is the seed: per-person preferences written for the engagement. This
plugin writes the same *kind* of per-person preference, captured live. Two
sources, one model.
