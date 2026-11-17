"""Reviewer ``--recheck``: judge an earlier review's open findings against the current sources (#4522).

Every test drives a real Reviewer with only the model call mocked: a mocked full review
writes the input evidence file, then a mocked re-check reads it.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from booley.mcp.base import McpToolResult
from booley.mcp.server import _params_to_argv
from booley.runtime.booley_dir import resolve_common_booley_dir
from booley.specialists.reviewer import ReviewerSpecialist

BASE_ARGS = ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "bugs"]
RESET = "Wrong reset value"
ENABLE = "Enable polarity inverted"
NAMING = "Signal naming nit"


def finding(
    summary: str,
    severity: str = "MAJOR",
    disposition: str = "current",
    file: str = "rtl/dut.sv",
    line: int = 3,
    category: str = "bugs",
) -> dict[str, Any]:
    return {
        "severity": severity,
        "confidence": "HIGH",
        "category": category,
        "file": file,
        "line": line,
        "summary": summary,
        "kind": "code_defect",
        "disposition": disposition,
        "ticket_clause": "Reset clears output",
        "fix_suggestion": f"Fix: {summary}",
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    for name in ("rtl/dut.sv", "rtl/other.sv"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(b"// line %d\n" % n for n in range(1, 11)))
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    return root


def _reviewer(root: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], output: str) -> Any:
    reviewer = ReviewerSpecialist()
    reviewer.parse_args(["--work-dir", str(root), *args])
    invoke = MagicMock(
        return_value=SimpleNamespace(output=output, captured_agent_capability_calls={})
    )
    monkeypatch.setattr(reviewer, "_invoke_agent", invoke)
    return reviewer, invoke


def run_tool(
    root: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], payload: dict[str, Any]
) -> tuple[McpToolResult, MagicMock]:
    reviewer, invoke = _reviewer(root, monkeypatch, args, json.dumps(payload))
    return reviewer._run(), invoke


def full_review(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    issues: list[dict[str, Any]],
    args: list[str] | None = None,
) -> tuple[Path, dict[str, str]]:
    """Run a full review; returns its evidence file and each summary's finding ID."""
    result, _ = run_tool(root, monkeypatch, args or BASE_ARGS, {"issues": issues})
    assert result.exit_code == 0, result.report_text
    ids = {row["summary"]: row["finding_id"] for row in result.detail["issue_list"]}
    return Path(result.detail["audit_evidence"]), ids


def verdict(
    finding_id: str, status: str, file: str = "rtl/dut.sv", line: int = 3
) -> dict[str, Any]:
    entry: dict[str, Any] = {"finding_id": finding_id, "verdict": status}
    if status == "fixed":
        entry["evidence"] = {"file": file, "line": line, "reason": "Reset now clears q"}
    return entry


def recheck(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: Path,
    verdicts: list[dict[str, Any]],
    *extra: str,
    **payload: Any,
) -> tuple[McpToolResult, MagicMock]:
    return run_tool(
        root, monkeypatch, ["--recheck", str(evidence), *extra], {"verdicts": verdicts, **payload}
    )


def statuses(result: McpToolResult) -> dict[str, str]:
    return {row["summary"]: row["status"] for row in result.detail["issue_list"]}


