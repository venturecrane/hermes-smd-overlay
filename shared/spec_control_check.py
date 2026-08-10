"""Authored-spec control self-check — is every declared spec actually installed?

ss-console #2234, the authored-spec analogue of :mod:`shared.connector_check`.
Runs INSIDE the gate's heartbeat emitter each tick: compares what the seat's
live ``customer.yaml`` DECLARES against what the root-owned manifest says is
INSTALLED, and shapes the gap into the per-property map the heartbeat ships to
the console.

WHY THIS IS NOT AT THE SEND SITE. ``shared.spec_gate`` already notices a broken
control — it writes a ``SPEC_GATE_TRIGGERED`` audit row every time one blocks or
is waived. That is a record, not an alarm: it fires only when something happens
to send, it lands in a per-seat SQLite file nobody watches, and on
``pilot-smokeball`` it accumulated for six days while the firm's mail quietly
stopped (ss-console #2228). A control's health must be reported by something
that runs whether or not the seat is busy. Hence a heartbeat check.

Design rules, inherited from ADR 0080 deliberately — an alert path that behaves
differently from the connector one is an alert path operators have to learn
twice:

* **Read-only.** Never writes config, manifest, or ledger.
* **States, not timestamps.** The console's alerter evaluates STORED values, so
  a frozen row from a dead seat cannot self-activate by wall-clock passage.
* **Absence is a hold, corruption is a page.** A seat that declares nothing →
  ``ok=True`` with an empty map: nothing to conclude, the console holds. A config
  or manifest this check cannot read → ``ok=False`` with ``entries=None``: the
  check itself is broken and the console pages ``spec_control_unprovable``
  rather than the whole class going silently dark. **The distinction is the
  point.** "The firm never installed a spec" and "this seat cannot see its spec
  tree" produce identical emptiness and want opposite responses — one is the
  firm's to fix, one is ours.
* **Both sides of the comparison ship.** Each entry carries ``declared`` and
  ``installed`` so a RECOVERED alert can say WHICH way it recovered: the spec
  arrived, or the declaration was withdrawn. A control whose all-clear also
  fires when the control is deleted is the same defect class this change exists
  to fix.

Keys are ``"<output_class>.<property>"`` — per PROPERTY, not per class, because
a seat can have ``staff.voice`` installed and ``staff.format`` missing, and
resolving one must not clear the alert on the other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from shared import spec_manifest
from shared.customer_config import CustomerConfig

logger = logging.getLogger("hermes_smd.spec_control_check")

#: The declaration value that binds a control. ``none`` is the other legal
#: authored value and means no spec is expected — not a gap.
_EXPECTED = "expected"

#: The two spec properties a class can declare. Mirrors the applier's
#: ``SPEC_PROPERTIES``; a third would need a matching manifest ``property``.
_PROPERTIES = ("voice", "format")


@dataclass(frozen=True)
class SpecControlCheck:
    """Outcome of one authored-spec control read.

    ``ok`` is the health of the CHECK ITSELF (config and manifest readable), not
    of any control. ``entries`` maps ``"<class>.<prop>"`` → payload entry;
    ``None`` when the check is broken — never emit a map you cannot trust.
    """

    ok: bool
    entries: dict[str, dict] | None


def _declared_properties() -> dict[str, list[str]] | None:
    """``{output_class: [prop, ...]}`` for every property declared ``expected``.

    ``None`` when the config cannot be resolved — unconfirmed is not "declares
    nothing". The same positively-confirm-or-stay-silent posture the gates use.
    """
    try:
        declared = CustomerConfig.from_volume().output_classes
    except Exception:  # noqa: BLE001 — unresolved config ⇒ the check is broken
        logger.debug("spec_control_check: output_classes unresolved", exc_info=True)
        return None
    if not isinstance(declared, dict):
        return None
    out: dict[str, list[str]] = {}
    for output_class, block in declared.items():
        if not isinstance(output_class, str) or not isinstance(block, dict):
            # A malformed class block is dropped, not guessed at — but it is not
            # a reason to distrust its siblings.
            continue
        props = [
            prop
            for prop in _PROPERTIES
            if str(block.get(f"{prop}_spec", "")).strip().lower() == _EXPECTED
        ]
        if props:
            out[output_class] = props
    return out


def check() -> SpecControlCheck:
    """Compare declared against installed. Never raises."""
    try:
        declared = _declared_properties()
        if declared is None:
            return SpecControlCheck(ok=False, entries=None)

        if not declared:
            # Nothing declared anywhere. A real, healthy state — most seats — and
            # distinct from "we could not look", which returns ok=False above.
            return SpecControlCheck(ok=True, entries={})

        state = spec_manifest.manifest_state()
        if state == spec_manifest.STATE_UNREADABLE:
            # This seat declares controls and cannot see its own spec tree. That
            # is OUR fault, not the firm's, and it must not be reported as a
            # missing spec — the gate makes the same distinction and refuses
            # rather than waiving on it.
            return SpecControlCheck(ok=False, entries=None)

        entries: dict[str, dict] = {}
        for output_class, props in sorted(declared.items()):
            installed_props = {
                entry.prop
                for entry in spec_manifest.entries_for_class(output_class)
                if spec_manifest.verify(entry)
            }
            for prop in props:
                entries[f"{output_class}.{prop}"] = {
                    "declared": True,
                    "installed": prop in installed_props,
                }
        return SpecControlCheck(ok=True, entries=entries)
    except Exception:  # noqa: BLE001 — a broken check pages, never goes dark
        logger.exception("spec_control_check: evaluation failed")
        return SpecControlCheck(ok=False, entries=None)


__all__ = ["SpecControlCheck", "check"]
