"""P0 event-to-watcher-to-Console reconciliation tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.harness.console.app import ConsolePhase
from booley.harness.console.events import (
    AgentThinking,
    CriteriaChanged,
    McpToolCompleted,
    McpToolProgress,
    McpToolStarted,
)
from booley.harness.console.widgets import MainPane, StatusBar, TicketHeader
from booley.harness.developer_display import DisplayWatcher

from .console_scenario import ConsoleTestApp

LEDGER_EVENTS: list[dict | str] = [
    "not-json",
    {"type": "endpoint_start", "endpoint": "lint", "target": "rtl"},
    {"type": "endpoint_progress", "line": "checking rtl/top.sv"},
    {
        "type": "endpoint_end",
        "endpoint": "lint",
        "target": "rtl",
        "exit_code": 0,
        "duration_s": 1.0,
        "summary": "clean",
        "lines_added": 3,
    },
    {"type": "endpoint_start", "endpoint": "reviewer", "target": "correctness"},
    {"type": "specialist_thinking", "text": "checking invariants"},
    {"type": "endpoint_start", "endpoint": "lint", "target": "nested"},
    {
        "type": "endpoint_end",
        "endpoint": "lint",
        "target": "nested",
        "exit_code": 0,
        "duration_s": 99.0,
        "cost_usd": 9.0,
        "input_tokens": 9000,
        "output_tokens": 9000,
        "lines_added": 900,
    },
    {
        "type": "endpoint_end",
        "endpoint": "reviewer",
        "target": "correctness",
        "exit_code": 1,
        "duration_s": 2.0,
        "cost_usd": 0.004,
        "input_tokens": 100,
        "output_tokens": 50,
        "summary": "one finding",
    },
    {
        "type": "endpoint_end",
        "endpoint": "tb_coder",
        "target": "tb/top_tb.sv",
        "exit_code": 0,
        "duration_s": 3.0,
        "cost_usd": 0.006,
        "input_tokens": 200,
        "output_tokens": 100,
        "lines_added": 9,
        "lines_removed": 2,
        "summary": "fixed",
        "display_lines": ["REPORT.md written"],
    },
    {
        "type": "criteria_update",
        "criteria": {"sim_pass": {"met": True, "mandatory": True}},
    },
    {"type": "future_event", "value": 1},
]


def _append_events(path: Path, events: list[dict | str]) -> None:
    lines = [event if isinstance(event, str) else json.dumps(event) for event in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _watcher_for_app(path: Path, app: ConsoleTestApp) -> DisplayWatcher:
    return DisplayWatcher(
        path,
        on_endpoint_start=lambda name, target: app.post_message(McpToolStarted(name, target)),
        on_endpoint_progress=lambda line: app.post_message(McpToolProgress(line)),
        on_specialist_thinking=lambda text: app.post_message(
            AgentThinking(text, is_specialist=True)
        ),
        on_criteria_update=lambda criteria: app.post_message(CriteriaChanged(criteria)),
        on_endpoint_summary=lambda name, target, code, duration, cost, summary, out_tok, added, removed, display: (
            app.post_message(
                McpToolCompleted(
                    name,
                    target,
                    code,
                    duration,
                    cost,
                    summary,
                    output_tokens=out_tok,
                    lines_added=added,
                    lines_removed=removed,
                    display_lines=display,
                )
            )
        ),
    )


@pytest.mark.asyncio
async def test_jsonl_stream_reconciles_boxes_strips_and_status_ledger(tmp_path: Path) -> None:
    """MAIN-14/STS ledger: only valid outermost completions contribute."""
    display = tmp_path / "display.jsonl"
    _append_events(display, LEDGER_EVENTS)

    app = ConsoleTestApp()
    watcher = _watcher_for_app(display, app)
    watcher._file_pos = 0
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        app.transition_to(ConsolePhase.RUNNING)
        with (
            patch("booley.harness.terminal.endpoint_box_open"),
            patch("booley.harness.terminal.endpoint_progress_line"),
            patch("booley.harness.terminal.endpoint_box_close"),
        ):
            watcher._poll_events()
        await pilot.pause()

        main = app.query_one(MainPane)
        marks = main.get_completion_marks()
        assert [mark.name for mark in marks] == ["lint", "reviewer", "tb_coder"]
        assert marks[2].start_line > marks[1].end_line
        assert [mark.cost_usd for mark in marks] == [0.0, 0.004, 0.006]
        assert [mark.summary for mark in marks] == ["clean", "one finding", "fixed"]
        content = main._content.plain
        assert "checking invariants" not in content
        assert "┌─ tb_coder [tb/top_tb.sv]" in content
        assert "REPORT.md written" in content
        assert "nested" not in content

        status = app.query_one(StatusBar)
        # Output tokens only: 50 (reviewer) + 100 (tb_coder). The nested lint
        # is excluded as before, and specialist *input* no longer contributes.
        assert status._output_tokens == 150
        assert status._cost_usd == pytest.approx(0.01)
        assert status._lines_added == 12
        assert status._lines_removed == 2

        header = app.query_one(TicketHeader)
        assert header._criteria["sim_pass"]["met"] is True


def test_watcher_stop_during_partial_record_is_prompt(tmp_path: Path) -> None:
    """LIFE-09: teardown never waits for the normal poll interval."""
    display = tmp_path / "display.jsonl"
    display.write_text('{"type":"endpoint_end"', encoding="utf-8")
    watcher = DisplayWatcher(display, poll_interval_s=60.0)
    watcher.start()
    watcher.stop()
    assert watcher._thread is None


def test_partial_json_record_is_retried_after_writer_finishes(tmp_path: Path) -> None:
    """MAIN-14: polling between write chunks must not discard the event."""
    display = tmp_path / "display.jsonl"
    display.write_text('{"type":"endpoint_start"', encoding="utf-8")
    callback = MagicMock()
    watcher = DisplayWatcher(display, on_endpoint_start=callback)

    watcher._poll_events()
    callback.assert_not_called()
    assert watcher._file_pos == 0

    with display.open("a", encoding="utf-8") as stream:
        stream.write(',"endpoint":"lint","target":"rtl"}\n')
    with patch("booley.harness.terminal.endpoint_box_open"):
        watcher._poll_events()

    callback.assert_called_once_with("lint", "rtl")
    assert watcher._file_pos == display.stat().st_size
