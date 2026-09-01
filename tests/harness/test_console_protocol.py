"""Tests for Console protocol extensions — Phase 0 + Phase 1 changes.

Covers:
- summary field in endpoint_end events
- specialist_thinking event routing
- criteria_update event routing
- Configurable poll interval
- terminal.py Console suppression
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from booley.harness.developer_display import DisplayWatcher, _push_initial_criteria
from booley.harness.terminal import (
    get_console_app,
    set_console_active,
)
from booley.mcp.base import McpToolResult, _endpoint_end_event

# ===========================================================================
# Phase 0a: summary field in endpoint_end
# ===========================================================================


class TestToolEndSummary:
    def test_tool_result_has_summary_field(self):
        r = McpToolResult()
        assert r.summary == ""

    def test_tool_result_summary_set(self):
        r = McpToolResult(summary="all pass")
        assert r.summary == "all pass"

    def test_endpoint_end_event_includes_summary(self):
        result = McpToolResult(exit_code=0, summary="clean")
        event = _endpoint_end_event("lint", "cfg_a", result, 5.0)
        assert event["summary"] == "clean"

    def test_endpoint_end_event_empty_summary(self):
        result = McpToolResult(exit_code=0)
        event = _endpoint_end_event("lint", None, result, 2.0)
        assert event["summary"] == ""


# ===========================================================================
# Phase 0c: criteria_update event
# ===========================================================================


class TestCriteriaUpdateEvent:
    def test_criteria_update_emitted_on_set_criterion(self, tmp_path: Path):
        from booley.criteria.state import CriterionEntry, DevelopmentState

        state = DevelopmentState()
        state._file_path = tmp_path / "state.json"
        state.criteria["lint_clean"] = CriterionEntry(mandatory=True)
        state.save()

        from booley.mcp.base import _emit_criteria_update

        # _write_display_event moved to MCP endpoint events (SRP); patch it at its home,
        # where _emit_criteria_update now resolves the name.
        with patch("booley.mcp.events._write_display_event") as mock_write:
            _emit_criteria_update(state)
            mock_write.assert_called_once()
            event = mock_write.call_args[0][0]
            assert event["type"] == "criteria_update"
            assert "lint_clean" in event["criteria"]
            assert event["criteria"]["lint_clean"]["met"] is False
            assert event["criteria"]["lint_clean"]["mandatory"] is True

    def test_criteria_update_excludes_internal_entries(self, tmp_path: Path):
        from booley.criteria.state import CriterionEntry, DevelopmentState

        state = DevelopmentState()
        state._file_path = tmp_path / "state.json"
        state.criteria["sim_pass"] = CriterionEntry(met=True, mandatory=True)
        state.criteria["_blocked_reason"] = CriterionEntry(met=False)
        state.save()

        from booley.mcp.base import _emit_criteria_update

        # _write_display_event moved to MCP endpoint events (SRP); patch it at its home.
        with patch("booley.mcp.events._write_display_event") as mock_write:
            _emit_criteria_update(state)
            event = mock_write.call_args[0][0]
            assert "sim_pass" in event["criteria"]
            assert "_blocked_reason" not in event["criteria"]

    def test_criteria_update_includes_status_history(self, tmp_path: Path):
        from booley.criteria.state import CriterionEntry, DevelopmentState

        state = DevelopmentState()
        state._file_path = tmp_path / "state.json"
        state.criteria["sim_pass"] = CriterionEntry(
            met=False,
            mandatory=True,
            ever_met=True,
            ever_failed=True,
            stale=True,
        )
        state.save()

        from booley.mcp.base import _emit_criteria_update

        with patch("booley.mcp.events._write_display_event") as mock_write:
            _emit_criteria_update(state)

        entry = mock_write.call_args[0][0]["criteria"]["sim_pass"]
        assert entry["stale"] is True
        assert entry["ever_met"] is True
        assert entry["ever_failed"] is True

    def test_initial_criteria_include_status_history(self, tmp_path: Path):
        from booley.criteria.state import CriterionEntry, DevelopmentState
        from booley.harness.console.events import CriteriaChanged

        state_path = tmp_path / "state.json"
        state = DevelopmentState()
        state._file_path = state_path
        state.criteria["sim_pass"] = CriterionEntry(
            met=False,
            mandatory=True,
            ever_met=True,
            stale=True,
        )
        state.save()
        app = MagicMock()

        _push_initial_criteria(state_path, app)

        criteria_event = next(
            call.args[0]
            for call in app.post_message.call_args_list
            if isinstance(call.args[0], CriteriaChanged)
        )
        entry = criteria_event.criteria["sim_pass"]
        assert entry["stale"] is True
        assert entry["ever_met"] is True


# ===========================================================================
# Phase 1a: Configurable poll interval
# ===========================================================================


class TestDisplayWatcherPollInterval:
    def test_default_poll_interval(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        watcher = DisplayWatcher(display)
        assert watcher._poll_interval_s == 2.0

    def test_custom_poll_interval(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        watcher = DisplayWatcher(display, poll_interval_s=0.15)
        assert watcher._poll_interval_s == 0.15


# ===========================================================================
# Phase 1b: New event type routing
# ===========================================================================


def _write_event(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


class TestDisplayWatcherNewEvents:
    def test_specialist_thinking_routes_to_callback(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        display.touch()
        callback = MagicMock()
        watcher = DisplayWatcher(display, on_specialist_thinking=callback)
        watcher._file_pos = 0

        _write_event(display, {"type": "specialist_thinking", "text": "analyzing..."})
        watcher._poll_events()
        callback.assert_called_once_with("analyzing...")

    def test_specialist_thinking_no_callback(self, tmp_path: Path):
        """No callback set → no crash."""
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        _write_event(display, {"type": "specialist_thinking", "text": "thinking"})
        watcher._poll_events()  # should not raise

    def test_specialist_thinking_is_suppressed_inside_endpoint(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        display.touch()
        callback = MagicMock()
        watcher = DisplayWatcher(display, on_specialist_thinking=callback)
        watcher._file_pos = 0

        _write_event(display, {"type": "endpoint_start", "endpoint": "reviewer"})
        _write_event(display, {"type": "specialist_thinking", "text": "still reviewing"})
        with patch("booley.harness.terminal.endpoint_box_open"):
            watcher._poll_events()

        assert watcher.endpoint_active()
        callback.assert_not_called()

    def test_criteria_update_routes_to_callback(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        display.touch()
        callback = MagicMock()
        watcher = DisplayWatcher(display, on_criteria_update=callback)
        watcher._file_pos = 0

        criteria = {"sim_pass": {"met": True, "mandatory": True}}
        _write_event(display, {"type": "criteria_update", "criteria": criteria})
        watcher._poll_events()
        callback.assert_called_once_with(criteria)

    def test_criteria_update_no_callback(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        _write_event(display, {"type": "criteria_update", "criteria": {}})
        watcher._poll_events()  # should not raise

    def test_endpoint_end_summary_routes_to_callback(self, tmp_path: Path):
        display = tmp_path / "display.jsonl"
        display.touch()
        callback = MagicMock()
        watcher = DisplayWatcher(display, on_endpoint_summary=callback)
        watcher._file_pos = 0

        _write_event(
            display,
            {
                "type": "endpoint_end",
                "endpoint": "lint",
                "target": "cfg",
                "exit_code": 0,
                "duration_s": 5.0,
                "cost_usd": 0.0,
                "summary": "clean",
            },
        )
        with patch("booley.harness.terminal.endpoint_box_close"):
            watcher._poll_events()
        callback.assert_called_once_with("lint", "cfg", 0, 5.0, 0.0, "clean", 0, 0, 0, None)

    def test_endpoint_end_empty_summary_still_routed(self, tmp_path: Path):
        """Callback fires even with empty summary (counters still need updating)."""
        display = tmp_path / "display.jsonl"
        display.touch()
        callback = MagicMock()
        watcher = DisplayWatcher(display, on_endpoint_summary=callback)
        watcher._file_pos = 0

        _write_event(
            display,
            {
                "type": "endpoint_end",
                "endpoint": "lint",
                "exit_code": 0,
                "duration_s": 5.0,
            },
        )
        with patch("booley.harness.terminal.endpoint_box_close"):
            watcher._poll_events()
        callback.assert_called_once()

    def test_log_mode_ignores_new_events(self, tmp_path: Path):
        """Log mode (no callbacks) processes new events without errors."""
        display = tmp_path / "display.jsonl"
        display.touch()
        watcher = DisplayWatcher(display)
        watcher._file_pos = 0

        _write_event(display, {"type": "specialist_thinking", "text": "..."})
        _write_event(display, {"type": "criteria_update", "criteria": {}})
        _write_event(
            display,
            {
                "type": "endpoint_end",
                "endpoint": "lint",
                "exit_code": 0,
                "duration_s": 1.0,
                "summary": "ok",
            },
        )
        with patch("booley.harness.terminal.endpoint_box_close"):
            watcher._poll_events()  # should not raise


# ===========================================================================
# Phase 4b: terminal.py Console suppression
# ===========================================================================


class TestTerminalConsoleSuppression:
    def setup_method(self):
        set_console_active(False)

    def teardown_method(self):
        set_console_active(False)

    def test_console_app_reference(self):
        assert get_console_app() is None
        app = object()
        set_console_active(True, app=app)
        assert get_console_app() is app
        set_console_active(False)
        assert get_console_app() is None

    def test_emit_suppresses_stdout_when_console_active(self, capsys):
        from booley.harness import terminal

        set_console_active(True)
        terminal.raw("suppressed line")
        captured = capsys.readouterr()
        assert "suppressed line" not in captured.out
        set_console_active(False)

    def test_emit_writes_stdout_when_console_inactive(self, capsys):
        from booley.harness import terminal

        set_console_active(False)
        terminal.raw("visible line")
        captured = capsys.readouterr()
        assert "visible line" in captured.out

    def test_emit_writes_log_regardless_of_console(self, tmp_path: Path):
        from booley.harness import terminal

        log_path = tmp_path / "run.log"
        terminal.open_log(log_path)
        try:
            set_console_active(True)
            terminal.raw("logged while console active")
            set_console_active(False)
        finally:
            terminal.close_log()

        log_content = log_path.read_text(encoding="utf-8")
        assert "logged while console active" in log_content

    def test_emit_ignores_dead_stdout_but_keeps_log(self, tmp_path: Path):
        from booley.harness import terminal

        log_path = tmp_path / "run.log"
        terminal._stdout_available = True
        terminal.open_log(log_path)
        try:
            with patch("builtins.print", side_effect=OSError(22, "Invalid argument")) as mocked:
                terminal.raw("first line")
                terminal.raw("second line")
        finally:
            terminal.close_log()
            terminal._stdout_available = True

        assert mocked.call_count == 1
        log_content = log_path.read_text(encoding="utf-8")
        assert "first line" in log_content
        assert "second line" in log_content
