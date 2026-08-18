"""Tests for ConsoleApp lifecycle phase state machine.

Codifies the activation/teardown handshake so events posted across phases
either route correctly or get dropped without ad-hoc try/except guards.
"""

from __future__ import annotations

import pytest

from booley.harness.console.app import ConsoleApp, ConsolePhase
from booley.harness.console.events import (
    ActivityChanged,
    AgentThinking,
    CriteriaChanged,
    DutInfoChanged,
    EditsChanged,
    FilesEdited,
    McpToolCompleted,
    McpToolProgress,
    McpToolStarted,
    SetupProgress,
    UsageChanged,
)
from booley.harness.console.widgets import MainPane, StatusBar, TicketHeader

# ===========================================================================
# Phase transition rules
# ===========================================================================


class TestPhaseTransitions:
    def test_initial_phase_is_pre_mount(self):
        app = ConsoleApp()
        assert app.phase is ConsolePhase.PRE_MOUNT

    def test_pre_mount_to_setup_allowed(self):
        app = ConsoleApp()
        assert app.transition_to(ConsolePhase.SETUP) is True
        assert app.phase is ConsolePhase.SETUP

    def test_setup_to_running_allowed(self):
        app = ConsoleApp()
        app.transition_to(ConsolePhase.SETUP)
        assert app.transition_to(ConsolePhase.RUNNING) is True
        assert app.phase is ConsolePhase.RUNNING

    def test_running_to_teardown_allowed(self):
        app = ConsoleApp()
        app.transition_to(ConsolePhase.SETUP)
        app.transition_to(ConsolePhase.RUNNING)
        assert app.transition_to(ConsolePhase.TEARDOWN) is True
        assert app.phase is ConsolePhase.TEARDOWN

    def test_teardown_to_exited_allowed(self):
        app = ConsoleApp()
        app.transition_to(ConsolePhase.SETUP)
        app.transition_to(ConsolePhase.TEARDOWN)
        assert app.transition_to(ConsolePhase.EXITED) is True
        assert app.phase is ConsolePhase.EXITED

    def test_setup_can_skip_to_teardown(self):
        """Preflight abort: harness gives up before reaching RUNNING."""
        app = ConsoleApp()
        app.transition_to(ConsolePhase.SETUP)
        assert app.transition_to(ConsolePhase.TEARDOWN) is True
        assert app.phase is ConsolePhase.TEARDOWN

    def test_running_can_force_exited_on_crash(self):
        """Textual app crash short-circuits the teardown handshake."""
        app = ConsoleApp()
        app.transition_to(ConsolePhase.SETUP)
        app.transition_to(ConsolePhase.RUNNING)
        assert app.transition_to(ConsolePhase.EXITED) is True
        assert app.phase is ConsolePhase.EXITED

    def test_running_cannot_go_back_to_setup(self):
        app = ConsoleApp()
        app.transition_to(ConsolePhase.SETUP)
        app.transition_to(ConsolePhase.RUNNING)
        assert app.transition_to(ConsolePhase.SETUP) is False
        assert app.phase is ConsolePhase.RUNNING

    def test_exited_is_terminal(self):
        app = ConsoleApp()
        app.transition_to(ConsolePhase.SETUP)
        app.transition_to(ConsolePhase.TEARDOWN)
        app.transition_to(ConsolePhase.EXITED)
        assert app.transition_to(ConsolePhase.SETUP) is False
        assert app.transition_to(ConsolePhase.RUNNING) is False
        assert app.transition_to(ConsolePhase.TEARDOWN) is False
        assert app.phase is ConsolePhase.EXITED

    def test_same_phase_is_noop(self):
        """Idempotent: harness's exit() override may double-transition."""
        app = ConsoleApp()
        app.transition_to(ConsolePhase.SETUP)
        assert app.transition_to(ConsolePhase.SETUP) is True
        assert app.phase is ConsolePhase.SETUP


# ===========================================================================
# on_mount drives PRE_MOUNT → SETUP
# ===========================================================================


