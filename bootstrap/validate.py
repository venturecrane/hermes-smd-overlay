"""customer.yaml schema validation.

Validates an authored ``customer.yaml`` against the schema documented
in ``operator/customer.yaml.schema.md`` (ss-console). Returns a
list of human-readable error strings; an empty list means the file
is valid.

Ported from
``ss-console/operator/adapter/validate_customer_yaml.py`` with
two adaptations:

* The source script was a CLI returning exit codes; this module is
  importable so the bootstrap CLI calls it before translation, and
  ``shared.customer_config.CustomerConfig`` can call it at runtime
  loader time.
* The source validated the existence of on-disk skill directories and
  connector wrappers relative to the ss-console layout. The overlay
  doesn't ship those trees in this repo — skills live in
  ``ss-console/operator/skills/`` and connector adapters live in
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

from bootstrap.secret_scan import finding_to_error, scan_parsed_value, scan_raw_yaml

logger = logging.getLogger(__name__)


# SOURCE OF TRUTH: ss-console src/lib/operator/customer-yaml/types.ts
# (ACCEPTED_VERTICALS). The console is the authoring gate; this on-box validator
# must accept exactly what the console accepts, or a config the console blessed
# would be rejected on apply (ADR 0044). Keep this set byte-identical to the TS
# list — the cross-repo contract fixtures pin the agreement.
ACCEPTED_VERTICALS = {
    "marketing-agency",
    "law-firm",
    "real-estate",
    "manufacturing",
    "insurance",
    "veterinary",
    "dental",
    "med-spa",
    "accounting",
    "title",
    "mortgage",
    "ria",
    "property-management",
    "home-services",
    "mixed",
}

# SOURCE OF TRUTH: ss-console ACCEPTED_TRUST_CEILINGS (types.ts). Already aligned.
ACCEPTED_CEILINGS = {"autonomous", "draft_for_review", "refused"}

ACCEPTED_BACKEND_PREFIXES = ("mcp:", "build:", "synthetic:")

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

    raw_text = customer_yaml.read_text()

    # Secret scan, raw pass FIRST — mirrors the console's two-pass detector
    # (ADR 0044 validator parity). Running it before the structural parse means a
    # malformed YAML that still leaks a secret fails closed. Findings never echo
    # the matched value.
    for finding in scan_raw_yaml(raw_text):
        _err(finding_to_error(finding), errors)

    try:
        cfg = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        _err(f"customer.yaml not valid YAML: {exc}", errors)
        return errors

    if cfg is None:
        _err("customer.yaml is empty", errors)
        return errors
    if not isinstance(cfg, dict):
        _err(f"customer.yaml root must be a mapping; got {type(cfg).__name__}", errors)
        return errors

    # Secret scan, parsed pass — JSONPath context per finding (e.g.
    # connectors.PracticeManagement.token_ref). Deduped against the raw pass so a
    # leak detected both ways is reported once.
    _scan_parsed_secrets(cfg, errors)

    _validate_top_level(cfg, errors)
    _validate_personas(cfg, errors)
    _validate_connectors(cfg, errors)
    _validate_memory(cfg, errors)
    _validate_voice_library(cfg, errors)

    return errors


def _scan_parsed_secrets(cfg: dict[str, Any], errors: list[str]) -> None:
    """Append parsed-value secret findings, skipping ones the raw pass already
    reported (same category+reason text) so a leak is not double-counted."""
    already = set(errors)
    for finding in scan_parsed_value(cfg):
        msg = finding_to_error(finding)
        # The raw and parsed passes describe the same leak differently (line vs
        # path); dedupe on the path-keyed message, and skip if the value-shape
        # reason already appears from the raw pass for an unkeyed line.
        if msg not in already:
            _err(msg, errors)
            already.add(msg)


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
            # Required: name + trust_ceiling. version/enabled are OPTIONAL — the
            # translator defaults them (version→"pending", enabled→falsy), and
            # the console validator (source of truth, sections-personas.ts) treats
            # them as optional-but-typed. Requiring them here false-rejected
            # console-valid configs on apply (ADR 0044 parity).
            for field in ("name", "trust_ceiling"):
                if field not in skill:
                    _err(f"{sk_prefix}: missing field {field}", errors)
            if "trust_ceiling" in skill and skill["trust_ceiling"] not in ACCEPTED_CEILINGS:
                _err(
                    f"{sk_prefix}: trust_ceiling must be one of {sorted(ACCEPTED_CEILINGS)}",
                    errors,
                )
            if "version" in skill and not isinstance(skill["version"], str):
                _err(f"{sk_prefix}: version must be a string when present", errors)
            if "enabled" in skill and not isinstance(skill["enabled"], bool):
                _err(f"{sk_prefix}: enabled must be a boolean when present", errors)


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
