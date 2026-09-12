"""Exercise deterministic Public QA run sealing, triage, and qualification."""

import json
from pathlib import Path

import pytest
import yaml

from qa import triage

STAMP = "2026-09-12T10:00:00Z"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


def write_suite(root: Path, configured: list[tuple[str, bool]]) -> Path:
    suite = root / "suite"
    scenario = suite / "scenarios" / "sample"
    scenario.mkdir(parents=True)
    value = {
        "scenario_id": "sample",
        "configured_scenarios": [
            {"id": configured_id, "required": required} for configured_id, required in configured
        ],
        "steps": [
            {
                "id": "exercise",
                "checks": [
                    {"id": check_id}
                    for check_id in ["check", "dependent", "independent", "one", "root", "two"]
                ],
                "requires": [],
            }
        ],
    }
    (scenario / "scenario.yaml").write_text(yaml.safe_dump(value))
    return suite


def check_result(
    run_id: str,
    result_id: str,
    check_id: str,
    status: str,
    *,
    caused_by: list[str] | None = None,
    corrects: str | None = None,
    observed: str = "observed behavior",
    step_id: str = "exercise",
    attempt: int = 1,
) -> dict:
    return {
        "run_record_format_version": 2,
        "record_type": "check-result",
        "run_id": run_id,
        "check_result_id": result_id,
        "step_id": step_id,
        "check_id": check_id,
        "timestamp": STAMP,
        "attempt": attempt,
        "status": status,
        "expected": "expected behavior",
        "observed": observed,
        "evidence_refs": ["evidence/log.txt"],
        "producing_step_identities": {},
        "caused_by_result_ids": caused_by or [],
        "corrects_result_id": corrects,
        "review_reasons": [] if status == "pass" else ["nonpass"],
        "evidence_integrity": "valid",
        "deviation": None,
        "recovery_refs": [],
    }


def observation(run_id: str, observation_id: str, text: str = "noticed") -> dict:
    return {
        "run_record_format_version": 2,
        "record_type": "observation",
        "run_id": run_id,
        "observation_id": observation_id,
        "timestamp": STAMP,
        "text": text,
        "step_id": "exercise",
        "check_result_ids": [],
        "caused_by_result_ids": [],
        "evidence_refs": ["evidence/log.txt"],
        "corrects_observation_id": None,
        "review_reasons": ["observation"],
    }


def write_run(
    root: Path,
    run_id: str,
    configured_id: str,
    results: list[dict],
    observations: list[dict] | None = None,
    *,
    suite_revision: str = "suite-v2",
    cleanup_status: str = "complete",
    execution_status: str = "completed",
) -> Path:
    run_root = root / run_id
    (run_root / "evidence").mkdir(parents=True)
    (run_root / "evidence/log.txt").write_text("evidence\n")
    selected = sorted({result["check_id"] for result in results})
    write_json(
        run_root / "run.json",
        {
            "run_record_format_version": 2,
            "record_type": "run",
            "run_id": run_id,
            "scenario_id": "sample",
            "configured_scenario_id": configured_id,
            "product_revision": "product-v1",
            "suite_revision": suite_revision,
            "selected_check_ids": selected,
            "parameters": {},
            "identities": {},
            "authority": {},
            "created_at": STAMP,
            "deadline_at": "2026-09-12T18:00:00Z",
            "admission_evidence": [],
        },
    )
    write_json(
        run_root / "operator-state.json",
        operator_state(run_id, cleanup_status, execution_status),
    )
    write_json(
        run_root / "cleanup-ledger.json",
        {
            "run_record_format_version": 2,
            "record_type": "cleanup-ledger",
            "run_id": run_id,
            "resources": [],
        },
    )
    write_jsonl(run_root / "check-results.jsonl", results)
    write_jsonl(run_root / "observations.jsonl", observations or [])
    (run_root / "run-summary.md").write_text("# Run summary\n")
    triage.seal_run(run_root)
    return run_root


def operator_state(run_id: str, cleanup_status: str, execution_status: str = "completed") -> dict:
    return {
        "run_record_format_version": 2,
        "record_type": "operator-state",
        "run_id": run_id,
        "stage": "finish",
        "status": "complete",
        "entered_at": STAMP,
        "completed_at": STAMP,
        "next_step_id": None,
        "next_check_attempt": None,
        "active_assignments": [],
        "outstanding_mutations": [],
        "execution_status": execution_status,
        "cleanup_status": cleanup_status,
        "last_updated_at": STAMP,
    }


