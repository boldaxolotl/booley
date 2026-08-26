"""Tests for dirty-worktree rejection in developer post guardrails."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.dev_support.development_state import DevelopmentState
from booley.harness.blocking import BlockingError
from booley.harness.developer import (
    _commit_ticket_paths,
    _run_post_guardrails,
)
from booley.harness.developer_guardrails import (
    DirtyFile,
    check_uncommitted_code_statuses,
)
from booley.harness.models import TicketContext
from booley.runtime.ticket_repositories import (
    TicketRepository,
    TicketWorkspace,
    TicketWorkspaceError,
    TicketWorkspaceRequest,
    WorkspaceMode,
)


def _make_ctx(tmp_path: Path) -> TicketContext:
    """Minimal context with a linked-worktree path and scoped RTL file."""
    wt = tmp_path / "wt"
    (wt / "rtl").mkdir(parents=True)
    (wt / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    return TicketContext(
        slug="dirty-handoff",
        ticket_path=tmp_path / "dirty-handoff.md",
        ticket_type="bugfix",
        branch="main",
        summary="reject uncommitted edits",
        scope_raw=["rtl/dut.sv"],
        worktree_path=wt,
        project_root=tmp_path,
    )


def _make_state(tmp_path: Path) -> Path:
    """Create an empty state file for debugger-count guardrail logic."""
    state_path = tmp_path / "booley_state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "dirty-handoff"
    state.save()
    return state_path


def _make_completed_state(tmp_path: Path) -> Path:
    """Create a state whose mandatory criteria are otherwise complete."""
    state_path = tmp_path / "booley_state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "dirty-handoff"
    state.init_criteria({"sim_pass_default": True})
    state.set_criterion("sim_pass_default", True)
    state.save()
    return state_path


def test_untracked_directories_are_expanded_to_file_paths(tmp_path: Path):
    result = MagicMock(returncode=0, stdout="?? rtl/dut.sv\0", stderr="")

    with patch("booley.harness.developer_guardrails.git_run", return_value=result) as git:
        dirty = check_uncommitted_code_statuses(tmp_path)

    assert dirty == [DirtyFile("rtl/dut.sv", "??")]
    git.assert_called_once_with(
        tmp_path,
        ["status", "--porcelain", "-z", "--untracked-files=all", "--ignore-submodules"],
        timeout=30,
    )


def test_paths_with_spaces_survive_porcelain_parsing(tmp_path: Path):
    """Plain porcelain C-quotes these; a quoted path would fail `git add`."""
    result = MagicMock(returncode=0, stdout="?? docs/Spec v1.pdf\0 M rtl/mem[0].sv\0", stderr="")

    with patch("booley.harness.developer_guardrails.git_run", return_value=result):
        dirty = check_uncommitted_code_statuses(tmp_path)

    assert dirty == [
        DirtyFile("docs/Spec v1.pdf", "??"),
        DirtyFile("rtl/mem[0].sv", " M"),
    ]


def test_rename_origin_field_is_not_read_as_a_record(tmp_path: Path):
    """`-z` appends the old path as its own field after a rename."""
    result = MagicMock(returncode=0, stdout="R  rtl/new.sv\0rtl/old.sv\0 M tb/t.sv\0", stderr="")

    with patch("booley.harness.developer_guardrails.git_run", return_value=result):
        dirty = check_uncommitted_code_statuses(tmp_path)

    assert dirty == [DirtyFile("rtl/new.sv", "R "), DirtyFile("tb/t.sv", " M")]


def test_uncommitted_edits_block_handoff_without_an_automatic_commit(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)
    commit = MagicMock(return_value=None)

    with (
        patch(
            "booley.harness.developer._check_ticket_dirty_statuses",
            return_value=[DirtyFile("rtl/dut.sv", " M")],
        ),
        patch("booley.runtime.git.commit_scope", side_effect=commit),
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is True
    commit.assert_not_called()
    block.assert_called_once()
    assert "Commit or restore every file" in block.call_args.args[1]


def test_repository_failure_does_not_skip_other_repository(tmp_path: Path):
    """A failed outer add must not prevent the authorized project commit."""
    ctx = _make_ctx(tmp_path)
    project_worktree = ctx.worktree_path / ".booley_project"
    repositories = (
        TicketRepository(ctx.worktree_path),
        TicketRepository(project_worktree, ".booley_project"),
    )
    workspace = TicketWorkspace(
        TicketWorkspaceRequest(
            project_root=ctx.project_root,
            worktree=ctx.worktree_path,
            ticket_slug=ctx.slug,
            base=ctx.branch,
            ticket_scope=tuple(ctx.scope_raw),
            mode=WorkspaceMode.FRESH,
        )
    )
    with (
        patch(
            "booley.harness.setup.project_worktree.ticket_workspace",
            return_value=workspace,
        ),
        patch(
            "booley.runtime.ticket_repositories.ticket_repositories",
            return_value=repositories,
        ),
        patch(
            "booley.runtime.git.commit_scope",
            side_effect=[BlockingError("outer failed"), None],
        ) as commit,
        pytest.raises(BlockingError, match="outer failed"),
    ):
        _commit_ticket_paths(
            ctx,
            ["rtl/dut.sv", ".booley_project/cores/dut.core"],
            "fix: route both",
        )

    assert commit.call_count == 2
    assert commit.call_args_list[1].args[0] == project_worktree
    assert commit.call_args_list[1].args[1] == ["cores/dut.core"]


def test_duplicated_source_root_dirty_files_are_rejected_before_tree_validation(
    tmp_path: Path,
):
    ctx = _make_ctx(tmp_path)
    nested = ctx.worktree_path / "rtl" / "rtl"
    nested.mkdir(parents=True)
    (nested / "other.sv").write_text("module other; endmodule\n", encoding="utf-8")
    state_path = _make_state(tmp_path)
    state = DevelopmentState.load(state_path)
    state.init_criteria({"sim_pass_default": True})
    state.save()

    with (
        patch(
            "booley.harness.developer._check_ticket_dirty_statuses",
            return_value=[DirtyFile("rtl/rtl/other.sv", "??")],
        ),
        patch("booley.runtime.git.commit_scope") as commit,
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=1)

    assert blocked is True
    commit.assert_not_called()
    reason = block.call_args.args[1]
    assert reason.startswith("Developer Agent stopped with uncommitted changes")
    assert "rtl/rtl/other.sv" in reason
    report = ctx.logs_dir / ".runtime" / "malformed_rtl_output.json"
    assert not report.exists()


def test_nested_rtl_is_caught_even_when_scope_names_no_rtl(tmp_path: Path):
    """The committed path: a TB-scoped ticket used to escape this check entirely."""
    ctx = _make_ctx(tmp_path)
    ctx.scope_raw = ["verif/tb_dut.sv"]
    nested = ctx.worktree_path / "rtl" / "rtl"
    nested.mkdir(parents=True)
    (nested / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer._check_ticket_dirty_statuses",
            side_effect=[[], []],
        ),
        patch("booley.runtime.git.commit_scope"),
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is True
    assert block.call_args.args[1].startswith("MALFORMED_SCORER_OUTPUT")


def test_scorer_file_deleted_under_a_new_glob_blocks(tmp_path: Path):
    """Held back from the commit, so the branch keeps a file the run deleted."""
    ctx = _make_ctx(tmp_path)
    ctx.scope_raw = ["rtl/dut.sv", "rtl/*.sv [new]"]
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer._check_ticket_dirty_statuses",
            return_value=[DirtyFile("rtl/legacy_fifo.sv", " D")],
        ),
        patch("booley.runtime.git.commit_scope") as commit,
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is True
    commit.assert_not_called()
    assert "rtl/legacy_fifo.sv" in block.call_args.args[1]


def test_done_ticket_with_uncommitted_scorer_file_blocks_handoff(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    state_path = _make_completed_state(tmp_path)

    with (
        patch(
            "booley.harness.developer._check_ticket_dirty_statuses",
            return_value=[DirtyFile("rtl/other.sv", " M")],
        ),
        patch("booley.runtime.git.commit_scope") as commit,
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw") as terminal,
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=2)

    assert blocked is True
    commit.assert_not_called()
    block.assert_called_once()
    assert any("uncommitted changes" in call.args[0] for call in terminal.call_args_list)


def test_deleted_files_under_new_scope_glob_block_dirty_handoff(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    ctx.scope_raw = ["rtl/dut.sv", "verif/lane1/*.sv [new]"]
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer._check_ticket_dirty_statuses",
            side_effect=[
                [
                    DirtyFile("rtl/dut.sv", " M"),
                    DirtyFile("verif/lane1/new_tb.sv", "??"),
                    DirtyFile("verif/lane1/old_tb.sv", " D"),
                ],
                [DirtyFile("verif/lane1/old_tb.sv", " D")],
            ],
        ),
        patch("booley.runtime.git.commit_scope", return_value=None) as commit,
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is True
    commit.assert_not_called()
    block.assert_called_once()


def test_git_status_error_blocks_handoff(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer._check_ticket_dirty_statuses",
            side_effect=TicketWorkspaceError("boom"),
        ),
        patch("booley.runtime.git.commit_scope") as commit,
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is True
    commit.assert_not_called()
    block.assert_called_once()


def test_missing_live_rtl_blocks_handoff(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    (ctx.worktree_path / "rtl" / "dut.sv").unlink()
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer._check_ticket_dirty_statuses",
            return_value=[],
        ),
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is True
    block.assert_called_once()


def test_committed_nested_rtl_output_blocks_handoff(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    nested = ctx.worktree_path / "rtl" / "rtl"
    nested.mkdir()
    (nested / "bad.sv").write_text("module bad; endmodule\n", encoding="utf-8")
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer._check_ticket_dirty_statuses",
            return_value=[],
        ),
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=3)

    assert blocked is True
    reason = block.call_args.args[1]
    assert reason.startswith("MALFORMED_SCORER_OUTPUT")
    assert "rtl/rtl/bad.sv" in reason
    report = ctx.logs_dir / ".runtime" / "malformed_rtl_output.json"
    assert "rtl/rtl/bad.sv" in report.read_text(encoding="utf-8")