class _BareConsoleApp(ConsoleApp):
    """ConsoleApp with no harness_work; useful for lifecycle-only tests.

    CSS_PATH is overridden to None to bypass Textual's relative-path lookup
    (which resolves against the *subclass* module — i.e. the tests/ tree —
    rather than the package source).
    """

    CSS_PATH = None


class TestOnMountTransition:
    @pytest.mark.asyncio
    async def test_on_mount_advances_to_setup(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.phase is ConsolePhase.SETUP

    @pytest.mark.asyncio
    async def test_exit_advances_to_teardown(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.exit()
            await pilot.pause()
            # After exit(), phase is TEARDOWN. Textual may have shut down
            # the message loop, but the phase tag itself is set.
            assert app.phase in (ConsolePhase.TEARDOWN, ConsolePhase.EXITED)

    @pytest.mark.asyncio
    async def test_user_quit_sets_flag_and_transitions(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_quit()
            await pilot.pause()
            assert app._user_quit is True
            assert app.phase in (ConsolePhase.TEARDOWN, ConsolePhase.EXITED)


# ===========================================================================
# Phase-gated message dispatch
# ===========================================================================


class TestPhaseGatedDispatch:
    @pytest.mark.asyncio
    async def test_endpoint_started_in_running_renders(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.transition_to(ConsolePhase.RUNNING)
            app.post_message(McpToolStarted("tb_coder", "tb"))
            await pilot.pause()
            pane = app.query_one(MainPane)
            content = str(pane.query_one("#main-content").render())
            assert "tb_coder" in content

    @pytest.mark.asyncio
    async def test_setup_progress_in_setup_renders(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            # Already in SETUP after on_mount.
            assert app.phase is ConsolePhase.SETUP
            app.post_message(SetupProgress("loading config..."))
            await pilot.pause()
            pane = app.query_one(MainPane)
            content = str(pane.query_one("#main-content").render())
            assert "loading config" in content

    @pytest.mark.asyncio
    async def test_tool_event_during_teardown_dropped(self):
        """Late events from background threads must not touch widgets after
        teardown. The handler returns early; the screen stays unchanged."""
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.transition_to(ConsolePhase.RUNNING)
            app.post_message(McpToolStarted("tb_coder", None))
            await pilot.pause()
            # Snapshot then transition to teardown and post a late event.
            pane = app.query_one(MainPane)
            before = str(pane.query_one("#main-content").render())
            app.transition_to(ConsolePhase.TEARDOWN)
            app.post_message(McpToolProgress("late progress line"))
            app.post_message(
                McpToolCompleted(
                    "tb_coder",
                    None,
                    exit_code=0,
                    duration_s=1.0,
                    cost_usd=0.0,
                    summary="done",
                )
            )
            await pilot.pause()
            after = str(pane.query_one("#main-content").render())
            # No "late progress line" smuggled in.
            assert "late progress line" not in after
            # Original content preserved.
            assert before in after or after == before

    @pytest.mark.asyncio
    async def test_criteria_update_during_teardown_dropped(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.transition_to(ConsolePhase.TEARDOWN)
            app.post_message(
                CriteriaChanged(
                    {
                        "sim_pass": {"met": False, "mandatory": True, "detail": {}, "params": {}},
                    }
                )
            )
            await pilot.pause()
            header = app.query_one(TicketHeader)
            # Internal state untouched.
            assert header._criteria == {}

    @pytest.mark.asyncio
    async def test_dut_info_during_teardown_dropped(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.transition_to(ConsolePhase.TEARDOWN)
            app.post_message(DutInfoChanged({"dut_top_module": "fifo"}))
            await pilot.pause()
            header = app.query_one(TicketHeader)
            assert header._dut_info == {}

    @pytest.mark.asyncio
    async def test_agent_thinking_during_setup_routes(self):
        """Developer Agent text can arrive during the setup phase — the
        setup divider gets drawn at first non-setup content. Should not
        crash and should append to MainPane."""
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.phase is ConsolePhase.SETUP
            app.post_message(AgentThinking("Analyzing the design..."))
            await pilot.pause()
            pane = app.query_one(MainPane)
            content = str(pane.query_one("#main-content").render())
            assert "Analyzing" in content


class TestUsageChanged:
    """The status bar ticks mid-run, not only when a endpoint box closes."""

    @pytest.mark.asyncio
    async def test_usage_accumulates_into_status_bar(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(UsageChanged(12_000, 0.25, context_tokens=90_000))
            app.post_message(UsageChanged(8_000, 0.15, context_tokens=142_000))
            await pilot.pause()
            bar = app.query_one(StatusBar)
            assert bar._output_tokens == 20_000
            # context is an absolute snapshot -- replaced, never summed
            assert bar._context_tokens == 142_000
            assert bar._cost_usd == pytest.approx(0.40)

    @pytest.mark.asyncio
    async def test_negative_cost_delta_converges_on_billed_total(self):
        """The estimate→actual switch may walk the running cost back down."""
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(UsageChanged(10_000, 0.50))
            app.post_message(UsageChanged(0, -0.10))
            await pilot.pause()
            assert app.query_one(StatusBar)._cost_usd == pytest.approx(0.40)

    @pytest.mark.asyncio
    async def test_usage_during_teardown_dropped(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.transition_to(ConsolePhase.TEARDOWN)
            app.post_message(UsageChanged(5_000, 1.0))
            await pilot.pause()
            assert app.query_one(StatusBar)._output_tokens == 0


class TestActivityChanged:
    @pytest.mark.asyncio
    async def test_post_processing_activity_routes_to_status_bar(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(ActivityChanged("post-processing"))
            await pilot.pause()
            assert app.query_one(StatusBar)._activity == "post-processing"

            app.post_message(ActivityChanged(""))
            await pilot.pause()
            assert app.query_one(StatusBar)._activity == ""


class TestEditsChanged:
    @pytest.mark.asyncio
    async def test_absolute_snapshot_replaces_prior_tool_delta(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(
                McpToolCompleted(
                    "tb_coder",
                    None,
                    0,
                    1.0,
                    0.0,
                    "done",
                    lines_added=5,
                    lines_removed=1,
                )
            )
            app.post_message(EditsChanged(17, 4))
            await pilot.pause()

            bar = app.query_one(StatusBar)
            assert (bar._lines_added, bar._lines_removed) == (17, 4)

    @pytest.mark.asyncio
    async def test_file_summary_is_appended_to_main_pane(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(FilesEdited(["rtl/top.sv"], {"rtl/top.sv": (7, 2)}))
            await pilot.pause()

            assert "edited rtl/top.sv  total +7 -2" in app.query_one(MainPane)._content.plain

    @pytest.mark.asyncio
    async def test_tool_can_carry_absolute_snapshot_atomically(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(
                McpToolCompleted(
                    "tb_coder",
                    None,
                    0,
                    1.0,
                    0.0,
                    "done",
                    lines_added=23,
                    lines_removed=7,
                    line_counts_absolute=True,
                )
            )
            await pilot.pause()

            bar = app.query_one(StatusBar)
            assert (bar._lines_added, bar._lines_removed) == (23, 7)


# ===========================================================================
# Idempotent teardown
# ===========================================================================


class TestIdempotentTeardown:
    @pytest.mark.asyncio
    async def test_double_exit_no_crash(self):
        """User quits while harness_work's finally also calls exit()."""
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.exit()
            app.exit()  # idempotent
            await pilot.pause()
            assert app.phase in (ConsolePhase.TEARDOWN, ConsolePhase.EXITED)

    @pytest.mark.asyncio
    async def test_transition_to_exited_then_message_dropped(self):
        app = _BareConsoleApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.transition_to(ConsolePhase.EXITED)
            # Any post is now a no-op; no exception escapes.
            app.post_message(McpToolStarted("tb_coder", None))
            await pilot.pause()
            pane = app.query_one(MainPane)
            content = str(pane.query_one("#main-content").render())
            assert "tb_coder" not in content
