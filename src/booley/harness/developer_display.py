"""Real-time display layer for the developer.

Two display channels:
1. Agent text — dimmed developer reasoning outside active endpoints
2. Endpoint boxes — per-endpoint boxes driven by display.jsonl written by MCP endpoints

DisplayWatcher polls display.jsonl on a background daemon thread.
agent_event_handler() is called from the streaming callback.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from booley.dev_support.development_state import DevelopmentState

from . import terminal
from .colors import bold_amber, chrome, dim, len_visible

if TYPE_CHECKING:
    from .console_metrics import WorktreeLineCounter
    from .models import TicketContext

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_S = 2.0
_HEARTBEAT_INTERVAL_S = 300.0  # 5 minutes


class DisplayWatcher:
    """Background thread that polls display.jsonl for MCP endpoint events.

    Emits endpoint_box_open/close and periodic heartbeats for open endpoints.
    Seeks to end of file on start (crash recovery: skip stale events).
    """

    def __init__(
        self,
        display_path: Path,
        *,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        on_endpoint_start: Callable[[str, str | None], None] | None = None,
        on_specialist_thinking: Callable[[str], None] | None = None,
        on_criteria_update: Callable[[dict], None] | None = None,
        on_endpoint_progress: Callable[[str], None] | None = None,
        on_endpoint_summary: Callable[
            [str, str | None, int, float, float, str, int, int, int, list[str] | None], None
        ]
        | None = None,  # (name, target, ...)
    ) -> None:
        self._path = display_path
        self._poll_interval_s = poll_interval_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._file_pos: int = 0
        # Track open endpoints for heartbeat: {endpoint_name: start_timestamp}.
        # Only the outermost Developer-invoked endpoint is recorded — nested
        # MCP tools invoked by Specialists are suppressed from all rendering.
        self._open_endpoints: dict[str, float] = {}
        # Final display lines streamed as Targets finish. Kept per outer endpoint
        # so endpoint_end can avoid rendering the same line twice in a live view.
        self._streamed_final_lines: dict[str, list[str]] = {}
        # Depth of open endpoint boxes, including nested Specialist invocations.
        # We render only when depth == 1; everything deeper is a specialist's
        # internal MCP tool call and should not appear in the developer view.
        self._nesting_depth: int = 0
        # Read by the agent-stream thread to suppress narration while a Flow or
        # Specialist already owns the display. ``threading.Event`` gives the
        # cross-thread handoff explicit synchronization instead of exposing the
        # watcher's mutable nesting counter.
        self._endpoint_active = threading.Event()
        # Reset on any display event so heartbeat only fires after true silence
        self._last_output: float = time.monotonic()
        # Console callbacks (None in log mode)
        self.on_endpoint_start = on_endpoint_start
        self.on_endpoint_progress = on_endpoint_progress
        self.on_specialist_thinking = on_specialist_thinking
        self.on_criteria_update = on_criteria_update
        self.on_endpoint_summary = on_endpoint_summary

    def start(self) -> None:
        """Start the watcher thread. Seeks to EOF to skip prior events."""
        if self._path.exists():
            self._file_pos = self._path.stat().st_size
        self._thread = threading.Thread(
            target=self._run,
            name="display-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the watcher to stop and wait for it to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def endpoint_active(self) -> bool:
        """Whether a Booley Flow or Specialist endpoint currently owns the UI."""
        return self._endpoint_active.is_set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._poll_events()
            now = time.monotonic()
            if now - self._last_output >= _HEARTBEAT_INTERVAL_S and self._open_endpoints:
                for name, start_ts in self._open_endpoints.items():
                    terminal.endpoint_heartbeat(name, now - start_ts)
                self._last_output = now
            self._stop_event.wait(timeout=self._poll_interval_s)

    def _poll_events(self) -> None:
        if not self._path.exists():
            return
        try:
            size = self._path.stat().st_size
            if size <= self._file_pos:
                return
            with self._path.open("r", encoding="utf-8") as f:
                f.seek(self._file_pos)
                while line := f.readline():
                    if not line.endswith("\n"):
                        # The writer has not atomically completed this record.
                        # Retry it from the same position on the next poll.
                        break
                    self._handle_line(line)
                    self._file_pos = f.tell()
        except OSError:
            logger.debug("display.jsonl read error", exc_info=True)

    def _handle_endpoint_start(self, event: dict) -> None:
        """Handle ``endpoint_start`` by opening the outermost endpoint box."""
        name = event.get("endpoint", "?")
        target = event.get("target") or None
        self._nesting_depth += 1
        self._endpoint_active.set()
        if self._nesting_depth == 1:
            self._open_endpoints[name] = time.monotonic()
            self._streamed_final_lines[name] = []
            terminal.endpoint_box_open(name, target)
            if self.on_endpoint_start:
                self.on_endpoint_start(name, target)

    def _handle_endpoint_progress(self, event: dict) -> None:
        """Handle ``endpoint_progress`` by emitting the outermost endpoint's progress."""
        line = event.get("line", "")
        if line and self._nesting_depth <= 1:
            completion = bool(event.get("completion"))
            if event.get("repeats_at_end"):
                name = event.get("endpoint", "?")
                self._streamed_final_lines.setdefault(name, []).append(line)
            if completion:
                terminal.endpoint_progress_line(line, dimmed=False)
            else:
                terminal.endpoint_progress_line(line)
            if self.on_endpoint_progress:
                self.on_endpoint_progress(line)

    def _unstreamed_display_lines(
        self,
        name: str,
        lines: object,
    ) -> list[str] | None:
        """Return final lines that were not already rendered live."""
        if not isinstance(lines, list):
            self._streamed_final_lines.pop(name, None)
            return None
        streamed = Counter(self._streamed_final_lines.pop(name, []))
        remaining: list[str] = []
        for line in lines:
            if streamed[line] > 0:
                streamed[line] -= 1
            else:
                remaining.append(line)
        return remaining

    def _handle_endpoint_end(self, event: dict) -> None:
        """display.jsonl ``endpoint_end``: close the box and route the summary."""
        # depth==0 means an orphan endpoint_end (e.g. a reconciled lock from
        # a prior session, or an out-of-band emitter) with no matching
        # endpoint_start; we still treat it as outermost so the summary
        # routes to the callback rather than being silently dropped.
        is_outermost = self._nesting_depth <= 1
        self._nesting_depth = max(0, self._nesting_depth - 1)
        if self._nesting_depth == 0:
            self._endpoint_active.clear()
        if not is_outermost:
            return
        name = event.get("endpoint", "?")
        target = event.get("target") or None
        exit_code = event.get("exit_code", 2)
        duration_s = event.get("duration_s", 0.0)
        cost_usd = event.get("cost_usd", 0.0)
        summary = event.get("summary", "")
        # Output only: a specialist's input tokens are mostly cache re-reads of
        # its own (separate) context, and their cost is already in cost_usd.
        output_tokens = event.get("output_tokens", 0)
        lines_added = event.get("lines_added", 0)
        lines_removed = event.get("lines_removed", 0)
        self._open_endpoints.pop(name, None)
        display_lines = self._unstreamed_display_lines(name, event.get("display_lines"))
        terminal.endpoint_box_close(
            name,
            target,
            exit_code=exit_code,
            duration_s=duration_s,
            cost_usd=cost_usd,
            display_lines=display_lines,
            dry_run=bool(event.get("dry_run")),
        )
        if self.on_endpoint_summary:
            self.on_endpoint_summary(
                name,
                target,
                exit_code,
                duration_s,
                cost_usd,
                summary,
                output_tokens,
                lines_added,
                lines_removed,
                display_lines,
            )

    def _handle_criteria_update(self, event: dict) -> None:
        """Forward a display.jsonl criteria snapshot."""
        criteria = event.get("criteria", {})
        if self.on_criteria_update:
            self.on_criteria_update(criteria)

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return

        etype = event.get("type", "")
        self._last_output = time.monotonic()
        if etype == "endpoint_start":
            self._handle_endpoint_start(event)
        elif etype == "endpoint_progress":
            self._handle_endpoint_progress(event)
        elif etype == "endpoint_end":
            self._handle_endpoint_end(event)
        elif etype == "specialist_thinking":
            text = event.get("text", "")
            # Endpoint progress/heartbeats already show liveness. Specialist
            # model narration is repetitive and competes visually with the
            # endpoint box that owns the screen.
            if text and not self.endpoint_active() and self.on_specialist_thinking:
                self.on_specialist_thinking(text)
        elif etype == "criteria_update":
            self._handle_criteria_update(event)


