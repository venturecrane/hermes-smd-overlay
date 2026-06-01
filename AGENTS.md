# AGENTS.md

Development notes for AI coding agents working in this repo.

## What this repo is

Plugin overlay for `NousResearch/hermes-agent`. Six plugins (five production + one probe) plus a bootstrap CLI. See [README.md](README.md) for the catalog.

## Hard rules

1. **Never modify Hermes core.** Per Teknium's May 2026 plugin policy (enforced via Hermes PR #5295), plugins MUST NOT modify core files. This overlay is plugin code only. If a feature appears to require core modification, file an issue first; we propose the change upstream, not vendor it locally.

2. **Hermes core APIs are the contract.** Plugins attach to documented hooks (`pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `transform_tool_result`, `on_session_end`). All six are verified at the pinned ref in [`docs/hook-surface.md`](docs/hook-surface.md). If a hook's behavior appears to drift, run the probe (`plugins/hermes-smd-hook-probe/`) against the new ref before changing plugin code.

3. **Plugin callbacks must be exception-safe.** Hermes' dispatcher wraps each callback in try/except, but a noisy callback creates log spam. Catch and log inside the plugin; never raise out of a hook.

4. **No secrets in code.** Credentials come from env vars (per the `requires_env` field in each `plugin.yaml`). The `shared/secrets.py` module gates access; never read from files outside that module.

5. **Per-customer isolation is at the Machine boundary.** This code assumes a single tenant per process. Do not add multi-tenant routing inside plugins; that lives in customer.yaml and is materialized at provisioning time.

## Layout

```
plugins/<name>/        — one directory per plugin; each has plugin.yaml + __init__.py
shared/                — modules imported by plugins (not registered as a plugin itself)
bootstrap/             — `hermes-smd` CLI (customer.yaml → profile config translation)
docs/                  — design docs and hook citations
tests/                 — pytest suite; one file per plugin
.github/workflows/     — CI (lint + pytest + probe smoke)
```

## Adding a plugin

1. Create `plugins/<plugin-name>/` with `plugin.yaml` + `__init__.py`.
2. `plugin.yaml` MUST declare `name`, `version`, `description`, `requires_env`, `hooks`.
3. `__init__.py` MUST expose `def register(ctx) -> None` and use `ctx.register_hook(name, callback)` for every hook in the manifest.
4. Add a test file at `tests/test_<plugin>.py`. At minimum, import the plugin and assert `register` is callable.
5. Update the catalog table in [README.md](README.md).

## Adding a hook attachment to an existing plugin

1. Verify the hook exists at the pinned Hermes ref. Add a citation to [`docs/hook-surface.md`](docs/hook-surface.md) if it isn't already there.
2. Add the hook name to the plugin's `plugin.yaml` `hooks:` list.
3. Wire it in `register(ctx)`.
4. Add a test case covering the new attachment.

## Verifying against a new Hermes pin

1. Bump the pinned ref in [`docs/hook-surface.md`](docs/hook-surface.md).
2. Re-run the citation grep:
   ```bash
   grep -rn 'invoke_hook(' --include="*.py" /path/to/hermes-agent | grep -v "^tests/"
   ```
3. Update file:line references that have moved.
4. Run the probe against a stock Hermes container at the new ref.
5. If any hook is missing or has shifted semantics, branch per the parent plan: (a) revise plugin design, (b) propose upstream contribution, (c) accept degraded feature.

## What lives elsewhere

- **SMD admin backend** (audit dashboards, voice training UI, customer console) lives in private `venturecrane/ss-console`. The plugins here emit data that backend consumes via HTTP/D1.
- **customer.yaml authoring + validation** lives in `ss-console/src/lib/operator/customer-yaml/`. The `bootstrap/translate.py` here re-implements the materialization to per-profile config.yaml, not the validation.
- **Skill catalog** lives in `ss-console/operator/skills/`. Published into Machine images at build time; not part of this overlay.
- **Hermes fork pin** is `venturecrane/hermes-agent`. No patches; tag-promotion scheme documented in `ss-console/docs/adr/0015-hermes-fork-vs-upstream.md`.

## Code style

- Python 3.11+. Type hints on public functions. No `from __future__ import annotations` unless a forward ref requires it.
- `ruff` for lint and format. CI fails on warnings.
- Standard library + Hermes plugin API only in `plugins/*/__init__.py`. Heavier dependencies (sqlite, httpx, anthropic) belong in module files imported by the plugin's register.
- No `print()` in plugin code. Use `logger = logging.getLogger(__name__)`.

## Commit conventions

Conventional Commits with venture scope omitted (this repo is single-venture):

```
feat(audit): emit per-tool D1 rows
fix(trust): handle missing customer slug
docs: cite on_session_end firing sites
```
