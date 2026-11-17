"""Reviewer diagnostics survive result/state boundaries without affecting gates."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.evidence.review_dispositions import (
    collect_review_dispositions,
)
from booley.evidence.review_vocabulary import ORIGIN_PRE_EXISTING
from booley.mcp.server import _structured_from_report
from booley.specialists.reviewer import (
    ReviewerSpecialist,
    parse_review_output,
)


def proposal(**overrides):
    return {
        "severity": "MAJOR",
        "confidence": "HIGH",
        "category": "bugs",
        "kind": "code_defect",
        "disposition": "current",
        "ticket_clause": "Paraphrased requirement",
        "file": "rtl/dut.sv",
        "line": 1,
        "summary": "Missing [SIM_RESULT] sentinel; remove all $dumpvars",
        **overrides,
    }


@pytest.fixture
def reviewer(tmp_path, monkeypatch):
    source = tmp_path / "rtl/dut.sv"
    source.parent.mkdir()
    source.write_text("module dut; endmodule\n")
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    endpoint = ReviewerSpecialist()
    endpoint.parse_args(
        [
            "--work-dir",
            str(tmp_path),
            "--scope",
            "rtl/dut.sv",
            "--category",
            "rtl",
            "--focus",
            "bugs",
        ]
    )
    monkeypatch.setattr(
        endpoint,
        "_invoke_agent",
        lambda _params: MagicMock(
            output=json.dumps({"issues": []}), captured_agent_capability_calls={}
        ),
    )
    return endpoint


def output(reviewer, monkeypatch, payload, mirror=None):
    monkeypatch.setattr(
        reviewer,
        "_invoke_agent",
        lambda _params: MagicMock(
            output=json.dumps(payload), captured_agent_capability_calls=mirror or {}
        ),
    )


GUIDES = ("bugs-checklist", "bugs-hunt")  # rtl/bugs runs one call per guide, same canned answer


def by_guide(rows):
    """Group audit rows by the guide call that produced them."""
    return {guide: [row for row in rows if row["guide"] == guide] for guide in GUIDES}


def evidence(result):
    return json.loads(Path(result.detail["audit_evidence"]).read_text())["detail"]


def open_rows(detail):
    """The corrective findings of a review detail."""
    return [
        row
        for row in detail["issue_list"]
        if row["disposition"] == "current" and row.get("origin") != ORIGIN_PRE_EXISTING
    ]


def assert_run_evidence(result):
    path = Path(result.detail["audit_evidence"])
    assert len(list(path.parent.glob("*.json"))) == 1, "one evidence file per run"
    assert result.detail["artifacts"]["reviewer_evidence"] == str(path)
    assert json.loads(path.read_text()) == {"exit_code": result.exit_code, "detail": result.detail}
    return path


def test_mixed_rows_preserve_raw_ordinals_and_duplicate_source_proposals(reviewer, monkeypatch):
    outside = proposal(file="../outside.sv")
    malformed = [None, {"severity": "SECRET", "summary": "<script>[link](bad)</script>"}]
    output(reviewer, monkeypatch, {"issues": [*malformed, outside, outside, proposal()]})
    result = reviewer._run()
    assert result.exit_code == 0
    assert len(open_rows(result.detail)) == 1
    assert result.detail["MAJOR"] == 1
    # Each guide call audits the same canned answer: rows are per call, ordinals stay raw.
    for guide, rows in by_guide(result.detail["filtered"]).items():
        assert [row["ordinal"] for row in rows] == [3, 4], guide
        assert rows[0]["finding_id"] == rows[1]["finding_id"], guide
    for guide, rows in by_guide(result.detail["rejected"]).items():
        assert [row["raw"] for row in rows] == malformed, guide
    assert evidence(result) == result.detail


@pytest.mark.parametrize("value", [None, 17, {"unexpected": [1, 2]}, "bad"])
def test_invalid_initial_wrapper_is_durable_error(reviewer, monkeypatch, value):
    output(reviewer, monkeypatch, {"issues": value})
    result = reviewer._run()
    assert result.exit_code == 2
    assert result.detail["rejected"][0]["raw"] == value
    assert evidence(result)["rejected"] == result.detail["rejected"]


def test_all_invalid_and_malformed_mirror_persist_on_error(reviewer, monkeypatch):
    output(
        reviewer,
        monkeypatch,
        {"issues": [False, {"line": "bad"}]},
        {"ReportFindings": [{"findings": [None, {}]}, {"findings": None}]},
    )
    result = reviewer._run()
    assert result.exit_code == 2
    assert len(result.detail["rejected"]) == 5 * 2 * len(
        GUIDES
    )  # 5 rows per attempt, two attempts per guide call
    assert {row["channel"] for row in result.detail["rejected"]} == {
        "canonical",
        "ReportFindings mirror",
    }
    assert evidence(result)["review_error"] is True


def test_source_only_audit_is_non_gating_and_surfaces_in_mcp(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [proposal(file="/outside/hostile[link].sv")]})
    result = reviewer._run()
    assert result.exit_code == 0
    assert open_rows(result.detail) == []
    assert [len(rows) for rows in by_guide(result.detail["filtered"]).values()] == [1, 1]
    assert result.detail["rejected"] == []
    payload = _structured_from_report(
        {"mcp_tool": "reviewer", "exit_code": 0, "detail": result.detail}
    )
    assert payload["reports"][0]["detail"]["filtered"] == result.detail["filtered"]


@pytest.mark.parametrize("disposition", ["current", "advisory", "deferred", "out_of_scope"])
def test_explicit_disposition_survives_deferred_headings(reviewer, monkeypatch, disposition):
    spec = Path(reviewer.args.work_dir) / "requirements.md"
    spec.write_text("## Deferred\n- Paraphrased requirement", encoding="utf-8")
    reviewer.args.spec = str(spec)
    output(reviewer, monkeypatch, {"issues": [proposal(disposition=disposition)]})
    result = reviewer._run()
    assert bool(open_rows(result.detail)) == (disposition == "current")
    assert result.detail["issue_list"][0]["disposition"] == disposition
    assert result.detail["filtered"] == []


def test_parser_preserves_canonical_ordinal_without_changing_identity():
    parsed = parse_review_output(json.dumps({"issues": [False, proposal()]}), "bugs")
    assert parsed.issues[0].proposal_ordinal == 2
    assert "proposal_ordinal" not in parsed.issues[0].to_dict()


def test_mcp_truncation_keeps_exact_evidence_pointer(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [{"malformed": "x" * 100000}]})
    result = reviewer._run()
    payload = _structured_from_report(
        {"mcp_tool": "reviewer", "exit_code": 2, "detail": result.detail}
    )
    assert payload["truncated"] is True
    assert payload["artifacts"]["reviewer_evidence"] == result.detail["audit_evidence"]


def test_missing_json_remains_error_with_durable_empty_audit(reviewer, monkeypatch):
    monkeypatch.setattr(
        reviewer,
        "_invoke_agent",
        lambda _params: MagicMock(output="No JSON here", captured_agent_capability_calls={}),
    )
    result = reviewer._run()
    assert result.exit_code == 2
    assert result.detail["rejected"] == []
    assert evidence(result)["review_error"] is True


def test_disagreeing_mirror_keeps_rejections_on_error(reviewer, monkeypatch):
    output(
        reviewer,
        monkeypatch,
        {"issues": [False]},
        {"ReportFindings": [{"findings": [None, {"file": "rtl/dut.sv", "summary": "bug"}]}]},
    )
    result = reviewer._run()
    assert result.exit_code == 2
    assert len(result.detail["rejected"]) == 2 * 2 * len(
        GUIDES
    )  # 2 rows per attempt, two attempts per guide call
    output(
        reviewer,
        monkeypatch,
        {"issues": []},
        {"ReportFindings": [{"findings": [None, {"file": "rtl/dut.sv", "summary": "bug"}]}]},
    )
    result = reviewer._run()
    assert result.exit_code == 2
    assert "leaves out or rates lower" in result.report_text


def test_reused_specialist_starts_audit_history_for_new_contract(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [None]})
    first = reviewer._run()
    other = Path(reviewer.args.work_dir, "rtl/other.sv")
    other.write_text("module other; endmodule\n")
    reviewer.args.scope = "rtl/other.sv"
    output(reviewer, monkeypatch, {"issues": [proposal(file="rtl/other.sv")]})
    result = reviewer._run()
    assert open_rows(result.detail)[0]["file"] == "rtl/other.sv"
    assert result.detail["filtered"] == []
    assert result.detail["rejected"] == []
    assert result.detail["audit_evidence"] != first.detail["audit_evidence"]
    assert evidence(first)["rejected"] == first.detail["rejected"]


@pytest.mark.parametrize(
    "clause", ["Required behavior paraphrased", "Unmatched", "- Bullet text", "Multi\nline clause"]
)
def test_valid_ticket_anchors_are_not_literal_filters(reviewer, monkeypatch, clause):
    output(reviewer, monkeypatch, {"issues": [proposal(ticket_clause=clause)]})
    result = reviewer._run()
    assert result.exit_code == 0
    assert open_rows(result.detail)[0]["ticket_clause"] == clause


def test_evidence_write_failure_is_not_swallowed(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": []})

    def fail_write(_path, _detail):
        raise OSError("evidence disk unavailable")

    monkeypatch.setattr("booley.specialists.reviewer.atomic_write_json", fail_write)
    with pytest.raises(OSError, match="evidence disk unavailable"):
        reviewer._run()


@pytest.mark.parametrize("late_error", [False, True], ids=["normal", "late-error"])
def test_recorded_review_uses_one_evidence_file(reviewer, monkeypatch, late_error):
    from booley.specialists import reviewer as module

    write = MagicMock(wraps=module.atomic_write_json)
    monkeypatch.setattr(module, "atomic_write_json", write)
    original_execute = reviewer._execute_review

    def finish_execution():
        result = original_execute()
        if late_error:
            result.exit_code = 2
            reviewer._audit["rejected"].append({"raw": "late error"})
        return result

    monkeypatch.setattr(reviewer, "_execute_review", finish_execution)
    output(reviewer, monkeypatch, {"issues": []})
    result = reviewer._run()
    assert (result.exit_code == 2) is late_error
    path = assert_run_evidence(result)
    assert [call.args[0] for call in write.call_args_list] == [path] * (2 if late_error else 1)
    if late_error:
        assert result.exit_code == 2
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
    reviewer, monkeypatch, summary
):
    root = Path(reviewer.args.work_dir)
    source = root / "tb/dut_tb.sv"
    source.parent.mkdir()
    source.write_text("module dut_tb; endmodule\n")
    config_dir = root / ".booley"
    config_dir.mkdir()
    (config_dir / "booley.toml").write_text(
        '[flows.sim]\npass_sentinels = ["CUSTOM PASS"]\ntrace_files = ["custom.vcd"]\n'
    )
    reviewer.args.scope = "tb/dut_tb.sv"
    reviewer.args.category = "tb"
    reviewer.args.focus = "quality"
    output(
        reviewer,
        monkeypatch,
        {"issues": [proposal(category="quality", file="tb/dut_tb.sv", summary=summary)]},
    )
    result = reviewer._run()
    assert open_rows(result.detail)[0]["summary"] == summary
    assert result.detail["filtered"] == []
    prompt = reviewer._build_prompt()
    assert "CUSTOM PASS" in prompt
    assert "custom.vcd" in prompt


def test_interactive_discovery_replaces_prior_advisory(reviewer, monkeypatch):
    key = "review_rtl_bugs_done"
    output(reviewer, monkeypatch, {"issues": [proposal(disposition="advisory")]})
    first = reviewer._run()
    assert first.exit_code == 0
    assert collect_review_dispositions({key: {"detail": first.detail}})
    output(reviewer, monkeypatch, {"issues": []})
    second = reviewer._run()
    assert collect_review_dispositions({key: {"detail": second.detail}}) == []


def test_fresh_superseded_proposal_is_rejected(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [proposal(disposition="superseded")]})
    result = reviewer._run()
    assert result.exit_code == 2
    assert result.detail["rejected"]


@pytest.mark.parametrize("payload, code", [({"issues": []}, 0), ({"issues": None}, 2)])
def test_actual_reviewer_evidence_keeps_versioned_project_clean(
    reviewer, monkeypatch, payload, code
):
    from booley.runtime import booley_dir
    from tests.project_runtime_git_support import assert_clean, initialize_repository

    root = Path(reviewer.args.work_dir)
    data = root / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text('[project]\nname = "fixture"\n')
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    initialize_repository(root, data, monkeypatch)
    booley_dir.reset_cache()
    try:
        output(reviewer, monkeypatch, payload)
        result = reviewer._run()
        assert result.exit_code == code
        path = Path(result.detail["audit_evidence"])
        assert path.parent == data / "reviewer-evidence"
        assert path.is_file()
        assert evidence(result) == result.detail
        assert_clean(root)
    finally:
        booley_dir.reset_cache()
