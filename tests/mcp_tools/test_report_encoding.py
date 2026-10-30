"""Report budgeting at its SDK-independent interface."""

from __future__ import annotations

import copy
import json

import pytest

from booley.mcp.report_encoding import ReportEncoder

STRUCTURED_LIMIT = 64 * 1024


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_structured_budget_boundary(offset):
    encoder = ReportEncoder()
    report = {"flow": "lint", "passed": False, "report_text": ""}
    overhead = len(json.dumps({"reports": [report], "passed": False}).encode())
    report["report_text"] = "x" * (STRUCTURED_LIMIT - overhead + offset)

    payload = encoder.structured_report(report)

    assert len(json.dumps(payload).encode()) <= STRUCTURED_LIMIT
    assert payload["passed"] is False
    if offset <= 0:
        assert payload["reports"] == [report]
        assert "truncated" not in payload
    else:
        assert payload["reports"] == []
        assert payload["truncated"] is True
        assert payload["flow"] == "lint"


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_unicode_budget_counts_serialized_json_bytes(offset):
    report = {"report_text": "λ" * 100}
    encoder = ReportEncoder()
    overhead = len(json.dumps({"reports": [report]}).encode())
    report["padding"] = "x" * (STRUCTURED_LIMIT - overhead - len(', "padding": ""') + offset)
    payload = encoder.structured_report(report)
    assert len(json.dumps(payload).encode()) <= STRUCTURED_LIMIT
    assert bool(payload.get("truncated")) == (offset > 0)


def test_text_tails_and_whole_line_deduplication(monkeypatch):
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", "13")
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", "12")
    report = {"report_text": "RESULT: FAIL", "detail": {"reason": "invalid design"}}

    text = ReportEncoder().text_result(1, "noise\nRESULT: FAIL", "warning", report)

    assert text.startswith("EXIT_CODE: 1")
    assert "truncated, showing last 13 bytes" in text
    assert text.count("RESULT: FAIL") == 1
    assert "detail.reason: invalid design" in text


def test_report_starting_with_an_indented_table_header_is_deduplicated():
    table = "  Target   Area\n  top      1.0 kGE\n\nRESULT: PASS"

    shown = ReportEncoder().text_result(0, f"noise\n{table}\n", "", {"report_text": table})
    echoed = ReportEncoder().text_result(
        1, "subprocess output:\n    RESULT: FAIL\n", "", {"report_text": "RESULT: FAIL"}
    )

    assert shown.count("RESULT: PASS") == 1
    assert echoed.count("RESULT: FAIL") == 2, "an indented echo is not the verdict"


def test_unicode_text_retains_historical_character_tail(monkeypatch):
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", "3")
    text = ReportEncoder().text_result(0, "αβγδ", "")
    assert text.endswith("βγδ")
    assert "showing last 3 bytes" in text


@pytest.mark.parametrize("value", ["", "bad", "0", "-1"])
def test_invalid_stream_limits_use_existing_defaults(monkeypatch, value):
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", value)
    text = ReportEncoder().text_result(0, "x" * 12001, "")
    assert "showing last 12000 bytes" in text


def test_oversized_implementation_preserves_verdict_metrics_and_recovery_paths():
    implementation = {
        "identity": {"flow": "synth", "target": "dut"},
        "status": {"grade": "fail", "passed": False, "diagnostic_excerpt": "x" * 70000},
        "metrics": {"area_kge": 12.5, "per_clock": {"clk": {"details": "x" * 70000}}},
        "conditions": {"comb_loops": 2},
        "recipe": {"snapshot": "x" * 70000},
        "artifacts": {"log": "reports/synth/1/run.log"},
    }
    report = {"flow": "synth", "passed": False, "implementation": implementation}
    before = copy.deepcopy(report)

    payload = ReportEncoder().structured_report(report)

    assert payload["passed"] is False
    assert payload["truncated"] is True
    assert payload["artifacts"]["log"] == "reports/synth/1/run.log"
    assert payload["implementation"]["metrics"]["area_kge"] == 12.5
    assert payload["implementation"]["conditions"]["comb_loops"] == 2
    assert "per_clock" not in payload["implementation"]["metrics"]
    assert len(json.dumps(payload).encode()) <= STRUCTURED_LIMIT
    assert report == before


def test_oversized_artifacts_are_bounded_without_mutating_report():
    report = {
        "passed": False,
        "report_text": "x" * 70000,
        "artifacts": {f"log-{index}": "λ" * 4000 for index in range(30)},
    }
    before = copy.deepcopy(report)
    payload = ReportEncoder().structured_report(report)
    assert payload["passed"] is False
    assert payload["omitted_artifact_entries"]["count"] > 0
    assert len(payload["omitted_artifact_entries"]["sample"]) <= 10
    assert len(json.dumps(payload).encode()) <= STRUCTURED_LIMIT
    assert report == before


def test_sim_projection_and_live_oversize_keep_nonterminal_semantics():
    active = {"target": "dut", "stage": "executing", "log": {"path": "run.log", "live": True}}
    report = {
        "flow": "sim",
        "run_id": "live",
        "complete": False,
        "partial": True,
        "phase": "running",
        "active": [active],
        "cycle_counts": {"dut": 123},
        "detail": {"large": "x" * 70000},
    }
    before = copy.deepcopy(report)
    payload = ReportEncoder().structured_report(report)
    assert payload["active"] == [active]
    assert payload["complete"] is False and payload["partial"] is True
    assert "passed" not in payload and "exit_code" not in payload
    assert report == before
    small = ReportEncoder().structured_report({"flow": "sim", "cycle_counts": {"dut": 123}})
    assert small == {"reports": [{"flow": "sim"}]}


@pytest.mark.parametrize("report", [None, {}, {"flow": object()}])
def test_absent_or_unserializable_report_falls_back_to_text(report):
    encoder = ReportEncoder()
    assert encoder.structured_report(report) is None
    assert encoder.text_result(2, "", "launch failed").endswith("launch failed")
