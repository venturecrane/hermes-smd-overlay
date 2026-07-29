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
import re
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

# ---- Email-channel seam (ADR 0078 / email-channel-seam spec D3/D5) -----------
#
# SOURCE OF TRUTH: ss-console customer-yaml/types.ts (MSGRAPH_ADAPTER,
# MSGRAPH_GUID_PATTERN, MSGRAPH_SECRET_REF_PATTERN, ACCEPTED_SEND_PROVIDERS). The
# on-box validator must accept exactly what the console accepts on these blocks
# (validator parity contract, ADR 0044); keep these aligned with the TS.
MSGRAPH_ADAPTER = "msgraph"
MSGRAPH_GUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MSGRAPH_SECRET_REF_PATTERN = re.compile(r"^fly-secret:[A-Za-z_][A-Za-z0-9_]*$")

# Provider-neutral persona send-as identity (ADR 0078 §4). send_identity.provider
# is one of these; the legacy agentmail_identity string normalizes to
# {provider: agentmail, address}. Mirrors ss-console ACCEPTED_SEND_PROVIDERS.
ACCEPTED_SEND_PROVIDERS = {"agentmail", "msgraph"}

# The Email-connector adapters that HAVE an inbound seam normalizer
# (shared.inbound_message.NORMALIZERS). Structural D3: an Email channel bound for
# inbound must be a member — "a channel that cannot be fenced cannot be bound".
EMAIL_SEAM_ADAPTERS = {"agentmail", MSGRAPH_ADAPTER}

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
    _validate_email_seam(cfg, errors)
    _validate_memory(cfg, errors)
    _validate_voice_library(cfg, errors)
    _validate_custody_guard(cfg, errors)

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


# ---- Credential-custody guard (ADR 0044 Decision 8 / ADR 0045 §7, ss #1841) ----
#
# Non-refused `code_execution` exposure lets agent-authored code read the
# gateway process environment — every raw connector/channel credential there
# is reachable, bypassing first-class tool classification entirely. The guard
# rejects a config that authors code_execution alongside gateway-held creds,
# unless each offending surface is explicitly accepted in the top-level
# `custody_exceptions` list. Eligibility is enum-limited to IDENTITY-CHANNEL
# adapters (the seat's own channels; blast radius = impersonating itself).
# Client-data adapters (smokeball, clio, microsoft-graph, ...) are NEVER
# exception-eligible: ADR 0045 — no paying client with a raw privileged
# credential reachable from the gateway. Disposition record:
# ss-console operator/contracts/connector-custody-dispositions.md.
_CUSTODY_EXCEPTION_ELIGIBLE = ("telegram", "agentmail", "brave")
# connectors{} backends whose credential lives behind the broker boundary
# (none today: Google rides the google_auth block, broker-held by
# construction, not a connectors{} backend). Grows as ADR 0045 migration
# step 7 moves connectors behind the broker.
_BROKER_MEDIATED_BACKENDS: frozenset[str] = frozenset()


def _gateway_cred_surfaces(cfg: dict[str, Any]) -> set[str]:
    """Authored surfaces that imply a raw credential in the gateway env.
    Authored-surface approximation: the live-runtime env scan (ADR 0045
    verification item 10) is the runtime backstop, not this validator."""
    surfaces: set[str] = set()
    connectors = cfg.get("connectors")
    if isinstance(connectors, dict):
        for value in connectors.values():
            if not isinstance(value, dict) or value.get("enabled") is False:
                continue
            adapter = value.get("adapter")
            backend = value.get("backend")
            if isinstance(adapter, str) and adapter:
                if not (isinstance(backend, str) and backend in _BROKER_MEDIATED_BACKENDS):
                    surfaces.add(adapter)
    telegram = cfg.get("telegram")
    # Block present and not explicitly disabled counts (fail-closed): the
    # bot token is a gateway-env credential whenever the channel is wired.
    if isinstance(telegram, dict) and telegram.get("enabled") is not False:
        surfaces.add("telegram")
    personas = cfg.get("personas")
    if isinstance(personas, list):
        for persona in personas:
            if not isinstance(persona, dict):
                continue
            send_as = persona.get("send_as")
            # A persona send_as (either the legacy agentmail_identity or the
            # provider-neutral send_identity block, ADR 0078 §4) implies the
            # AgentMail identity channel as a gateway-env credential surface —
            # mirrors the console custody guard treating a non-null send_as as an
            # 'agentmail' surface.
            if isinstance(send_as, dict) and (
                send_as.get("agentmail_identity") or send_as.get("send_identity") is not None
            ):
                surfaces.add("agentmail")
    return surfaces


