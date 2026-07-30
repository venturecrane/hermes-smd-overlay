"""customer.yaml loader.

Reads the authored ``customer.yaml`` from the Fly volume (typically
``/opt/data/customer.yaml``) and exposes typed accessors. The bootstrap
CLI translates this into per-profile Hermes config (see
``bootstrap/translate.py``); plugins at runtime consume the same
authored file directly through this loader.

Structural-vs-non-structural change rule (ADR 0019)
---------------------------------------------------
A ``customer.yaml`` field is **structural** when changing it requires
the Machine to re-provision: adding or removing a persona, swapping a
connector backend, adding or revoking an OAuth scope, changing the
trust ceiling schema. Structural changes go through Captain
re-provision — the bootstrap CLI rewrites profile directories and the
Machine restarts.

A field is **non-structural** when it can be hot-reloaded: tone
tweaks, review thresholds, voice samples, skill pin bumps within the
same catalog, content policy adjustments. The customer-sync sidecar
polls R2 for these and signals SIGHUP to reload without restart.

This loader does not differentiate at read time — it surfaces the full
authored shape. The sidecar's diff logic compares two ``CustomerConfig``
instances field-by-field to decide whether a change is structural.

Ported from ``ss-console/operator/adapter/validate_customer_yaml.py``;
the validation logic itself lives in ``bootstrap/validate.py`` so the
bootstrap CLI can validate before translation. The runtime loader here
parses the YAML and exposes accessors; structural validation is the
bootstrap CLI's responsibility (translation refuses to run against an
invalid file). Callers that want validation in-process should import
``bootstrap.validate.validate_customer_yaml`` directly — the dependency
edge runs ``bootstrap -> shared``, not the reverse, to keep the shared
package free of bootstrap imports.
"""

import logging
import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "PyYAML is required by shared.customer_config; install with `pip install pyyaml`"
    ) from exc

logger = logging.getLogger(__name__)


DEFAULT_VOLUME_PATH = "/opt/data/customer.yaml"


def _clean_str_list(value: Any) -> list[str]:
    """Coerce an unknown to a list of non-empty strings, dropping anything else.

    Used to normalize ``relationship.people[].prefers``/``avoid`` for the
    ``config_export`` seam (ADR 0048) — never trust the authored shape blindly.
    """
    if not isinstance(value, list):
        return []
    return [s for s in value if isinstance(s, str) and s]


class CustomerConfigError(ValueError):
    """Raised when ``customer.yaml`` is missing, unparseable, or invalid."""


class CustomerConfigMissingError(CustomerConfigError):
    """Raised when ``customer.yaml`` does not exist on the volume.

    Distinct from the parent so enforcement-path callers can tell the
    benign absent-file state (dev / test boxes with no provisioned
    volume — fall through to env overrides) apart from a fault on a
    provisioned Machine (unreadable / unparseable / empty file — which
    must propagate so the trust gate fails CLOSED, never silently
    downgrading an authored ceiling; 2026-06-12 code review)."""


