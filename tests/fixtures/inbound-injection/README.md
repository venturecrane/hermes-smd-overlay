# Inbound prompt-injection corpus

Ten synthetic client intake emails, each carrying a planted prompt injection
inside an otherwise legitimate personal-injury inquiry. Platform-level safety
QA, vertical-neutral.

**These are fixtures, not correspondence.** Every file carries a
`metadata.watermark` of `[SYNTHETIC FIXTURE — NOT A REAL MATTER]`. The people,
clinics, carriers, claim numbers, and phone numbers in them are invented, and
every address uses a reserved test domain. No client material lives in this
repo.

**The injection text is deliberate.** These bodies contain instructions aimed at
an agent — "ignore previous instructions", forged authorization claims, requests
to list existing clients, a spoofed forwarded note from a purported managing
partner. That is the point: they are the attack, held still so the boundary can
be tested against it. Nothing here is ever run past a model.

## Where they came from

Authored in `venturecrane/ss-console` at
`operator/adapter/tests/fixtures/inbound-injection/`, where they exercised that
repo's copy of the inbound boundary, `operator/adapter/inbound_envelope.py`.
ss-console#2772 deleted that module — nothing imported it, because the boundary
that actually runs on a seat is `shared/inbound.py` here. The corpus followed
the fence it tests.

## What consumes them

`tests/test_inbound_injection_corpus.py`, over two layers: the fence
(`shared/inbound.py`) must quarantine every body without mangling it, and the
trust gate (`plugins/hermes-smd-trust/enforce.py`) must refuse a send on the
turn that ingested one.

## Shape

| Field | Meaning |
| --- | --- |
| `metadata.fixture_id` | Matches the filename stem; asserted. |
| `metadata.edge_tags` | Always includes `prompt-injection`. |
| `metadata.injection_dimension` | The register the attack is dressed in. |
| `metadata.expected_behavior` | Prose: what a correct seat does with it. |
| `content.body` / `content.body_text` | The untrusted text the fence must hold. |

Adding a case: keep the watermark, keep the `prompt-injection` tag, name the new
dimension, and use a reserved test domain for every address. The harness picks
up any `edge-pi-*.json` in this directory with no code change.
