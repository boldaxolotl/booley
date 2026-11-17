"""Hand-run Reviewer: inline spec text, opt-in failing exit, unscoped-file findings (issues #3269, #4052).

Every test drives a real Interactive Reviewer (no review state, no Goal recorder)
through its public run, with the model replaced by the shared scripted agent.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from booley.core.models import AgentCallParams, AgentResult
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.mcp_tools.reviewer_support import (
    ScriptedAgent,
    guide_of,
    install_agent,
    issue,
    open_findings,
    run_review,
)

BASE_ARGS = ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"]
SPEC_ARGS = ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "spec"]
TB_ARGS = ["--scope", "tb/tb.sv", "--category", "tb", "--focus", "quality"]
BUGS_GUIDES = ("bugs-checklist", "bugs-hunt")


def rtl_args(focus: str) -> list[str]:
    """Arguments of an rtl review of ``rtl/dut.sv`` for *focus*."""
    return ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", focus]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with RTL, testbench and firmware files."""
    for name in ("rtl/dut.sv", "tb/tb.sv", "fw/start.c"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// authored\n", encoding="utf-8", newline="\n")
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    return tmp_path


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> ScriptedAgent:
    return install_agent(monkeypatch)


def review(project: Path, args: list[str]) -> EndpointOutcome:
    """Run one hand-run Reviewer invocation against *project*."""
    result, _ = run_review(["--work-dir", str(project), *args])
    return result


def test_spec_text_is_reviewed_and_reported(project: Path, agent: ScriptedAgent) -> None:
    text = "Reset drives q low.\nEnable is active high."
    agent.answer({"issues": [issue(category="spec")]})
    result = review(project, [*SPEC_ARGS, "--spec-text", text])
    assert result.exit_code == 0, result.report_text
    assert f"## Specification (source: inline text)\n\n{text}" in agent.calls[0].prompt
    assert f"Spec (inline text):\n{text}" in result.report_text


def test_spec_and_spec_text_together_is_a_usage_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_review(["--work-dir", str(project), *SPEC_ARGS, "--spec", "s.md", "--spec-text", "x"])
    assert exit_info.value.code == 2
    assert "--spec-text: not allowed with argument --spec" in capsys.readouterr().err


def test_empty_spec_text_is_an_error(project: Path, agent: ScriptedAgent) -> None:
    result = review(project, [*SPEC_ARGS, "--spec-text", "  \n"])
    assert result.exit_code == 2
    assert "--spec-text must not be empty" in result.report_text
    assert agent.calls == []


def test_missing_spec_file_fails_while_same_words_as_spec_text_review(
    project: Path, agent: ScriptedAgent
) -> None:
    result = review(project, [*SPEC_ARGS, "--spec", "missing.md"])
    assert result.exit_code == 2
    assert str(project / "missing.md") in result.report_text
    assert agent.calls == []

    result = review(project, [*SPEC_ARGS, "--spec-text", "missing.md"])
    assert result.exit_code == 0, result.report_text
    assert "missing.md" in agent.calls[0].prompt


def test_completed_review_exits_zero_without_fail_on(project: Path, agent: ScriptedAgent) -> None:
    agent.answer({"issues": [issue()]})
    result = review(project, BASE_ARGS)
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
    project: Path, agent: ScriptedAgent, fail_on: str, severity: str, expected: int
) -> None:
    agent.answer({"issues": [issue(severity=severity)]})
    result = review(project, [*BASE_ARGS, "--fail-on", fail_on])
    assert result.exit_code == expected, result.report_text


