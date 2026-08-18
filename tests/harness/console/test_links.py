"""Tests for harness.console.links — click resolver + invocation."""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.harness._editor_config import VSCODE_EDITOR, ResolvedEditor
from booley.harness.console.links import (
    LinkContext,
    LinkTarget,
    ResolvedAction,
    _empty_tempfile,
    _materialize_fork_base,
    invoke,
    resolve,
)

# ---------------------------------------------------------------------------
# Resolution rules table — the core of Phase 2.2
# ---------------------------------------------------------------------------


@pytest.fixture
def workdirs(tmp_path: Path) -> tuple[Path, Path]:
    """Create a project root + worktree root pair, both with .git markers."""
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    project.mkdir()
    worktree.mkdir()
    return project, worktree


@pytest.fixture
def ctx(workdirs: tuple[Path, Path]) -> LinkContext:
    project, worktree = workdirs
    return LinkContext(
        project_root=project,
        worktree_root=worktree,
        fork_base_sha="abc123",
        rtl_dirs=("rtl/",),
        tb_dirs=("verif/",),
    )


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestResolveFile:
    def test_rtl_under_both_yields_diff(
        self,
        ctx: LinkContext,
        workdirs: tuple[Path, Path],
    ):
        """RTL source present in both worktree and project → diff action."""
        project, worktree = workdirs
        _write(project / "rtl/fifo.sv", "old")
        _write(worktree / "rtl/fifo.sv", "new")
        with patch(
            "booley.harness.console.links._materialize_fork_base",
            return_value=Path("/tmp/base-copy.sv"),
        ):
            action = resolve(LinkTarget(kind="file", raw="rtl/fifo.sv"), ctx)
        assert action.kind == "diff"
        # worktree copy is the "right side" (current state), fork-base is left.
        # We pass (worktree, base) — invoke() decides which is left/right.
        assert action.args[0] == str((worktree / "rtl/fifo.sv").resolve())

    def test_rtl_only_in_worktree_yields_diff_against_empty(
        self,
        ctx: LinkContext,
        workdirs: tuple[Path, Path],
    ):
        """Newly-created RTL file (no project copy) → diff vs empty file."""
        _, worktree = workdirs
        _write(worktree / "rtl/new.sv", "fresh")
        action = resolve(LinkTarget(kind="file", raw="rtl/new.sv"), ctx)
        assert action.kind == "diff"
        # Two args: worktree path + empty tempfile path.
        assert len(action.args) == 2
        assert Path(action.args[1]).exists()
        assert Path(action.args[1]).read_text() == ""

    def test_plan_in_project_yields_open(
        self,
        ctx: LinkContext,
        workdirs: tuple[Path, Path],
    ):
        """Non-source file in project (plan/spec/ADR) → open."""
        project, _ = workdirs
        _write(project / "docs/plan.md", "...")
        action = resolve(LinkTarget(kind="file", raw="docs/plan.md"), ctx)
        assert action.kind == "open"
        assert action.args == (str((project / "docs/plan.md").resolve()),)

    def test_open_at_line_when_line_captured(
        self,
        ctx: LinkContext,
        workdirs: tuple[Path, Path],
    ):
        """Project-only file with a line number → open_at_line."""
        project, _ = workdirs
        _write(project / "docs/plan.md", "...")
        action = resolve(
            LinkTarget(kind="file", raw="docs/plan.md", line=42),
            ctx,
        )
        assert action.kind == "open_at_line"
        assert action.line == 42

    def test_post_cleanup_diff_degrades_to_open(
        self,
        ctx: LinkContext,
        workdirs: tuple[Path, Path],
    ):
        """Worktree gone → RTL file diff degrades to plain-open of project copy."""
        project, _ = workdirs
        _write(project / "rtl/fifo.sv", "old")
        ctx.worktree_root = None
        action = resolve(LinkTarget(kind="file", raw="rtl/fifo.sv"), ctx)
        assert action.kind == "open"
        assert action.args == (str((project / "rtl/fifo.sv").resolve()),)

    def test_post_cleanup_no_project_copy_yields_none(
        self,
        ctx: LinkContext,
    ):
        """Both worktree gone and project file missing → ``none`` with hint."""
        ctx.worktree_root = None
        action = resolve(LinkTarget(kind="file", raw="rtl/ghost.sv"), ctx)
        assert action.kind == "none"
        assert "not found" in action.hint

    def test_non_source_in_worktree_yields_open(
        self,
        ctx: LinkContext,
        workdirs: tuple[Path, Path],
    ):
        """Worktree-side non-RTL file (e.g. a debug script) → plain open."""
        _, worktree = workdirs
        _write(worktree / "scripts/debug.py", "...")
        action = resolve(LinkTarget(kind="file", raw="scripts/debug.py"), ctx)
        assert action.kind == "open"
        assert action.args == (str((worktree / "scripts/debug.py").resolve()),)


