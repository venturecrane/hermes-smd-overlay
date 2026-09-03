"""Overlay half of the escalation_ledger cross-repo sync gate (ss #2289 fix 4).

``shared/escalation_ledger.py`` is the FIFTH copy of one module. The other four
live in venturecrane/ss-console — the canonical
``operator/workspace_broker/escalation_ledger.py`` plus three vendored skill
copies — and are locked to each other, byte for byte, by
``operator/tests/test_escalation_ledger_sync.py`` there.

Why byte-identity is load-bearing rather than tidy: this copy computes the
``item_key`` the broker receives, and the console's copies compute the key
``pre_run`` joins against and the key the broker validates. If the two
derivations drift by so much as a normalization rule, the join silently returns
nothing and every downstream suppression is inert while looking healthy. That is
not hypothetical — it is what ss #2151 / overlay#239 found on the pilot seat: 160
events, 128 item states, zero of them matching any of the 37 open Smokeball
items. Fire-once and the seven-day ack snooze had never once worked.

Nothing in THIS repo checked the copy. The ss-console manifest
(``operator/contracts/overlay-pairs.json``) records an ``overlaySha256`` for it,
verified against the overlay at the PINNED ``OVERLAY_REF`` by the
``operator-substrate`` workflow — so drift is caught there, but only for the
commit a Machine already ships, and only after someone bumps the pin. An edit
landing on overlay ``main`` was unpinned until then.

WHAT THIS GATE CATCHES: an edit to ``shared/escalation_ledger.py`` made without
consciously restamping it from the ss-console canonical. It fails on the same
commit that makes the edit, in this repo's own hermetic CI.

WHAT IT DOES NOT CATCH: a restamp that records a digest which is not in fact the
ss-console canonical (this repo has no offline view of that file — the check is
that the change was DELIBERATE, not that it was correct), or the console side
moving first and leaving this copy behind. The console-side companion test
``test_overlay_copy_is_pinned_byte_identical`` covers that direction by refusing
a silent split between ``sha256`` and ``overlaySha256``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COPY = _REPO_ROOT / "shared" / "escalation_ledger.py"

# sha256 of venturecrane/ss-console::operator/workspace_broker/escalation_ledger.py
# at the commit this copy was stamped from. Updating this number is the conscious
# act: restamp the file from the canonical (never hand-edit it), then paste the
# new digest here and reference the paired ss-console PR in the commit message.
#
# 2026-08-11 (ss #2289 / overlay#254): item-key components are normalized before
# hashing and has_stable_identity takes (source_id, matter_id).
# 2026-08-31 (ledger-integrity pair): derive_state resets acked+resolved+
# handed_off symmetrically on a raise (a fresh raise re-opens the item);
# validate_append refuses a resolved/handed_off with no prior raise on the same
# item_key (RELEASE_EVENTS), and validates the optional `determination` payload
# a hold release carries on its resolved event.
# 2026-09-02 (ss#2152 / this PR): validate_append accepts an optional `acked_by`
# on an `acked` event and refuses it on every other kind. It carries the verified
# confirmer -- the firm's OWN authored users[].full_name plus the sha256 of that
# person's canonical address -- so the commitment "every confirmation is logged
# with the attorney's name" has a field to be true in. Both halves are required
# together: a name with no key is unjoinable, a key with no name cannot be
# written into a memo a human reads.
CANONICAL_SHA256 = "4f99f46510d67ee5522226e0631252d6849b8fa21eb09e96507ae7afe6b6bf9e"

CANONICAL_PATH = "operator/workspace_broker/escalation_ledger.py"
CANONICAL_REPO = "venturecrane/ss-console"


def test_copy_exists() -> None:
    assert _COPY.is_file(), f"vendored escalation_ledger.py missing at {_COPY}"


def test_copy_matches_the_pinned_canonical_digest() -> None:
    actual = hashlib.sha256(_COPY.read_bytes()).hexdigest()
    assert actual == CANONICAL_SHA256, (
        f"shared/escalation_ledger.py no longer matches its pinned canonical digest.\n"
        f"  pinned  {CANONICAL_SHA256}\n"
        f"  actual  {actual}\n"
        f"This file is a byte-identical copy of {CANONICAL_REPO}::{CANONICAL_PATH}. "
        f"It computes the item_key the broker receives, so an edit here that the "
        f"console does not also carry re-forks the ledger join (ss #2151).\n"
        f"If you meant to edit the module: change the CANONICAL first, restamp all "
        f"five copies from it, update CANONICAL_SHA256 and the ss-console manifest's "
        f"sha256/overlaySha256, and ship both PRs — console first."
    )
