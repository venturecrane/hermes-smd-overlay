"""customer.yaml schema validation.

Validates an authored ``customer.yaml`` against the schema documented
in ``ai-employee/customer.yaml.schema.md`` (ss-console). Returns a
list of human-readable error strings; an empty list means the file
is valid.

Ported from
``ss-console/ai-employee/adapter/validate_customer_yaml.py`` with
two adaptations:

* The source script was a CLI returning exit codes; this module is
  importable so the bootstrap CLI calls it before translation, and
  ``shared.customer_config.CustomerConfig`` can call it at runtime
  loader time.
* The source validated the existence of on-disk skill directories and
  connector wrappers relative to the ss-console layout. The overlay
  doesn't ship those trees in this repo — skills live in
  ``ss-console/ai-employee/skills/`` and connector adapters live in
  separate plugin directories. Schema validation here is the
  structural shape check; existence checks are deferred to the
  translation step, which knows where the per-customer skill catalog
  is materialized.
"""

import logging
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "PyYAML is required by bootstrap.validate; install with `pip install pyyaml`"
    ) from exc

logger = logging.getLogger(__name__)


ACCEPTED_VERTICALS = {
    "marketing-agency",
    "law-firm",
    "real-estate",
    "manufacturing",
    "insurance",
    "mixed",
}

ACCEPTED_CEILINGS = {"autonomous", "draft_for_review", "refused"}

ACCEPTED_BACKEND_PREFIXES = ("composio:", "mcp:", "build:", "synthetic:")

REQUIRED_TOP_LEVEL_FIELDS = (
    "customer_id",
    "customer_name",
    "vertical",
    "fly_region",
    "hermes_ref",
    "model",
)


def _err(msg: str, errors: list[str]) -> None:
    errors.append(msg)


def validate_customer_yaml(customer_yaml: Path) -> list[str]:
    """Validate the file at ``customer_yaml`` and return error strings.

    Args:
        customer_yaml: Path to the authored ``customer.yaml``.

    Returns:
        List of human-readable error messages. Empty list means the
        file is structurally valid. The list is ordered roughly by
        where in the document the error appears.
    """
    errors: list[str] = []

    if not customer_yaml.exists():
        _err(f"customer.yaml not found at {customer_yaml}", errors)
        return errors

    try:
        with customer_yaml.open() as handle:
            cfg = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        _err(f"customer.yaml not valid YAML: {exc}", errors)
        return errors

    if cfg is None:
        _err("customer.yaml is empty", errors)
        return errors
    if not isinstance(cfg, dict):
        _err(f"customer.yaml root must be a mapping; got {type(cfg).__name__}", errors)
        return errors

    _validate_top_level(cfg, errors)
    _validate_personas(cfg, errors)
    _validate_connectors(cfg, errors)
    _validate_memory(cfg, errors)
    _validate_voice_library(cfg, errors)

    return errors


def _validate_top_level(cfg: dict[str, Any], errors: list[str]) -> None:
    for field in REQUIRED_TOP_LEVEL_FIELDS:
        if field not in cfg:
            _err(f"missing required top-level field: {field}", errors)

    vertical = cfg.get("vertical")
    if vertical is not None and vertical not in ACCEPTED_VERTICALS:
        _err(
            f"vertical must be one of {sorted(ACCEPTED_VERTICALS)}; got {vertical!r}",
            errors,
        )


