"""Human result contracts at the existing Dashboard boundary and pure projection."""

from dataclasses import replace
from pathlib import Path

from rich.console import Console
from rich.style import Style

from booley.harness.dashboard.app import (
    SEMANTIC_STYLE,
    DashboardApp,
    job_detail,
    job_paths,
)
from booley.harness.dashboard.jobs import JobView
from booley.harness.dashboard.links import linked_text
from booley.presentation.report_results import ResultLine, present_report, project_facts
from booley.runtime.job_records import JobRecord

STAMP = "2026-10-08T10:00:00Z"


def job(report=None, exit_code=0, endpoint="sim", **kwargs):
    return JobView(
        Path("/jobs"),
        JobRecord("one", endpoint, STAMP, 60, status="done", exit_code=exit_code, ended_at=STAMP),
        "completed",
        report=report,
        **kwargs,
    )


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


def test_details_never_dump_structures():
    texts = [
        job_detail(
            job(
                {
                    "detail": {"arbitrary": {"secret": 1}},
                    "report_text": "{\"bad\": 1}\n{'bad': 2}\n\x1b[31mbad",
                }
            )
        ),
    ]
    for text in texts:
        assert "{'" not in text and '{"' not in text and "json.dumps" not in text, (
            "structured data must become readable facts"
        )
        assert "\x1b" not in text
    assert (
        present_report({"flow": "unknown", "report_text": '{"secret": 1}'}).result_rows[0].value
        == "Summary unavailable"
    )


def test_older_report_criterion_fields_show_no_criterion_row():
    for criterion in [
        {"criterion_key": "coverage_top", "criterion_met": False},
        {"criterion_key": "coverage_top", "criterion_met": True},
        {"criterion_key": "", "criterion_met": False},
        {"criterion_met": True},
        {"criterion_key": "sim_top", "criterion_met": None},
        {"criterion_key": "sim_top"},
    ]:
        report = {"flow": "sim", "exit_code": 0, **criterion}
        text = job_detail(job(report))
        assert "Flow result: PASSED" in text
        assert "Criterion" not in text and "not met" not in text
        assert text.splitlines()[0].startswith("Flow result:")

        result = present_report(report)
        assert not hasattr(result, "criterion")
    styled = DashboardApp(lambda: None)._job_detail(job(report))
    assert styled.get_style_at_offset(Console(), styled.plain.index("PASSED")) == Style.parse(
        SEMANTIC_STYLE["pass"]
    )


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
        report = {
            "flow"
            if endpoint in {"sim", "lint", "synth", "fpga", "mutation"}
            else "mcp_tool": endpoint,
            "exit_code": 0 if code != 0 else 1,
            "passed": code != 0,
            "detail": detail,
        }
        item = job(report, code, endpoint)
        assert f"Flow result: {word}" in job_detail(item), (
            "JobRecord exit owns the result taxonomy"
        )

        assert present_report({**report, "exit_code": code}).flow_result.semantic == semantic
        app = DashboardApp(lambda: None)
        styled = app._job_detail(item)
        assert styled.get_style_at_offset(Console(), styled.plain.index(word)) == Style.parse(
            SEMANTIC_STYLE[semantic]
        )


ROWS = [
    (
        "sim",
        {"records": {"top": {**simulation_record(), "grade": "inconclusive"}}},
        "grade inconclusive",
        [],
    ),
    (
        "sim",
        {"coverage": True, "targets": ["alpha", "beta"]},
        "Coverage: evaluation unavailable",
        ["alpha", "beta"],
    ),
    *[
        (
            "reviewer",
            {"contract": {"mode": "done"}, "review_outcome": outcome, "issues": 0},
            f"Review: {outcome}",
            [],
        )
        for outcome in ("advisory", "corrective", "no_findings")
    ],
    ("sim", {"records": {"top": simulation_record()}}, "2/2 tests passed", ["top"]),
    (
        "sim",
        {"mode": "elab-only", "targets": [{"target": "top", "passed": True}]},
        "1/1 Targets elaborated",
        ["top"],
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
        ["legacy_bad", "Elaboration failed: yes"],
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
        ["evaluation fail", "1/2 tests passed", "test_bad", "23"],
    ),
    (
        "lint",
        {"targets": ["top"], "total_warnings": 3, "eda_tools": {"top": "verilator"}},
        "Lint: 3 warnings",
        ["verilator"],
    ),
    (
        "synth",
        {"implementation": implementation()},
        "Synthesis: 1 Target",
        ["Area: 12", "WNS: -0.2", "Fmax: 190", "Latches: 1", "Critical: yes"],
    ),
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
        ["timing missed", "LUTs: 10", "FFs: 20", "BRAMs: 2", "DSPs: 3", "WNS: -1"],
    ),
    (
        "reviewer",
        {"mode": "clean", "gate_passed": True, "issues": 0, "CRITICAL": 0},
        "Review: 0 pending",
        ["Gate: passed"],
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
        ["Major: 1", "Gate: passed"],
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
        ["unit.sv:12", "min 3"],
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
        ["Waiver candidates: 1", "Hypotheses: 2", "Eligibility: eligible", "Recommendations: 1"],
    ),
]


