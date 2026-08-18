"""P0/P1 whole-app interaction tests for the Console terminal UI."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Static

from booley.config.editor import ResolvedEditor
from booley.harness.console.app import ConsolePhase, _strip_region_for_mark
from booley.harness.console.events import AgentThinking, CriteriaChanged
from booley.harness.console.links import (
    InvokeResult,
    LinkContext,
    LinkTarget,
    ResolvedAction,
)
from booley.harness.console.widgets import BottomStrip, MainPane, StatusBar, TicketHeader, TopStrip

from .console_scenario import ConsoleScenario, ConsoleTestApp


def _render_status(bar: StatusBar) -> str:
    return str(bar.query_one("#status-text", Static).render())


def _strip_counts(app: ConsoleTestApp) -> tuple[int, int]:
    top = app.query_one(TopStrip)
    bottom = app.query_one(BottomStrip)
    return (
        len(top._entries) + top._overflow_count,
        len(bottom._entries) + bottom._overflow_count,
    )


def _click_event(target: LinkTarget | None = None) -> SimpleNamespace:
    meta = {"booley_target": target} if target is not None else {}
    return SimpleNamespace(style=SimpleNamespace(meta=meta), stop=MagicMock())


@pytest.mark.asyncio
async def test_follow_mode_pauses_on_single_up_and_resumes_at_tail() -> None:
    """SCR-01 through SCR-04: append must respect explicit user scrolling."""
    app = ConsoleTestApp()
    scenario = ConsoleScenario(app)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        scenario.post_setup()
        scenario.start_running()
        scenario.add_history(40)
        await pilot.pause()

        main = app.query_one(MainPane)
        main.focus()
        main.scroll_end(animate=False)
        await pilot.pause()
        assert main._auto_scroll is True
        assert main.scroll_y == main.max_scroll_y

        await pilot.press("up")
        await pilot.pause()
        paused_y = main.scroll_y
        assert paused_y < main.max_scroll_y
        assert main._auto_scroll is False

        scenario.complete_endpoint("lint", "late", summary="arrived while paused")
        app.post_message(CriteriaChanged({"late": {"met": True}}))
        await pilot.pause()
        assert main.scroll_y == paused_y
        assert main._auto_scroll is False

        await pilot.press("end")
        await pilot.wait_for_scheduled_animations()
        assert main._auto_scroll is True
        scenario.complete_endpoint("lint", "tail", summary="followed")
        await pilot.pause()
        await pilot.pause()
        assert main.scroll_y == main.max_scroll_y


@pytest.mark.asyncio
async def test_follow_mode_survives_viewport_growth_at_tail() -> None:
    """A layout clamp to the new tail is not an upward user scroll."""
    app = ConsoleTestApp()
    scenario = ConsoleScenario(app)
    async with app.run_test(size=(80, 18)) as pilot:
        await pilot.pause()
        scenario.start_running()
        scenario.add_history(30)
        await pilot.pause()

        main = app.query_one(MainPane)
        main.scroll_end(animate=False)
        await pilot.pause()
        old_y = main.scroll_y

        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        assert main.scroll_y < old_y
        assert main.scroll_y == main.max_scroll_y
        assert main._auto_scroll is True


@pytest.mark.asyncio
async def test_home_end_and_short_pane_scrolling_are_safe() -> None:
    """SCR-05/SCR-08: keyboard endpoints work and an empty pane is harmless."""
    app = ConsoleTestApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        main = app.query_one(MainPane)
        main.focus()
        await pilot.press("up", "down", "pageup", "pagedown", "home", "end")
        await pilot.pause()
        assert main.scroll_y == 0

        scenario = ConsoleScenario(app)
        scenario.start_running()
        scenario.add_history(30)
        await pilot.pause()
        await pilot.press("home")
        await pilot.wait_for_scheduled_animations()
        assert main.scroll_y == 0
        assert main._auto_scroll is False
        await pilot.press("end")
        await pilot.wait_for_scheduled_animations()
        assert main.scroll_y == main.max_scroll_y
        assert main._auto_scroll is True


@pytest.mark.asyncio
async def test_middle_viewport_shows_both_strips_with_exact_accounting() -> None:
    """STR-03/04/06/07: above and below summaries coexist without loss."""
    app = ConsoleTestApp()
    scenario = ConsoleScenario(app)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        scenario.start_running()
        scenario.add_history(36)
        await pilot.pause()

        main = app.query_one(MainPane)
        main.scroll_to(y=main.max_scroll_y // 2, animate=False)
        await pilot.pause()
        app._update_strips()
        await pilot.pause()
        app._update_strips()
        await pilot.pause()

        resolved = main.resolve_mark_divider_visual_rows()
        viewport_bottom = main.scroll_y + main.scrollable_content_region.height
        regions = [
            _strip_region_for_mark(
                open_s, open_e, close_s, close_e, main.scroll_y, viewport_bottom
            )
            for _mark, open_s, open_e, close_s, close_e in resolved
        ]
        expected_above = regions.count("above")
        expected_below = regions.count("below")
        assert expected_above > 0
        assert expected_below > 0
        assert app.query_one(TopStrip).display is True
        assert app.query_one(BottomStrip).display is True
        assert _strip_counts(app) == (expected_above, expected_below)

        top = app.query_one(TopStrip)
        bottom = app.query_one(BottomStrip)
        assert len(top._entries) <= app._strip_capacity()
        assert len(bottom._entries) <= app._strip_capacity()


@pytest.mark.asyncio
async def test_wrapped_history_does_not_duplicate_visible_tools_in_top_strip() -> None:
    """Regression for the PicoRV32 synth -> sim -> mutation display bug."""
    app = ConsoleTestApp()
    scenario = ConsoleScenario(app)
    async with app.run_test(size=(50, 18)) as pilot:
        await pilot.pause()
        scenario.start_running()
        wrap_drift = " ".join(["x" * 25] * 3)
        for _ in range(12):
            app.post_message(AgentThinking(wrap_drift))
        scenario.complete_endpoint("lint", "lint_core", display_lines=["0 warnings"])
        scenario.complete_endpoint("synth", "asic_core", display_lines=["23.8 KGE"])
        scenario.complete_endpoint(
            "sim",
            "sim_core,sim_wb,sim_zbb_disabled",
            display_lines=["3/3 targets passed", "sim_core", "sim_wb", "sim_zbb_disabled"],
        )
        scenario.complete_endpoint("mutation_tester", "sim_core", exit_code=1)
        await pilot.pause()
        await pilot.pause(0.1)

        top = app.query_one(TopStrip)
        top_names = [entry.plain.split()[1] for entry in top._entries]
        assert top_names[-1] == "synth"
        assert "sim" not in top_names
        assert "mutation_tester" not in top_names


@pytest.mark.asyncio
async def test_narrow_strip_entries_are_one_rendered_row_each() -> None:
    """Every counted strip entry remains visible on a narrow terminal."""
    app = ConsoleTestApp()
    scenario = ConsoleScenario(app)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        scenario.start_running()
        for index in range(12):
            scenario.complete_endpoint(
                "mutation_tester",
                f"target-{'x' * 80}-{index}",
                summary="summary " + "y" * 120,
            )
        await pilot.pause()
        await pilot.pause(0.1)

        top = app.query_one(TopStrip)
        content = top.query_one("#top-strip-content", Static)
        expected_rows = len(top._entries) + int(top._overflow_count > 0)
        rendered_rows = content.visual.get_height(content.styles, content.size.width)
        assert rendered_rows == expected_rows
        assert content.region.height == expected_rows


@pytest.mark.asyncio
@pytest.mark.parametrize(("rows", "capacity"), [(39, 4), (40, 8)])
async def test_strip_capacity_boundary(rows: int, capacity: int) -> None:
    """STR-05: the capacity changes exactly at forty terminal rows."""
    app = ConsoleTestApp()
    scenario = ConsoleScenario(app)
    async with app.run_test(size=(120, rows)) as pilot:
        await pilot.pause()
        scenario.start_running()
        scenario.add_history(40)
        await pilot.pause()
        main = app.query_one(MainPane)
        main.scroll_end(animate=False)
        await pilot.pause()
        app._update_strips()
        await pilot.pause()
        assert app._strip_capacity() == capacity
        assert len(app.query_one(TopStrip)._entries) == capacity


@pytest.mark.asyncio
async def test_resize_preserves_follow_mode_or_paused_neighborhood() -> None:
    """SCR-07/STR-09: wrapping changes must not silently resume following."""
    app = ConsoleTestApp()
    scenario = ConsoleScenario(app)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        scenario.start_running()
        scenario.add_history(30)
        await pilot.pause()
        main = app.query_one(MainPane)
        main.focus()
        main.scroll_end(animate=False)
        await pilot.pause()

        await pilot.resize_terminal(70, 24)
        await pilot.pause()
        assert main._auto_scroll is True
        assert main.scroll_y == main.max_scroll_y

        await pilot.press("pageup")
        await pilot.pause()
        assert main._auto_scroll is False
        await pilot.resize_terminal(100, 30)
        await pilot.pause()
        assert main._auto_scroll is False
        assert 0 <= main.scroll_y <= main.max_scroll_y


@pytest.mark.asyncio
async def test_criteria_toggle_and_live_shrink_preserve_activity_scroll() -> None:
    """CRT-01/07/08: header scrolling is independent from activity history."""
    app = ConsoleTestApp()
    scenario = ConsoleScenario(app)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        scenario.post_setup()
        scenario.start_running()
        scenario.add_history(30)
        await pilot.pause()
        main = app.query_one(MainPane)
        main.scroll_to(y=main.max_scroll_y // 2, animate=False)
        await pilot.pause()
        activity_y = main.scroll_y

        criteria = {
            f"criterion_{index:02d}": {
                "met": index % 3 == 0,
                "mandatory": True,
                "detail": {} if index % 3 else {"value": index},
                "params": {},
            }
            for index in range(50)
        }
        app.post_message(CriteriaChanged(criteria))
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        header = app.query_one(TicketHeader)
        assert header._expanded is True
        assert main.scroll_y == activity_y

        header.scroll_end(animate=False)
        await pilot.pause()
        assert header.max_scroll_y > 0
        app.post_message(CriteriaChanged(dict(list(criteria.items())[:2])))
        await pilot.pause()
        assert header._expanded is True
        assert 0 <= header.scroll_y <= header.max_scroll_y
        assert main.scroll_y == activity_y

        await pilot.press("c")
        await pilot.pause()
        assert header._expanded is False
        assert main.scroll_y == activity_y


@pytest.mark.asyncio
async def test_click_routes_to_editor_and_hint_restores_live_counters(tmp_path) -> None:
    """LNK-07/08/09: click failures are transient and never lose counters."""
    app = ConsoleTestApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pane = app.query_one(MainPane)
        bar = app.query_one(StatusBar)
        ctx = LinkContext(project_root=tmp_path, worktree_root=None, fork_base_sha=None)
        editor = ResolvedEditor(
            open=("missing-editor", "{file}"),
            open_at_line=("missing-editor", "{file}:{line}"),
            diff=None,
        )
        pane.set_link_context(ctx, editor)
        bar.update_counters(output_tokens=10, cost_usd=0.25, lines_added=2, lines_removed=1)

        target = LinkTarget(kind="file", raw="rtl/top.sv", line=7)
        event = _click_event(target)
        action = ResolvedAction(kind="open_at_line", args=(str(tmp_path / "rtl/top.sv"),), line=7)
        with (
            patch("booley.harness.console.links.resolve", return_value=action) as resolve,
            patch(
                "booley.harness.console.links.invoke",
                return_value=InvokeResult(ok=False, hint="editor not found: missing-editor"),
            ) as invoke,
        ):
            pane.on_click(event)  # type: ignore[arg-type]
        await pilot.pause()
        resolve.assert_called_once_with(target, ctx)
        invoke.assert_called_once_with(action, editor)
        event.stop.assert_called_once()
        assert "editor not found" in _render_status(bar)

        bar.update_counters(output_tokens=5, cost_usd=0.05, lines_added=3)
        bar.show_hint("second failure")
        await pilot.pause()
        assert "second failure" in _render_status(bar)
        bar._clear_hint()
        await pilot.pause()
        rendered = _render_status(bar)
        assert "15 out" in rendered
        assert "$0.30" in rendered
        assert "+5" in rendered
        assert "-1" in rendered

        non_link = _click_event()
        pane.on_click(non_link)  # type: ignore[arg-type]
        non_link.stop.assert_not_called()


@pytest.mark.asyncio
async def test_file_edit_name_opens_in_vscode(tmp_path) -> None:
    app = ConsoleTestApp()
    edited = tmp_path / "notes.md"
    edited.write_text("changed", encoding="utf-8")
    editor = ResolvedEditor(
        open=("code", "--goto", "{file}"),
        open_at_line=("code", "--goto", "{file}:{line}"),
        diff=("code", "--diff", "{left}", "{right}"),
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pane = app.query_one(MainPane)
        ctx = LinkContext(project_root=tmp_path, worktree_root=None, fork_base_sha=None)
        pane.set_link_context(ctx, editor)
        pane.append_file_edits(["notes.md"])

        linked_spans = [
            span
            for span in pane._content.spans
            if getattr(span.style, "meta", {}).get("booley_target") is not None
        ]
        assert len(linked_spans) == 1
        target = linked_spans[0].style.meta["booley_target"]
        assert target == LinkTarget(kind="file", raw="notes.md")
        assert pane._editor == editor


@pytest.mark.asyncio
async def test_late_strip_timer_after_teardown_is_harmless() -> None:
    """STR-10/LIFE-04: a debounced refresh may fire after teardown."""
    app = ConsoleTestApp()
    scenario = ConsoleScenario(app)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        scenario.start_running()
        scenario.add_history(20)
        await pilot.pause()
        main = app.query_one(MainPane)
        main.scroll_to(y=1, animate=False)
        await pilot.pause()
        assert app._strip_timer is not None
        app.transition_to(ConsolePhase.TEARDOWN)
        await pilot.pause(0.1)
        assert app.phase is ConsolePhase.TEARDOWN