def two_findings(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    return full_review(
        project,
        monkeypatch,
        [finding(RESET), finding(ENABLE, "MINOR", line=5), finding(NAMING, "MINOR", "advisory")],
    )


def test_recheck_sends_only_open_findings_and_reports_each_verdict(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    result, invoke = recheck(
        project,
        monkeypatch,
        evidence,
        [verdict(ids[RESET], "fixed"), verdict(ids[ENABLE], "still_open")],
    )

    assert result.exit_code == 0, result.report_text
    invoke.assert_called_once()
    prompt = invoke.call_args.args[0].prompt
    assert ids[RESET] in prompt
    assert ids[ENABLE] in prompt
    assert ids[NAMING] not in prompt
    assert NAMING not in prompt
    assert "Fix: Wrong reset value" in prompt
    assert "Reset clears output" in prompt
    assert statuses(result) == {RESET: "fixed", ENABLE: "still_open", NAMING: "advisory"}
    assert f"{RESET} (ID: {ids[RESET]}) — fixed" in result.report_text
    assert f"{ENABLE} (ID: {ids[ENABLE]}) — still open" in result.report_text
    assert result.report_text.endswith(f"Reviewer evidence: {result.detail['audit_evidence']}")
    assert Path(result.detail["audit_evidence"]) != evidence
    assert result.detail["recheck_of"] == str(evidence)
    assert result.detail["issues"] == 1


def test_recheck_reports_no_new_findings_from_extra_entries(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    result, _ = recheck(
        project,
        monkeypatch,
        evidence,
        [
            verdict(ids[RESET], "fixed"),
            verdict("0123456789abcdef", "still_open"),
            "not an object",
            verdict(ids[ENABLE], "maybe"),
            verdict(ids[RESET], "still_open"),
            verdict(ids[ENABLE], "still_open"),
        ],
        issues=[finding("Brand new problem")],
    )

    assert sorted(row["finding_id"] for row in result.detail["issue_list"]) == sorted(ids.values())
    assert "Brand new problem" not in result.report_text
    assert statuses(result) == {RESET: "fixed", ENABLE: "still_open", NAMING: "advisory"}
    assert sorted(row["reason"] for row in result.detail["rejected"]) == [
        "duplicate_verdict",
        "invalid_verdict",
        "unknown_finding",
        "unknown_finding",
    ]


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        {"file": "rtl/dut.sv", "line": 3},
        {"file": "rtl/missing.sv", "line": 3, "reason": "moved"},
        {"file": "rtl/dut.sv", "line": 999, "reason": "past the end"},
        {"file": "rtl/dut.sv", "line": 0, "reason": "not a line"},
        {"file": "rtl/dut.sv", "line": True, "reason": "bool is not a line"},
        {"file": "../outside.sv", "line": 1, "reason": "outside the work directory"},
        {"file": "rtl", "line": 1, "reason": "a directory"},
        "rtl/dut.sv:3",
        {"file": "rtl/dut\0.sv", "line": 1, "reason": "a NUL byte"},
    ],
    ids=[
        "none",
        "no-reason",
        "missing-file",
        "line-past-end",
        "line-zero",
        "line-bool",
        "outside",
        "directory",
        "not-an-object",
        "nul-byte",
    ],
)
def test_fixed_without_current_source_evidence_is_still_open(
    project: Path, monkeypatch: pytest.MonkeyPatch, evidence: Any
) -> None:
    (project.parent / "outside.sv").write_text("// outside\n")
    path, ids = full_review(project, monkeypatch, [finding(RESET)])
    claim: dict[str, Any] = {"finding_id": ids[RESET], "verdict": "fixed"}
    if evidence is not None:
        claim["evidence"] = evidence
    result, _ = recheck(project, monkeypatch, path, [claim])

    assert statuses(result) == {RESET: "still_open"}
    assert "recheck_evidence" not in result.detail["issue_list"][0]
    assert [row["reason"] for row in result.detail["rejected"]] == ["fixed_without_evidence"]
    assert "still open" in result.report_text


def test_fixed_with_evidence_keeps_the_citation(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, ids = full_review(project, monkeypatch, [finding(RESET)])
    result, _ = recheck(
        project, monkeypatch, path, [verdict(ids[RESET], "fixed", "rtl/other.sv", 10)]
    )

    record = result.detail["issue_list"][0]
    assert record["status"] == "fixed"
    assert record["recheck_evidence"] == {
        "file": "rtl/other.sv",
        "line": 10,
        "reason": "Reset now clears q",
    }
    assert result.detail["rejected"] == []


def test_omitted_finding_is_cant_tell(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    result, _ = recheck(project, monkeypatch, evidence, [verdict(ids[RESET], "fixed")])

    assert statuses(result)[ENABLE] == "cant_tell"
    assert f"{ENABLE} (ID: {ids[ENABLE]}) — can't tell" in result.report_text
    assert [row["reason"] for row in result.detail["rejected"]] == ["omitted_finding"]
    assert result.detail["issues"] == 1


def test_output_without_a_verdicts_wrapper_is_a_specialist_error(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, _ = two_findings(project, monkeypatch)
    reviewer, _ = _reviewer(project, monkeypatch, ["--recheck", str(evidence)], "All fixed.")
    result = reviewer._run()

    assert result.exit_code == 2
    assert "no verdicts JSON" in result.report_text
    assert result.detail["review_error"] is True
    assert Path(result.detail["audit_evidence"]).is_file()


def test_provider_failure_is_a_specialist_error(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, _ = two_findings(project, monkeypatch)
    reviewer, invoke = _reviewer(project, monkeypatch, ["--recheck", str(evidence)], "")
    invoke.side_effect = RuntimeError("provider down")
    result = reviewer._run()

    assert result.exit_code == 2
    assert "provider down" in result.report_text


@pytest.mark.parametrize(
    ("fail_on", "severity", "status", "expected"),
    [
        ("major", "MAJOR", "fixed", 0),
        ("major", "MAJOR", "still_open", 1),
        ("major", "MAJOR", "cant_tell", 1),
        ("major", "MINOR", "still_open", 0),
        ("minor", "MINOR", "cant_tell", 1),
    ],
)
def test_fail_on_counts_only_still_open_and_cant_tell(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_on: str,
    severity: str,
    status: str,
    expected: int,
) -> None:
    path, ids = full_review(
        project,
        monkeypatch,
        [finding(RESET, severity), finding(NAMING, "CRITICAL", "advisory")],
    )
    verdicts = [] if status == "cant_tell" else [verdict(ids[RESET], status)]
    result, _ = recheck(project, monkeypatch, path, verdicts, "--fail-on", fail_on)

    assert result.exit_code == expected, result.report_text


def test_recheck_without_fail_on_exits_zero(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, ids = full_review(project, monkeypatch, [finding(RESET, "CRITICAL")])
    result, _ = recheck(project, monkeypatch, path, [verdict(ids[RESET], "still_open")])

    assert result.exit_code == 0, result.report_text


def _edit(mutate: Callable[[dict[str, Any]], Any]) -> Callable[[Path], None]:
    def apply(path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    return apply


def _no_open(data: dict[str, Any]) -> None:
    for row in data["detail"]["issue_list"]:
        row["status"] = "fixed"


UNUSABLE = {
    "missing": (lambda p: p.unlink(), "evidence file not found"),
    "directory": (lambda p: (p.unlink(), p.mkdir()), "evidence file not found"),
    "not-json": (lambda p: p.write_text("{nope", encoding="utf-8"), "is not JSON"),
    "not-utf8": (lambda p: p.write_bytes(b"\xff\xfe\x00"), "is not JSON"),
    "array": (lambda p: p.write_text("[]", encoding="utf-8"), "is not a Reviewer evidence file"),
    "detail-version": (
        _edit(lambda d: d["detail"].update(review_detail_version=3)),
        "has detail version 3",
    ),
    "contract-version": (
        _edit(lambda d: d["detail"]["contract"].update(version=3)),
        "has detail version 3",
    ),
    "no-issue-list": (_edit(lambda d: d["detail"].pop("issue_list")), "has no findings list"),
    "bad-finding": (
        _edit(lambda d: d["detail"]["issue_list"].__setitem__(0, "x")),
        "finding 1 is malformed",
    ),
    "bad-severity": (
        _edit(lambda d: d["detail"]["issue_list"][0].update(severity="HUGE")),
        "finding 1 is malformed",
    ),
    "bad-category": (
        _edit(lambda d: d["detail"]["contract"].update(category="sw")),
        "no usable review contract",
    ),
    "bad-focus": (
        _edit(lambda d: d["detail"]["contract"].update(focus="quality")),
        "no usable review contract",
    ),
    "empty-scope": (
        _edit(lambda d: d["detail"]["contract"].update(scope=[])),
        "no usable review contract",
    ),
    "no-open-findings": (_edit(_no_open), "has no open findings"),
}


@pytest.mark.parametrize("case", sorted(UNUSABLE))
def test_unusable_evidence_file_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    path, _ = full_review(project, monkeypatch, [finding(RESET)])
    damage, message = UNUSABLE[case]
    damage(path)
    result, invoke = recheck(project, monkeypatch, path, [])

    assert result.exit_code == 2
    assert message in result.report_text
    assert str(path) in result.report_text
    invoke.assert_not_called()


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (["--scope", "rtl/dut.sv"], "--scope"),
        (["--base", "HEAD"], "--base"),
        (["--category", "rtl"], "--category"),
        (["--focus", "bugs"], "--focus"),
        (["--spec", "spec.md"], "--spec"),
        (["--spec-text", "text"], "--spec-text"),
        (["--steer", "look at reset"], "--steer"),
        (["--scope", "rtl/dut.sv", "--steer", "x"], "--scope, --steer"),
    ],
)
def test_recheck_refuses_review_options(
    project: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str], named: str
) -> None:
    path, _ = full_review(project, monkeypatch, [finding(RESET)])
    result, invoke = recheck(project, monkeypatch, path, [], *flags)

    assert result.exit_code == 2
    assert f"--recheck takes category, focus and scope from the evidence file; remove {named}" in (
        result.report_text
    )
    invoke.assert_not_called()


def test_display_tag_follows_the_evidence_file_once_it_is_read(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, ids = full_review(project, monkeypatch, [finding(RESET)])
    reviewer, _ = _reviewer(
        project,
        monkeypatch,
        ["--recheck", str(path)],
        json.dumps({"verdicts": [verdict(ids[RESET], "fixed")]}),
    )
    assert reviewer.display_tag == "recheck"
    reviewer._run()
    assert reviewer.display_tag == "rtl/bugs"


def test_recheck_evidence_feeds_a_later_recheck(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    first, _ = recheck(
        project,
        monkeypatch,
        evidence,
        [verdict(ids[RESET], "fixed"), verdict(ids[ENABLE], "still_open")],
    )
    second_input = Path(first.detail["audit_evidence"])
    second, invoke = recheck(
        project, monkeypatch, second_input, [verdict(ids[ENABLE], "fixed", line=5)]
    )

    assert second.exit_code == 0, second.report_text
    prompt = invoke.call_args.args[0].prompt
    assert ids[ENABLE] in prompt
    assert ids[RESET] not in prompt
    assert statuses(second) == {RESET: "fixed", ENABLE: "fixed", NAMING: "advisory"}
    assert sorted(row["finding_id"] for row in second.detail["issue_list"]) == sorted(ids.values())
    carried = next(r for r in second.detail["issue_list"] if r["summary"] == RESET)
    assert carried["recheck_evidence"]["file"] == "rtl/dut.sv"
    assert second.detail["contract"]["category"] == "rtl"
    assert second.detail["contract"]["focus"] == "bugs"
    assert second.detail["contract"]["scope"] == ["rtl/dut.sv"]
    assert second.detail["recheck_of"] == str(second_input)
    assert second.detail["issues"] == 0


def test_recheck_dry_run_lists_findings_without_calling_the_model(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    before = sorted(evidence.parent.iterdir())
    result, invoke = recheck(project, monkeypatch, evidence, [], "--dry-run")

    assert result.exit_code == 0, result.report_text
    invoke.assert_not_called()
    assert result.detail["mode"] == "dry_run"
    assert sorted(result.detail["open_finding_ids"]) == sorted([ids[RESET], ids[ENABLE]])
    assert result.detail["carried_over"] == 1
    assert result.detail["category"] == "rtl"
    assert result.detail["focus"] == "bugs"
    assert result.detail["scope"] == ["rtl/dut.sv"]
    assert sorted(evidence.parent.iterdir()) == before


def test_recheck_grants_only_scope_files_that_still_exist(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, ids = full_review(
        project,
        monkeypatch,
        [finding(RESET), finding(ENABLE, file="rtl/other.sv")],
        ["--scope", "rtl/dut.sv,rtl/other.sv", "--category", "rtl", "--focus", "bugs"],
    )
    (project / "rtl/other.sv").unlink()
    granted: list[tuple[Any, ...]] = []

    @contextlib.contextmanager
    def recording(params: Any, _access: Any, category: Any = None, **kwargs: Any) -> Iterator[Any]:
        granted.append((category, kwargs["scope"]))
        yield params, None

    monkeypatch.setattr("booley.specialists.specialist.isolated_agent_workspace", recording)
    monkeypatch.setattr(
        "booley.specialists.specialist._call_agent_sync",
        lambda *_a, **_k: SimpleNamespace(
            output=json.dumps({"verdicts": [verdict(ids[RESET], "fixed")]}),
            captured_agent_capability_calls={},
        ),
    )
    reviewer = ReviewerSpecialist()
    reviewer.parse_args(["--work-dir", str(project), "--recheck", str(path)])
    result = reviewer._run()

    assert result.exit_code == 0, result.report_text
    assert granted == [("rtl", ("rtl/dut.sv",))]
    assert statuses(result) == {RESET: "fixed", ENABLE: "cant_tell"}


def test_removed_scope_file_is_named_in_the_prompt(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = full_review(
        project,
        monkeypatch,
        [finding(RESET), finding(ENABLE, file="rtl/other.sv")],
        ["--scope", "rtl/dut.sv,rtl/other.sv", "--category", "rtl", "--focus", "bugs"],
    )
    (project / "rtl/other.sv").unlink()
    _, invoke = recheck(project, monkeypatch, path, [])

    prompt = invoke.call_args.args[0].prompt
    assert "removed since the review: rtl/other.sv" in prompt
    assert "  - rtl/dut.sv" in prompt


def test_spec_recheck_carries_the_reviewed_inline_spec(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "Reset drives q low.\nEnable is active high."
    path, ids = full_review(
        project,
        monkeypatch,
        [finding(RESET, category="spec")],
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "spec", "--spec-text", text],
    )
    _, invoke = recheck(project, monkeypatch, path, [verdict(ids[RESET], "fixed")])

    assert f"## Specification (source: inline text)\n\n{text}" in invoke.call_args.args[0].prompt


def test_spec_recheck_reads_the_current_spec_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = project / "spec.md"
    spec.write_text("Original requirement.\n", encoding="utf-8")
    path, ids = full_review(
        project,
        monkeypatch,
        [finding(RESET, category="spec")],
        ["--scope", "rtl/dut.sv", "--category", "rtl", "--focus", "spec", "--spec", str(spec)],
    )
    spec.write_text("Amended requirement.\n", encoding="utf-8")
    _, invoke = recheck(project, monkeypatch, path, [verdict(ids[RESET], "fixed")])
    assert "Amended requirement." in invoke.call_args.args[0].prompt

    spec.unlink()
    _, invoke = recheck(project, monkeypatch, path, [verdict(ids[RESET], "fixed")])
    assert "## Specification" not in invoke.call_args.args[0].prompt


def test_mcp_recheck_argument_runs_the_same_recheck(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    argv = _params_to_argv({"recheck": str(evidence), "fail_on": "major"})
    reviewer, _ = _reviewer(
        project,
        monkeypatch,
        argv,
        json.dumps(
            {"verdicts": [verdict(ids[RESET], "fixed"), verdict(ids[ENABLE], "still_open")]}
        ),
    )
    result = reviewer._run()

    assert statuses(result) == {RESET: "fixed", ENABLE: "still_open", NAMING: "advisory"}
    assert result.exit_code == 0  # the open finding is MINOR, below major


def test_recheck_reads_only_the_given_file_and_writes_only_its_evidence(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    elsewhere = project.parent / "elsewhere" / "kept.json"
    elsewhere.parent.mkdir()
    evidence.replace(elsewhere)
    booley_dir = resolve_common_booley_dir(project)
    assert not list((booley_dir / "reviewer-evidence").glob("*.json"))
    before = {p for p in booley_dir.rglob("*") if p.is_file()}

    result, _ = recheck(project, monkeypatch, elsewhere, [verdict(ids[RESET], "fixed")])

    assert result.exit_code == 0, result.report_text
    after = {p for p in booley_dir.rglob("*") if p.is_file()}
    assert after - before == {Path(result.detail["audit_evidence"])}
    assert before - after == set()
    assert elsewhere.exists()
