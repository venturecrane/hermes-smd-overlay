"""Distribution-level checks for runtime data files."""

import zipfile
from pathlib import Path

from setuptools import build_meta


def test_wheel_contains_fabrication_marker_registry(tmp_path: Path) -> None:
    wheel_name = build_meta.build_wheel(str(tmp_path))
    with zipfile.ZipFile(tmp_path / wheel_name) as wheel:
        assert "shared/fabrication_markers.json" in wheel.namelist()