def _validate_custody_guard(cfg: dict[str, Any], errors: list[str]) -> None:
    exceptions_raw = cfg.get("custody_exceptions")
    exceptions: set[str] = set()
    if exceptions_raw is not None:
        if not isinstance(exceptions_raw, list):
            _err(
                f"custody_exceptions: must be a list; got {type(exceptions_raw).__name__}",
                errors,
            )
            return
        for i, entry in enumerate(exceptions_raw):
            if not isinstance(entry, str) or entry not in _CUSTODY_EXCEPTION_ELIGIBLE:
                _err(
                    f"custody_exceptions[{i}]: {entry!r} is not exception-eligible "
                    f"(identity-channel adapters only: {sorted(_CUSTODY_EXCEPTION_ELIGIBLE)}; "
                    "client-data connectors can never be excepted — ADR 0045)",
                    errors,
                )
            elif entry in exceptions:
                _err(f"custody_exceptions[{i}]: duplicate entry {entry!r}", errors)
            else:
                exceptions.add(entry)

    personas = cfg.get("personas")
    offenders: list[str] = []
    if isinstance(personas, list):
        for persona in personas:
            if not isinstance(persona, dict):
                continue
            entitlements = persona.get("entitlements")
            exposure = entitlements.get("exposure") if isinstance(entitlements, dict) else None
            ceiling = exposure.get("code_execution") if isinstance(exposure, dict) else None
            if ceiling is not None and ceiling != "refused":
                offenders.append(str(persona.get("slug") or persona.get("name") or "?"))
    if not offenders:
        return
    uncovered = sorted(_gateway_cred_surfaces(cfg) - exceptions)
    if uncovered:
        _err(
            f"personas ({', '.join(offenders)}) author non-refused code_execution while "
            f"gateway-held credential surfaces exist without an authored custody exception: "
            f"{uncovered}. Executed code can read these credentials from the gateway env, "
            "bypassing tool classification (ADR 0044 Decision 8 / ADR 0045, ss #1841). "
            "Either author code_execution: refused, move the connector behind the broker, "
            "or accept an IDENTITY-CHANNEL surface explicitly via top-level "
            "custody_exceptions (client-data connectors are never eligible).",
            errors,
        )


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
    _validate_send_as(persona.get("send_as"), f"{prefix}.send_as", errors)
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


def _validate_send_as(raw: Any, path: str, errors: list[str]) -> None:
    """Validate a persona ``send_as`` block (ADR 0078 §4 / email-channel-seam D5).

    Mirrors ss-console ``checkSendAs``. Optional (absent ⇒ no error). Two authored
    forms: the provider-neutral ``send_identity: {provider, address}`` or the
    deprecated ``agentmail_identity: <address>`` string. Authoring BOTH is a hard
    error (ambiguous — fail closed rather than silently pick one). Missing both is
    an error when the block itself is present."""
    if raw is None:
        return
    if not isinstance(raw, dict):
        _err(f"{path}: must be an object", errors)
        return
    has_send_identity = raw.get("send_identity") is not None
    has_legacy = raw.get("agentmail_identity") is not None
    if has_send_identity and has_legacy:
        _err(
            f"{path}: sets both send_identity and the deprecated agentmail_identity — "
            "author exactly one (send_identity is preferred; agentmail_identity is "
            "back-compat only)",
            errors,
        )
        return
    if has_send_identity:
        _validate_send_identity(raw.get("send_identity"), f"{path}.send_identity", errors)
        return
    if has_legacy:
        legacy = raw.get("agentmail_identity")
        if not isinstance(legacy, str) or not legacy:
            _err(
                f"{path}.agentmail_identity: must be a non-empty string when set",
                errors,
            )
        return
    _err(
        f"{path}.send_identity: send_as requires send_identity {{provider, address}} "
        "(or legacy agentmail_identity)",
        errors,
    )