def _validate_personas(cfg: dict[str, Any], errors: list[str]) -> None:
    personas = cfg.get("personas")
    if personas is None:
        _err("personas: missing (at least one persona is required)", errors)
        return
    if not isinstance(personas, list):
        _err(
            f"personas must be a list; got {type(personas).__name__}",
            errors,
        )
        return
    if not personas:
        _err("personas: must contain at least one persona", errors)
        return

    seen_slugs: set[str] = set()
    for i, persona in enumerate(personas):
        prefix = f"personas[{i}]"
        if not isinstance(persona, dict):
            _err(f"{prefix}: must be a mapping", errors)
            continue
        slug = persona.get("slug")
        if not slug:
            _err(f"{prefix}: missing slug", errors)
        elif slug in seen_slugs:
            _err(f"{prefix}: duplicate slug {slug!r}", errors)
        else:
            seen_slugs.add(slug)
        for field in ("name", "status"):
            if field not in persona:
                _err(f"{prefix}({slug}): missing field {field}", errors)
        for j, skill in enumerate(persona.get("skills", []) or []):
            sk_prefix = f"{prefix}({slug}).skills[{j}]"
            if not isinstance(skill, dict):
                _err(f"{sk_prefix}: must be a mapping", errors)
                continue
            for field in ("name", "version", "trust_ceiling", "enabled"):
                if field not in skill:
                    _err(f"{sk_prefix}: missing field {field}", errors)
            if "trust_ceiling" in skill and skill["trust_ceiling"] not in ACCEPTED_CEILINGS:
                _err(
                    f"{sk_prefix}: trust_ceiling must be one of {sorted(ACCEPTED_CEILINGS)}",
                    errors,
                )


def _validate_connectors(cfg: dict[str, Any], errors: list[str]) -> None:
    connectors = cfg.get("connectors") or {}
    if not isinstance(connectors, dict):
        _err(
            f"connectors must be a mapping; got {type(connectors).__name__}",
            errors,
        )
        return
    for key, conn in connectors.items():
        prefix = f"connectors.{key}"
        if not isinstance(conn, dict):
            _err(f"{prefix}: must be a mapping", errors)
            continue
        backend = conn.get("backend")
        if not backend:
            _err(f"{prefix}: missing backend", errors)
            continue
        if not isinstance(backend, str) or not backend.startswith(ACCEPTED_BACKEND_PREFIXES):
            _err(
                f"{prefix}: backend {backend!r} must start with one of {ACCEPTED_BACKEND_PREFIXES}",
                errors,
            )


def _validate_memory(cfg: dict[str, Any], errors: list[str]) -> None:
    memory = cfg.get("memory")
    if memory is None:
        # Memory block is required for cost telemetry rollup; per source.
        _err("memory: missing (d1_namespace, r2_vault_path, vectorize_index required)", errors)
        return
    if not isinstance(memory, dict):
        _err(f"memory must be a mapping; got {type(memory).__name__}", errors)
        return
    for field in ("d1_namespace", "r2_vault_path", "vectorize_index"):
        if field not in memory:
            _err(f"memory.{field}: missing", errors)

    customer_id = cfg.get("customer_id")
    if customer_id:
        # Isolation invariant: memory.* must match customer_id.
        if memory.get("d1_namespace") and memory["d1_namespace"] != customer_id:
            _err(
                f"memory.d1_namespace ({memory['d1_namespace']!r}) "
                f"must match customer_id ({customer_id!r})",
                errors,
            )
        r2_path = memory.get("r2_vault_path", "")
        if r2_path and f"vaults/{customer_id}/" not in r2_path:
            _err(
                f"memory.r2_vault_path ({r2_path!r}) must contain 'vaults/{customer_id}/'",
                errors,
            )
        index = memory.get("vectorize_index", "")
        expected_index = f"hermes-{customer_id}-vault"
        if index and index != expected_index:
            _err(
                f"memory.vectorize_index ({index!r}) must equal {expected_index!r}",
                errors,
            )


def _validate_voice_library(cfg: dict[str, Any], errors: list[str]) -> None:
    voice = cfg.get("voice_library")
    if voice is None:
        return  # voice_library is optional
    if not isinstance(voice, dict):
        _err(
            f"voice_library must be a mapping; got {type(voice).__name__}",
            errors,
        )


__all__ = [
    "ACCEPTED_BACKEND_PREFIXES",
    "ACCEPTED_CEILINGS",
    "ACCEPTED_VERTICALS",
    "REQUIRED_TOP_LEVEL_FIELDS",
    "validate_customer_yaml",
]