class TestSandboxPrefixStripping:
    def test_strips_work_prefix(
        self,
        ctx: LinkContext,
        workdirs: tuple[Path, Path],
    ):
        """``/work/rtl/fifo.sv`` from a sandboxed EDA tool → resolves on host."""
        project, worktree = workdirs
        _write(project / "rtl/fifo.sv", "x")
        _write(worktree / "rtl/fifo.sv", "y")
        ctx.sandbox_mount_prefix = "/work"
        with patch(
            "booley.harness.console.links._materialize_fork_base",
            return_value=Path("/tmp/base-copy.sv"),
        ):
            action = resolve(
                LinkTarget(kind="file", raw="/work/rtl/fifo.sv"),
                ctx,
            )
        assert action.kind == "diff"

    def test_strips_with_trailing_slash(
        self,
        ctx: LinkContext,
        workdirs: tuple[Path, Path],
    ):
        ctx.sandbox_mount_prefix = "/work/"  # tolerated
        project, _ = workdirs
        _write(project / "docs/x.md", "x")
        action = resolve(LinkTarget(kind="file", raw="/work/docs/x.md"), ctx)
        assert action.kind == "open"


class TestResolveTicket:
    def test_ticket_resolves_to_current_path(
        self,
        ctx: LinkContext,
        tmp_path: Path,
    ):
        """find_ticket_file is called at click time; we pass through its path."""
        ticket_path = tmp_path / "tickets/board/active/foo.md"
        _write(ticket_path)
        ctx.tickets_dir = tmp_path / "tickets"
        ctx.find_ticket_file = MagicMock(return_value=(ticket_path, "active"))
        action = resolve(LinkTarget(kind="ticket", raw="foo"), ctx)
        ctx.find_ticket_file.assert_called_once_with(
            ctx.tickets_dir,
            "foo",
        )
        assert action.kind == "open"
        assert action.args == (str(ticket_path),)

    def test_missing_ticket_yields_none(self, ctx: LinkContext, tmp_path: Path):
        ctx.tickets_dir = tmp_path
        ctx.find_ticket_file = MagicMock(return_value=(None, None))
        action = resolve(LinkTarget(kind="ticket", raw="ghost"), ctx)
        assert action.kind == "none"
        assert "ghost" in action.hint

    def test_no_board_wiring_yields_none(self, ctx: LinkContext):
        # find_ticket_file/tickets_dir both None — Console wasn't wired
        # to the board (e.g. running in a non-project dir).
        action = resolve(LinkTarget(kind="ticket", raw="x"), ctx)
        assert action.kind == "none"


class TestFalsePositiveGuard:
    def test_token_outside_project_returns_none(
        self,
        ctx: LinkContext,
    ):
        """A token like ``foo.bar`` that resolves nowhere → ``none``."""
        action = resolve(LinkTarget(kind="file", raw="foo.bar"), ctx)
        assert action.kind == "none"


# ---------------------------------------------------------------------------
# Fork-base materialization
# ---------------------------------------------------------------------------


