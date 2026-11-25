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


NO_EVIDENCE = [
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
]


def test_fixed_without_current_source_evidence_is_still_open(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project.parent / "outside.sv").write_text("// outside\n")
    path, ids = full_review(project, monkeypatch, [finding(RESET)])
    for evidence in NO_EVIDENCE:
        claim: dict[str, Any] = {"finding_id": ids[RESET], "verdict": "fixed"}
        if evidence is not None:
            claim["evidence"] = evidence
        result, _ = recheck(project, monkeypatch, path, [claim])

        assert statuses(result) == {RESET: "still_open"}, evidence
        assert "recheck_evidence" not in result.detail["issue_list"][0], evidence
        assert [row["reason"] for row in result.detail["rejected"]] == [
            "fixed_without_evidence"
        ], evidence
        assert "still open" in result.report_text, evidence


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


FAIL_ON_CASES = [
    ("major", "MAJOR", "fixed", 0),
    ("major", "MAJOR", "still_open", 1),
    ("major", "MAJOR", "cant_tell", 1),
    ("major", "MINOR", "still_open", 0),
    ("minor", "MINOR", "cant_tell", 1),
]


def test_fail_on_counts_only_still_open_and_cant_tell(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for fail_on, severity, status, expected in FAIL_ON_CASES:
        path, ids = full_review(
            project,
            monkeypatch,
            [finding(RESET, severity), finding(NAMING, "CRITICAL", "advisory")],
        )
        verdicts = [] if status == "cant_tell" else [verdict(ids[RESET], status)]
        result, _ = recheck(project, monkeypatch, path, verdicts, "--fail-on", fail_on)

        assert result.exit_code == expected, (fail_on, severity, status, result.report_text)


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
        "has contract version 3",
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
    "bad-fields": (
        _edit(lambda d: d["detail"]["issue_list"][0].pop("file")),
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


def test_unusable_evidence_file_is_refused(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for case, (damage, message) in UNUSABLE.items():
        path, _ = full_review(project, monkeypatch, [finding(RESET)])
        damage(path)
        result, invoke = recheck(project, monkeypatch, path, [])

        assert result.exit_code == 2, case
        assert message in result.report_text, case
        assert str(path) in result.report_text, case
        invoke.assert_not_called()


CONFLICTS = [
    (["--scope", "rtl/dut.sv"], "--scope"),
    (["--base", "HEAD"], "--base"),
    (["--category", "rtl"], "--category"),
    (["--focus", "bugs"], "--focus"),
    (["--spec", "spec.md"], "--spec"),
    (["--spec-text", "text"], "--spec-text"),
    (["--steer", "look at reset"], "--steer"),
    (["--scope", "rtl/dut.sv", "--steer", "x"], "--scope, --steer"),
]


def test_recheck_refuses_review_options(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = full_review(project, monkeypatch, [finding(RESET)])
    for flags, named in CONFLICTS:
        result, invoke = recheck(project, monkeypatch, path, [], *flags)

        assert result.exit_code == 2, named
        assert (
            f"--recheck takes category, focus and scope from the evidence file; remove {named}"
            in result.report_text
        )
        invoke.assert_not_called()


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
    assert ids[RESET] in result.report_text
    assert ids[ENABLE] in result.report_text
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

    result, _ = recheck(
        project,
        monkeypatch,
        elsewhere,
        [waived(ids[RESET]), verdict(ids[ENABLE], "fixed", line=5)],
        "--waive",
        ids[RESET],
        "Chosen by the integration",
    )

    assert result.exit_code == 0, result.report_text
    assert statuses(result)[RESET] == "waived"
    after = {p for p in booley_dir.rglob("*") if p.is_file()}
    assert after - before == {Path(result.detail["audit_evidence"])}
    assert before - after == set()
    assert elsewhere.exists()


def test_recheck_reports_an_open_finding_that_shares_its_id_with_an_advisory_one(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = full_review(
        project, monkeypatch, [finding(RESET), finding(RESET, disposition="advisory")]
    )
    result, _ = recheck(project, monkeypatch, evidence, [verdict(ids[RESET], "still_open")])

    assert result.exit_code == 0, result.report_text
    assert result.report_text.count(ids[RESET]) == 1
    assert [row["status"] for row in result.detail["issue_list"]] == ["still_open", "advisory"]


# --- --waive on a re-check (#4523) ---

WHY = "The reset value is chosen by the integration"


def waived(finding_id: str, ruling: str = "The reason holds.") -> dict[str, Any]:
    return {"finding_id": finding_id, "verdict": "waived", "waiver_ruling": ruling}


def test_waive_without_recheck_is_refused(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, invoke = run_tool(
        project, monkeypatch, [*BASE_ARGS, "--waive", "0123456789abcdef", WHY], {"issues": []}
    )

    assert result.exit_code == 2
    assert "--waive needs --recheck" in result.report_text
    invoke.assert_not_called()


def test_invalid_waiver_proposal_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    cases = {
        "unknown": (["--waive", "0123456789abcdef", WHY], "0123456789abcdef"),
        "advisory": (["--waive", ids[NAMING], WHY], ids[NAMING]),
        "repeated": (["--waive", ids[RESET], WHY, "--waive", ids[RESET], "Again"], ids[RESET]),
        "blank": (["--waive", ids[RESET], "  "], ids[RESET]),
    }
    for case, (flags, named) in cases.items():
        result, invoke = recheck(project, monkeypatch, evidence, [], *flags)

        assert result.exit_code == 2, case
        assert f"--waive {named}" in result.report_text, case
        invoke.assert_not_called()
    result, invoke = recheck(
        project, monkeypatch, evidence, [], "--waive", "0123456789abcdef", WHY, "--dry-run"
    )
    assert result.exit_code == 2
    invoke.assert_not_called()


def test_accepted_waiver_is_waived_excluded_from_fail_on_and_listed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    result, invoke = recheck(
        project,
        monkeypatch,
        evidence,
        [waived(ids[RESET]), verdict(ids[ENABLE], "still_open")],
        "--waive",
        ids[RESET],
        WHY,
        "--fail-on",
        "major",
    )

    assert result.exit_code == 0, result.report_text
    prompt = invoke.call_args.args[0].prompt
    assert f"waiver proposal: {WHY}" in prompt
    assert '"waived"' in prompt
    assert statuses(result) == {RESET: "waived", ENABLE: "still_open", NAMING: "advisory"}
    record = next(r for r in result.detail["issue_list"] if r["summary"] == RESET)
    assert record["justification"] == WHY
    assert record["waiver_ruling"] == "The reason holds."
    assert result.detail["issues"] == 1
    assert f"{RESET} (ID: {ids[RESET]}) — waived" in result.report_text
    assert "ACCEPTED WAIVERS" in result.report_text
    assert (
        f"[MAJOR] rtl/dut.sv:3 — {RESET} (ID: {ids[RESET]}) — reason: {WHY}" in result.report_text
    )
    assert "RESULT: 0 fixed, 1 waived, 1 still open or can't tell" in result.report_text


def test_refused_waiver_is_judged_as_usual_and_reported(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    ruling = "The cited reset is still wrong."
    result, _ = recheck(
        project,
        monkeypatch,
        evidence,
        [
            {**verdict(ids[RESET], "still_open"), "waiver_ruling": ruling},
            verdict(ids[ENABLE], "fixed", line=5),
        ],
        "--waive",
        ids[RESET],
        WHY,
        "--waive",
        ids[ENABLE],
        "Not needed",
        "--fail-on",
        "major",
    )

    assert result.exit_code == 1, result.report_text
    assert statuses(result) == {RESET: "still_open", ENABLE: "fixed", NAMING: "advisory"}
    assert f"still open — waiver refused: {ruling}" in result.report_text
    assert "fixed — waiver refused: the Reviewer gave no reason" in result.report_text
    assert "ACCEPTED WAIVERS" not in result.report_text
    assert result.detail["refused_waivers"] == [
        {"finding_id": ids[RESET], "justification": WHY, "ruling": ruling},
        {"finding_id": ids[ENABLE], "justification": "Not needed", "ruling": ""},
    ]
    assert all("justification" not in row for row in result.detail["issue_list"])


def test_unsolicited_waived_verdict_is_still_open(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    for extra in ([], ["--waive", ids[ENABLE], WHY]):
        result, _ = recheck(
            project,
            monkeypatch,
            evidence,
            [waived(ids[RESET]), verdict(ids[ENABLE], "still_open")],
            *extra,
        )

        assert statuses(result)[RESET] == "still_open", extra
        assert [row["reason"] for row in result.detail["rejected"]] == ["unsolicited_waiver"]
        assert "ACCEPTED WAIVERS" not in result.report_text


def test_waived_finding_carries_over_without_rejudging(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    first, _ = recheck(
        project,
        monkeypatch,
        evidence,
        [waived(ids[RESET]), verdict(ids[ENABLE], "still_open")],
        "--waive",
        ids[RESET],
        WHY,
    )
    second, invoke = recheck(
        project,
        monkeypatch,
        Path(first.detail["audit_evidence"]),
        [verdict(ids[ENABLE], "fixed", line=5)],
    )

    assert second.exit_code == 0, second.report_text
    assert ids[RESET] not in invoke.call_args.args[0].prompt
    assert statuses(second)[RESET] == "waived"
    assert (
        next(r for r in second.detail["issue_list"] if r["summary"] == RESET)["justification"]
        == WHY
    )
    assert "ACCEPTED WAIVERS" in second.report_text
    assert f"(ID: {ids[RESET]}) — reason: {WHY}" in second.report_text

    third, invoke = recheck(project, monkeypatch, Path(second.detail["audit_evidence"]), [])

    assert third.exit_code == 0, third.report_text
    invoke.assert_not_called()
    assert statuses(third) == {RESET: "waived", ENABLE: "fixed", NAMING: "advisory"}
    assert f"(ID: {ids[RESET]}) — reason: {WHY}" in third.report_text
    assert Path(third.detail["audit_evidence"]).is_file()


def test_mcp_waive_argument_runs_the_same_waiver(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, ids = two_findings(project, monkeypatch)
    argv = _params_to_argv({"recheck": str(evidence), "waive": [[ids[RESET], WHY]]})
    reviewer, _ = _reviewer(
        project,
        monkeypatch,
        argv,
        json.dumps({"verdicts": [waived(ids[RESET]), verdict(ids[ENABLE], "still_open")]}),
    )
    result = reviewer._run()

    assert statuses(result)[RESET] == "waived"
    assert "ACCEPTED WAIVERS" in result.report_text
