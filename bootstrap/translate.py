"""customer.yaml → per-profile Hermes config translation.

For each persona in ``customer.yaml.personas[]`` the bootstrap CLI writes:

  $HERMES_HOME/profiles/<persona-slug>/config.yaml   (Hermes-native config shape)
  $HERMES_HOME/profiles/<persona-slug>/SOUL.md       (per-persona identity)

The Hermes-native config consumes the multi-persona pattern documented
in ADR 0011; per-persona ``SOUL.md`` is what Hermes loads as identity
at profile boot. The tuned Honcho block embedded in each ``config.yaml``
matches the disposition decided in ADR 0016 (mirror, don't gate;
``recallMode: hybrid``, ``dialecticCadence: 3-5``, ``dialecticDepth: 1``,
``user_observe_me: true``, all other observation flags off,
``writeFrequency: session``).

Structural-vs-non-structural change rule (ADR 0019)
---------------------------------------------------
:func:`translate_customer_yaml` is the **structural** path: persona
add/remove, connector backend swap, OAuth scope change, trust ceiling
schema change. It rewrites profile directories from scratch and is
followed by a Machine restart so Hermes re-reads identity and
connector wiring from a clean slate. The function is idempotent —
re-running with the same input produces the same on-disk output (same
file bytes, same mtimes where unchanged) so repeated invocations
during debugging or recovery do not churn the volume.

:func:`start_customer_sync` is the **non-structural** path: tone
tweaks, review thresholds, voice samples, skill pin bumps within the
same catalog. The sidecar polls R2, applies the diff in place, and
signals the Hermes process with SIGHUP to reload without restart.
Structural diffs are rejected with a logged warning and posted to the
admin portal as a Captain re-provision request; the sidecar does NOT
trigger a restart itself.

Skill pin resolution
--------------------
Each persona may carry one or more entries in ``skills[]`` with a
``version`` field holding a 6-character content hash of the skill's
``SKILL.md`` + references. :func:`_resolve_skill_pins` computes the
actual content hash of the on-disk skill directory and:

* Refuses to translate if a pin disagrees with the actual hash (deploy
  was built against a different skill version than the customer
  expects).
* Tolerates the literal string ``pending`` for skills authored in
  Phase C — the pin is set after the skill is hashed.

Ported from
``ss-console/ai-employee/adapter/validate_customer_yaml.py`` +
``ss-console/ai-employee/adapter/resolve_skill_pins.py``.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "PyYAML is required by bootstrap.translate; install with `pip install pyyaml`"
    ) from exc

from bootstrap.validate import validate_customer_yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuned Honcho config block (ADR 0016)
# ---------------------------------------------------------------------------


def _honcho_block() -> dict[str, Any]:
    """Return the canonical Honcho config block.

    Embedded verbatim into every per-profile ``config.yaml``. Values
    are not pulled from ``customer.yaml`` — they are the SMD overlay's
    Honcho-disposition decision, not a customer-tunable knob. If a
    customer needs a different cadence, the answer is a follow-on ADR,
    not a per-customer override here.
    """
    return {
        "recallMode": "hybrid",
        "dialecticCadence": "3-5",
        "dialecticDepth": 1,
        "user_observe_me": True,
        "user_observe_others": False,
        "ai_observe_me": False,
        "ai_observe_others": False,
        "writeFrequency": "session",
    }


# ---------------------------------------------------------------------------
# Local-memory-files disposition (ADR 0016)
# ---------------------------------------------------------------------------


def _local_memory_files_block() -> dict[str, Any]:
    """Return the canonical local-memory-files declaration.

    Per ADR 0016, Honcho is the memory provider; Hermes' local-file
    memory sources (``MEMORY.md`` and ``USER.md``) must NOT be loaded
    because they would double-process memory and create state divergence
    between Honcho and the local files.

    The block declares both files disabled and records the rationale so
    operators inspecting a profile's ``config.yaml`` see the intent
    explicitly. The actual on-disk enforcement is the tombstone files
    written alongside (see :func:`_write_memory_tombstones`).
    """
    return {
        "memory_md_enabled": False,
        "user_md_enabled": False,
        "provider": "honcho",
        "rationale": (
            "ADR 0016 — Honcho is the memory provider; "
            "local files are tombstoned to prevent double-processing"
        ),
    }


# Tombstone-file contents. Single comment block each, idempotent across
# re-runs. Written into the profile directory alongside config.yaml and
# SOUL.md by ``translate_customer_yaml``.
_MEMORY_MD_TOMBSTONE = """<!--
This file is intentionally a tombstone (empty).

