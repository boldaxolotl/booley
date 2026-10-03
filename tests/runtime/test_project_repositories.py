"""Tests for shared Project repository inspection."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from booley.runtime.project_repositories import inspect_symbolic_branch


@pytest.fixture(autouse=True)
def _isolated_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    monkeypatch.delenv("GIT_TEMPLATE_DIR", raising=False)
    for variable in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(variable, raising=False)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path, branch: str = "release/next") -> Path:
    repository = tmp_path / "project"
    repository.mkdir()
    _git(repository, "init", "-b", branch)
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    return repository


def test_inspect_symbolic_branch_returns_attached_branch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    inspection = inspect_symbolic_branch(repository)

    assert inspection.branch == "release/next"
    assert inspection.result.returncode == 0


def test_inspect_symbolic_branch_reports_detached_head(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _git(repository, "checkout", "--detach")

    inspection = inspect_symbolic_branch(repository)

    assert inspection.branch is None
    assert inspection.result.returncode != 0


def test_inspect_symbolic_branch_ignores_same_named_tag(tmp_path: Path) -> None:
    repository = _repository(tmp_path, "shared-name")
    _git(repository, "tag", "shared-name")

    inspection = inspect_symbolic_branch(repository)

    assert inspection.branch == "shared-name"


def test_project_topology_keeps_independent_configured_and_fixed_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.core.project_dir import reset_cache
    from booley.runtime.project_repositories import (
        paired_project_repository,
        project_topology,
        resolve_inner_project_repo,
    )

    outer = _repository(tmp_path)
    configured_parent = tmp_path / "configured"
    configured_parent.mkdir()
    standalone = _repository(configured_parent)
    (outer / "booley.toml").write_text(f'[project]\ndir = "{standalone}"\n', encoding="utf-8")
    _git(standalone, "worktree", "add", "--detach", str(outer / ".booley_project"))
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path / "absent"))
    reset_cache()
    topology = project_topology(outer)
    assert topology.paired is not None
    assert topology.paired.worktree == outer / ".booley_project"
    assert topology.standalone == standalone
    assert paired_project_repository(outer) == topology.paired
    assert resolve_inner_project_repo(outer) == standalone


@pytest.mark.parametrize("kind", ["absent", "directory", "standalone", "configured"])
def test_project_topology_classifies_real_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from booley.core.project_dir import reset_cache
    from booley.runtime.project_repositories import project_topology

    outer = _repository(tmp_path)
    ambient = tmp_path / "empty-project"
    ambient.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(ambient))
    reset_cache()
    project = outer / ("custom" if kind == "configured" else ".booley_project")
    if kind != "absent":
        project.mkdir()
    if kind in {"standalone", "configured"}:
        _git(project, "init")
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

    monkeypatch.setattr(project_repositories, "resolve_checkout_project_dir", forbidden)
    assert project_repositories.paired_project_repository(outer) is None
    _git(outer, "config", "core.worktree", str(outer))
    (outer / ".booley_project").mkdir()
    (outer / ".booley_project/.git").write_text(f"gitdir: {outer / '.git'}\n", encoding="utf-8")
    with pytest.raises(project_repositories.RepositoryCheckoutError, match="unexpected root"):
        project_repositories.paired_project_repository(outer)
    (outer / ".booley_project/.git").write_text("gitdir: /missing\n", encoding="utf-8")
    with pytest.raises(project_repositories.RepositoryCheckoutError, match="unavailable"):
        project_repositories.paired_project_repository(outer)