def init_triage(root: Path, suite: Path) -> Path:
    triage_root = root / "triage"
    triage.init_session(triage_root, suite, "product-v1", "suite-v2")
    return triage_root


def admit(triage_root: Path, run_root: Path, key: str = "admit") -> dict:
    return triage.admit_run(triage_root, run_root, None, key)


def decide(
    tmp_path: Path,
    triage_root: Path,
    case_id: str,
    disposition: str,
    details: dict,
    key: str = "decide",
) -> dict:
    path = tmp_path / f"{key}.json"
    write_json(path, details)
    return triage.decide_case(triage_root, case_id, disposition, path, key)


def finding_details(scope: str = "in-scope") -> dict:
    return {
        "summary": "confirmed defect",
        "original_text": "observed behavior",
        "owner": "Booley",
        "qualification_scope": scope,
        "scope_reason": "The exercised behavior is in the target suite."
        if scope == "in-scope"
        else "Upstream-only behavior.",
        "evidence_refs": ["evidence/log.txt"],
    }


def qa_change_details(invalidates: bool) -> dict:
    return {
        "summary": "incorrect QA expectation" if invalidates else "clearer QA wording",
        "change": "Update the Check contract.",
        "rationale": "The suite should express the actual authority.",
        "evidence_refs": ["evidence/log.txt"],
        "affected_area": "sample.check",
        "invalidates_evidence": invalidates,
    }


def test_explicit_cause_groups_failures_but_similar_text_does_not(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    results = [
        check_result("run-1", "root", "root", "fail", observed="same error"),
        check_result(
            "run-1", "dependent", "dependent", "fail", caused_by=["root"], observed="same error"
        ),
        check_result("run-1", "independent", "independent", "fail", observed="same error"),
    ]
    run_root = write_run(tmp_path, "run-1", "required", results)

    projection = admit(init_triage(tmp_path, suite), run_root)

    sizes = sorted(len(case["candidate_ids"]) for case in projection["cases"])
    assert sizes == [1, 2]
    assert len(projection["similarity_hints"]) == 1
    assert len(projection["similarity_hints"][0]["candidate_ids"]) == 3


def test_blocked_cause_must_follow_scenario_prerequisite_direction(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    scenario_path = suite / "scenarios/sample/scenario.yaml"
    scenario = yaml.safe_load(scenario_path.read_text())
    scenario["steps"] = [
        {"id": "root-step", "checks": [{"id": "root"}], "requires": []},
        {
            "id": "dependent-step",
            "checks": [{"id": "dependent"}],
            "requires": ["root-step"],
        },
    ]
    scenario_path.write_text(yaml.safe_dump(scenario))
    results = [
        check_result("run-1", "dependent", "dependent", "fail", step_id="dependent-step"),
        check_result(
            "run-1",
            "root",
            "root",
            "blocked",
            caused_by=["dependent"],
            step_id="root-step",
        ),
    ]
    run_root = write_run(tmp_path, "run-1", "required", results)
    triage_root = init_triage(tmp_path, suite)

    with pytest.raises(triage.TriageError, match="prerequisite direction"):
        admit(triage_root, run_root)


def test_corrected_failure_and_observation_remain_candidates(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    results = [
        check_result("run-1", "bad", "check", "fail"),
        check_result("run-1", "fixed", "check", "pass", corrects="bad", attempt=2),
    ]
    run_root = write_run(tmp_path, "run-1", "required", results, [observation("run-1", "obs")])

    projection = admit(init_triage(tmp_path, suite), run_root)

    candidates = projection["candidates"].values()
    result = next(item for item in candidates if item["kind"] == "check-result-chain")
    assert result["source_ids"] == ["bad", "fixed"]
    assert result["historical_statuses"] == ["fail", "pass"]
    assert any(item["kind"] == "observation-chain" for item in candidates)


def test_partial_triage_has_no_qualification_projection(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "bad", "check", "fail")]
    )
    triage_root = init_triage(tmp_path, suite)

    admit(triage_root, run_root)

    assert not (triage_root / "qualification.json").exists()
    with pytest.raises(triage.TriageError, match="pending cases"):
        triage.finalize_session(triage_root, "finish")


def test_product_defect_fails_even_when_another_required_run_is_missing(tmp_path):
    suite = write_suite(tmp_path, [("required", True), ("missing", True)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "bad", "check", "fail")]
    )
    triage_root = init_triage(tmp_path, suite)
    projection = admit(triage_root, run_root)
    case_id = projection["cases"][0]["case_id"]
    decide(tmp_path, triage_root, case_id, "product-defect", finding_details())

    finished = triage.finalize_session(triage_root, "finish")

    assert finished["qualification"]["qualification"] == "failed"
    required = finished["qualification"]["required_configured_scenarios"]
    assert {item["outcome"] for item in required} == {"failed", "missing"}