@pytest.mark.parametrize("args", [BASE_ARGS, TB_ARGS], ids=["rtl", "tb"])
def test_unscoped_file_findings_are_filtered_as_source_scope(
    project: Path, agent: ScriptedAgent, args: list[str]
) -> None:
    """A finding in a file outside the review scope is audit evidence, never open (#4052)."""
    category = "quality" if args is TB_ARGS else "bugs"
    in_scope = args[1]
    (project / "sw").mkdir()
    (project / "sw/main.c").write_text("// authored\n", encoding="utf-8", newline="\n")
    unscoped = [project / "fw/start.c", project / "sw/main.c"]  # results use absolute paths
    agent.answer(
        {
            "issues": [
                *(
                    issue(file=str(path), summary="Stack never initialised", category=category)
                    for path in unscoped
                ),
                issue(severity="MINOR", file=in_scope, summary="Style nit", category=category),
            ]
        }
    )
    result = review(project, [*args, "--fail-on", "major"])
    assert result.exit_code == 0, result.report_text
    assert [row["file"] for row in open_findings(result.detail)] == [in_scope]
    assert [row["file"] for row in result.detail["issue_list"]] == [in_scope]
    assert sorted((row["file"], row["reason"]) for row in result.detail["filtered"]) == sorted(
        (str(path), "source_scope")
        for path in unscoped
        for _ in range(2)  # one per call
    )
    assert "OUT OF SCOPE (firmware" not in result.report_text
    assert "outside explicit source scope" in result.report_text


def test_fw_scope_follows_scope_contract(project: Path, agent: ScriptedAgent) -> None:
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
    result = review(project, ["--scope", scope, *TB_ARGS[2:]])
    assert result.exit_code == 0, result.report_text
    assert f"  - {scope}" in agent.calls[0].prompt
    assert "firmware" not in result.report_text.lower()

    agent.calls.clear()
    result = review(project, ["--scope", scope, *BASE_ARGS[2:]])
    assert result.exit_code == 2
    assert "Unsupported RTL source kind" in result.report_text
    assert "firmware" not in result.report_text.lower()
    assert agent.calls == []


# --- Two guide calls: rtl/bugs runs a checklist call and a hunt call (issue #4016) ---


def test_bugs_review_runs_checklist_and_hunt_concurrently(
    project: Path, agent: ScriptedAgent
) -> None:
    """Both guide calls run at once and their usage sums."""
    barrier = threading.Barrier(2, timeout=20)  # sequential calls would break it

    def meet(_params: AgentCallParams) -> None:
        barrier.wait()

    agent.on_call = meet
    agent.answer(
        {
            "bugs-checklist": AgentResult(
                output=json.dumps({"issues": [issue(summary="Checklist finding")]}),
                cost_usd=0.5,
                input_tokens=10,
                session_id="session-bugs-checklist",
            ),
            "bugs-hunt": AgentResult(
                output=json.dumps(
                    {"issues": [issue(severity="MINOR", summary="Hunt finding", line=7)]}
                ),
                cost_usd=0.25,
                input_tokens=20,
                session_id="session-bugs-hunt",
            ),
        }
    )
    transcripts = project / "transcripts"
    result, reviewer = run_review(
        ["--work-dir", str(project), *BASE_ARGS, "--transcript-dir", str(transcripts)]
    )

    assert result.exit_code == 0, result.report_text
    # Each call's prompt carries only its own guide.
    assert sorted(guide_of(call) for call in agent.calls) == list(BUGS_GUIDES)
    summaries = {row["summary"]: row for row in result.detail["issue_list"]}
    assert set(summaries) == {"Checklist finding", "Hunt finding"}
    assert summaries["Checklist finding"]["guide_kinds"] == ["checklist"]
    assert summaries["Hunt finding"]["guide_kinds"] == ["hunt"]
    # Private seam: usage is stamped onto the outcome only by the session publication
    # path, which the public ``prepare_endpoint_invocation().run`` does not take.
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