class TestMaterializeForkBase:
    def test_writes_git_show_output_to_tempfile(self, ctx: LinkContext):
        proc = MagicMock(returncode=0, stdout=b"module fifo;\nendmodule\n")
        with patch(
            "booley.harness.console.links.subprocess.run",
            return_value=proc,
        ):
            tmp = _materialize_fork_base("rtl/fifo.sv", ctx)
        assert tmp is not None
        assert tmp.exists()
        assert tmp.read_bytes() == b"module fifo;\nendmodule\n"
        # Cached on second call (no second subprocess.run needed).
        with patch(
            "booley.harness.console.links.subprocess.run",
            side_effect=AssertionError("should not call again"),
        ):
            tmp2 = _materialize_fork_base("rtl/fifo.sv", ctx)
        assert tmp2 == tmp

    def test_git_show_failure_returns_none(self, ctx: LinkContext):
        proc = MagicMock(returncode=128, stdout=b"", stderr=b"unknown ref")
        with patch(
            "booley.harness.console.links.subprocess.run",
            return_value=proc,
        ):
            assert _materialize_fork_base("rtl/ghost.sv", ctx) is None

    def test_git_missing_returns_none(self, ctx: LinkContext):
        with patch(
            "booley.harness.console.links.subprocess.run",
            side_effect=FileNotFoundError("git"),
        ):
            assert _materialize_fork_base("rtl/x.sv", ctx) is None

    def test_no_fork_base_sha_returns_none(self, ctx: LinkContext):
        ctx.fork_base_sha = None
        assert _materialize_fork_base("rtl/x.sv", ctx) is None


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


class TestInvoke:
    def test_open_substitutes_file_placeholder(self):
        editor = VSCODE_EDITOR
        with patch(
            "booley.harness.console.links.subprocess.Popen",
        ) as mock_popen:
            result = invoke(
                ResolvedAction(kind="open", args=("/tmp/a.sv",)),
                editor,
            )
        assert result.ok
        argv = mock_popen.call_args.args[0]
        assert argv == ["code", "--goto", "/tmp/a.sv"]

    def test_open_at_line_substitutes_file_and_line(self):
        editor = VSCODE_EDITOR
        with patch(
            "booley.harness.console.links.subprocess.Popen",
        ) as mock_popen:
            result = invoke(
                ResolvedAction(
                    kind="open_at_line",
                    args=("/tmp/a.sv",),
                    line=42,
                ),
                editor,
            )
        assert result.ok
        argv = mock_popen.call_args.args[0]
        assert argv == ["code", "--goto", "/tmp/a.sv:42"]

    def test_diff_substitutes_left_right(self):
        editor = VSCODE_EDITOR
        with patch(
            "booley.harness.console.links.subprocess.Popen",
        ) as mock_popen:
            result = invoke(
                ResolvedAction(
                    kind="diff",
                    args=("/tmp/wt.sv", "/tmp/base.sv"),
                ),
                editor,
            )
        assert result.ok
        argv = mock_popen.call_args.args[0]
        assert argv == ["code", "--diff", "/tmp/wt.sv", "/tmp/base.sv"]

    def test_diff_degrades_when_editor_has_no_diff(self):
        """Editor without diff support → diff degrades to open of left side."""
        editor = ResolvedEditor(
            open=("ed", "{file}"),
            open_at_line=("ed", "{file}:{line}"),
            diff=None,
        )
        assert editor.diff is None
        with patch(
            "booley.harness.console.links.subprocess.Popen",
        ) as mock_popen:
            result = invoke(
                ResolvedAction(
                    kind="diff",
                    args=("/tmp/wt.sv", "/tmp/base.sv"),
                ),
                editor,
            )
        assert result.ok
        argv = mock_popen.call_args.args[0]
        assert argv == ["ed", "/tmp/wt.sv"]

    def test_none_action_returns_hint(self):
        editor = VSCODE_EDITOR
        result = invoke(
            ResolvedAction(kind="none", hint="nope"),
            editor,
        )
        assert not result.ok
        assert result.hint == "nope"

    def test_editor_not_found_returns_hint(self):
        editor = VSCODE_EDITOR
        with patch(
            "booley.harness.console.links.subprocess.Popen",
            side_effect=FileNotFoundError("code"),
        ):
            result = invoke(
                ResolvedAction(kind="open", args=("/tmp/a",)),
                editor,
            )
        assert not result.ok
        assert "editor not found" in result.hint

    def test_popen_oserror_returns_hint(self):
        editor = VSCODE_EDITOR
        with patch(
            "booley.harness.console.links.subprocess.Popen",
            side_effect=OSError("perm denied"),
        ):
            result = invoke(
                ResolvedAction(kind="open", args=("/tmp/a",)),
                editor,
            )
        assert not result.ok
        assert "perm denied" in result.hint


class TestEmptyTempfile:
    def test_creates_empty_file(self):
        p = _empty_tempfile()
        try:
            assert p.exists()
            assert p.read_bytes() == b""
        finally:
            with contextlib.suppress(OSError):
                p.unlink()
