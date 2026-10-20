"""Endpoint events and shared criteria display projection."""

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
# Phase 0c: criteria_update event
# ===========================================================================


class TestCriteriaUpdateEvent:
    def test_display_projection_bounds_untrusted_fields(self):
        assert endpoint_events._truncate_utf8("short") == "short"
        assert endpoint_events._truncate_utf8("abcdef", 5) == "ab…"
        assert endpoint_events._bounded_criteria("not-a-mapping") == {}

        safe = endpoint_events._display_safe_event(
            {
                "type": "criteria_update",
                "endpoint": "x" * 3_000,
                "display_lines": [str(index) for index in range(20)],
                "criteria": {
                    "ignored": "not-a-mapping",
                    "lint": {
                        "met": 1,
                        "presentation": {"label": "Lint", "detail": "clean"},
                    },
                },
            }
        )

        assert safe["endpoint"].endswith("…")
        assert len(safe["display_lines"]) == 16
        assert safe["display_lines_truncated"] == 4
        assert safe["criteria"] == {
            "lint": {
                "met": True,
                "presentation": {"label": "Lint", "detail": "clean"},
            }
        }

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

    def test_minimal_criteria_marks_omitted_entries(self, monkeypatch):
        monkeypatch.setattr(endpoint_events, "MAX_DISPLAY_EVENT_BYTES", 70)
        minimal = endpoint_events._minimal_display_event(
            {
                "type": "criteria_update",
                "criteria": {"criterion-with-a-long-name": {"met": True}},
            }
        )

        assert minimal["criteria"] == {}
        assert minimal["omitted_criteria"] is True

    def test_write_failure_is_best_effort(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
        with patch("pathlib.Path.open", side_effect=OSError("read only")):
            endpoint_events._write_display_event({"type": "endpoint_start"})

    def test_criteria_update_emitted_on_set_criterion(self, tmp_path: Path):
        from booley.criteria.state import CriterionEntry, DevelopmentState

        state = DevelopmentState()
        state._file_path = tmp_path / "state.json"
        state.criteria["lint_clean"] = CriterionEntry(mandatory=True)
        state.save()

        from booley.flows.endpoint_events import _emit_criteria_update

        # _write_display_event lives in endpoint events; patch it at its home,
        # where _emit_criteria_update now resolves the name.
        with patch("booley.flows.endpoint_events._write_display_event") as mock_write:
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

        from booley.flows.endpoint_events import _emit_criteria_update

        # _write_display_event lives in endpoint events; patch it at its home.
        with patch("booley.flows.endpoint_events._write_display_event") as mock_write:
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

        from booley.flows.endpoint_events import _emit_criteria_update

        with patch("booley.flows.endpoint_events._write_display_event") as mock_write:
            _emit_criteria_update(state)

        entry = mock_write.call_args[0][0]["criteria"]["sim_pass"]
        assert entry["stale"] is True
        assert entry["ever_met"] is True
        assert entry["ever_failed"] is True

    def test_criteria_update_is_a_bounded_presentation_projection(self, tmp_path: Path):
        from booley.criteria.state import CriterionEntry, DevelopmentState

        state = DevelopmentState()
        state.criteria["synthesis_ok_asic"] = CriterionEntry(
            met=False,
            mandatory=True,
            detail={"warning": "PRIVATE-EVIDENCE-" + "x" * (3 * 1024 * 1024)},
            params={"target": "asic"},
        )

        from booley.flows.endpoint_events import _emit_criteria_update

        with patch("booley.flows.endpoint_events._write_display_event") as mock_write:
            _emit_criteria_update(state)

        event = mock_write.call_args.args[0]
        encoded = json.dumps(event).encode("utf-8")
        entry = event["criteria"]["synthesis_ok_asic"]
        assert len(encoded) <= 64 * 1024
        assert "PRIVATE-EVIDENCE" not in encoded.decode("utf-8")
        assert "detail" not in entry
        assert "params" not in entry
        assert entry["presentation"]["label"] == "ASIC synthesis · asic"

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
            {
                "type": "criteria_update",
                "criteria": {
                    "huge": {
                        "met": False,
                        "presentation": {"label": "x" * (3 * 1024 * 1024)},
                        "detail": {"raw": "must not be copied"},
                    }
                },
            },
        )
        for event in events:
            _write_display_event(event)

        records = (runtime / "display.jsonl").read_bytes().splitlines(keepends=True)
        assert [json.loads(record)["type"] for record in records] == [
            event["type"] for event in events
        ]
        assert all(len(record) <= 64 * 1024 for record in records)

    def test_initial_and_incremental_criteria_use_the_same_projection(self, tmp_path: Path):
        from booley.criteria.state import (
            CriterionEntry,
            DevelopmentState,
        )
        from booley.flows.endpoint_events import _emit_criteria_update

        state_path = tmp_path / "state.json"
        state = DevelopmentState()
        state._file_path = state_path
        state.criteria["lint_clean_rtl"] = CriterionEntry(
            met=False,
            mandatory=True,
            ever_failed=True,
            detail={"warnings": 3},
            params={"target": "rtl"},
        )
        state.save()
        from booley.criteria.presentation import criteria_display_snapshot

        initial = criteria_display_snapshot(DevelopmentState.load(state_path))
        with patch("booley.flows.endpoint_events._write_display_event") as mock_write:
            _emit_criteria_update(state)

        assert mock_write.call_args.args[0]["criteria"] == initial

    def test_initial_criteria_include_status_history(self, tmp_path: Path):
        from booley.criteria.state import (
            CriterionEntry,
            DevelopmentState,
        )

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
        from booley.criteria.presentation import criteria_display_snapshot

        entry = criteria_display_snapshot(DevelopmentState.load(state_path))["sim_pass"]
        assert entry["stale"] is True
        assert entry["ever_met"] is True


# ===========================================================================