def test_each_flow_report_row():
    for endpoint, detail, headline, required in ROWS:
        report = {
            "flow"
            if endpoint in {"sim", "lint", "synth", "fpga", "mutation"}
            else "mcp_tool": endpoint,
            "exit_code": 0,
            "detail": detail,
        }
        text = job_detail(job(report, endpoint=endpoint))
        assert headline in text, "each Flow needs its own compact human headline"
        assert all(part in text for part in required)
        assert "{'" not in text and '{"' not in text

        result = present_report(report)
        if endpoint == "coverage_analyst" or (
            endpoint == "reviewer" and detail.get("mode") == "done"
        ):
            assert not result.flow_result.value.startswith("PASSED")
        pure = "\n".join(
            f"{line.label}: {line.value}" for line in (result.flow_result, *result.result_rows)
        )
        assert headline in pure and all(part in pure for part in required)


def test_mutation_detail_has_no_invalid_row():
    """A Mutant that does not build is replaced, so no result states an invalid count."""
    detail = {"detected": 3, "not_detected": 1, "invalid": 2, "total_valid": 4, "min_detected": 3}
    result = present_report({"flow": "mutation", "exit_code": 0, "detail": detail})

    labels = [line.label for line in result.result_rows]
    assert "Not detected" in labels
    assert not any("invalid" in label.lower() for label in labels)
    assert "Invalid" not in job_detail(job({"flow": "mutation", "exit_code": 0, "detail": detail}))


def test_report_locations_and_coverage_miss():
    for key in [False, True]:
        for evaluation in ["pass", "fail", "blocked"]:
            report = {
                "flow": "sim",
                "exit_code": 0,
                "detail": {
                    "coverage": True,
                    "targets": {
                        "top": {"evaluation": evaluation},
                        "other": {"evaluation": "pass"},
                    },
                    "records": {"top": simulation_record()},
                },
            }
            if key:
                report.update(criterion_key="coverage_top", criterion_met=evaluation == "pass")
            text = job_detail(job(report))
            assert "Flow result: PASSED" in text and f"evaluation {evaluation}" in text
            assert "Criterion:" not in text

            assert present_report(report).flow_result.semantic == "pass"


def test_multitarget_and_list_caps():
    simulation_records = {f"t{i}": simulation_record(2, 1 if i < 5 else 0) for i in range(8)}
    report = {"flow": "sim", "detail": {"records": simulation_records}}
    text = job_detail(job(report))
    assert "16/21 tests passed" in text, "aggregate counts must use canonical observation counts"
    assert "3 more Targets" in text and "t7:" not in text
    simulation_records = {
        "top": {
            "observation_counts": {"functional": {"pass": 2, "fail": 8}},
            "observations": [{"test": f"bad{i}", "functional": "fail"} for i in range(7)],
            "observations_truncated": True,
        }
    }
    text = job_detail(job({"flow": "sim", "detail": {"records": simulation_records}}))
    assert "3 more failures" in text and "bad6" not in text
    assert "preview" in text

    result = present_report({"flow": "sim", "detail": {"records": {"top": {}}}})
    assert "unavailable" in result.flow_result.value
    assert len(result.result_rows) <= 24
    targets = {
        "one": {"status": {"grade": "pass"}},
        "two": {"status": {"grade": "warn"}},
        "three": {"status": {"grade": "fail"}},
    }
    result = present_report({"flow": "synth", "detail": {"implementation": {"results": targets}}})
    assert "2/3 Targets passed" in result.flow_result.value
    assert [line.label for line in result.result_rows[:3]] == ["  one", "  two", "  three"]


