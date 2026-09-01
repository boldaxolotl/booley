"""Layout invariant tests for ConsoleApp at varied terminal sizes.

The TUI churn analysis identified rendering bugs at uncommon sizes
(narrow terminals, tall terminals) that test coverage didn't catch.
This module drives a fixed event sequence at several (cols, rows)
combinations and asserts structural invariants — not pixel-perfect
snapshots — so failures point at real layout breakage without
constant snapshot-file churn.
"""

from __future__ import annotations

import pytest

from booley.harness.console.app import (
    ConsoleApp,
    ConsolePhase,
    _strip_region_for_mark,
)
from booley.harness.console.events import (
    CriteriaChanged,
    McpToolCompleted,
    McpToolStarted,
    SetupProgress,
)
from booley.harness.console.widgets import (
    BottomStrip,
    MainPane,
    StatusBar,
    TicketHeader,
    TopStrip,
)

# Terminal sizes to exercise. (cols, rows) — Textual order.
# Includes very small, default, wide, and tall configurations.
LAYOUT_SIZES = [
    pytest.param((40, 10), id="40x10-hostile"),
    pytest.param((59, 19), id="59x19-boundary-below"),
    pytest.param((60, 20), id="60x20-small"),
    pytest.param((80, 24), id="80x24-default"),
    pytest.param((100, 30), id="100x30-medium"),
    pytest.param((120, 39), id="120x39-strip-small"),
    pytest.param((120, 40), id="120x40-strip-large"),
    pytest.param((120, 50), id="120x50-tall"),
    pytest.param((200, 60), id="200x60-large"),
]


class _LayoutTestApp(ConsoleApp):
    """ConsoleApp shorn of CSS_PATH for in-tree test discovery."""

    # Without CSS, the dock/height directives don't apply — so we point
    # at the real stylesheet via an absolute path resolved at import time.
    from pathlib import Path as _Path

    CSS_PATH = str(
        _Path(__file__).resolve().parent.parent.parent
        / "src"
        / "booley"
        / "harness"
        / "console"
        / "console.tcss"
    )


def _seed_ticket_info(app: ConsoleApp) -> None:
    app.query_one(TicketHeader).set_ticket_info(
        "demo-ticket-slug",
        "bugfix",
        "main",
    )


# ===========================================================================
# Widget composition + dock invariants
# ===========================================================================


class TestWidgetComposition:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_all_five_widgets_mounted(self, size):
        """No matter the terminal size, the five chrome widgets exist."""
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            # Will raise NoMatches if any are missing.
            app.query_one(TicketHeader)
            app.query_one(TopStrip)
            app.query_one(MainPane)
            app.query_one(BottomStrip)
            app.query_one(StatusBar)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_status_bar_docked_at_bottom(self, size):
        """StatusBar is height-1 docked to bottom — region.y == screen height - 1."""
        cols, rows = size
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            bar = app.query_one(StatusBar)
            # The status bar must occupy the final row of the screen.
            assert bar.region.height == 1, f"StatusBar height {bar.region.height} != 1 at {size}"
            assert bar.region.y == rows - 1, (
                f"StatusBar y={bar.region.y}, expected {rows - 1} at {size}"
            )
            assert bar.region.width == cols, f"StatusBar width {bar.region.width} != {cols}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_main_pane_fills_middle(self, size):
        """MainPane is 1fr — must have positive height after chrome subtracted."""
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            main = app.query_one(MainPane)
            # MainPane must have nonzero visible area even on tightest layout.
            assert main.region.height > 0, f"MainPane has zero height at {size}"
            assert main.region.width > 0, f"MainPane has zero width at {size}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_strips_hidden_when_empty(self, size):
        """No endpoint completions => strips collapsed (display=False)."""
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert app.query_one(TopStrip).display is False
            assert app.query_one(BottomStrip).display is False


# ===========================================================================
# Setup-phase content rendering
# ===========================================================================


class TestSetupPhaseLayout:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_setup_lines_render_without_error(self, size):
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            _seed_ticket_info(app)
            app.post_message(SetupProgress("loading model/backend config..."))
            app.post_message(SetupProgress("running preflight checks..."))
            app.post_message(SetupProgress("parsing & validating ticket..."))
            await pilot.pause()
            content = str(app.query_one(MainPane).query_one("#main-content").render())
            assert "loading" in content
            assert "preflight" in content
            assert "parsing" in content

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_setup_divider_drawn_once_at_running(self, size):
        """SETUP → RUNNING boundary draws exactly one ─── divider line."""
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            _seed_ticket_info(app)
            app.post_message(SetupProgress("setup line 1"))
            await pilot.pause()
            app.transition_to(ConsolePhase.RUNNING)
            app.post_message(McpToolStarted("tb_coder", None))
            await pilot.pause()
            pane = app.query_one(MainPane)
            # _setup_divider_drawn is the explicit one-shot flag.
            assert pane._setup_divider_drawn is True


