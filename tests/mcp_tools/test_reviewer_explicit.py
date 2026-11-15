"""Explicit discovery/verification review contracts (issue #1507)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from booley.criteria.freshness import evaluate_verification_freshness
from booley.criteria.state import DevelopmentState
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.mcp import server
from booley.mcp.base import McpToolResult
from booley.mcp.schema_extractor import extract_schema
from booley.mcp.server import _params_to_argv
from booley.runtime.file_lock import nonblocking_file_lock
from booley.specialists.reviewer import ReviewerSpecialist

KEY = "review_rtl_bugs_done"
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


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/dut.sv").write_text("module dut; endmodule\n")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    return tmp_path


def endpoint(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[ReviewerSpecialist, MagicMock]:
    reviewer = ReviewerSpecialist()
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


def discovery(
    root: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[McpToolResult, ReviewerSpecialist, MagicMock]:
    reviewer, invoke = endpoint(
        root,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"],
        **kwargs,
    )
    return reviewer._run(), reviewer, invoke


@pytest.mark.parametrize(
    "argument",
    [
        ["--category", "rtl"],
        ["--focus", "bugs"],
        ["--scope", "rtl/dut.sv"],
        ["--base", "main"],
        ["--steer", "waive"],
        ["--dry-run"],
        ["--instruction", "waive"],
    ],
)
def test_verify_refuses_other_review_arguments(
    project: Path, monkeypatch: pytest.MonkeyPatch, argument: list[str]
) -> None:
    discovery(project, monkeypatch)
    reviewer, invoke = endpoint(project, monkeypatch, ["--verify", *argument])
    assert reviewer._run().exit_code == 2
    invoke.assert_not_called()


@pytest.mark.parametrize(
    "status,requested,expected",
    [("WAIVED", True, 0), ("STILL_PRESENT", True, 1), ("WAIVED", False, 1)],
)
def test_waivers_require_explicit_open_id_and_reviewer_acceptance(
    project: Path, monkeypatch: pytest.MonkeyPatch, status: str, requested: bool, expected: int
) -> None:
    initial, _, _ = discovery(project, monkeypatch)
    finding_id = ReviewerSpecialist._open_findings(initial.detail)[0]["finding_id"]
    args = ["--verify", "--fail-on", "major"] + (
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


def test_verify_rejects_unknown_waiver_and_no_open_findings(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"]
    reviewer, _ = endpoint(project, monkeypatch, args)
    assert reviewer._run().exit_code == 0
    reviewer, invoke = endpoint(
        project, monkeypatch, [*args[:-1], "security"], payload={"issues": []}
    )
    assert reviewer._run().exit_code == 0
    assert invoke.call_count == 1
    verifier, invoke = endpoint(project, monkeypatch, ["--verify"])
    assert "no open findings" in verifier._run().report_text
    invoke.assert_not_called()


def test_done_verify_preserves_completion_freshness(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_mcp_waiver_pairs_roundtrip() -> None:
    reviewer = ReviewerSpecialist()
    schema = extract_schema(reviewer._parser)
    assert schema["properties"]["waive"]["items"]["minItems"] == 2
    args = _params_to_argv(
        {"verify": True, "waive": [["id1", "first reason"], ["id2", "second reason"]]}
    )
    parsed = reviewer.parse_args(args)
    assert parsed.waive == [["id1", "first reason"], ["id2", "second reason"]]


def test_verify_does_not_certify_a_changed_spec(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = project / "spec.md"
    spec.write_text("Reset clears output")
    reviewer, _ = endpoint(
        project,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "spec", "--spec", "spec.md"],
        payload={"issues": [{**ISSUE, "category": "spec"}]},
    )
    assert reviewer._run().exit_code == 0
    spec.write_text("New requirements after discovery")
    verifier, _ = endpoint(
        project,
        monkeypatch,
        ["--verify"],
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


def test_source_change_during_review_cannot_certify_unreviewed_bytes(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewer, invoke = endpoint(
        project,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"],
        payload={"issues": []},
    )
    response = invoke.return_value

    def mutate(*args: list[str], **kwargs: Any) -> MagicMock:
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


def test_done_verify_provider_error_keeps_completion_evidence(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_interactive_history_survives_without_explicit_state_environment(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOOLEY_STATE_FILE")
    reviewer, _ = endpoint(
        project,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"],
    )
    assert reviewer._run().exit_code == 0  # Interactive discovery defaults to done.
    assert reviewer.state.file_path is not None
    verifier, _ = endpoint(
        project,
        monkeypatch,
        ["--verify"],
        payload={
            "findings": [{"index": 1, "status": "FIXED", "evidence": "rtl/dut.sv:1 corrected"}]
        },
    )
    assert verifier._run().exit_code == 0
    assert verifier.state.file_path == reviewer.state.file_path


@pytest.mark.parametrize("next_focus", ["bugs", "security"])
def test_done_findings_need_no_verify_and_do_not_block_discovery(
    project: Path, monkeypatch: pytest.MonkeyPatch, next_focus: str
) -> None:
    state = DevelopmentState.load(project / "state.json")
    state.criteria.clear()
    state.init_criteria({"review_rtl_bugs_done": True, "review_rtl_security_done": True})
    state.save()
    initial, _, _ = discovery(project, monkeypatch)
    assert initial.exit_code == 0 and initial.detail["issue_list"]
    (project / "rtl/dut.sv").write_text("module dut; wire changed; endmodule")
    reviewer, invoke = endpoint(
        project,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", next_focus],
        payload={"issues": []},
    )
    assert reviewer._run().exit_code == 0
    # rtl/bugs declares two guides (checklist + hunt), one agent call each; security has one.
    assert invoke.call_count == (2 if next_focus == "bugs" else 1)


def test_review_history_is_scoped_to_checkout_under_shared_booley_dir(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOOLEY_STATE_FILE")
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / "shared"))
    (tmp_path / "shared").mkdir()
    first, _ = endpoint(
        project,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"],
    )
    assert first._run().exit_code == 0
    other = tmp_path / "other"
    other.mkdir()
    second, invoke = endpoint(other, monkeypatch, ["--verify"])
    assert first.state.file_path != second.state.file_path
    assert "no prior review" in second._run().report_text
    invoke.assert_not_called()


def test_review_calls_refuse_overlap_and_reload_history_loaded_before_other_call(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = ["--scope", "rtl/dut.sv", "--category", "rtl"]
    first, invoke = endpoint(project, monkeypatch, [*args, "--focus", "bugs"])
    second, second_invoke = endpoint(
        project, monkeypatch, [*args, "--focus", "security"], payload={"issues": []}
    )
    lock_path = (project / "state.json").with_suffix(".review.lock")
    with lock_path.open("a+") as handle, nonblocking_file_lock(handle):
        assert "Another review is running" in first._run().report_text
        invoke.assert_not_called()
    assert first._run().exit_code == 0
    result = second._run()
    assert result.exit_code == 0
    assert second_invoke.call_count == 1
    assert result.detail["review_call_order"] == 2
    assert "review_rtl_bugs_done" in DevelopmentState.load(project / "state.json").criteria


def test_mcp_pair_forwarding_is_generic() -> None:
    assert _params_to_argv({"pair": [["one", "first"], ["two", "second"]]}) == [
        "--pair",
        "one",
        "first",
        "--pair",
        "two",
        "second",
    ]


@pytest.mark.parametrize("flag", ["--ver", "--sco", "--ste"])
def test_review_arguments_do_not_accept_abbreviations(flag: str) -> None:
    with pytest.raises(SystemExit):
        ReviewerSpecialist().parse_args([flag, "value"])


def test_mcp_endpoint_command_forwards_verify_waivers(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        server, "_resolve_transcript_dir", lambda *args, **kwargs: project / "transcripts"
    )
    command = server._endpoint_command(
        "reviewer",
        {"verify": True, "waive": [["finding", "intentional by specification"]]},
        {
            "module": "reviewer",
            "module_path": "booley.specialists.reviewer",
            "is_specialist": True,
        },
        {},
    )
    assert command[:3] == ["python", "-m", "booley.specialists.reviewer"]
    args = ReviewerSpecialist().parse_args(command[3:])
    assert args.verify and args.waive == [["finding", "intentional by specification"]]


@pytest.mark.parametrize("disposition", ["advisory", "deferred", "out_of_scope"])
def test_done_verify_keeps_non_corrective_findings_and_completion_metadata(
    project: Path, monkeypatch: pytest.MonkeyPatch, disposition: str
) -> None:
    state = DevelopmentState.load(project / "state.json")
    state.criteria.clear()
    state.init_criteria({"review_rtl_bugs_done": True})
    state.save()
    discovery(
        project,
        monkeypatch,
        payload={
            "issues": [
                ISSUE,
                {**ISSUE, "summary": "Retained observation", "disposition": disposition},
            ]
        },
    )
    before = DevelopmentState.load(project / "state.json").criteria["review_rtl_bugs_done"].detail
    observation = next(row for row in before["issue_list"] if row["disposition"] == disposition)
    verifier, invoke = endpoint(
        project,
        monkeypatch,
        ["--verify"],
        payload={
            "findings": [{"index": 1, "status": "FIXED", "evidence": "rtl/dut.sv:1 corrected"}]
        },
    )
    result = verifier._run()
    assert result.exit_code == 0
    assert result.detail["observations"] == [observation]
    after = DevelopmentState.load(project / "state.json").criteria["review_rtl_bugs_done"].detail
    assert observation in after["issue_list"] and len(after["issue_list"]) == 2
    assert after["pending"] == [] and after["resolved"][0]["status"] == "fixed"
    receipt_path = Path(result.detail["audit_evidence"])
    assert after["audit_evidence"] == after["artifacts"]["reviewer_evidence"] == str(receipt_path)
    assert result.detail["artifacts"]["reviewer_evidence"] == str(receipt_path)
    receipt_detail = json.loads(receipt_path.read_text())["detail"]
    for field in (
        "receipt_id",
        "_source_fingerprint",
        "contract",
        "observation_count",
        "review_outcome",
        "gate_passed",
    ):
        assert after[field] == before[field]
        assert receipt_detail[field] == before[field]
    assert invoke.call_count == 1


def test_reviewer_schema_does_not_inject_interactive_scope_default() -> None:
    assert "default" not in extract_schema(ReviewerSpecialist()._parser)["properties"]["scope"]


def test_missing_spec_during_verify_explains_recovery(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = project / "spec.md"
    spec.write_text("Reset clears output")
    reviewer, _ = endpoint(
        project,
        monkeypatch,
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "spec", "--spec", "spec.md"],
        payload={"issues": [{**ISSUE, "category": "spec"}]},
    )
    assert reviewer._run().exit_code == 0
    spec.unlink()
    verifier, invoke = endpoint(project, monkeypatch, ["--verify"])
    result = verifier._run()
    assert result.exit_code == 2 and "Restore the original specification" in result.report_text
    assert "Pass --spec" not in result.report_text
    invoke.assert_not_called()