@pytest.mark.parametrize(
    ("focus", "guides", "answers", "kinds"),
    [
        pytest.param(
            "protocol",
            ("protocol-cdc-checklist", "protocol-cdc-hunt"),
            {
                "protocol-cdc-checklist": {
                    "issues": [issue(summary="Checklist finding", category="protocol")]
                },
                "protocol-cdc-hunt": {
                    "issues": [issue(summary="Hunt finding", line=7, category="protocol")]
                },
            },
            {"Checklist finding": ["checklist"], "Hunt finding": ["hunt"]},
            id="protocol",
        ),
        pytest.param(
            "security",
            ("security-checklist", "security-hunt"),
            {
                "security-checklist": {
                    "issues": [issue(summary="Key stays after wipe", category="security")]
                },
                "security-hunt": {
                    "issues": [
                        issue(
                            severity="MINOR",
                            summary="Debug read-back",
                            category="security",
                            line=7,
                        )
                    ]
                },
            },
            {"Key stays after wipe": ["checklist"], "Debug read-back": ["hunt"]},
            id="security",
        ),
        pytest.param(
            "optimization",
            ("optimization-checklist", "optimization-hunt"),
            {  # one shared answer: both calls report it and the union merges
                "*": {
                    "issues": [
                        issue(
                            severity="MINOR",
                            summary="Counter is wider than its range",
                            category="optimization",
                        )
                    ]
                }
            },
            {"Counter is wider than its range": ["checklist", "hunt"]},
            id="optimization",
        ),
    ],
)
def test_review_runs_checklist_and_hunt_calls(
    project: Path,
    agent: ScriptedAgent,
    focus: str,
    guides: tuple[str, str],
    answers: dict[str, Any],
    kinds: dict[str, list[str]],
) -> None:
    """A review makes a checklist call and a hunt call, each prompt carrying only its guide."""
    agent.answer(answers)
    result = review(project, rtl_args(focus))

    assert result.exit_code == 0, result.report_text
    assert sorted(guide_of(call) for call in agent.calls) == sorted(guides)
    assert {row["summary"]: row["guide_kinds"] for row in result.detail["issue_list"]} == kinds
    assert [(c["kind"], c["guides"], c["status"]) for c in result.detail["guide_calls"]] == [
        ("checklist", [guides[0]], "ok"),
        ("hunt", [guides[1]], "ok"),
    ]


def test_exact_duplicate_findings_merge_with_highest_severity_and_confidence(
    project: Path, agent: ScriptedAgent
) -> None:
    checklist = issue(severity="MAJOR", confidence="MEDIUM", summary="Reset value is wrong")
    hunt = issue(
        severity="CRITICAL", confidence="LOW", summary="Reset leaves q stale", file="./rtl/dut.sv"
    )
    agent.answer({"bugs-checklist": {"issues": [checklist]}, "bugs-hunt": {"issues": [hunt]}})
    result = review(project, BASE_ARGS)
    assert len(agent.calls) == 2
    (row,) = result.detail["issue_list"]
    assert (row["severity"], row["confidence"]) == ("CRITICAL", "MEDIUM")
    assert row["summary"] == "Reset leaves q stale"  # the highest-severity member represents it
    assert row["guide_kinds"] == ["checklist", "hunt"]
    assert "merged 1 exact duplicate" in result.report_text


def test_merge_only_joins_findings_of_different_calls_and_counts_real_merges(
    project: Path, agent: ScriptedAgent
) -> None:
    """Two findings of one call at one place stay two; an out-of-scope one is no merge."""
    first = issue(summary="First defect here")
    second = issue(severity="MINOR", confidence="LOW", summary="Second defect here")
    outside = issue(summary="Outside the scope", file="rtl/elsewhere.sv")
    hunt = issue(summary="Hunt defect", line=9)
    agent.answer(
        {"bugs-checklist": {"issues": [first, second, outside]}, "bugs-hunt": {"issues": [hunt]}}
    )
    result = review(project, BASE_ARGS)
    summaries = {row["summary"] for row in result.detail["issue_list"]}
    assert summaries == {"First defect here", "Second defect here", "Hunt defect"}
    assert "merged 0 exact duplicate" in result.report_text


def test_merged_finding_is_represented_by_highest_confidence_on_a_severity_tie(
    project: Path, agent: ScriptedAgent
) -> None:
    low = issue(confidence="LOW", summary="Less certain wording")
    high = issue(summary="More certain wording")
    agent.answer({"bugs-checklist": {"issues": [low]}, "bugs-hunt": {"issues": [high]}})
    result = review(project, BASE_ARGS)
    (row,) = result.detail["issue_list"]
    assert row["summary"] == "More certain wording"


