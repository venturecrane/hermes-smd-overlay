"""Build the ``operator.runtime.config/v1`` facts snapshot (ADR 0043 ``config`` kind).

The console's drift audit (ss-console ``operator/bin/operator-drift-audit.py``)
reads this Machine's *actual materialized state* through the ADR 0043 read seam
and diffs it against the repo's declared desired-state. This module produces the
Machine-side facts snapshot it reads.

THREE HARD RULES, because this snapshot crosses the trust boundary to the console:

1. **Presence, never values.** ``env_presence`` reports only ``{present, empty}``
   per allow-listed var — never a value, length, prefix, or hash of a secret. The
   allow-list is exactly ``contracts/consumes.yaml`` (the vars the overlay
   *declares* it reads); a var the overlay never reads is never reported, so the
   snapshot can't become an environment dump.

2. **Truthful or degraded, never fabricated.** Every introspection that can't run
   (agent process not found, profiles dir absent, a config file that won't parse)
   appends to ``degraded[]`` and omits/empties that field. The diff engine treats
   a degraded field as **unknown, never absent** — this is what stops a transient
   read failure from being misread as "the cron job vanished."

3. **The agent's env, not the gate's.** The strip-violation check (a stripped
   secret reappearing in the *agent's* environment) is only meaningful against the
   agent process's real environment. The gate runs as a separate process with a
   different env, so ``env_presence`` reads the live agent's ``/proc/<pid>/environ``
   — discovered by the one env var injected only into the agent
   (``SMD_CUSTOMER_SLUG``), never into the gate. If the agent process can't be
   found, ``env_presence`` degrades rather than reporting the gate's env (which
   would be both false-positive and false-negative prone for exactly the stripped
   vars we care about).
"""

from __future__ import annotations

import hashlib
import json
import os
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml

from shared.consumes_conformance import declared_vars

SCHEMA = "operator.runtime.config/v1"

# Default Hermes home (overlay convention; HERMES_HOME overrides at runtime).
_DEFAULT_HERMES_HOME = "/opt/data"

# Substrings (case-insensitive) that mark a config key as secret-bearing; such
# keys are redacted before the config digest so the hash never depends on a
# secret value and a value can't leak through a digest collision probe.
_SECRET_KEY_MARKERS = ("token", "secret", "password", "credential", "api_key", "apikey")

# Build-baked overlay-ref sentinel locations (ss-console Dockerfile writes one);
# the PEP 610 direct_url.json is preferred because it reports the commit pip
# ACTUALLY installed, not a label that could drift from the install.
_SENTINEL_PATHS = ("/app/OVERLAY_REF", "/app/contracts/OVERLAY_REF")


# --------------------------------------------------------------------------- #
# Impure adapters (filesystem / /proc / package metadata). Each is small, each
# degrades to a sentinel rather than raising, so build_snapshot stays pure.
# --------------------------------------------------------------------------- #


def _read_proc_env_names(pid: int) -> dict[str, bool] | None:
    """Return ``{VAR_NAME: is_empty}`` for one process, or None if unreadable.

    Parses ``/proc/<pid>/environ`` (NUL-separated ``KEY=VALUE``). Records ONLY
    the name and whether the value is the empty string — the value itself is
    never retained or returned.
    """
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (OSError, ValueError):
        return None
    out: dict[str, bool] = {}
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        name, sep, value = chunk.partition(b"=")
        if not sep:
            continue
        try:
            key = name.decode("ascii")
        except UnicodeDecodeError:
            continue
        out[key] = value == b""
    return out


