"""Explicit discovery/verification and Goal-owned review contracts (issue #1507)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from booley.criteria.freshness import evaluate_verification_freshness
from booley.criteria.state import DevelopmentState
from booley.flows.execution_persistence import NoAcceptanceRecorder
from booley.flows.review_scope import resolve_review_files
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.goals.freshness import evaluate_goal_freshness, stamp_goal_detail
from booley.goals.model import GoalArgError, GoalFamily, GoalSpec, parse_goal_arg
from booley.mcp.schema_extractor import extract_schema
from booley.mcp.server import _params_to_argv
from booley.specialists.reviewer import ReviewerSpecialist

KEY = "review_rtl_bugs_clean"
ISSUE = {
    "severity": "MAJOR",
    "confidence": "HIGH",
    "category": "bugs",
    "file": "rtl/dut.sv",
    "line": 1,
    "summary": "Wrong reset value",
    "kind": "code_defect",
    "disposition": "current",
    "ticket_clause": "Reset clears output",
}


class GoalRecorder(NoAcceptanceRecorder):
    def review_goal_inputs(self, key):
        assert key in {KEY, "review_rtl_bugs_done"}
        return {"paths": ["rtl/dut.sv"]}


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/dut.sv").write_text("module dut; endmodule\n")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    state = DevelopmentState.load(tmp_path / "state.json")
    state.init_criteria({KEY: True})
    state.save()
    return tmp_path


def endpoint(root, monkeypatch, args, *, goal=True, payload=None):
    reviewer = ReviewerSpecialist()
    if goal:
        reviewer._acceptance_recorder = GoalRecorder()
    reviewer.parse_args(["--work-dir", str(root), *args])
    reviewer.read_state()
    invoke = MagicMock(
        return_value=MagicMock(
            output=json.dumps(payload if payload is not None else {"issues": [ISSUE]}),
            captured_agent_capability_calls={},
        )
    )
    monkeypatch.setattr(reviewer, "_invoke_agent", invoke)
    return reviewer, invoke


def discovery(root, monkeypatch, **kwargs):
    reviewer, invoke = endpoint(
        root, monkeypatch, ["--category", "rtl", "--focus", "bugs"], **kwargs
    )
    return reviewer._run(), reviewer, invoke


@pytest.mark.parametrize("scope", [{}, {"target": "top", "paths": ["rtl/dut.sv"]}, {"paths": []}])
def test_review_goal_requires_exactly_one_nonempty_scope(scope):
    with pytest.raises(GoalArgError, match=r"exactly one|must not be empty"):
        parse_goal_arg({"family": "REVIEW", "review": "rtl_bugs", "verdict": "clean", **scope})


@pytest.mark.parametrize("path", ["../dut.sv", "/dut.sv", "C:\\dut.sv", "a\\..\\dut.sv"])
def test_review_paths_cannot_escape(path):
    with pytest.raises(GoalArgError, match="relative"):
        parse_goal_arg(
            {"family": "REVIEW", "review": "rtl_bugs", "verdict": "clean", "paths": [path]}
        )


def test_review_paths_match_category_and_stay_inside_worktree(project):
    with pytest.raises(ValueError, match="category"):
        resolve_review_files(project, "rtl", {"paths": ["tb/test.py"]})
    outside = project.parent / "outside.sv"
    outside.write_text("module outside; endmodule")
    (project / "rtl/link.sv").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        resolve_review_files(project, "rtl", {"paths": ["rtl/link.sv"]})


@pytest.mark.parametrize(
    "option,value", [("--scope", "rtl/dut.sv"), ("--spec", "spec.md"), ("--steer", "skip reset")]
)
def test_goal_refuses_developer_review_inputs(project, monkeypatch, option, value):
    reviewer, invoke = endpoint(
        project, monkeypatch, ["--category", "rtl", "--focus", "bugs", option, value]
    )
    result = reviewer._run()
    assert result.exit_code == 2
    assert "Review Goal defines" in result.report_text
    invoke.assert_not_called()


def test_goal_discovery_refuses_open_findings_and_verify_only_checks_fixes(project, monkeypatch):
    initial, _, _ = discovery(project, monkeypatch)
    assert initial.exit_code == 1
    assert initial.detail["pending"][0]["finding_id"] in initial.report_text
    refused, _, invoke = discovery(project, monkeypatch)
    assert refused.exit_code == 2
    assert "--verify" in refused.report_text
    invoke.assert_not_called()
    (project / "rtl/dut.sv").write_text("module dut; logic fixed; endmodule\n")
    verifier, invoke = endpoint(
        project,
        monkeypatch,
        ["--verify"],
        payload={
            "findings": [
                {"index": 1, "status": "FIXED", "evidence": "rtl/dut.sv:1 reset value corrected"},
            ]
        },
    )
    result = verifier._run()
    assert result.exit_code == 0
    assert result.criterion_met
    assert result.detail["pending"] == []
    assert "FIXED" in result.report_text and "VERIFY RESULT: PASS (0 issues)" in result.report_text
    assert invoke.call_count == 1
    assert "ONLY job" in invoke.call_args.args[0].system_prompt
    assert not evaluate_verification_freshness(
        KEY,
        verifier.state.criteria[KEY],
        work_dir=project,
        fingerprint_provider=compute_source_fingerprint,
    ).stale
    (project / "rtl/dut.sv").write_text("module dut; logic changed_again; endmodule\n")
    assert evaluate_verification_freshness(
        KEY,
        verifier.state.criteria[KEY],
        work_dir=project,
        fingerprint_provider=compute_source_fingerprint,
    ).stale


@pytest.mark.parametrize(
    "argument",
    [
        ["--category", "rtl"],
        ["--focus", "bugs"],
        ["--scope", "rtl/dut.sv"],
        ["--steer", "waive"],
        ["--dry-run"],
        ["--model", "heavy"],
        ["--instruction", "waive"],
    ],
)
def test_verify_refuses_other_review_arguments(project, monkeypatch, argument):
    discovery(project, monkeypatch)
    reviewer, invoke = endpoint(project, monkeypatch, ["--verify", *argument])
    assert reviewer._run().exit_code == 2
    invoke.assert_not_called()


@pytest.mark.parametrize(
    "status,requested,expected",
    [("WAIVED", True, 0), ("STILL_PRESENT", True, 1), ("WAIVED", False, 1)],
)
def test_waivers_require_explicit_open_id_and_reviewer_acceptance(
    project, monkeypatch, status, requested, expected
):
    initial, _, _ = discovery(project, monkeypatch)
    finding_id = initial.detail["pending"][0]["finding_id"]
    args = ["--verify"] + (
        ["--waive", finding_id, "Reset output is intentionally retained by policy"]
        if requested
        else []
    )
    reviewer, _ = endpoint(
        project,
        monkeypatch,
        args,
        payload={
            "findings": [
                {
                    "index": 1,
                    "status": status,
                    "justification": "Retained reset is allowed by policy",
                }
            ]
        },
    )
    result = reviewer._run()
    assert result.exit_code == expected
    assert bool(result.detail["resolved"]) == (expected == 0)


def test_verify_rejects_unknown_waiver_and_no_open_findings(project, monkeypatch):
    discovery(project, monkeypatch)
    reviewer, invoke = endpoint(project, monkeypatch, ["--verify", "--waive", "unknown", "reason"])
    assert reviewer._run().exit_code == 2
    invoke.assert_not_called()
    state = DevelopmentState.load(project / "state.json")
    state.criteria[KEY].detail["pending"] = []
    state.save()
    reviewer, invoke = endpoint(project, monkeypatch, ["--verify"])
    assert "no open findings" in reviewer._run().report_text
    invoke.assert_not_called()


def test_interactive_discovery_replaces_findings_and_latest_call_controls_verify(
    project, monkeypatch
):
    args = ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"]
    reviewer, _ = endpoint(project, monkeypatch, args, goal=False)
    assert reviewer._run().exit_code == 1
    reviewer, invoke = endpoint(
        project, monkeypatch, [*args[:-1], "security"], goal=False, payload={"issues": []}
    )
    assert reviewer._run().exit_code == 0
    assert invoke.call_count == 1
    verifier, invoke = endpoint(project, monkeypatch, ["--verify"], goal=False)
    assert "no open findings" in verifier._run().report_text
    invoke.assert_not_called()


def test_done_verify_preserves_completion_freshness(project, monkeypatch):
    state = DevelopmentState.load(project / "state.json")
    state.criteria.clear()
    state.init_criteria({"review_rtl_bugs_done": True})
    state.save()
    initial, _, _ = discovery(project, monkeypatch)
    before = DevelopmentState.load(project / "state.json").criteria["review_rtl_bugs_done"].detail
    (project / "rtl/dut.sv").write_text("module dut; logic fixed; endmodule\n")
    verifier, _ = endpoint(
        project,
        monkeypatch,
        ["--verify"],
        payload={"findings": [{"index": 1, "status": "FIXED", "evidence": "rtl/dut.sv:1 fixed"}]},
    )
    result = verifier._run()
    assert initial.criterion_met
    assert result.exit_code == 0
    assert (
        not result.criterion_met
    )  # Source edits already made completion stale; verify cannot refresh it.
    after = DevelopmentState.load(project / "state.json").criteria["review_rtl_bugs_done"].detail
    assert after["_source_fingerprint"] == before["_source_fingerprint"]
    assert after["contract"] == before["contract"]
    assert evaluate_verification_freshness(
        "review_rtl_bugs_done",
        {"met": True, "detail": after},
        work_dir=project,
        fingerprint_provider=compute_source_fingerprint,
    ).stale


def test_verify_budgets_keep_cumulative_limit_after_category_reset(project, monkeypatch):
    discovery(project, monkeypatch)
    for _cycle in range(3):
        state = DevelopmentState.load(project / "state.json")
        state.reset_category("rtl")
        state.save()
        verifier, invoke = endpoint(
            project,
            monkeypatch,
            ["--verify"],
            payload={"findings": [{"index": 1, "status": "STILL_PRESENT"}]},
        )
        assert verifier._run().exit_code == 1
        assert invoke.call_count == 1
    state = DevelopmentState.load(project / "state.json")
    state.reset_category("rtl")
    state.save()
    verifier, invoke = endpoint(project, monkeypatch, ["--verify"])
    assert "3 review/resolve cycles exhausted" in verifier._run().report_text
    invoke.assert_not_called()


def test_mcp_waiver_pairs_roundtrip():
    reviewer = ReviewerSpecialist()
    schema = extract_schema(reviewer._parser)
    assert schema["properties"]["waive"]["items"]["minItems"] == 2
    args = _params_to_argv(
        {"verify": True, "waive": [["id1", "first reason"], ["id2", "second reason"]]}
    )
    parsed = reviewer.parse_args(args)
    assert parsed.waive == [["id1", "first reason"], ["id2", "second reason"]]


def test_target_scope_uses_dependency_closure_and_category(project):
    (project / "top.core").write_text("""CAPI=2:
