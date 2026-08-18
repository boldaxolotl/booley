"""ConsoleApp — Textual App subclass for the Booley Console TUI.

Lifecycle phases
----------------
The app's life is a strict linear progression:

    PRE_MOUNT → SETUP → RUNNING → TEARDOWN → EXITED

* PRE_MOUNT: instance constructed; widgets not yet composed. No handlers fire
  here in practice because Textual queues messages until ``on_mount``.
* SETUP: ``on_mount`` has run; widgets exist. The harness worker emits
  ``SetupProgress`` events for preflight / parse-validate / config load.
* RUNNING: harness has entered the ticket body. Endpoint/criteria/agent events
  are accepted.
* TEARDOWN: ``app.exit()`` requested — either by the harness (worker
  completed or raised) or by the user pressing 'q'. Late messages from
  background threads are silently dropped.
* EXITED: ``run_async`` has returned. Any post is a no-op.

Each ``on_*`` handler is gated on the current phase: events outside their
valid window are dropped with a DEBUG log line. This replaces ad-hoc
"is the widget still mounted?" guards that accreted across past fixes.
"""

from __future__ import annotations

import enum
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.timer import Timer

from .events import (
    ActivityChanged,
    AgentThinking,
    CriteriaChanged,
    DeveloperBudgetChanged,
    DutInfoChanged,
    EditsChanged,
    FilesEdited,
    McpToolCompleted,
    McpToolProgress,
    McpToolStarted,
    SetupProgress,
    UsageChanged,
)
from .widgets import (
    BottomStrip,
    MainPane,
    ScrollPositionChanged,
    StatusBar,
    TicketHeader,
    TopStrip,
    _render_entry_line,
)

logger = logging.getLogger(__name__)


class ConsolePhase(enum.Enum):
    """Lifecycle phase of the Console TUI. See module docstring."""

    PRE_MOUNT = "pre_mount"
    SETUP = "setup"
    RUNNING = "running"
    TEARDOWN = "teardown"
    EXITED = "exited"


# Allowed forward transitions. Anything else is logged + ignored.
# Skipping forward (SETUP → TEARDOWN without RUNNING) is permitted because
# the harness can abort before entering the ticket body. EXITED is reachable
# from any non-terminal phase because Textual app crashes can short-circuit
# the normal exit() teardown handshake.
_PHASE_TRANSITIONS: dict[ConsolePhase, frozenset[ConsolePhase]] = {
    ConsolePhase.PRE_MOUNT: frozenset(
        {ConsolePhase.SETUP, ConsolePhase.TEARDOWN, ConsolePhase.EXITED}
    ),
    ConsolePhase.SETUP: frozenset(
        {ConsolePhase.RUNNING, ConsolePhase.TEARDOWN, ConsolePhase.EXITED}
    ),
    ConsolePhase.RUNNING: frozenset({ConsolePhase.TEARDOWN, ConsolePhase.EXITED}),
    ConsolePhase.TEARDOWN: frozenset({ConsolePhase.EXITED}),
    ConsolePhase.EXITED: frozenset(),
}

# Which phases each message class is valid in. Events arriving outside
# their valid window are dropped silently. SETUP-only events posted
# during RUNNING are still rendered (setup divider already drawn).
_VALID_PHASES_BY_MESSAGE: dict[str, frozenset[ConsolePhase]] = {
    "ActivityChanged": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "SetupProgress": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "AgentThinking": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "McpToolStarted": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "McpToolProgress": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "McpToolCompleted": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "CriteriaChanged": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "DutInfoChanged": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "EditsChanged": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "FilesEdited": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "UsageChanged": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
    "DeveloperBudgetChanged": frozenset({ConsolePhase.SETUP, ConsolePhase.RUNNING}),
}


def _row_span_visible(
    start_y: int,
    end_y: int,
    viewport_top: float,
    viewport_bottom: float,
) -> bool:
    """Return True when a visual row interval intersects the viewport."""
    return start_y < viewport_bottom and end_y > viewport_top


def _strip_region_for_mark(
    open_start: int,
    open_end: int,
    close_start: int,
    close_end: int,
    viewport_top: float,
    viewport_bottom: float,
) -> str | None:
    """Classify a completed MCP endpoint for strip rendering."""
    open_visible = _row_span_visible(open_start, open_end, viewport_top, viewport_bottom)
    close_visible = _row_span_visible(close_start, close_end, viewport_top, viewport_bottom)
    if open_visible or close_visible:
        return None
    if close_end <= viewport_top:
        return "above"
    if open_start >= viewport_bottom:
        return "below"
    return None


