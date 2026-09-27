"""Tests for shared Project repository inspection."""

from __future__ import annotations

import subprocess
from pathlib import Path

from booley.runtime.project_repositories import inspect_symbolic_branch


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
