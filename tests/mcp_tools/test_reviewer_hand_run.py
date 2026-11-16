"""Hand-run Reviewer: inline spec text, opt-in failing exit, unscoped-file findings (issues #3269, #4052).

Every test drives a real Interactive Reviewer (no review state, no Goal recorder)
with the agent call mocked to return canned findings.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from booley.evidence.review_receipt import review_receipt_drift
from booley.evidence.review_vocabulary import ORIGIN_PRE_EXISTING
from booley.mcp.base import McpToolResult
from booley.specialists.reviewer import ReviewerSpecialist

BASE_ARGS = ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"]
SECURITY_ARGS = ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "security"]
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


def build_project(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with RTL, testbench and firmware files ."""
    for name in ("rtl/dut.sv", "tb/tb.sv", "fw/start.c"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// authored\n")
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
    invoke = MagicMock(
        return_value=MagicMock(
            output=json.dumps(payload if payload is not None else {"issues": []}),
            captured_agent_capability_calls={},
        )
    )
    monkeypatch.setattr(reviewer, "_invoke_agent", invoke)
    return reviewer._run(), invoke


def open_findings(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """The corrective rows of a review detail."""
    return [
        row
        for row in detail["issue_list"]
        if row["disposition"] == "current" and row.get("origin") != ORIGIN_PRE_EXISTING
    ]


def prompt_of(invoke: MagicMock) -> str:
    return invoke.call_args.args[0].prompt


def test_spec_text_is_reviewed_and_reported(
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


def test_completed_review_exits_zero_without_fail_on(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = review(project, monkeypatch, BASE_ARGS, {"issues": [finding("MAJOR")]})
    assert result.exit_code == 0, result.report_text
    assert len(open_findings(result.detail)) == 1


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


@pytest.mark.parametrize("args", [BASE_ARGS, TB_ARGS], ids=["rtl", "tb"])
def test_unscoped_file_findings_are_filtered_as_source_scope(
    project: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    """A finding in a file outside the review scope is audit evidence, never open (#4052)."""
    category = "quality" if args is TB_ARGS else "bugs"
    in_scope = args[1]
    (project / "sw").mkdir()
    (project / "sw/main.c").write_text("// authored\n")
    unscoped = [project / "fw/start.c", project / "sw/main.c"]  # results use absolute paths
    result, _ = review(
        project,
        monkeypatch,
        [*args, "--fail-on", "major"],
        {
            "issues": [
                *(
                    finding("MAJOR", str(path), "Stack never initialised", category)
                    for path in unscoped
                ),
                finding("MINOR", in_scope, "Style nit", category),
            ]
        },
    )
    assert result.exit_code == 0, result.report_text
    assert [row["file"] for row in open_findings(result.detail)] == [in_scope]
    assert [row["file"] for row in result.detail["issue_list"]] == [in_scope]
    assert sorted((row["file"], row["reason"]) for row in result.detail["filtered"]) == sorted(
        (str(path), "source_scope")
        for path in unscoped
        for _ in range(2)  # one per call
    )
    assert "OUT OF SCOPE (firmware" not in result.report_text


def test_fw_scope_follows_scope_contract(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fw/`` has no scope special case: the ``.core`` roles and the scope contract decide."""
    (project / "design.core").write_text(
        "CAPI=2:\nname: ::fw:0\nfilesets:\n"
        "  rtl:\n    files: [rtl/dut.sv]\n"
        "  tb:\n    tags: [tb]\n    files: [tb/tb.sv]\n"
        "targets:\n  sim:\n    default_tool: verilator\n    filesets: [rtl, tb]\n",
        encoding="utf-8",
        newline="\n",
    )
    scope = "fw/start.c"
    result, invoke = review(project, monkeypatch, ["--scope", scope, *TB_ARGS[2:]])
    assert result.exit_code == 0, result.report_text
    assert f"  - {scope}" in prompt_of(invoke)
    assert "firmware" not in result.report_text.lower()

    result, invoke = review(project, monkeypatch, ["--scope", scope, *BASE_ARGS[2:]])
    assert result.exit_code == 2
    assert "Unsupported RTL source kind" in result.report_text
    assert "firmware" not in result.report_text.lower()
    invoke.assert_not_called()


# --- Two guide calls: rtl/bugs runs a checklist call and a hunt call (issue #4016) ---

BUGS_GUIDES = ("bugs-checklist", "bugs-hunt")


def guide_of(params: Any, guides: tuple[str, ...] = BUGS_GUIDES, focus: str = "bugs") -> str:
    """The one guide of the focus that a call's system prompt carries."""
    carried = [g for g in guides if f"## Review guide — {focus} ({g})" in params.system_prompt]
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
    guides: tuple[str, ...] = BUGS_GUIDES,
    focus: str = "bugs",
    route: Callable[[Any], str] | None = None,
) -> tuple[McpToolResult, list[Any]]:
    """Run a review (bugs unless told otherwise) whose agent answers per guide; returns the result and every call.
    *route* names a call's key when the guides alone cannot tell the calls apart.

    An answer is a payload dict, a ready agent result, or an exception to raise. A list
    of answers is consumed one per call of that guide, the last one repeating.
    """
    reviewer = reviewer or ReviewerSpecialist()
    reviewer.parse_args(["--work-dir", str(root), *args])
    calls: list[Any] = []

    def fake_agent(params: Any) -> Any:
        calls.append(params)
        answer = answers[route(params) if route else guide_of(params, guides, focus)]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, MagicMock) else agent_result(answer)

    monkeypatch.setattr(reviewer, "_invoke_agent", fake_agent)
    return reviewer._run(), calls


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


def test_protocol_review_runs_checklist_and_hunt_calls(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A protocol review makes a checklist call and a hunt call, each with its own guide."""
    guides = ("protocol-cdc-checklist", "protocol-cdc-hunt")
    result, calls = review_by_guide(
        project,
        monkeypatch,
        [*BASE_ARGS[:-1], "protocol"],
        {
            "protocol-cdc-checklist": {
                "issues": [finding("MAJOR", summary="Checklist finding", category="protocol")]
            },
            "protocol-cdc-hunt": {
                "issues": [finding("MAJOR", summary="Hunt finding", line=7, category="protocol")]
            },
        },
        guides=guides,
        focus="protocol",
    )

    assert result.exit_code == 0, result.report_text
    assert len(calls) == 2
    for call in calls:
        carried = [g for g in guides if f"({g})" in call.system_prompt]
        assert len(carried) == 1
    summaries = {row["summary"]: row for row in result.detail["issue_list"]}
    assert summaries["Checklist finding"]["guide_kinds"] == ["checklist"]
    assert summaries["Hunt finding"]["guide_kinds"] == ["hunt"]
    assert [(c["kind"], c["guides"]) for c in result.detail["guide_calls"]] == [
        ("checklist", ["protocol-cdc-checklist"]),
        ("hunt", ["protocol-cdc-hunt"]),
    ]


def test_security_review_runs_checklist_and_hunt_calls(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A security review makes one call per guide, each prompt carrying only its own guide."""
    guides = ("security-checklist", "security-hunt")
    prompts: dict[str, str] = {}
    answers = {
        "security-checklist": {
            "issues": [finding("MAJOR", summary="Key stays after wipe", category="security")]
        },
        "security-hunt": {
            "issues": [finding("MINOR", summary="Debug read-back", category="security", line=7)]
        },
    }

    def fake_agent(params: Any) -> Any:
        carried = [
            g for g in guides if f"## Review guide — security ({g})" in params.system_prompt
        ]
        assert len(carried) == 1, carried
        prompts[carried[0]] = params.system_prompt
        return agent_result(answers[carried[0]])

    reviewer = ReviewerSpecialist()
    reviewer.parse_args(["--work-dir", str(project), *SECURITY_ARGS])
    monkeypatch.setattr(reviewer, "_invoke_agent", fake_agent)
    result = reviewer._run()

    assert result.exit_code == 0, result.report_text
    assert set(prompts) == set(guides)
    summaries = {row["summary"]: row for row in result.detail["issue_list"]}
    assert set(summaries) == {"Key stays after wipe", "Debug read-back"}
    assert summaries["Key stays after wipe"]["guide_kinds"] == ["checklist"]
    assert summaries["Debug read-back"]["guide_kinds"] == ["hunt"]
    calls = result.detail["guide_calls"]
    assert [(c["kind"], c["guides"], c["status"]) for c in calls] == [
        ("checklist", ["security-checklist"], "ok"),
        ("hunt", ["security-hunt"], "ok"),
    ]


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


def test_merge_only_joins_findings_of_different_calls_and_counts_real_merges(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two findings of one call at one place stay two; an out-of-scope one is no merge."""
    first = finding("MAJOR", summary="First defect here")
    second = finding("MINOR", summary="Second defect here")
    second["confidence"] = "LOW"
    outside = finding("MAJOR", summary="Outside the scope", file="rtl/elsewhere.sv")
    hunt = finding("MAJOR", summary="Hunt defect", line=9)
    result, _ = review_by_guide(
        project,
        monkeypatch,
        BASE_ARGS,
        {"bugs-checklist": {"issues": [first, second, outside]}, "bugs-hunt": {"issues": [hunt]}},
    )
    summaries = {row["summary"] for row in result.detail["issue_list"]}
    assert summaries == {"First defect here", "Second defect here", "Hunt defect"}
    assert "merged 0 exact duplicate" in result.report_text


def test_merged_finding_is_represented_by_highest_confidence_on_a_severity_tie(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    low = finding("MAJOR", summary="Less certain wording")
    low["confidence"] = "LOW"
    high = finding("MAJOR", summary="More certain wording")
    result, _ = review_by_guide(
        project,
        monkeypatch,
        BASE_ARGS,
        {"bugs-checklist": {"issues": [low]}, "bugs-hunt": {"issues": [high]}},
    )
    (row,) = result.detail["issue_list"]
    assert row["summary"] == "More certain wording"


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
    assert len(open_findings(result.detail)) == 1


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


def broken_answers() -> dict[str, Any]:
    """Each way a guide call can fail, as a scripted agent answer."""
    return {
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


def test_any_failing_guide_call_is_a_specialist_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Either call failing in any way makes the review an error."""
    failures = ("raises", "no_json", "all_rejected", "mirror_disagrees")
    for number, (failing, failure) in enumerate((g, f) for g in BUGS_GUIDES for f in failures):
        project = build_project(tmp_path / f"case{number}", monkeypatch)
        _assert_failing_guide_is_error(project, monkeypatch, failing, failure)


def _assert_failing_guide_is_error(
    project: Path, monkeypatch: pytest.MonkeyPatch, failing: str, failure: str
) -> None:
    broken = broken_answers()
    healthy = {"issues": [finding("MAJOR", summary="Healthy call finding", line=7)]}
    answers = {guide: (broken[failure] if guide == failing else healthy) for guide in BUGS_GUIDES}
    transcripts = project / "transcripts"
    result, calls = review_by_guide(
        project, monkeypatch, [*BASE_ARGS, "--transcript-dir", str(transcripts)], answers
    )

    # The healthy call is not cancelled; the failing call is retried once and fails again.
    assert [guide_of(c) for c in calls].count(failing) == 2
    assert len(calls) == 3
    assert result.exit_code == 2, result.report_text
    assert f"ERROR: {failing} call failed" in result.report_text
    assert f"INFO: {failing} call failed; retrying it once" in result.report_text
    other = next(guide for guide in BUGS_GUIDES if guide != failing)
    assert f"ERROR: {other} call failed" not in result.report_text
    assert "issue_list" not in result.detail
    assert "pending" not in result.detail
    # The healthy call's finding is published nowhere: not in the report or detail.
    assert "Healthy call finding" not in result.report_text
    assert "Healthy call finding" not in json.dumps(result.detail, default=str)
    assert result.detail["review_error"] is True
    status = {c["guides"][0]: c["status"] for c in result.detail["guide_calls"]}
    assert status == {failing: "error", other: "ok"}
    failing_call = next(c for c in result.detail["guide_calls"] if c["guides"] == [failing])
    assert Path(failing_call["transcript"]).name == f"reviewer-{failing}.jsonl"
    (first_try,) = failing_call["failed_attempts"]
    assert first_try["status"] == "error"
    assert first_try["attempt_id"] != failing_call["attempt_id"]


@pytest.mark.parametrize("failure", ["raises", "no_json", "all_rejected", "mirror_disagrees"])
@pytest.mark.parametrize("failing", BUGS_GUIDES)
def test_failed_guide_call_is_retried_once_and_the_review_succeeds(
    project: Path, monkeypatch: pytest.MonkeyPatch, failing: str, failure: str
) -> None:
    """A first attempt that fails (here the ReportFindings mirror check) is retried once."""
    healthy_other = finding("MAJOR", summary="Other call finding", line=7)
    retried = finding("MAJOR", summary="Retried call finding", line=9)
    other = next(guide for guide in BUGS_GUIDES if guide != failing)
    result, calls = review_by_guide(
        project,
        monkeypatch,
        BASE_ARGS,
        {
            failing: [broken_answers()[failure], {"issues": [retried]}],
            other: {"issues": [healthy_other]},
        },
    )

    assert result.exit_code == 0, result.report_text
    assert [guide_of(c) for c in calls].count(failing) == 2
    assert [guide_of(c) for c in calls].count(other) == 1
    assert f"INFO: {failing} call failed; retrying it once" in result.report_text
    assert "the review is incomplete" not in result.report_text
    summaries = {row["summary"] for row in open_findings(result.detail)}
    assert summaries == {"Other call finding", "Retried call finding"}
    rows = {c["guides"][0]: c for c in result.detail["guide_calls"]}
    assert {c["status"] for c in rows.values()} == {"ok"}
    (first_try,) = rows[failing]["failed_attempts"]
    assert first_try["status"] == "error"
    assert first_try["attempt_id"] != rows[failing]["attempt_id"]
    assert "failed_attempts" not in rows[other]


def test_single_call_review_is_not_retried(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, invoke = review(
        project, monkeypatch, [*BASE_ARGS[:-1], "code_style"], {"issues": [{"severity": "BOGUS"}]}
    )
    assert result.exit_code == 2, result.report_text
    assert invoke.call_count == 1
    assert "retrying" not in result.report_text


def test_single_guide_focus_makes_one_call_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec and code style reviews still make one call and one transcript."""
    cases = [
        (SPEC_ARGS, "spec", "spec-hunt"),
        ([*BASE_ARGS[:-1], "code_style"], "code_style", "code_style-checklist"),
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

    shutil.copytree(refs_dir() / "code_review", package_data / "refs/code_review")
    monkeypatch.setattr("booley.runtime.paths.package_data_dir", lambda: package_data)

    result, _ = review_by_guide(
        project,
        monkeypatch,
        BASE_ARGS,
        {"bugs-checklist": {"issues": []}, "bugs-hunt": {"issues": []}},
    )
    assert review_receipt_drift(result.detail, project) == []

    with (package_data / "refs/code_review/rtl" / f"{guide}.md").open(
        "a", encoding="utf-8", newline="\n"
    ) as f:
        f.write("\nOne more rule.\n")
    assert review_receipt_drift(result.detail, project) == ["guides"]


# --- TB quality: language checklists and one hunt call (issue #4021) ---

TB_GUIDES = ("c-cpp-checklist", "hdl-checklist", "cocotb-checklist", "tb-hunt")
TB_LANGUAGE_SCOPES = {
    "hdl": ("tb/tb.sv", ["hdl-checklist"]),
    "cocotb": ("tb/test.py", ["cocotb-checklist"]),
    "cpp": ("tb/main.cpp", ["c-cpp-checklist"]),
    "all": (
        "tb/main.cpp,tb/test.py,tb/tb.sv",
        ["c-cpp-checklist", "hdl-checklist", "cocotb-checklist"],
    ),
}
TB_STYLE_MARKER = "Project style rule 4021"


def tb_scope_args(scope: str) -> list[str]:
    return ["--scope", scope, *TB_ARGS[2:]]


def tb_route(params: Any) -> str:
    """``tb-hunt`` or ``checklist`` for a call; a checklist call may carry several guides."""
    hunt = "## Review guide — quality (tb-hunt)" in params.system_prompt
    others = [g for g in TB_GUIDES[:3] if f"({g})" in params.system_prompt]
    assert bool(others) != hunt, (hunt, others)
    return "tb-hunt" if hunt else "checklist"


def tb_project(root: Path) -> Path:
    for name in ("tb/test.py", "tb/main.cpp"):
        (root / name).write_text("// authored\n", encoding="utf-8", newline="\n")
    overlay = root / ".booley" / "tb_style_guide.md"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(TB_STYLE_MARKER, encoding="utf-8", newline="\n")
    return root


@pytest.mark.parametrize("language", list(TB_LANGUAGE_SCOPES))
def test_tb_quality_runs_language_checklists_and_one_hunt_call(
    project: Path, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    """The scope's language checklists share one call; a hunt call runs beside it."""
    scope, checklists = TB_LANGUAGE_SCOPES[language]
    tb_project(project)
    result, calls = review_by_guide(
        project,
        monkeypatch,
        tb_scope_args(scope),
        {
            "checklist": {
                "issues": [finding("MAJOR", scope.split(",")[0], "Checklist finding", "quality")]
            },
            "tb-hunt": {
                "issues": [
                    finding("MAJOR", scope.split(",")[0], "Hunt finding", "quality", line=9)
                ]
            },
        },
        route=tb_route,
    )
    assert result.exit_code == 0, result.report_text
    by_route = {tb_route(call): call.system_prompt for call in calls}
    assert sorted(by_route) == ["checklist", "tb-hunt"]
    headings = re.findall(r"^## Review guide — .*$", by_route["checklist"], flags=re.MULTILINE)
    assert headings == [f"## Review guide — quality ({name})" for name in checklists]
    hunt_headings = re.findall(r"^## Review guide — .*$", by_route["tb-hunt"], flags=re.MULTILINE)
    assert hunt_headings == ["## Review guide — quality (tb-hunt)"]
    # Style rules stay with the checklist call; the hunt call carries none of them.
    assert TB_STYLE_MARKER in by_route["checklist"]
    assert TB_STYLE_MARKER not in by_route["tb-hunt"]
    assert "Testbench style guide" not in by_route["tb-hunt"]
    packaged = "Canonical style guide for SystemVerilog" in by_route["checklist"]
    assert packaged == (language != "cpp")
    assert "Canonical style guide for SystemVerilog" not in by_route["tb-hunt"]
    kinds = {row["summary"]: row["guide_kinds"] for row in result.detail["issue_list"]}
    assert kinds == {"Checklist finding": ["checklist"], "Hunt finding": ["hunt"]}
    assert [(c["kind"], c["guides"]) for c in result.detail["guide_calls"]] == [
        ("checklist", checklists),
        ("hunt", ["tb-hunt"]),
    ]


def test_tb_hunt_call_gets_the_checklist_calls_inputs(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both calls share one user prompt: spec, simulation context and project contract."""
    (project / ".booley").mkdir()
    (project / ".booley" / "booley.toml").write_text(
        "[flows.sim]\n"
        'pass_sentinels = ["ALL TESTS OK"]\n'
        'fail_sentinels = ["TESTS BROKEN"]\n'
        'trace_files = ["waves.vcd"]\n',
        encoding="utf-8",
        newline="\n",
    )
    result, calls = review_by_guide(
        project,
        monkeypatch,
        [*TB_ARGS, "--spec-text", "Reset drives q low."],
        {"checklist": {"issues": []}, "tb-hunt": {"issues": []}},
        route=tb_route,
    )
    assert result.exit_code == 0, result.report_text
    prompts = {tb_route(call): call.prompt for call in calls}
    assert sorted(prompts) == ["checklist", "tb-hunt"]
    assert prompts["tb-hunt"] == prompts["checklist"]
    for heading in ("## Specification", "## Simulation Context", "## Project Simulation Contract"):
        assert heading in prompts["tb-hunt"]
    assert "Reset drives q low." in prompts["tb-hunt"]
    for configured in ("ALL TESTS OK", "TESTS BROKEN", "waves.vcd"):
        assert configured in prompts["tb-hunt"]


def test_tb_hunt_guide_defers_to_the_project_simulation_contract(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hunt guide yields its sentinel and trace defaults to the project's configuration."""
    (project / ".booley").mkdir()
    (project / ".booley" / "booley.toml").write_text(
        '[flows.sim]\npass_sentinels = ["ALL TESTS OK"]\nfail_sentinels = ["TESTS BROKEN"]\n'
        'trace_files = ["waves.vcd"]\n',
        encoding="utf-8",
        newline="\n",
    )
    demand = finding("MAJOR", "tb/tb.sv", "Testbench never prints [SIM_RESULT]", "quality")
    result, calls = review_by_guide(
        project,
        monkeypatch,
        TB_ARGS,
        {"checklist": {"issues": []}, "tb-hunt": {"issues": [demand]}},
        route=tb_route,
    )
    hunts = [call for call in calls if tb_route(call) == "tb-hunt"]
    assert len(hunts) == 1, "expected one tb-hunt call"
    (hunt,) = hunts
    guide = hunt.system_prompt.split("## Review guide — quality (tb-hunt)")[1]
    contract = [line for line in guide.splitlines() if "Project Simulation Contract" in line]
    assert any("configured pass and fail sentinels" in line for line in contract)
    assert any("replace this guide's defaults" in line for line in contract)
    assert "never a finding" in guide
    assert "ALL TESTS OK" in hunt.prompt
    assert "waves.vcd" in hunt.prompt
    # What the hunt reports about the contract stays a model judgment: nothing is filtered.
    assert [row["summary"] for row in result.detail["issue_list"]] == [demand["summary"]]
    assert result.detail["filtered"] == []


def test_optimization_review_runs_checklist_and_hunt_calls(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An optimization review makes a checklist call and a hunt call and merges the union."""
    args = ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "optimization"]
    same = finding("MINOR", summary="Counter is wider than its range", category="optimization")
    result, invoke = review(project, monkeypatch, args, {"issues": [same]})

    assert result.exit_code == 0, result.report_text
    assert invoke.call_count == 2
    systems = [call.args[0].system_prompt for call in invoke.call_args_list]
    for guide in ("optimization-checklist", "optimization-hunt"):
        carrying = [s for s in systems if f"## Review guide \u2014 optimization ({guide})" in s]
        assert len(carrying) == 1, guide
    assert [c["kind"] for c in result.detail["guide_calls"]] == ["checklist", "hunt"]
    issues = result.detail["issue_list"]
    assert len(issues) == 1
    assert issues[0]["guide_kinds"] == ["checklist", "hunt"]
