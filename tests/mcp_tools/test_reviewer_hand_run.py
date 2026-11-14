"""Hand-run Reviewer: inline spec text, opt-in failing exit, firmware context (issue #3269).

Every test drives a real Interactive Reviewer (real ``reviewer-state``, no Goal recorder)
with the agent call mocked to return canned findings.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from booley.criteria.state import DevelopmentState
from booley.evidence.review_receipt import review_receipt_drift
from booley.mcp.base import McpToolResult
from booley.specialists.reviewer import ReviewerSpecialist

BASE_ARGS = ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"]
SPEC_ARGS = ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "spec"]
TB_ARGS = ["--scope", "tb/tb.sv", "--category", "tb", "--focus", "quality"]


def finding(
    severity: str = "MAJOR",
    file: str = "rtl/dut.sv",
    summary: str = "Wrong reset value",
    category: str = "bugs",
    line: int = 3,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "confidence": "HIGH",
        "category": category,
        "file": file,
        "line": line,
        "summary": summary,
        "kind": "code_defect",
        "disposition": "current",
        "ticket_clause": "Reset clears output",
    }


def waiver_verdicts(count: int) -> dict[str, Any]:
    """A verify answer accepting the waiver proposed for each of *count* findings."""
    return {
        "findings": [
            {"index": index, "status": "WAIVED", "justification": "Accepted as designed"}
            for index in range(1, count + 1)
        ]
    }


def verdicts(*statuses: str) -> dict[str, Any]:
    """A verify answer; FIXED rows cite evidence so they are not demoted."""
    return {
        "findings": [
            {"index": index, "status": status, "evidence": "rtl/dut.sv:1 corrected"}
            for index, status in enumerate(statuses, 1)
        ]
    }


def build_project(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with RTL, testbench and firmware files and its own Reviewer state file."""
    for name in ("rtl/dut.sv", "tb/tb.sv", "fw/start.c"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// authored\n")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(root / "state.json"))
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    return root


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return build_project(tmp_path, monkeypatch)


def review(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    payload: dict[str, Any] | None = None,
) -> tuple[McpToolResult, MagicMock]:
    """Run one hand-run Reviewer invocation; returns its result and the agent mock."""
    reviewer = ReviewerSpecialist()
    reviewer.parse_args(["--work-dir", str(root), *args])
    reviewer.read_state()
    invoke = MagicMock(
        return_value=MagicMock(
            output=json.dumps(payload if payload is not None else {"issues": []}),
            captured_agent_capability_calls={},
        )
    )
    monkeypatch.setattr(reviewer, "_invoke_agent", invoke)
    return reviewer._run(), invoke


def prompt_of(invoke: MagicMock) -> str:
    return invoke.call_args.args[0].prompt


def test_spec_text_is_reviewed_reported_and_reused_by_verify(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "Reset drives q low.\nEnable is active high."
    result, invoke = review(
        project,
        monkeypatch,
        [*SPEC_ARGS, "--spec-text", text],
        {"issues": [finding(category="spec")]},
    )
    assert result.exit_code == 0, result.report_text
    assert f"## Specification (source: inline text)\n\n{text}" in prompt_of(invoke)
    assert f"Spec (inline text):\n{text}" in result.report_text
    assert review_receipt_drift(result.detail, project) == []

    verifier, invoke = review(project, monkeypatch, ["--verify"], verdicts("STILL_PRESENT"))
    assert verifier.exit_code == 0, verifier.report_text
    assert text in prompt_of(invoke)
    assert f"Spec (inline text):\n{text}" in verifier.report_text


def test_spec_and_spec_text_together_is_a_usage_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        ReviewerSpecialist().parse_args(
            ["--work-dir", str(project), *SPEC_ARGS, "--spec", "s.md", "--spec-text", "x"]
        )
    assert exit_info.value.code == 2
    assert "--spec-text: not allowed with argument --spec" in capsys.readouterr().err


def test_empty_spec_text_is_an_error(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, invoke = review(project, monkeypatch, [*SPEC_ARGS, "--spec-text", "  \n"])
    assert result.exit_code == 2
    assert "--spec-text must not be empty" in result.report_text
    invoke.assert_not_called()


def test_missing_spec_file_fails_while_same_words_as_spec_text_review(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, invoke = review(project, monkeypatch, [*SPEC_ARGS, "--spec", "missing.md"])
    assert result.exit_code == 2
    assert str(project / "missing.md") in result.report_text
    invoke.assert_not_called()

    result, invoke = review(project, monkeypatch, [*SPEC_ARGS, "--spec-text", "missing.md"])
    assert result.exit_code == 0, result.report_text
    assert "missing.md" in prompt_of(invoke)


def test_completed_review_and_verify_exit_zero_without_fail_on(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = review(project, monkeypatch, BASE_ARGS, {"issues": [finding("MAJOR")]})
    assert result.exit_code == 0, result.report_text
    assert len(ReviewerSpecialist._open_findings(result.detail)) == 1

    for _attempt in range(2):
        result, _ = review(project, monkeypatch, ["--verify"], verdicts("STILL_PRESENT"))
        assert result.exit_code == 0, result.report_text
        assert len(result.detail["pending"]) == 1

    # The verify budget is a refusal, not a finding: exit 1 stays reserved for --fail-on.
    result, invoke = review(project, monkeypatch, ["--verify"])
    assert result.exit_code == 2
    assert "verify attempts exhausted" in result.report_text
    invoke.assert_not_called()


@pytest.mark.parametrize(
    ("fail_on", "severity", "expected"),
    [
        ("major", "MAJOR", 1),
        ("major", "CRITICAL", 1),
        ("major", "MINOR", 0),
        ("critical", "CRITICAL", 1),
        ("critical", "MAJOR", 0),
        ("minor", "MINOR", 1),
    ],
)
def test_fail_on_threshold(
    project: Path, monkeypatch: pytest.MonkeyPatch, fail_on: str, severity: str, expected: int
) -> None:
    result, _ = review(
        project, monkeypatch, [*BASE_ARGS, "--fail-on", fail_on], {"issues": [finding(severity)]}
    )
    assert result.exit_code == expected, result.report_text


def test_verify_fail_on_counts_findings_still_open(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review(
        project,
        monkeypatch,
        BASE_ARGS,
        {"issues": [finding("MAJOR"), finding("MINOR", summary="Style nit", line=4)]},
    )
    result, _ = review(
        project,
        monkeypatch,
        ["--verify", "--fail-on", "major"],
        verdicts("STILL_PRESENT", "STILL_PRESENT"),
    )
    assert result.exit_code == 1, result.report_text
    # Only the minor finding stays open; a major threshold no longer fails.
    result, _ = review(
        project,
        monkeypatch,
        ["--verify", "--fail-on", "major"],
        verdicts("FIXED", "STILL_PRESENT"),
    )
    assert result.exit_code == 0, result.report_text
    assert len(result.detail["pending"]) == 1


def test_goal_mode_refuses_spec_text_and_fail_on(project: Path) -> None:
    from booley.flows.execution_persistence import NoAcceptanceRecorder, ReviewInputs

    class GoalRecorder(NoAcceptanceRecorder):
        def review_goal_inputs(self, key: str) -> ReviewInputs:
            return ReviewInputs(("rtl/dut.sv",))

    for option, value in (("--spec-text", "words"), ("--fail-on", "major")):
        reviewer = ReviewerSpecialist()
        reviewer._acceptance_recorder = GoalRecorder()
        reviewer.parse_args(
            ["--work-dir", str(project), "--category", "rtl", "--focus", "bugs", option, value]
        )
        reviewer.read_state()
        result = reviewer._run()
        assert result.exit_code == 2
        assert option in result.report_text

    verifier = ReviewerSpecialist()
    verifier._acceptance_recorder = GoalRecorder()
    verifier.parse_args(["--work-dir", str(project), "--verify", "--fail-on", "major"])
    verifier.read_state()
    result = verifier._run()
    assert result.exit_code == 2
    assert "--fail-on" in result.report_text


@pytest.mark.parametrize("args", [BASE_ARGS, TB_ARGS], ids=["rtl", "tb"])
def test_firmware_findings_are_out_of_scope(
    project: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    category = "quality" if args is TB_ARGS else "bugs"
    in_scope = args[1]
    firmware = project / "fw/start.c"  # snapshot results are restored to absolute paths
    result, _ = review(
        project,
        monkeypatch,
        [*args, "--fail-on", "major"],
        {
            "issues": [
                finding("MAJOR", str(firmware), "Stack never initialised", category),
                finding("MINOR", in_scope, "Style nit", category),
            ]
        },
    )
    assert result.exit_code == 0, result.report_text
    assert [row["file"] for row in ReviewerSpecialist._open_findings(result.detail)] == [in_scope]
    (row,) = [r for r in result.detail["issue_list"] if r["file"] == str(firmware)]
    assert row["disposition"] == "out_of_scope"
    assert result.report_text.count("OUT OF SCOPE (firmware is read-only context)") == 1
    assert result.report_text.count("Stack never initialised") == 1


@pytest.mark.parametrize("args", [BASE_ARGS, TB_ARGS], ids=["rtl", "tb"])
@pytest.mark.parametrize("spelling", ["relative", "absolute"])
def test_firmware_only_scope_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], spelling: str
) -> None:
    scope = "fw/start.c" if spelling == "relative" else str(project / "fw/start.c")
    result, invoke = review(project, monkeypatch, ["--scope", scope, *args[2:]])
    assert result.exit_code == 2
    assert "firmware review is not supported yet" in result.report_text.lower()
    invoke.assert_not_called()


def test_mixed_scope_reviews_non_firmware_and_reports_firmware_as_context(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, invoke = review(
        project, monkeypatch, ["--scope", "rtl/dut.sv,fw/start.c", *BASE_ARGS[2:]]
    )
    assert result.exit_code == 0, result.report_text
    assert "  - rtl/dut.sv" in prompt_of(invoke)
    assert "  - fw/start.c" not in prompt_of(invoke)
    assert "INFO: firmware is read-only context, not reviewed: fw/start.c" in result.report_text


# --- Two guide calls: rtl/bugs runs a checklist call and a hunt call (issue #4016) ---

BUGS_GUIDES = ("bugs-checklist", "bugs-hunt")
BUGS_CLEAN_KEY = "review_rtl_bugs_clean"


def guide_of(params: Any) -> str:
    """The one bugs guide a call's system prompt carries."""
    carried = [g for g in BUGS_GUIDES if f"## Review guide — bugs ({g})" in params.system_prompt]
    assert len(carried) == 1, carried
    return carried[0]


def agent_result(payload: dict[str, Any], mirror: dict[str, Any] | None = None) -> MagicMock:
    """A scripted agent answer: final-message JSON plus the ReportFindings mirror."""
    return MagicMock(output=json.dumps(payload), captured_agent_capability_calls=mirror or {})


def review_by_guide(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    answers: dict[str, Any],
    *,
    reviewer: ReviewerSpecialist | None = None,
) -> tuple[McpToolResult, list[Any]]:
    """Run a bugs review whose agent answers per guide; returns the result and every call.

    An answer is a payload dict, a ready agent result, or an exception to raise.
    """
    reviewer = reviewer or ReviewerSpecialist()
    reviewer.parse_args(["--work-dir", str(root), *args])
    reviewer.read_state()
    calls: list[Any] = []

    def fake_agent(params: Any) -> Any:
        calls.append(params)
        answer = answers[guide_of(params)]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, MagicMock) else agent_result(answer)

    monkeypatch.setattr(reviewer, "_invoke_agent", fake_agent)
    return reviewer._run(), calls


def seed_clean_criterion(root: Path) -> None:
    """Make the next review a ``_clean`` (disposition loop) discovery."""
    state = DevelopmentState.load(root / "state.json")
    state.set_criterion(BUGS_CLEAN_KEY, met=False)
    state.save()


def test_bugs_review_runs_checklist_and_hunt_concurrently(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real agent seam runs both guide calls at once and sums their usage."""
    import threading
    from types import SimpleNamespace

    barrier = threading.Barrier(2, timeout=20)  # sequential calls would break it
    answers = {
        "bugs-checklist": ({"issues": [finding("MAJOR", summary="Checklist finding")]}, 0.5, 10),
        "bugs-hunt": ({"issues": [finding("MINOR", summary="Hunt finding", line=7)]}, 0.25, 20),
    }
    systems: dict[str, str] = {}

    def fake_call(params: Any, on_event: Any = None) -> Any:
        guide = guide_of(params)
        systems[guide] = params.system_prompt
        barrier.wait()
        payload, cost, tokens = answers[guide]
        return SimpleNamespace(
            output=json.dumps(payload),
            captured_agent_capability_calls={},
            input_tokens=tokens,
            output_tokens=tokens,
            cached_tokens=0,
            cache_create_tokens=0,
            cost_usd=cost,
            session_id=f"session-{guide}",
        )

    monkeypatch.setattr("booley.specialists.specialist._call_agent_sync", fake_call)

    # The usage lock and per-call model bookkeeping run for real; the workspace snapshot
    # (which needs a set-up FuseSoC project) is skipped, as in the other Reviewer unit tests.
    @contextlib.contextmanager
    def passthrough(params: Any, *_args: Any, **_kwargs: Any) -> Iterator[tuple[Any, None]]:
        yield params, None

    monkeypatch.setattr("booley.specialists.specialist.isolated_agent_workspace", passthrough)
    reviewer = ReviewerSpecialist()
    transcripts = project / "transcripts"
    reviewer.parse_args(
        ["--work-dir", str(project), *BASE_ARGS, "--transcript-dir", str(transcripts)]
    )
    reviewer.read_state()
    result = reviewer._run()

    assert result.exit_code == 0, result.report_text
    # Each call's prompt carries only its own guide.
    assert "(bugs-hunt)" not in systems["bugs-checklist"]
    assert "(bugs-checklist)" not in systems["bugs-hunt"]
    summaries = {row["summary"]: row for row in result.detail["issue_list"]}
    assert set(summaries) == {"Checklist finding", "Hunt finding"}
    assert summaries["Checklist finding"]["guide_kinds"] == ["checklist"]
    assert summaries["Hunt finding"]["guide_kinds"] == ["hunt"]
    # Usage of both calls counts, though they ran concurrently.
    assert reviewer._total_cost_usd == pytest.approx(0.75)
    assert reviewer._total_input_tokens == 30
    calls = result.detail["guide_calls"]
    assert [(c["kind"], c["guides"], c["status"]) for c in calls] == [
        ("checklist", ["bugs-checklist"], "ok"),
        ("hunt", ["bugs-hunt"], "ok"),
    ]
    assert [Path(c["transcript"]).name for c in calls] == [
        "reviewer-bugs-checklist.jsonl",
        "reviewer-bugs-hunt.jsonl",
    ]
    assert len({c["attempt_id"] for c in calls}) == 2
    # The first call's session is the one kept.
    assert reviewer._last_session_id == "session-bugs-checklist"


def test_exact_duplicate_findings_merge_with_highest_severity_and_confidence(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checklist = finding("MAJOR", summary="Reset value is wrong")
    checklist["confidence"] = "MEDIUM"
    hunt = finding("CRITICAL", summary="Reset leaves q stale", file="./rtl/dut.sv")
    hunt["confidence"] = "LOW"
    result, calls = review_by_guide(
        project,
        monkeypatch,
        BASE_ARGS,
        {"bugs-checklist": {"issues": [checklist]}, "bugs-hunt": {"issues": [hunt]}},
    )
    assert len(calls) == 2
    (row,) = result.detail["issue_list"]
    assert (row["severity"], row["confidence"]) == ("CRITICAL", "MEDIUM")
    assert row["summary"] == "Reset leaves q stale"  # the highest-severity member represents it
    assert row["guide_kinds"] == ["checklist", "hunt"]
    assert "merged 1 exact duplicate" in result.report_text


def test_near_duplicate_findings_stay_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different line, kind or file is never an exact duplicate."""
    changes = [{"line": 4}, {"kind": "proof_gap"}, {"file": "rtl/other.sv"}]
    for number, changed in enumerate(changes):
        project = build_project(tmp_path / f"case{number}", monkeypatch)
        _assert_near_duplicates_stay_separate(project, monkeypatch, changed)


def _assert_near_duplicates_stay_separate(
    project: Path, monkeypatch: pytest.MonkeyPatch, changed: dict[str, Any]
) -> None:
    other = {**finding("MAJOR", summary="Same problem, elsewhere"), **changed}
    (project / "rtl/other.sv").write_text("// authored\n")
    result, _ = review_by_guide(
        project,
        monkeypatch,
        [*BASE_ARGS[:1], "rtl/dut.sv,rtl/other.sv", *BASE_ARGS[2:]],
        {"bugs-checklist": {"issues": [finding()]}, "bugs-hunt": {"issues": [other]}},
    )
    assert len(result.detail["issue_list"]) == 2
    assert "merged 0 exact duplicate" in result.report_text


def test_duplicates_never_merge_across_open_finding_and_observation(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open finding is never hidden inside an observation."""
    advisory = {**finding("MAJOR", summary="Seen by the hunt"), "disposition": "advisory"}
    result, _ = review_by_guide(
        project,
        monkeypatch,
        BASE_ARGS,
        {"bugs-checklist": {"issues": [finding()]}, "bugs-hunt": {"issues": [advisory]}},
    )
    rows = result.detail["issue_list"]
    assert sorted(row["status"] for row in rows) == ["advisory", "current"]
    assert len(ReviewerSpecialist._open_findings(result.detail)) == 1


def test_findings_record_guide_kinds_and_gating_ignores_them(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.specialists.reviewer import ReviewIssue, _finding_record

    hunt_only = finding("MAJOR", summary="Only the hunt saw this", line=9)
    result, _ = review_by_guide(
        project,
        monkeypatch,
        [*BASE_ARGS, "--fail-on", "major"],
        {"bugs-checklist": {"issues": []}, "bugs-hunt": {"issues": [hunt_only]}},
    )
    assert result.exit_code == 1, result.report_text  # a hunt-only finding gates as any other
    (row,) = result.detail["issue_list"]
    assert row["guide_kinds"] == ["hunt"]
    untagged = {key: value for key, value in row.items() if key != "guide_kinds"}
    assert _finding_record(ReviewIssue.from_dict(untagged))["finding_id"] == row["finding_id"]


def test_any_failing_guide_call_is_a_specialist_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Either call failing in any way makes the review an error, in both review modes."""
    failures = ("raises", "no_json", "all_rejected", "mirror_disagrees")
    for number, (mode, failing, failure) in enumerate(
        (m, g, f) for m in ("done", "clean") for g in BUGS_GUIDES for f in failures
    ):
        project = build_project(tmp_path / f"case{number}", monkeypatch)
        _assert_failing_guide_is_error(project, monkeypatch, failing, failure, mode)


def _assert_failing_guide_is_error(
    project: Path, monkeypatch: pytest.MonkeyPatch, failing: str, failure: str, mode: str
) -> None:
    broken: dict[str, Any] = {
        "raises": RuntimeError("boom"),
        "no_json": MagicMock(output="no JSON here", captured_agent_capability_calls={}),
        "all_rejected": {"issues": [{"severity": "BOGUS"}]},
        "mirror_disagrees": agent_result(
            {"issues": []},
            {
                "ReportFindings": [
                    {"findings": [{"file": "rtl/dut.sv", "line": 3, "summary": "x"}]}
                ]
            },
        ),
    }
    healthy = {"issues": []}
    answers = {guide: (broken[failure] if guide == failing else healthy) for guide in BUGS_GUIDES}
    key = "review_rtl_bugs_done"
    if mode == "clean":
        seed_clean_criterion(project)
        key = BUGS_CLEAN_KEY
    transcripts = project / "transcripts"
    result, calls = review_by_guide(
        project, monkeypatch, [*BASE_ARGS, "--transcript-dir", str(transcripts)], answers
    )

    assert len(calls) == 2  # the healthy call is not cancelled
    assert result.exit_code == 2, result.report_text
    assert f"ERROR: {failing} call failed" in result.report_text
    other = next(guide for guide in BUGS_GUIDES if guide != failing)
    assert f"ERROR: {other} call failed" not in result.report_text
    assert "issue_list" not in result.detail
    assert "pending" not in result.detail
    assert result.detail["review_error"] is True
    status = {c["guides"][0]: c["status"] for c in result.detail["guide_calls"]}
    assert status == {failing: "error", other: "ok"}
    failing_call = next(c for c in result.detail["guide_calls"] if c["guides"] == [failing])
    assert Path(failing_call["transcript"]).name == f"reviewer-{failing}.jsonl"
    state = DevelopmentState.load(project / "state.json")
    assert state.is_met(key) is False
    # A failed discovery leaves nothing to verify.
    verifier, _ = review(project, monkeypatch, ["--verify"], verdicts())
    assert verifier.exit_code == 2


def test_goal_tb_support_only_scope_still_makes_one_call(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No selected guide is one guide-less call, never zero calls."""
    from booley.flows.execution_persistence import NoAcceptanceRecorder, ReviewInputs

    (project / "tb/notes.txt").write_text("support file\n")

    class GoalRecorder(NoAcceptanceRecorder):
        def review_goal_inputs(self, key: str) -> ReviewInputs:
            return ReviewInputs(("tb/notes.txt",))

    reviewer = ReviewerSpecialist()
    reviewer._acceptance_recorder = GoalRecorder()
    reviewer.parse_args(["--work-dir", str(project), "--category", "tb", "--focus", "quality"])
    reviewer.read_state()
    invoke = MagicMock(return_value=agent_result({"issues": []}))
    monkeypatch.setattr(reviewer, "_invoke_agent", invoke)
    result = reviewer._run()
    assert invoke.call_count == 1, result.report_text
    assert "## Review guide" not in invoke.call_args.args[0].system_prompt
    assert "guide_calls" not in result.detail
    # No guide ran, so the receipt records none to go stale.
    assert result.detail["contract"]["guides"] == []


def test_verify_after_two_guide_review_is_one_call_and_waives_either_guide(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_clean_criterion(project)
    checklist = finding("MAJOR", summary="Checklist finding")
    hunt = finding("MAJOR", summary="Hunt finding", line=8)
    result, _ = review_by_guide(
        project,
        monkeypatch,
        BASE_ARGS,
        {"bugs-checklist": {"issues": [checklist]}, "bugs-hunt": {"issues": [hunt]}},
    )
    by_summary = {row["summary"]: row["finding_id"] for row in result.detail["pending"]}
    assert set(by_summary) == {"Checklist finding", "Hunt finding"}

    verify_args = ["--verify"]
    for summary in ("Hunt finding", "Checklist finding"):
        verify_args += ["--waive", by_summary[summary], "Accepted as designed"]
    verifier, invoke = review(project, monkeypatch, verify_args, waiver_verdicts(2))
    assert invoke.call_count == 1  # verify never fans out
    assert "## Review guide" not in invoke.call_args.args[0].system_prompt
    assert verifier.exit_code == 0, verifier.report_text
    assert {row["status"] for row in verifier.detail["resolved"]} == {"waived"}
    assert verifier.detail["pending"] == []


def test_single_guide_focus_makes_one_call_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec, protocol and testbench quality reviews still make one call and one transcript."""
    cases = [
        (SPEC_ARGS, "spec", "spec-hunt"),
        ([*BASE_ARGS[:-1], "protocol"], "protocol", "protocol-cdc-checklist"),
        (TB_ARGS, "quality", "hdl-checklist"),
    ]
    for number, (args, category, guide) in enumerate(cases):
        project = build_project(tmp_path / f"case{number}", monkeypatch)
        _assert_one_call(project, monkeypatch, args, category, guide)


def _assert_one_call(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    category: str,
    guide: str,
) -> None:
    extra = ["--spec-text", "Reset drives q low."] if category == "spec" else []
    transcripts = project / "transcripts"
    result, invoke = review(
        project,
        monkeypatch,
        [*args, *extra, "--transcript-dir", str(transcripts)],
        {"issues": [finding(file=args[1], category=category)]},
    )
    assert result.exit_code == 0, result.report_text
    assert invoke.call_count == 1
    assert f"({guide})" in invoke.call_args.args[0].system_prompt
    assert invoke.call_args.args[0].transcript_path == transcripts / "reviewer.jsonl"
    assert "guide calls reported" not in result.report_text
    assert "guide_calls" not in result.detail
    (row,) = result.detail["issue_list"]
    assert row["guide_kinds"] == [("hunt" if guide == "spec-hunt" else "checklist")]
    assert not [r for r in result.detail["filtered"] + result.detail["rejected"] if "guide" in r]


def test_audit_rows_name_their_guide_call(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = finding(file="../outside.sv")
    result, _ = review_by_guide(
        project,
        monkeypatch,
        BASE_ARGS,
        {
            "bugs-checklist": {"issues": [outside]},
            "bugs-hunt": {"issues": [finding(), {"severity": "BOGUS"}]},
        },
    )
    attempts = {c["guides"][0]: c["attempt_id"] for c in result.detail["guide_calls"]}
    (filtered,) = result.detail["filtered"]
    assert (filtered["guide"], filtered["attempt_id"]) == (
        "bugs-checklist",
        attempts["bugs-checklist"],
    )
    (rejected,) = result.detail["rejected"]
    assert (rejected["guide"], rejected["attempt_id"]) == ("bugs-hunt", attempts["bugs-hunt"])


def test_editing_either_bugs_guide_stales_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number, guide in enumerate(BUGS_GUIDES):
        project = build_project(tmp_path / f"case{number}", monkeypatch)
        _assert_guide_edit_stales_receipt(project, monkeypatch, tmp_path / f"data{number}", guide)


def _assert_guide_edit_stales_receipt(
    project: Path, monkeypatch: pytest.MonkeyPatch, package_data: Path, guide: str
) -> None:
    import shutil

    from booley.runtime.paths import refs_dir
    from booley.specialists.verification_freshness import refresh_verification_freshness

    shutil.copytree(refs_dir() / "code_review", package_data / "refs/code_review")
    monkeypatch.setattr("booley.runtime.paths.package_data_dir", lambda: package_data)

    result, _ = review_by_guide(
        project,
        monkeypatch,
        BASE_ARGS,
        {"bugs-checklist": {"issues": []}, "bugs-hunt": {"issues": []}},
    )
    assert review_receipt_drift(result.detail, project) == []
    state = DevelopmentState.load(project / "state.json")
    assert state.is_met("review_rtl_bugs_done") is True

    with (package_data / "refs/code_review/rtl" / f"{guide}.md").open(
        "a", encoding="utf-8", newline="\n"
    ) as f:
        f.write("\nOne more rule.\n")
    assert review_receipt_drift(result.detail, project) == ["guides"]
    refresh_verification_freshness(state, work_dir=project)
    entry = state.criteria["review_rtl_bugs_done"]
    assert entry.met is False
    assert entry.detail["stale_review_dimensions"] == ["guides"]