def test_qa_defect_is_incomplete_and_projection_is_idempotent(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "bad", "check", "fail")]
    )
    triage_root = init_triage(tmp_path, suite)
    case_id = admit(triage_root, run_root)["cases"][0]["case_id"]
    decide(
        tmp_path,
        triage_root,
        case_id,
        "qa-invalidating-defect",
        qa_change_details(True),
    )
    (triage_root / "qa-changes.jsonl").unlink()
    triage.render_session(triage_root)

    finished = triage.finalize_session(triage_root, "finish")

    assert finished["qualification"]["qualification"] == "incomplete"
    assert len(triage.read_jsonl(triage_root / "qa-changes.jsonl")) == 1


def test_neutral_disposition_cannot_hide_failed_check(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "bad", "check", "fail")]
    )
    triage_root = init_triage(tmp_path, suite)
    case_id = admit(triage_root, run_root)["cases"][0]["case_id"]

    with pytest.raises(triage.TriageError, match="cannot neutralize"):
        decide(
            tmp_path,
            triage_root,
            case_id,
            "product-defect",
            finding_details("out-of-scope"),
        )


def test_out_of_scope_observation_is_visible_but_neutral(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    results = [check_result("run-1", "ok", "check", "pass")]
    run_root = write_run(tmp_path, "run-1", "required", results, [observation("run-1", "obs")])
    triage_root = init_triage(tmp_path, suite)
    case_id = admit(triage_root, run_root)["cases"][0]["case_id"]
    decide(
        tmp_path,
        triage_root,
        case_id,
        "product-defect",
        finding_details("out-of-scope"),
    )

    finished = triage.finalize_session(triage_root, "finish")

    assert finished["qualification"]["qualification"] == "passed"
    assert len(triage.read_jsonl(triage_root / "findings.jsonl")) == 1


def test_sealed_run_mutation_blocks_resume(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "bad", "check", "fail")]
    )
    triage_root = init_triage(tmp_path, suite)
    admit(triage_root, run_root)
    (run_root / "run-summary.md").write_text("changed\n")

    with pytest.raises(triage.TriageError, match="sealed record changed"):
        triage.render_session(triage_root)


def test_different_suite_revision_requires_equivalence(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path,
        "run-1",
        "required",
        [check_result("run-1", "ok", "check", "pass")],
        suite_revision="suite-other",
    )
    triage_root = init_triage(tmp_path, suite)

    with pytest.raises(triage.TriageError, match="equivalence reason"):
        admit(triage_root, run_root)
    projection = triage.admit_run(
        triage_root, run_root, "Only editorial wording changed.", "equivalent"
    )
    assert projection["runs"]["run-1"]["suite_equivalence_reason"]


def test_recording_error_uses_only_sealed_evidence(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "bad", "check", "fail")]
    )
    triage_root = init_triage(tmp_path, suite)
    case_id = admit(triage_root, run_root)["cases"][0]["case_id"]
    details = {
        "corrected_status": "pass",
        "reason": "The operator transcribed the status incorrectly.",
        "evidence_refs": ["new-evidence.txt"],
    }

    with pytest.raises(triage.TriageError, match="evidence not sealed"):
        decide(tmp_path, triage_root, case_id, "recording-error", details)


def test_manifest_is_completion_authority(tmp_path):
    results = [check_result("run-1", "ok", "check", "pass")]
    run_root = tmp_path / "run-1"
    (run_root / "evidence").mkdir(parents=True)
    (run_root / "evidence/log.txt").write_text("evidence\n")
    complete_run_files(run_root, results)

    assert not (run_root / "run-manifest.json").exists()
    sealed = triage.seal_run(run_root)

    assert sealed.run_id == "run-1"
    assert (run_root / "run-manifest.json").is_file()


