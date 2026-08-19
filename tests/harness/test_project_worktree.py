"""Integration tests for Ticket Mode's paired stealth-project repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.harness.developer import _check_ticket_dirty_statuses, _commit_ticket_paths
from booley.harness.models import TicketContext
from booley.harness.setup.project_worktree import ProjectWorktreeError, prepare_project_worktree
from booley.runtime.project_dir import reset_cache
from booley.runtime.ticket_repositories import project_repository_scope, project_ticket_branch
from booley.ticket_board.project_git_ops import (
    cleanup_project_ticket_branch,
    merge_project_ticket_branch,
)


@pytest.fixture(autouse=True)
def _clear_project_dir_cache():
    reset_cache()
    yield
    reset_cache()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _make_ticket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TicketContext:
    root = tmp_path / "rtl-project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "rtl").mkdir()
    (root / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    _commit_all(root, "feat: initial rtl")
    common = Path(_git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = root / common
    (common / "info" / "exclude").write_text("/.booley_project\n", encoding="utf-8")

    project = root / ".booley_project"
    (project / "cores").mkdir(parents=True)
    (project / ".gitignore").write_text("/worktrees/\n", encoding="utf-8")
    (project / "cores" / "dut.core").write_text("CAPI=2:\nname: ::dut:0\n", encoding="utf-8")
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.invalid")
    _commit_all(project, "feat: initial project config")

    ticket_worktree = project / "worktrees" / "change-core"
    _git(root, "worktree", "add", "-b", "change-core", str(ticket_worktree), "main")
    snapshot = ticket_worktree / ".booley_project" / "cores"
    snapshot.mkdir(parents=True)
    (snapshot / "dut.core").write_text("stale copy\n", encoding="utf-8")

    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    reset_cache()
    return TicketContext(
        slug="change-core",
        ticket_path=tmp_path / "change-core.md",
        ticket_type="bugfix",
        branch="main",
        summary="change core and RTL",
        scope_raw=["rtl/dut.sv", ".booley_project/cores/dut.core"],
        worktree_path=ticket_worktree,
        project_root=root,
    )


def test_scoped_stealth_project_gets_paired_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)

    nested = prepare_project_worktree(ctx)

    assert nested == ctx.worktree_path / ".booley_project"
    assert (nested / ".git").is_file()
    assert (nested / "cores" / "dut.core").read_text(encoding="utf-8").startswith("CAPI=2:")
    assert _git(nested, "branch", "--show-current") == project_ticket_branch(ctx.slug)
    assert _git(nested, "rev-parse", "--abbrev-ref", "@{upstream}") == "main"


def test_project_scope_is_rebased_for_inner_precommit_hook() -> None:
    assert project_repository_scope(
        [
            "rtl/dut.sv",
            ".booley_project/cores/*.core",
            ".booley_project/docs/new.md [new]",
            ".booley_project",
        ]
    ) == ["cores/*.core", "docs/new.md [new]", "**"]


def test_inner_repo_is_observable_even_without_project_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    ctx.scope_raw = ["rtl/dut.sv"]

    nested = prepare_project_worktree(ctx)
    assert nested is not None
    (nested / "cores" / "dut.core").write_text("accidental edit\n", encoding="utf-8")

    assert [entry.path for entry in _check_ticket_dirty_statuses(ctx.worktree_path)] == [
        ".booley_project/cores/dut.core"
    ]


def test_outer_and_project_edits_commit_to_separate_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    nested = prepare_project_worktree(ctx)
    assert nested is not None
    (ctx.worktree_path / "rtl" / "dut.sv").write_text(
        "module dut; wire changed; endmodule\n", encoding="utf-8"
    )
    (nested / "cores" / "dut.core").write_text(
        "CAPI=2:\nname: ::dut:1\n", encoding="utf-8"
    )

    dirty = _check_ticket_dirty_statuses(ctx.worktree_path)
    paths = [entry.path for entry in dirty]
    assert paths == ["rtl/dut.sv", ".booley_project/cores/dut.core"]

    _commit_ticket_paths(ctx, paths, "fix: route project edits")

    assert _git(ctx.worktree_path, "show", "--format=", "--name-only", "HEAD") == "rtl/dut.sv"
    assert _git(nested, "show", "--format=", "--name-only", "HEAD") == "cores/dut.core"
    assert _check_ticket_dirty_statuses(ctx.worktree_path) == []


def test_dirty_project_repo_is_not_silently_snapshotted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    project = ctx.project_root / ".booley_project"
    (project / "cores" / "dut.core").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ProjectWorktreeError, match="uncommitted changes"):
        prepare_project_worktree(ctx)


def test_project_branch_merges_and_cleans_up_with_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    nested = prepare_project_worktree(ctx)
    assert nested is not None
    core = nested / "cores" / "dut.core"
    core.write_text("CAPI=2:\nname: ::dut:2\n", encoding="utf-8")
    _commit_ticket_paths(ctx, [".booley_project/cores/dut.core"], "fix: update core")

    ok, error = merge_project_ticket_branch(
        ctx.project_root,
        ctx.slug,
        "merge(change-core): project content",
    )

    assert ok, error
    source_core = ctx.project_root / ".booley_project" / "cores" / "dut.core"
    assert "::dut:2" in source_core.read_text(encoding="utf-8")
    assert cleanup_project_ticket_branch(ctx.project_root, ctx.slug)
    assert not nested.exists()
    branches = _git(ctx.project_root / ".booley_project", "branch", "--format=%(refname:short)")
    assert project_ticket_branch(ctx.slug) not in branches.splitlines()


def test_project_merge_conflict_is_aborted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    nested = prepare_project_worktree(ctx)
    assert nested is not None
    (nested / "cores" / "dut.core").write_text("ticket version\n", encoding="utf-8")
    _commit_ticket_paths(ctx, [".booley_project/cores/dut.core"], "fix: branch version")

    source = ctx.project_root / ".booley_project"
    (source / "cores" / "dut.core").write_text("base version\n", encoding="utf-8")
    _commit_all(source, "fix: conflicting base version")

    ok, error = merge_project_ticket_branch(ctx.project_root, ctx.slug, "merge conflict")

    assert not ok
    assert "CONFLICT" in error
    merge_head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--verify", "MERGE_HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge_head.returncode != 0
    assert _git(source, "status", "--porcelain") == ""
