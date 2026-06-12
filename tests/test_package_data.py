"""Distribution-level checks for runtime data files.

The wheel is built ONCE per module (setuptools' build_meta keeps global build
state, so building twice in one process is unreliable) and both data-file
assertions read its namelist.
"""

import zipfile
from pathlib import Path

import pytest
from setuptools import build_meta


@pytest.fixture(scope="module")
def wheel_namelist(tmp_path_factory) -> list[str]:
    out = tmp_path_factory.mktemp("wheel")
    wheel_name = build_meta.build_wheel(str(out))
    with zipfile.ZipFile(Path(out) / wheel_name) as wheel:
        return wheel.namelist()


def test_wheel_contains_fabrication_marker_registry(wheel_namelist: list[str]) -> None:
    assert "shared/fabrication_markers.json" in wheel_namelist


def test_wheel_contains_consumes_contract(wheel_namelist: list[str]) -> None:
    """consumes.yaml MUST ship in the wheel.

    The env-presence allow-list (config snapshot) and the boot-time conformance
    WARN both read it at runtime via importlib.resources; if it stops shipping,
    those degrade silently on every Machine. Permanent guard for that regression.
    """
    assert "contracts/consumes.yaml" in wheel_namelist
