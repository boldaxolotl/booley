"""Reviewer diagnostics survive result/state boundaries without affecting gates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from booley.evidence.review_dispositions import collect_review_dispositions
from booley.mcp.report_encoding import ReportEncoder
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.specialists.reviewer import ReviewerSpecialist, parse_review_output
from tests.mcp_tools.reviewer_support import (
    ScriptedAgent,
    install_agent,
    issue,
    open_findings,
    run_review,
)

GUIDES = ("bugs-checklist", "bugs-hunt")  # rtl/bugs runs one call per guide, same canned answer


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> ScriptedAgent:
    return install_agent(monkeypatch)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    return tmp_path


def argv(
    project: Path,
    *extra: str,
    scope: str = "rtl/dut.sv",
    category: str = "rtl",
    focus: str = "bugs",
) -> list[str]:
    return [
        *("--work-dir", str(project), "--scope", scope, "--category", category),
        *("--focus", focus, *extra),
    ]


def by_guide(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group audit rows by the guide call that produced them."""
    return {guide: [row for row in rows if row["guide"] == guide] for guide in GUIDES}


def evidence(result: EndpointOutcome) -> dict[str, Any]:
    return json.loads(Path(result.detail["audit_evidence"]).read_text(encoding="utf-8"))["detail"]


def assert_run_evidence(result: EndpointOutcome) -> Path:
    path = Path(result.detail["audit_evidence"])
    assert len(list(path.parent.glob("*.json"))) == 1, "one evidence file per run"
    assert result.detail["artifacts"]["reviewer_evidence"] == str(path)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"exit_code": result.exit_code, "detail": result.detail}
    return path


def test_mixed_rows_preserve_raw_ordinals_and_duplicate_source_proposals(
    project: Path, agent: ScriptedAgent
) -> None:
    outside = issue(file="../outside.sv")
    malformed = [None, {"severity": "SECRET", "summary": "<script>[link](bad)</script>"}]
    agent.answer({"issues": [*malformed, outside, outside, issue()]})
    result, _ = run_review(argv(project))
    assert result.exit_code == 0
    assert len(open_findings(result.detail)) == 1
    assert result.detail["MAJOR"] == 1
    # Each guide call audits the same canned answer: rows are per call, ordinals stay raw.
    for guide, rows in by_guide(result.detail["filtered"]).items():
        assert [row["ordinal"] for row in rows] == [3, 4], guide
        assert rows[0]["finding_id"] == rows[1]["finding_id"], guide
    for guide, rows in by_guide(result.detail["rejected"]).items():
        assert [row["raw"] for row in rows] == malformed, guide
    assert evidence(result) == result.detail


@pytest.mark.parametrize("value", [None, 17, {"unexpected": [1, 2]}, "bad"])
def test_invalid_initial_wrapper_is_durable_error(
    project: Path, agent: ScriptedAgent, value: object
) -> None:
    agent.answer({"issues": value})
    result, _ = run_review(argv(project))
    assert result.exit_code == 2
    assert result.detail["rejected"][0]["raw"] == value
    assert evidence(result)["rejected"] == result.detail["rejected"]


def test_all_invalid_and_malformed_mirror_persist_on_error(
    project: Path, agent: ScriptedAgent
) -> None:
    mirror = agent.reported(
        {"findings": [None, {}]},
        {"findings": None},
        output=json.dumps({"issues": [False, {"line": "bad"}]}),
    )
    agent.answer(mirror)
    result, _ = run_review(argv(project))
    assert result.exit_code == 2
    # 5 rows per attempt, two attempts per guide call
    assert len(result.detail["rejected"]) == 5 * 2 * len(GUIDES)
    channels = {row["channel"] for row in result.detail["rejected"]}
    assert channels == {"canonical", "ReportFindings mirror"}
    assert evidence(result)["review_error"] is True


def test_source_only_audit_is_non_gating_and_surfaces_in_mcp(
    project: Path, agent: ScriptedAgent
) -> None:
    agent.answer({"issues": [issue(file="/outside/hostile[link].sv")]})
    result, _ = run_review(argv(project))
    assert result.exit_code == 0
    assert open_findings(result.detail) == []
    assert [len(rows) for rows in by_guide(result.detail["filtered"]).values()] == [1, 1]
    assert result.detail["rejected"] == []
    report = {"mcp_tool": "reviewer", "exit_code": 0, "detail": result.detail}
    payload = ReportEncoder.structured_report(report)
    assert payload["reports"][0]["detail"]["filtered"] == result.detail["filtered"]


