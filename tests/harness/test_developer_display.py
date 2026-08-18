"""Tests for developer_display — DisplayWatcher and agent_event_handler."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.harness.developer_display import DisplayWatcher, agent_event_handler

# ===========================================================================
# DisplayWatcher — endpoint_start / endpoint_end events
# ===========================================================================


class TestDisplayWatcherEvents:
    def _write_event(self, path: Path, event: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def test_endpoint_start_opens_box(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        # file_pos starts at 0 since file is empty before start()
        watcher._file_pos = 0

        self._write_event(
            display, {"type": "endpoint_start", "endpoint": "sim", "target": "cfg_a"}
        )
        with patch("booley.harness.terminal.endpoint_box_open") as mock_open:
            watcher._poll_events()
            mock_open.assert_called_once_with("sim", "cfg_a")

        assert "sim" in watcher._open_endpoints

    def test_endpoint_end_closes_box(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        # Need a matching endpoint_start so the watcher knows this end is at
        # outermost depth (orphan endpoint_end with no open box is suppressed).
        self._write_event(display, {"type": "endpoint_start", "endpoint": "lint"})
        self._write_event(
            display,
            {
                "type": "endpoint_end",
                "endpoint": "lint",
                "target": None,
                "exit_code": 0,
                "duration_s": 12.5,
                "cost_usd": 0.03,
                "display_lines": ["all good"],
            },
        )
        with (
            patch("booley.harness.terminal.endpoint_box_open"),
            patch("booley.harness.terminal.endpoint_box_close") as mock_close,
        ):
            watcher._poll_events()
            mock_close.assert_called_once_with(
                "lint",
                None,
                exit_code=0,
                duration_s=12.5,
                cost_usd=0.03,
                display_lines=["all good"],
                dry_run=False,
            )

    def test_endpoint_start_then_end_sequence(self, tmp_path: Path):
        """Open + close: endpoint removed from _open_endpoints after end."""
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        self._write_event(display, {"type": "endpoint_start", "endpoint": "lint"})
        with patch("booley.harness.terminal.endpoint_box_open"):
            watcher._poll_events()

        assert "lint" in watcher._open_endpoints

        self._write_event(
            display,
            {
                "type": "endpoint_end",
                "endpoint": "lint",
                "exit_code": 0,
                "duration_s": 3.0,
            },
        )
        with patch("booley.harness.terminal.endpoint_box_close"):
            watcher._poll_events()

        assert "lint" not in watcher._open_endpoints

    def test_streamed_final_line_is_not_repeated_at_endpoint_end(self, tmp_path: Path):
        """A completed Target renders now while the final event stays self-contained."""
        display = tmp_path / "display.jsonl"
        display.touch()
        summaries: list[list[str] | None] = []
        watcher = DisplayWatcher(
            display,
            on_endpoint_summary=lambda *args: summaries.append(args[-1]),
        )
        watcher._file_pos = 0

        self._write_event(display, {"type": "endpoint_start", "endpoint": "sim"})
        self._write_event(
            display,
            {
                "type": "endpoint_progress",
                "endpoint": "sim",
                "line": "✓ target_a  1.0s",
                "completion": True,
                "repeats_at_end": True,
            },
        )
        self._write_event(
            display,
            {
                "type": "endpoint_end",
                "endpoint": "sim",
                "exit_code": 0,
                "display_lines": ["2/2 targets passed, 2.0s", "✓ target_a  1.0s"],
            },
        )

        with (
            patch("booley.harness.terminal.endpoint_box_open"),
            patch("booley.harness.terminal.endpoint_progress_line") as mock_progress,
            patch("booley.harness.terminal.endpoint_box_close") as mock_close,
        ):
            watcher._poll_events()

        mock_progress.assert_called_once_with("✓ target_a  1.0s", dimmed=False)
        assert mock_close.call_args.kwargs["display_lines"] == ["2/2 targets passed, 2.0s"]
        assert summaries == [["2/2 targets passed, 2.0s"]]

    def test_malformed_json_skipped(self, tmp_path: Path):
        """Broken JSON lines should not crash the watcher."""
        display = tmp_path / "display.jsonl"
        display.write_text("NOT JSON\n", encoding="utf-8")
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        with patch("booley.harness.terminal.endpoint_box_open") as mock_open:
            watcher._poll_events()  # should not raise
            mock_open.assert_not_called()

    def test_empty_lines_skipped(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        display.write_text("\n  \n", encoding="utf-8")
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        with patch("booley.harness.terminal.endpoint_box_open") as mock_open:
            watcher._poll_events()
            mock_open.assert_not_called()

    def test_nested_endpoint_invocations_suppressed(self, tmp_path: Path):
        """MCP tools invoked by Specialists (nested inside an open endpoint box) should
        not produce their own open/progress/close rendering. Only the outermost
        developer-invoked endpoint is visible."""
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        # Developer Agent → debugger; debugger (specialist) → simulate; back to debugger.
        self._write_event(display, {"type": "endpoint_start", "endpoint": "debugger"})
        self._write_event(display, {"type": "endpoint_progress", "line": "parsing diagnosis"})
        self._write_event(
            display, {"type": "endpoint_start", "endpoint": "sim", "target": "default"}
        )
        self._write_event(display, {"type": "endpoint_progress", "line": "0/1 configs passed"})
        self._write_event(
            display,
            {
                "type": "endpoint_end",
                "endpoint": "sim",
                "target": "default",
                "exit_code": 1,
                "duration_s": 60.0,
            },
        )
        self._write_event(
            display, {"type": "endpoint_progress", "line": "checking bwave availability"}
        )
        self._write_event(
            display,
            {
                "type": "endpoint_end",
                "endpoint": "debugger",
                "exit_code": 0,
                "duration_s": 204.0,
            },
        )

        with (
            patch("booley.harness.terminal.endpoint_box_open") as mock_open,
            patch("booley.harness.terminal.endpoint_box_close") as mock_close,
            patch("booley.harness.terminal.endpoint_progress_line") as mock_prog,
        ):
            watcher._poll_events()

            # Only the outermost debugger box should be rendered.
            mock_open.assert_called_once_with("debugger", None)
            assert mock_close.call_count == 1
            assert mock_close.call_args.args == ("debugger", None)
            # Only debugger's own progress lines; simulate's are suppressed.
            assert [c.args[0] for c in mock_prog.call_args_list] == [
                "parsing diagnosis",
                "checking bwave availability",
            ]

        # Heartbeat tracking should also only have the outermost endpoint.
        assert list(watcher._open_endpoints.keys()) == []
        assert watcher._nesting_depth == 0

    def test_unknown_event_type_ignored(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        self._write_event(display, {"type": "metrics", "data": 42})
        with (
            patch("booley.harness.terminal.endpoint_box_open") as mock_open,
            patch("booley.harness.terminal.endpoint_box_close") as mock_close,
        ):
            watcher._poll_events()
            mock_open.assert_not_called()
            mock_close.assert_not_called()


# ===========================================================================
# DisplayWatcher — heartbeat timing
# ===========================================================================


class TestDisplayWatcherHeartbeat:
    def test_heartbeat_fires_after_interval(self, tmp_path: Path):
        """After 5 min of silence with open endpoints, heartbeat should fire."""
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        fake_start = 1000.0
        watcher._open_endpoints["sim"] = fake_start
        watcher._last_output = fake_start

        def mock_monotonic():
            return fake_start + 301.0

        def stop_on_wait(**kwargs):
            watcher._stop_event.set()

        with (
            patch("booley.harness.developer_display.time.monotonic", side_effect=mock_monotonic),
            patch.object(watcher._stop_event, "wait", side_effect=stop_on_wait),
            patch.object(watcher._stop_event, "is_set", side_effect=[False, True]),
            patch("booley.harness.terminal.endpoint_heartbeat") as mock_hb,
        ):
            watcher._run()
            mock_hb.assert_called_once_with("sim", 301.0)

    def test_no_heartbeat_without_open_endpoints(self, tmp_path: Path):
        """No open endpoints → no heartbeat even after interval elapses."""
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        def mock_monotonic():
            return 400.0

        with (
            patch("booley.harness.developer_display.time.monotonic", side_effect=mock_monotonic),
            patch.object(watcher._stop_event, "wait"),
            patch.object(watcher._stop_event, "is_set", side_effect=[False, True]),
            patch("booley.harness.terminal.endpoint_heartbeat") as mock_hb,
        ):
            watcher._run()
            mock_hb.assert_not_called()

    def test_recent_event_suppresses_heartbeat(self, tmp_path: Path):
        """An endpoint_start event resets the silence timer — no premature heartbeat."""
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        # Watcher started long ago, but endpoint just opened (recent event)
        watcher._open_endpoints["coder"] = 900.0
        watcher._last_output = 900.0

        def mock_monotonic():
            return 924.0  # only 24s since last output

        def stop_on_wait(**kwargs):
            watcher._stop_event.set()

        with (
            patch("booley.harness.developer_display.time.monotonic", side_effect=mock_monotonic),
            patch.object(watcher._stop_event, "wait", side_effect=stop_on_wait),
            patch.object(watcher._stop_event, "is_set", side_effect=[False, True]),
            patch("booley.harness.terminal.endpoint_heartbeat") as mock_hb,
        ):
            watcher._run()
            mock_hb.assert_not_called()


# ===========================================================================
# DisplayWatcher — crash recovery (seek to EOF on start)
# ===========================================================================


class TestDisplayWatcherCrashRecovery:
    def test_start_seeks_to_eof(self, tmp_path: Path):
        """start() should skip pre-existing content (crash recovery)."""
        display = tmp_path / "display.jsonl"
        stale = json.dumps({"type": "endpoint_start", "endpoint": "stale_tool"}) + "\n"
        display.write_text(stale, encoding="utf-8")
        expected_pos = display.stat().st_size  # use actual file size (platform-safe)

        watcher = DisplayWatcher(display)
        # Patch thread start so we don't actually spawn a thread
        with patch.object(watcher, "_run"):
            watcher.start()

        assert watcher._file_pos == expected_pos

        # Polling now should find nothing new
        with patch("booley.harness.terminal.endpoint_box_open") as mock_open:
            watcher._poll_events()
            mock_open.assert_not_called()

    def test_new_events_after_recovery_processed(self, tmp_path: Path):
        """Events written after start() should be picked up."""
        display = tmp_path / "display.jsonl"
        stale = json.dumps({"type": "endpoint_start", "endpoint": "old"}) + "\n"
        display.write_text(stale, encoding="utf-8")

        watcher = DisplayWatcher(display)
        with patch.object(watcher, "_run"):
            watcher.start()

        # Append a new event after start
        with display.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "endpoint_start", "endpoint": "fresh"}) + "\n")

        with patch("booley.harness.terminal.endpoint_box_open") as mock_open:
            watcher._poll_events()
            mock_open.assert_called_once_with("fresh", None)

    def test_start_handles_missing_file(self, tmp_path: Path):
        """start() with non-existent file: file_pos stays 0."""
        display = tmp_path / "display.jsonl"  # not created
        watcher = DisplayWatcher(display)
        with patch.object(watcher, "_run"):
            watcher.start()

        assert watcher._file_pos == 0


# ===========================================================================
# agent_event_handler — routes agent_text
# ===========================================================================


class TestAgentEventHandler:
    def test_routes_agent_text(self):
        with patch("booley.harness.terminal.agent_text") as mock_at:
            agent_event_handler({"type": "agent_text", "text": "thinking..."})
            mock_at.assert_called_once_with("thinking...")

    def test_routes_agent_thinking(self):
        """Reasoning blocks render too — a turn is mostly thinking, rarely prose."""
        with patch("booley.harness.terminal.agent_text") as mock_at:
            agent_event_handler({"type": "agent_thinking", "text": "let me check the FSM"})
            mock_at.assert_called_once_with("let me check the FSM")

    def test_suppresses_agent_narration_while_endpoint_is_active(self):
        with patch("booley.harness.terminal.agent_text") as mock_at:
            agent_event_handler(
                {"type": "agent_text", "text": "still waiting for synth"},
                endpoint_active=lambda: True,
            )
            mock_at.assert_not_called()

    def test_ignores_usage_events(self):
        """usage is a Console-only counter signal — nothing to print in log mode."""
        with patch("booley.harness.terminal.agent_text") as mock_at:
            agent_event_handler({"type": "usage", "tokens": 1200, "cost_usd": 0.4})
            mock_at.assert_not_called()

    def test_ignores_non_agent_text_events(self):
        with patch("booley.harness.terminal.agent_text") as mock_at:
            agent_event_handler({"type": "endpoint_start", "endpoint": "lint"})
            mock_at.assert_not_called()

    def test_ignores_event_without_type(self):
        with patch("booley.harness.terminal.agent_text") as mock_at:
            agent_event_handler({"text": "orphan"})
            mock_at.assert_not_called()

    def test_ignores_agent_text_with_empty_string(self):
        """agent_text with empty text should not call terminal."""
        with patch("booley.harness.terminal.agent_text") as mock_at:
            agent_event_handler({"type": "agent_text", "text": ""})
            mock_at.assert_not_called()

    def test_ignores_agent_text_with_missing_text_key(self):
        """agent_text event with no 'text' key → empty default → no call."""
        with patch("booley.harness.terminal.agent_text") as mock_at:
            agent_event_handler({"type": "agent_text"})
            mock_at.assert_not_called()


# ===========================================================================
# _make_console_event_handler — Console TUI routing
# ===========================================================================


class TestConsoleEventHandler:
    """The Console handler must render reasoning and tick the status bar.

    Regression guard: the handler used to match ``agent_text`` only. A real
    developer turn is overwhelmingly ``agent_thinking`` blocks (12 vs 1 in a
    sampled transcript), so the Console sat blank between MCP tool calls.
    """

    @staticmethod
    async def _posted(event: dict) -> list:
        """Run the handler against a stub app, returning the posted messages."""
        from unittest.mock import MagicMock

        from booley.harness.developer_display import _make_console_event_handler

        app = MagicMock()
        posted: list = []
        app.post_message = posted.append
        handler = _make_console_event_handler(app)
        with patch("booley.harness.terminal.agent_text"):
            handler(event)
        # post_message is scheduled via call_soon_threadsafe; let it run.
        await asyncio.sleep(0)
        return posted

    @pytest.mark.asyncio
    async def test_agent_thinking_reaches_the_pane(self):
        from booley.harness.console.events import AgentThinking

        posted = await self._posted({"type": "agent_thinking", "text": "checking the FSM"})
        assert len(posted) == 1
        assert isinstance(posted[0], AgentThinking)
        assert posted[0].text == "checking the FSM"
        assert posted[0].is_specialist is False

    @pytest.mark.asyncio
    async def test_agent_text_still_reaches_the_pane(self):
        from booley.harness.console.events import AgentThinking

        posted = await self._posted({"type": "agent_text", "text": "done"})
        assert len(posted) == 1
        assert isinstance(posted[0], AgentThinking)

    @pytest.mark.asyncio
    async def test_agent_text_is_suppressed_while_endpoint_is_active(self):
        from unittest.mock import MagicMock

        from booley.harness.developer_display import _make_console_event_handler

        app = MagicMock()
        posted: list = []
        app.post_message = posted.append
        handler = _make_console_event_handler(app, endpoint_active=lambda: True)

        with patch("booley.harness.terminal.agent_text") as mock_at:
            handler({"type": "agent_text", "text": "the Flow is still running"})
            await asyncio.sleep(0)

        assert posted == []
        mock_at.assert_not_called()

    @pytest.mark.asyncio
    async def test_usage_becomes_a_counter_update(self):
        from booley.harness.console.events import UsageChanged

        posted = await self._posted(
            {
                "type": "usage",
                "output_tokens": 1234,
                "cost_usd": 0.5,
                "context_tokens": 142_000,
                "context_limit": 1_000_000,
            }
        )
        assert len(posted) == 1
        assert isinstance(posted[0], UsageChanged)
        assert posted[0].output_tokens == 1234
        assert posted[0].cost_usd == 0.5
        assert posted[0].context_tokens == 142_000
        assert posted[0].context_limit == 1_000_000

    @pytest.mark.asyncio
    async def test_usage_without_a_known_context_limit(self):
        """A model with no published window yields None, not 0."""
        from booley.harness.console.events import UsageChanged

        posted = await self._posted(
            {"type": "usage", "output_tokens": 10, "cost_usd": 0.0, "context_tokens": 500}
        )
        assert isinstance(posted[0], UsageChanged)
        assert posted[0].context_limit is None

    @pytest.mark.asyncio
    async def test_file_change_refreshes_absolute_edit_counts(self):
        from unittest.mock import MagicMock

        from booley.harness.console.events import EditsChanged, FilesEdited
        from booley.harness.developer_display import _make_console_event_handler

        app = MagicMock()
        posted: list = []
        app.post_message = posted.append
        counter = MagicMock()
        counter.snapshot_by_file.return_value = {
            "rtl/top.sv": (12, 3),
            "tb/top_tb.sv": (5, 1),
        }
        counter.normalize_path.side_effect = lambda path: path
        handler = _make_console_event_handler(app, counter)

        handler({"type": "file_change", "paths": ["rtl/top.sv"]})
        await asyncio.sleep(0)

        assert isinstance(posted[0], FilesEdited)
        assert posted[0].files == ["rtl/top.sv"]
        assert posted[0].line_counts == {"rtl/top.sv": (12, 3)}
        assert isinstance(posted[1], EditsChanged)
        assert (posted[1].lines_added, posted[1].lines_removed) == (17, 4)

    @pytest.mark.asyncio
    async def test_file_change_without_paths_still_refreshes_totals(self):
        from unittest.mock import MagicMock

        from booley.harness.console.events import EditsChanged
        from booley.harness.developer_display import _make_console_event_handler

        app = MagicMock()
        posted: list = []
        app.post_message = posted.append
        counter = MagicMock()
        counter.snapshot_by_file.return_value = {"rtl/top.sv": (2, 1)}

        handler = _make_console_event_handler(app, counter)
        handler({"type": "file_change"})
        await asyncio.sleep(0)

        assert len(posted) == 1
        assert isinstance(posted[0], EditsChanged)

    @pytest.mark.asyncio
    async def test_file_change_still_reports_path_when_diff_snapshot_fails(self):
        from unittest.mock import MagicMock

        from booley.harness.console.events import FilesEdited
        from booley.harness.developer_display import _make_console_event_handler

        app = MagicMock()
        posted: list = []
        app.post_message = posted.append
        counter = MagicMock()
        counter.normalize_path.side_effect = lambda path: path
        counter.snapshot_by_file.return_value = None

        handler = _make_console_event_handler(app, counter)
        handler({"type": "file_change", "paths": ["rtl/top.sv"]})
        await asyncio.sleep(0)

        assert len(posted) == 1
        assert isinstance(posted[0], FilesEdited)
        assert posted[0].files == ["rtl/top.sv"]
        assert posted[0].line_counts == {}

    @pytest.mark.asyncio
    async def test_empty_text_posts_nothing(self):
        assert await self._posted({"type": "agent_thinking", "text": ""}) == []

    @pytest.mark.asyncio
    async def test_unknown_event_posts_nothing(self):
        assert await self._posted({"type": "endpoint_start", "endpoint": "lint"}) == []
