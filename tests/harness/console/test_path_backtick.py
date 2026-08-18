"""Tests for harness.console.path_backtick — raw-path pre-processor."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.harness.console.links import LinkContext
from booley.harness.console.path_backtick import wrap_paths_in_backticks


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    (p / "rtl").mkdir()
    (p / "rtl" / "fifo.sv").write_text("// rtl")
    (p / "verif").mkdir()
    (p / "verif" / "tb_fifo.sv").write_text("// tb")
    return p


@pytest.fixture
def ctx(project: Path) -> LinkContext:
    return LinkContext(
        project_root=project,
        worktree_root=None,
        fork_base_sha=None,
        rtl_dirs=("rtl/",),
        tb_dirs=("verif/",),
    )


class TestWrapPaths:
    def test_wraps_resolvable_path_with_line(self, ctx: LinkContext):
        line = "rtl/fifo.sv:42: syntax error"
        out = wrap_paths_in_backticks(line, ctx)
        assert out == "`rtl/fifo.sv:42`: syntax error"

    def test_wraps_path_with_line_and_col(self, ctx: LinkContext):
        # The col index is dropped from the wrapped token; line is enough
        # for the editor to anchor on.
        line = "rtl/fifo.sv:42:7: bad token"
        out = wrap_paths_in_backticks(line, ctx)
        assert out.startswith("`rtl/fifo.sv:42`")

    def test_leaves_non_resolving_token_alone(self, ctx: LinkContext):
        # foo.bar doesn't exist under project/worktree — left untouched.
        out = wrap_paths_in_backticks("foo.bar:99: nope", ctx)
        assert out == "foo.bar:99: nope"

    def test_leaves_identifier_alone(self, ctx: LinkContext):
        # No path-line shape (no extension before the colon).
        out = wrap_paths_in_backticks("error in clk: signal", ctx)
        assert out == "error in clk: signal"

    def test_leaves_already_backticked_alone(self, ctx: LinkContext):
        # Negative lookbehind in the regex skips tokens that already
        # follow a backtick (e.g. when the renderer re-wraps).
        line = "`rtl/fifo.sv:42`: oops"
        out = wrap_paths_in_backticks(line, ctx)
        # Idempotent: rerunning the wrap doesn't double-wrap.
        assert out == line

    def test_handles_multiple_paths_in_one_line(self, ctx: LinkContext):
        line = "rtl/fifo.sv:1 and verif/tb_fifo.sv:2"
        out = wrap_paths_in_backticks(line, ctx)
        assert "`rtl/fifo.sv:1`" in out
        assert "`verif/tb_fifo.sv:2`" in out


class TestSandboxPrefixStripping:
    def test_strips_work_prefix(self, ctx: LinkContext):
        ctx.sandbox_mount_prefix = "/work"
        out = wrap_paths_in_backticks("/work/rtl/fifo.sv:42: error", ctx)
        # Stripped to project-relative before wrapping.
        assert out == "`rtl/fifo.sv:42`: error"

    def test_strips_prefix_with_trailing_slash(self, ctx: LinkContext):
        ctx.sandbox_mount_prefix = "/work/"
        out = wrap_paths_in_backticks("/work/rtl/fifo.sv:1", ctx)
        assert out == "`rtl/fifo.sv:1`"

    def test_unstripped_absolute_path_left_alone(self, ctx: LinkContext):
        # An absolute path that isn't under the configured mount prefix
        # and isn't a real file falls through.
        out = wrap_paths_in_backticks(
            "/not/a/real/file.sv:42: error",
            ctx,
        )
        assert out == "/not/a/real/file.sv:42: error"


class TestEdgeCases:
    def test_empty_line(self, ctx: LinkContext):
        assert wrap_paths_in_backticks("", ctx) == ""

    def test_no_paths(self, ctx: LinkContext):
        assert wrap_paths_in_backticks("just text", ctx) == "just text"

    def test_path_without_line_number_left_alone(self, ctx: LinkContext):
        # The regex requires :digit after the extension, so bare paths
        # in prose don't match (they should be backticked by the LLM
        # if intended as links).
        assert wrap_paths_in_backticks("see rtl/fifo.sv", ctx) == "see rtl/fifo.sv"