Honcho is the memory provider for this profile per ADR 0016.
Hermes' local-file memory sources (MEMORY.md and USER.md) must NOT
be loaded — they would double-process memory and create state
divergence between Honcho and the local files.

The presence of this empty file pre-empts Hermes' default template
auto-creation at profile boot. Do not edit; rerun `hermes-smd
bootstrap` to restore.
-->
""".encode()

_USER_MD_TOMBSTONE = """<!--
This file is intentionally a tombstone (empty).

Honcho is the memory provider for this profile per ADR 0016.
Hermes' local-file memory sources (MEMORY.md and USER.md) must NOT
be loaded — they would double-process memory and create state
divergence between Honcho and the local files.

The presence of this empty file pre-empts Hermes' default template
auto-creation at profile boot. Do not edit; rerun `hermes-smd
bootstrap` to restore.
-->
""".encode()


# ---------------------------------------------------------------------------
# Skill pin resolution (ported from resolve_skill_pins.py)
# ---------------------------------------------------------------------------


def _skill_content_hash(skill_dir: Path) -> str:
    """Deterministic content hash for a skill directory.

    Sorts files recursively by relative path, concatenates each file's
    bytes preceded by its relative path, sha256s the result. The first
    six characters of the hex digest are the pin used in
    ``customer.yaml``; the full digest is the audit ID.
    """
    if not skill_dir.exists():
        return "missing"
    digest = hashlib.sha256()
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(skill_dir).as_posix()
            digest.update(rel.encode())
            digest.update(b"\x00")
            digest.update(path.read_bytes())
            digest.update(b"\x00")
    return digest.hexdigest()


def _resolve_skill_pins(
    personas: list[dict[str, Any]],
    skills_dir: Path,
) -> dict[str, str]:
    """Resolve every persona's skill version pins against on-disk content.

    Args:
        personas: The ``personas[]`` list from ``customer.yaml``.
        skills_dir: Root directory of the skill catalog.

    Returns:
        A dict mapping ``<persona-slug>/<skill-name>`` to the resolved
        6-char pin (the actual on-disk pin, with ``pending`` skills
        replaced by the actual hash so per-profile config can record
        the resolved value).

    Raises:
        TranslateError: If any enabled skill has a pinned version that
            disagrees with the on-disk content hash, or if the skill
            directory cannot be found.
    """
    resolved: dict[str, str] = {}
    errors: list[str] = []

    for persona in personas:
        persona_slug = persona.get("slug", "?")
        for skill in persona.get("skills", []) or []:
            if not skill.get("enabled"):
                continue
            name = skill.get("name")
            if not name:
                errors.append(f"persona {persona_slug!r}: skill entry missing name")
                continue
            pinned = str(skill.get("version", "pending"))

            skill_dir = skills_dir / name
            if not skill_dir.exists():
                errors.append(f"persona {persona_slug!r}: skill {name!r} not found at {skill_dir}")
                continue

            actual_hash = _skill_content_hash(skill_dir)
            actual_pin = actual_hash[:6]

            if pinned == "pending":
                resolved[f"{persona_slug}/{name}"] = actual_pin
                continue

            if pinned != actual_pin:
                errors.append(
                    f"persona {persona_slug!r}: skill {name!r} pinned version "
                    f"{pinned!r} != actual content hash {actual_pin!r}; rebuild "
                    "the Machine image or roll back the pin"
                )
            else:
                resolved[f"{persona_slug}/{name}"] = actual_pin

    if errors:
        raise TranslateError("skill pin resolution failed:\n  - " + "\n  - ".join(errors))
    return resolved


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TranslateError(RuntimeError):
    """Raised when ``customer.yaml`` cannot be translated.

    Causes include validation failures, skill-pin mismatches, and
    filesystem errors writing per-profile directories.
    """


# ---------------------------------------------------------------------------
# Per-profile materialization
# ---------------------------------------------------------------------------


def _persona_config(
    persona: dict[str, Any],
    customer: dict[str, Any],
    resolved_pins: dict[str, str],
) -> dict[str, Any]:
    """Build the Hermes-native ``config.yaml`` body for one persona."""
    persona_slug = persona.get("slug", "")
    skills: list[dict[str, Any]] = []
    for skill in persona.get("skills", []) or []:
        if not skill.get("enabled"):
            continue
        name = skill.get("name")
        key = f"{persona_slug}/{name}"
        skills.append(
            {
                "name": name,
                "version": resolved_pins.get(key, str(skill.get("version", "pending"))),
                "trust_ceiling": skill.get("trust_ceiling"),
            }
        )

    return {
        "schema_version": 1,
        "profile_slug": persona_slug,
        "customer_id": customer.get("customer_id"),
        "customer_name": customer.get("customer_name"),
        "vertical": customer.get("vertical"),
        "model": customer.get("model"),
        "persona": {
            "name": persona.get("name"),
            "title": persona.get("title"),
            "status": persona.get("status"),
            "tone": list(persona.get("tone", []) or []),
        },
        "skills": skills,
        "connectors": customer.get("connectors") or {},
        "scope": customer.get("scope") or {},
        "escalation": customer.get("escalation") or {},
        "voice_library": customer.get("voice_library") or {},
        "memory": customer.get("memory") or {},
        "honcho": _honcho_block(),
        "local_memory_files": _local_memory_files_block(),
    }


def _soul_body(persona: dict[str, Any], customer: dict[str, Any]) -> str:
    """Build the per-persona ``SOUL.md`` body.

    The shape is intentionally minimal: identity (name, title, tone)
    plus the customer context that anchors the persona. Authored voice
    samples and skill catalogs are consumed at runtime from the same
    profile directory, not embedded here.
    """
    persona_name = persona.get("name") or persona.get("slug") or "Persona"
    title = persona.get("title") or "AI Associate"
    tone = persona.get("tone") or []
    tone_block = "\n".join(f"- {item}" for item in tone) if tone else "- (none specified)"

    customer_name = customer.get("customer_name") or customer.get("customer_id") or "this customer"
    vertical = customer.get("vertical") or "unspecified"

    return (
        f"# {persona_name}\n\n"
        f"You are {persona_name}, {title} at {customer_name}.\n\n"
        f"## Vertical\n\n"
        f"{vertical}\n\n"
        f"## Tone\n\n"
        f"{tone_block}\n"
    )


def _write_if_changed(target: Path, content: bytes) -> bool:
    """Write ``content`` to ``target`` only if the current bytes differ.

    Returns ``True`` if the file was written (or created), ``False`` if
    it already held the desired bytes. Idempotency primitive: the
    bootstrap subcommand reports the number of profiles written, but
    repeated runs against unchanged input do not churn the volume.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = target.read_bytes()
        if existing == content:
            return False
    target.write_bytes(content)
    return True