def find_agent_env(own_slug: str | None) -> dict[str, bool] | None:
    """Discover the live agent process and return its ``{NAME: is_empty}`` env map.

    The agent is the process carrying ``SMD_CUSTOMER_SLUG`` (injected only into
    the agent, never into this gate process). When ``own_slug`` is known we also
    require the value to match, so a stray process can't be mistaken for ours.
    Returns None when no such process is readable (→ env_presence degrades).
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    self_pid = os.getpid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        env = _read_proc_env_names(pid)
        if env is None or "SMD_CUSTOMER_SLUG" not in env:
            continue
        # Confirm it's *our* agent, not some other tenant (defense in depth even
        # though Machines are single-tenant): compare the actual slug value.
        if own_slug:
            try:
                raw = Path(f"/proc/{pid}/environ").read_bytes()
            except OSError:
                continue
            if f"SMD_CUSTOMER_SLUG={own_slug}".encode() not in raw.split(b"\x00"):
                continue
        return env
    return None


def resolve_overlay_ref() -> dict[str, str | None]:
    """Resolve the installed overlay commit.

    Primary: PEP 610 ``direct_url.json`` — the commit pip resolved at install,
    i.e. the code that is ACTUALLY running. Fallback: a build-baked sentinel
    file. ``{value: None, source: None}`` when neither resolves (→ degraded)."""
    try:
        dist = metadata.distribution("hermes-smd-overlay")
        raw = dist.read_text("direct_url.json")
        if raw:
            data = json.loads(raw)
            commit = (data.get("vcs_info") or {}).get("commit_id")
            if commit:
                return {"value": str(commit), "source": "direct_url"}
    except Exception:
        pass
    for path in _SENTINEL_PATHS:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return {"value": value, "source": "sentinel"}
    return {"value": None, "source": None}


def _redact_secrets(obj: Any) -> Any:
    """Recursively replace secret-keyed values with a fixed sentinel.

    The digest must not depend on any secret value. Keys whose name contains a
    secret marker get their value replaced by ``"<redacted>"`` before hashing."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            lname = str(key).lower()
            if any(marker in lname for marker in _SECRET_KEY_MARKERS):
                out[key] = "<redacted>"
            else:
                out[key] = _redact_secrets(value)
        return out
    if isinstance(obj, list):
        return [_redact_secrets(v) for v in obj]
    return obj


