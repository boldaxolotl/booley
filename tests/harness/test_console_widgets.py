"""Tests for Console widgets — vertical log view redesign.

Uses Textual's App.run_test() for headless widget testing.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from booley.harness.console.widgets import (
    BottomStrip,
    MainPane,
    McpToolCompletionMark,
    StatusBar,
    TicketHeader,
    TopStrip,
    _format_dut_info,
    _render_entry_line,
)

# ===========================================================================
# Test app harness — minimal app for testing individual widgets
# ===========================================================================


class MainPaneTestApp(App):
    def compose(self) -> ComposeResult:
        yield MainPane()


@pytest.mark.asyncio
async def test_main_pane_shows_compact_file_edit_summary():
    async with MainPaneTestApp().run_test() as pilot:
        pane = pilot.app.query_one(MainPane)
        pane.append_file_edits(
            ["rtl/top.sv", "tb/top_tb.sv"],
            {"rtl/top.sv": (12, 3), "tb/top_tb.sv": (5, 0)},
        )
        await pilot.pause()

        content = pane._content.plain
        assert "edited rtl/top.sv  total +12 -3" in content
        assert "edited tb/top_tb.sv  total +5 -0" in content


@pytest.mark.asyncio
async def test_main_pane_keeps_file_edit_when_counts_are_unavailable():
    async with MainPaneTestApp().run_test() as pilot:
        pane = pilot.app.query_one(MainPane)
        pane.append_file_edits(["rtl/top.sv"])
        await pilot.pause()

        assert "edited rtl/top.sv" in pane._content.plain


class TicketHeaderTestApp(App):
    def compose(self) -> ComposeResult:
        yield TicketHeader()


class TopStripTestApp(App):
    def compose(self) -> ComposeResult:
        yield TopStrip()


class BottomStripTestApp(App):
    def compose(self) -> ComposeResult:
        yield BottomStrip()


class StatusBarTestApp(App):
    def compose(self) -> ComposeResult:
        yield StatusBar()


# ===========================================================================
# _format_dut_info — pure helper
# ===========================================================================


class TestFormatDutInfo:
    def test_both_present(self):
        s = _format_dut_info({"dut_top_module": "fifo", "tb_top_module": "tb_fifo"})
        assert s == "DUT: fifo  |  TB: tb_fifo"

    def test_dut_missing(self):
        """An unplanned slot is omitted, not placeheld."""
        s = _format_dut_info({"tb_top_module": "tb_fifo"})
        assert s == "TB: tb_fifo"

    def test_tb_missing(self):
        s = _format_dut_info({"dut_top_module": "fifo"})
        assert s == "DUT: fifo"

    def test_both_missing(self):
        """Nothing known yet → no line at all (callers skip on empty)."""
        assert _format_dut_info({}) == ""

    def test_none(self):
        assert _format_dut_info(None) == ""


# ===========================================================================
# MainPane
# ===========================================================================


class TestMainPane:
    @pytest.mark.asyncio
    async def test_setup_line(self):
        async with MainPaneTestApp().run_test() as pilot:
            pane = pilot.app.query_one(MainPane)
            pane.append_setup_line("Creating worktree...")
            await pilot.pause()
            content = str(pane.query_one("#main-content").render())
            assert "worktree" in content

    @pytest.mark.asyncio
    async def test_append_thinking_accumulates(self):
        async with MainPaneTestApp().run_test() as pilot:
            pane = pilot.app.query_one(MainPane)
            pane.append_thinking("Analyzing the design...")
            pane.append_thinking("Checking constraints...")
            await pilot.pause()
            content = str(pane.query_one("#main-content").render())
            assert "Analyzing" in content
            assert "Checking" in content

    @pytest.mark.asyncio
    async def test_open_endpoint_box(self):
        async with MainPaneTestApp().run_test() as pilot:
            pane = pilot.app.query_one(MainPane)
            pane.open_endpoint_box("tb_coder", "tb")
            await pilot.pause()
            content = str(pane.query_one("#main-content").render())
            assert "tb_coder" in content
            assert "tb" in content
            assert "┌" in content

    @pytest.mark.asyncio
    async def test_reviewer_endpoint_box_uses_reviewer_style_and_safe_width(self):
        async with MainPaneTestApp().run_test() as pilot:
            pane = pilot.app.query_one(MainPane)
            pane.open_endpoint_box("reviewer", "setup")
            await pilot.pause()

            content = pane.query_one("#main-content").render()
            assert "reviewer" in str(content)
            assert pane._endpoint_style == "color(183)"
            assert pane._box_width() == pane.scrollable_content_region.width

    @pytest.mark.asyncio
    async def test_elaboration_endpoint_uses_simulation_flow_style(self):
        async with MainPaneTestApp().run_test() as pilot:
            pane = pilot.app.query_one(MainPane)
            pane.open_endpoint_box("sim", "default")
            simulation_style = pane._endpoint_style
            pane.close_endpoint_box("sim", "default", 0, 1.0, 0.0, None)
            pane.open_endpoint_box("elab", "default")
            await pilot.pause()

            assert simulation_style == "color(75)"
            assert pane._endpoint_style == simulation_style

    @pytest.mark.asyncio
    async def test_close_endpoint_box(self):
        async with MainPaneTestApp().run_test() as pilot:
            pane = pilot.app.query_one(MainPane)
            pane.open_endpoint_box("lint", None)
            pane.close_endpoint_box("lint", None, 0, 4.0, 0.0, None, summary="clean")
            await pilot.pause()
            content = str(pane.query_one("#main-content").render())
            assert "└" in content
            assert "✓" in content

    @pytest.mark.asyncio
    async def test_close_endpoint_box_with_display_lines(self):
        async with MainPaneTestApp().run_test() as pilot:
            pane = pilot.app.query_one(MainPane)
            pane.open_endpoint_box("sim", "default")
            pane.close_endpoint_box(
                "sim",
                "default",
                1,
                12.0,
                0.05,
                ["Error: timeout on clk", "3 assertions failed"],
                summary="3 errors",
            )
            await pilot.pause()
            content = str(pane.query_one("#main-content").render())
            assert "timeout on clk" in content
            assert "3 assertions failed" in content

    @pytest.mark.asyncio
    async def test_get_completion_marks(self):
        async with MainPaneTestApp().run_test() as pilot:
            pane = pilot.app.query_one(MainPane)
            pane.open_endpoint_box("lint", None)
            pane.close_endpoint_box("lint", None, 0, 4.0, 0.0, None, summary="clean")
            pane.open_endpoint_box("sim", "cfg_a")
            pane.close_endpoint_box("sim", "cfg_a", 1, 18.0, 0.05, None, summary="fail")
            await pilot.pause()
            marks = pane.get_completion_marks()
            assert len(marks) == 2
            assert marks[0].name == "lint"
            assert marks[1].name == "sim"
            assert marks[1].exit_code == 1

    @pytest.mark.asyncio
    async def test_content_accumulates_across_endpoints(self):
        async with MainPaneTestApp().run_test() as pilot:
            pane = pilot.app.query_one(MainPane)
            pane.append_thinking("thinking first")
            pane.open_endpoint_box("lint", None)
            pane.append_endpoint_line("checking...")
            pane.close_endpoint_box("lint", None, 0, 2.0, 0.0, None)
            pane.open_endpoint_box("tb_coder", "tb")
            await pilot.pause()
            content = str(pane.query_one("#main-content").render())
            # All content visible — nothing cleared
            assert "thinking" in content
            assert "lint" in content
            assert "tb_coder" in content


# ===========================================================================
# TicketHeader
# ===========================================================================


class TestTicketHeader:
    @pytest.mark.asyncio
    async def test_set_ticket_info(self):
        async with TicketHeaderTestApp().run_test() as pilot:
            header = pilot.app.query_one(TicketHeader)
            header.set_ticket_info("my-ticket", "feature", "main")
            await pilot.pause()
            content = str(header.query_one("#header-content").render())
            assert "my-ticket" in content
            assert "feature" in content
            assert "target" not in content

    @pytest.mark.asyncio
    async def test_compact_shows_counts(self):
        """Compact view shows just counts per status bucket — names live in expanded."""
        async with TicketHeaderTestApp().run_test() as pilot:
            header = pilot.app.query_one(TicketHeader)
            header.set_ticket_info("test", "feature", "main")
            header.update_criteria(
                {
                    "review_rtl": {"met": True, "mandatory": True, "detail": {}, "params": {}},
                    "review_tb": {"met": True, "mandatory": True, "detail": {}, "params": {}},
                    "sim_pass": {
                        "met": False,
                        "mandatory": True,
                        "detail": {"exit_code": 1},
                        "ever_failed": True,
                        "params": {},
                    },
                    "synthesis_ok": {
                        "met": True,
                        "mandatory": True,
                        "detail": {"cells": 1200},
                        "ever_met": True,
                        "stale": True,
                        "params": {},
                    },
                    "coverage_toggle": {
                        "met": False,
                        "mandatory": True,
                        "detail": {},
                        "params": {},
                    },
                    "coverage_fsm": {"met": False, "mandatory": True, "detail": {}, "params": {}},
                }
            )
            await pilot.pause()
            content = str(header.query_one("#header-content").render())
            assert "2 met" in content
            assert "1 failing" in content
            assert "1 recheck" in content
            assert "2 not run" in content
            assert "✓" in content
            assert "✗" in content
            assert "↻" in content
            assert "○" in content
            assert "press c" in content
            # No criterion names in compact view.
            assert "review_rtl" not in content
            assert "coverage_toggle" not in content

    @pytest.mark.asyncio
    async def test_expanded_order_and_icons_for_all_statuses(self):
        """Expanded criteria prioritize actionable states and label each group."""
        async with TicketHeaderTestApp().run_test() as pilot:
            header = pilot.app.query_one(TicketHeader)
            header.set_ticket_info("test", "feature", "main")
            header.update_criteria(
                {
                    "lint_clean": {
                        "met": True,
                        "mandatory": True,
                        "detail": {"warnings": 0},
                        "params": {},
                    },
                    "sim_pass": {
                        "met": False,
                        "mandatory": True,
                        "detail": {"exit_code": 1},
                        "ever_failed": True,
                        "params": {},
                    },
                    "synthesis_ok": {
                        "met": True,
                        "mandatory": True,
                        "detail": {"cells": 1200},
                        "ever_met": True,
                        "stale": True,
                        "params": {},
                    },
                    "formal_check": {
                        "met": False,
                        "mandatory": True,
                        "detail": {},
                        "params": {},
                    },
                }
            )
            header.toggle_expanded()
            await pilot.pause()
            content = str(header.query_one("#header-content").render())
            i_lint = content.find("lint_clean")
            i_sim = content.find("sim_pass")
            i_syn = content.find("synthesis_ok")
            i_formal = content.find("formal_check")
            assert 0 <= i_sim < i_syn < i_formal < i_lint
            assert "✗ Failing" in content
            assert "↻ Needs recheck" in content
            assert "○ Not run" in content
            assert "✓ Met" in content

    @pytest.mark.asyncio
    async def test_toggle_expanded(self):
        async with TicketHeaderTestApp().run_test() as pilot:
            header = pilot.app.query_one(TicketHeader)
            header.set_ticket_info("my-ticket", "feature", "main")
            header.update_criteria(
                {
                    "sim_pass": {"met": False, "mandatory": True, "detail": {}, "params": {}},
                }
            )
            header.toggle_expanded()
            await pilot.pause()
            assert header._expanded is True
            assert header.has_class("expanded")

            header.toggle_expanded()
            await pilot.pause()
            assert header._expanded is False
            assert not header.has_class("expanded")

    @pytest.mark.asyncio
    async def test_dut_info_hidden_when_empty(self):
        """No DUT/TB line before the developer plans them — placeholders read as clutter."""
        async with TicketHeaderTestApp().run_test() as pilot:
            header = pilot.app.query_one(TicketHeader)
            header.set_ticket_info("test", "feature", "main")
            await pilot.pause()
            content = str(header.query_one("#header-content").render())
            assert "DUT:" not in content
            assert "TB:" not in content
            assert "not yet planned" not in content

    @pytest.mark.asyncio
    async def test_dut_info_rendered_from_state(self):
        async with TicketHeaderTestApp().run_test() as pilot:
            header = pilot.app.query_one(TicketHeader)
            header.set_ticket_info("test", "feature", "main")
            header.update_dut_info({"dut_top_module": "fifo", "tb_top_module": "tb_fifo"})
            await pilot.pause()
            content = str(header.query_one("#header-content").render())
            assert "DUT: fifo" in content
            assert "TB: tb_fifo" in content

    @pytest.mark.asyncio
    async def test_met_failing_and_not_run_split(self):
        async with TicketHeaderTestApp().run_test() as pilot:
            header = pilot.app.query_one(TicketHeader)
            header.set_ticket_info("test", "feature", "main")
            header.update_criteria(
                {
                    "sim_pass": {
                        "met": True,
                        "mandatory": True,
                        "detail": {"exit_code": 0},
                        "params": {},
                    },
                    "lint_clean": {
                        "met": False,
                        "mandatory": True,
                        "detail": {"warnings": 3},
                        "params": {},
                    },
                    "coverage_toggle": {
                        "met": False,
                        "mandatory": True,
                        "detail": {},
                        "params": {},
                    },
                }
            )
            await pilot.pause()
            content = str(header.query_one("#header-content").render())
            assert "✓" in content
            assert "✗" in content
            assert "○" in content
            assert "not run" in content


# ===========================================================================
# TopStrip / BottomStrip
# ===========================================================================


class TestTopStrip:
    @pytest.mark.asyncio
    async def test_hidden_when_empty(self):
        async with TopStripTestApp().run_test() as pilot:
            strip = pilot.app.query_one(TopStrip)
            strip.update_entries([], 0)
            await pilot.pause()
            assert strip.display is False

    @pytest.mark.asyncio
    async def test_shows_entries(self):
        async with TopStripTestApp().run_test() as pilot:
            strip = pilot.app.query_one(TopStrip)
            mark = McpToolCompletionMark(0, 2, "lint", None, 0, 4.0, 0.0, "clean")
            strip.update_entries([_render_entry_line(mark)], 0)
            await pilot.pause()
            assert strip.display is True
            content = str(strip.query_one("#top-strip-content").render())
            assert "lint" in content

    @pytest.mark.asyncio
    async def test_overflow_indicator(self):
        async with TopStripTestApp().run_test() as pilot:
            strip = pilot.app.query_one(TopStrip)
            mark = McpToolCompletionMark(0, 2, "lint", None, 0, 4.0, 0.0, "clean")
            strip.update_entries([_render_entry_line(mark)], 5)
            await pilot.pause()
            content = str(strip.query_one("#top-strip-content").render())
            assert "5 more above" in content


class TestBottomStrip:
    @pytest.mark.asyncio
    async def test_hidden_when_empty(self):
        async with BottomStripTestApp().run_test() as pilot:
            strip = pilot.app.query_one(BottomStrip)
            strip.update_entries([], 0)
            await pilot.pause()
            assert strip.display is False

    @pytest.mark.asyncio
    async def test_shows_entries(self):
        async with BottomStripTestApp().run_test() as pilot:
            strip = pilot.app.query_one(BottomStrip)
            mark = McpToolCompletionMark(0, 2, "sim", "cfg_a", 1, 18.0, 0.05, "3 errors")
            strip.update_entries([_render_entry_line(mark)], 0)
            await pilot.pause()
            assert strip.display is True
            content = str(strip.query_one("#bottom-strip-content").render())
            assert "sim" in content

    @pytest.mark.asyncio
    async def test_overflow_indicator(self):
        async with BottomStripTestApp().run_test() as pilot:
            strip = pilot.app.query_one(BottomStrip)
            mark = McpToolCompletionMark(0, 2, "tb_coder", "tb", 0, 120.0, 0.60, "done")
            strip.update_entries([_render_entry_line(mark)], 2)
            await pilot.pause()
            content = str(strip.query_one("#bottom-strip-content").render())
            assert "2 more below" in content


# ===========================================================================
# StatusBar
# ===========================================================================


class TestStatusBar:
    @pytest.mark.asyncio
    async def test_initial_display(self):
        async with StatusBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(StatusBar)
            await pilot.pause()
            content = str(bar.query_one("#status-text").render())
            assert "elapsed" in content
            assert "$0.00" in content

    @pytest.mark.asyncio
    async def test_update_counters(self):
        async with StatusBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(StatusBar)
            bar.update_counters(output_tokens=5000, cost_usd=0.50, lines_added=10, lines_removed=3)
            await pilot.pause()
            content = str(bar.query_one("#status-text").render())
            assert "$0.50" in content
            assert "+10" in content
            assert "-3" in content

    @pytest.mark.asyncio
    async def test_high_level_activity_is_visible_and_clearable(self):
        async with StatusBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(StatusBar)
            bar.set_activity("post-processing")
            await pilot.pause()
            content = str(bar.query_one("#status-text").render())
            assert "post-processing" in content
            assert "elapsed" in content

            bar.set_activity("")
            await pilot.pause()
            assert "post-processing" not in str(bar.query_one("#status-text").render())
