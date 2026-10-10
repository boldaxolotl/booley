"""The image alias is the only prefix exempted from report nofollow guards."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.conftest import require_symlinks, symlink_or_skip

from booley.runtime import sandbox_layout


def configure_project_alias(monkeypatch, alias, target, *, owner=0, parent_mode=0o755):
    """Use real links; substitute only image ownership/protection metadata."""
    if os.name != "posix":
        pytest.skip("authenticated image alias is POSIX-only")
    monkeypatch.setattr(sandbox_layout, "PROJECT_DIR_TARGET", str(alias))
    monkeypatch.setattr(sandbox_layout, "PROJECT_ALIAS_TARGET", str(target))
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path not in (alias, alias.parent):
            return info
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        fields["st_uid"] = owner
        if path == alias.parent:
            fields["st_mode"] = stat.S_IFDIR | parent_mode
        return SimpleNamespace(**fields)

    monkeypatch.setattr(Path, "lstat", lstat)


@pytest.fixture
def project_alias(tmp_path, monkeypatch):
    require_symlinks(tmp_path)
    target = tmp_path / "real-project-data"
    target.mkdir()
    alias = tmp_path / "booley-project"
    symlink_or_skip(alias, target, target_is_directory=True)
    configure_project_alias(monkeypatch, alias, target)
    return alias, target


def test_trusted_alias_preserves_suffix_and_missing_descendants(project_alias):
    alias, target = project_alias
    assert (
        sandbox_layout.canonical_project_alias_path(alias / "missing/reports")
        == target / "missing/reports"
    )
    (target / "link").symlink_to(target, target_is_directory=True)
    result = sandbox_layout.canonical_project_alias_path(alias / "link/reports")
    assert result == target / "link/reports"
    assert result.parent.is_symlink()


@pytest.mark.parametrize("defect", ["target", "owner", "writable_parent", "directory", "missing"])
def test_untrusted_alias_is_unchanged(project_alias, monkeypatch, defect):
    alias, target = project_alias
    if defect in {"target", "directory", "missing"}:
        alias.unlink()
        if defect == "target":
            alias.symlink_to(target / "other", target_is_directory=True)
        elif defect == "directory":
            alias.mkdir()
    elif defect == "owner":
        configure_project_alias(monkeypatch, alias, target, owner=1000)
    else:
        configure_project_alias(monkeypatch, alias, target, parent_mode=0o777)
    path = alias / "reports"
    assert sandbox_layout.canonical_project_alias_path(path) == path


def test_alias_traversal_is_refused(project_alias):
    alias, _target = project_alias
    with pytest.raises(ValueError, match="traversal"):
        sandbox_layout.canonical_project_alias_path(alias / "../reports")


def test_other_prefixes_and_relative_paths_are_unchanged(project_alias):
    alias, _target = project_alias
    for path in (
        Path("booley-project/reports"),
        alias.with_name(alias.name + "-other") / "reports",
    ):
        assert sandbox_layout.canonical_project_alias_path(path) == path


def test_non_posix_paths_receive_no_exemption(monkeypatch):
    path = Path("/booley-project/reports")
    monkeypatch.setattr(sandbox_layout, "os", SimpleNamespace(name="nt"))
    assert sandbox_layout.canonical_project_alias_path(path) == path
