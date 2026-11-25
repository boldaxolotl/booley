"""Tests for booley.runtime.paths — package data resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.core import resources
from booley.runtime import paths
from tests.host_platform_fake import use_sys_platform


@pytest.mark.parametrize("case", ["installed", "shadow-with-fallback", "shadow-without-fallback"])
def test_package_data_dir_prefers_the_installed_package_with_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
):
    package = tmp_path / ("installed" if case == "installed" else "shadow")
    if case == "installed":
        (package / "data" / "refs").mkdir(parents=True)
    if case == "shadow-without-fallback":
        # No refs next to resources.py either, so the __file__-relative fallback fails too.
        monkeypatch.setattr(
            resources, "__file__", str(tmp_path / "missing" / "core" / "resources.py")
        )
    # importlib.resources decides which installed package "booley" names; no public input
    # points it at a shadow package.
    monkeypatch.setattr(resources, "files", lambda _name: package)

    result = paths.package_data_dir()

    assert result == resources.package_data_dir()
    if case == "shadow-with-fallback":
        # The source package's data: it holds refs and lies outside the shadow.
        assert (result / "refs").is_dir()
        assert not result.resolve().is_relative_to(tmp_path.resolve())
    else:
        assert result == package / "data"


@pytest.mark.parametrize(
    ("accessor", "relative"),
    [
        (paths.refs_dir, "refs"),
        (paths.user_docs_dir, "docs"),
        (paths.troubleshooting_path, "docs/TROUBLESHOOTING.md"),
        (paths.changelog_path, "refs/CHANGELOG.md"),
        (paths.docker_data_dir, "docker"),
        (paths.skills_dir, "skills"),
        (paths.cheatsheet_path, "cheatsheet.md"),
    ],
)
def test_accessors_name_the_wheel_layout_under_the_package_data(accessor, relative: str):
    assert accessor() == paths.package_data_dir() / relative


@pytest.mark.parametrize(("windows", "name"), [(True, "bwave.exe"), (False, "bwave")])
def test_bwave_binary_suffix_follows_the_platform(
    monkeypatch: pytest.MonkeyPatch, windows: bool, name: str
):
    use_sys_platform(monkeypatch, windows=windows)
    assert paths.bwave_binary() == paths.package_data_dir() / "bin" / name
