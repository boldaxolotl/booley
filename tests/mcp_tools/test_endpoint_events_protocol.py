"""Endpoint display events and their byte limits."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from booley.flows import endpoint_events
from booley.flows.endpoint_events import _endpoint_end_event, _endpoint_start_event
from booley.mcp.base import McpToolResult
from booley.runtime.display_identity import DisplayIdentity, DisplayScope

# ===========================================================================
# Summary field in endpoint_end
# ===========================================================================


class TestToolEndSummary:
    def test_endpoint_end_event_includes_summary(self):
        result = McpToolResult(exit_code=0, summary="clean")
        event = _endpoint_end_event("lint", "cfg_a", result, 5.0)
        assert event["summary"] == "clean"

    def test_endpoint_end_event_empty_summary(self):
        result = McpToolResult(exit_code=0)
        event = _endpoint_end_event("lint", None, result, 2.0)
        assert event["summary"] == ""

    def test_endpoint_events_include_additive_display_label(self):
        result = McpToolResult(exit_code=0)

        start = _endpoint_start_event(
            "sim",
            "::lib:core:0#sim_core",
            display_label="target sim_core · test smoke",
            identity=DisplayIdentity("sim-run-1", DisplayScope.DEVELOPER),
        )
        end = _endpoint_end_event(
            "sim",
            "::lib:core:0#sim_core",
            result,
            2.0,
            display_label="target sim_core · test smoke",
            identity=DisplayIdentity("sim-run-1", DisplayScope.DEVELOPER),
        )

        assert start["target"] == "::lib:core:0#sim_core"
        assert end["target"] == "::lib:core:0#sim_core"
        assert start["display_label"] == end["display_label"]
        assert start["invocation_id"] == end["invocation_id"] == "sim-run-1"
        assert start["display_scope"] == end["display_scope"] == "developer"


# ===========================================================================
# Display records
# ===========================================================================


class TestDisplayRecords:
    def test_oversized_records_fall_back_to_minimal_or_dropped(self, monkeypatch):
        oversized = {
            "type": "endpoint_end",
            "endpoint": "lint",
            "unexpected": "x" * (endpoint_events.MAX_DISPLAY_EVENT_BYTES + 1),
        }
        minimal = json.loads(endpoint_events._serialize_display_event(oversized))
        assert minimal == {"type": "endpoint_end", "endpoint": "lint", "truncated": True}

        monkeypatch.setattr(endpoint_events, "MAX_DISPLAY_EVENT_BYTES", 50)
        dropped = json.loads(endpoint_events._serialize_display_event(oversized))
        assert dropped == {"type": "display_event_dropped", "truncated": True}

    def test_write_failure_is_best_effort(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
        with patch("pathlib.Path.open", side_effect=OSError("read only")):
            endpoint_events._write_display_event({"type": "endpoint_start"})

    def test_every_display_record_has_a_hard_byte_limit(self, tmp_path: Path, monkeypatch):
        runtime = tmp_path / ".runtime"
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime))
        from booley.flows.endpoint_events import _write_display_event

        events = (
            {
                "type": "endpoint_end",
                "endpoint": "reviewer",
                "report_text": "x" * (3 * 1024 * 1024),
                "display_lines": ["y" * (3 * 1024 * 1024)],
            },
            {"type": "endpoint_progress", "line": "x" * (3 * 1024 * 1024)},
            {"type": "specialist_thinking", "text": "x" * (3 * 1024 * 1024)},
        )
        for event in events:
            _write_display_event(event)

        records = (runtime / "display.jsonl").read_bytes().splitlines(keepends=True)
        assert [json.loads(record)["type"] for record in records] == [
            event["type"] for event in events
        ]
        assert all(len(record) <= 64 * 1024 for record in records)


# ===========================================================================