def _yaml_bytes(data: dict[str, Any]) -> bytes:
    """Serialize ``data`` to YAML with stable key order."""
    return yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).encode()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def translate_customer_yaml(
    customer_yaml_path: str,
    hermes_home: str,
    *,
    skills_dir: str | None = None,
) -> list[str]:
    """Translate ``customer.yaml`` into per-profile Hermes config.

    For each persona in ``customer.yaml.personas[]`` writes:

    * ``<hermes_home>/profiles/<slug>/config.yaml`` — Hermes-native
      config with the resolved skill catalog, connector wiring,
      scope, the tuned Honcho block (see ADR 0016), and the
      ``local_memory_files`` block declaring MEMORY.md / USER.md
      disabled.
    * ``<hermes_home>/profiles/<slug>/SOUL.md`` — per-persona identity
      consumed by Hermes at profile boot.
    * ``<hermes_home>/profiles/<slug>/MEMORY.md`` — tombstone (empty)
      pre-empting Hermes' default-template auto-creation. Honcho is
      the memory provider per ADR 0016; this local file must NOT be
      loaded.
    * ``<hermes_home>/profiles/<slug>/USER.md`` — tombstone (empty)
      with the same rationale.

    The function is idempotent. Re-running with the same input
    produces the same on-disk bytes; unchanged files are not
    rewritten. Translation is preceded by schema validation
    (:func:`bootstrap.validate.validate_customer_yaml`) and skill-pin
    resolution; either failing raises :class:`TranslateError` before
    any disk write occurs.

    Args:
        customer_yaml_path: Absolute path to the authored
            ``customer.yaml`` (typically ``/opt/data/customer.yaml``
            on the Fly volume).
        hermes_home: Hermes home directory under which profile
            directories live (typically ``~/.hermes`` or the
            ``HERMES_HOME`` env var).
        skills_dir: Override for the skill catalog root. Defaults to
            ``$SKILLS_DIR`` env var if set, otherwise
            ``<hermes_home>/skills``.

    Returns:
        List of persona slugs whose profile directories were
        materialized.

    Raises:
        TranslateError: If validation fails, skill pins disagree with
            on-disk content, or any required field is missing.
    """
    yaml_path = Path(customer_yaml_path)
    home_path = Path(hermes_home)
    skills_path = Path(skills_dir or os.environ.get("SKILLS_DIR") or (home_path / "skills"))

    validation_errors = validate_customer_yaml(yaml_path)
    if validation_errors:
        raise TranslateError(
            "customer.yaml failed validation:\n  - " + "\n  - ".join(validation_errors)
        )

    with yaml_path.open() as handle:
        customer = yaml.safe_load(handle) or {}
    personas = customer.get("personas") or []
    if not personas:
        raise TranslateError("customer.yaml has no personas to translate")

    resolved_pins = _resolve_skill_pins(personas, skills_path)

    written_slugs: list[str] = []
    profiles_root = home_path / "profiles"
    for persona in personas:
        slug = persona.get("slug")
        if not slug:
            raise TranslateError("persona missing slug after validation")
        profile_dir = profiles_root / slug
        config_path = profile_dir / "config.yaml"
        soul_path = profile_dir / "SOUL.md"
        memory_md_path = profile_dir / "MEMORY.md"
        user_md_path = profile_dir / "USER.md"

        config_body = _persona_config(persona, customer, resolved_pins)
        soul_body = _soul_body(persona, customer)

        wrote_config = _write_if_changed(config_path, _yaml_bytes(config_body))
        wrote_soul = _write_if_changed(soul_path, soul_body.encode())
        # ADR 0016 — Honcho is the memory provider; tombstone the local
        # MEMORY.md / USER.md so Hermes does not auto-populate them with
        # default templates at profile boot.
        wrote_memory_md = _write_if_changed(memory_md_path, _MEMORY_MD_TOMBSTONE)
        wrote_user_md = _write_if_changed(user_md_path, _USER_MD_TOMBSTONE)
        if wrote_config or wrote_soul or wrote_memory_md or wrote_user_md:
            logger.info(
                "translate: wrote profile %s (config=%s, soul=%s, memory_md=%s, user_md=%s)",
                slug,
                wrote_config,
                wrote_soul,
                wrote_memory_md,
                wrote_user_md,
            )
        else:
            logger.debug("translate: profile %s already up to date", slug)
        written_slugs.append(slug)

    return written_slugs


def start_customer_sync(
    customer_yaml_path: str,
    r2_bucket: str,
    interval: int,
) -> None:
    """Long-running R2 sidecar — not implemented in this overlay revision.

    The non-structural reload path (tone tweaks, voice samples, in-
    catalog skill pin bumps; see ADR 0019) is filed as follow-on work
    against the overlay's customer-sync follow-on issue. The CLI
    plumbing in :mod:`bootstrap.cli` is in place so this function can
    be filled in without changing the operator surface.

    Args:
        customer_yaml_path: Absolute path to the on-disk
            ``customer.yaml`` to keep in sync.
        r2_bucket: R2 source identifier (URL or bucket reference).
        interval: Poll interval in seconds.

    Raises:
        NotImplementedError: Always — the sidecar is filed as a
            follow-on.
    """
    raise NotImplementedError(
        "customer-sync sidecar is filed as a follow-on to §7 "
        "(non-structural reload path; see ADR 0019)"
    )


__all__ = [
    "TranslateError",
    "start_customer_sync",
    "translate_customer_yaml",
]
