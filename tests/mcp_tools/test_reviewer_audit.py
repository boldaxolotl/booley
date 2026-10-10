"""Reviewer diagnostics survive result/state boundaries without affecting gates."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.criteria.freshness import evaluate_verification_freshness
from booley.criteria.state import DevelopmentState
from booley.evidence.review_dispositions import (
    collect_review_audit,
    collect_review_dispositions,
    review_report_required,
)
from booley.mcp.server import _structured_from_report
from booley.specialists.reviewer import (
    ReviewerSpecialist,
    ReviewIssue,
    _finding_record,
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
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    monkeypatch.setattr("booley.specialists.reviewer._load_ticket_document", lambda: (None, ""))
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
    endpoint.read_state()
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


def evidence(result):
    return json.loads(Path(result.detail["audit_evidence"]).read_text())["detail"]


def test_mixed_rows_preserve_raw_ordinals_and_duplicate_source_proposals(reviewer, monkeypatch):
    outside = proposal(file="../outside.sv")
    malformed = [None, {"severity": "SECRET", "summary": "<script>[link](bad)</script>"}]
    output(reviewer, monkeypatch, {"issues": [*malformed, outside, outside, proposal()]})
    result = reviewer._run()
    assert result.exit_code == 1
    assert len(result.detail["pending"]) == 1
    assert result.detail["MAJOR"] == 1
    assert [row["ordinal"] for row in result.detail["filtered"]] == [3, 4]
    assert [row["raw"] for row in result.detail["rejected"]] == malformed
    assert result.detail["filtered"][0]["finding_id"] == result.detail["filtered"][1]["finding_id"]
    assert evidence(result) == result.detail
    assert len(collect_review_dispositions(reviewer.state.criteria)) == 1
    assert len(collect_review_audit(reviewer.state.criteria)) == 4


@pytest.mark.parametrize("value", [None, 17, {"unexpected": [1, 2]}, "bad"])
def test_invalid_initial_wrapper_is_durable_error(reviewer, monkeypatch, value):
    output(reviewer, monkeypatch, {"issues": value})
    result = reviewer._run()
    assert result.exit_code == 2
    assert result.detail["rejected"][0]["raw"] == value
    assert evidence(result)["rejected"] == result.detail["rejected"]
    assert reviewer.state.is_met("review_rtl_bugs_clean") is False


def test_all_invalid_and_malformed_mirror_persist_on_error(reviewer, monkeypatch):
    output(
        reviewer,
        monkeypatch,
        {"issues": [False, {"line": "bad"}]},
        {"ReportFindings": [{"findings": [None, {}]}, {"findings": None}]},
    )
    result = reviewer._run()
    assert result.exit_code == 2
    assert len(result.detail["rejected"]) == 5
    assert {row["channel"] for row in result.detail["rejected"]} == {
        "canonical",
        "ReportFindings mirror",
    }
    assert evidence(result)["review_error"] is True


def test_source_only_audit_is_non_gating_and_surfaces_in_mcp(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [proposal(file="/outside/hostile[link].sv")]})
    result = reviewer._run()
    assert result.exit_code == 0
    assert result.detail["pending"] == []
    assert review_report_required(reviewer.state.criteria) is False
    assert len(collect_review_audit(reviewer.state.criteria)) == 1
    payload = _structured_from_report(
        {"mcp_tool": "reviewer", "exit_code": 0, "detail": result.detail}
    )
    assert payload["reports"][0]["detail"]["filtered"] == result.detail["filtered"]
    calls = []
    monkeypatch.setattr(reviewer, "_invoke_agent", calls.append)
    cached = reviewer._run()
    assert calls == []
    assert cached.detail["filtered"] == result.detail["filtered"]


@pytest.mark.parametrize("disposition", ["current", "advisory", "deferred", "out_of_scope"])
def test_explicit_disposition_survives_deferred_headings(reviewer, monkeypatch, disposition):
    monkeypatch.setattr(
        "booley.specialists.reviewer._load_ticket_text",
        lambda: ("## Deferred\n- Paraphrased requirement", "ticket"),
    )
    output(reviewer, monkeypatch, {"issues": [proposal(disposition=disposition)]})
    result = reviewer._run()
    assert result.criterion_met == (disposition != "current")
    collection = "pending" if disposition == "current" else "observations"
    assert result.detail[collection][0]["disposition"] == disposition
    assert result.detail["filtered"] == []


@pytest.mark.parametrize(
    "rows",
    [
        None,
        "bad",
        [{"index": 1, "status": "FIXED"}],
        [{"index": 1, "status": "WAIVED"}],
        [{"index": 2, "status": "FIXED", "evidence": "outside"}],
    ],
)
def test_verify_rejections_keep_obligations_and_raw_evidence(reviewer, monkeypatch, rows):
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    initial = reviewer._run()
    monkeypatch.setattr(
        reviewer,
        "_invoke_agent_with_resume",
        lambda _params: MagicMock(output=json.dumps({"findings": rows})),
    )
    result = reviewer._run()
    assert result.criterion_met is False
    assert len(result.detail["pending"]) == 1
    assert result.detail["resolved"] == []
    assert result.detail["rejected"][0]["raw"] == (rows[0] if isinstance(rows, list) else rows)
    assert result.detail["rejected"][0]["phase"] == "verification"
    assert initial.detail["rejected"] == []


def test_audit_survives_verification_and_rediscovery(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [proposal(), proposal(file="rtl/other.sv")]})
    first = reviewer._run()
    Path(reviewer.args.work_dir, "rtl/dut.sv").write_text("module dut; logic fixed; endmodule\n")
    monkeypatch.setattr(
        reviewer,
        "_invoke_agent_with_resume",
        lambda _params: MagicMock(
            output=json.dumps(
                {"findings": [{"index": 1, "status": "FIXED", "evidence": "fixed logic"}]}
            )
        ),
    )
    output(reviewer, monkeypatch, {"issues": [proposal(file="rtl/other.sv")]})
    second = reviewer._run()
    assert second.criterion_met is True
    assert len(second.detail["filtered"]) == 2
    assert len({row["attempt_id"] for row in second.detail["filtered"]}) == 2
    assert second.detail["filtered"][0]["evidence"] == first.detail["filtered"][0]["evidence"]
    original = json.loads(Path(second.detail["filtered"][0]["evidence"]).read_text())
    assert original["detail"]["filtered"][0] == first.detail["filtered"][0]
    assert len(second.detail["resolved"]) == 1


def test_failed_semantics_migration_archives_receipt_and_preserves_obligations(
    reviewer, monkeypatch
):
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    first = reviewer._run()
    entry = reviewer.state.criteria["review_rtl_bugs_clean"]
    old = entry.detail
    old["contract"].pop("filtering_semantics_revision")
    old["resolved"] = [
        {
            **proposal(summary="explicit waiver"),
            "status": "waived",
            "disposition_actor": "human",
            "justification": "accepted",
        },
        {
            **proposal(summary="policy exclusion"),
            "status": "excluded",
            "disposition_actor": "harness_policy",
        },
    ]
    reviewer.state.save()
    output(reviewer, monkeypatch, {"issues": [None]})
    failed = reviewer._run()
    assert failed.exit_code == 2
    assert failed.detail["needs_discovery"] is True
    assert failed.detail["pending"] == first.detail["pending"]
    assert failed.detail["resolved"] == old["resolved"]
    archive = json.loads(Path(failed.detail["receipt_history"][-1]).read_text())
    assert archive["resolved"] == old["resolved"]
    output(reviewer, monkeypatch, {"issues": []})
    restarted = reviewer._run()
    assert restarted.criterion_met is False
    assert restarted.detail["pending"] == first.detail["pending"]
    assert restarted.detail["resolved"] == old["resolved"]


@pytest.mark.parametrize("version", [None, 3, 4])
def test_read_only_acceptance_rejects_old_semantics_with_unchanged_source(reviewer, version):
    contract = reviewer._review_contract_detail()
    contract.pop("filtering_semantics_revision")
    detail = {
        "review_detail_version": version,
        "contract": contract,
        "_source_fingerprint": {"categories": ["rtl"], "fingerprint": {"rtl": {"digest": "same"}}},
    }
    result = evaluate_verification_freshness(
        "review_rtl_bugs_clean",
        {"met": True, "detail": detail},
        work_dir=Path(reviewer.args.work_dir),
        fingerprint_provider=lambda *_args, **_kwargs: {"rtl": {"digest": "same"}},
    )
    assert result.stale is True
    assert result.review_dimensions == ("filtering_semantics",)
    detail.pop("contract")
    assert (
        evaluate_verification_freshness(
            "review_rtl_bugs_clean",
            {"met": True, "detail": detail},
            work_dir=Path(reviewer.args.work_dir),
            fingerprint_provider=lambda *_args, **_kwargs: {},
        ).stale
        is True
    )


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


def test_more_severe_mirror_keeps_rejections_on_error(reviewer, monkeypatch):
    output(
        reviewer,
        monkeypatch,
        {"issues": [False]},
        {"ReportFindings": [{"findings": [None, {"file": "rtl/dut.sv", "summary": "bug"}]}]},
    )
    result = reviewer._run()
    assert result.exit_code == 2
    assert len(result.detail["rejected"]) == 2
    output(
        reviewer,
        monkeypatch,
        {"issues": []},
        {"ReportFindings": [{"findings": [None, {"file": "rtl/dut.sv", "summary": "bug"}]}]},
    )
    result = reviewer._run()
    assert result.exit_code == 2
    assert "more severe" in result.report_text


def test_reused_specialist_starts_audit_history_for_new_contract(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [None]})
    first = reviewer._run()
    other = Path(reviewer.args.work_dir, "rtl/other.sv")
    other.write_text("module other; endmodule\n")
    reviewer.args.scope = "rtl/other.sv"
    output(reviewer, monkeypatch, {"issues": [proposal(file="rtl/other.sv")]})
    result = reviewer._run()
    assert result.criterion_met is False
    assert result.detail["pending"][0]["file"] == "rtl/other.sv"
    assert result.detail["filtered"] == []
    assert result.detail["rejected"] == []
    archived = json.loads(Path(result.detail["receipt_history"][-1]).read_text())
    assert archived["rejected"] == first.detail["rejected"]
    assert evidence(first)["rejected"] == first.detail["rejected"]


def test_historical_policy_exclusion_does_not_suppress_fresh_finding(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": []})
    reviewer._run()
    entry = reviewer.state.criteria["review_rtl_bugs_clean"]
    entry.detail["contract"].pop("filtering_semantics_revision")
    entry.detail["resolved"] = [
        {
            **_finding_record(ReviewIssue.from_dict(proposal())),
            "status": "excluded",
            "disposition_actor": "harness_policy",
        }
    ]
    reviewer.state.save()
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    result = reviewer._run()
    assert result.criterion_met is False
    assert len(result.detail["pending"]) == 1
    assert result.detail["resolved"][0]["disposition_actor"] == "harness_policy"
    normalized = collect_review_dispositions(reviewer.state.criteria)
    assert len(normalized) == 1
    assert normalized[0]["disposition"] == "open"
    assert normalized[0]["finding_id"] == result.detail["pending"][0]["finding_id"]


@pytest.mark.parametrize("status", ["FIXED", "WAIVED"])
def test_rediscovered_pending_finding_overrides_historical_resolution(
    reviewer, monkeypatch, status
):
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    reviewer._run()
    monkeypatch.setattr(
        reviewer,
        "_invoke_agent_with_resume",
        lambda _params: MagicMock(
            output=json.dumps(
                {
                    "findings": [
                        {
                            "index": 1,
                            "status": status,
                            "evidence": "rtl/dut.sv:1 — previously corrected",
                            "justification": "Previously accepted by the user",
                        }
                    ]
                }
            )
        ),
    )
    assert reviewer._run().criterion_met is True
    entry = reviewer.state.criteria["review_rtl_bugs_clean"]
    entry.detail["contract"].pop("filtering_semantics_revision")
    reviewer.state.save()

    result = reviewer._run()

    assert result.criterion_met is False
    assert len(result.detail["pending"]) == 1
    assert result.detail["resolved"][0]["status"] == status.lower()
    normalized = collect_review_dispositions(reviewer.state.criteria)
    assert len(normalized) == 1
    assert normalized[0]["disposition"] == "open"
    assert normalized[0]["finding_id"] == result.detail["pending"][0]["finding_id"]


@pytest.mark.parametrize(
    "clause", ["Required behavior paraphrased", "Unmatched", "- Bullet text", "Multi\nline clause"]
)
def test_valid_ticket_anchors_are_not_literal_filters(reviewer, monkeypatch, clause):
    output(reviewer, monkeypatch, {"issues": [proposal(ticket_clause=clause)]})
    result = reviewer._run()
    assert result.exit_code == 1
    assert result.detail["pending"][0]["ticket_clause"] == clause


def test_error_diagnostics_accumulate_within_unchanged_contract(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [None]})
    first = reviewer._run()
    output(reviewer, monkeypatch, {"issues": [False]})
    second = reviewer._run()
    assert [row["raw"] for row in second.detail["rejected"]] == [None, False]
    assert len({row["attempt_id"] for row in second.detail["rejected"]}) == 2
    assert second.detail["rejected"][0]["evidence"] == first.detail["audit_evidence"]
    assert second.detail.get("receipt_history", []) == []


def test_evidence_write_failure_cannot_publish_passing_criterion(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": []})

    def fail_write(_path, _detail):
        raise OSError("evidence disk unavailable")

    monkeypatch.setattr("booley.specialists.reviewer.atomic_write_json", fail_write)
    with pytest.raises(OSError, match="evidence disk unavailable"):
        reviewer._run()
    persisted = DevelopmentState.load(Path(reviewer.args.state_file))
    assert persisted.is_met("review_rtl_bugs_clean") is False


def test_passing_criterion_already_references_durable_evidence(reviewer, monkeypatch):
    original_set = reviewer.set_criterion
    published = []

    def inspect_publication(key, met, *, detail):
        if met:
            assert json.loads(Path(detail["audit_evidence"]).read_text())["detail"] == detail
            published.append(key)
        original_set(key, met, detail=detail)

    monkeypatch.setattr(reviewer, "set_criterion", inspect_publication)
    output(reviewer, monkeypatch, {"issues": []})
    result = reviewer._run()
    assert result.criterion_met is True
    assert published == ["review_rtl_bugs_clean"]


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
    config_dir = root / ".booley_project"
    config_dir.mkdir()
    (config_dir / "booley.toml").write_text(
        '[flows.sim]\npass_sentinels = ["CUSTOM PASS"]\ntrace_files = ["custom.vcd"]\n'
    )
    reviewer.args.scope = "tb/dut_tb.sv"
    reviewer.args.category = "tb"
    reviewer.args.focus = "quality"
    reviewer.state.init_criteria({"review_tb_quality_clean": True})
    reviewer.state.save()
    output(
        reviewer,
        monkeypatch,
        {"issues": [proposal(category="quality", file="tb/dut_tb.sv", summary=summary)]},
    )
    result = reviewer._run()
    assert result.criterion_met is False
    assert result.detail["pending"][0]["summary"] == summary
    assert result.detail["filtered"] == []
    prompt = reviewer._build_prompt()
    assert "CUSTOM PASS" in prompt
    assert "custom.vcd" in prompt


@pytest.mark.parametrize("mode", ["done", "clean"])
@pytest.mark.parametrize("disposition", ["current", "advisory", "deferred", "out_of_scope"])
def test_real_reviewer_output_roundtrips_package(reviewer, monkeypatch, mode, disposition):
    from booley.review.artifact import ReviewPackage
    from tests.review.test_artifact import _package

    reviewer.state.criteria.clear()
    key = f"review_rtl_bugs_{mode}"
    reviewer.state.init_criteria({key: True})
    output(reviewer, monkeypatch, {"issues": [proposal(disposition=disposition)]})
    result = reviewer._run()
    rows = collect_review_dispositions({key: {"detail": result.detail}})
    package = ReviewPackage.parse({**_package(), "review_dispositions": rows})
    roundtrip = ReviewPackage.parse(package.to_dict())
    row = roundtrip.review_dispositions[0]
    assert row.disposition == ("open" if disposition == "current" else "reported")
    assert row.reviewer_disposition == disposition


@pytest.mark.parametrize("mode", ["done", "clean"])
def test_real_history_preserves_advisory_under_superseded(reviewer, monkeypatch, mode):
    from booley.review.artifact import ReviewPackage
    from tests.review.test_artifact import _package

    reviewer.state.criteria.clear()
    key = f"review_rtl_bugs_{mode}"
    reviewer.state.init_criteria({key: True})
    output(reviewer, monkeypatch, {"issues": [proposal(disposition="advisory")]})
    first = reviewer._run()
    assert first.exit_code == 0
    Path(reviewer.args.work_dir, "rtl/dut.sv").write_text("module dut; wire changed; endmodule\n")
    output(reviewer, monkeypatch, {"issues": []})
    monkeypatch.setattr(
        reviewer,
        "_invoke_agent_with_resume",
        lambda _params: MagicMock(output=json.dumps({"findings": []})),
    )
    second = reviewer._run()
    rows = collect_review_dispositions({key: {"detail": second.detail}})
    assert rows[0]["status"] == "superseded"
    package = ReviewPackage.parse({**_package(), "review_dispositions": rows})
    row = ReviewPackage.parse(package.to_dict()).review_dispositions[0]
    assert row.disposition == "reported"
    assert row.reviewer_disposition == "advisory"


def test_fresh_superseded_proposal_is_rejected(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [proposal(disposition="superseded")]})
    result = reviewer._run()
    assert result.exit_code == 2
    assert result.detail["rejected"]


@pytest.mark.parametrize("payload, code", [({"issues": []}, 0), ({"issues": None}, 2)])
def test_actual_reviewer_evidence_keeps_versioned_project_clean(
    reviewer, monkeypatch, payload, code
):
    from booley.runtime import project_dir
    from tests.runtime.test_project_runtime_gitignore import assert_clean, initialize_repository

    root = Path(reviewer.args.work_dir)
    data = root / ".booley_project"
    data.mkdir()
    (data / "booley.toml").write_text('[project]\nname = "fixture"\n')
    state_path = data / "goals" / "audit" / "state.json"
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    reviewer.state._file_path = state_path
    reviewer.args.state_file = state_path
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    reviewer.state.save()
    initialize_repository(root, data, monkeypatch)
    project_dir.reset_cache()
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
        project_dir.reset_cache()
