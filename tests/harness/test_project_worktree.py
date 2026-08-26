"""Integration tests for Ticket Mode's paired stealth-project repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.harness.developer import _check_ticket_dirty_statuses, _commit_ticket_paths
from booley.harness.models import TicketContext
from booley.harness.setup.project_worktree import ProjectWorktreeError, prepare_project_worktree
from booley.runtime.project_dir import reset_cache
from booley.runtime.ticket_repositories import (
    TicketWorkspace,
    TicketWorkspaceError,
    TicketWorkspaceRequest,
    WorkspaceDisposition,
    WorkspaceMode,
    project_repository_scope,
    project_ticket_branch,
)
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


def _commit_board_ticket(project: Path, state: str, slug: str) -> Path:
    ticket = project / "tickets" / "board" / state / f"{slug}.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text(f"# {slug}\n", encoding="utf-8")
    _commit_all(project, f"chore: add {slug} ticket")
    return ticket


def _move_board_ticket(ticket: Path, state: str) -> Path:
    destination = ticket.parent.parent / state / ticket.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    ticket.rename(destination)
    return destination


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


def _workspace(ctx: TicketContext) -> TicketWorkspace:
    assert ctx.worktree_path is not None
    return TicketWorkspace.open(
        TicketWorkspaceRequest(
            project_root=ctx.project_root,
            worktree=ctx.worktree_path,
            ticket_slug=ctx.slug,
            base=ctx.branch,
            ticket_scope=tuple(ctx.scope_raw),
            mode=WorkspaceMode(ctx.workspace_intent),
        )
    )


def test_scoped_stealth_project_gets_paired_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)

    nested = _workspace(ctx).prepare()

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

    assert [entry.path for entry in _check_ticket_dirty_statuses(ctx)] == [
        ".booley_project/cores/dut.core"
    ]


@pytest.mark.parametrize("damage", ["missing", "non_file"])
def test_pending_changes_fail_when_paired_checkout_cannot_be_inspected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    workspace = _workspace(ctx)
    nested = workspace.prepare()
    assert nested is not None
    git_pointer = nested / ".git"
    git_pointer.unlink()
    if damage == "non_file":
        git_pointer.mkdir()

    with pytest.raises(TicketWorkspaceError, match="paired project"):
        workspace.pending_changes()


def test_outer_and_project_edits_commit_to_separate_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    nested = prepare_project_worktree(ctx)
    assert nested is not None
    (ctx.worktree_path / "rtl" / "dut.sv").write_text(
        "module dut; wire changed; endmodule\n", encoding="utf-8"
    )
    (nested / "cores" / "dut.core").write_text("CAPI=2:\nname: ::dut:1\n", encoding="utf-8")

    workspace = _workspace(ctx)
    assert workspace.authored_project_dir == nested
    dirty = workspace.pending_changes()
    paths = [entry.path for entry in dirty]
    assert paths == ["rtl/dut.sv", ".booley_project/cores/dut.core"]

    workspace.commit(paths, "fix: route project edits")

    assert _git(ctx.worktree_path, "show", "--format=", "--name-only", "HEAD") == "rtl/dut.sv"
    assert _git(nested, "show", "--format=", "--name-only", "HEAD") == "cores/dut.core"
    assert workspace.pending_changes() == ()

    ok, error = workspace.finish(
        WorkspaceDisposition.MERGE,
        "merge(change-core): project content",
    )

    assert ok, error
    source_core = ctx.project_root / ".booley_project" / "cores" / "dut.core"
    assert "::dut:1" in source_core.read_text(encoding="utf-8")
    assert not nested.exists()
    branches = _git(ctx.project_root / ".booley_project", "branch", "--format=%(refname:short)")
    assert project_ticket_branch(ctx.slug) not in branches.splitlines()


def test_workspace_resumes_existing_project_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    workspace = _workspace(ctx)
    nested = workspace.prepare()
    assert nested is not None
    (nested / "cores" / "dut.core").write_text("interrupted\n", encoding="utf-8")
    workspace.commit([".booley_project/cores/dut.core"], "fix: preserve interruption")
    head = _git(nested, "rev-parse", "HEAD")

    request = workspace.request
    resumed = TicketWorkspace.open(
        TicketWorkspaceRequest(
            project_root=request.project_root,
            worktree=request.worktree,
            ticket_slug=request.ticket_slug,
            base=request.base,
            ticket_scope=request.ticket_scope,
            mode=WorkspaceMode.RESUME,
            expected_sha=head,
        )
    )

    assert resumed.prepare() == nested
    assert _git(nested, "rev-parse", "HEAD") == head


def test_workspace_discard_removes_project_recovery_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    workspace = _workspace(ctx)
    nested = workspace.prepare()
    assert nested is not None
    (nested / "cores" / "dut.core").write_text("discard me\n", encoding="utf-8")
    workspace.commit([".booley_project/cores/dut.core"], "fix: discarded work")

    ok, error = workspace.finish(WorkspaceDisposition.DISCARD)

    assert ok, error
    assert not nested.exists()
    branches = _git(ctx.project_root / ".booley_project", "branch", "--format=%(refname:short)")
    assert project_ticket_branch(ctx.slug) not in branches.splitlines()


def test_dirty_project_repo_is_not_silently_snapshotted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    project = ctx.project_root / ".booley_project"
    (project / "cores" / "dut.core").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ProjectWorktreeError, match="uncommitted changes"):
        prepare_project_worktree(ctx)


def test_unstaged_board_enqueue_and_claim_do_not_block_project_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    project = ctx.project_root / ".booley_project"
    draft = _commit_board_ticket(project, "drafts", ctx.slug)

    queued = _move_board_ticket(draft, "queue")
    active = _move_board_ticket(queued, "active")
    nested = prepare_project_worktree(ctx)

    assert nested is not None
    assert active.is_file()
    assert _git(project, "status", "--short").splitlines() == [
        f"D tickets/board/drafts/{ctx.slug}.md",
        "?? tickets/board/active/",
    ]


def test_staged_board_change_still_blocks_project_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    project = ctx.project_root / ".booley_project"
    queued = _commit_board_ticket(project, "queue", ctx.slug)
    _move_board_ticket(queued, "active")
    _git(project, "add", "-A")

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


def test_workspace_merge_can_preserve_paired_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    workspace = _workspace(ctx)
    nested = workspace.prepare()
    assert nested is not None
    (nested / "cores" / "dut.core").write_text(
        "CAPI=2:\nname: ::dut:preserved\n",
        encoding="utf-8",
    )
    workspace.commit([".booley_project/cores/dut.core"], "fix: preserve paired checkout")

    ok, error = workspace.finish(
        WorkspaceDisposition.MERGE,
        "merge project content without cleanup",
        cleanup=False,
    )

    assert ok, error
    assert nested.exists()
    assert "::dut:preserved" in (
        ctx.project_root / ".booley_project" / "cores" / "dut.core"
    ).read_text(encoding="utf-8")
    assert workspace.finish(WorkspaceDisposition.DISCARD) == (True, "")
    assert not nested.exists()


def test_project_branch_merges_with_unstaged_board_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    project = ctx.project_root / ".booley_project"
    queued = _commit_board_ticket(project, "queue", ctx.slug)
    nested = prepare_project_worktree(ctx)
    assert nested is not None
    (nested / "cores" / "dut.core").write_text("CAPI=2:\nname: ::dut:3\n", encoding="utf-8")
    _commit_ticket_paths(ctx, [".booley_project/cores/dut.core"], "fix: update core")
    review = _move_board_ticket(queued, "review")

    ok, error = merge_project_ticket_branch(ctx.project_root, ctx.slug, "merge project content")

    assert ok, error
    assert review.is_file()
    assert not queued.exists()
    assert "::dut:3" in (project / "cores" / "dut.core").read_text(encoding="utf-8")
    assert _git(project, "status", "--short").splitlines() == [
        f"D tickets/board/queue/{ctx.slug}.md",
        "?? tickets/board/review/",
    ]


def test_project_branch_cannot_modify_ticket_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    project = ctx.project_root / ".booley_project"
    _commit_board_ticket(project, "queue", ctx.slug)
    nested = prepare_project_worktree(ctx)
    assert nested is not None
    nested_ticket = nested / "tickets" / "board" / "queue" / f"{ctx.slug}.md"
    nested_ticket.write_text("tampered\n", encoding="utf-8")
    _commit_all(nested, "bad: modify ticket state")

    ok, error = merge_project_ticket_branch(ctx.project_root, ctx.slug, "merge ticket state")

    assert not ok
    assert "modifies Ticket Board state" in error
    source_ticket = project / "tickets" / "board" / "queue" / f"{ctx.slug}.md"
    assert source_ticket.read_text(encoding="utf-8") == f"# {ctx.slug}\n"


def test_project_merge_conflict_is_aborted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ticket(tmp_path, monkeypatch)
    workspace = _workspace(ctx)
    nested = workspace.prepare()
    assert nested is not None
    (nested / "cores" / "dut.core").write_text("ticket version\n", encoding="utf-8")
    _commit_ticket_paths(ctx, [".booley_project/cores/dut.core"], "fix: branch version")

    source = ctx.project_root / ".booley_project"
    (source / "cores" / "dut.core").write_text("base version\n", encoding="utf-8")
    _commit_all(source, "fix: conflicting base version")

    ok, error = workspace.finish(WorkspaceDisposition.MERGE, "merge conflict")

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
    assert nested.exists()
    assert _git(nested, "branch", "--show-current") == project_ticket_branch(ctx.slug)
