"""Tests for deterministic interactive-triage packages."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote

from booley.harness import triage_package as tp


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


def test_review_facts_materialize_rename_pair_and_oldest_first_commits(
    tmp_path: Path, monkeypatch
):
    ctx = _context(tmp_path)
    monkeypatch.setattr(tp, "_usage_summary", lambda _ctx: "tokens=10 cost=$0.01")

    facts = tp.build_review_facts(ctx)

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
    monkeypatch.setattr(tp, "_usage_summary", lambda _ctx: "unavailable")

    facts = tp.build_review_facts(ctx)

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
    monkeypatch.setattr(tp, "_usage_summary", lambda _ctx: "unavailable")

    changes = {row["path"]: row for row in tp.build_review_facts(ctx)["changed_files"]}

    assert changes["rtl/link.sv"]["content_kind"] == "symlink"
    assert changes["rtl/link.sv"]["presentation"] == "text"
    assert changes["rtl/blob.bin"]["content_kind"] == "regular"
    assert changes["rtl/blob.bin"]["presentation"] == "binary"
    assert changes["deps/ip"]["content_kind"] == "submodule"
    assert changes["deps/ip"]["action"] == "added"
    assert changes["deps/ip"]["new_endpoint"]["workspace_path"] is None


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
    monkeypatch.setattr(tp, "_usage_summary", lambda _ctx: "unavailable")

    facts = tp.build_review_facts(ctx)
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
    monkeypatch.setattr(tp, "_usage_summary", lambda _ctx: "unavailable")

    facts = tp.build_review_facts(ctx)
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
    monkeypatch.setattr(tp, "_usage_summary", lambda _ctx: "unavailable")

    facts = tp.build_review_facts(ctx)

    assert facts["health"]["unverified_transitions"] == ["sim_pass"]


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
    assert "Choose: **approve** / **archive** / **reset** / **skip**." in rendered


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
