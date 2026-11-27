"""Tests for shared Booley Directory's repository inspection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.conftest import reset_git_command_config
from tests.git_support import git

from booley.runtime.project_repositories import inspect_symbolic_branch


@pytest.fixture(autouse=True)
def _isolated_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    reset_git_command_config(monkeypatch)
    monkeypatch.delenv("GIT_TEMPLATE_DIR", raising=False)
    for variable in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(variable, raising=False)


def _repository(tmp_path: Path, branch: str = "release/next") -> Path:
    repository = tmp_path / "project"
    repository.mkdir()
    git(repository, "init", "-b", branch)
    (repository / "README.md").write_text("demo\n", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "initial")
    return repository


@pytest.mark.parametrize(
    ("branch", "then", "expected"),
    [
        ("release/next", (), "release/next"),
        ("release/next", ("checkout", "--detach"), None),
        ("shared-name", ("tag", "shared-name"), "shared-name"),
    ],
    ids=["attached", "detached", "same-named-tag"],
)
def test_inspect_symbolic_branch(
    tmp_path: Path, branch: str, then: tuple[str, ...], expected: str | None
) -> None:
    repository = _repository(tmp_path, branch)
    if then:
        git(repository, *then)

    inspection = inspect_symbolic_branch(repository)

    assert inspection.branch == expected
    assert (inspection.result.returncode == 0) is (expected is not None)


def test_project_topology_keeps_independent_configured_and_fixed_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.core.booley_dir import reset_cache
    from booley.runtime.project_repositories import (
        paired_project_repository,
        project_topology,
        resolve_inner_project_repo,
    )

    outer = _repository(tmp_path)
    configured_parent = tmp_path / "configured"
    configured_parent.mkdir()
    standalone = _repository(configured_parent)
    (outer / "booley.toml").write_text(
        f'[project]\ndir = "{standalone.as_posix()}"\n', encoding="utf-8"
    )
    git(standalone, "worktree", "add", "--detach", str(outer / ".booley"))
    # An ambient selection must not redirect the control repository.
    git(standalone, "worktree", "add", "--detach", str(tmp_path / "ambient"))
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / "ambient"))
    reset_cache()
    topology = project_topology(outer)
    assert topology.paired is not None
    assert topology.paired.worktree == outer / ".booley"
    assert topology.standalone == standalone
    assert paired_project_repository(outer) == topology.paired
    assert resolve_inner_project_repo(outer) == standalone


@pytest.mark.parametrize("kind", ["absent", "directory", "standalone", "configured"])
def test_project_topology_classifies_real_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from booley.core.booley_dir import reset_cache
    from booley.runtime.project_repositories import project_topology

    outer = _repository(tmp_path)
    ambient = tmp_path / "empty-project"
    ambient.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(ambient))
    reset_cache()
    project = outer / ("custom" if kind == "configured" else ".booley")
    if kind != "absent":
        project.mkdir()
    if kind in {"standalone", "configured"}:
        git(project, "init")
    if kind == "configured":
        (outer / "booley.toml").write_text('[project]\ndir = "custom"\n', encoding="utf-8")
    topology = project_topology(outer)
    assert topology.paired is None
    assert topology.standalone == (project if kind in {"standalone", "configured"} else None)


def test_paired_projection_never_resolves_ambient_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime import project_repositories

    outer = _repository(tmp_path)

    def forbidden(_root):
        raise AssertionError("paired selection consulted configured Project")

    monkeypatch.setattr(project_repositories, "resolve_checkout_booley_dir", forbidden)
    assert project_repositories.paired_project_repository(outer) is None
    git(outer, "config", "core.worktree", str(outer))
    (outer / ".booley").mkdir()
    (outer / ".booley/.git").write_text(f"gitdir: {outer / '.git'}\n", encoding="utf-8")
    with pytest.raises(project_repositories.RepositoryCheckoutError, match="unexpected root"):
        project_repositories.paired_project_repository(outer)
    (outer / ".booley/.git").write_text("gitdir: /missing\n", encoding="utf-8")
    with pytest.raises(project_repositories.RepositoryCheckoutError, match="unavailable"):
        project_repositories.paired_project_repository(outer)


def test_paired_identity_filesystem_error_has_typed_repository_context(tmp_path, monkeypatch):
    from booley.runtime.project_repositories import (
        RepositoryCheckoutError,
        paired_project_repository,
    )

    outer = _repository(tmp_path)
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    source = _repository(source_parent)
    paired = outer / ".booley"
    git(source, "worktree", "add", "--detach", str(paired))
    samefile = Path.samefile

    def unavailable(path, other):
        if path == paired:
            raise PermissionError("filesystem identity unavailable")
        return samefile(path, other)

    monkeypatch.setattr(Path, "samefile", unavailable)
    with pytest.raises(RepositoryCheckoutError, match=r"cannot be resolved.*identity unavailable"):
        paired_project_repository(outer)


def test_repository_inspection_ignores_foreign_selectors(tmp_path, monkeypatch):
    from tests.git_support import hostile

    from booley.runtime.project_repositories import git_directories, run_git

    intended = _repository(tmp_path)
    parent = tmp_path / "foreign-parent"
    parent.mkdir()
    foreign = _repository(parent, branch="foreign")
    before = git_directories(intended)
    for key, value in hostile(foreign).items():
        monkeypatch.setenv(key, value)
    assert inspect_symbolic_branch(intended).branch == "release/next"
    assert git_directories(intended) == before
    assert Path(run_git(intended, "rev-parse", "--show-toplevel").stdout.strip()) == intended


def _pair(tmp_path: Path, location: Path) -> None:
    """Attach a Paired Checkout of a fresh project repository at *location*."""
    parent = tmp_path / "project-repo"
    parent.mkdir()
    git(_repository(parent), "worktree", "add", "--detach", str(location))


def test_relocated_paired_checkout_prefix_names_the_configured_directory(tmp_path: Path) -> None:
    from booley.runtime.project_repositories import paired_project_repository

    outer = _repository(tmp_path)
    (outer / "booley.toml").write_text('[project]\ndir = "state"\n', encoding="utf-8")
    _pair(tmp_path, outer / "state")

    paired = paired_project_repository(outer)

    assert paired is not None
    assert paired.worktree == outer / "state"
    assert paired.path_prefix == "state"
    assert paired.prefixed_path("cores/x") == "state/cores/x"


def test_paired_checkout_keeps_the_fixed_name_when_the_configured_directory_is_not_one(
    tmp_path: Path,
) -> None:
    """Pairings made by earlier Booley versions at ``.booley`` stay recognized."""
    from booley.runtime.project_repositories import paired_project_repository

    outer = _repository(tmp_path)
    (outer / "booley.toml").write_text('[project]\ndir = "../outside"\n', encoding="utf-8")
    assert paired_project_repository(outer) is None
    _pair(tmp_path, outer / ".booley")

    paired = paired_project_repository(outer)

    assert paired is not None and paired.path_prefix == ".booley"