def agent_event_handler(
    event: dict,
    endpoint_active: Callable[[], bool] | None = None,
) -> None:
    """Callback for streaming events from the agent backend.

    Routes the agent's reasoning (``agent_thinking``) and prose
    (``agent_text``) to terminal.agent_text() while no endpoint is active.
    ``usage`` events carry no log-mode rendering — the run summary already
    reports final totals.
    """
    if event.get("type") in ("agent_text", "agent_thinking"):
        if endpoint_active is not None and endpoint_active():
            return
        text = event.get("text", "")
        if text:
            terminal.agent_text(text)


# ---------------------------------------------------------------------------
# Console TUI / terminal wiring helpers
# ---------------------------------------------------------------------------


def _console_setup_msg(text: str) -> None:
    """Post a SetupProgress message to the Console TUI if active."""
    app = terminal.get_console_app()
    if app is not None:
        from .console.events import SetupProgress

        app.post_message(SetupProgress(text))


def _console_activity(activity: str) -> None:
    """Show a persistent high-level activity in the Console status bar."""
    app = terminal.get_console_app()
    if app is not None:
        from .console.events import ActivityChanged

        app.post_message(ActivityChanged(activity))


def _display_ticket_banner(ctx: TicketContext) -> None:
    """Print the styled ticket info box to the terminal."""
    from booley.config.settings import get_backend_config

    terminal.raw()
    _DISPLAY = {"claude": ("Claude", chrome), "codex": ("ChatGPT", chrome)}
    bcfg = get_backend_config()
    p_name, p_color = _DISPLAY.get(bcfg.provider, (bcfg.provider, dim))
    backend_line = f"{dim('agent')} {p_color(p_name)}"

    lines = [bold_amber(ctx.slug)]
    type_line = f"{ctx.ticket_type} · {ctx.branch}"
    lines.append(type_line)
    lines.append(backend_line)

    width = max(len_visible(l) for l in lines) + 4
    terminal.raw(f"  {dim('┌' + '─' * width + '─')}")
    for l in lines:
        pad = width - len_visible(l) - 2
        terminal.raw(f"  {dim('│')}  {l}{' ' * pad}{dim('│')}")
    terminal.raw(f"  {dim('└' + '─' * width + '─')}")
    terminal.raw()