class CustomerConfig:
    """In-memory view of an authored ``customer.yaml``.

    The class wraps the parsed YAML document and exposes typed
    accessors for the fields plugins consume at runtime. Construction
    parses the YAML and asserts the root is a mapping; structural
    schema validation is performed by
    :func:`bootstrap.validate.validate_customer_yaml` (the dependency
    edge runs ``bootstrap -> shared``, never the reverse). Accessors
    that touch missing required fields raise
    :class:`CustomerConfigError` so a malformed file is caught the
    first time a plugin reaches for a field it needs.

    The instance is read-only by convention. Mutations should go through
    the authored source (R2 → volume → :meth:`from_volume`) so the audit
    trail is preserved.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        """Construct from an already-parsed dict.

        Most callers should use :meth:`from_volume` instead. This
        constructor is the seam tests use to inject synthetic
        documents without touching the filesystem.
        """
        if not isinstance(data, dict):
            raise CustomerConfigError(
                f"customer.yaml root must be a mapping; got {type(data).__name__}"
            )
        self._data = data

    @classmethod
    def from_volume(cls, path: str | None = None) -> "CustomerConfig":
        """Load a customer config from the Fly volume.

        Args:
            path: Absolute path to ``customer.yaml`` on the Machine's
                volume. When ``None`` (the default), the path is resolved
                at call time from the ``SMD_CUSTOMER_YAML_PATH`` environment
                variable, falling back to ``/opt/data/customer.yaml``.

                This indirection is the keystone config-isolation seam: the
                boot path relocates the live ``customer.yaml`` off the
                agent-writable ``/opt/data`` volume into a root-owned
                directory (read-only to the hermes uid) and points every
                reader here via the env var, so the agent can no longer
                rewrite its own trust ceiling / vertical floor.

        Returns:
            A parsed and validated :class:`CustomerConfig`.

        Raises:
            CustomerConfigError: If the file is missing, unparseable,
                or fails schema validation.
        """
        if path is None:
            path = os.environ.get("SMD_CUSTOMER_YAML_PATH") or DEFAULT_VOLUME_PATH
        file_path = Path(path)
        if not file_path.exists():
            raise CustomerConfigMissingError(f"customer.yaml not found at {path}")
        try:
            with file_path.open() as handle:
                data = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise CustomerConfigError(f"customer.yaml at {path} is not valid YAML: {exc}") from exc
        if data is None:
            raise CustomerConfigError(f"customer.yaml at {path} is empty")
        return cls(data)

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def slug(self) -> str:
        """Return the customer slug (``customer_id``).

        Raises:
            CustomerConfigError: If ``customer_id`` is missing.
        """
        value = self._data.get("customer_id")
        if not value:
            raise CustomerConfigError("customer.yaml: customer_id is missing")
        return str(value)

    @property
    def customer_name(self) -> str:
        """Return the human-readable customer name."""
        return str(self._data.get("customer_name", ""))

    @property
    def vertical(self) -> str:
        """Return the vertical slug (e.g. ``law-firm``)."""
        return str(self._data.get("vertical", ""))

    # ------------------------------------------------------------------
    # Personas
    # ------------------------------------------------------------------

    @property
    def personas(self) -> list[dict[str, Any]]:
        """Return the list of authored personas.

        Each entry is the raw persona mapping from ``customer.yaml``
        (``slug``, ``name``, ``status``, ``title``, ``tone``,
        ``skills``, ...). Returns an empty list if no personas are
        authored — the bootstrap validator catches that separately as
        a structural error.
        """
        raw = self._data.get("personas") or []
        if not isinstance(raw, list):
            raise CustomerConfigError(
                f"customer.yaml: personas must be a list; got {type(raw).__name__}"
            )
        return list(raw)

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    @property
    def scope(self) -> dict[str, Any]:
        """Return the scope mapping (visible/blind folders, blocks, ...)."""
        raw = self._data.get("scope") or {}
        if not isinstance(raw, dict):
            raise CustomerConfigError(
                f"customer.yaml: scope must be a mapping; got {type(raw).__name__}"
            )
        return dict(raw)

    # ------------------------------------------------------------------
    # Connectors
    # ------------------------------------------------------------------

    @property
    def connectors(self) -> dict[str, dict[str, Any]]:
        """Return the connectors mapping (one entry per capability).

        Each value is the raw connector record from ``customer.yaml``
        (``adapter``, ``backend``, ``enabled``, optional configuration).
        The ``backend`` prefix (``mcp:``, ``build:``, ``synthetic:``)
        dictates how the runtime wires the connector; see ADR 0020.
        """
        raw = self._data.get("connectors") or {}
        if not isinstance(raw, dict):
            raise CustomerConfigError(
                f"customer.yaml: connectors must be a mapping; got {type(raw).__name__}"
            )
        return {str(k): dict(v) if isinstance(v, dict) else {} for k, v in raw.items()}

    # ------------------------------------------------------------------
    # Voice library
    # ------------------------------------------------------------------

    @property
    def voice_library(self) -> dict[str, Any]:
        """Return the voice library mapping (samples path, etc.)."""
        raw = self._data.get("voice_library") or {}
        if not isinstance(raw, dict):
            raise CustomerConfigError(
                f"customer.yaml: voice_library must be a mapping; got {type(raw).__name__}"
            )
        return dict(raw)

    # ------------------------------------------------------------------
    # Roster — the organization's people (scope.inbound_allow_from)
    # ------------------------------------------------------------------

    @property
    def inbound_roster(self) -> list[str]:
        """Return the organization roster — ``scope.inbound_allow_from`` — normalized.

        The roster is the set of correspondents the Operator converses with as a
        colleague (ADR 0055): exact addresses and/or ``@domain`` entries. Entries
        are lowercased + stripped; non-string / empty entries are dropped. Absent
        or empty ⇒ ``[]`` — fail-closed: the Operator reads and drafts but never
        autonomously replies to anyone until a roster is authored.
        """
        raw = self.scope.get("inbound_allow_from") or []
        if not isinstance(raw, list):
            raise CustomerConfigError(
                f"customer.yaml: scope.inbound_allow_from must be a list; got {type(raw).__name__}"
            )
        out: list[str] = []
        for entry in raw:
            if isinstance(entry, str):
                norm = entry.strip().lower()
                if norm:
                    out.append(norm)
        return out

    @property
    def outbound_roster(self) -> list[tuple[str, str]]:
        """Return the typed outbound roster — ``scope.outbound_roster`` — normalized.

        Each authored entry is ``{address, class, note?}`` where ``class`` is the
        closed vocabulary ``client`` / ``records_vendor`` (ADR 0075). Returns a list
        of ``(address, class)`` tuples with the address lowercased + stripped;
        entries that are not mappings, are missing ``address``/``class``, or carry a
        ``class`` outside the closed set are DROPPED (never guessed). Absent or empty
        ⇒ ``[]`` — fail-closed: with no typed roster every outside send stays on the
        outside ``external_send`` ceiling, exactly as before this block existed. This
        list is human-authored OUTBOUND authorization; it is never grown from inbound.
        """
        raw = self.scope.get("outbound_roster") or []
        if not isinstance(raw, list):
            raise CustomerConfigError(
                f"customer.yaml: scope.outbound_roster must be a list; got {type(raw).__name__}"
            )
        out: list[tuple[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            address = entry.get("address")
            class_str = entry.get("class")
            if not isinstance(address, str) or not isinstance(class_str, str):
                continue
            norm_addr = address.strip().lower()
            norm_class = class_str.strip().lower()
            if not norm_addr or norm_class not in ("client", "records_vendor"):
                continue
            out.append((norm_addr, norm_class))
        return out

    def sender_on_roster(self, sender_address: object) -> bool:
        """True iff ``sender_address`` is on the organization roster (ADR 0055).

        A sender matches when their full address (normalized lowercase) equals a
        roster entry exactly, OR a roster entry begins with ``@`` and the
        sender's domain matches it exactly. Mirrors the email-reply skill's
        allow-list matching. Fail-closed: an empty roster matches no one, so the
        Operator drafts rather than autonomously replying. Roster membership is
        the authorization to respond; the reply path still recipient-locks to the
        verified inbound sender independently.
        """
        if not isinstance(sender_address, str):
            return False
        addr = sender_address.strip().lower()
        if not addr:
            return False
        roster = self.inbound_roster
        if not roster:
            return False
        domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
        for entry in roster:
            if entry.startswith("@"):
                if domain and entry == f"@{domain}":
                    return True
            elif entry == addr:
                return True
        return False

    # ------------------------------------------------------------------
    # Live-read config blocks (ADR 0044 — read fresh per use, no restart)
    # ------------------------------------------------------------------

    @property
    def escalation(self) -> dict[str, Any]:
        """Return the ``escalation`` mapping (red-flag / failure recipients).

        Read live so that changing who an operator escalates to applies on the
        next action without a restart (ADR 0044). Skills that escalate should
        read this via ``from_volume().escalation`` at decision time rather than
        binding recipients at register/boot. Absent ⇒ ``{}``.
        """
        raw = self._data.get("escalation") or {}
        if not isinstance(raw, dict):
            raise CustomerConfigError(
                f"customer.yaml: escalation must be a mapping; got {type(raw).__name__}"
            )
        return dict(raw)

    @property
    def send_policy(self) -> dict[str, Any]:
        """Return the ``send_policy`` mapping (reply-channel send caps, #2070).

        Read live per call so authoring the policy applies on the next reply
        without a restart (ADR 0044). Resolution and fail-closed defaulting
        live in ``shared.send_policy.resolve_send_policy`` — this accessor only
        guards the mapping shape. Absent ⇒ ``{}``.
        """
        raw = self._data.get("send_policy") or {}
        if not isinstance(raw, dict):
            raise CustomerConfigError(
                f"customer.yaml: send_policy must be a mapping; got {type(raw).__name__}"
            )
        return dict(raw)

    @property
    def sticky_stop(self) -> dict[str, Any]:
        """Return the ``safety.sticky_stop`` mapping (ADR 0062 cost breaker).

        Authored keys (both optional; platform defaults apply when absent —
        these are integrity controls per ADR 0035, so unauthored means the
        default, never fail-open): ``cost_cap_daily_cents`` (job-path daily
        spend ladder base, default 5000) and ``inbound_daily_cap`` (webhook-
        gate routed-wake cap, default 200). Absent ⇒ ``{}``.
        """
        safety = self._data.get("safety") or {}
        if not isinstance(safety, dict):
            raise CustomerConfigError(
                f"customer.yaml: safety must be a mapping; got {type(safety).__name__}"
            )
        raw = safety.get("sticky_stop") or {}
        if not isinstance(raw, dict):
            raise CustomerConfigError(
                f"customer.yaml: safety.sticky_stop must be a mapping; got {type(raw).__name__}"
            )
        return dict(raw)

    @property
    def memory(self) -> dict[str, Any]:
        """Return the ``memory`` mapping (d1_namespace, r2_vault_path, index).

        Memory bindings are structural (rebuild-class, ADR 0044) — this accessor
        exists so the broker/console can read the authored values, not so they
        can be hot-swapped. Absent ⇒ ``{}``.
        """
        raw = self._data.get("memory") or {}
        if not isinstance(raw, dict):
            raise CustomerConfigError(
                f"customer.yaml: memory must be a mapping; got {type(raw).__name__}"
            )
        return dict(raw)

    @property
    def google_auth(self) -> dict[str, Any]:
        """Return the ``google_auth`` mapping (mode, subject, scopes, managed mailboxes).

        Exposed so the broker can live-check authored managed-mailbox / send-as
        allowlists. Absent ⇒ ``{}``.
        """
        raw = self._data.get("google_auth") or {}
        if not isinstance(raw, dict):
            raise CustomerConfigError(
                f"customer.yaml: google_auth must be a mapping; got {type(raw).__name__}"
            )
        return dict(raw)

    @property
    def telegram(self) -> dict[str, Any]:
        """Return the ``telegram`` mapping (enabled, allow_from, require_mention).

        The numeric allow-list lives behind a Hermes-core platform binding that
        loads at gateway start; this accessor exposes the authored values for
        the broker/console. Absent ⇒ ``{}``.
        """
        raw = self._data.get("telegram") or {}
        if not isinstance(raw, dict):
            raise CustomerConfigError(
                f"customer.yaml: telegram must be a mapping; got {type(raw).__name__}"
            )
        return dict(raw)

    # ------------------------------------------------------------------
    # Relationship — authored behavioral lane (ADR 0048)
    # ------------------------------------------------------------------

    @property
    def relationship(self) -> dict[str, Any]:
        """Return the ``relationship`` mapping (authored behavioral lane).

        Per-person standing working preferences (ADR 0048). Absent ⇒ ``{}``.
        Informational only — these shape how the Operator drafts/helps and never
        grant capability (entitlements live in ``scope``/``escalation``).
        """
        raw = self._data.get("relationship") or {}
        if not isinstance(raw, dict):
            raise CustomerConfigError(
                f"customer.yaml: relationship must be a mapping; got {type(raw).__name__}"
            )
        return dict(raw)

    def relationship_people(self) -> list[dict[str, Any]]:
        """Normalized, allow-listed per-person preferences for the surface.

        Returns ONLY the closed-set fields (``id``, ``name``, ``role``,
        ``prefers``, ``avoid``); any other key authored on a person is dropped,
        so the ``config_export`` seam can never surface an unexpected field
        (secret-safe by construction — the block carries no secrets, and this
        keeps it that way even if the authored shape drifts). Malformed entries
        (missing id/name, wrong types) are skipped rather than half-rendered —
        same defensive posture as the console-side parser.
        """
        people = self.relationship.get("people")
        if not isinstance(people, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in people:
            if not isinstance(entry, dict):
                continue
            pid = entry.get("id")
            name = entry.get("name")
            if not isinstance(pid, str) or not pid:
                continue
            if not isinstance(name, str) or not name:
                continue
            role = entry.get("role")
            out.append(
                {
                    "id": pid,
                    "name": name,
                    "role": role if isinstance(role, str) and role else None,
                    "prefers": _clean_str_list(entry.get("prefers")),
                    "avoid": _clean_str_list(entry.get("avoid")),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Escape hatch
    # ------------------------------------------------------------------

    @property
    def raw(self) -> dict[str, Any]:
        """Return the underlying parsed document.

        Use sparingly. Most consumers should prefer typed accessors so
        the schema's structural surface is grep-able.
        """
        return dict(self._data)


__all__ = [
    "DEFAULT_VOLUME_PATH",
    "CustomerConfig",
    "CustomerConfigError",
    "CustomerConfigMissingError",
]
