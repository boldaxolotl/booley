"""Git topologies for Booley Directory tests: main, linked Snapshot, Paired Checkout."""

from __future__ import annotations

from pathlib import Path

import pytest


def isolate_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fixture repositories independent of the developer's Git configuration."""
    empty = tmp_path / "empty-git-config"
    empty.touch()
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.delenv("BOOLEY_DIR", raising=False)


def init_repo(root: Path) -> None:
    from tests.project_runtime_git_support import git

    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")


def commit_all(root: Path) -> None:
    from tests.project_runtime_git_support import git

    git(root, "add", "-A")
    git(root, "commit", "-qm", "fixture")


def main_project(tmp_path: Path, *, relative: str = ".booley", toml: str = "") -> Path:
    """A committed Git checkout whose Main Booley Directory is ``<root>/<relative>``."""
    main = tmp_path / "main"
    init_repo(main)
    (main / relative).mkdir(parents=True)
    (main / relative / "booley.toml").write_text("", encoding="utf-8")
    (main / "README.md").write_text("fixture\n", encoding="utf-8")
    if toml:
        (main / "booley.toml").write_text(toml, encoding="utf-8")
    if relative == ".booley":
        (main / ".gitignore").write_text(".booley/\n", encoding="utf-8")
    commit_all(main)
    return main


def snapshot(main: Path, *, relative: str = ".booley") -> Path:
    """A linked worktree carrying a copy (Snapshot) of the Booley Directory."""
    import shutil

    from tests.project_runtime_git_support import git

    worktree = main / relative / "worktrees" / "s1"
    git(main, "worktree", "add", "-q", "--detach", str(worktree))
    shutil.copytree(
        main / relative,
        worktree / relative,
        ignore=shutil.ignore_patterns("worktrees"),
        dirs_exist_ok=True,
    )
    return worktree


def paired_checkout(main: Path) -> Path:
    """A linked worktree whose ``.booley`` is a worktree of the Versioned Booley Directory."""
    from tests.project_runtime_git_support import git

    versioned = main / ".booley"
    init_repo(versioned)
    (versioned / ".gitignore").write_text("worktrees/\n", encoding="utf-8")
    commit_all(versioned)
    worktree = main.parent / "paired"
    git(main, "worktree", "add", "-q", "--detach", str(worktree))
    git(versioned, "worktree", "add", "-q", "--detach", str(worktree / ".booley"))
    return worktree


def enter_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Work from a linked Snapshot worktree on the host: ``(Main Booley Directory, worktree)``.

    No ``BOOLEY_DIR`` is set, the CWD is the worktree, and the resolver cache is
    cleared, so the selected directory is the worktree's own and only the Common
    resolver reaches main.
    """
    from booley.runtime.booley_dir import reset_cache

    isolate_git(tmp_path, monkeypatch)
    main = main_project(tmp_path)
    worktree = snapshot(main)
    monkeypatch.chdir(worktree)
    reset_cache()
    return main / ".booley", worktree