def test_configuration_and_artifact_links():
    item = job(
        {"detail": {}},
        artifacts=("/a/log", "/a/log", "/a/two", "/a/three", "/a/four"),
        report_path=Path("/a/report"),
    )
    item = replace(
        item,
        record=replace(
            item.record,
            ended_at="",
            argv=[
                "python",
                "-m",
                "private.module",
                "--target",
                "top",
                "--tests",
                "a,b",
                "--mode",
                "run",
                "--report-dir",
                "/internal",
            ],
        ),
    )
    text = job_detail(item)
    assert "python -m" not in text, "configuration exposes public selection flags only"
    assert "Target: top" in text and "Tests: a,b" in text and "Mode: run" in text
    assert "1 more artifacts" in text and text.splitlines()[-1] == str(Path("/a/report"))
    for field in ("Peak memory:", "Stage:", "End:", "EDA tool:"):
        assert field not in text
    assert job_paths(item) == ("/a/log", "/a/two", "/a/three", str(Path("/a/report")))
    links = linked_text(text, job_paths(item))
    assert len(links.spans) == 4


def test_simulation_record_producer_failure_axes_are_visible():

    for execution, functional, assertions in [
        ("crash", "not_observed", "not_observed"),
        ("aborted", "not_observed", "not_observed"),
        ("setup_error", "not_observed", "not_observed"),
        ("blocked_by_build", "not_observed", "not_observed"),
        ("not_run", "not_observed", "not_observed"),
        ("timeout", "not_observed", "not_observed"),
        ("completed", "pass", "dirty"),
        ("completed", "inconclusive", "clean"),
    ]:
        observation = {
            "test": "producer_failure",
            "execution": execution,
            "functional": functional,
            "assertions": assertions,
            "failure_class": "infrastructure" if execution == "aborted" else "design",
            "assertion_count": 1 if assertions == "dirty" else 0,
            "detail": {"reason": "producer diagnostic"},
            "cycle_count": None,
        }
        counts = {
            axis: {observation[axis]: 1} for axis in ("execution", "functional", "assertions")
        }
        report = {
            "flow": "sim",
            "exit_code": 1,
            "detail": {
                "records": {
                    "top": {
                        "grade": "fail",
                        "observation_counts": counts,
                        "observations": [observation],
                    }
                }
            },
        }
        result = present_report(report)
        text = "\n".join(f"{row.label}: {row.value}" for row in result.result_rows)
        assert "producer_failure" in text, "producer failure test name must remain visible"
        assert observation["failure_class"] in text


def test_multitarget_implementation_metrics_keep_target_names():
    for endpoint in ("synth", "fpga"):
        targets = {
            name: {
                "status": {"grade": "fail", "failure_reasons": [reason]},
                "metrics": {"area_kge": area, "per_clock": {"clk": {"wns_ns": slack}}},
            }
            for name, area, slack, reason in (
                ("alpha", 12, -1, "alpha timing"),
                ("beta", 24, -2, "beta timing"),
            )
        }
        result = present_report(
            {"flow": endpoint, "exit_code": 1, "detail": {"implementation": {"results": targets}}}
        )
        assert result.result_rows == (
            ResultLine("  alpha", "fail", "fail"),
            ResultLine("  beta", "fail", "fail"),
            ResultLine("  alpha · Area", "12"),
            ResultLine("  alpha · Clock clk", "WNS: -1"),
            ResultLine("  alpha · Failure reason", "alpha timing", "fail"),
            ResultLine("  beta · Area", "24"),
            ResultLine("  beta · Clock clk", "WNS: -2"),
            ResultLine("  beta · Failure reason", "beta timing", "fail"),
        ), "implementation metrics must identify their Target"


def test_known_endpoint_unknown_detail_uses_safe_fallback():

    for endpoint in ("lint", "reviewer", "mutation", "coverage_analyst"):
        for detail in (
            {"eda_tool_error": "launch failed"},
            {"total_warnings": {"bad": 1}},
            {"issues": None},
        ):
            report = {
                "flow" if endpoint in {"lint", "mutation"} else "mcp_tool": endpoint,
                "exit_code": 2,
                "detail": detail,
                "report_text": 'Executable unavailable\n{"secret": 1}\n\x1b[31mbad',
            }
            result = present_report(report)
            text = "\n".join(f"{row.label}: {row.value}" for row in result.result_rows)
            assert "Executable unavailable" in text, (
                "unrecognized endpoint detail must retain fallback diagnostics"
            )
            assert '{"' not in text and "\x1b" not in text
            assert result.flow_result.value == "ERROR"


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


