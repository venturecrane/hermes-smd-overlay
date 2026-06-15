"""Live-apply safety semantics — pure decision functions (ADR 0044 WS3).

These functions decide whether one ``customer.yaml`` may replace another *on a
running Machine* (the live path), and how the change is directed. They are
pure: they take two already-parsed config dicts (or scalar ceiling values) and
return a verdict. No I/O, no global state. Every function fails CLOSED — when
the shape is unexpected or a comparison is ambiguous, the safer answer is
returned (reject / not-live-writable / tightening).

Three concerns:

1. **Ceiling direction.** A trust ceiling moves along
   ``refused < draft_for_review < autonomous`` (least → most permissive).
   :func:`classify_direction` reports whether ``new`` is *tightening* (more
   restrictive), *widening* (more permissive), or *same* relative to ``old``.
   The caller branches: a tightening change must apply-or-fail-loud (a narrower
   safety posture must take effect immediately); a widening change may be
   deferred to a Captain-supervised re-provision.

2. **Floor preservation.** A vertical pack (and the content-class floors) pin
   certain action classes to a non-raisable ceiling — e.g. the law-firm pack
   floors ``external_send`` at ``draft_for_review``. :func:`floor_preserving`
   rejects any diff that would raise an effective ceiling *above* its floor.
   A live apply can never widen past a compliance floor.

3. **Live-writability.** Only an explicit allow-list of fields may change on the
   live path. Rebuild-class fields (``vertical``, ``model``, ``memory.*``,
   persona OAuth, connector backends) require a full re-provision and are
   rejected here so the live path can never silently diverge from the image the
   Machine booted. :func:`live_writable` is the allow-list; :func:`changed_paths`
   computes the diff so the caller can check each touched path.

Plus :func:`next_epoch`, the monotonic config-epoch counter stamped on each
applied config.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Any

from shared.action_classes import VERTICAL_FLOORS as _SHARED_VERTICAL_FLOORS

# ---------------------------------------------------------------------------
# Ceiling permissiveness ordering
#
# The three content classes per ADR 0035. Ordered least → most permissive:
# ``refused`` (no action) < ``draft_for_review`` (human reviews) < ``autonomous``
# (fires on its own). A higher index == MORE permissive == WIDER. Mirrors the
# string values in ``hermes-smd-trust/enforce.py`` Ceiling enum so the two
# enforcement surfaces agree on the vocabulary.
# ---------------------------------------------------------------------------

CEILING_ORDER: tuple[str, ...] = ("refused", "draft_for_review", "autonomous")
_CEILING_RANK: dict[str, int] = {name: i for i, name in enumerate(CEILING_ORDER)}


class Direction(str, enum.Enum):
    """Direction of a ceiling change from old → new."""

    TIGHTENING = "tightening"  # new is MORE restrictive (toward refused)
    WIDENING = "widening"  # new is MORE permissive (toward autonomous)
    SAME = "same"  # no change in permissiveness


class CeilingValueError(ValueError):
    """A ceiling string is not one of :data:`CEILING_ORDER`."""


def _rank(ceiling: object) -> int:
    """Permissiveness rank of a ceiling string. Fail closed on anything unknown.

    An unrecognized or non-string ceiling is treated as the *most permissive*
    value possible — ``len(CEILING_ORDER)`` — so that:

    * comparing a known ceiling *against* a garbled one reads as the garbled
      side being wider (a widening change → deferred / rejected, never silently
      applied as a tightening), and
    * a floor check sees a garbled effective ceiling as ABOVE every real floor,
      so :func:`floor_preserving` rejects it.

    Both directions land on the safe side: an unparseable ceiling never sneaks
    through as a no-op or a tightening.
    """
    if isinstance(ceiling, str):
        rank = _CEILING_RANK.get(ceiling.strip().lower())
        if rank is not None:
            return rank
    return len(CEILING_ORDER)


def classify_direction(old: object, new: object) -> Direction:
    """Classify a ceiling change ``old → new`` by permissiveness.

    Returns :attr:`Direction.TIGHTENING` when ``new`` is more restrictive than
    ``old`` (moved toward ``refused``), :attr:`Direction.WIDENING` when more
    permissive (moved toward ``autonomous``), or :attr:`Direction.SAME`.

    Unknown values fail closed via :func:`_rank` (treated as maximally
    permissive), so an unparseable ``new`` reads as widening — deferred or
    rejected by the caller, never applied as a silent tightening.
    """
    old_rank = _rank(old)
    new_rank = _rank(new)
    if new_rank < old_rank:
        return Direction.TIGHTENING
    if new_rank > old_rank:
        return Direction.WIDENING
    return Direction.SAME


# ---------------------------------------------------------------------------
# Vertical-pack compliance floors — DERIVED from the shared source of truth
#
# A floor pins an action class to a ceiling that a customer's authored config
# can only *narrow*, never raise. The live applier must additionally reject any
# diff that would raise an effective ceiling above its floor. The authoritative
# map is ``shared.action_classes.VERTICAL_FLOORS`` (string-keyed) — the SAME map
# ``hermes-smd-trust/enforce.py`` derives its runtime enum map from. Reading it
# here (rather than hand-copying) means the apply-time floor check and the live
# ceiling resolver can never disagree about which floors are in force; a new
# floor added to the shared map is honored by both without a second edit
# (derive-don't-duplicate, 2026-06-15 review of PR #81).
#
# law-firm / external-send-draft-floor → external_send pinned to
#   draft_for_review (client-/tribunal-bound mail ships under a human reviewer's
#   identity, ADR 0005). Non-raisable on the live path.
# ---------------------------------------------------------------------------


def vertical_floors(vertical: object) -> dict[str, str]:
    """Return the per-action-class compliance floors for a vertical slug.

    Reads the shared source-of-truth map (``shared.action_classes.VERTICAL_FLOORS``)
    so it tracks ``enforce.py`` automatically. Returns ``{}`` for verticals with
    no declared floor (e.g. ``mixed``). A non-string / unknown vertical yields
    ``{}`` — there is no floor to enforce, which is correct: a floor only ever
    *adds* a constraint. Returns a fresh dict so callers cannot mutate the
    shared source.
    """
    if not isinstance(vertical, str):
        return {}
    floors = _SHARED_VERTICAL_FLOORS.get(vertical.strip().lower())
    return dict(floors) if floors else {}


def _action_ceilings(cfg: object) -> dict[str, str]:
    """Extract the per-action-class ceiling map from ``scope.action_ceilings``.

    Parsed defensively: a missing / non-mapping ``scope`` or ``action_ceilings``
    yields ``{}``. Keys/values are coerced to stripped lower-case strings so the
    floor comparison is vocabulary-aligned. Non-string entries are dropped (they
    cannot name a real action class or ceiling).
    """
    if not isinstance(cfg, Mapping):
        return {}
    scope = cfg.get("scope")
    if not isinstance(scope, Mapping):
        return {}
    raw = scope.get("action_ceilings")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str):
            out[key.strip().lower()] = value.strip().lower()
    return out


def floor_preserving(old_cfg: object, new_cfg: object) -> bool:
    """True iff ``new_cfg`` does not raise any effective ceiling above its floor.

    The vertical is read from ``new_cfg`` (the config being applied governs which
    floors are in force). For each action class with a declared floor, the new
    config's authored per-action ceiling must be at or below the floor's
    permissiveness — i.e. ``rank(authored) <= rank(floor)``. A config that
    authors ``external_send: autonomous`` on a law-firm Machine is *not*
    floor-preserving and must be rejected on the live path.

    ``old_cfg`` is accepted for symmetry and future cross-config invariants; the
    floor check itself only needs the config being applied. Fails CLOSED: any
    unexpected shape or unparseable ceiling reads as a violation (returns
    ``False``) rather than waving the change through.
    """
    if not isinstance(new_cfg, Mapping):
        return False

    floors = vertical_floors(new_cfg.get("vertical"))
    if not floors:
        # No declared floor for this vertical — nothing to preserve.
        return True

    authored = _action_ceilings(new_cfg)
    for action_class, floor in floors.items():
        floor_rank = _rank(floor)
        authored_value = authored.get(action_class)
        if authored_value is None:
            # Unauthored: the runtime applies the floor itself (the pack floor is
            # the effective ceiling). That is floor-preserving by construction —
            # the customer never raised above it.
            continue
        if _rank(authored_value) > floor_rank:
            # Authored ABOVE the floor — a live apply would widen past a
            # compliance floor. Reject.
            return False
    return True


# ---------------------------------------------------------------------------
# Live-writability allow-list
#
# Only these top-level fields / sub-trees may change on the LIVE path (applied
# to a running Machine with no reboot). Everything else is rebuild-class: it
# changes the image the Machine booted (a new persona's OAuth scopes, a swapped
# connector backend, the model, the memory bindings) and must go through a
# Captain-supervised re-provision. The live applier REJECTS a diff that touches
# any non-allow-listed path so the running Machine can never silently diverge
# from its provisioned image.
#
# An entry is a dotted path PREFIX. ``persona`` writability is expressed at the
# leaf grain (``personas[].skills[].enabled`` / ``.trust_ceiling``) because a
# persona's OAuth and identity are NOT live-writable; see
# ``_NEVER_LIVE_WRITABLE`` for the rebuild-class persona leaves.
# ---------------------------------------------------------------------------

_LIVE_WRITABLE_PREFIXES: tuple[str, ...] = (
    "scope.trust_ceiling",
    "scope.action_ceilings",
    "escalation",
    "webhook_triggers",
    "demo",
    # Persona skill enablement + per-skill trust ceiling are live-writable. The
    # array index segment (``personas.0.skills.3.enabled``) is normalized to a
    # wildcard before this prefix match — see ``_normalize_path``.
    "personas.*.skills.*.enabled",
    "personas.*.skills.*.trust_ceiling",
)

# Rebuild-class paths that must NEVER apply live, even if a future allow-list
# entry would otherwise cover them. Checked FIRST so an over-broad allow-list
# prefix can never accidentally admit a rebuild-class change. These are the
# fields whose change alters the booted image (ADR 0019 structural changes).
_NEVER_LIVE_WRITABLE_PREFIXES: tuple[str, ...] = (
    "vertical",
    "model",
    "memory",
    "hermes_ref",
    "customer_id",
    "fly_region",
    "connectors",  # connector backends — swap = re-provision
    # Persona OAuth / identity / roster are rebuild-class. The live-writable
    # carve-outs above are the ONLY persona leaves that may move.
    "personas.*.google_auth",
    "personas.*.oauth",
    "personas.*.slug",
    "personas.*.status",
)


def _normalize_path(field_path: str) -> str:
    """Replace numeric array-index segments with ``*`` for prefix matching.

    ``personas.0.skills.3.enabled`` → ``personas.*.skills.*.enabled``. Keeps the
    allow-list free of concrete indices so it matches any element. Non-numeric
    segments are preserved verbatim.
    """
    parts = field_path.split(".")
    return ".".join("*" if part.isdigit() else part for part in parts)


def _matches_prefix(normalized: str, prefix: str) -> bool:
    """True iff ``normalized`` equals ``prefix`` or is a child path under it.

    A child match requires a segment boundary: ``escalation`` matches
    ``escalation`` and ``escalation.recipients`` but NOT ``escalation_extra``.
    Both sides are already normalized (array indices → ``*``).
    """
    if normalized == prefix:
        return True
    return normalized.startswith(prefix + ".")


def live_writable(field_path: object) -> bool:
    """True iff ``field_path`` may be changed on the live path (allow-list).

    ``field_path`` is a dotted path into the config (e.g.
    ``scope.trust_ceiling``, ``personas.0.skills.2.enabled``). Array indices are
    normalized to ``*`` before matching. A rebuild-class path
    (``vertical``, ``model``, ``memory.*``, persona OAuth, connector backends) is
    rejected even if an allow-list prefix would otherwise cover it — the
    never-list wins.

    Fails CLOSED: a non-string or empty path, or one not on the allow-list,
    returns ``False``. The live applier treats a touched non-writable path as a
    reason to reject the whole diff (it must go through re-provision).
    """
    if not isinstance(field_path, str) or not field_path.strip():
        return False
    normalized = _normalize_path(field_path.strip())

    # Never-list wins: a rebuild-class path is never live-writable.
    for prefix in _NEVER_LIVE_WRITABLE_PREFIXES:
        if _matches_prefix(normalized, prefix):
            return False

    for prefix in _LIVE_WRITABLE_PREFIXES:
        if _matches_prefix(normalized, prefix):
            return True
    return False


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------


def changed_paths(old: object, new: object, _prefix: str = "") -> list[str]:
    """Return the dotted paths whose value differs between ``old`` and ``new``.

    Recurses into nested mappings and lists. A path is reported at the SHALLOWEST
    level where the two sides diverge in shape (e.g. a mapping replaced by a
    scalar reports the parent path, not its former children). List elements are
    addressed by index (``personas.0``); a length change reports the index that
    appears in only one side.

    Used by the live applier to enumerate every touched path so each can be
    checked against :func:`live_writable`. Pure and deterministic (sorted keys).
    """
    if _same_value(old, new):
        return []

    if isinstance(old, Mapping) and isinstance(new, Mapping):
        return _diff_mappings(old, new, _prefix)
    if isinstance(old, list) and isinstance(new, list):
        return _diff_lists(old, new, _prefix)

    # Differing scalars, or a type change (mapping↔scalar, list↔mapping, …):
    # report at this level. An empty prefix means the documents differ at the
    # root — report ``"."`` so the caller sees a concrete (non-writable) path.
    return [_prefix or "."]


def _same_value(old: object, new: object) -> bool:
    """Structural equality that treats two mappings/lists as equal only when
    their normalized contents match. Falls back to ``==`` for scalars."""
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        return not _diff_mappings(old, new, "")
    if isinstance(old, list) and isinstance(new, list):
        return not _diff_lists(old, new, "")
    if isinstance(old, Mapping) or isinstance(new, Mapping):
        return False
    if isinstance(old, list) or isinstance(new, list):
        return False
    return old == new


def _join(prefix: str, segment: str) -> str:
    return f"{prefix}.{segment}" if prefix else segment


def _diff_mappings(old: Mapping[Any, Any], new: Mapping[Any, Any], prefix: str) -> list[str]:
    paths: list[str] = []
    for key in sorted(set(old) | set(new), key=str):
        seg = _join(prefix, str(key))
        if key not in old or key not in new:
            paths.append(seg)
            continue
        paths.extend(changed_paths(old[key], new[key], seg))
    return paths


def _diff_lists(old: list[Any], new: list[Any], prefix: str) -> list[str]:
    paths: list[str] = []
    for i in range(max(len(old), len(new))):
        seg = _join(prefix, str(i))
        if i >= len(old) or i >= len(new):
            paths.append(seg)
            continue
        paths.extend(changed_paths(old[i], new[i], seg))
    return paths


def non_live_writable_changes(old_cfg: object, new_cfg: object) -> list[str]:
    """Return the changed paths that are NOT live-writable (rebuild-class).

    An empty list means every diff between ``old_cfg`` and ``new_cfg`` is on the
    live-writable allow-list and the change may apply to the running Machine. A
    non-empty list is the set of rebuild-class paths that force the change
    through a re-provision instead — the live applier rejects on a non-empty
    result. Deterministic, sorted.
    """
    return sorted(p for p in changed_paths(old_cfg, new_cfg) if not live_writable(p))


# ---------------------------------------------------------------------------
# Config epoch
# ---------------------------------------------------------------------------


def next_epoch(prev: object) -> int:
    """Return the next monotonic config-epoch counter.

    Each applied config is stamped with an epoch one greater than the last. A
    missing / non-integer / negative previous value resets the floor to ``0`` so
    the first apply stamps ``1`` and the counter only ever increases. A
    boolean is explicitly rejected (``True`` is an ``int`` subclass but is never
    a real epoch) and treated as absent.
    """
    if isinstance(prev, bool):
        prev = None
    if isinstance(prev, int) and prev >= 0:
        return prev + 1
    return 1


__all__ = [
    "CEILING_ORDER",
    "CeilingValueError",
    "Direction",
    "changed_paths",
    "classify_direction",
    "floor_preserving",
    "live_writable",
    "next_epoch",
    "non_live_writable_changes",
    "vertical_floors",
]