def _refresh_link_ctx_post_setup(ctx) -> None:
    """Populate the LinkContext's worktree + fork-base after setup runs.

    No-op when the Console isn't active (``ctx._link_ctx`` unset).
    """
    link_ctx = getattr(ctx, "_link_ctx", None)
    if link_ctx is None or ctx.worktree_path is None:
        return
    try:
        from .console.links import resolve_fork_base_sha

        sha = resolve_fork_base_sha(ctx.worktree_path, ctx.branch)
        link_ctx.attach_worktree(ctx.worktree_path, sha)
    except Exception:
        logger.exception("Refreshing link context after setup failed")


def _attach_click_links(app, ctx, project_root: Path) -> None:
    """Wire MainPane to the click-link resolver and VS Code.

    Failures degrade silently — Console keeps running with plain text
    when link wiring can't be set up (Textual missing, etc.).
    Worktree info is set later (post-setup) via
    ``ctx._link_ctx.attach_worktree`` so file-target clicks early in the
    run resolve against project only.
    """
    try:
        from booley.config.settings import VSCODE_EDITOR, resolve_editor

        from .console.links import build_link_context
        from .console.widgets import MainPane

        editor = resolve_editor() or VSCODE_EDITOR

        link_ctx = build_link_context(
            project_root=project_root,
            worktree_root=ctx.worktree_path,  # may be None pre-setup
            fork_base_sha=None,  # filled in post-setup
        )
        # Stash on ctx so the setup-step hook can update worktree info
        # without threading another argument through the call chain.
        ctx._link_ctx = link_ctx
        app.query_one(MainPane).set_link_context(link_ctx, editor)
    except Exception:
        logger.exception("Click-link wiring failed (continuing without links)")


