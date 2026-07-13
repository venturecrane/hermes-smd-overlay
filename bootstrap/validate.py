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

# SOURCE OF TRUTH: ss-console ACCEPTED_EXPOSURE_CEILINGS (types.ts). The legal
# values for a persona exposure entry — the ADR 0035 content classes plus the
# ADR 0071 `confirm` (send after an explicit in-turn approval) ceiling. Kept
# aligned with the console.
ACCEPTED_CEILINGS = {"autonomous", "confirm", "draft_for_review", "refused"}

# SOURCE OF TRUTH: ss-console ACCEPTED_ACTION_CLASSES (types.ts), minus ``read``.
# ADR 0056: exposure is authored PER action class; ``read`` is never customer-
# authored (enforcement always allows reads), so it must not appear in an
# exposure map. The console rejects a ``read`` exposure key explicitly.
AUTHORED_EXPOSURE_ACTION_CLASSES = {
    "internal_write",
    "external_send",
    "external_send_internal",
    "external_send_client",
    "external_send_vendor",
    "commitment",
    "destructive",
    "code_execution",
}

# The send action classes — the only classes for which the `confirm` ceiling
# (ADR 0071) has defined behavior, and the classes a typed outbound roster
# (scope.outbound_roster) resolves to (ADR 0075). Kept as a named set so the
# confirm guard and any future send-scoped check share one definition.
SEND_ACTION_CLASSES = {
    "external_send",
    "external_send_internal",
    "external_send_client",
    "external_send_vendor",
}

# Closed vocabulary for a scope.outbound_roster entry's `class` (ADR 0075).
OUTBOUND_ROSTER_CLASSES = {"client", "records_vendor"}

# Public-mail providers where a whole-@domain grant is meaningless (the domain is
# shared by millions), so a DOMAIN-form outbound_roster entry is rejected — but an
# EXACT address at one of these domains is valid (a PI client is a consumer on
# gmail, so `jane@gmail.com` as a client must pass). Mirrors the console rule.
_PUBLIC_MAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "yahoo.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
}

# Legacy entitlement fields retired by ADR 0056 with NO compatibility shim. The
# console rejects each as ``LegacyEntitlementField``; the on-box validator must
# reject them too or a config the console would block could land on the volume
# through a bypassed authoring path.
LEGACY_ENTITLEMENT_FIELDS = ("trust_ceiling", "action_ceilings")

# native: — a bundled Hermes provider (not an external server we wire). Web
# search rides this: `native:brave-free` -> config web.search_backend, handled by
# translate._materialize_web_search. Added 2026-07-08 with the ADR 0070 native cut.
ACCEPTED_BACKEND_PREFIXES = ("mcp:", "build:", "synthetic:", "native:")

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
    _validate_webhook_triggers(cfg, errors)
    _validate_scope_entitlements(cfg, errors)
    _validate_outbound_roster(cfg, errors)
    _validate_google_auth_entitlements(cfg, errors)
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
        _validate_one_persona(persona, i, seen_slugs, errors)


def _validate_one_persona(persona: Any, i: int, seen_slugs: set[str], errors: list[str]) -> None:
    prefix = f"personas[{i}]"
    if not isinstance(persona, dict):
        _err(f"{prefix}: must be a mapping", errors)
        return
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
    _validate_entitlements(persona.get("entitlements"), f"{prefix}({slug}).entitlements", errors)
    skills = persona.get("skills", []) or []
    for j, skill in enumerate(skills):
        _validate_skill(skill, f"{prefix}({slug}).skills[{j}]", errors)
    _validate_persona_cron(persona.get("cron"), skills, f"{prefix}({slug}).cron", errors)


