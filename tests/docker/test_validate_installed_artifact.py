"""Tests for the installed-wheel validator used by image builds."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

VALIDATOR = Path(__file__).parents[2] / ".github/scripts/validate_installed_artifact.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_installed_artifact", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_entry_point_uses_active_install_scheme(monkeypatch, tmp_path):
    validator = _load_validator()
    scripts = tmp_path / "local-bin"
    monkeypatch.setattr(validator.sysconfig, "get_path", lambda name: str(scripts))

    assert validator._entry_point_executable("booley") == scripts / "booley"


def test_invalid_wheel_metadata_raises_explicit_validation_error(tmp_path):
    validator = _load_validator()
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("first.dist-info/METADATA", "Version: 1\n")
        archive.writestr("second.dist-info/METADATA", "Version: 1\n")

    with pytest.raises(validator.ArtifactValidationError, match="2 METADATA files"):
        validator._assert_wheel_matches_install(wheel, set())
