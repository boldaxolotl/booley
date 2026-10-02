"""Tests for deterministic interactive-triage packages."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from urllib.parse import quote

import pytest

from booley.criteria.freshness import (
    VerificationFreshness,
    evaluate_verification_freshness,
    verification_freshness_eligible,
)
from booley.criteria.presentation import state_criterion_presentation
from booley.criteria.templates import encode_criterion_component
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.review import triage_package as tp


@dataclass(frozen=True)
class Context:
    project_root: Path
    slug: str
    log_dir: Path
    runtime_dir: Path
    worktree: Path
    ticket_path: Path
    base_sha: str
    head_sha: str
    feature_branch: str = "demo"
    project_repository: object | None = None


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _context(tmp_path: Path) -> Context:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.name", "Test")
    _git(worktree, "config", "user.email", "test@example.com")
    source = worktree / "rtl" / "old.sv"
    source.parent.mkdir()
    source.write_text(
        "module old;\n  logic a;\n  logic b;\n  assign a = b;\nendmodule\n",
        encoding="utf-8",
    )
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", "base")
    base = _git(worktree, "rev-parse", "HEAD")
    source.rename(source.with_name("new.sv"))
    source.with_name("new.sv").write_text(
        "module new;\n  logic a;\n  logic b;\n  assign a = b;\nendmodule\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "rename implementation")
    head = _git(worktree, "rev-parse", "HEAD")
    log_dir = tmp_path / "logs" / "demo"
    runtime = log_dir / ".runtime" / "triage-prep"
    state = {
        "criteria": {
            "sim_pass": {
                "mandatory": True,
                "met": True,
                "detail": {"tests_passed": 2, "tests_total": 2},
            },
            "review_security_done": {"mandatory": False, "met": False},
        },
        "timeline": [],
    }
    state_path = log_dir / ".runtime" / "booley_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    (log_dir / "REPORT.md").write_text("report\n", encoding="utf-8")
    ticket = tmp_path / "ticket.md"
    ticket.write_text("ticket\n", encoding="utf-8")
    return Context(tmp_path, "demo", log_dir, runtime, worktree, ticket, base, head)


def _assessment() -> dict:
    return {
        "recommendation": "approve",
        "reason": "mandatory checks pass",
        "decision_blockers": [],
        "scope_deviations": [],
        "developer_summary": "renamed the module",
        "uncertainties": "none",
        "optional_omissions": "security review was optional",
        "findings": [],
    }


def _never_freshness_eligible(*_args: object, **_kwargs: object) -> bool:
    return False


def _test_coverage_report_resolver(report_root: Path, value: object) -> Path | None:
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
        return None
    return report_root / value["path"]


def _facts(
    ctx: Context,
    *,
    run_economics: str = "unavailable",
    freshness_eligible=verification_freshness_eligible,
) -> dict:
    state = json.loads((ctx.log_dir / ".runtime" / "booley_state.json").read_text())
    from booley.ticket_board.review_preparation import _project_review_report

    state = _project_review_report(state, ctx.log_dir)
    scope_path = ctx.log_dir / ".runtime" / "scope_deviations.json"
    scope = json.loads(scope_path.read_text()) if scope_path.is_file() else {}
    evidence = tp.ResolvedReviewEvidence.capture(
        state=state,
        scope=scope,
        dirty_worktree=[],
        developer_crashes=[],
        missing_evidence=[],
    )
    return tp.build_review_facts(
        ctx,
        evidence,
        freshness_eligible=freshness_eligible,
        freshness_evaluator=partial(
            evaluate_verification_freshness,
            fingerprint_provider=compute_source_fingerprint,
        ),
        criterion_presenter=state_criterion_presentation,
        coverage_report_resolver=_test_coverage_report_resolver,
        run_economics=run_economics,
    )


def test_review_facts_consume_frozen_board_evidence(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    scope_path = ctx.log_dir / ".runtime" / "scope_deviations.json"
    captured_state = json.loads(state_path.read_text(encoding="utf-8"))
    captured_state["criteria"]["captured_only"] = {"mandatory": False, "met": True}
    evidence = tp.ResolvedReviewEvidence.capture(
        state=captured_state,
        scope={"decidable": False, "harness_paths": ["captured/path"]},
        dirty_worktree=[" M captured.sv"],
        developer_crashes=["captured.crash.json"],
        missing_evidence=["captured.txt"],
    )
    state_path.unlink()
    scope_path.write_text(
        json.dumps({"decidable": True, "harness_paths": ["live/path"]}),
        encoding="utf-8",
    )

    facts = tp.build_review_facts(
        ctx,
        evidence,
        freshness_eligible=verification_freshness_eligible,
        freshness_evaluator=partial(
            evaluate_verification_freshness,
            fingerprint_provider=compute_source_fingerprint,
        ),
        criterion_presenter=state_criterion_presentation,
        coverage_report_resolver=_test_coverage_report_resolver,
    )

    assert "captured_only" in {row["criterion"] for row in facts["criteria"]}
    assert facts["health"] == {
        "dirty_worktree": [" M captured.sv"],
        "exit_2_tools": [],
        "developer_crashes": ["captured.crash.json"],
        "missing_evidence": ["captured.txt"],
        "harness_paths": ["captured/path"],
        "scope_undecidable": True,
        "unverified_transitions": [],
    }


def test_review_facts_project_live_staleness_without_mutating_evidence(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    (ctx.worktree / "design.core").write_text(
        "CAPI=2:\nname: ::design:0\nfilesets:\n"
        "  rtl: {files: [rtl/new.sv]}\n"
        "  tb: {files: [tb/tb.sv], tags: [tb]}\n"
        "targets:\n  sim: {filesets: [rtl, tb], toplevel: tb}\n",
        encoding="utf-8",
    )
    (ctx.worktree / "tb").mkdir()
    tb = ctx.worktree / "tb" / "tb.sv"
    tb.write_text("module tb; endmodule\n", encoding="utf-8")
    stamp = {
        "categories": ["rtl", "tb"],
        "target": "sim",
        "fingerprint": compute_source_fingerprint(ctx.worktree, target="sim"),
    }
    state = {
        "criteria": {
            "sim_pass_sim": {
                "mandatory": True,
                "met": True,
                "detail": {
                    SOURCE_FINGERPRINT_DETAIL_KEY: stamp,
                    "tests_passed": 3,
                    "tests_total": 3,
                },
            },
            "_report_submitted": {"mandatory": True, "met": True},
        }
    }
    evidence = tp.ResolvedReviewEvidence.capture(
        state=state,
        scope={},
        dirty_worktree=[],
        developer_crashes=[],
        missing_evidence=[],
    )
    tb.write_text("module tb; // changed\nendmodule\n", encoding="utf-8")

    facts = tp.build_review_facts(
        ctx,
        evidence,
        freshness_eligible=verification_freshness_eligible,
        freshness_evaluator=partial(
            evaluate_verification_freshness,
            fingerprint_provider=compute_source_fingerprint,
        ),
        criterion_presenter=state_criterion_presentation,
        coverage_report_resolver=_test_coverage_report_resolver,
    )

    rows = {row["criterion"]: row for row in facts["criteria"]}
    assert rows["sim_pass_sim"]["outcome"] == "met"
    assert rows["sim_pass_sim"]["freshness"] == "stale"
    assert rows["sim_pass_sim"]["changed_categories"] == ["tb"]
    assert rows["sim_pass_sim"]["status"] == "STALE (tb)"
    assert "tests_passed" not in rows["sim_pass_sim"]["metric"]
    assert rows["_report_submitted"]["freshness"] == "stale"
    assert evidence.state() == state


def test_live_staleness_preserves_locked_submitted_report(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    state = {
        "criteria": {
            "sim_pass_sim": {"mandatory": True, "met": True},
            "_report_submitted": {"mandatory": True, "met": True, "locked": True},
        }
    }
    evidence = tp.ResolvedReviewEvidence.capture(
        state=state,
        scope={},
        dirty_worktree=[],
        developer_crashes=[],
        missing_evidence=[],
    )

    facts = tp.build_review_facts(
        ctx,
        evidence,
        freshness_eligible=verification_freshness_eligible,
        freshness_evaluator=lambda *_args, **_kwargs: VerificationFreshness(
            True,
            ("tb",),
            "changed",
        ),
        criterion_presenter=state_criterion_presentation,
        coverage_report_resolver=_test_coverage_report_resolver,
    )

    rows = {row["criterion"]: row for row in facts["criteria"]}
    assert rows["sim_pass_sim"]["freshness"] == "stale"
    assert rows["_report_submitted"]["freshness"] == "current"


def test_stale_mandatory_freshness_forces_hold_once() -> None:
    facts = {
        "scope": {"deviations": []},
        "criteria": [
            {
                "criterion": "sim_pass_sim",
                "required": "mandatory",
                "freshness": "stale",
                "changed_categories": ["tb"],
            }
        ],
    }

    assessment = tp.validate_assessment(_assessment(), facts)
    assessment = tp.validate_assessment(assessment, facts)

    assert assessment["recommendation"] == "hold"
    assert assessment["decision_blockers"] == [
        "Stale mandatory verification evidence: sim_pass_sim (tb)."
    ]


def test_review_facts_materialize_rename_pair_and_oldest_first_commits(
    tmp_path: Path, monkeypatch
):
    ctx = _context(tmp_path)
    facts = _facts(ctx, run_economics="tokens=10 cost=$0.01")

    assert [row["subject"] for row in facts["commits"]] == ["rename implementation"]
    assert [row["criterion"] for row in facts["criteria"]] == [
        "sim_pass",
        "review_security_done",
    ]
    change = facts["changed_files"][0]
    assert change["status"].startswith("R")
    assert change["old_path"] == "rtl/old.sv"
    assert change["path"] == "rtl/new.sv"
    assert Path(change["diff_left"]).read_text(encoding="utf-8").startswith("module old")
    assert Path(change["diff_right"]).read_text(encoding="utf-8").startswith("module new")


def test_review_facts_include_every_waiver_with_justification(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["criteria"]["review_security_clean"] = {
        "mandatory": True,
        "met": True,
        "detail": {
            "issues": 0,
            "pending": [],
            "resolved": [
                {
                    "severity": "MINOR",
                    "file": "rtl/new.sv",
                    "line": 4,
                    "summary": "Intentional visibility",
                    "status": "waived",
                    "justification": "The debug interface is required by the ticket.",
                }
            ],
        },
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    facts = _facts(ctx, run_economics="tokens=10 cost=$0.01")

    waiver = facts["review_dispositions"][0]
    assert waiver["disposition"] == "waived"
    assert waiver["severity"] == "MINOR"
    assert waiver["justification"] == "The debug interface is required by the ticket."


def test_cycle_comparison_is_prominent_and_discloses_workload_drift(
    tmp_path: Path, monkeypatch
) -> None:
    ctx = _context(tmp_path)
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["criteria"]["cycle_count_binding"] = {
        "mandatory": True,
        "met": True,
        "detail": {
            "cycle_comparison": {
                "target": "sim_coremark",
                "test": "coremark",
                "baseline_ref": "a" * 40,
                "baseline_cycles": 100,
                "cycles": 90,
                "delta_cycles": -10,
                "delta_pct": -10.0,
                "checks": [
                    {
                        "param": "cycle_count_reduce_at_least",
                        "threshold": 5,
                        "unit": "percent",
                        "pass": True,
                    }
                ],
                "workload_changed": True,
                "known_input_changes": [
                    {"path": "rtl/new.sv", "role": "rtl", "status": "modified"}
                ],
                "provenance_limitation": "ambient inputs are not observed",
            }
        },
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    facts = _facts(ctx)
    package = {**facts, "assessment": _assessment(), "html_path": None}
    rendered = tp.render_review_briefing(package, [])

    assert facts["cycle_comparisons"][0]["delta_cycles"] == -10
    assert "#### Cycle Count comparisons" in rendered
    assert "observed Cycle Count change" in rendered
    assert "known workload inputs changed" in rendered
    assert "[rtl/new.sv](" in rendered
    assert "does not establish causality" in rendered


def test_review_facts_include_paired_project_repository(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    project = ctx.worktree / ".booley_project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.com")
    core = project / "cores" / "demo.core"
    core.parent.mkdir()
    core.write_text("name: ::demo:0\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-qm", "project base")
    base = _git(project, "rev-parse", "HEAD")
    core.write_text("name: ::demo:1\n", encoding="utf-8")
    _git(project, "commit", "-qam", "update project core")
    head = _git(project, "rev-parse", "HEAD")
    project_repository = type(
        "ProjectRepository",
        (),
        {"worktree": project, "base_sha": base, "head_sha": head},
    )()
    ctx = replace(ctx, project_repository=project_repository)
    facts = _facts(ctx)

    assert facts["commits"][-1]["repository"] == "project"
    assert facts["commits"][-1]["subject"] == "update project core"
    project_change = facts["changed_files"][-1]
    assert project_change["repository"] == "project"
    assert project_change["path"] == ".booley_project/cores/demo.core"
    assert Path(project_change["diff_left"]).read_text(encoding="utf-8") == ("name: ::demo:0\n")
    assert Path(project_change["diff_right"]).read_text(encoding="utf-8") == ("name: ::demo:1\n")


def test_review_facts_classify_symlink_binary_and_submodule_content(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    link = ctx.worktree / "rtl" / "link.sv"
    link.symlink_to("new.sv")
    (ctx.worktree / "rtl" / "blob.bin").write_bytes(b"before\0after")
    submodule_commit = _git(ctx.worktree, "rev-parse", "HEAD")
    _git(
        ctx.worktree,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{submodule_commit},deps/ip",
    )
    _git(ctx.worktree, "add", "rtl/link.sv", "rtl/blob.bin")
    _git(ctx.worktree, "commit", "-qm", "add special content")
    ctx = replace(ctx, head_sha=_git(ctx.worktree, "rev-parse", "HEAD"))
    changes = {row["path"]: row for row in _facts(ctx)["changed_files"]}

    assert changes["rtl/link.sv"]["content_kind"] == "symlink"
    assert changes["rtl/link.sv"]["presentation"] == "text"
    assert changes["rtl/blob.bin"]["content_kind"] == "regular"
    assert changes["rtl/blob.bin"]["presentation"] == "binary"
    assert changes["deps/ip"]["content_kind"] == "submodule"
    assert changes["deps/ip"]["action"] == "added"
    assert changes["deps/ip"]["new_endpoint"]["workspace_path"] is None


def test_mutation_criterion_links_to_preserved_campaign_report(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    report = (
        ctx.log_dir
        / ".runtime"
        / "mcp-tool-reports"
        / "mutation_tester"
        / "1"
        / "mutation-results.md"
    )
    report.parent.mkdir(parents=True)
    report.write_text("# Mutation Test Results\n", encoding="utf-8")
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    mutation_key = f"mutation_score_{encode_criterion_component('sim_core')}"
    state["criteria"][mutation_key] = {
        "mandatory": True,
        "met": True,
        "params": {"target": "sim_core", "min_detected": 7, "total": 8},
        "detail": {
            "detected": 7,
            "total_valid": 8,
            "not_detected": 1,
            "invalid": 0,
            "artifacts": {
                "results": "../logs/demo/.runtime/mcp-tool-reports/"
                "mutation_tester/1/mutation-results.md"
            },
        },
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    facts = _facts(ctx, freshness_eligible=_never_freshness_eligible)
    package = {**facts, "assessment": _assessment(), "html_path": None}
    rendered = tp.render_review_briefing(package, [])
    mutation = next(row for row in facts["criteria"] if row["criterion"] == mutation_key)

    assert mutation["report_path"] == str(report.resolve())
    assert "detected=7, total_valid=8, not_detected=1, invalid=0" in mutation["metric"]
    report_link = quote(str(report.resolve()), safe="/:")
    assert f"[Mutation testing · sim_core]({report_link})" in rendered


def _coverage_key(target: str) -> str:
    return "_".join(
        ("coverage", encode_criterion_component(target), encode_criterion_component("line"))
    )


def _coverage_entry(
    target: str,
    *,
    covered: int,
    eligible: int,
    reference: dict[str, object] | None = None,
    stale: bool = False,
) -> dict[str, object]:
    detail: dict[str, object] = {
        "evaluation": {
            "metrics": [
                {
                    "metric": "line",
                    "covered_points": covered,
                    "eligible_points": eligible,
                    "actual_percent": 100 * covered / eligible,
                }
            ]
        }
    }
    if reference is not None:
        detail["coverage_campaign_reference"] = reference
    return {
        "mandatory": True,
        "met": True,
        "stale": stale,
        "params": {"target": target, "metrics": {"line": {"min_pct": 90}}},
        "detail": detail,
    }


def _presentation_criteria(
    target: str, reference: dict[str, object]
) -> tuple[dict[str, object], str, str, str]:
    sim_key = f"sim_pass_{encode_criterion_component(target)}"
    coverage_key = _coverage_key(target)
    stale_coverage_key = _coverage_key("sim_old")
    criteria = {
        sim_key: {
            "mandatory": True,
            "met": True,
            "ever_failed": False,
            "params": {"target": target, "from_state": "fail", "test_selector": "all"},
            "detail": {"tests_passed": 3, "tests_total": 3},
        },
        coverage_key: {
            **_coverage_entry(target, covered=3, eligible=3, reference=reference),
            "presentation": {"label": "untrusted override"},
        },
        stale_coverage_key: _coverage_entry("sim_old", covered=9, eligible=10, stale=True),
        "sim_pass_failed": {
            "mandatory": False,
            "met": False,
            "ever_failed": True,
            "params": {"target": "sim_failed"},
            "detail": {"error_gist": "assertion failed", "reason": "bad result"},
        },
    }
    return criteria, sim_key, coverage_key, stale_coverage_key


def _presentation_case(tmp_path: Path) -> tuple[Context, dict, str, str, str, Path]:
    ctx = _context(tmp_path)
    campaign = ctx.log_dir / ".runtime" / "flow-reports" / "sim" / "run-1" / "coverage.json"
    campaign.parent.mkdir(parents=True)
    campaign_bytes = b'{"coverage_campaign":{"path":"campaign.json"}}\n'
    campaign.write_bytes(campaign_bytes)
    reference = {
        "path_base": "reports_root",
        "path": "sim/run-1/coverage.json",
        "bytes": len(campaign_bytes),
        "sha256": "sha256:" + hashlib.sha256(campaign_bytes).hexdigest(),
        "nested_campaign_sha256": "sha256:" + "a" * 64,
    }
    criteria, sim_key, coverage_key, stale_coverage_key = _presentation_criteria(
        "vendor:library:core:1#sim_generated", reference
    )
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["criteria"] = criteria
    state_path.write_text(json.dumps(state), encoding="utf-8")
    evidence = tp.ResolvedReviewEvidence.capture(
        state=state,
        scope={},
        dirty_worktree=[],
        developer_crashes=[],
        missing_evidence=[],
    )
    facts = tp.build_review_facts(
        ctx,
        evidence,
        freshness_eligible=_never_freshness_eligible,
        freshness_evaluator=evaluate_verification_freshness,
        criterion_presenter=state_criterion_presentation,
        coverage_report_resolver=_test_coverage_report_resolver,
    )
    return ctx, facts, sim_key, coverage_key, stale_coverage_key, campaign


def test_ticket_v2_criteria_have_readable_review_facts(tmp_path: Path) -> None:
    _, facts, _, coverage_key, stale_coverage_key, campaign = _presentation_case(tmp_path)
    rows = {row["criterion"]: row for row in facts["criteria"]}
    assert rows[coverage_key]["category"] == "Coverage"
    assert rows[coverage_key]["label"] == "Coverage · sim_generated"
    assert "3/3 (100%)" in rows[coverage_key]["detail"]
    assert rows[coverage_key]["report_path"] == str(campaign.resolve())
    assert rows[stale_coverage_key]["status"] == "STALE"
    assert "9/10" not in rows[stale_coverage_key]["detail"]
    assert "assertion failed" in rows["sim_pass_failed"]["metric"]
    assert "bad result" in rows["sim_pass_failed"]["metric"]
    assert facts["health"]["unverified_transitions"] == ["Simulation · sim_generated"]


def test_ticket_v2_criteria_render_readable_review_briefing(tmp_path: Path) -> None:
    ctx, facts, sim_key, coverage_key, _, campaign = _presentation_case(tmp_path)

    path = tp.write_triage_package(ctx, facts, _assessment(), None)
    package = tp.load_triage_package(path)
    rendered = tp.render_review_briefing(package, [])
    assert "Simulation · sim_generated" in rendered
    assert "Coverage · sim_generated" in rendered
    assert "3/3 (100%)" in rendered
    assert f"[Coverage · sim_generated]({quote(str(campaign.resolve()), safe='/:')})" in rendered
    assert sim_key not in rendered
    assert coverage_key not in rendered
    assert "| Other |" not in rendered


def test_review_facts_and_briefing_reveal_recipe_changes(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["criteria"]["synthesis_ok_core"] = {
        "mandatory": True,
        "met": True,
        "detail": {
            "cells": 90,
            "recipe_comparison": {
                "target": "synth_core",
                "baseline_ref": "a" * 40,
                "baseline_fingerprint": "b" * 64,
                "current_fingerprint": "c" * 64,
                "changed": True,
                "changes": [
                    {
                        "path": "parameters.ENABLE_ZBB",
                        "before": 0,
                        "after": 1,
                    }
                ],
            },
            "checks": [
                {
                    "param": "cell_count_increase_at_most",
                    "pass": True,
                    "baseline": 85,
                    "current": 90,
                    "pct": 5.88,
                    "threshold": 11,
                }
            ],
        },
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    facts = _facts(ctx)
    package = {**facts, "assessment": _assessment(), "html_path": None}
    rendered = tp.render_review_briefing(package, [])

    assert facts["recipe_comparisons"][0]["target"] == "synth_core"
    assert "#### Implementation Target recipes" in rendered
    assert "parameters.ENABLE_ZBB" in rendered
    assert "cell_count_increase_at_most" in rendered


def test_review_facts_and_briefing_reveal_fpga_recipe_changes(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["criteria"]["fpga_impl_ok_core"] = {
        "mandatory": True,
        "met": True,
        "detail": {
            "recipe_comparison": {
                "flow": "fpga",
                "target": "fpga_core",
                "baseline_fingerprint": "b" * 64,
                "current_fingerprint": "c" * 64,
                "changed": True,
                "changes": [
                    {
                        "path": "flow_options.part",
                        "before": "xc7a35t",
                        "after": "xc7a200t",
                    }
                ],
            },
            "checks": [
                {
                    "param": "lut_count_increase_at_most",
                    "pass": True,
                    "baseline": 100,
                    "current": 105,
                    "pct": 5.0,
                    "threshold": 10,
                }
            ],
        },
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    facts = _facts(ctx)
    package = {**facts, "assessment": _assessment(), "html_path": None}
    rendered = tp.render_review_briefing(package, [])

    comparison = next(row for row in facts["recipe_comparisons"] if row["flow"] == "fpga")
    assert comparison["target"] == "fpga_core"
    assert "`fpga:fpga_core`" in rendered
    assert "flow_options.part" in rendered
    assert "lut_count_increase_at_most" in rendered


def test_review_facts_record_unverified_fail_to_pass_transition(tmp_path: Path, monkeypatch):
    ctx = _context(tmp_path)
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["criteria"]["sim_pass"].update({"params": {"from_state": "fail"}, "ever_failed": False})
    state_path.write_text(json.dumps(state), encoding="utf-8")
    facts = _facts(ctx)

    assert facts["health"]["unverified_transitions"] == ["Simulation"]


def test_assessment_fills_missing_scope_deviation_for_human_review(tmp_path: Path):
    facts = {"scope": {"deviations": ["rtl/outside.sv"]}}

    assessment = tp.validate_assessment(_assessment(), facts)

    assert assessment["recommendation"] == "hold"
    assert assessment["scope_deviations"] == [
        {
            "path": "rtl/outside.sv",
            "classification": "Needs review",
            "reason": "The report agent did not return exactly one assessment for this deviation.",
        }
    ]
    assert "Human scope classification required" in assessment["decision_blockers"][0]


def test_assessment_normalizes_empty_optional_omissions():
    assessment = _assessment()
    assessment["optional_omissions"] = ""

    validated = tp.validate_assessment(assessment, {"scope": {"deviations": []}})

    assert validated["optional_omissions"] == "none"


def test_render_uses_precomputed_package_without_raw_evidence(tmp_path: Path):
    ctx = _context(tmp_path)
    package = {
        "slug": "demo",
        "assessment": _assessment(),
        "criteria": [],
        "commits": [],
        "changed_files": [],
        "developer_report_path": str(ctx.log_dir / "REPORT.md"),
        "html_path": str(ctx.log_dir / "explanation.html"),
        "run_economics": "tokens=10 cost=$0.01",
        "health": {},
    }

    rendered = tp.render_review_briefing(package, [])

    assert "**Recommendation:** approve" in rendered
    assert "Health checks: all passed." in rendered
    assert "Choose: **approve** / **reset** / **archive** / **skip**." in rendered


def test_accepted_review_presentation_keeps_packages_without_inspection():
    package = {"assessment": {"recommendation": "hold"}, "payload": "unchanged"}
    assert tp.accepted_review_presentation(package) == package


def test_accepted_presentation_removes_unaccepted_blocker(tmp_path: Path):
    ctx = _context(tmp_path)
    package = {
        "slug": "demo",
        "inspection": {
            "disposition": "unaccepted",
            "reason": "inspect",
            "blocked_reason": "verification",
        },
        "assessment": {
            **_assessment(),
            "recommendation": "hold",
            "decision_blockers": ["Not accepted: finish verification, then use board approve."],
        },
        "criteria": [],
        "commits": [],
        "changed_files": [],
        "developer_report_path": str(ctx.log_dir / "REPORT.md"),
        "html_path": None,
        "run_economics": "unavailable",
        "health": {},
    }

    presented = tp.accepted_review_presentation(package)
    rendered = tp.render_review_briefing(presented, [])

    assert "Acceptance: accepted" in rendered
    assert "Not accepted:" not in rendered
    assert "**Recommendation:** approve" in rendered


def test_unaccepted_menu_offers_approval_when_mandatory_criteria_are_met(tmp_path: Path):
    ctx = _context(tmp_path)
    package = {
        "slug": "demo",
        "inspection": {
            "disposition": "unaccepted",
            "reason": "inspect",
            "blocked_reason": "verification",
        },
        "assessment": _assessment(),
        "criteria": [
            {
                "category": "Review",
                "criterion": "implementation_done",
                "required": "mandatory",
                "status": "met",
                "metric": "persisted criterion state",
            }
        ],
        "commits": [],
        "changed_files": [],
        "developer_report_path": str(ctx.log_dir / "REPORT.md"),
        "html_path": None,
        "run_economics": "unavailable",
        "health": {},
    }

    rendered = tp.render_review_briefing(package, [])

    assert "Choose: **approve** / **fix here** / **review**" in rendered


def test_render_presents_reports_first_in_review_order(tmp_path: Path):
    ctx = _context(tmp_path)
    package = {
        "slug": "demo",
        "assessment": _assessment(),
        "criteria": [],
        "commits": [],
        "changed_files": [],
        "developer_report_path": str(ctx.log_dir / "REPORT.md"),
        "html_path": str(ctx.log_dir / "explanation.html"),
        "explanation": {
            "background": [{"title": "Why", "body": "The change reorders the briefing."}],
            "code_references": [
                {"path": "src/booley/harness/triage_package.py", "summary": "Renderer order"}
            ],
        },
        "review_dispositions": [
            {
                "criterion": "review_rtl_bugs_clean",
                "severity": "MINOR",
                "file": "rtl/dut.sv",
                "line": 7,
                "disposition": "fixed",
                "summary": "Example finding",
            }
        ],
        "run_economics": "tokens=10 cost=$0.01",
        "health": {},
    }

    rendered = tp.render_review_briefing(package, [])

    ordered_sections = (
        "#### Reports",
        "#### Decision summary",
        "#### Findings",
        "#### Explanation highlights",
        "#### Scope deviations",
        "#### Changed files",
        "#### Criteria",
        "#### Review findings and dispositions",
        "#### Commit history",
        "#### Run economics",
    )
    positions = [rendered.index(section) for section in ordered_sections]
    assert positions == sorted(positions)
    assert rendered.index("Developer Agent report") < rendered.index("Polished HTML report")
    assert "The change reorders the briefing." in rendered


def test_render_marks_undecidable_scope_as_blocker(tmp_path: Path):
    ctx = _context(tmp_path)
    package = {
        "slug": "demo",
        "assessment": _assessment(),
        "scope": {"decidable": False, "deviations": []},
        "criteria": [],
        "commits": [],
        "changed_files": [],
        "developer_report_path": str(ctx.log_dir / "REPORT.md"),
        "html_path": str(ctx.log_dir / "explanation.html"),
        "run_economics": "tokens=10 cost=$0.01",
        "health": {"scope_undecidable": True},
    }

    rendered = tp.render_review_briefing(package, [])

    assert "**Recommendation:** hold" in rendered
    assert "Scope calculation was undecidable." in rendered
    assert "do not infer clean scope" in rendered


def test_render_surfaces_unverified_transition(tmp_path: Path):
    ctx = _context(tmp_path)
    package = {
        "slug": "demo",
        "assessment": _assessment(),
        "criteria": [],
        "commits": [],
        "changed_files": [],
        "developer_report_path": str(ctx.log_dir / "REPORT.md"),
        "html_path": None,
        "run_economics": "unavailable",
        "health": {"unverified_transitions": ["sim_pass"]},
    }

    rendered = tp.render_review_briefing(package, [])

    assert "UNVERIFIED TRANSITION: sim_pass" in rendered


def test_changed_file_links_are_absolute(tmp_path: Path):
    ctx = _context(tmp_path)
    path = ctx.worktree / "rtl" / "new.sv"
    package = {
        "worktree": str(ctx.worktree),
        "changed_files": [
            {"status": "M", "path": "rtl/new.sv", "diff_left": str(tmp_path / "left")}
        ],
    }
    lines = []

    tp._render_changes(lines, package, set())

    assert quote(str(path.resolve()), safe="/:") in "\n".join(lines)


def test_changed_symlink_link_does_not_follow_target(tmp_path: Path):
    ctx = _context(tmp_path)
    outside = tmp_path / "outside.sv"
    outside.write_text("outside\n", encoding="utf-8")
    link = ctx.worktree / "rtl" / "link.sv"
    link.symlink_to(outside)
    package = {
        "worktree": str(ctx.worktree),
        "changed_files": [
            {"status": "M", "path": "rtl/link.sv", "diff_left": str(tmp_path / "left")}
        ],
    }
    lines = []

    tp._render_changes(lines, package, set())

    rendered = "\n".join(lines)
    assert quote(str(link.absolute()), safe="/:") in rendered
    assert quote(str(outside), safe="/:") not in rendered


def test_review_shows_developer_justifications_in_scope_and_file_sections(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    reason = "The shared module also needs the new interface."
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = json.loads(state_path.read_text())
    state["criteria"]["_report_submitted"] = {
        "met": True,
        "detail": {"file_justifications": {"rtl/new.sv": reason}},
    }
    state_path.write_text(json.dumps(state))
    facts = _facts(ctx)
    facts["assessment"] = {
        "scope_deviations": [
            {"path": "rtl/new.sv", "classification": "Needs review", "reason": "Outside scope"}
        ]
    }
    lines = []
    tp._render_scope(lines, facts)
    tp._render_changes(lines, facts, set())
    assert "\n".join(lines).count(reason) == 2


def test_review_rejects_malformed_persisted_justifications(tmp_path):
    import pytest

    ctx = _context(tmp_path)
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    malformed = [
        {"criteria": None},
        {"criteria": {"_report_submitted": []}},
        {"criteria": {"_report_submitted": {"detail": None}}},
        {"criteria": {"_report_submitted": {"detail": {"file_justifications": []}}}},
        {"criteria": {"_report_submitted": {"detail": {"file_justifications": {"a": 1}}}}},
        {"criteria": {"_report_submitted": {"detail": {"file_justifications": {"a": " "}}}}},
    ]
    for state in malformed:
        state_path.write_text(json.dumps(state))
        with pytest.raises(tp.TriagePackageError, match="file justifications"):
            _facts(ctx)


def _waiver_row(candidate_id: str, status: str, *, needed: bool = False) -> dict:
    return {
        "candidate_id": candidate_id,
        "status": status,
        "status_reason": "Eligible zero-hit point of this Campaign.",
        "needed": needed,
        "criteria": ["coverage_line"],
        "target": "acme:demo:counter:1#sim",
        "point_id": "cp1:abc",
        "location": "rtl/counter.sv:4",
        "waiver_reason": "unreachable",
        "justification": "Reset-only | tied off",
        "proposal_count": 2,
        "metrics": [
            {
                "metric": "line",
                "strict_percent": 50.0,
                "provisional_percent": 100.0,
                "minimum_percent": 70,
            }
        ],
    }


def test_waiver_candidates_round_trip_through_the_review_package(tmp_path: Path) -> None:
    """ADR 0066: candidate rows are part of the immutable, schema-checked package."""
    from booley.review.artifact import ReviewPackage

    ctx = _context(tmp_path)
    facts = {**_facts(ctx), "waiver_candidates": [_waiver_row("wc-1", "offered", needed=True)]}
    package = ReviewPackage.parse({**facts, "assessment": _assessment(), "html_path": None})

    assert package.to_dict()["waiver_candidates"] == [_waiver_row("wc-1", "offered", needed=True)]
    legacy = {**facts, "assessment": _assessment(), "html_path": None}
    del legacy["waiver_candidates"]
    assert ReviewPackage.parse(legacy).waiver_candidates == ()


def test_malformed_waiver_candidate_rows_are_rejected(tmp_path: Path) -> None:
    import pytest

    from booley.review.artifact import ReviewArtifactError, ReviewPackage

    ctx = _context(tmp_path)
    for bad in (
        {**_waiver_row("wc-1", "approved")},
        {**_waiver_row("wc-1", "offered"), "needed": "yes"},
        {**_waiver_row("wc-1", "offered"), "proposal_count": 0},
        {**_waiver_row("wc-1", "offered"), "waiver_reason": "covered_elsewhere"},
    ):
        value = {
            **_facts(ctx),
            "waiver_candidates": [bad],
            "assessment": _assessment(),
            "html_path": None,
        }
        with pytest.raises(ReviewArtifactError):
            ReviewPackage.parse(value)


def test_briefing_groups_waiver_candidates_and_asks_for_decisions(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    rows = [
        _waiver_row("wc-offered", "offered", needed=True),
        _waiver_row("wc-extra", "not_needed"),
        {**_waiver_row("wc-old", "stale"), "status_reason": "RTL source changed"},
    ]
    package = {
        **_facts(ctx),
        "waiver_candidates": rows,
        "assessment": _assessment(),
        "html_path": None,
        "inspection": {
            "schema": 1,
            "disposition": "unaccepted",
            "reason": "Coverage is met only provisionally",
            "blocked_reason": "",
            "heads": {},
            "ticket_generation": "g",
        },
    }
    package["criteria"] = [
        {**row, "required": "mandatory", "status": "unmet"} for row in package["criteria"][:1]
    ] or package["criteria"]

    rendered = tp.render_review_briefing(package, [])

    section = rendered.split("#### Waiver Candidates", 1)[1]
    assert section.index("Offered for your decision") < section.index("wc-offered")
    assert section.index("wc-offered") < section.index("Not needed") < section.index("wc-extra")
    assert "**needed**" in section
    assert "line: 50.0% → 100.0% (min 70%)" in section
    assert "candidate record, unverified" in section
    assert "Reset-only \\| tied off" in section  # Markdown table pipes stay escaped
    assert "Why: RTL source changed" in section
    assert "--accept-waivers" in section
    assert "**decide waivers and approve**" in rendered.rsplit("Choose:", 1)[1]


def test_briefing_omits_the_section_without_candidates(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    package = {**_facts(ctx), "assessment": _assessment(), "html_path": None}

    assert "Waiver Candidates" not in tp.render_review_briefing(package, [])


@pytest.mark.parametrize("mode", ["done", "clean"])
@pytest.mark.parametrize(
    "original", ["current", "advisory", "deferred", "out_of_scope", "superseded"]
)
@pytest.mark.parametrize("report_enabled", [True, False])
def test_normalized_review_facts_write_package(tmp_path, mode, original, report_enabled):
    ctx = _context(tmp_path)
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = json.loads(state_path.read_text())
    collection = (
        "issue_list"
        if mode == "done"
        else ("pending" if original == "current" else "observations")
    )
    state["criteria"] = {
        f"review_rtl_bugs_{mode}": {
            "mandatory": False,
            "met": True,
            "detail": {
                collection: [
                    {
                        "severity": "MAJOR",
                        "summary": "visible finding",
                        "disposition": original,
                        "line": 0,
                    }
                ]
            },
        }
    }
    state_path.write_text(json.dumps(state))
    if not report_enabled:
        (ctx.log_dir / "REPORT.md").unlink()
    facts = _facts(ctx, freshness_eligible=_never_freshness_eligible)
    path = tp.write_triage_package(
        ctx, facts, _assessment(), ctx.log_dir / "explanation.html" if report_enabled else None
    )
    package = tp.load_triage_package(path)
    if mode == "done":
        assert (tp.DONE_FINDINGS_HOLD in package["assessment"]["decision_blockers"]) == (
            original == "current"
        )
    row = package.review_dispositions[0]
    assert row.disposition == ("open" if original == "current" else "reported")
    assert row.reviewer_disposition == original
    rendered = tp.render_review_briefing(package, [])
    assert "Reviewer disposition" in rendered
    assert f"{row.disposition} | {original} |" in rendered


def test_reviewer_disposition_markdown_is_inert():
    lines = []
    tp._render_review_dispositions(
        lines,
        {
            "review_dispositions": [
                {
                    "disposition": "open",
                    "reviewer_disposition": "<script>|bad\ntext",
                    "summary": "finding",
                }
            ]
        },
    )
    rendered = "\n".join(lines)
    assert "\\<script>\\|bad" in rendered
    assert "bad\ntext" not in rendered


def test_current_done_finding_package_requires_human_even_with_agent_approve(tmp_path):
    """Real publication exposes outstanding findings independently of report enablement."""
    ctx = _context(tmp_path)
    facts = _facts(ctx)
    from booley.evidence.review_dispositions import collect_review_dispositions

    facts["review_dispositions"] = collect_review_dispositions(
        {
            "review_rtl_bugs_done": {
                "detail": {
                    "issue_list": [
                        {
                            "criterion": "review_rtl_bugs_done",
                            "finding_id": "minor",
                            "severity": "MINOR",
                            "file": "rtl/new.sv",
                            "line": 1,
                            "summary": "current finding",
                            "disposition": "current",
                            "status": "current",
                        }
                    ]
                }
            }
        }
    )
    assessment = _assessment()
    assessment["recommendation"] = "approve"
    path = tp.write_triage_package(ctx, facts, assessment, None)
    package = tp.load_triage_package(path)
    assert package["assessment"]["recommendation"] == "hold"
    assert tp.DONE_FINDINGS_HOLD in package["assessment"]["decision_blockers"]
    assert "current finding" in tp.render_review_briefing(package, [])


def test_generic_acceptance_does_not_claim_human_approval():
    package = {
        "inspection": {"disposition": "unaccepted"},
        "review_dispositions": [{"summary": "still visible"}],
        "assessment": {
            "recommendation": "hold",
            "decision_blockers": [tp.DONE_FINDINGS_HOLD],
            "findings": [tp.DONE_FINDINGS_HOLD],
        },
    }
    accepted = tp.accepted_review_presentation(package)
    assert accepted["assessment"]["recommendation"] == "approve"
    assert accepted["assessment"]["decision_blockers"] == []
    assert "accepted by the Human" not in " ".join(accepted["assessment"]["findings"])
    assert tp.DONE_FINDINGS_HOLD in accepted["assessment"]["findings"]
    assert accepted["review_dispositions"] == package["review_dispositions"]


def test_combined_done_findings_and_coverage_choices_are_both_visible(tmp_path):
    from booley.evidence.review_dispositions import collect_review_dispositions

    ctx = _context(tmp_path)
    facts = _facts(ctx)
    facts["review_dispositions"] = collect_review_dispositions(
        {
            "review_old_done": {
                "detail": {
                    "issue_list": [
                        {
                            "severity": "MINOR",
                            "summary": "historical obligation",
                            "disposition": "current",
                        }
                    ]
                }
            }
        }
    )
    facts["waiver_candidates"] = [_waiver_row("wc-offered", "offered", needed=True)]
    facts["inspection"] = {
        "schema": 1,
        "disposition": "unaccepted",
        "reason": "combined review",
        "blocked_reason": "",
        "heads": {},
        "ticket_generation": "g",
    }
    facts["criteria"] = [
        {**row, "required": "mandatory", "status": "unmet"} for row in facts["criteria"][:1]
    ]
    path = tp.write_triage_package(ctx, facts, _assessment(), None)
    package = tp.load_triage_package(path)
    briefing = tp.render_review_briefing(package, [])
    assert tp.DONE_FINDINGS_HOLD in package["assessment"]["decision_blockers"]
    assert "historical obligation" in briefing
    assert "wc-offered" in briefing
    assert "decide waivers and approve" in briefing


def test_generic_accepted_projection_keeps_one_neutral_note():
    package = {
        "inspection": {"disposition": "accepted"},
        "assessment": {
            "recommendation": "hold",
            "decision_blockers": [tp.DONE_FINDINGS_HOLD],
            "findings": [tp.DONE_FINDINGS_ACCEPTED, tp.DONE_FINDINGS_HOLD],
        },
    }
    projected = tp.accepted_review_presentation(package)
    assert projected["assessment"]["findings"].count(tp.DONE_FINDINGS_ACCEPTED) == 1


@pytest.mark.parametrize("completed", [False, True])
def test_triage_uses_only_effective_report_and_justifications(tmp_path, completed):
    from booley.criteria.state import DevelopmentState
    from booley.ticket_board import report_submission as rs

    ctx = _context(tmp_path)
    state = DevelopmentState.load(ctx.log_dir / ".runtime/booley_state.json")
    state.set_criterion(rs.KEY, False)
    attempt = rs.Submission(ctx.log_dir, "a" * 32, {}, "execution")
    attempt.stage(b"candidate report")
    detail = {
        rs.ID_KEY: "a" * 32,
        rs.DIGEST_KEY: attempt.row["report_sha256"],
        "file_justifications": {"rtl/new.sv": "Explained change."},
    }
    state.set_criterion(rs.KEY, True, detail=detail)
    state.save()
    if completed:
        attempt.commit()
    attempt.close()
    facts = _facts(ctx)
    assert bool(facts["scope"]["file_justifications"]) is completed
    report = Path(facts["developer_report_path"])
    assert (report == ctx.log_dir / "REPORT.md") is completed
    assert (b"candidate report" in report.read_bytes()) is completed