def _validate_skill(skill: Any, sk_prefix: str, errors: list[str]) -> None:
    """Validate one persona skill under the ADR 0056 model.

    Required: name + initiation (an object carrying boolean manual/scheduled/
    webhook). version/enabled are OPTIONAL — the translator defaults them
    (version→"pending", enabled→falsy) and the console treats them as
    optional-but-typed. The retired entitlement fields (trust_ceiling,
    action_ceilings) are rejected with no shim, mirroring the console's
    LegacyEntitlementField.
    """
    if not isinstance(skill, dict):
        _err(f"{sk_prefix}: must be a mapping", errors)
        return
    if "name" not in skill:
        _err(f"{sk_prefix}: missing field name", errors)
    for legacy in LEGACY_ENTITLEMENT_FIELDS:
        if legacy in skill:
            _err(
                f"{sk_prefix}.{legacy}: retired (ADR 0056); use "
                "personas[].entitlements.exposure and skills[].initiation",
                errors,
            )
    _validate_initiation(skill.get("initiation"), f"{sk_prefix}.initiation", errors)
    if "version" in skill and not isinstance(skill["version"], str):
        _err(f"{sk_prefix}: version must be a string when present", errors)
    if "enabled" in skill and not isinstance(skill["enabled"], bool):
        _err(f"{sk_prefix}: enabled must be a boolean when present", errors)


def _validate_initiation(raw: Any, path: str, errors: list[str]) -> None:
    """Every skill declares how it may START (ADR 0056). The three flags are
    required and must be booleans; the console errors when initiation is absent
    on a skill, so the on-box validator does too."""
    if raw is None:
        _err(f"{path}: required (manual/scheduled/webhook booleans)", errors)
        return
    if not isinstance(raw, dict):
        _err(f"{path}: must be a mapping", errors)
        return
    for flag in ("manual", "scheduled", "webhook"):
        if flag not in raw:
            _err(f"{path}.{flag}: required boolean", errors)
        elif not isinstance(raw[flag], bool):
            _err(f"{path}.{flag}: must be a boolean", errors)


def _validate_entitlements(raw: Any, path: str, errors: list[str]) -> None:
    """Validate persona-level ``entitlements.exposure`` (ADR 0056).

    Absent ⇒ valid (sparse, fail-closed at runtime). Rejects the retired
    scalar fields, and validates each exposure entry: key in the authored
    action-class set (``read`` is never authored), value a legal ceiling.
    """
    if raw is None:
        return
    if not isinstance(raw, dict):
        _err(f"{path}: must be a mapping when present", errors)
        return
    for legacy in LEGACY_ENTITLEMENT_FIELDS:
        if legacy in raw:
            _err(f"{path}.{legacy}: retired (ADR 0056); use exposure", errors)
    exposure = raw.get("exposure")
    if exposure is None:
        return
    if not isinstance(exposure, dict):
        _err(f"{path}.exposure: must be a mapping when present", errors)
        return
    for key, value in exposure.items():
        ep = f"{path}.exposure.{key}"
        if key == "read":
            _err(f"{ep}: read is always allowed and must not be authored as exposure", errors)
        elif key not in AUTHORED_EXPOSURE_ACTION_CLASSES:
            _err(
                f"{ep}: exposure key must be one of {sorted(AUTHORED_EXPOSURE_ACTION_CLASSES)}",
                errors,
            )
        elif value not in ACCEPTED_CEILINGS:
            _err(f"{ep}: must be one of {sorted(ACCEPTED_CEILINGS)}", errors)
        elif value == "confirm" and key not in SEND_ACTION_CLASSES:
            # `confirm` (ADR 0071) only has defined behavior in enforce()'s send
            # branch; reject it on any non-send class so it can't be authored where
            # it does nothing.
            _err(
                f"{ep}: 'confirm' is only valid for the send classes "
                f"{sorted(SEND_ACTION_CLASSES)} (ADR 0071)",
                errors,
            )


def _validate_persona_cron(raw: Any, skills: Any, path: str, errors: list[str]) -> None:
    """A cron entry may only target a skill that grants ``initiation.scheduled``
    (ADR 0056). Mirrors the console's checkCronSkill so a cron the console would
    reject cannot land on the volume through a bypassed authoring path."""
    if raw is None:
        return
    if not isinstance(raw, list):
        _err(f"{path}: must be a list when present", errors)
        return
    scheduled_skills = _skills_granting(skills, "scheduled")
    for k, entry in enumerate(raw):
        if not isinstance(entry, dict):
            _err(f"{path}[{k}]: must be a mapping", errors)
            continue
        skill = entry.get("skill")
        if isinstance(skill, str) and skill and skill not in scheduled_skills:
            _err(
                f"{path}[{k}].skill: cron references {skill!r} but that enabled skill "
                "does not grant initiation.scheduled",
                errors,
            )


