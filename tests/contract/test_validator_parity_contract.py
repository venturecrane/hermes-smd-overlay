"""Cross-repo validator parity contract (ADR 0044).

The on-box Python validator (``bootstrap.validate``) and the console TS validator
(``ss-console/src/lib/operator/customer-yaml``) gate the SAME file at two points
in the apply path: the console blesses an edit at authoring time, the broker
re-validates the pulled file on-box before writing it to the volume. If they
disagree, a config the console accepted could be rejected on apply, or a danger
the console would catch could land on the Machine if its path is bypassed.

This test pins the agreement. The two repos hold ``validator_parity_fixtures.json``
with DATA-identical fixtures (each repo formats the file per its own tooling —
the console runs prettier on commit — so the raw bytes differ, but the fixture
DATA does not). Each repo's test asserts ITS validator classifies every fixture
as the manifest's ``expect``, and pins a canonical-content hash of the ``fixtures``
array (formatting-independent) that must equal the constant in the console's
``customer-yaml-parity-contract.test.ts``. A one-sided change to the fixture data
trips the hash, forcing a re-sync.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bootstrap.validate import validate_customer_yaml

_MANIFEST = Path(__file__).parent / "validator_parity_fixtures.json"

# Canonical-content hash of the `fixtures` array (sorted keys, compact separators)
# — independent of file formatting, so prettier in the console repo cannot break
# it. MUST equal PINNED_CONTENT_SHA256 in the console contract test. Update in
# BOTH repos whenever the fixture data changes.
_PINNED_CONTENT_SHA256 = "fe2b8d0186d887ff29b5cf7c497191b839daca28a9ff110fb738cf8cac6e84c2"


def _load() -> dict:
    return json.loads(_MANIFEST.read_text())


def _content_hash(manifest: dict) -> str:
    canon = json.dumps(
        manifest["fixtures"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def test_manifest_content_hash_is_pinned():
    """The cross-repo drift guard: the canonical content of the fixtures must
    match the digest shared with the console test. A one-sided edit trips this."""
    actual = _content_hash(_load())
    assert actual == _PINNED_CONTENT_SHA256, (
        "fixture data changed without updating _PINNED_CONTENT_SHA256 here AND in "
        "the console contract test — the two repos have drifted."
    )


def _fixture_ids():
    return [f["name"] for f in _load()["fixtures"]]


@pytest.mark.parametrize("fixture", _load()["fixtures"], ids=_fixture_ids())
def test_python_validator_matches_contract(fixture, tmp_path):
    """The on-box validator classifies each fixture as the manifest's expect."""
    path = tmp_path / "customer.yaml"
    path.write_text(fixture["yaml"])
    errors = validate_customer_yaml(path)
    accepted = errors == []
    if fixture["expect"] == "accept":
        assert accepted, f"{fixture['name']}: expected accept, got errors: {errors}"
    else:
        assert not accepted, f"{fixture['name']}: expected reject ({fixture['note']}), but accepted"