def test_cross_run_duplicate_fails_both_runs_without_duplicate_finding(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    first = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "bad-1", "check", "fail")]
    )
    second = write_run(
        tmp_path, "run-2", "required", [check_result("run-2", "bad-2", "check", "fail")]
    )
    triage_root = init_triage(tmp_path, suite)
    first_case = admit(triage_root, first, "admit-1")["cases"][0]["case_id"]
    decide(tmp_path, triage_root, first_case, "product-defect", finding_details(), "first")
    projection = admit(triage_root, second, "admit-2")
    second_case = next(
        case["case_id"] for case in projection["cases"] if case["case_id"] != first_case
    )
    decide(
        tmp_path,
        triage_root,
        second_case,
        "duplicate-finding",
        {"duplicate_of_case_id": first_case},
        "duplicate",
    )

    finished = triage.finalize_session(triage_root, "finish")

    assert finished["qualification"]["scenario_run_outcomes"] == {
        "run-1": "failed",
        "run-2": "failed",
    }
    findings = triage.read_jsonl(triage_root / "findings.jsonl")
    assert len(findings) == 1
    assert findings[0]["source_run_ids"] == ["run-1"]


def test_later_passing_run_does_not_erase_trustworthy_failure(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    failed = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "bad", "check", "fail")]
    )
    passed = write_run(
        tmp_path, "run-2", "required", [check_result("run-2", "ok", "check", "pass")]
    )
    triage_root = init_triage(tmp_path, suite)
    case_id = admit(triage_root, failed, "admit-fail")["cases"][0]["case_id"]
    decide(tmp_path, triage_root, case_id, "product-defect", finding_details())
    admit(triage_root, passed, "admit-pass")

    qualification = triage.finalize_session(triage_root, "finish")["qualification"]

    assert qualification["scenario_run_outcomes"] == {
        "run-1": "failed",
        "run-2": "passed",
    }
    assert qualification["required_configured_scenarios"][0]["outcome"] == "failed"
    assert qualification["qualification"] == "failed"


def test_merge_split_reopen_replay_preserves_unique_membership(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path,
        "run-1",
        "required",
        [
            check_result("run-1", "bad-1", "one", "fail"),
            check_result("run-1", "bad-2", "two", "fail"),
        ],
    )
    triage_root = init_triage(tmp_path, suite)
    projection = admit(triage_root, run_root)
    source_ids = [case["case_id"] for case in projection["cases"]]
    merged = triage.merge_case_ids(triage_root, source_ids, "same defect", "merge")
    merged_case = merged["cases"][0]
    groups = tmp_path / "groups.json"
    write_json(groups, [[item] for item in merged_case["candidate_ids"]])
    split = triage.split_case_from_file(
        triage_root, merged_case["case_id"], groups, "different remedies", "split"
    )
    case_id = split["cases"][0]["case_id"]
    decide(tmp_path, triage_root, case_id, "unresolved", {"reason": "needs study"})
    triage.reopen_case(triage_root, case_id, "new evidence", "reopen")
    replayed = triage.render_session(triage_root)

    memberships = [item for case in replayed["cases"] for item in case["candidate_ids"]]
    assert sorted(memberships) == sorted(replayed["candidates"])
    assert len(memberships) == len(set(memberships))
    assert (
        next(case for case in replayed["cases"] if case["case_id"] == case_id)["disposition"]
        is None
    )
    assert (
        triage.reopen_case(triage_root, case_id, "new evidence", "reopen")["state"]["status"]
        == "triage"
    )


def test_cleanup_failure_is_incomplete_without_a_candidate(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path,
        "run-1",
        "required",
        [check_result("run-1", "ok", "check", "pass")],
        cleanup_status="failed",
    )
    triage_root = init_triage(tmp_path, suite)
    admit(triage_root, run_root)

    qualification = triage.finalize_session(triage_root, "finish")["qualification"]

    assert qualification["scenario_run_outcomes"]["run-1"] == "incomplete"
    assert qualification["qualification"] == "incomplete"


