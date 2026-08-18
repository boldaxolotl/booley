"""Tests for leftover-edit persistence in developer post guardrails."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from booley.dev_support.development_state import DevelopmentState
from booley.dev_support.validate_commit_msg import MAX_SUMMARY_LEN, validate_message
from booley.harness.developer import _leftover_commit_message, _run_post_guardrails
from booley.harness.developer_guardrails import (
    DirtyFile,
    GitStatusError,
    check_uncommitted_code_statuses,
)
from booley.harness.models import TicketContext


def _make_ctx(tmp_path: Path) -> TicketContext:
    """Minimal context with a linked-worktree path and scoped RTL file."""
    wt = tmp_path / "wt"
    (wt / "rtl").mkdir(parents=True)
    (wt / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    return TicketContext(
        slug="leftover-edits",
        ticket_path=tmp_path / "leftover-edits.md",
        ticket_type="bugfix",
        branch="main",
        summary="persist leftover edits",
        scope_raw=["rtl/dut.sv"],
        worktree_path=wt,
        project_root=tmp_path,
    )


def _make_state(tmp_path: Path) -> Path:
    """Create an empty state file for debugger-count guardrail logic."""
    state_path = tmp_path / "booley_state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "leftover-edits"
    state.save()
    return state_path


def _make_completed_state(tmp_path: Path) -> Path:
    """Create a state whose mandatory criteria are otherwise complete."""
    state_path = tmp_path / "booley_state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "leftover-edits"
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


def test_leftover_edits_are_committed_before_handoff(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)
    commit = MagicMock(return_value=None)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=[[DirtyFile("rtl/dut.sv", " M")], []],
        ),
        patch("booley.harness.git_utils.commit_scope", side_effect=commit),
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is False
    commit.assert_called_once_with(
        ctx.worktree_path,
        ["rtl/dut.sv"],
        "fix(leftover-edits): persist leftover edits (1 file)",
        literal=True,
    )
    block.assert_not_called()


def test_remaining_dirty_files_block_handoff(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=[
                [DirtyFile("rtl/dut.sv", " M"), DirtyFile("rogue.sv", " M")],
                [DirtyFile("rtl/dut.sv", " M"), DirtyFile("rogue.sv", " M")],
            ],
        ),
        patch("booley.harness.git_utils.commit_scope", return_value=None),
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is True
    block.assert_called_once()


def test_out_of_scope_files_are_committed_not_abandoned(tmp_path: Path):
    """Scope is advisory (ADR 0046): out-of-scope edits ride the same commit.

    Abandoning them was the bug -- the branch shipped without work the agent
    had already validated in its worktree.
    """
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=[
                [DirtyFile("rtl/dut.sv", " M"), DirtyFile("rtl/other.sv", " M")],
                [],
            ],
        ),
        patch("booley.harness.git_utils.commit_scope", return_value=None) as commit,
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is False
    assert commit.call_args.args[1] == ["rtl/dut.sv", "rtl/other.sv"]
    block.assert_not_called()


def test_out_of_scope_file_still_dirty_after_commit_blocks(tmp_path: Path):
    """The commit was attempted and did not take -- that is a real failure."""
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=[
                [DirtyFile("rtl/dut.sv", " M"), DirtyFile("README.md", " M")],
                [DirtyFile("README.md", " M")],
            ],
        ),
        patch("booley.harness.git_utils.commit_scope", return_value=None),
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is True
    block.assert_called_once()


def test_harness_owned_dirty_files_are_left_uncommitted(tmp_path: Path):
    """Forbidden paths are skipped, not blocked on: the hook is the real gate."""
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=[
                [
                    DirtyFile("rtl/dut.sv", " M"),
                    DirtyFile(".booley_project/booley.toml", " M"),
                ],
                [DirtyFile(".booley_project/booley.toml", " M")],
            ],
        ),
        patch("booley.harness.git_utils.commit_scope", return_value=None) as commit,
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is False
    assert commit.call_args.args[1] == ["rtl/dut.sv"]
    block.assert_not_called()


def test_stealth_cores_are_ordinary_work_not_harness_owned(tmp_path: Path):
    """`.booley_project/cores/` is design description (ADR 0036), not bookkeeping."""
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=[[DirtyFile(".booley_project/cores/dut.core", " M")], []],
        ),
        patch("booley.harness.git_utils.commit_scope", return_value=None) as commit,
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is False
    assert commit.call_args.args[1] == [".booley_project/cores/dut.core"]
    block.assert_not_called()


def test_scoped_project_doc_is_committed_not_rejected(tmp_path: Path):
    """A project-dir memory map is authored work, not harness bookkeeping."""
    ctx = _make_ctx(tmp_path)
    path = ".booley_project/docs/fw/memory-map.md"
    ctx.scope_raw.append(path)
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=[[DirtyFile(path, "M ")], []],
        ),
        patch("booley.harness.git_utils.commit_scope", return_value=None) as commit,
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is False
    assert commit.call_args.args[1] == [path]
    block.assert_not_called()


def test_scorer_files_still_dirty_after_commit_block(tmp_path: Path):
    """The post-commit backstop: the branch lacks work the criteria measured."""
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            return_value=[DirtyFile("rtl/other.sv", " M")],
        ),
        patch("booley.harness.git_utils.commit_scope") as commit,
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is True
    commit.assert_called_once()
    assert "scorer-consumed file(s) still uncommitted" in block.call_args.args[1]
    assert (ctx.logs_dir / ".runtime" / "dirty_scorer_files.json").exists()


def test_duplicated_source_root_dirty_files_get_malformed_report(tmp_path: Path):
    """The dirty-tree path: the commit did not take, and the path is malformed."""
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)
    state = DevelopmentState.load(state_path)
    state.init_criteria({"sim_pass_default": True})
    state.save()

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            return_value=[DirtyFile("rtl/rtl/other.sv", "??")],
        ),
        patch("booley.harness.git_utils.commit_scope") as commit,
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=1)

    assert blocked is True
    commit.assert_called_once()
    reason = block.call_args.args[1]
    assert reason.startswith("MALFORMED_SCORER_OUTPUT")
    assert "duplicated source root" in reason
    report = ctx.logs_dir / ".runtime" / "dirty_scorer_files.json"
    assert "rtl/rtl/other.sv" in report.read_text(encoding="utf-8")


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
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=[[], []],
        ),
        patch("booley.harness.git_utils.commit_scope"),
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
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            return_value=[DirtyFile("rtl/legacy_fifo.sv", " D")],
        ),
        patch("booley.harness.git_utils.commit_scope") as commit,
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is True
    commit.assert_not_called()
    assert "rtl/legacy_fifo.sv" in block.call_args.args[1]


def test_done_but_dirty_scorer_files_get_distinct_report(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    state_path = _make_completed_state(tmp_path)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            return_value=[DirtyFile("rtl/other.sv", " M")],
        ),
        patch("booley.harness.git_utils.commit_scope"),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=2)

    assert blocked is True
    reason = block.call_args.args[1]
    assert reason.startswith("DONE_BUT_DIRTY")
    report = ctx.logs_dir / ".runtime" / "dirty_scorer_files.json"
    assert report.exists()
    assert "rtl/other.sv" in report.read_text(encoding="utf-8")


def test_deleted_files_under_new_scope_glob_do_not_block(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    ctx.scope_raw = ["rtl/dut.sv", "verif/lane1/*.sv [new]"]
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=[
                [
                    DirtyFile("rtl/dut.sv", " M"),
                    DirtyFile("verif/lane1/new_tb.sv", "??"),
                    DirtyFile("verif/lane1/old_tb.sv", " D"),
                ],
                [DirtyFile("verif/lane1/old_tb.sv", " D")],
            ],
        ),
        patch("booley.harness.git_utils.commit_scope", return_value=None) as commit,
        patch("booley.harness.developer.git_run", return_value=MagicMock(returncode=0, stdout="")),
        patch("booley.harness.developer.block_ticket") as block,
        patch("booley.harness.developer.terminal.raw"),
    ):
        blocked = _run_post_guardrails(ctx, state_path, run_index=0)

    assert blocked is False
    commit.assert_called_once_with(
        ctx.worktree_path,
        ["rtl/dut.sv", "verif/lane1/new_tb.sv"],
        "fix(leftover-edits): persist leftover edits (2 files)",
        literal=True,
    )
    block.assert_not_called()


def test_git_status_error_blocks_handoff(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    state_path = _make_state(tmp_path)

    with (
        patch(
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
            side_effect=GitStatusError("boom"),
        ),
        patch("booley.harness.git_utils.commit_scope") as commit,
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
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
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
            "booley.harness.developer_guardrails.check_uncommitted_code_statuses",
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


# ---------------------------------------------------------------------------
# Leftover-edit commit subject (F-52)
# ---------------------------------------------------------------------------


def _ctx_for_message(tmp_path: Path, **overrides) -> TicketContext:
    ctx = _make_ctx(tmp_path)
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def test_leftover_commit_subject_names_ticket_and_file_count(tmp_path: Path):
    """The catch-all commit must identify the ticket, not just say 'leftover'."""
    ctx = _ctx_for_message(tmp_path)
    msg = _leftover_commit_message(ctx, ["rtl/dut.sv", "rtl/other.sv"])
    assert msg == "fix(leftover-edits): persist leftover edits (2 files)"
    assert validate_message(msg) == []


def test_leftover_commit_type_follows_ticket_type(tmp_path: Path):
    ctx = _ctx_for_message(tmp_path)
    for ticket_type, expected in (
        ("feature", "feat"),
        ("bugfix", "fix"),
        ("refactor", "refactor"),
        ("verification", "test"),
        ("something-else", "chore"),
    ):
        ctx.ticket_type = ticket_type
        assert _leftover_commit_message(ctx, ["rtl/dut.sv"]).startswith(f"{expected}(")


def test_leftover_commit_subject_is_truncated_within_limit(tmp_path: Path):
    """A long ticket title must not overflow the subject-length rule."""
    ctx = _ctx_for_message(tmp_path, summary="x" * 300)
    msg = _leftover_commit_message(ctx, ["rtl/dut.sv"])
    summary = msg.split(": ", 1)[1]
    assert len(summary) <= MAX_SUMMARY_LEN
    assert summary.endswith("(1 file)")
    assert validate_message(msg) == []


def test_leftover_commit_falls_back_when_validation_fails(tmp_path: Path):
    """A ticket title must never be able to block its own handoff commit."""
    ctx = _ctx_for_message(tmp_path)
    with patch(
        "booley.dev_support.validate_commit_msg.validate_message",
        return_value=["nope"],
    ):
        assert _leftover_commit_message(ctx, ["rtl/dut.sv"]) == "fix: commit leftover edits"


def test_leftover_commit_drops_scope_for_unusable_slug(tmp_path: Path):
    """A slug with characters the scope grammar rejects degrades to no scope."""
    ctx = _ctx_for_message(tmp_path, slug="bad slug/with.chars")
    msg = _leftover_commit_message(ctx, ["rtl/dut.sv"])
    assert msg.startswith("fix: ")
    assert validate_message(msg) == []