@pytest.mark.parametrize(
    "changed", [{"line": 4}, {"kind": "proof_gap"}, {"file": "rtl/other.sv"}], ids=str
)
def test_near_duplicate_findings_stay_separate(
    project: Path, agent: ScriptedAgent, changed: dict[str, Any]
) -> None:
    """A different line, kind or file is never an exact duplicate."""
    (project / "rtl/other.sv").write_text("// authored\n", encoding="utf-8", newline="\n")
    other = issue(summary="Same problem, elsewhere", **changed)
    agent.answer({"bugs-checklist": {"issues": [issue()]}, "bugs-hunt": {"issues": [other]}})
    result = review(project, [*BASE_ARGS[:1], "rtl/dut.sv,rtl/other.sv", *BASE_ARGS[2:]])
    assert len(result.detail["issue_list"]) == 2
    assert "merged 0 exact duplicate" in result.report_text


def test_duplicates_never_merge_across_open_finding_and_observation(
    project: Path, agent: ScriptedAgent
) -> None:
    """An open finding is never hidden inside an observation."""
    advisory = issue(summary="Seen by the hunt", disposition="advisory")
    agent.answer({"bugs-checklist": {"issues": [issue()]}, "bugs-hunt": {"issues": [advisory]}})
    result = review(project, BASE_ARGS)
    rows = result.detail["issue_list"]
    assert sorted(row["status"] for row in rows) == ["advisory", "current"]
    assert len(open_findings(result.detail)) == 1


def test_findings_record_guide_kinds_and_gating_ignores_them(
    project: Path, agent: ScriptedAgent
) -> None:
    """The guide kind tags a row but is no part of its identity or its gating."""
    finding = issue(summary="Only the hunt saw this", line=9)
    args = [*BASE_ARGS, "--fail-on", "major"]
    agent.answer({"bugs-checklist": {"issues": []}, "bugs-hunt": {"issues": [finding]}})
    by_hunt = review(project, args)
    assert by_hunt.exit_code == 1, by_hunt.report_text  # a hunt-only finding gates as any other
    (hunt_row,) = by_hunt.detail["issue_list"]
    assert hunt_row["guide_kinds"] == ["hunt"]

    agent.answer({"bugs-checklist": {"issues": [finding]}, "bugs-hunt": {"issues": []}})
    by_checklist = review(project, args)
    (checklist_row,) = by_checklist.detail["issue_list"]
    assert checklist_row["guide_kinds"] == ["checklist"]
    assert checklist_row["finding_id"] == hunt_row["finding_id"]


def broken_answers(agent: ScriptedAgent) -> dict[str, Any]:
    """Each way a guide call can fail, as a scripted agent answer."""
    return {
        "raises": RuntimeError("boom"),
        "no_json": "no JSON here",
        "all_rejected": {"issues": [{"severity": "BOGUS"}]},
        "mirror_disagrees": agent.reported(
            {"findings": [{"file": "rtl/dut.sv", "line": 3, "summary": "x"}]},
            output=json.dumps({"issues": []}),
        ),
    }


FAILURES = ("raises", "no_json", "all_rejected", "mirror_disagrees")


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("failing", BUGS_GUIDES)
def test_any_failing_guide_call_is_a_specialist_error(
    project: Path, agent: ScriptedAgent, failing: str, failure: str
) -> None:
    """Either call failing in any way makes the review an error."""
    healthy = {"issues": [issue(summary="Healthy call finding", line=7)]}
    agent.answer(
        {
            guide: (broken_answers(agent)[failure] if guide == failing else healthy)
            for guide in BUGS_GUIDES
        }
    )
    transcripts = project / "transcripts"
    result = review(project, [*BASE_ARGS, "--transcript-dir", str(transcripts)])

    # The healthy call is not cancelled; the failing call is retried once and fails again.
    assert [guide_of(c) for c in agent.calls].count(failing) == 2
    assert len(agent.calls) == 3
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


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("failing", BUGS_GUIDES)
def test_failed_guide_call_is_retried_once_and_the_review_succeeds(
    project: Path, agent: ScriptedAgent, failing: str, failure: str
) -> None:
    """A first attempt that fails (here the ReportFindings mirror check) is retried once."""
    healthy_other = issue(summary="Other call finding", line=7)
    retried = issue(summary="Retried call finding", line=9)
    other = next(guide for guide in BUGS_GUIDES if guide != failing)
    agent.answer(
        {
            failing: [broken_answers(agent)[failure], {"issues": [retried]}],
            other: {"issues": [healthy_other]},
        }
    )
    result = review(project, BASE_ARGS)

    assert result.exit_code == 0, result.report_text
    assert [guide_of(c) for c in agent.calls].count(failing) == 2
    assert [guide_of(c) for c in agent.calls].count(other) == 1
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


