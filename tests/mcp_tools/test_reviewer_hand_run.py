"""Hand-run Reviewer: inline spec text, opt-in failing exit, firmware context (issue #3269).

Every test drives a real Interactive Reviewer (real ``reviewer-state``, no Goal recorder)
with the agent call mocked to return canned findings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

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
) -> dict[str, Any]:
    return {
        "severity": severity,
        "confidence": "HIGH",
        "category": category,
        "file": file,
        "line": 3,
        "summary": summary,
        "kind": "code_defect",
        "disposition": "current",
        "ticket_clause": "Reset clears output",
    }


def verdicts(*statuses: str) -> dict[str, Any]:
    """A verify answer; FIXED rows cite evidence so they are not demoted."""
    return {
        "findings": [
            {"index": index, "status": status, "evidence": "rtl/dut.sv:1 corrected"}
            for index, status in enumerate(statuses, 1)
        ]
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("rtl/dut.sv", "tb/tb.sv", "fw/start.c"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("// authored\n")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    return tmp_path


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
        {"issues": [finding("MAJOR"), finding("MINOR", summary="Style nit")]},
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