def _skills_granting(skills: Any, flag: str) -> set[str]:
    """Names of ENABLED skills whose ``initiation.<flag>`` is true."""
    out: set[str] = set()
    if not isinstance(skills, list):
        return out
    for skill in skills:
        if not isinstance(skill, dict) or not skill.get("enabled"):
            continue
        name = skill.get("name")
        initiation = skill.get("initiation")
        if (
            isinstance(name, str)
            and name
            and isinstance(initiation, dict)
            and initiation.get(flag) is True
        ):
            out.add(name)
    return out


def _validate_webhook_triggers(cfg: dict[str, Any], errors: list[str]) -> None:
    """A webhook_trigger may only target an enabled skill that grants
    ``initiation.webhook`` on the named persona (ADR 0056). Mirrors the
    console's checkWebhookTriggers initiation gate (the security-relevant
    field); the source↔connector connectivity check is left to the console's
    authoring UX, so the on-box validator is never stricter than the console
    on the fields the console blessed."""
    triggers = cfg.get("webhook_triggers")
    if triggers is None:
        return
    if not isinstance(triggers, list):
        _err(f"webhook_triggers must be a list; got {type(triggers).__name__}", errors)
        return
    webhook_skills = _webhook_skills_by_persona(cfg.get("personas"))
    for i, trig in enumerate(triggers):
        prefix = f"webhook_triggers[{i}]"
        if not isinstance(trig, dict):
            _err(f"{prefix}: must be a mapping", errors)
            continue
        persona = trig.get("persona")
        skill = trig.get("skill")
        if not isinstance(persona, str) or not isinstance(skill, str):
            continue  # missing-field shape is the console's authoring concern
        granted = webhook_skills.get(persona)
        if granted is None:
            _err(f"{prefix}.persona: {persona!r} is not a declared persona slug", errors)
        elif skill not in granted:
            _err(
                f"{prefix}.skill: {skill!r} is not an enabled skill granting "
                f"initiation.webhook on persona {persona!r}",
                errors,
            )


def _webhook_skills_by_persona(personas: Any) -> dict[str, set[str]]:
    """Map persona slug → names of enabled skills granting initiation.webhook."""
    out: dict[str, set[str]] = {}
    if not isinstance(personas, list):
        return out
    for persona in personas:
        if not isinstance(persona, dict):
            continue
        slug = persona.get("slug")
        if isinstance(slug, str) and slug:
            out[slug] = _skills_granting(persona.get("skills"), "webhook")
    return out


def _validate_scope_entitlements(cfg: dict[str, Any], errors: list[str]) -> None:
    """Reject the retired scope-level entitlement fields (ADR 0056)."""
    scope = cfg.get("scope")
    if not isinstance(scope, dict):
        return
    for legacy in LEGACY_ENTITLEMENT_FIELDS:
        if legacy in scope:
            _err(
                f"scope.{legacy}: retired (ADR 0056); exposure is authored per "
                "persona at personas[].entitlements.exposure",
                errors,
            )


def _canon_roster_address(raw: str) -> str | None:
    """Canonicalize an outbound-roster address to ``@domain`` or ``local@domain``.

    Mirrors the runtime classifier's ``_canonicalize_roster_entry`` (strict:
    lowercased, no display-name/list/whitespace, exact-domain, no plus-tag
    widening) so the validator's notion of "same address" matches the classifier's
    notion of "match". Returns ``None`` for anything malformed.
    """
    s = raw.strip().lower()
    if not s or any(ch in s for ch in ("<", ">", '"', " ", "\t", ",", ";", "\n", "\r")):
        return None
    if s.startswith("@"):
        domain = s[1:]
        labels = domain.split(".")
        if len(labels) < 2 or any(label == "" for label in labels):
            return None
        return f"@{domain}"
    if s.count("@") != 1:
        return None
    local, _, domain = s.partition("@")
    if not local or not domain:
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(label == "" for label in labels):
        return None
    return f"{local}@{domain}"


def _canonical_inbound_keys(scope: dict[str, Any]) -> set[str]:
    """Canonical keys for ``scope.inbound_allow_from`` (for the collision check)."""
    out: set[str] = set()
    raw = scope.get("inbound_allow_from")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                canon = _canon_roster_address(entry)
                if canon is not None:
                    out.add(canon)
    return out