def test_single_call_review_is_not_retried(project: Path, agent: ScriptedAgent) -> None:
    agent.answer({"issues": [{"severity": "BOGUS"}]})
    result = review(project, rtl_args("code_style"))
    assert result.exit_code == 2, result.report_text
    assert len(agent.calls) == 1
    assert "retrying" not in result.report_text


@pytest.mark.parametrize(
    ("args", "category", "guide"),
    [
        (SPEC_ARGS, "spec", "spec-hunt"),
        (rtl_args("code_style"), "code_style", "code_style-checklist"),
    ],
    ids=["spec", "code_style"],
)
def test_single_guide_focus_makes_one_call_as_before(
    project: Path, agent: ScriptedAgent, args: list[str], category: str, guide: str
) -> None:
    """Spec and code style reviews still make one call and one transcript."""
    extra = ["--spec-text", "Reset drives q low."] if category == "spec" else []
    transcripts = project / "transcripts"
    agent.answer({"issues": [issue(file=args[1], category=category)]})
    result = review(project, [*args, *extra, "--transcript-dir", str(transcripts)])
    assert result.exit_code == 0, result.report_text
    (call,) = agent.calls
    assert guide_of(call) == guide
    assert call.transcript_path == transcripts / "reviewer.jsonl"
    assert "guide calls reported" not in result.report_text
    assert "guide_calls" not in result.detail
    (row,) = result.detail["issue_list"]
    assert row["guide_kinds"] == [("hunt" if guide == "spec-hunt" else "checklist")]
    assert not [r for r in result.detail["filtered"] + result.detail["rejected"] if "guide" in r]


def test_audit_rows_name_their_guide_call(project: Path, agent: ScriptedAgent) -> None:
    agent.answer(
        {
            "bugs-checklist": {"issues": [issue(file="../outside.sv")]},
            "bugs-hunt": {"issues": [issue(), {"severity": "BOGUS"}]},
        }
    )
    result = review(project, BASE_ARGS)
    attempts = {c["guides"][0]: c["attempt_id"] for c in result.detail["guide_calls"]}
    (filtered,) = result.detail["filtered"]
    assert (filtered["guide"], filtered["attempt_id"]) == (
        "bugs-checklist",
        attempts["bugs-checklist"],
    )
    (rejected,) = result.detail["rejected"]
    assert (rejected["guide"], rejected["attempt_id"]) == ("bugs-hunt", attempts["bugs-hunt"])


# --- TB quality: language checklists and one hunt call (issue #4021) ---

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
_HEADING = re.compile(r"^## Review guide — .*$", flags=re.MULTILINE)


def tb_scope_args(scope: str) -> list[str]:
    return ["--scope", scope, *TB_ARGS[2:]]


def tb_route(params: Any) -> str:
    """``tb-hunt`` or ``checklist`` for a call; a checklist call may carry several guides."""
    return "tb-hunt" if guide_of(params) == "tb-hunt" else "checklist"


def tb_project(root: Path) -> Path:
    for name in ("tb/test.py", "tb/main.cpp"):
        (root / name).write_text("// authored\n", encoding="utf-8", newline="\n")
    overlay = root / ".booley" / "tb_style_guide.md"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(TB_STYLE_MARKER, encoding="utf-8", newline="\n")
    return root