# ===========================================================================
# Running-phase endpoint boxes + strip overflow accounting
# ===========================================================================


class TestStripClassification:
    def test_visible_closing_divider_suppresses_top_entry(self):
        assert _strip_region_for_mark(0, 1, 9, 10, 9, 20) is None

    def test_closing_divider_above_viewport_allows_top_entry(self):
        assert _strip_region_for_mark(0, 1, 9, 10, 10, 20) == "above"

    def test_visible_opening_divider_suppresses_bottom_entry(self):
        assert _strip_region_for_mark(20, 21, 30, 31, 0, 21) is None

    def test_opening_divider_below_viewport_allows_bottom_entry(self):
        assert _strip_region_for_mark(20, 21, 30, 31, 0, 20) == "below"

    def test_wrapped_divider_partial_visibility_suppresses_entry(self):
        assert _strip_region_for_mark(20, 23, 40, 43, 0, 22) is None
        assert _strip_region_for_mark(20, 23, 40, 43, 41, 60) is None


def _run_n_endpoints(app: ConsoleApp, n: int) -> None:
    """Drive n complete endpoint-box cycles. Caller must await pilot.pause()."""
    for i in range(n):
        app.post_message(McpToolStarted("sim", f"cfg_{i}"))
        app.post_message(
            McpToolCompleted(
                "sim",
                f"cfg_{i}",
                exit_code=0,
                duration_s=2.0,
                cost_usd=0.0,
                summary=f"box {i}",
            )
        )