def _config_digest_and_skills(config_path: Path) -> tuple[str | None, list[str] | None]:
    """Hash a profile ``config.yaml`` (secrets redacted) and pull enabled skills.

    Returns ``(sha256_hex_or_None, skills_or_None)``. A parse failure yields
    ``(None, None)`` so the caller can mark the field degraded rather than guess."""
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    redacted = _redact_secrets(data)
    canonical = json.dumps(redacted, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    skills = _extract_skill_names(data.get("skills"))
    return digest, skills


def _extract_skill_names(raw: Any) -> list[str] | None:
    """Best-effort enabled-skill names from a Hermes config ``skills`` value.

    Tolerates a list of strings or a list of ``{name: ...}`` dicts. Unknown
    shapes return None (→ skills introspection degrades, never fabricates)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
        else:
            return None
    return sorted(names)


def _read_cron_jobs(jobs_path: Path) -> list[dict[str, Any]] | None:
    """Read a profile's materialized cron jobs (name + schedule only).

    This is the same ``cron/jobs.json`` the gateway ticks (ADR 0047 / the C1
    profile-home fix), so it is the authoritative materialized cron state.
    Returns None on a parse failure (→ that profile's cron degrades)."""
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw_jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(raw_jobs, list):
        return None
    out: list[dict[str, Any]] = []
    for job in raw_jobs:
        if not isinstance(job, dict):
            continue
        out.append(
            {
                "name": job.get("name") or job.get("id"),
                "schedule": job.get("schedule") or job.get("cron"),
                "skill": job.get("skill"),
                "last_status": job.get("last_status"),
            }
        )
    return out


def read_profiles(hermes_home: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Enumerate ``$HERMES_HOME/profiles/<slug>/`` and read each profile's
    materialized config + cron. Returns ``(profiles, degraded)``."""
    degraded: list[dict[str, str]] = []
    profiles_root = Path(hermes_home) / "profiles"
    if not profiles_root.is_dir():
        return [], [{"field": "materialized.profiles", "reason": "profiles dir absent"}]

    profiles: list[dict[str, Any]] = []
    for entry in sorted(profiles_root.iterdir()):
        if not entry.is_dir():
            continue
        slug = entry.name
        prof: dict[str, Any] = {"slug": slug}

        config_path = entry / "config.yaml"
        if config_path.is_file():
            prof["config_present"] = True
            digest, skills = _config_digest_and_skills(config_path)
            prof["config_sha256"] = digest
            if digest is None:
                degraded.append(
                    {"field": f"materialized.{slug}.config_sha256", "reason": "config unparseable"}
                )
            if skills is None:
                prof["skills_enabled"] = None
                degraded.append(
                    {
                        "field": f"materialized.{slug}.skills_enabled",
                        "reason": "unknown skills shape",
                    }
                )
            else:
                prof["skills_enabled"] = skills
        else:
            prof["config_present"] = False
            prof["config_sha256"] = None
            prof["skills_enabled"] = None
            degraded.append(
                {"field": f"materialized.{slug}.config", "reason": "config.yaml absent"}
            )

        jobs_path = entry / "cron" / "jobs.json"
        if jobs_path.is_file():
            jobs = _read_cron_jobs(jobs_path)
            if jobs is None:
                prof["cron"] = {"available": False, "jobs": []}
                degraded.append(
                    {"field": f"materialized.{slug}.cron", "reason": "jobs.json unparseable"}
                )
            else:
                prof["cron"] = {"available": True, "jobs": jobs}
        else:
            # No jobs file is a real, determinable fact (no cron materialized),
            # NOT a degraded read — the diff engine compares this to authored
            # cron to decide cron_not_registered.
            prof["cron"] = {"available": True, "jobs": []}

        profiles.append(prof)
    return profiles, degraded


# --------------------------------------------------------------------------- #
# Pure assembly
# --------------------------------------------------------------------------- #


def build_snapshot(
    *,
    allowlist: list[str] | None,
    agent_env: dict[str, bool] | None,
    overlay_ref: dict[str, str | None],
    profiles: list[dict[str, Any]],
    extra_degraded: list[dict[str, str]],
) -> dict[str, Any]:
    """Assemble the wire snapshot from already-read inputs (pure, unit-testable).

    ``allowlist`` None → the consumes.yaml allow-list couldn't be loaded.
    ``agent_env`` None → the agent process wasn't introspectable. Either case
    degrades ``env_presence`` to unknown rather than emitting a partial/wrong map.
    """
    degraded: list[dict[str, str]] = list(extra_degraded)

    if allowlist is None:
        env_presence: dict[str, dict[str, bool]] | None = None
        degraded.append({"field": "env_presence", "reason": "consumes allow-list unavailable"})
    elif agent_env is None:
        env_presence = None
        degraded.append({"field": "env_presence", "reason": "agent process not introspectable"})
    else:
        env_presence = {}
        for var in allowlist:
            present = var in agent_env
            env_presence[var] = {"present": present, "empty": present and agent_env[var]}

    if overlay_ref.get("value") is None:
        degraded.append({"field": "overlay_ref", "reason": "no direct_url.json or sentinel"})

    # registry (running tool/connector registry) lives in the agent process's
    # memory, which the gate cannot read — honestly degraded, not guessed.
    degraded.append({"field": "registry", "reason": "agent in-memory registry not gate-readable"})

    return {
        "schema": SCHEMA,
        "overlay_ref": overlay_ref,
        "env_presence": env_presence,
        "materialized": {"profiles": profiles},
        "registry": {"tools_registered": None, "connectors_active": None},
        "degraded": degraded,
    }


def snapshot(*, own_slug: str | None = None, hermes_home: str | None = None) -> dict[str, Any]:
    """Top-level: read live Machine state and assemble the config snapshot.

    Thin impure orchestration over the adapters above + the pure ``build_snapshot``.
    Never raises for an introspection failure — every failure becomes a
    ``degraded[]`` entry."""
    home = hermes_home or os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    slug = own_slug or os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG")

    try:
        allowlist: list[str] | None = sorted(declared_vars().keys())
    except Exception:
        allowlist = None

    agent_env = find_agent_env(slug)
    overlay_ref = resolve_overlay_ref()
    profiles, profile_degraded = read_profiles(home)

    return build_snapshot(
        allowlist=allowlist,
        agent_env=agent_env,
        overlay_ref=overlay_ref,
        profiles=profiles,
        extra_degraded=profile_degraded,
    )


__all__ = [
    "SCHEMA",
    "build_snapshot",
    "snapshot",
    "find_agent_env",
    "resolve_overlay_ref",
    "read_profiles",
]
