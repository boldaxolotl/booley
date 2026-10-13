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
    spec = Path(reviewer.args.work_dir) / "requirements.md"
    spec.write_text("## Deferred\n- Paraphrased requirement", encoding="utf-8")
    reviewer.args.spec = str(spec)
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
    contract_passes(reviewer, monkeypatch, [{"index": 1, "status": "STILL_PRESENT"}], [None])
    failed = reviewer._run()
    assert failed.exit_code == 2
    assert failed.detail["needs_discovery"] is True
    assert failed.detail["pending"] == first.detail["pending"]
    assert failed.detail["resolved"] == old["resolved"]
    archive = json.loads(Path(failed.detail["receipt_history"][-1]).read_text())
    assert archive["resolved"] == old["resolved"]
    calls = contract_passes(reviewer, monkeypatch, [{"index": 1, "status": "STILL_PRESENT"}], [])
    restarted = reviewer._run()
    assert restarted.exit_code == 1
    assert [phase for phase, _ in calls] == ["verify", "discovery"]
    assert restarted.criterion_met is False
    assert restarted.detail["pending"] == [
        {**first.detail["pending"][0], "status": "still_present"}
    ]
    assert restarted.detail["resolved"] == old["resolved"]
    assert "Reviewer evidence contract changed; restarting discovery." in restarted.report_text


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
    from tests.project_runtime_git_support import assert_clean, initialize_repository

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


def contract_passes(reviewer, monkeypatch, statuses, discoveries):
    calls = []

    def verify(params):
        calls.append(("verify", params))
        return MagicMock(output=json.dumps({"findings": statuses}))

    def discover(params):
        calls.append(("discovery", params))
        return MagicMock(
            output=json.dumps({"issues": discoveries}), captured_agent_capability_calls={}
        )

    monkeypatch.setattr(reviewer, "_invoke_agent_with_resume", verify)
    monkeypatch.setattr(reviewer, "_invoke_agent", discover)
    return calls


def fixed_finding(index=1):
    return {"index": index, "status": "FIXED", "evidence": "rtl/dut.sv:1 — corrected logic"}


@pytest.mark.parametrize("change", ["steer", "decisions", "spec", "ticket"])
def test_contract_change_verifies_fixed_findings_then_discovers(
    reviewer, monkeypatch, change, capsys
):
    root = Path(reviewer.args.work_dir)
    logs = root / "logs"
    logs.mkdir()
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    document = logs / {
        "decisions": "answered_questions.md",
        "spec": "spec.md",
        "ticket": "ticket.md",
    }.get(change, "unused.md")
    document.write_text("Original contract\n")
    if change == "spec":
        reviewer.args.spec = str(document)
        reviewer.args.focus = "spec"
        reviewer.state.init_criteria({"review_rtl_spec_clean": True})
        reviewer.state.save()
    output(reviewer, monkeypatch, {"issues": [proposal(category=reviewer.args.focus)]})
    reviewer._run()
    capsys.readouterr()
    (root / "rtl/dut.sv").write_text("module dut; logic fixed; endmodule\n")
    if change == "steer":
        reviewer.args.steer = ["New context"]
    else:
        document.write_text("Updated contract\n")
    calls = contract_passes(reviewer, monkeypatch, [fixed_finding()], [])

    result = reviewer._run()

    assert [phase for phase, _ in calls] == ["verify", "discovery"]
    assert result.exit_code == 0
    assert result.criterion_met is True
    assert result.detail["pending"] == []
    assert result.detail["resolved"][0]["status"] == "fixed"
    assert result.detail["verify_attempts"] == 0
    assert "total_verify_cycles" not in result.detail
    assert "needs_discovery" not in result.detail
    restart = "Reviewer scope or context changed; restarting discovery."
    assert restart in result.report_text
    assert restart in result.display_lines
    assert result.report_text.index("[review-verify]") < result.report_text.index("new contract")
    assert result.report_text.count("RESULT:") == 1
    assert restart in capsys.readouterr().out
    if change != "steer":
        assert "Updated contract" in calls[0][1].prompt
    if change == "ticket":
        assert "Updated contract" in calls[1][1].prompt


def test_contract_change_merges_verified_and_discovered_findings_by_id(reviewer, monkeypatch):
    old = proposal(summary="Old open finding")
    output(reviewer, monkeypatch, {"issues": [old, proposal(summary="Fixed finding")]})
    reviewer._run()
    reviewer.args.steer = ["Changed context"]
    calls = contract_passes(
        reviewer,
        monkeypatch,
        [{"index": 1, "status": "STILL_PRESENT"}, fixed_finding(2)],
        [old, proposal(severity="MINOR", summary="New finding")],
    )

    result = reviewer._run()

    assert [phase for phase, _ in calls] == ["verify", "discovery"]
    assert result.exit_code == 1
    assert len(result.detail["pending"]) == 2
    assert len({row["finding_id"] for row in result.detail["pending"]}) == 2
    assert result.detail["MAJOR"] == 1
    assert result.detail["MINOR"] == 1
    assert len(result.detail["resolved"]) == 1
    assert result.report_text.count("RESULT:") == 1
    for text in [result.report_text, "\n".join(result.display_lines)]:
        assert text.count("Old open finding") == 1
        assert text.count("New finding") == 1
        assert "1 major, 1 minor" in text