def _make_console_event_handler(
    app,
    line_counter: WorktreeLineCounter | None = None,
    endpoint_active: Callable[[], bool] | None = None,
) -> Callable[[dict], None]:
    """Create an agent_event_handler that posts to both terminal (log) and Console.

    The handler is called from the Docker subprocess reading thread, so
    we must use call_soon_threadsafe to post into Textual's event loop.

    Both ``agent_thinking`` and ``agent_text`` are rendered between endpoints.
    While a Flow or Specialist owns the display, its progress and heartbeat
    provide liveness and repetitive agent narration is suppressed.
    """
    import asyncio

    from .console.events import (
        AgentThinking,
        DeveloperBudgetChanged,
        EditsChanged,
        FilesEdited,
        UsageChanged,
    )

    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

    def _post(msg: object) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(app.post_message, msg)

    def handler(event: dict) -> None:
        etype = event.get("type")
        if etype in ("agent_text", "agent_thinking"):
            if endpoint_active is not None and endpoint_active():
                return
            text = event.get("text", "")
            if text:
                terminal.agent_text(text)
                _post(AgentThinking(text))
        elif etype == "usage":
            limit = event.get("context_limit")
            context = event.get("context_tokens")
            _post(
                UsageChanged(
                    int(event.get("output_tokens", 0)),
                    float(event.get("cost_usd", 0.0)),
                    context_tokens=int(context) if context is not None else None,
                    context_limit=int(limit) if limit else None,
                )
            )
        elif etype == "developer_budget":
            _post(
                DeveloperBudgetChanged(
                    wall_elapsed_seconds=float(event["wall_elapsed_seconds"]),
                    active_elapsed_seconds=float(event["active_elapsed_seconds"]),
                    wall_limit_seconds=int(event["wall_limit_seconds"]),
                    active_limit_seconds=int(event["active_limit_seconds"]),
                    paused=bool(event.get("paused", False)),
                    pause_reason=str(event.get("pause_reason", "")),
                )
            )
        elif etype == "file_change" and line_counter is not None:
            changed: list[str] = []
            seen: set[str] = set()
            for raw_path in event.get("paths", []):
                path = line_counter.normalize_path(raw_path)
                if path is None or path in seen:
                    continue
                changed.append(path)
                seen.add(path)
            files = line_counter.snapshot_by_file()
            if changed:
                changed_counts = (
                    {path: files.get(path, (0, 0)) for path in changed}
                    if files is not None
                    else None
                )
                _post(FilesEdited(changed, changed_counts))
            if files is None:
                return
            _post(
                EditsChanged(
                    sum(added for added, _removed in files.values()),
                    sum(removed for _added, removed in files.values()),
                )
            )

    return handler


def _wire_console_callbacks(
    watcher: DisplayWatcher,
    app,
    line_counter: WorktreeLineCounter | None = None,
) -> None:
    """Set DisplayWatcher callbacks to post Textual messages.

    Callbacks fire on the DisplayWatcher daemon thread, so all posts
    use call_soon_threadsafe to reach Textual's event loop safely.
    """
    import asyncio

    from .console.events import (
        AgentThinking,
        CriteriaChanged,
        McpToolCompleted,
        McpToolProgress,
        McpToolStarted,
    )

    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

    def _post(msg: object) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(app.post_message, msg)

    def on_endpoint_start(name: str, target: str | None) -> None:
        _post(McpToolStarted(name, target))

    def on_endpoint_progress(line: str) -> None:
        _post(McpToolProgress(line))

    def on_specialist_thinking(text: str) -> None:
        _post(AgentThinking(text, is_specialist=True))

    def on_criteria_update(criteria: dict) -> None:
        _post(CriteriaChanged(criteria))

    def on_endpoint_summary(
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
    ) -> None:
        counts = line_counter.snapshot() if line_counter is not None else None
        _post(
            McpToolCompleted(
                name,
                target,
                exit_code,
                duration_s,
                cost_usd,
                summary,
                output_tokens=output_tokens,
                lines_added=counts[0] if counts is not None else lines_added,
                lines_removed=counts[1] if counts is not None else lines_removed,
                line_counts_absolute=counts is not None,
                display_lines=display_lines,
            )
        )

    watcher.on_endpoint_start = on_endpoint_start
    watcher.on_endpoint_progress = on_endpoint_progress
    watcher.on_specialist_thinking = on_specialist_thinking
    watcher.on_criteria_update = on_criteria_update
    watcher.on_endpoint_summary = on_endpoint_summary


def _push_initial_criteria(state_path: Path, app: object) -> None:
    """Read booley_state.json and post initial criteria to the Console."""
    try:
        from .console.events import CriteriaChanged

        if not state_path.exists():
            return
        state = DevelopmentState.load(state_path)
        snapshot = {}
        for key, entry in state.criteria.items():
            if key.startswith("_"):
                continue
            display_entry = {
                "met": entry.met,
                "mandatory": entry.mandatory,
                "detail": entry.detail or {},
                "params": entry.params or {},
            }
            if entry.stale:
                display_entry["stale"] = True
            if entry.ever_met:
                display_entry["ever_met"] = True
            if entry.ever_failed:
                display_entry["ever_failed"] = True
            snapshot[key] = display_entry
        if snapshot:
            app.post_message(CriteriaChanged(snapshot))
    except Exception:  # initial UI push is best-effort; failure must not abort startup
        logger.debug("Failed to push initial criteria", exc_info=True)
