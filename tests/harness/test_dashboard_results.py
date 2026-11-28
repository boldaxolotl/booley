"""The headline and result word the web Dashboard and the Job notice read from a report."""

from __future__ import annotations

import pytest

from booley.presentation.report_results import present_report, project_facts


def simulation_record(passed=2, failed=0):
    return {
        "grade": "fail" if failed else "pass",
        "observation_counts": {
            "execution": {"completed": passed + failed},
            "functional": {"pass": passed, "fail": failed},
            "assertions": {"pass": passed + failed},
        },
        "observations": [
            {
                "test": "test_bad",
                "functional": "fail",
                "failure_kind": "assertion",
                "cycle_count": 23,
            }
        ]
        if failed
        else [{"test": "test_ok", "functional": "pass"}],
    }


def implementation(grade="pass"):
    return {
        "grade": grade,
        "results": {
            "top": {
                "status": {
                    "grade": grade,
                    "failure_reasons": ["timing missed"] if grade == "fail" else [],
                },
                "metrics": {
                    "area_kge": 12,
                    "latches": 1,
                    "wns_ns": -0.2,
                    "per_clock": {"clk": {"wns_ns": -0.2, "fmax_mhz": 190}},
                },
                "conditions": {"has_critical": True},
            }
        },
    }


def report_of(endpoint, detail, exit_code=0):
    key = "flow" if endpoint in {"sim", "lint", "synth", "fpga", "mutation"} else "mcp_tool"
    return {key: endpoint, "exit_code": exit_code, "detail": detail}


HEADLINES = [
    (
        "sim",
        {"records": {"top": {**simulation_record(), "grade": "inconclusive"}}},
        "grade inconclusive",
    ),
    ("sim", {"coverage": True, "targets": ["alpha", "beta"]}, "Coverage: evaluation unavailable"),
    *[
        (
            "reviewer",
            {"contract": {"mode": "done"}, "review_outcome": outcome, "issues": 0},
            f"Review: {outcome}",
        )
        for outcome in ("advisory", "corrective", "no_findings")
    ],
    ("sim", {"records": {"top": simulation_record()}}, "2/2 tests passed"),
    (
        "sim",
        {"mode": "elab-only", "targets": [{"target": "top", "passed": True}]},
        "1/1 Targets elaborated",
    ),
    (
        "sim",
        {
            "targets": 2,
            "targets_passed": 1,
            "elab_failed": True,
            "cycle_counts": [
                {"target": "top", "test": "legacy_bad", "verdict": "FAIL", "cycles": None}
            ],
        },
        "1/2 Targets passed",
    ),
    (
        "sim",
        {
            "coverage": True,
            "targets": {
                "top": {"simulation": "pass", "collection": "complete", "evaluation": "fail"}
            },
            "records": {"top": simulation_record(1, 1)},
        },
        "Coverage: 0/1 Targets met",
    ),
    (
        "lint",
        {"targets": ["top"], "total_warnings": 3, "eda_tools": {"top": "verilator"}},
        "Lint: 3 warnings",
    ),
    ("synth", {"implementation": implementation()}, "Synthesis: 1 Target"),
    (
        "fpga",
        {
            "implementation": {
                "grade": "fail",
                "results": {
                    "top": {
                        "status": {"grade": "fail", "failure_reasons": ["timing missed"]},
                        "metrics": {
                            "lut_count": 10,
                            "ff_count": 20,
                            "bram_count": 2,
                            "dsp_count": 3,
                            "per_clock": {"clk": {"wns_ns": -1, "fmax_mhz": 100}},
                        },
                    }
                },
            }
        },
        "FPGA: 1 Target",
    ),
    (
        "reviewer",
        {"mode": "clean", "gate_passed": True, "issues": 0, "CRITICAL": 0},
        "Review: 0 pending",
    ),
    (
        "reviewer",
        {
            "mode": "done",
            "review_outcome": "advisory",
            "issues": 2,
            "MAJOR": 1,
            "gate_passed": True,
        },
        "Review: advisory",
    ),
    (
        "mutation",
        {
            "detected": 3,
            "not_detected": 1,
            "total_valid": 4,
            "min_detected": 3,
            "classified": [
                {"status": "not_detected", "category": "logic", "file": "unit.sv", "line": 12}
            ],
        },
        "Mutation: 3/4 detected",
    ),
    (
        "coverage_analyst",
        {
            "closure_recommendation": "coverage_not_ready",
            "eligibility": "eligible",
            "recommendations": ["change workload"],
            "waiver_candidates": ["w"],
            "hypotheses": ["h1", "h2"],
        },
        "Coverage analysis: coverage_not_ready",
    ),
]


def test_each_flow_report_headline():
    for endpoint, detail, headline in HEADLINES:
        result = present_report(report_of(endpoint, detail))
        assert headline in result.flow_result.value, "each Flow needs its own compact headline"
        assert "{'" not in result.flow_result.value and '{"' not in result.flow_result.value
        if endpoint == "coverage_analyst" or (
            endpoint == "reviewer" and detail.get("mode") == "done"
        ):
            assert not result.flow_result.value.startswith("PASSED")


