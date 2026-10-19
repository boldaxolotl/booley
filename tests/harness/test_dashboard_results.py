"""Human result contracts at the existing Dashboard boundary and pure projection."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console
from rich.style import Style

import booley.harness.dashboard.app as module
from booley.criteria.presentation import CriterionPresentation
from booley.harness.dashboard.app import (
    SEMANTIC_STYLE,
    DashboardApp,
    Navigation,
    _evidence_detail,
    job_detail,
    job_paths,
)
from booley.harness.dashboard.jobs import JobView
from booley.harness.dashboard.links import linked_text
from booley.harness.dashboard.model import GoalDetail
from booley.harness.dashboard.results import present_report, project_facts
from booley.review.goal_package import proposal_facts
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


def campaign(passed=2, failed=0):
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


def proposal_app(monkeypatch, facts):

    view = SimpleNamespace(
        proposal=SimpleNamespace(id="p"), state="pending", closed_by_abandonment=False
    )
    goal = SimpleNamespace(status=SimpleNamespace(proposals=[view]))
    app = DashboardApp(lambda: None)
    app.navigation = Navigation("proposal", selected="p")
    monkeypatch.setattr(app, "_session", lambda: ("", goal))
    monkeypatch.setattr(module, "proposal_presentation", lambda _: facts)
    return app, goal


def test_details_never_dump_structures(monkeypatch):
    goal = GoalDetail(
        "key",
        "passing",
        CriterionPresentation("Human Goal", "", "", "", ""),
        {"recorded_at": STAMP, "detail": {"warnings": 2, "unknown": {"arbitrary": "secret"}}},
        ({"producer": "sim", "detail": {"passed": True}},),
    )
    app, selected = proposal_app(
        monkeypatch,
        {
            "kind": "retarget",
            "state": "pending",
            "before": "Old",
            "after": "New",
            "rationale": "scope",
            "unknown": {"secret": 1},
        },
    )
    texts = [
        job_detail(
            job(
                {
                    "detail": {"arbitrary": {"secret": 1}},
                    "report_text": "{\"bad\": 1}\n{'bad': 2}\n\x1b[31mbad",
                }
            )
        ),
        _evidence_detail(goal),
        app._selected_proposal_detail(),
        app._session_activity(selected)[0][1],
    ]
    for text in texts:
        assert "{'" not in text and '{"' not in text and "json.dumps" not in text, (
            "structured data must become readable facts"
        )
        assert "\x1b" not in text
    assert "Extra data in report" in _evidence_detail(goal)

    assert (
        present_report({"flow": "unknown", "report_text": '{"secret": 1}'}).result_rows[0].value
        == "Summary unavailable"
    )


def test_flow_and_criterion_are_independent():
    report = {
        "flow": "sim",
        "exit_code": 0,
        "criterion_key": "coverage_top",
        "criterion_met": False,
    }
    text = job_detail(job(report))
    assert "Flow result: PASSED" in text, "exit success must remain separate from Criterion"
    assert "Criterion: coverage_top · not met" in text
    assert text.splitlines()[0].startswith("Flow result:")

    result = present_report(report)
    assert result.flow_result.semantic == "pass" and result.criterion.semantic == "fail"
    styled = DashboardApp(lambda: None)._job_detail(job(report))
    assert styled.get_style_at_offset(Console(), styled.plain.index("PASSED")) == Style.parse(
        SEMANTIC_STYLE["pass"]
    )
    assert styled.get_style_at_offset(Console(), styled.plain.index("not met")) == Style.parse(
        SEMANTIC_STYLE["fail"]
    )


def test_criterion_hidden():
    for criterion in [
        {},
        {"criterion_key": "", "criterion_met": False},
        {"criterion_met": True},
        {"criterion_key": "sim_top", "criterion_met": None},
        {"criterion_key": "sim_top"},
        {"criterion_key": "", "criterion_met": True, "detail": {"targets": 2}},
    ]:
        text = job_detail(job({"flow": "sim", **criterion}))
        assert "Flow result: PASSED" in text, (
            "unbound results still need an execution-derived Flow result"
        )
        assert "Criterion:" not in text

        assert present_report({"exit_code": 0, **criterion}).criterion is None


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
            "flow" if endpoint in {"sim", "lint", "synth", "fpga"} else "mcp_tool": endpoint,
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
        {"campaigns": {"top": {**campaign(), "grade": "inconclusive"}}},
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
    ("sim", {"campaigns": {"top": campaign()}}, "2/2 tests passed", ["top"]),
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
            "campaigns": {"top": campaign(1, 1)},
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
        "mutation_tester",
        {
            "detected": 3,
            "not_detected": 1,
            "invalid": 2,
            "total_valid": 4,
            "min_detected": 3,
            "classified": [
                {"status": "not_detected", "category": "logic", "file": "unit.sv", "line": 12}
            ],
        },
        "Mutation: 3/4 detected",
        ["Invalid: 2", "unit.sv:12", "min 3"],
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
            "flow" if endpoint in {"sim", "lint", "synth", "fpga"} else "mcp_tool": endpoint,
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
                    "campaigns": {"top": campaign()},
                },
            }
            if key:
                report.update(criterion_key="coverage_top", criterion_met=evaluation == "pass")
            text = job_detail(job(report))
            assert "Flow result: PASSED" in text and f"evaluation {evaluation}" in text
            assert ("Criterion:" in text) == key

            assert present_report(report).flow_result.semantic == "pass"


def test_multitarget_and_list_caps():
    campaigns = {f"t{i}": campaign(2, 1) for i in range(8)}
    report = {"flow": "sim", "detail": {"campaigns": campaigns}}
    text = job_detail(job(report))
    assert "16/24 tests passed" in text, "aggregate counts must use canonical observation counts"
    assert "3 more Targets" in text and "t7:" not in text
    campaigns = {
        "top": {
            "observation_counts": {"functional": {"pass": 2, "fail": 8}},
            "observations": [{"test": f"bad{i}", "functional": "fail"} for i in range(7)],
            "observations_truncated": True,
        }
    }
    text = job_detail(job({"flow": "sim", "detail": {"campaigns": campaigns}}))
    assert "3 more failures" in text and "bad6" not in text
    assert "preview" in text

    result = present_report({"flow": "sim", "detail": {"campaigns": {"top": {}}}})
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


def test_goal_proposal_facts_preserved(monkeypatch):
    facts = {
        "kind": "retarget",
        "state": "pending",
        "before": "Old Goal",
        "after": "New Goal",
        "rationale": "new scope",
        "review_retarget_effect": "Discards prior evidence",
        "waiver": {
            "candidate_id": "w1",
            "candidate": {"scope": "line", "reason": "unreachable"},
            "policy": {"enabled": True, "config": {"anchor": "project", "directory": "waivers"}},
            "approval_root": "hidden",
        },
        "decision": {
            "decision": "approve",
            "reason": "accepted scope",
            "source": "chat",
            "payload_digest": "hidden",
        },
    }
    app, selected = proposal_app(monkeypatch, facts)
    text = app._selected_proposal_detail()
    assert "Before: Old Goal" in text, "proposal approval facts need labeled human text"
    for part in (
        "New Goal",
        "new scope",
        "Discards",
        "unreachable",
        "enabled",
        "waivers",
        "accepted scope",
        "Approval remains in chat",
    ):
        assert part in text
    assert "hidden" not in text
    inline = app._session_activity(selected)[0][1]
    assert (
        "Old Goal" in inline
        and "New Goal" in inline
        and "pending" in inline
        and "\n" not in inline
    )
    goal = GoalDetail(
        "key",
        "passing",
        CriterionPresentation("Human Goal", "", "", "", ""),
        {"recorded_at": STAMP, "detail": {"passed": True, "warnings": 2}},
        ({"producer": "sim", "recorded_at": STAMP, "detail": {"passed": False}},),
        artifacts=("/a/evidence",),
        test_runs=("Test Run frozen -> rerun exact",),
    )
    text = _evidence_detail(goal)
    assert text.startswith("Test Run frozen -> rerun exact\n")
    assert (
        "Immutable derivation provenance" in text
        and "sim" in text
        and STAMP in text
        and "/a/evidence" in text
    )


def test_campaign_producer_failure_axes_are_visible():

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
                "campaigns": {
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
        text = "\n".join(f"{row.label}: {row.value}" for row in result.result_rows)
        for name, area, slack, reason in (
            ("alpha", 12, -1, "alpha timing"),
            ("beta", 24, -2, "beta timing"),
        ):
            assert f"{name} · Area: {area}" in text, (
                "implementation metrics must identify their Target"
            )
            assert f"{name} · Clock clk: WNS: {slack}" in text
            assert f"{name} · Failure reason: {reason}" in text


def test_known_endpoint_unknown_detail_uses_safe_fallback():

    for endpoint in ("lint", "reviewer", "mutation_tester", "coverage_analyst"):
        for detail in (
            {"eda_tool_error": "launch failed"},
            {"total_warnings": {"bad": 1}},
            {"issues": None},
        ):
            report = {
                "flow" if endpoint == "lint" else "mcp_tool": endpoint,
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


def test_retarget_warning_preserves_required_followup():

    facts = proposal_facts(
        {
            "id": "p",
            "kind": "retarget",
            "before": None,
            "after": {"family": "review"},
            "rationale": "scope",
        },
        lambda _: "Review",
    )
    rows = project_facts(facts)
    warning = next(row.value for row in rows if row.label == "Review retarget effect")
    assert warning.endswith("A new discovery review is required after approval."), (
        "retarget safety warning must remain complete"
    )