def test_narrowed_scope_still_verifies_prior_finding_outside_scope(reviewer, monkeypatch):
    root = Path(reviewer.args.work_dir)
    (root / "rtl/other.sv").write_text("module other; endmodule\n")
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    reviewer._run()
    reviewer.args.scope = "rtl/other.sv"
    calls = contract_passes(reviewer, monkeypatch, [{"index": 1, "status": "STILL_PRESENT"}], [])

    result = reviewer._run()

    assert [phase for phase, _ in calls] == ["verify", "discovery"]
    assert "rtl/dut.sv:1" in calls[0][1].prompt
    assert "outside" in calls[0][1].prompt
    assert "rtl/other.sv" in calls[1][1].prompt
    assert result.detail["pending"][0]["file"] == "rtl/dut.sv"
    assert result.detail["pending"][0]["status"] == "still_present"
    assert "Missing [SIM_RESULT]" in result.report_text


@pytest.mark.parametrize("status", ["FIXED", "WAIVED"])
def test_new_contract_discovery_reopens_same_verified_finding(reviewer, monkeypatch, status):
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    first = reviewer._run()
    reviewer.args.steer = ["Changed context"]
    disposition = {
        **fixed_finding(),
        "status": status,
        "justification": "Previously accepted policy",
    }
    contract_passes(reviewer, monkeypatch, [disposition], [proposal()])

    result = reviewer._run()

    assert result.criterion_met is False
    assert result.detail["pending"][0]["finding_id"] == first.detail["pending"][0]["finding_id"]
    assert result.detail["resolved"][0]["status"] == status.lower()
    assert collect_review_dispositions(reviewer.state.criteria)[0]["disposition"] == "open"
    assert "[WAIVED" not in result.report_text
    assert "[WAIVED" not in "\n".join(result.display_lines)


@pytest.mark.parametrize("remaining", [False, True])
def test_changed_contract_bypasses_and_resets_exhausted_budgets(reviewer, monkeypatch, remaining):
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    reviewer._run()
    entry = reviewer.state.criteria["review_rtl_bugs_clean"]
    entry.detail.update(verify_attempts=2, total_verify_cycles=3)
    reviewer.state.save()
    reviewer.args.steer = ["New contract"]
    statuses = [{"index": 1, "status": "STILL_PRESENT"}] if remaining else [fixed_finding()]
    calls = contract_passes(reviewer, monkeypatch, statuses, [])

    result = reviewer._run()

    assert [phase for phase, _ in calls] == ["verify", "discovery"]
    assert result.criterion_met is (not remaining)
    assert result.detail["verify_attempts"] == 0
    assert "total_verify_cycles" not in result.detail
    assert "attempt 0/2" not in "\n".join(result.display_lines)


def test_restart_without_open_findings_discovers_only(reviewer, monkeypatch):
    reviewer._run()
    reviewer.args.steer = ["New context"]
    calls = contract_passes(reviewer, monkeypatch, [], [proposal()])

    result = reviewer._run()

    assert [phase for phase, _ in calls] == ["discovery"]
    assert result.exit_code == 1
    assert "restarting discovery" in result.report_text
    assert any("restarting discovery" in line for line in result.display_lines)
    assert "Missing [SIM_RESULT]" in result.report_text