def _validate_outbound_roster(cfg: dict[str, Any], errors: list[str]) -> None:
    """Validate ``scope.outbound_roster`` (ADR 0075).

    Each entry is an object with ``address`` (a ``local@domain`` exact address or
    an ``@domain`` grant), ``class`` in the closed set {client, records_vendor},
    and an optional ``note``. A whole-@domain grant at a public-mail provider is
    rejected (the domain is shared by millions) — but an EXACT address at such a
    domain is valid (a PI client is a consumer on gmail). A canonical address
    appearing under more than one outbound class, or also in
    ``scope.inbound_allow_from``, is rejected: a recipient has exactly one class.
    """
    scope = cfg.get("scope")
    if not isinstance(scope, dict):
        return
    raw = scope.get("outbound_roster")
    if raw is None:
        return
    if not isinstance(raw, list):
        _err(f"scope.outbound_roster must be a list; got {type(raw).__name__}", errors)
        return
    inbound_keys = _canonical_inbound_keys(scope)
    seen_class: dict[str, str] = {}
    for i, entry in enumerate(raw):
        _validate_one_outbound_entry(entry, i, inbound_keys, seen_class, errors)


def _validate_one_outbound_entry(
    entry: Any,
    i: int,
    inbound_keys: set[str],
    seen_class: dict[str, str],
    errors: list[str],
) -> None:
    prefix = f"scope.outbound_roster[{i}]"
    if not isinstance(entry, dict):
        _err(f"{prefix}: must be a mapping", errors)
        return
    address = entry.get("address")
    class_str = entry.get("class")
    note = entry.get("note")
    if not isinstance(address, str) or not address.strip():
        _err(f"{prefix}.address: required non-empty string", errors)
        return
    if not isinstance(class_str, str) or class_str not in OUTBOUND_ROSTER_CLASSES:
        _err(f"{prefix}.class: must be one of {sorted(OUTBOUND_ROSTER_CLASSES)}", errors)
        return
    if note is not None and not isinstance(note, str):
        _err(f"{prefix}.note: must be a string when present", errors)
    canon = _canon_roster_address(address)
    if canon is None:
        _err(
            f"{prefix}.address: {address!r} must be an exact address (local@domain) "
            "or an @domain grant",
            errors,
        )
        return
    if canon.startswith("@") and canon[1:] in _PUBLIC_MAIL_DOMAINS:
        _err(
            f"{prefix}.address: a whole-@domain grant at a public-mail provider "
            f"({canon[1:]}) is not allowed; author the exact address",
            errors,
        )
        return
    if canon in inbound_keys:
        _err(
            f"{prefix}.address: {canon!r} is already in scope.inbound_allow_from; a "
            "recipient cannot be both internal and a typed outbound class",
            errors,
        )
        return
    prior = seen_class.get(canon)
    if prior is not None and prior != class_str:
        _err(
            f"{prefix}.address: {canon!r} appears in more than one outbound roster "
            f"class ({prior}, {class_str})",
            errors,
        )
        return
    seen_class[canon] = class_str


def _validate_google_auth_entitlements(cfg: dict[str, Any], errors: list[str]) -> None:
    """Reject the retired managed-mailbox action_ceilings (ADR 0056)."""
    google_auth = cfg.get("google_auth")
    if not isinstance(google_auth, dict):
        return
    mailboxes = google_auth.get("managed_mailboxes")
    if not isinstance(mailboxes, list):
        return
    for i, mailbox in enumerate(mailboxes):
        if isinstance(mailbox, dict) and "action_ceilings" in mailbox:
            _err(
                f"google_auth.managed_mailboxes[{i}].action_ceilings: retired "
                "(ADR 0056); exposure is authored per persona",
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
    "AUTHORED_EXPOSURE_ACTION_CLASSES",
    "LEGACY_ENTITLEMENT_FIELDS",
    "OUTBOUND_ROSTER_CLASSES",
    "REQUIRED_TOP_LEVEL_FIELDS",
    "SEND_ACTION_CLASSES",
    "validate_customer_yaml",
]
