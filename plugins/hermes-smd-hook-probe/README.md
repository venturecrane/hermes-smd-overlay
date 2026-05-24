# hermes-smd-hook-probe

Runtime verification probe for the documented Hermes hook surface. Installs against a stock Hermes container, fires a test scenario, and emits structured JSON log lines that prove each documented hook fires in the expected order.

This is the belt-and-suspenders companion to the static-analysis citations in `docs/hook-surface.md`. If the probe runs and the expected log lines appear in the expected order against an unmodified Hermes container, the citations are load-bearing.

Unlike the four production sibling plugins (`hermes-smd-audit`, `hermes-smd-trust`, `hermes-smd-voice`, `hermes-smd-memory-mirror`) which are stubs pending §7 of the build plan, this probe is real. It runs as-is.

## Hooks attached

All six hooks the SMD overlay depends on, at Hermes pinned ref `v2026.5.16` (commit `a91a57fa5a13d516c38b07a141a9ce8a3daabeb0`).

| Hook | Firing site (Hermes v2026.5.16) | Kwargs |
|---|---|---|
| `pre_tool_call` | `hermes_cli/plugins.py:1419-1426` from `model_tools.py:778` | `tool_name, args, task_id, session_id, tool_call_id` |
| `post_tool_call` | `model_tools.py:826-836` | `tool_name, args, result, task_id, session_id, tool_call_id, duration_ms` |
| `pre_llm_call` | `run_agent.py:12447-12457` | `session_id, user_message, conversation_history, is_first_turn, model, platform, sender_id` |
| `post_llm_call` | `run_agent.py:15901-15910` | `session_id, user_message, assistant_response, conversation_history, model, platform` |
| `transform_tool_result` | `model_tools.py:847-857` | `tool_name, args, result, task_id, session_id, tool_call_id, duration_ms` |
| `on_session_end` | `run_agent.py:16016-16024` (primary) + `cli.py:13831-13839` (safety net) | `session_id, completed, interrupted, model, platform` |

## What it does

On every hook firing the probe emits exactly one JSON-encoded log line at `INFO` level via the standard `logging` module. The shape:

```json
{
  "event": "hermes_smd_hook_probe",
  "hook_name": "post_tool_call",
  "sequence": 42,
  "timestamp_iso": "2026-05-24T14:42:00.123456+00:00",
  "kwargs_digest": {"tool_name": "str", "args": "dict", "result": "str", "...": "..."},
  "kwargs_seen": ["args", "duration_ms", "result", "session_id", "task_id", "tool_call_id", "tool_name"]
}
```

- `sequence` is monotonically incremented on every firing, guarded by a `threading.Lock`. Sorting log lines by `sequence` gives strict firing order.
- `kwargs_digest` records the Python type name of each received kwarg, never the value.
- `kwargs_seen` is the sorted list of kwarg key names, used by the verification step to confirm the kwargs match the citations.

The probe is observer-only. Every callback returns `None` and wraps its body in `try/except`; any exception is logged at `WARNING` and never propagates into Hermes.

## Install

```bash
hermes plugins install venturecrane/hermes-smd-overlay --enable
hermes plugins enable hermes-smd-hook-probe
```

The probe ships with no env requirements (`requires_env: []` in `plugin.yaml`), so it has no provisioning prerequisites beyond a working Hermes install.

## Reading the output

The probe logs through Python's standard `logging` module at `INFO`. By default Hermes writes that to `~/.hermes/logs/hermes.log` (verify against your local Hermes config; CLI runs may also stream to stderr).

Filter for probe events and sort by sequence:

```bash
grep '"event": "hermes_smd_hook_probe"' ~/.hermes/logs/hermes.log | jq -s 'sort_by(.sequence)'
```

## Ordering invariants the probe verifies

After a test scenario runs, the sorted output must satisfy all of the following:

- `pre_tool_call` MUST fire before `post_tool_call` for the same `tool_call_id`.
- `post_tool_call` MUST fire before `transform_tool_result` for the same `tool_call_id`.
- `pre_llm_call` MUST fire before `post_llm_call` for the same `session_id` within a turn.
- `on_session_end` MAY fire from either the primary site (per-turn, `run_agent.py:16016-16024`) or the safety-net site (interrupted CLI exit, `cli.py:13831-13839`) but NOT both for the same turn.

Any violation is a hook-surface regression. Stop and reconcile against `docs/hook-surface.md` before shipping.

## Re-verification

This probe is co-maintained with `docs/hook-surface.md`. On every Hermes pinned-ref bump:

1. Re-install the overlay against the new ref.
2. Re-run the probe with the standard test scenario.
3. Confirm the ordering invariants above still hold.
4. Update the citation line numbers in both `docs/hook-surface.md` and this README's hook table.

If any kwarg shape changes (a key is added, removed, or renamed in `kwargs_seen` between two runs), treat that as a breaking change and notify the overlay maintainers before bumping the pin.