def _validate_send_identity(raw: Any, path: str, errors: list[str]) -> None:
    """Validate a ``send_identity: {provider, address}`` sub-block (ADR 0078 §4)."""
    if not isinstance(raw, dict):
        _err(f"{path}: must be an object", errors)
        return
    provider = raw.get("provider")
    if not isinstance(provider, str) or provider not in ACCEPTED_SEND_PROVIDERS:
        _err(
            f"{path}.provider: must be one of {sorted(ACCEPTED_SEND_PROVIDERS)}",
            errors,
        )
        return
    address = raw.get("address")
    if not isinstance(address, str) or not address:
        _err(f"{path}.address: must be a non-empty string", errors)


# Restrictiveness ordering for the exposure/exposure_ceiling coherence check.
# Mirrors the trust plugin's _RESTRICTIVENESS (higher == more restrictive).
_CEILING_RESTRICTIVENESS = {
    "autonomous": 0,
    "confirm": 1,
    "draft_for_review": 2,
    "refused": 3,
}


def _validate_exposure_map(
    exposure: Any, path: str, errors: list[str], *, field: str
) -> dict[str, str]:
    """Shared per-entry validation for ``exposure`` and ``exposure_ceiling``.

    Returns the valid entries (for the cross-map coherence check); invalid
    entries are reported and excluded.
    """
    out: dict[str, str] = {}
    if exposure is None:
        return out
    if not isinstance(exposure, dict):
        _err(f"{path}.{field}: must be a mapping when present", errors)
        return out
    for key, value in exposure.items():
        ep = f"{path}.{field}.{key}"
        if key == "read":
            _err(f"{ep}: read is always allowed and must not be authored as {field}", errors)
        elif key not in AUTHORED_EXPOSURE_ACTION_CLASSES:
            _err(
                f"{ep}: {field} key must be one of {sorted(AUTHORED_EXPOSURE_ACTION_CLASSES)}",
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
        else:
            out[str(key)] = str(value)
    return out


def _validate_entitlements(raw: Any, path: str, errors: list[str]) -> None:
    """Validate persona-level ``entitlements`` (ADR 0056; ss#2003 Q7).

    Absent ⇒ valid (sparse, fail-closed at runtime). Rejects the retired
    scalar fields, and validates each ``exposure`` / ``exposure_ceiling``
    entry: key in the authored action-class set (``read`` is never authored),
    value a legal ceiling. ``exposure_ceiling`` is the letter-commitment bound
    for the runtime entitlement dial: the authored ``exposure`` must sit at or
    below it (a config authoring a posture above its own ceiling is
    incoherent, not sparse).
    """
    if raw is None:
        return
    if not isinstance(raw, dict):
        _err(f"{path}: must be a mapping when present", errors)
        return
    for legacy in LEGACY_ENTITLEMENT_FIELDS:
        if legacy in raw:
            _err(f"{path}.{legacy}: retired (ADR 0056); use exposure", errors)
    exposure = _validate_exposure_map(raw.get("exposure"), path, errors, field="exposure")
    ceiling = _validate_exposure_map(
        raw.get("exposure_ceiling"), path, errors, field="exposure_ceiling"
    )
    for key, bound in ceiling.items():
        authored = exposure.get(key)
        if authored is not None and (
            _CEILING_RESTRICTIVENESS[authored] < _CEILING_RESTRICTIVENESS[bound]
        ):
            _err(
                f"{path}.exposure.{key}: authored value {authored!r} exceeds its own "
                f"exposure_ceiling {bound!r} — raise the ceiling or lower the exposure",
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
        _validate_trigger_throttle(trig.get("throttle"), prefix, errors)
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


def _validate_trigger_throttle(throttle: Any, prefix: str, errors: list[str]) -> None:
    """``throttle.cooldown_minutes`` must be a non-negative integer when
    authored (ss-console #1781). The gate's runtime resolver tolerates a
    malformed block by falling back to the platform default; the validator
    surfaces the typo at provision time instead of silently changing the
    authored intent."""
    if throttle is None:
        return
    if not isinstance(throttle, dict):
        _err(
            f"{prefix}.throttle: must be a mapping; got {type(throttle).__name__}",
            errors,
        )
        return
    for key in throttle:
        if key != "cooldown_minutes":
            _err(
                f"{prefix}.throttle.{key}: unknown throttle key (known: cooldown_minutes)",
                errors,
            )
            return
    raw = throttle.get("cooldown_minutes")
    if raw is None:
        return
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        _err(
            f"{prefix}.throttle.cooldown_minutes: must be a non-negative "
            f"integer (0 disables); got {raw!r}",
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
            continue
        _validate_msgraph_connector(key, conn, errors)


def _validate_msgraph_connector(key: str, conn: dict[str, Any], errors: list[str]) -> None:
    """Validate the msgraph-specific knobs on a connector (email-channel-seam D5).

    Mirrors ss-console ``checkMsgraph``: when ``adapter == msgraph`` the
    ``msgraph_auth`` block is REQUIRED and validated and ``poll_seconds`` (optional)
    must be a positive integer; on any other adapter BOTH must be absent — a present
    block is a hard error (no dead config)."""
    adapter = conn.get("adapter")
    raw_auth = conn.get("msgraph_auth")
    raw_poll = conn.get("poll_seconds")
    prefix = f"connectors.{key}"
    if adapter != MSGRAPH_ADAPTER:
        if raw_auth is not None:
            _err(
                f"{prefix}.msgraph_auth: only valid when adapter is {MSGRAPH_ADAPTER!r} "
                f"(adapter is {adapter!r})",
                errors,
            )
        if raw_poll is not None:
            _err(
                f"{prefix}.poll_seconds: only valid when adapter is {MSGRAPH_ADAPTER!r} "
                f"(adapter is {adapter!r})",
                errors,
            )
        return
    _validate_msgraph_auth(f"{prefix}.msgraph_auth", raw_auth, errors)
    _validate_poll_seconds(f"{prefix}.poll_seconds", raw_poll, errors)


def _validate_msgraph_auth(path: str, raw: Any, errors: list[str]) -> None:
    """Validate the required ``msgraph_auth`` block (adapter is msgraph, D5).

    Fail-closed: absent or malformed ⇒ error, never a silent default. tenant_id /
    client_id are GUIDs, mailbox is an email address, secret_ref references a
    per-seat Fly secret (``fly-secret:<ENV_NAME>`` — ADR 0010 custody, never an
    ``infisical:`` token_ref)."""
    if raw is None:
        _err(f"{path}: required when adapter is {MSGRAPH_ADAPTER!r}", errors)
        return
    if not isinstance(raw, dict):
        _err(f"{path}: must be an object", errors)
        return
    tenant_id = raw.get("tenant_id")
    if not isinstance(tenant_id, str) or not MSGRAPH_GUID_PATTERN.match(tenant_id):
        _err(f"{path}.tenant_id: must be a GUID (8-4-4-4-12 hex)", errors)
    client_id = raw.get("client_id")
    if not isinstance(client_id, str) or not MSGRAPH_GUID_PATTERN.match(client_id):
        _err(f"{path}.client_id: must be a GUID (8-4-4-4-12 hex)", errors)
    mailbox = raw.get("mailbox")
    if not isinstance(mailbox, str) or "@" not in mailbox:
        _err(f"{path}.mailbox: must be the operator mailbox email address", errors)
    secret_ref = raw.get("secret_ref")
    if not isinstance(secret_ref, str) or not MSGRAPH_SECRET_REF_PATTERN.match(secret_ref):
        _err(
            f"{path}.secret_ref: must reference a per-seat Fly secret as "
            "'fly-secret:<ENV_NAME>' (ADR 0010 custody)",
            errors,
        )


def _validate_poll_seconds(path: str, raw: Any, errors: list[str]) -> None:
    """``poll_seconds`` (adapter is msgraph) is optional; when present it must be a
    positive integer. Absent ⇒ the overlay poller applies its default cadence."""
    if raw is None:
        return
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        _err(f"{path}: must be a positive integer (seconds)", errors)


def _validate_email_seam(cfg: dict[str, Any], errors: list[str]) -> None:
    """D3 structural enforcement (ADR 0078 §3 / email-channel-seam spec): the
    inbound trust spine is the only door.

    An Email connector wired to carry inbound agent turns MUST bind an adapter
    that has a seam normalizer (``shared.inbound_message.NORMALIZERS``) — "a
    channel that cannot be fenced cannot be bound". Scope: EMAIL only (Telegram
    and other channels are tracked separately).

    Inbound-bound is the fence-relevant condition, and it IS the invariant, not a
    weakening (Captain call 2026-07-24). D3's invariant is that inbound must not
    reach the model unfenced (the F1 bypass). The ONLY push-style wake path is
    ``webhook_triggers`` — an Email connector whose adapter is named as a
    ``webhook_triggers[].source`` carries gate→router inbound, so it must be
    fenceable. An Email connector NOT wired as a trigger source is outbound-only /
    read MCP tooling: it never wakes the agent, and its tool-result reads are
    already quarantined at ``transform_tool_result``, so leaving it unfenced is
    safe. The delegated ``microsoft-graph`` adapter stays legitimate for
    read/draft tooling — ADR 0078 rejected it only as the SENSITIVE-seat
    mail-identity default, not wholesale — so outlawing it here would be wrong.

    DO NOT tighten this to "every Email adapter" without the coordinated ss-console
    change: the cross-repo parity fixtures (byte-shared, hash-pinned) bind an
    outbound-only softeria ``microsoft-graph`` Email that the console accepts, so
    full-strict enforcement requires swapping those fixtures (microsoft-graph →
    msgraph) and re-pinning the content hash in BOTH repos. That is deliberately
    deferred (slice-4/5 coordination item) — the inbound-bound scope here is the
    correct invariant in the meantime, not a placeholder to "fix" back to strict."""
    connectors = cfg.get("connectors")
    if not isinstance(connectors, dict):
        return
    email = connectors.get("Email")
    if not isinstance(email, dict) or email.get("enabled") is False:
        return
    adapter = email.get("adapter")
    if not isinstance(adapter, str) or not adapter:
        return  # adapter-shape errors are the console's authoring concern
    if adapter in EMAIL_SEAM_ADAPTERS:
        return  # bound via a seam normalizer — fenceable
    triggers = cfg.get("webhook_triggers")
    if not isinstance(triggers, list):
        return
    inbound_sources = {
        trig.get("source")
        for trig in triggers
        if isinstance(trig, dict) and isinstance(trig.get("source"), str)
    }
    if adapter in inbound_sources:
        _err(
            f"connectors.Email.adapter: {adapter!r} carries inbound (a webhook_triggers "
            f"source names it) but has no seam normalizer; an Email channel bound for "
            f"inbound must use a seam adapter {sorted(EMAIL_SEAM_ADAPTERS)} — a channel "
            "that cannot be fenced cannot be bound (ADR 0078 D3)",
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
    "ACCEPTED_SEND_PROVIDERS",
    "ACCEPTED_VERTICALS",
    "AUTHORED_EXPOSURE_ACTION_CLASSES",
    "EMAIL_SEAM_ADAPTERS",
    "LEGACY_ENTITLEMENT_FIELDS",
    "MSGRAPH_ADAPTER",
    "MSGRAPH_GUID_PATTERN",
    "MSGRAPH_SECRET_REF_PATTERN",
    "OUTBOUND_ROSTER_CLASSES",
    "REQUIRED_TOP_LEVEL_FIELDS",
    "SEND_ACTION_CLASSES",
    "validate_customer_yaml",
]