def test_exit_taxonomy_and_soft_warnings():
    for code, endpoint, detail, word, semantic in [
        (2, "sim", {}, "ERROR", "attention"),
        (130, "sim", {}, "CANCELLED", "unknown"),
        (1, "sim", {}, "FAILED", "fail"),
        (0, "sim", {}, "PASSED", "pass"),
        (0, "synth", {"implementation": implementation("warn")}, "PASSED with warnings", "warn"),
        (0, "fpga", {"implementation": implementation("warn")}, "PASSED with warnings", "warn"),
        (0, "lint", {"total_warnings": 2}, "PASSED with warnings", "warn"),
        (None, "sim", {}, "unavailable", "unknown"),
        (17, "sim", {}, "unavailable", "unknown"),
        (
            0,
            "coverage_analyst",
            {"closure_recommendation": "coverage_not_ready"},
            "coverage_not_ready",
            "unknown",
        ),
        (0, "reviewer", {"mode": "done", "review_outcome": "advisory"}, "advisory", "unknown"),
        (
            2,
            "coverage_analyst",
            {"closure_recommendation": "coverage_ready"},
            "ERROR",
            "attention",
        ),
        (
            130,
            "reviewer",
            {"mode": "done", "review_outcome": "no_findings"},
            "CANCELLED",
            "unknown",
        ),
    ]:
        result = present_report(report_of(endpoint, detail, code))
        assert result.flow_word.value == word, "the exit code owns the result taxonomy"
        assert result.flow_result.semantic == semantic


def test_headlines_aggregate_over_every_target():
    records = {f"t{i}": simulation_record(2, 1 if i < 5 else 0) for i in range(8)}
    result = present_report(report_of("sim", {"records": records}))
    assert "16/21 tests passed" in result.flow_result.value

    result = present_report(report_of("sim", {"records": {"top": {}}}))
    assert "unavailable" in result.flow_result.value
    targets = {
        "one": {"status": {"grade": "pass"}},
        "two": {"status": {"grade": "warn"}},
        "three": {"status": {"grade": "fail"}},
    }
    result = present_report(report_of("synth", {"implementation": {"results": targets}}))
    assert "2/3 Targets passed" in result.flow_result.value


@pytest.mark.parametrize(
    "endpoint", ["sim", "synth", "lint", "reviewer", "mutation", "coverage_analyst"]
)
@pytest.mark.parametrize(
    "detail",
    [{"eda_tool_error": "launch failed"}, {"total_warnings": {"bad": 1}}, {"issues": None}],
)
def test_unrecognized_detail_adds_no_headline(endpoint, detail):
    result = present_report(report_of(endpoint, detail, 2))
    assert result.flow_result.value == "ERROR"


def test_lint_preparation_failure_adds_no_headline():
    detail = {"plan": {}, "metrics": {"t0": {"unavailable": "lint setup failed: boom"}}}
    assert present_report(report_of("lint", detail, 2)).flow_result.value == "ERROR"


@pytest.mark.parametrize(
    "endpoint,detail,headline",
    [
        ("lint", {"eda_tools": {"t": "verible"}}, "Lint: warning count unavailable · verible"),
        ("lint", {"total_warnings": "many"}, "Lint: warning count unavailable"),
        ("lint", {"targets": ["t"]}, "Lint: warning count unavailable"),
        ("reviewer", {"MAJOR": 1}, "Review: unavailable pending"),
        ("reviewer", {"gate_passed": False}, "Review: unavailable pending"),
        ("mutation", {"total_valid": 4}, "Mutation: unavailable/4 detected"),
        ("mutation", {"not_detected": 1}, "Mutation: unavailable/unavailable detected"),
        ("mutation", {"min_detected": 3}, "Mutation: unavailable/unavailable detected (min 3)"),
        ("mutation", {"survivors": ["m1"]}, "Mutation: unavailable/unavailable detected"),
        (
            "mutation",
            {"classified": [{"status": "not_detected"}]},
            "Mutation: unavailable/unavailable detected",
        ),
        ("coverage_analyst", {"eligibility": "eligible"}, "Coverage analysis: unavailable"),
        ("coverage_analyst", {"hypotheses": []}, "Coverage analysis: unavailable"),
    ],
)
def test_partial_report_keeps_a_headline(endpoint, detail, headline):
    result = present_report(report_of(endpoint, detail, 2))
    assert result.flow_result.value == f"ERROR · {headline}"


def test_goal_metric_evidence_keeps_measurements_and_omission_marker():

    facts = {
        "producer": "synth",
        "detail": {
            "area_kge": 12,
            "cells": 42,
            "wns_ns": -0.2,
            "per_clock": {"clk": {"fmax_mhz": 210}},
            "novel_metric": 99,
        },
    }
    rows = project_facts(facts)
    text = "\n".join(f"{row.label}: {row.value}" for row in rows)
    assert "Area: 12" in text, "Goal evidence must preserve producer measurements"
    assert "Cells: 42" in text and "WNS: -0.2" in text
    assert "Fmax: 210" in text and "clk" in text
    assert "Extra data in report" in text


def test_multiline_proposal_facts_preserve_human_text():

    for key in ("rationale", "reason", "quote"):
        rows = project_facts({key: "first line\nsecond\tpart", "source": "human"})
        text = "\n".join(f"{row.label}: {row.value}" for row in rows)
        assert "first line second part" in text, (
            "ordinary multiline human facts must remain readable"
        )
        assert "\t" not in text
    assert "danger" not in str(project_facts({"rationale": "\x1b[31mdanger"}))