def capped_target(grade, reasons=()):
    return {
        "status": {"grade": grade, "failure_reasons": list(reasons), "warning_reasons": ["slow"]},
        "metrics": {
            "area_kge": 1,
            "area_um2": 2,
            "cell_count": 3,
            "wns_ns": 0.1,
            "latches": 0,
            "unexpected_latches": 0,
            "lut_count": 4,
            "ff_count": 5,
            "per_clock": {"clk": {"wns_ns": 0.1}},
        },
    }


def test_failures_survive_row_cap():
    def rows(*, reasons, overall):
        report = {
            "flow": "synth",
            "exit_code": 1,
            "detail": {
                "implementation": {
                    "results": {
                        "a": capped_target("pass"),
                        "b": capped_target("pass"),
                        "c": capped_target("fail", ["c timing"] if reasons else []),
                    }
                }
            },
            **({"failure_reason": "overall"} if overall else {}),
        }
        return present_report(report).result_rows

    result = rows(reasons=True, overall=True)
    assert len(result) == 24
    assert result[2] == ResultLine("  c", "fail", "fail")
    assert result[-3:] == (
        ResultLine("  c · Failure reason", "c timing", "fail"),
        ResultLine("Failure reason", "overall", "fail"),
        ResultLine("More", "12 facts in report"),
    )
    reference = [row for row in rows(reasons=False, overall=False) if row.semantic != "fail"]
    assert [row for row in result[:-1] if row.semantic != "fail"] == reference[:20]

    report = {
        "flow": "synth",
        "exit_code": 1,
        "failure_reason": "overall",
        "detail": {
            "implementation": {
                "results": {
                    "a": capped_target("pass"),
                    "b": capped_target("pass"),
                    "c": capped_target("fail", ["c timing"]),
                }
            }
        },
    }
    item = job(report, 1, "synth")
    assert "c · Failure reason: c timing" in job_detail(item)
    styled = DashboardApp(lambda: None)._job_detail(item)
    assert styled.get_style_at_offset(Console(), styled.plain.index("c timing")) == Style.parse(
        SEMANTIC_STYLE["fail"]
    )


def test_failed_targets_past_item_limit_are_named():
    def seven(make):
        return {f"t{i}": make(i == 6) for i in range(7)}

    cases = [
        (
            "synth",
            {
                "implementation": {
                    "results": seven(
                        lambda bad: (
                            capped_target("fail", ["late"])
                            if bad
                            else {"status": {"grade": "pass"}}
                        )
                    )
                }
            },
            [("  t6", "fail", "fail"), ("  t6 · Failure reason", "late", "fail")],
        ),
        (
            "fpga",
            {
                "implementation": {
                    "results": seven(lambda bad: {"status": {"grade": "error" if bad else "pass"}})
                }
            },
            [("  t6", "error", "fail")],
        ),
        (
            "sim",
            {
                "records": seven(
                    lambda bad: {
                        **simulation_record(2, 0),
                        "grade": "inconclusive" if bad else "pass",
                        "observations": [{"test": "test_bad", "functional": "inconclusive"}]
                        if bad
                        else [],
                    }
                )
            },
            [("  t6 · Failed", "test_bad", "fail")],
        ),
        (
            "sim",
            {
                "coverage": True,
                "targets": seven(
                    lambda bad: (
                        {
                            "simulation": "blocked",
                            "collection": "infrastructure_error",
                            "evaluation": "blocked",
                            "error": "boom",
                        }
                        if bad
                        else {"simulation": "pass", "collection": "ok", "evaluation": "pass"}
                    )
                ),
            },
            [
                (
                    "  t6",
                    "simulation blocked · collection infrastructure_error · evaluation blocked",
                    "fail",
                )
            ],
        ),
        (
            "sim",
            {
                "coverage": True,
                "targets": seven(
                    lambda bad: {
                        "simulation": "pass",
                        "collection": "ok",
                        "evaluation": "pass",
                        **({"error": "publish failed"} if bad else {}),
                    }
                ),
            },
            [("  t6", "simulation pass · collection ok · evaluation pass", "fail")],
        ),
        (
            "sim",
            {
                "mode": "elab-only",
                "targets": seven(
                    lambda bad: (
                        {"passed": False, "verdict": "failed", "reason": "syntax"}
                        if bad
                        else {"passed": True}
                    )
                ),
            },
            [("  t6", "failed · Reason: syntax", "fail")],
        ),
        (
            "lint",
            {
                "eda_tools": seven(lambda _: "verilator"),
                "metrics": {"t6": {"unavailable": "late"}},
            },
            [("  t6", "verilator", "fail"), ("  t6 · Error", "late", "fail")],
        ),
    ]
    for endpoint, detail, expected in cases:
        rows = [
            (row.label, row.value, row.semantic)
            for row in present_report({"flow": endpoint, "detail": detail}).result_rows
        ]
        for row in expected:
            assert row in rows, endpoint
        assert ("  t6", "fail") in [(label, sem) for label, _, sem in rows], endpoint
        assert not any(label.startswith("  t5") for label, _, _ in rows), endpoint
        assert ("Targets", "1 more Targets in report", "") in rows, endpoint