@pytest.mark.parametrize("disposition", ["current", "advisory", "deferred", "out_of_scope"])
def test_explicit_disposition_survives_deferred_headings(
    project: Path, agent: ScriptedAgent, disposition: str
) -> None:
    spec = project / "requirements.md"
    spec.write_text("## Deferred\n- Paraphrased requirement", encoding="utf-8")
    finding = issue(disposition=disposition, ticket_clause="Paraphrased requirement")
    agent.answer({"issues": [finding]})
    result, _ = run_review(argv(project, "--spec", str(spec)))
    assert bool(open_findings(result.detail)) == (disposition == "current")
    assert result.detail["issue_list"][0]["disposition"] == disposition
    assert result.detail["filtered"] == []


def test_parser_preserves_canonical_ordinal_without_changing_identity() -> None:
    parsed = parse_review_output(json.dumps({"issues": [False, issue()]}), "bugs")
    assert parsed.issues[0].proposal_ordinal == 2
    assert "proposal_ordinal" not in parsed.issues[0].to_dict()


def test_mcp_truncation_keeps_exact_evidence_pointer(project: Path, agent: ScriptedAgent) -> None:
    agent.answer({"issues": [{"malformed": "x" * 100000}]})
    result, _ = run_review(argv(project))
    report = {"mcp_tool": "reviewer", "exit_code": 2, "detail": result.detail}
    payload = ReportEncoder.structured_report(report)
    assert payload["truncated"] is True
    assert payload["artifacts"]["reviewer_evidence"] == result.detail["audit_evidence"]


def test_missing_json_remains_error_with_durable_empty_audit(
    project: Path, agent: ScriptedAgent
) -> None:
    agent.answer("No JSON here")
    result, _ = run_review(argv(project))
    assert result.exit_code == 2
    assert result.detail["rejected"] == []
    assert evidence(result)["review_error"] is True


def test_disagreeing_mirror_keeps_rejections_on_error(project: Path, agent: ScriptedAgent) -> None:
    mirror_rows = {"findings": [None, {"file": "rtl/dut.sv", "summary": "bug"}]}
    agent.answer(agent.reported(mirror_rows, output=json.dumps({"issues": [False]})))
    result, _ = run_review(argv(project))
    assert result.exit_code == 2
    # 2 rows per attempt, two attempts per guide call
    assert len(result.detail["rejected"]) == 2 * 2 * len(GUIDES)
    agent.answer(agent.reported(mirror_rows, output=json.dumps({"issues": []})))
    result, _ = run_review(argv(project))
    assert result.exit_code == 2
    assert "leaves out or rates lower" in result.report_text


def test_reused_specialist_starts_audit_history_for_new_contract(
    project: Path, agent: ScriptedAgent
) -> None:
    """Regression of PR #852: a second run on one Reviewer must not inherit the first audit."""
    agent.answer({"issues": [None]})
    first, reviewer = run_review(argv(project))
    (project / "rtl/other.sv").write_text("module other; endmodule\n", encoding="utf-8")
    agent.answer({"issues": [issue(file="rtl/other.sv")]})
    result, _ = run_review(argv(project, scope="rtl/other.sv"), endpoint=reviewer)
    assert open_findings(result.detail)[0]["file"] == "rtl/other.sv"
    assert result.detail["filtered"] == []
    assert result.detail["rejected"] == []
    assert result.detail["audit_evidence"] != first.detail["audit_evidence"]
    assert evidence(first)["rejected"] == first.detail["rejected"]


@pytest.mark.parametrize(
    "clause", ["Required behavior paraphrased", "Unmatched", "- Bullet text", "Multi\nline clause"]
)
def test_valid_ticket_anchors_are_not_literal_filters(
    project: Path, agent: ScriptedAgent, clause: str
) -> None:
    agent.answer({"issues": [issue(ticket_clause=clause)]})
    result, _ = run_review(argv(project))
    assert result.exit_code == 0
    assert open_findings(result.detail)[0]["ticket_clause"] == clause


