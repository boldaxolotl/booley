"""Custom Textual Message subclasses for Console.

Posted from the DisplayWatcher daemon thread via app.post_message().
"""

from __future__ import annotations

from textual.message import Message


class AgentThinking(Message):
    """Developer Agent or specialist text chunk."""

    def __init__(self, text: str, *, is_specialist: bool = False) -> None:
        super().__init__()
        self.text = text
        self.is_specialist = is_specialist


class UsageChanged(Message):
    """Agent usage update.

    ``output_tokens`` and ``cost_usd`` are deltas since the last update, which
    the status bar accumulates. ``context_tokens`` is an absolute snapshot of
    the current prompt size (how full the context window is), and
    ``context_limit`` is that model's window, or ``None`` when unknown.
    """

    def __init__(
        self,
        output_tokens: int,
        cost_usd: float,
        context_tokens: int | None = None,
        context_limit: int | None = None,
    ) -> None:
        super().__init__()
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.context_tokens = context_tokens
        self.context_limit = context_limit


class DeveloperBudgetChanged(Message):
    """Live Developer Agent active-time and wall-clock budget reading."""

    def __init__(
        self,
        *,
        wall_elapsed_seconds: float,
        active_elapsed_seconds: float,
        wall_limit_seconds: int,
        active_limit_seconds: int,
        paused: bool,
        pause_reason: str,
    ) -> None:
        super().__init__()
        self.wall_elapsed_seconds = wall_elapsed_seconds
        self.active_elapsed_seconds = active_elapsed_seconds
        self.wall_limit_seconds = wall_limit_seconds
        self.active_limit_seconds = active_limit_seconds
        self.paused = paused
        self.pause_reason = pause_reason


class EditsChanged(Message):
    """Absolute line delta between the ticket worktree and its fork base."""

    def __init__(self, lines_added: int, lines_removed: int) -> None:
        super().__init__()
        self.lines_added = lines_added
        self.lines_removed = lines_removed


class FilesEdited(Message):
    """Files changed by one developer edit and their fork-base line counts."""

    def __init__(
        self,
        files: list[str],
        line_counts: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        super().__init__()
        self.files = files
        self.line_counts = line_counts or {}


class McpToolStarted(Message):
    """MCP endpoint invocation began."""

    def __init__(self, name: str, target: str | None = None) -> None:
        super().__init__()
        self.name = name
        self.target = target


class McpToolCompleted(Message):
    """MCP endpoint finished (carries summary + counter deltas).

    ``output_tokens`` is the specialist agent's own output. Its *input* is
    deliberately not surfaced: it belongs to a separate context window, not the
    developer's, and its cost is already inside ``cost_usd``.
    """

    def __init__(
        self,
        name: str,
        target: str | None,
        exit_code: int,
        duration_s: float,
        cost_usd: float,
        summary: str,
        output_tokens: int = 0,
        lines_added: int = 0,
        lines_removed: int = 0,
        display_lines: list[str] | None = None,
        line_counts_absolute: bool = False,
    ) -> None:
        super().__init__()
        self.name = name
        self.target = target
        self.exit_code = exit_code
        self.duration_s = duration_s
        self.cost_usd = cost_usd
        self.summary = summary
        self.output_tokens = output_tokens
        self.lines_added = lines_added
        self.lines_removed = lines_removed
        self.line_counts_absolute = line_counts_absolute
        self.display_lines = display_lines


class McpToolProgress(Message):
    """MCP endpoint progress line (live output inside an open endpoint box)."""

    def __init__(self, line: str) -> None:
        super().__init__()
        self.line = line


class CriteriaChanged(Message):
    """Criteria snapshot update."""

    def __init__(self, criteria: dict) -> None:
        super().__init__()
        self.criteria = criteria


class DutInfoChanged(Message):
    """DUT-info snapshot update (dut_top_module / tb_top_module)."""

    def __init__(self, dut_info: dict) -> None:
        super().__init__()
        self.dut_info = dut_info


class SetupProgress(Message):
    """Setup phase status line."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class ActivityChanged(Message):
    """High-level harness activity shown persistently in the status bar."""

    def __init__(self, activity: str) -> None:
        super().__init__()
        self.activity = activity
