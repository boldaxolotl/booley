"""Git checkout fixtures: a main Project, its Snapshot worktree and its Paired Checkout."""

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


def snapshot(main: Path, *, relative: str = ".booley", name: str = "s1") -> Path:
    """A linked worktree carrying a copy (Snapshot) of the Booley Directory."""
    import shutil

    from tests.project_runtime_git_support import git

    worktree = main / relative / "worktrees" / name
    git(main, "worktree", "add", "-q", "--detach", str(worktree))
    shutil.copytree(
        main / relative,
        worktree / relative,
        ignore=shutil.ignore_patterns("worktrees"),
        dirs_exist_ok=True,
    )
    return worktree


def paired_checkout(main: Path, location: Path | None = None) -> Path:
    """A linked worktree whose ``.booley`` is a worktree of the Versioned Booley Directory.

    *location* defaults to a sibling of *main*; pass ``main/.booley/worktrees/<name>``
    to register it like an Interactive Worktree.
    """
    from tests.project_runtime_git_support import git

    versioned = main / ".booley"
    init_repo(versioned)
    (versioned / ".gitignore").write_text("worktrees/\n", encoding="utf-8")
    commit_all(versioned)
    worktree = location or main.parent / "paired"
    git(main, "worktree", "add", "-q", "--detach", str(worktree))
    git(versioned, "worktree", "add", "-q", "--detach", str(worktree / ".booley"))
    return worktree


LAYOUTS = ("snapshot", "paired")
RUNTIMES = ("host", "sandbox")


def linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, layout: str, runtime: str
) -> tuple[Path, Path]:
    """Return ``(main, worktree)``: a linked worktree registered in main's ``worktrees/``.

    Main and the worktree carry *different* ``devcontainer.json`` bytes, as real
    per-folder seeding produces. ``runtime="host"`` selects each checkout's own
    Booley Directory by walk-up; ``"sandbox"`` selects main's through
    ``BOOLEY_DIR``, as the Sandbox does.
    """
    from booley.runtime.booley_dir import reset_cache

    isolate_git(tmp_path, monkeypatch)
    main = main_project(tmp_path)
    if layout == "snapshot":
        worktree = snapshot(main)
    else:
        worktree = paired_checkout(main, main / ".booley" / "worktrees" / "p1")
    for root in (main, worktree):
        devcontainer = root / ".devcontainer" / "devcontainer.json"
        devcontainer.parent.mkdir(parents=True, exist_ok=True)
        devcontainer.write_text(f'{{"name": "{root.name}"}}\n', encoding="utf-8")
    if runtime == "sandbox":
        monkeypatch.setenv("BOOLEY_DIR", str(main / ".booley"))
    reset_cache()
    return main, worktree