def test_evidence_write_failure_is_not_swallowed(
    project: Path, agent: ScriptedAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_write(_path: Path, _detail: dict[str, Any]) -> None:
        raise OSError("evidence disk unavailable")

    monkeypatch.setattr("booley.specialists.reviewer.atomic_write_json", fail_write)
    with pytest.raises(OSError, match="evidence disk unavailable"):
        run_review(argv(project))


@pytest.mark.parametrize("late_error", [False, True], ids=["normal", "late-error"])
def test_recorded_review_uses_one_evidence_file(
    project: Path, agent: ScriptedAgent, monkeypatch: pytest.MonkeyPatch, late_error: bool
) -> None:
    from booley.specialists import reviewer as module

    write = MagicMock(wraps=module.atomic_write_json)
    monkeypatch.setattr(module, "atomic_write_json", write)
    reviewer = ReviewerSpecialist()
    # Private seam: a late error after the first evidence write has no public trigger, and
    # the test pins that the evidence file is rewritten in place rather than duplicated.
    original_execute = reviewer._execute_review

    def finish_execution() -> EndpointOutcome:
        result = original_execute()
        if late_error:
            result.exit_code = 2
            reviewer._audit["rejected"].append({"raw": "late error"})
        return result

    monkeypatch.setattr(reviewer, "_execute_review", finish_execution)
    result, _ = run_review(argv(project), endpoint=reviewer)
    assert (result.exit_code == 2) is late_error
    path = assert_run_evidence(result)
    assert [call.args[0] for call in write.call_args_list] == [path] * (2 if late_error else 1)
    if late_error:
        assert result.detail["review_error"] is True
        assert result.detail["rejected"] == [{"raw": "late error"}]


@pytest.mark.parametrize(
    "summary",
    [
        "Missing [SIM_RESULT] sentinel; must emit it",
        "Remove all user-authored $dumpfile/$dumpvars calls; forbidden",
    ],
)
def test_configured_tb_policy_does_not_discard_valid_current_finding(
    project: Path, agent: ScriptedAgent, summary: str
) -> None:
    (project / "tb").mkdir()
    (project / "tb/dut_tb.sv").write_text("module dut_tb; endmodule\n", encoding="utf-8")
    (project / ".booley").mkdir()
    (project / ".booley/booley.toml").write_text(
        '[flows.sim]\npass_sentinels = ["CUSTOM PASS"]\ntrace_files = ["custom.vcd"]\n',
        encoding="utf-8",
    )
    finding = issue(category="quality", file="tb/dut_tb.sv", summary=summary)
    agent.answer({"issues": [finding]})
    result, _ = run_review(argv(project, scope="tb/dut_tb.sv", category="tb", focus="quality"))
    assert open_findings(result.detail)[0]["summary"] == summary
    assert result.detail["filtered"] == []
    prompt = agent.calls[0].prompt
    assert "CUSTOM PASS" in prompt
    assert "custom.vcd" in prompt


def test_interactive_discovery_replaces_prior_advisory(
    project: Path, agent: ScriptedAgent
) -> None:
    key = "review_rtl_bugs_done"
    agent.answer({"issues": [issue(disposition="advisory")]})
    first, _ = run_review(argv(project))
    assert first.exit_code == 0
    assert collect_review_dispositions({key: {"detail": first.detail}})
    agent.answer({"issues": []})
    second, _ = run_review(argv(project))
    assert collect_review_dispositions({key: {"detail": second.detail}}) == []


def test_fresh_superseded_proposal_is_rejected(project: Path, agent: ScriptedAgent) -> None:
    agent.answer({"issues": [issue(disposition="superseded")]})
    result, _ = run_review(argv(project))
    assert result.exit_code == 2
    assert result.detail["rejected"]


@pytest.mark.parametrize("payload, code", [({"issues": []}, 0), ({"issues": None}, 2)])
def test_actual_reviewer_evidence_keeps_versioned_project_clean(
    project: Path,
    agent: ScriptedAgent,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    code: int,
) -> None:
    from booley.runtime import booley_dir
    from tests.project_runtime_git_support import assert_clean, initialize_repository

    data = project / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text('[project]\nname = "fixture"\n', encoding="utf-8")
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    initialize_repository(project, data, monkeypatch)
    booley_dir.reset_cache()
    try:
        agent.answer(payload)
        result, _ = run_review(argv(project))
        assert result.exit_code == code
        path = Path(result.detail["audit_evidence"])
        assert path.parent == data / "reviewer-evidence"
        assert path.is_file()
        assert evidence(result) == result.detail
        assert_clean(project)
    finally:
        booley_dir.reset_cache()
