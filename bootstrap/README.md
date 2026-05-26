# bootstrap

`hermes-smd` CLI — translates `customer.yaml` into per-profile Hermes configuration and runs the R2 polling sidecar for non-structural updates.

The console script is wired in `pyproject.toml`:

```toml
[project.scripts]
hermes-smd = "bootstrap.cli:main"
```

After `pip install -e .` the `hermes-smd` binary is on `PATH`.

## Subcommands

### `hermes-smd bootstrap`

Structural translation path. Run at Machine boot and after any structural change.

```
hermes-smd bootstrap [--customer-yaml PATH] [--hermes-home PATH]
```

| Flag              | Default                                  | Purpose                                            |
| ----------------- | ---------------------------------------- | -------------------------------------------------- |
| `--customer-yaml` | `/opt/data/customer.yaml`                | Path to authored `customer.yaml` on the Fly volume |
| `--hermes-home`   | `$HERMES_HOME` env var, or `~/.hermes`   | Hermes home directory                              |

For each persona in `customer.yaml.personas[]` the command writes:

```
$HERMES_HOME/profiles/<persona-slug>/config.yaml
$HERMES_HOME/profiles/<persona-slug>/SOUL.md
$HERMES_HOME/profiles/<persona-slug>/MEMORY.md   # tombstone (ADR 0016)
$HERMES_HOME/profiles/<persona-slug>/USER.md     # tombstone (ADR 0016)
$HERMES_HOME/profiles/<persona-slug>/skill-bundles/<bundle-slug>.yaml   # one per bundle (ADR 0021 Stream D)
```

The Hermes-native multi-persona pattern is documented in ADR 0011; per-persona SOUL.md is what Hermes loads as identity at profile boot.

The `MEMORY.md` and `USER.md` files are tombstones — each contains only a single HTML comment block. Per ADR 0016, Honcho is the memory provider for SMD AI Employee profiles, and Hermes' local-file memory sources (`MEMORY.md`, `USER.md`) must NOT be loaded because they would double-process memory and create state divergence with Honcho. The tombstones pre-empt Hermes' default-template auto-creation at profile boot.

The generated `config.yaml` also carries an explicit `local_memory_files` block declaring both files disabled, so operators inspecting a profile see the intent without needing to open the tombstone files.

The `skill-bundles/` directory contains one YAML per entry in `customer.yaml.personas[<n>].bundles[]` (ADR 0021 Stream D). Each file matches the Hermes-native bundle shape (`slug`, `description`, `skills`, optional `instruction`); Hermes loads `~/.hermes/profiles/<slug>/skill-bundles/*.yaml` at profile boot and exposes each as a `/<bundle-slug>` slash command. Bundle files declared previously but removed from the current `customer.yaml` are deleted from disk so stale bundles do not accumulate. Personas with no `bundles[]` block do not get an empty `skill-bundles/` directory.

### `hermes-smd customer-sync`

Long-running sidecar. Polls R2 for non-structural updates and signals Hermes with SIGHUP when a reload-eligible diff is detected.

```
hermes-smd customer-sync --r2-bucket URL [--customer-yaml PATH] [--interval SECONDS]
```

| Flag              | Default                   | Purpose                                            |
| ----------------- | ------------------------- | -------------------------------------------------- |
| `--customer-yaml` | `/opt/data/customer.yaml` | On-disk path to keep in sync                       |
| `--r2-bucket`     | (required)                | R2 source identifier (URL or bucket reference)     |
| `--interval`      | `300`                     | Poll interval in seconds                           |

## Structural vs non-structural changes (ADR 0019)

The CLI splits responsibility along the structural/non-structural seam.

**Structural — require Captain re-provision via `bootstrap`:**

- Adding or removing a persona
- Swapping a connector backend (e.g. `mcp:` → `build:`)
- Adding or revoking an OAuth scope
- Changing the trust ceiling schema

The Machine restarts after `bootstrap` writes so Hermes re-reads identity and connector wiring from scratch.

**Non-structural — hot-reload via SIGHUP through `customer-sync`:**

- Tone tweaks
- Review thresholds
- Voice samples
- Skill pin bumps within the same catalog
- Content policy adjustments

The sidecar applies the diff in place and signals the Hermes process; no restart is required. Structural diffs detected by the sidecar are rejected with a logged warning.

## Status

The CLI plumbing (argument parsing, validation, logging, exit codes) is real today. The underlying translation and sync actions raise `NotImplementedError` until §7 of the build plan ports the logic from `ss-console/ai-employee/adapter/validate_customer_yaml.py` and `resolve_skill_pins.py`.

Exit codes:

| Code | Meaning                                                          |
| ---- | ---------------------------------------------------------------- |
| 0    | Success                                                          |
| 1    | Unexpected error (logged with traceback)                         |
| 2    | Argument validation failure                                      |
| 3    | Subcommand not yet implemented (expected pre-§7)                 |
| 130  | Interrupted (Ctrl-C)                                             |