def write_sim_contract(project: Path) -> None:
    """A project simulation contract with custom sentinels and trace file."""
    (project / ".booley").mkdir(exist_ok=True)
    (project / ".booley" / "booley.toml").write_text(
        "[flows.sim]\n"
        'pass_sentinels = ["ALL TESTS OK"]\n'
        'fail_sentinels = ["TESTS BROKEN"]\n'
        'trace_files = ["waves.vcd"]\n',
        encoding="utf-8",
        newline="\n",
    )


@pytest.mark.parametrize("language", list(TB_LANGUAGE_SCOPES))
def test_tb_quality_runs_language_checklists_and_one_hunt_call(
    project: Path, agent: ScriptedAgent, language: str
) -> None:
    """The scope's language checklists share one call; a hunt call runs beside it."""
    scope, checklists = TB_LANGUAGE_SCOPES[language]
    tb_project(project)
    first_file = scope.split(",")[0]
    agent.answer(
        {
            "tb-hunt": {
                "issues": [
                    issue(file=first_file, summary="Hunt finding", category="quality", line=9)
                ]
            },
            "*": {
                "issues": [issue(file=first_file, summary="Checklist finding", category="quality")]
            },
        }
    )
    result = review(project, tb_scope_args(scope))
    assert result.exit_code == 0, result.report_text
    by_route = {tb_route(call): call.system_prompt or "" for call in agent.calls}
    assert sorted(by_route) == ["checklist", "tb-hunt"]
    headings = _HEADING.findall(by_route["checklist"])
    assert headings == [f"## Review guide — quality ({name})" for name in checklists]
    assert _HEADING.findall(by_route["tb-hunt"]) == ["## Review guide — quality (tb-hunt)"]
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


def test_tb_hunt_call_gets_the_checklist_calls_inputs(project: Path, agent: ScriptedAgent) -> None:
    """Both calls share one user prompt: spec, simulation context and project contract."""
    write_sim_contract(project)
    result = review(project, [*TB_ARGS, "--spec-text", "Reset drives q low."])
    assert result.exit_code == 0, result.report_text
    prompts = {tb_route(call): call.prompt for call in agent.calls}
    assert sorted(prompts) == ["checklist", "tb-hunt"]
    assert prompts["tb-hunt"] == prompts["checklist"]
    for heading in ("## Specification", "## Simulation Context", "## Project Simulation Contract"):
        assert heading in prompts["tb-hunt"]
    assert "Reset drives q low." in prompts["tb-hunt"]
    for configured in ("ALL TESTS OK", "TESTS BROKEN", "waves.vcd"):
        assert configured in prompts["tb-hunt"]


def test_tb_hunt_guide_defers_to_the_project_simulation_contract(
    project: Path, agent: ScriptedAgent
) -> None:
    """The hunt guide yields its sentinel and trace defaults to the project's configuration."""
    write_sim_contract(project)
    demand = issue(
        file="tb/tb.sv", summary="Testbench never prints [SIM_RESULT]", category="quality"
    )
    agent.answer({"tb-hunt": {"issues": [demand]}, "*": {"issues": []}})
    result = review(project, TB_ARGS)
    hunts = [call for call in agent.calls if tb_route(call) == "tb-hunt"]
    assert len(hunts) == 1, "expected one tb-hunt call"
    (hunt,) = hunts
    guide = (hunt.system_prompt or "").split("## Review guide — quality (tb-hunt)")[1]
    contract = [line for line in guide.splitlines() if "Project Simulation Contract" in line]
    assert any("configured pass and fail sentinels" in line for line in contract)
    assert any("replace this guide's defaults" in line for line in contract)
    assert "never a finding" in guide
    assert "ALL TESTS OK" in hunt.prompt
    assert "waves.vcd" in hunt.prompt
    # What the hunt reports about the contract stays a model judgment: nothing is filtered.
    assert [row["summary"] for row in result.detail["issue_list"]] == [demand["summary"]]
    assert result.detail["filtered"] == []
