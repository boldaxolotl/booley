"""Tests for harness.render_md Rich-mode rendering (clickable links)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.harness.console.links import LinkContext, LinkTarget
from booley.harness.render_md import inline_rich


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    (p / "rtl").mkdir()
    (p / "rtl" / "fifo.sv").write_text("// rtl")
    (p / "docs").mkdir()
    (p / "docs" / "plan.md").write_text("# plan")
    return p


@pytest.fixture
def ctx(project: Path) -> LinkContext:
    return LinkContext(
        project_root=project,
        worktree_root=None,
        fork_base_sha=None,
        rtl_dirs=("rtl/",),
        tb_dirs=(),
    )


def _spans_with_meta(text):
    """Return list of (start, end, meta_dict) for spans carrying meta."""
    out = []
    for span in text.spans:
        meta = getattr(span.style, "meta", None) if hasattr(span.style, "meta") else None
        if meta:
            out.append((span.start, span.end, meta))
    return out


class TestInlineRichNoContext:
    def test_returns_plain_text(self):
        """link_ctx=None preserves the historic ANSI semantics."""
        t = inline_rich("plain text")
        assert t.plain == "plain text"

    def test_backtick_becomes_cyan_without_meta(self):
        """No link context → backtick is styled but not clickable."""
        t = inline_rich("see `code`")
        # The backticks themselves are stripped, just like ANSI mode.
        assert t.plain == "see code"
        # No meta on any span.
        assert _spans_with_meta(t) == []


class TestInlineRichWithContext:
    def test_resolvable_path_carries_link_target(self, ctx: LinkContext):
        t = inline_rich("see `rtl/fifo.sv` for details", ctx)
        assert t.plain == "see rtl/fifo.sv for details"
        spans = _spans_with_meta(t)
        assert len(spans) == 1
        _, _, meta = spans[0]
        target: LinkTarget = meta["booley_target"]
        assert target.kind == "file"
        assert target.raw == "rtl/fifo.sv"
        assert target.line is None

    def test_path_with_line_extracts_line(self, ctx: LinkContext):
        t = inline_rich("see `rtl/fifo.sv:42` line", ctx)
        spans = _spans_with_meta(t)
        assert len(spans) == 1
        target = spans[0][2]["booley_target"]
        assert target.kind == "file"
        assert target.raw == "rtl/fifo.sv"
        assert target.line == 42

    def test_unresolvable_token_falls_back_to_plain_style(self, ctx: LinkContext):
        # `clk` is not a real file; render with style but no meta.
        t = inline_rich("signal `clk` toggles", ctx)
        assert _spans_with_meta(t) == []

    def test_ticket_sentinel_yields_ticket_target(self, ctx: LinkContext):
        t = inline_rich("active `ticket:fifo_basic` now", ctx)
        # Displayed text is the slug (no ``ticket:`` prefix).
        assert t.plain == "active fifo_basic now"
        spans = _spans_with_meta(t)
        assert len(spans) == 1
        target = spans[0][2]["booley_target"]
        assert target.kind == "ticket"
        assert target.raw == "fifo_basic"

    def test_multiple_links_in_one_line(self, ctx: LinkContext):
        t = inline_rich("`rtl/fifo.sv:1` and `docs/plan.md`", ctx)
        spans = _spans_with_meta(t)
        assert len(spans) == 2
        kinds = {s[2]["booley_target"].kind for s in spans}
        assert kinds == {"file"}
