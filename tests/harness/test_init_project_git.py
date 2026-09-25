"""Project-data Git initialization follows the outer Project branch."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from tests.harness.git_support import git_stdout as _git


def _outer_project(tmp_path: Path, branch: str) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", branch)
    project_dir = root / ".booley_project"
    project_dir.mkdir()
    return root, project_dir


def _commit_outer(root: Path) -> None:
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")


@pytest.mark.parametrize("branch", ["main", "release/next"])
def test_project_data_git_inherits_outer_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    branch: str,
) -> None:
    root, project_dir = _outer_project(tmp_path, branch)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text("[init]\n\tdefaultBranch = master\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "missing-system-config"))

    init_cmd._init_project_git_repo(project_dir, InitContext(project_root=root))

    assert _git(project_dir, "symbolic-ref", "HEAD") == f"refs/heads/{branch}"
    assert branch in capsys.readouterr().out


def test_check_only_names_branch_without_creating_repository(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, project_dir = _outer_project(tmp_path, "release/next")

    init_cmd._init_project_git_repo(
        project_dir,
        InitContext(project_root=root, check_only=True),
    )

    assert not (project_dir / ".git").exists()
    assert "git init -b release/next" in capsys.readouterr().out


def test_existing_project_data_repository_is_untouched(tmp_path: Path) -> None:
    root, project_dir = _outer_project(tmp_path, "main")
    _git(project_dir, "init", "-b", "legacy")

    init_cmd._init_project_git_repo(project_dir, InitContext(project_root=root))

    assert _git(project_dir, "symbolic-ref", "HEAD") == "refs/heads/legacy"


def test_partial_project_data_repository_is_repaired(tmp_path: Path) -> None:
    root, project_dir = _outer_project(tmp_path, "main")
    (project_dir / ".git").mkdir()

    init_cmd._init_project_git_repo(project_dir, InitContext(project_root=root))

    assert _git(project_dir, "symbolic-ref", "HEAD") == "refs/heads/main"


def test_detached_outer_checkout_defers_project_data_initialization(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, project_dir = _outer_project(tmp_path, "main")
    _commit_outer(root)
    _git(root, "checkout", "--detach")

    init_cmd._init_project_git_repo(project_dir, InitContext(project_root=root))

    assert not (project_dir / ".git").exists()
    output = capsys.readouterr().out
    assert "attach the outer checkout to a branch" in output

    _git(root, "switch", "-c", "feature/retry")
    init_cmd._init_project_git_repo(project_dir, InitContext(project_root=root))

    assert _git(project_dir, "symbolic-ref", "HEAD") == "refs/heads/feature/retry"
