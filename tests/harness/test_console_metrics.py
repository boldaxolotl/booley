"""Worktree line metrics used by the ticket Console."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from booley.harness.console_metrics import WorktreeLineCounter


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test")
    (project / "tracked.sv").write_text("one\n", encoding="utf-8")
    _git(project, "add", "tracked.sv")
    _git(project, "commit", "-qm", "base")

    worktree = project / ".state" / "worktrees" / "ticket"
    worktree.parent.mkdir(parents=True)
    _git(project, "worktree", "add", "-qb", "ticket", str(worktree), "main")
    dot_git = worktree / ".git"
    git_dir = Path(dot_git.read_text(encoding="utf-8").strip().removeprefix("gitdir: "))
    return project, worktree, dot_git, git_dir


def _rewrite_git_pointer(dot_git: Path, git_dir: str) -> None:
    with dot_git.open("r+", encoding="utf-8") as pointer_file:
        pointer_file.seek(0)
        pointer_file.write(f"gitdir: {git_dir}\n")
        pointer_file.truncate()


def test_snapshot_includes_tracked_and_untracked_text(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked.sv").write_text("one\ntwo\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.sv")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "branch", "base")
    counter = WorktreeLineCounter(tmp_path, "base")

    (tmp_path / "tracked.sv").write_text("one changed\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "new.sv").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"not\x00text\n")
    (tmp_path / "link.sv").symlink_to("new.sv")

    assert counter.snapshot() == (4, 1)
    assert counter.snapshot_by_file() == {
        "new.sv": (2, 0),
        "tracked.sv": (2, 1),
    }


def test_normalize_agent_reported_paths(tmp_path):
    _git(tmp_path, "init", "-q")
    counter = WorktreeLineCounter(tmp_path, "HEAD")

    assert counter.normalize_path("rtl/top.sv") == "rtl/top.sv"
    assert counter.normalize_path(str(tmp_path / "rtl/top.sv")) == "rtl/top.sv"
    assert counter.normalize_path("/work/rtl/top.sv") == "rtl/top.sv"
    assert (
        counter.normalize_path(f"/booley-project/worktrees/{tmp_path.name}/rtl/top.sv")
        == "rtl/top.sv"
    )
    assert counter.normalize_path("../outside.sv") is None
    assert counter.normalize_path("/etc/outside.sv") is None


def test_normalize_project_relative_path_into_worktree(tmp_path):
    project = tmp_path / "project"
    worktree = project / ".state" / "worktrees" / "ticket"
    worktree.mkdir(parents=True)
    _git(worktree, "init", "-q")
    counter = WorktreeLineCounter(worktree, "HEAD", reported_root=project)

    reported = ".state/worktrees/ticket/rtl/top.sv"
    assert counter.normalize_path(reported) == "rtl/top.sv"


def test_snapshot_returns_none_without_git_metadata(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert WorktreeLineCounter(worktree, "HEAD").snapshot() is None


def test_snapshot_returns_none_with_malformed_git_pointer(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("not a git pointer\n", encoding="utf-8")

    assert WorktreeLineCounter(worktree, "HEAD").snapshot() is None


def test_snapshot_accepts_relative_git_pointer(tmp_path):
    _project, worktree, dot_git, git_dir = _linked_worktree(tmp_path)
    _rewrite_git_pointer(dot_git, os.path.relpath(git_dir, start=worktree))
    counter = WorktreeLineCounter(worktree, "main")
    (worktree / "tracked.sv").write_text("one\ntwo\n", encoding="utf-8")

    assert counter.snapshot_by_file() == {"tracked.sv": (1, 0)}


def test_snapshot_returns_none_for_unmapped_runtime_git_pointer(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(
        "gitdir: /work/.git/worktrees/ticket\n",
        encoding="utf-8",
    )

    assert WorktreeLineCounter(worktree, "HEAD").snapshot() is None


def test_snapshot_remains_absolute_after_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "rtl.sv").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "rtl.sv")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "branch", "base")
    counter = WorktreeLineCounter(tmp_path, "base")

    (tmp_path / "rtl.sv").write_text("one\ntwo\n", encoding="utf-8")
    _git(tmp_path, "commit", "-qam", "edit")
    (tmp_path / "rtl.sv").write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert counter.snapshot() == (2, 0)


def test_snapshot_maps_runtime_authored_worktree_gitdir_to_host(tmp_path):
    project, worktree, dot_git, git_dir = _linked_worktree(tmp_path)
    _rewrite_git_pointer(dot_git, f"/work/.git/worktrees/{git_dir.name}")

    counter = WorktreeLineCounter(worktree, "main", reported_root=project)
    (worktree / "tracked.sv").write_text("one\ntwo\n", encoding="utf-8")

    assert counter.snapshot_by_file() == {"tracked.sv": (1, 0)}
