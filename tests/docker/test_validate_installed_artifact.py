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


def test_worktree_script_ships_in_the_distribution():
    """The runtime worktree script is package data and an asserted wheel resource.

    Ticket setup and `booley worktree new` resolve it relative to the installed
    package; a wheel without it blocks both at runtime.
    """
    import fnmatch
    import tomllib

    from booley.runtime.paths import worktree_create_script

    resource = "booley/runtime/worktree_create.sh"
    validator = _load_validator()
    assert resource in validator.EXPECTED_RESOURCES

    pyproject = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text("utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["booley"]
    relative = resource.removeprefix("booley/")
    assert any(fnmatch.fnmatch(relative, pattern) for pattern in package_data)

    script = worktree_create_script()
    assert script.is_file()
    assert script.as_posix().endswith(resource)