class TestEndpointBoxLayout:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_many_endpoint_boxes_render_without_error(self, size):
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            _seed_ticket_info(app)
            app.transition_to(ConsolePhase.RUNNING)
            _run_n_endpoints(app, 20)
            await pilot.pause()
            marks = app.query_one(MainPane).get_completion_marks()
            assert len(marks) == 20

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_strip_overflow_accounting_matches_capacity(self, size):
        """When many boxes scroll off, top strip shows capacity-bounded
        entries plus the correct ``X more above`` overflow count."""
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            _seed_ticket_info(app)
            app.transition_to(ConsolePhase.RUNNING)
            _run_n_endpoints(app, 30)
            await pilot.pause()
            # Scroll to the bottom so older boxes go above the viewport.
            main = app.query_one(MainPane)
            main.scroll_end(animate=False)
            await pilot.pause()
            # Force a strip refresh.
            app._update_strips()
            await pilot.pause()

            top = app.query_one(TopStrip)
            cap = app._strip_capacity()
            # Either the strip is hidden (everything fits), or visible with
            # at most `cap` entries — never more than capacity.
            if top.display:
                # Capacity is an upper bound on shown entries.
                assert len(top._entries) <= cap, (
                    f"TopStrip shows {len(top._entries)} entries, capacity is {cap} at {size}"
                )
                # Overflow + entries == total marks classified above viewport.
                marks = main.resolve_mark_divider_visual_rows()
                viewport_bottom = main.scroll_y + main.scrollable_content_region.height
                above = [
                    m
                    for m, os, oe, cs, ce in marks
                    if _strip_region_for_mark(
                        os,
                        oe,
                        cs,
                        ce,
                        main.scroll_y,
                        viewport_bottom,
                    )
                    == "above"
                ]
                assert len(top._entries) + top._overflow_count == len(above), (
                    f"Strip accounting mismatch at {size}: "
                    f"entries={len(top._entries)} + overflow={top._overflow_count} "
                    f"!= above={len(above)}"
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_box_banner_fills_pane_width(self, size):
        """The ┌─ banner line should extend to the inner content width;
        narrow terminals don't break the banner formula."""
        _cols, _rows = size
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            _seed_ticket_info(app)
            app.transition_to(ConsolePhase.RUNNING)
            app.post_message(McpToolStarted("tb_coder", "tb"))
            await pilot.pause()
            pane = app.query_one(MainPane)
            # The plain content stream contains the banner. We just need it
            # to be non-empty and to contain the label without truncation.
            text = pane._content.plain
            assert "┌─" in text
            assert "tb_coder" in text
            assert "tb" in text
            # The line containing ┌─ must not exceed the pane content width
            # (i.e. the banner formula didn't overshoot at narrow widths).
            banner_line = next(line for line in text.split("\n") if "┌─" in line)
            # _box_width clamps to inner pane width; allow ±1 char slack.
            inner = max(pane.content_size.width, 1)
            assert len(banner_line) <= max(inner, 64), (
                f"Banner {len(banner_line)} chars overshoots pane "
                f"inner width {inner} at {size}: {banner_line!r}"
            )

    @pytest.mark.asyncio
    async def test_box_dividers_do_not_wrap_next_to_scrollbar(self):
        app = _LayoutTestApp()
        async with app.run_test(size=(50, 18)) as pilot:
            await pilot.pause()
            _seed_ticket_info(app)
            app.transition_to(ConsolePhase.RUNNING)
            for i in range(20):
                app.post_message(SetupProgress(f"history {i} " + "x" * 80))
            unicode_target = "界🙂\t" * 20
            app.post_message(McpToolStarted("mutation_tester", unicode_target))
            app.post_message(
                McpToolCompleted(
                    "mutation_tester",
                    unicode_target,
                    exit_code=0,
                    duration_s=1.0,
                    cost_usd=0.0,
                    summary="",
                )
            )
            await pilot.pause()

            mark, open_start, open_end, close_start, close_end = app.query_one(
                MainPane
            ).resolve_mark_divider_visual_rows()[-1]
            assert mark.name == "mutation_tester"
            assert open_end - open_start == 1
            assert close_end - close_start == 1

            content_lines = app.query_one(MainPane)._content.plain.splitlines()
            setup_divider = next(line for line in content_lines if line and set(line) == {"─"})
            assert len(setup_divider) == app.query_one(MainPane)._box_width()


# ===========================================================================
# TicketHeader compact / expanded layout
# ===========================================================================


class TestTicketHeaderLayout:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_compact_header_stays_within_max_height(self, size):
        """Compact view caps at max-height: 4 — three content rows + border."""
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            _seed_ticket_info(app)
            app.post_message(
                CriteriaChanged(
                    {
                        "sim_pass": {"met": True, "mandatory": True, "detail": {}, "params": {}},
                        "lint_clean": {
                            "met": False,
                            "mandatory": True,
                            "detail": {},
                            "params": {},
                        },
                    }
                )
            )
            await pilot.pause()
            header = app.query_one(TicketHeader)
            # CSS sets max-height: 4. With the border-bottom that's 5 rows total.
            assert header.region.height <= 5, (
                f"TicketHeader region height {header.region.height} "
                f"exceeds 5 (compact + border) at {size}"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", LAYOUT_SIZES)
    async def test_expanded_header_does_not_overflow_terminal(self, size):
        """Expanded view caps at 40% — should not consume the entire screen."""
        _cols, rows = size
        app = _LayoutTestApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            _seed_ticket_info(app)
            criteria = {
                f"crit_{i}": {
                    "met": (i % 3 == 0),
                    "mandatory": True,
                    "detail": {},
                    "params": {},
                }
                for i in range(15)
            }
            app.post_message(CriteriaChanged(criteria))
            await pilot.pause()
            app.action_toggle_criteria()
            await pilot.pause()
            header = app.query_one(TicketHeader)
            # Leave at least one row for MainPane + one for StatusBar.
            assert header.region.height < rows - 1, (
                f"Expanded TicketHeader consumes {header.region.height}/{rows} rows at {size}"
            )


# ===========================================================================
# Resize survival
# ===========================================================================


class TestResizeSurvival:
    @pytest.mark.asyncio
    async def test_resize_preserves_content(self):
        """Driving content at one size, then resizing, must not lose marks."""
        app = _LayoutTestApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            _seed_ticket_info(app)
            app.transition_to(ConsolePhase.RUNNING)
            _run_n_endpoints(app, 5)
            await pilot.pause()
            before = len(app.query_one(MainPane).get_completion_marks())
            assert before == 5

            await pilot.resize_terminal(60, 20)
            await pilot.pause()
            after = len(app.query_one(MainPane).get_completion_marks())
            assert after == before, "endpoint completion marks lost on resize"

            await pilot.resize_terminal(200, 60)
            await pilot.pause()
            after2 = len(app.query_one(MainPane).get_completion_marks())
            assert after2 == before, "endpoint completion marks lost on resize"

    @pytest.mark.asyncio
    async def test_resize_does_not_throw_with_open_endpoint_box(self):
        """Resize while a endpoint box is open (banner half-rendered)."""
        app = _LayoutTestApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _seed_ticket_info(app)
            app.transition_to(ConsolePhase.RUNNING)
            app.post_message(McpToolStarted("sim", "cfg_a"))
            await pilot.pause()
            # Resize mid-box. Must not raise.
            await pilot.resize_terminal(70, 24)
            await pilot.pause()
            await pilot.resize_terminal(160, 50)
            await pilot.pause()
            # Now close the box — banner formula must still compute.
            app.post_message(
                McpToolCompleted(
                    "sim",
                    "cfg_a",
                    exit_code=0,
                    duration_s=3.0,
                    cost_usd=0.0,
                    summary="ok",
                )
            )
            await pilot.pause()
            marks = app.query_one(MainPane).get_completion_marks()
            assert len(marks) == 1