def test_unchanged_contract_verifies_only_and_increments_budgets(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    first = reviewer._run()
    calls = contract_passes(reviewer, monkeypatch, [{"index": 1, "status": "STILL_PRESENT"}], [])

    result = reviewer._run()

    assert "[review-verify]" not in first.report_text
    assert [phase for phase, _ in calls] == ["verify"]
    assert result.detail["verify_attempts"] == 1
    assert result.detail["total_verify_cycles"] == 1
    assert "restarting discovery" not in result.report_text


@pytest.mark.parametrize("failed_phase", ["verify", "discovery"])
def test_changed_contract_phase_failure_retains_obligations_for_retry(
    reviewer, monkeypatch, failed_phase
):
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    first = reviewer._run()
    reviewer.args.steer = ["New context"]
    calls = contract_passes(reviewer, monkeypatch, [fixed_finding()], [])

    def fail(params):
        calls.append((failed_phase, params))
        raise RuntimeError("Provider unavailable")

    method = "_invoke_agent_with_resume" if failed_phase == "verify" else "_invoke_agent"
    monkeypatch.setattr(reviewer, method, fail)
    result = reviewer._run()

    assert result.exit_code == 2
    assert result.detail["pending"] == first.detail["pending"]
    assert result.detail["needs_discovery"] is True
    assert "restarting discovery" in result.report_text
    assert "[review-verify]" in result.report_text
    assert [phase for phase, _ in calls] == (
        ["verify"] if failed_phase == "verify" else ["verify", "discovery"]
    )
    retry_calls = contract_passes(reviewer, monkeypatch, [fixed_finding()], [])
    retry = reviewer._run()
    assert retry.exit_code == 0
    assert [phase for phase, _ in retry_calls] == ["verify", "discovery"]


def test_changed_tb_policy_and_decisions_reach_verification_prompt(reviewer, monkeypatch):
    root = Path(reviewer.args.work_dir)
    source = root / "tb/dut_tb.sv"
    source.parent.mkdir()
    source.write_text("module dut_tb; endmodule\n")
    config = root / ".booley_project/booley.toml"
    config.parent.mkdir()
    config.write_text('[flows.sim]\npass_sentinels = ["OLD PASS"]\n')
    spec = root / "spec.md"
    spec.write_text("Testbench must verify reset.\n")
    logs = root / "logs"
    logs.mkdir()
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    reviewer.args.category = "tb"
    reviewer.args.focus = "quality"
    reviewer.args.scope = "tb/dut_tb.sv"
    reviewer.args.spec = str(spec)
    reviewer.state.init_criteria({"review_tb_quality_clean": True})
    reviewer.state.save()
    output(reviewer, monkeypatch, {"issues": [proposal(category="quality", file="tb/dut_tb.sv")]})
    reviewer._run()
    config.write_text('[flows.sim]\npass_sentinels = ["NEW PASS"]\n')
    (logs / "answered_questions.md").write_text("Reset may take two cycles.\n")
    calls = contract_passes(reviewer, monkeypatch, [fixed_finding()], [])

    result = reviewer._run()

    assert result.exit_code == 0
    assert [phase for phase, _ in calls] == ["verify", "discovery"]
    prompt = calls[0][1].prompt
    assert "Testbench must verify reset." in prompt
    assert "Reset may take two cycles." in prompt
    assert "NEW PASS" in prompt
    assert "OLD PASS" not in prompt


def test_changed_contract_ignores_resolved_and_noncurrent_pending_rows(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [proposal(summary="Open obligation")]})
    reviewer._run()
    entry = reviewer.state.criteria["review_rtl_bugs_clean"]
    entry.detail["pending"].extend(
        [
            {**proposal(summary=status), "status": status}
            for status in ["fixed", "waived", "excluded", "superseded"]
        ]
        + [proposal(summary="advisory", disposition="advisory")]
    )
    reviewer.state.save()
    reviewer.args.steer = ["New context"]
    calls = contract_passes(reviewer, monkeypatch, [fixed_finding()], [])

    result = reviewer._run()

    assert result.criterion_met is True
    prompt = calls[0][1].prompt
    assert "Open obligation" in prompt
    assert "2. [" not in prompt
    assert [row["summary"] for row in result.detail["resolved"]] == ["Open obligation"]


def test_changed_steering_waiver_reaches_discovery_and_is_shown(reviewer, monkeypatch):
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    reviewer._run()
    reviewer.args.steer = ["Waive finding 1: the Goal deliberately requires this behavior"]
    waiver = {
        "index": 1,
        "status": "WAIVED",
        "justification": "The Goal deliberately requires this behavior",
    }
    calls = contract_passes(reviewer, monkeypatch, [waiver], [])

    result = reviewer._run()

    assert result.exit_code == 0
    assert "Waivers accepted in the preceding verification pass" in calls[1][1].prompt
    assert waiver["justification"] in calls[1][1].prompt
    assert "[WAIVED MAJOR]" in result.report_text
    assert "[WAIVED MAJOR]" in "\n".join(result.display_lines)
    assert result.detail["resolved"][0]["justification"] == waiver["justification"]


def test_next_verify_starts_fresh_after_combined_contract_call(reviewer, monkeypatch):
    root = Path(reviewer.args.work_dir)
    logs = root / "logs"
    logs.mkdir()
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    (root / "rtl/other.sv").write_text("module other; endmodule\n")
    output(reviewer, monkeypatch, {"issues": [proposal()]})
    reviewer._run()
    reviewer.args.scope = "rtl/other.sv"
    contract_passes(reviewer, monkeypatch, [{"index": 1, "status": "STILL_PRESENT"}], [])
    reviewer._last_session_id = "discovery-session"
    result = reviewer._run()
    assert result.exit_code == 1
    assert not (logs / "reviewer-rtl-bugs.session_id").exists()
    calls = contract_passes(reviewer, monkeypatch, [fixed_finding()], [])

    result = reviewer._run()

    assert result.exit_code == 0
    assert [phase for phase, _ in calls] == ["verify"]
    assert calls[0][1].resume_session is False
    assert "Verify whether the following issues" in calls[0][1].prompt
    assert "rtl/dut.sv:1" in calls[0][1].prompt


def test_empty_pending_does_not_resurrect_legacy_findings_on_restart(reviewer, monkeypatch):
    reviewer._run()
    entry = reviewer.state.criteria["review_rtl_bugs_clean"]
    entry.detail["issue_list"] = [proposal()]
    reviewer.state.save()
    reviewer.args.steer = ["Changed contract"]
    calls = contract_passes(reviewer, monkeypatch, [], [])

    result = reviewer._run()

    assert result.exit_code == 0
    assert [phase for phase, _ in calls] == ["discovery"]
    assert result.detail["pending"] == []