@pytest.mark.parametrize("condition", ["operator-error", "invalid-evidence"])
def test_invalid_execution_or_evidence_cannot_pass(tmp_path, condition):
    suite = write_suite(tmp_path, [("required", True)])
    result = check_result("run-1", "ok", "check", "pass")
    execution_status = "completed"
    if condition == "operator-error":
        execution_status = "operator-error"
    else:
        result["evidence_integrity"] = "invalid"
        result["review_reasons"] = ["evidence-integrity"]
    run_root = write_run(
        tmp_path,
        "run-1",
        "required",
        [result],
        execution_status=execution_status,
    )
    triage_root = init_triage(tmp_path, suite)
    projection = admit(triage_root, run_root)
    for case in projection["cases"]:
        decide(
            tmp_path,
            triage_root,
            case["case_id"],
            "expected-observation",
            {"reason": "The recorded behavior was expected."},
            f"decide-{case['case_id']}",
        )

    qualification = triage.finalize_session(triage_root, "finish")["qualification"]

    assert qualification["scenario_run_outcomes"]["run-1"] == "incomplete"
    assert qualification["qualification"] == "incomplete"


def test_finalize_retry_is_idempotent_and_summary_includes_outcome(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")]
    )
    triage_root = init_triage(tmp_path, suite)
    admit(triage_root, run_root)

    triage.finalize_session(triage_root, "finish")
    event_count = len(triage.read_jsonl(triage_root / "triage-events.jsonl"))
    triage.finalize_session(triage_root, "finish")

    assert len(triage.read_jsonl(triage_root / "triage-events.jsonl")) == event_count
    assert "Qualification: `passed`" in (triage_root / "triage-summary.md").read_text()


def test_optional_configured_scenario_is_reported_separately(tmp_path):
    suite = write_suite(tmp_path, [("required", True), ("optional", False)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")]
    )
    triage_root = init_triage(tmp_path, suite)
    admit(triage_root, run_root)

    qualification = triage.finalize_session(triage_root, "finish")["qualification"]

    assert qualification["qualification"] == "passed"
    assert qualification["optional_configured_scenarios"][0]["outcome"] == "missing"


def test_version_one_run_record_is_rejected(tmp_path):
    run_root = tmp_path / "run-1"
    (run_root / "evidence").mkdir(parents=True)
    (run_root / "evidence/log.txt").write_text("evidence\n")
    complete_run_files(run_root, [check_result("run-1", "ok", "check", "pass")])
    run = json.loads((run_root / "run.json").read_text())
    run["run_record_format_version"] = 1
    write_json(run_root / "run.json", run)

    with pytest.raises(triage.TriageError, match="2 was expected"):
        triage.seal_run(run_root)


def test_frozen_helper_revision_blocks_resume(tmp_path, monkeypatch):
    suite = write_suite(tmp_path, [("required", True)])
    triage_root = init_triage(tmp_path, suite)
    monkeypatch.setattr(triage, "helper_revision", lambda: "0" * 64)

    with pytest.raises(triage.TriageError, match="helper revision"):
        triage.render_session(triage_root)


def test_frozen_policy_revision_blocks_resume(tmp_path, monkeypatch):
    suite = write_suite(tmp_path, [("required", True)])
    triage_root = init_triage(tmp_path, suite)
    monkeypatch.setattr(triage, "default_policy_revision", lambda: "0" * 64)

    with pytest.raises(triage.TriageError, match="qualification policy"):
        triage.render_session(triage_root)


def complete_run_files(run_root: Path, results: list[dict]) -> None:
    write_json(
        run_root / "run.json",
        {
            "run_record_format_version": 2,
            "record_type": "run",
            "run_id": "run-1",
            "scenario_id": "sample",
            "configured_scenario_id": "required",
            "product_revision": "product-v1",
            "suite_revision": "suite-v2",
            "selected_check_ids": ["check"],
            "parameters": {},
            "identities": {},
            "authority": {},
            "created_at": STAMP,
            "deadline_at": "2026-09-12T18:00:00Z",
            "admission_evidence": [],
        },
    )
    write_json(run_root / "operator-state.json", operator_state("run-1", "complete"))
    write_json(
        run_root / "cleanup-ledger.json",
        {
            "run_record_format_version": 2,
            "record_type": "cleanup-ledger",
            "run_id": "run-1",
            "resources": [],
        },
    )
    write_jsonl(run_root / "check-results.jsonl", results)
    write_jsonl(run_root / "observations.jsonl", [])
    (run_root / "run-summary.md").write_text("# Run summary\n")