def lint_detail(count=2, **extra):
    names = [f"t{i}" for i in range(count)]
    return {
        "targets": names,
        "total_warnings": 0,
        "eda_tools": dict.fromkeys(names, "verilator"),
        **extra,
    }


def test_lint_errored_targets_show_their_first_error():
    bad = {"plain": {"errors": 1}, "counted_items": {"errors": ["a.sv:3 SYNTAX: bad"]}}
    warnings = [{"severity": "warning", "target": "t0", "message": "w"}] * 20
    t1 = [("  t1", "verilator", "fail"), ("  t1 · Error", "a.sv:3 SYNTAX: bad", "fail")]
    nomsg = ("  t1 · Error", "error message not in report", "fail")
    clean = {f"t{i}": {"plain": {"errors": 0}} for i in range(5)}
    cases = [
        (
            lint_detail(8, metrics={"t1": bad}),
            [*t1[:1], ("Targets", "3 more Targets in report", ""), t1[1]],
        ),
        (lint_detail(2, total_warnings=25, findings=warnings, metrics={"t1": bad}), t1),
        (
            lint_detail(findings=[{"severity": "error", "target": "t1", "message": "t1 broke"}]),
            [t1[0], ("  t1 · Error", "t1 broke", "fail")],
        ),
        (
            lint_detail(1, metrics={"t0": {"unavailable": "no verilator"}}),
            [("  t0", "verilator", "fail"), ("  Error", "no verilator", "fail")],
        ),
        (lint_detail(findings=warnings, findings_by_target={"t1": {"errors": 1}}), [t1[0], nomsg]),
        (
            lint_detail(
                1,
                metrics={"t0": {"plain": {"errors": 1}, "counted_items": {"errors": ["\x1b[0m"]}}},
            ),
            [("  t0", "verilator", "fail"), ("  Error", nomsg[1], "fail")],
        ),
        (lint_detail(5, metrics=clean, findings=warnings, findings_by_target=clean), []),
    ]
    for detail, expected in cases:
        report = {"flow": "lint", "exit_code": 1, "detail": detail}
        rows = [(row.label, row.value, row.semantic) for row in present_report(report).result_rows]
        assert [row for row in rows if row[2] or row[0] == "Targets"] == expected, detail
    old = {"total_warnings": 3, "eda_tools": {"t0": "verilator", "t1": "verible"}}
    rows = present_report({"flow": "lint", "detail": old}).result_rows
    assert [(row.label, row.value) for row in rows] == [
        ("  t0", "verilator"),
        ("  t1", "verible"),
        ("Warnings", "3"),
    ]


def test_lint_preparation_failure_shows_the_failed_target():
    detail = {"plan": {}, "metrics": {"t0": {"unavailable": "lint setup failed: boom"}}}
    report = present_report({"flow": "lint", "exit_code": 2, "detail": detail})
    assert [(row.label, row.value, row.semantic) for row in report.result_rows] == [
        ("  t0", "unavailable", "fail"),
        ("  Error", "lint setup failed: boom", "fail"),
    ]
    assert report.flow_result.value == "ERROR"