class ConsoleApp(App):
    """Full-screen TUI for the Booley harness."""

    TITLE = "booley"
    CSS_PATH = "console.tcss"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", show=True),
        Binding("c", "toggle_criteria", "Criteria", show=True),
    ]

    _harness_work: Callable[[], Coroutine] | None = None

    def __init__(
        self,
        *,
        shutdown_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self._shutdown_event = shutdown_event
        self._user_quit = False
        self._strip_timer: Timer | None = None
        self._phase: ConsolePhase = ConsolePhase.PRE_MOUNT
        self._phase_lock = threading.Lock()

    # --- Phase machinery ----------------------------------------------------

    @property
    def phase(self) -> ConsolePhase:
        """Current lifecycle phase. Safe to read from any thread."""
        return self._phase

    def transition_to(self, target: ConsolePhase) -> bool:
        """Move to *target* if the transition is legal. Idempotent.

        Returns True on a successful transition, False if rejected.
        Same-phase calls are a no-op that return True. Illegal transitions
        log a warning and leave the phase unchanged.
        """
        with self._phase_lock:
            if target is self._phase:
                return True
            allowed = _PHASE_TRANSITIONS.get(self._phase, frozenset())
            if target not in allowed:
                logger.warning(
                    "ConsoleApp: illegal phase transition %s -> %s (ignored)",
                    self._phase.value,
                    target.value,
                )
                return False
            old = self._phase
            self._phase = target
            logger.debug("ConsoleApp phase: %s -> %s", old.value, target.value)
            return True

    def _handler_phase_ok(self, message_name: str) -> bool:
        """Check whether *message_name* should be handled in the current phase.

        Returns True when the handler should proceed. Returns False (with a
        DEBUG log) when the event is out-of-phase and must be dropped.
        """
        valid = _VALID_PHASES_BY_MESSAGE.get(message_name)
        if valid is None or self._phase in valid:
            return True
        logger.debug(
            "Dropping %s in phase %s (valid: %s)",
            message_name,
            self._phase.value,
            ",".join(p.value for p in valid),
        )
        return False

    def compose(self) -> ComposeResult:
        yield TicketHeader()
        yield TopStrip()
        yield MainPane()
        yield BottomStrip()
        yield StatusBar()

    def on_mount(self) -> None:
        # Widgets composed: leave PRE_MOUNT for SETUP before any harness
        # work runs. Setup events now route correctly.
        self.transition_to(ConsolePhase.SETUP)
        if self._harness_work is not None:
            self.run_worker(
                self._harness_work,
                thread=False,
                exit_on_error=False,
            )

    # --- Message handlers ---

    def on_agent_thinking(self, event: AgentThinking) -> None:
        if not self._handler_phase_ok("AgentThinking"):
            return
        main = self.query_one(MainPane)
        if event.is_specialist:
            main.append_specialist_text(event.text)
        else:
            main.append_thinking(event.text)

    def on_usage_changed(self, event: UsageChanged) -> None:
        if not self._handler_phase_ok("UsageChanged"):
            return
        self.query_one(StatusBar).update_counters(
            output_tokens=event.output_tokens,
            cost_usd=event.cost_usd,
            context_tokens=event.context_tokens,
            context_limit=event.context_limit,
        )

    def on_developer_budget_changed(self, event: DeveloperBudgetChanged) -> None:
        if not self._handler_phase_ok("DeveloperBudgetChanged"):
            return
        self.query_one(StatusBar).set_developer_budget(
            wall_elapsed_seconds=event.wall_elapsed_seconds,
            active_elapsed_seconds=event.active_elapsed_seconds,
            wall_limit_seconds=event.wall_limit_seconds,
            active_limit_seconds=event.active_limit_seconds,
            paused=event.paused,
            pause_reason=event.pause_reason,
        )

    def on_edits_changed(self, event: EditsChanged) -> None:
        if not self._handler_phase_ok("EditsChanged"):
            return
        self.query_one(StatusBar).set_line_counts(event.lines_added, event.lines_removed)

    def on_files_edited(self, event: FilesEdited) -> None:
        if not self._handler_phase_ok("FilesEdited"):
            return
        self.query_one(MainPane).append_file_edits(event.files, event.line_counts)

    def on_mcp_tool_started(self, event: McpToolStarted) -> None:
        if not self._handler_phase_ok("McpToolStarted"):
            return
        main = self.query_one(MainPane)
        main.open_endpoint_box(event.name, event.target)

    def on_mcp_tool_progress(self, event: McpToolProgress) -> None:
        if not self._handler_phase_ok("McpToolProgress"):
            return
        self.query_one(MainPane).append_endpoint_line(event.line)

    def on_mcp_tool_completed(self, event: McpToolCompleted) -> None:
        if not self._handler_phase_ok("McpToolCompleted"):
            return
        main = self.query_one(MainPane)
        main.close_endpoint_box(
            event.name,
            event.target,
            event.exit_code,
            event.duration_s,
            event.cost_usd,
            event.display_lines,
            summary=event.summary,
        )
        self._update_strips()
        # No context reading here: a specialist's prompt lives in its own
        # context window, not the developer's, so it must not overwrite the
        # figure the status bar is tracking.
        status = self.query_one(StatusBar)
        status.update_counters(
            output_tokens=event.output_tokens,
            cost_usd=event.cost_usd,
            lines_added=0 if event.line_counts_absolute else event.lines_added,
            lines_removed=0 if event.line_counts_absolute else event.lines_removed,
        )
        if event.line_counts_absolute:
            status.set_line_counts(event.lines_added, event.lines_removed)

    def on_criteria_changed(self, event: CriteriaChanged) -> None:
        if not self._handler_phase_ok("CriteriaChanged"):
            return
        self.query_one(TicketHeader).update_criteria(event.criteria)

    def on_dut_info_changed(self, event: DutInfoChanged) -> None:
        if not self._handler_phase_ok("DutInfoChanged"):
            return
        self.query_one(TicketHeader).update_dut_info(event.dut_info)

    def on_setup_progress(self, event: SetupProgress) -> None:
        if not self._handler_phase_ok("SetupProgress"):
            return
        self.query_one(MainPane).append_setup_line(event.text)

    def on_activity_changed(self, event: ActivityChanged) -> None:
        if not self._handler_phase_ok("ActivityChanged"):
            return
        self.query_one(StatusBar).set_activity(event.activity)

    def on_scroll_position_changed(self, event: ScrollPositionChanged) -> None:
        # Scroll events are UI-internal; we still gate on phase so we don't
        # touch widgets during teardown.
        if self._phase not in (ConsolePhase.SETUP, ConsolePhase.RUNNING):
            return
        if self._strip_timer is not None:
            self._strip_timer.stop()
        self._strip_timer = self.set_timer(0.05, self._update_strips)

    # --- Actions ---

    def action_toggle_criteria(self) -> None:
        self.query_one(TicketHeader).toggle_expanded()

    def action_quit(self) -> None:
        self._user_quit = True
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        self.exit()

    def exit(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        # Centralize the TEARDOWN transition: both harness_work's finally
        # block and the user-quit action route through here.
        self.transition_to(ConsolePhase.TEARDOWN)
        return super().exit(*args, **kwargs)

    # --- Strip management ---

    # Strip capacity flexes with terminal height: +1 on small terminals,
    # +2 on large vs. the original 3/6 baseline.
    _STRIP_CAP_SMALL = 4
    _STRIP_CAP_LARGE = 8
    _STRIP_LARGE_THRESHOLD = 40  # terminal rows

    def _strip_capacity(self) -> int:
        return (
            self._STRIP_CAP_LARGE
            if self.size.height >= self._STRIP_LARGE_THRESHOLD
            else self._STRIP_CAP_SMALL
        )

    def _update_strips(self) -> None:
        # Internal refresh — must tolerate being called during teardown when
        # the timer fires after widgets have unmounted, or before the first
        # mount has actually placed widgets on the screen.
        if self._phase not in (ConsolePhase.SETUP, ConsolePhase.RUNNING):
            return
        from textual.css.query import NoMatches

        try:
            main = self.query_one(MainPane)
        except NoMatches:
            return
        resolved = main.resolve_mark_divider_visual_rows()
        scroll_y = main.scroll_y
        viewport_bottom = scroll_y + main.scrollable_content_region.height

        above = []
        below = []
        for mark, open_start, open_end, close_start, close_end in resolved:
            region = _strip_region_for_mark(
                open_start,
                open_end,
                close_start,
                close_end,
                scroll_y,
                viewport_bottom,
            )
            if region == "above":
                above.append(mark)
            elif region == "below":
                below.append(mark)

        cap = self._strip_capacity()
        top_entries = [_render_entry_line(m) for m in above[-cap:]]
        top_overflow = max(0, len(above) - cap)
        self.query_one(TopStrip).update_entries(top_entries, top_overflow)

        bottom_entries = [_render_entry_line(m) for m in below[:cap]]
        bottom_overflow = max(0, len(below) - cap)
        self.query_one(BottomStrip).update_entries(bottom_entries, bottom_overflow)