name: ::top:0
filesets:
  rtl: {files: [rtl/dut.sv], file_type: systemVerilogSource, depend: ["::dep:0"]}
  tb: {files: [tb/test.py], file_type: user, tags: [tb]}
targets:
  top: {filesets: [rtl, tb], toplevel: dut}
  other: {filesets: [tb], toplevel: dut}
""")
    (project / "dep.core").write_text("""CAPI=2:
name: ::dep:0
filesets:
  rtl: {files: [rtl/dep.sv], file_type: systemVerilogSource}
targets:
  default: {filesets: [rtl]}
""")
    (project / "rtl/dep.sv").write_text("module dep; endmodule")
    (project / "tb").mkdir()
    (project / "tb/test.py").write_text("async def test(): pass")
    assert resolve_review_files(project, "rtl", {"target": "top"}) == ["rtl/dep.sv", "rtl/dut.sv"]
    assert resolve_review_files(project, "tb", {"target": "top"}) == ["tb/test.py"]


def test_goal_freshness_ignores_unreviewed_files(project, monkeypatch):
    initial, reviewer, _ = discovery(project, monkeypatch, payload={"issues": []})
    assert initial.criterion_met
    goal = GoalSpec(KEY, GoalFamily.REVIEW, None, {"paths": ["rtl/dut.sv"]}, ("ad-hoc",))
    entry = reviewer.state.criteria[KEY]
    entry.detail = stamp_goal_detail(
        KEY, entry.detail, work_dir=project, goal=goal, target_surface={"digest": "surface"}
    )
    (project / "rtl/unrelated.sv").write_text("module unrelated; endmodule")
    assert not evaluate_goal_freshness(
        KEY, reviewer.state.criteria[KEY], goal=goal, work_dir=project
    ).stale


def test_verify_does_not_certify_a_changed_spec(project, monkeypatch):
    spec = project / "spec.md"
    spec.write_text("Reset clears output")
    reviewer, _ = endpoint(
        project,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "spec", "--spec", "spec.md"],
        goal=False,
        payload={"issues": [{**ISSUE, "category": "spec"}]},
    )
    assert reviewer._run().exit_code == 0
    spec.write_text("New requirements after discovery")
    verifier, _ = endpoint(
        project,
        monkeypatch,
        ["--verify"],
        goal=False,
        payload={
            "findings": [{"index": 1, "status": "FIXED", "evidence": "rtl/dut.sv:1 corrected"}]
        },
    )
    result = verifier._run()
    assert result.exit_code == 0
    freshness = evaluate_verification_freshness(
        "review_rtl_spec_done",
        verifier.state.criteria["review_rtl_spec_done"],
        work_dir=project,
        fingerprint_provider=compute_source_fingerprint,
    )
    assert freshness.stale and "spec" in freshness.review_dimensions


def test_source_change_during_review_cannot_certify_unreviewed_bytes(project, monkeypatch):
    reviewer, invoke = endpoint(
        project, monkeypatch, ["--category", "rtl", "--focus", "bugs"], payload={"issues": []}
    )
    response = invoke.return_value

    def mutate(*args, **kwargs):
        (project / "rtl/dut.sv").write_text("module dut; wire unreviewed; endmodule")
        return response

    invoke.side_effect = mutate
    assert reviewer._run().exit_code == 0
    freshness = evaluate_verification_freshness(
        KEY,
        reviewer.state.criteria[KEY],
        work_dir=project,
        fingerprint_provider=compute_source_fingerprint,
    )
    assert freshness.stale


def test_done_verify_provider_error_keeps_completion_evidence(project, monkeypatch):
    state = DevelopmentState.load(project / "state.json")
    state.criteria.clear()
    state.init_criteria({"review_rtl_bugs_done": True})
    state.save()
    discovery(project, monkeypatch)
    before = (
        DevelopmentState.load(project / "state.json").criteria["review_rtl_bugs_done"].to_dict()
    )
    verifier, invoke = endpoint(project, monkeypatch, ["--verify"])
    invoke.side_effect = RuntimeError("provider unavailable")
    assert verifier._run().exit_code == 2
    after = (
        DevelopmentState.load(project / "state.json").criteria["review_rtl_bugs_done"].to_dict()
    )
    assert after["met"] == before["met"]
    assert after["detail"]["contract"] == before["detail"]["contract"]
    assert after["detail"]["_source_fingerprint"] == before["detail"]["_source_fingerprint"]


def test_interactive_history_survives_without_explicit_state_environment(project, monkeypatch):
    monkeypatch.delenv("BOOLEY_STATE_FILE")
    reviewer, _ = endpoint(
        project,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"],
        goal=False,
    )
    assert reviewer._run().exit_code == 0  # Interactive discovery defaults to done.
    assert reviewer.state.file_path is not None
    verifier, _ = endpoint(
        project,
        monkeypatch,
        ["--verify"],
        goal=False,
        payload={
            "findings": [{"index": 1, "status": "FIXED", "evidence": "rtl/dut.sv:1 corrected"}]
        },
    )
    assert verifier._run().exit_code == 0
    assert verifier.state.file_path == reviewer.state.file_path


def test_goal_resolves_open_review_before_starting_another_focus(project, monkeypatch):
    discovery(project, monkeypatch)
    reviewer, invoke = endpoint(project, monkeypatch, ["--category", "rtl", "--focus", "security"])
    result = reviewer._run()
    assert result.exit_code == 2
    assert "before starting another discovery review" in result.report_text
    invoke.assert_not_called()
