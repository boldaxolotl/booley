"""Exercise deterministic Public QA run sealing, triage, and qualification."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
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
                    for check_id in [
                        "check",
                        "dependent",
                        "independent",
                        "one",
                        "root",
                        "two",
                        "cleanup.preserve-borrowed",
                        "cleanup-preservation",
                    ]
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
    parameters: dict | None = None,
    seal: bool = True,
) -> Path:
    if not (root / "suite" / "scenarios" / "sample" / "scenario.yaml").is_file():
        write_suite(root, [(configured_id, True)])
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
            "parameters": parameters or {},
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
    if seal:
        triage.seal_run(run_root, root / "suite")
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
    scenario_path = suite / "scenarios/sample/scenario.yaml"
    scenario = yaml.safe_load(scenario_path.read_text())
    scenario["steps"] = [
        {"id": "root-step", "checks": [{"id": "root"}], "requires": []},
        {
            "id": "dependent-step",
            "checks": [{"id": "dependent"}],
            "requires": ["root-step"],
        },
        {"id": "independent-step", "checks": [{"id": "independent"}], "requires": []},
    ]
    scenario_path.write_text(yaml.safe_dump(scenario))
    results = [
        check_result("run-1", "root", "root", "fail", observed="same error", step_id="root-step"),
        check_result(
            "run-1",
            "dependent",
            "dependent",
            "fail",
            caused_by=["root"],
            observed="same error",
            step_id="dependent-step",
        ),
        check_result(
            "run-1",
            "independent",
            "independent",
            "fail",
            observed="same error",
            step_id="independent-step",
        ),
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
    run_root = write_run(tmp_path, "run-1", "required", results, seal=False)
    with pytest.raises(triage.TriageError, match="direct Scenario prerequisite"):
        triage.seal_run(run_root, suite)
    assert not (run_root / "run-manifest.json").exists()


def test_blocked_cause_requires_direct_check_prerequisite(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    scenario_path = suite / "scenarios/sample/scenario.yaml"
    scenario = yaml.safe_load(scenario_path.read_text())
    scenario["steps"] = [
        {"id": "root-step", "checks": [{"id": "root"}], "requires": []},
        {"id": "middle-step", "checks": [{"id": "middle"}], "requires": ["root-step"]},
        {"id": "leaf-step", "checks": [{"id": "leaf"}], "requires": ["middle-step"]},
    ]
    scenario_path.write_text(yaml.safe_dump(scenario))
    results = [
        check_result("run-1", "root-result", "root", "fail", step_id="root-step"),
        check_result("run-1", "middle-result", "middle", "blocked", step_id="middle-step"),
        check_result(
            "run-1",
            "leaf-result",
            "leaf",
            "blocked",
            caused_by=["root-result"],
            step_id="leaf-step",
        ),
    ]
    run_root = write_run(tmp_path, "run-1", "required", results, seal=False)
    with pytest.raises(triage.TriageError, match="direct Scenario prerequisite"):
        triage.seal_run(run_root, suite)


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


def test_admission_rejects_narrowed_configured_scenario_scope(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    scenario_path = suite / "scenarios/sample/scenario.yaml"
    scenario = yaml.safe_load(scenario_path.read_text())
    scenario["check_sets"] = [{"id": "required-checks", "checks": ["one", "two"]}]
    scenario["configured_scenarios"][0].update(
        {
            "parameters": {"host_os": "test-os"},
            "check_sets": ["required-checks"],
            "exclusions": [],
        }
    )
    scenario_path.write_text(yaml.safe_dump(scenario))
    run_root = write_run(
        tmp_path,
        "run-1",
        "required",
        [check_result("run-1", "ok", "one", "pass")],
        parameters={"host_os": "test-os"},
        seal=False,
    )
    with pytest.raises(triage.TriageError, match="selected Checks differ"):
        triage.seal_run(run_root, suite)


def test_admission_rejects_changed_configured_scenario_parameters(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    scenario_path = suite / "scenarios/sample/scenario.yaml"
    scenario = yaml.safe_load(scenario_path.read_text())
    scenario["configured_scenarios"][0]["parameters"] = {"host_os": "expected"}
    scenario_path.write_text(yaml.safe_dump(scenario))
    run_root = write_run(
        tmp_path,
        "run-1",
        "required",
        [check_result("run-1", "ok", "check", "pass")],
        parameters={"host_os": "different"},
        seal=False,
    )
    with pytest.raises(triage.TriageError, match="parameters differ"):
        triage.seal_run(run_root, suite)


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
    suite = write_suite(tmp_path, [("required", True)])
    results = [check_result("run-1", "ok", "check", "pass")]
    run_root = tmp_path / "run-1"
    (run_root / "evidence").mkdir(parents=True)
    (run_root / "evidence/log.txt").write_text("evidence\n")
    complete_run_files(run_root, results)

    assert not (run_root / "run-manifest.json").exists()
    sealed = triage.seal_run(run_root, suite)

    assert sealed.run_id == "run-1"
    assert (run_root / "run-manifest.json").is_file()


def test_seal_rejects_wrong_step_with_actionable_identity(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    result = check_result("run-1", "bad-step", "check", "fail", step_id="check")
    root = write_run(tmp_path, "run-1", "required", [result], seal=False)

    with pytest.raises(
        triage.TriageError,
        match="bad-step: Check check recorded Step check; required Step exercise",
    ):
        triage.seal_run(root, suite)
    assert not (root / "run-manifest.json").exists()
    assert not (root / "evidence-manifest.json").exists()
    assert (root / "run-summary.md").read_text() == "# Run summary\n"


def test_seal_rejects_unselected_result(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(
        tmp_path,
        "run-1",
        "required",
        [
            check_result("run-1", "extra", "check", "pass"),
            check_result("run-1", "selected", "one", "pass"),
        ],
        seal=False,
    )
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["one"]
    write_json(root / "run.json", run)

    with pytest.raises(triage.TriageError, match="unselected Checks"):
        triage.seal_run(root, suite)
    assert not (root / "run-manifest.json").exists()


def test_historical_manifest_without_scenario_digest_still_admits(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")])
    path = root / "run-manifest.json"
    manifest = triage.read_json(path)
    del manifest["scenario_snapshot_sha256"]
    write_json(path, manifest)

    assert admit(init_triage(tmp_path, suite), root)["runs"]["run-1"]


def test_snapshot_mismatch_rejected_at_admission(tmp_path):
    write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")])
    target = write_suite(tmp_path / "other", [("required", True)])
    scenario_path = target / "scenarios/sample/scenario.yaml"
    scenario_path.write_text(scenario_path.read_text() + "# editorial change\n")
    triage_root = init_triage(tmp_path / "other", target)

    with pytest.raises(triage.TriageError, match="snapshot differs"):
        admit(triage_root, root)


def test_summary_replaces_phantom_check_and_reports_suspicions(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    result = check_result("run-1", "uncertain", "check", "fail", observed="expected behavior")
    result["evidence_refs"] = []
    result["evidence_integrity"] = "uncertain"
    root = write_run(tmp_path, "run-1", "required", [result], seal=False)
    (root / "run-summary.md").write_text("# Failed Check phantom\n")
    triage.seal_run(root, suite)
    summary = (root / "run-summary.md").read_text()

    assert "uncertain: expected and observed text are identical" in summary
    assert "uncertain: no direct evidence reference" in summary
    assert "phantom" not in summary
    assert triage.validate_run(root).results[0]["evidence_integrity"] == "uncertain"
    before = (root / "run-manifest.json").read_bytes()
    triage.seal_run(root, suite)
    assert (root / "run-manifest.json").read_bytes() == before


def test_summary_flags_blank_nonpass_fields(tmp_path):
    write_suite(tmp_path, [("required", True)])
    blocked = check_result("run-1", "blank-expected", "check", "blocked")
    blocked["expected"] = "   "
    unavailable = check_result("run-1", "blank-observed", "one", "unavailable")
    unavailable["observed"] = "\t"
    root = write_run(tmp_path, "run-1", "required", [blocked, unavailable])

    summary = (root / "run-summary.md").read_text()
    assert "blank-expected: expected text is blank" in summary
    assert "blank-observed: observed text is blank" in summary
    assert [item["status"] for item in triage.validate_run(root).results] == [
        "blocked",
        "unavailable",
    ]


def test_seal_requires_suite_root_in_api_and_cli(tmp_path):
    root = tmp_path / "run-1"
    with pytest.raises(triage.TriageError, match="requires --suite-root"):
        triage.seal_run(root)
    command = subprocess.run(
        [sys.executable, "qa/triage.py", "seal-run", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert command.returncode != 0
    assert "--suite-root" in command.stderr


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
    assert findings[0]["source_run_ids"] == ["run-1", "run-2"]
    assert set(findings[0]["source_case_ids"]) == {first_case, second_case}


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


def test_later_pass_does_not_silently_supersede_incomplete_run(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    incomplete = write_run(
        tmp_path,
        "run-1",
        "required",
        [check_result("run-1", "blocked", "check", "blocked")],
    )
    passed = write_run(
        tmp_path, "run-2", "required", [check_result("run-2", "ok", "check", "pass")]
    )
    triage_root = init_triage(tmp_path, suite)
    case_id = admit(triage_root, incomplete, "admit-incomplete")["cases"][0]["case_id"]
    decide(
        tmp_path,
        triage_root,
        case_id,
        "infrastructure-failure",
        {"reason": "The test host stopped."},
    )
    admit(triage_root, passed, "admit-pass")

    qualification = triage.finalize_session(triage_root, "finish")["qualification"]

    assert qualification["scenario_run_outcomes"] == {
        "run-1": "incomplete",
        "run-2": "passed",
    }
    assert qualification["required_configured_scenarios"][0]["outcome"] == "incomplete"
    assert qualification["qualification"] == "incomplete"


def test_human_can_supersede_incomplete_run_with_complete_rerun(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    incomplete = write_run(
        tmp_path,
        "run-1",
        "required",
        [check_result("run-1", "blocked", "check", "blocked")],
    )
    passed = write_run(
        tmp_path, "run-2", "required", [check_result("run-2", "ok", "check", "pass")]
    )
    triage_root = init_triage(tmp_path, suite)
    case_id = admit(triage_root, incomplete, "admit-incomplete")["cases"][0]["case_id"]
    decide(
        tmp_path,
        triage_root,
        case_id,
        "infrastructure-failure",
        {"reason": "The test host stopped."},
    )
    admit(triage_root, passed, "admit-pass")

    triage.supersede_run(
        triage_root,
        "run-1",
        "run-2",
        "complete-rerun",
        "The rerun completed the same frozen scope on a healthy host.",
        "supersede",
    )
    qualification = triage.finalize_session(triage_root, "finish")["qualification"]

    assert qualification["qualification"] == "passed"
    assert qualification["required_configured_scenarios"][0]["active_run_ids"] == ["run-2"]
    assert qualification["supersession_decisions"] == [
        {
            "run_id": "run-1",
            "replacement_run_id": "run-2",
            "basis": "complete-rerun",
            "reason": "The rerun completed the same frozen scope on a healthy host.",
        }
    ]


def test_concurrent_admissions_preserve_every_event(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_roots = [
        write_run(
            tmp_path,
            f"run-{index}",
            "required",
            [check_result(f"run-{index}", f"ok-{index}", "check", "pass")],
        )
        for index in range(8)
    ]
    triage_root = init_triage(tmp_path, suite)
    helper = Path(triage.__file__).resolve()

    def admit_process(item: tuple[int, Path]) -> subprocess.CompletedProcess[str]:
        index, run_root = item
        return subprocess.run(
            [
                sys.executable,
                str(helper),
                "admit",
                str(triage_root),
                str(run_root),
                "--idempotency-key",
                f"admit-{index}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(admit_process, enumerate(run_roots)))

    assert [(result.returncode, result.stderr) for result in results] == [(0, "")] * 8
    events = triage.read_jsonl(triage_root / "triage-events.jsonl")
    assert [event["sequence"] for event in events] == list(range(1, 9))
    assert {event["payload"]["run_id"] for event in events} == {
        f"run-{index}" for index in range(8)
    }


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


def reseal_with_resources(run_root: Path, resources: list[dict]) -> triage.RunRecords:
    (run_root / "run-manifest.json").unlink()
    ledger = triage.read_json(run_root / "cleanup-ledger.json")
    ledger["resources"] = resources
    write_json(run_root / "cleanup-ledger.json", ledger)
    return triage.seal_run(run_root, run_root.parent / "suite")


@pytest.mark.parametrize(
    ("resources", "status", "outcome"),
    [
        (
            [
                {
                    "identity": "scratch",
                    "actual_disposition": None,
                    "active_authority_possible": False,
                    "cleanup_reason": "no release record",
                }
            ],
            "unverified",
            "passed",
        ),
        (
            [
                {
                    "identity": "session",
                    "actual_disposition": None,
                    "active_authority_possible": True,
                    "cleanup_reason": "shutdown unknown",
                }
            ],
            "unverified",
            "incomplete",
        ),
        (
            [
                {
                    "identity": "session",
                    "actual_disposition": "released",
                    "active_authority_possible": None,
                    "cleanup_reason": "shutdown unknown",
                }
            ],
            "unverified",
            "incomplete",
        ),
        (
            [
                {
                    "identity": "session",
                    "actual_disposition": "released",
                    "active_authority_possible": None,
                    "safe_shutdown_evidence_refs": ["evidence/log.txt"],
                }
            ],
            "unverified",
            "passed",
        ),
        ([{"actual_disposition": None}], "unverified", "incomplete"),
        (
            [
                {
                    "identity": "session",
                    "actual_disposition": "release-failed",
                    "active_authority_possible": True,
                }
            ],
            "failed",
            "incomplete",
        ),
    ],
)
def test_cleanup_reconciliation_and_qualification(tmp_path, resources, status, outcome):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")]
    )
    sealed = reseal_with_resources(run_root, resources)
    assert sealed.manifest["cleanup_status"] == status
    assert sealed.state["cleanup_status"] == status
    assert f"Cleanup status: `{status}`" in (run_root / "run-summary.md").read_text()
    assert (
        triage.seal_run(run_root, run_root.parent / "suite").manifest_hash == sealed.manifest_hash
    )

    triage_root = init_triage(tmp_path, suite)
    admit(triage_root, run_root)
    summary = (triage_root / "triage-summary.md").read_text()
    assert resources[0].get("identity", "resource #1") in summary
    qualification = triage.finalize_session(triage_root, "finish")["qualification"]
    assert qualification["scenario_run_outcomes"]["run-1"] == outcome
    assert (
        status in " ".join(qualification["scenario_run_reasons"]["run-1"]).lower()
        or status == "failed"
    )


def test_cleanup_complete_with_disposition_stays_complete(tmp_path):
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")]
    )
    sealed = reseal_with_resources(
        run_root,
        [
            {
                "identity": "scratch",
                "actual_disposition": "released",
                "active_authority_possible": False,
                "safe_shutdown_evidence_refs": ["evidence/log.txt"],
            }
        ],
    )
    assert sealed.manifest["cleanup_status"] == "complete"
    assert "Cleanup reconciliation" not in (run_root / "run-summary.md").read_text()


def test_four_unknown_dispositions_seal_and_remain_visible(tmp_path):
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")]
    )
    resources = [
        {
            "identity": f"resource-{number}",
            "actual_disposition": None,
            "active_authority_possible": False,
            "cleanup_reason": "disposition not observed",
        }
        for number in range(4)
    ]
    sealed = reseal_with_resources(run_root, resources)
    assert sealed.manifest["cleanup_status"] == "unverified"
    summary = (run_root / "run-summary.md").read_text()
    for number in range(4):
        assert f"`resource-{number}`: disposition `unknown`" in summary


@pytest.mark.parametrize(
    ("status", "resource"),
    [
        (
            "unverified",
            {
                "identity": "scratch",
                "actual_disposition": None,
                "active_authority_possible": False,
                "cleanup_reason": "missing release proof",
            },
        ),
        (
            "failed",
            {
                "identity": "session",
                "actual_disposition": "release-failed",
                "active_authority_possible": True,
                "cleanup_reason": "shutdown failed",
            },
        ),
    ],
)
def test_premarked_cleanup_status_still_records_affected_resources(tmp_path, status, resource):
    run_root = write_run(
        tmp_path,
        "run-1",
        "required",
        [check_result("run-1", "ok", "check", "pass")],
        cleanup_status=status,
    )
    sealed = reseal_with_resources(run_root, [resource])
    summary = (run_root / "run-summary.md").read_text()
    assert f"Cleanup status: `{status}`" in summary
    assert resource["identity"] in summary
    assert resource["cleanup_reason"] in summary
    assert (
        triage.seal_run(run_root, run_root.parent / "suite").manifest_hash == sealed.manifest_hash
    )
    assert (run_root / "run-summary.md").read_text() == summary


def test_legacy_complete_seal_keeps_historical_outcome(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")]
    )
    ledger = triage.read_json(run_root / "cleanup-ledger.json")
    ledger["resources"] = [{"name": "old resource", "actual_disposition": None}]
    write_json(run_root / "cleanup-ledger.json", ledger)
    manifest = triage.read_json(run_root / "run-manifest.json")
    manifest["files"]["cleanup-ledger.json"] = triage.sha256_file(run_root / "cleanup-ledger.json")
    write_json(run_root / "run-manifest.json", manifest)
    assert triage.validate_run(run_root).manifest["cleanup_status"] == "complete"

    triage_root = init_triage(tmp_path, suite)
    admit(triage_root, run_root)
    qualification = triage.finalize_session(triage_root, "finish")["qualification"]
    assert qualification["scenario_run_outcomes"]["run-1"] == "passed"


def test_shutdown_evidence_reference_must_be_sealed(tmp_path):
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")]
    )
    with pytest.raises(triage.TriageError, match="unknown shutdown evidence"):
        reseal_with_resources(
            run_root,
            [
                {
                    "identity": "session",
                    "actual_disposition": None,
                    "active_authority_possible": True,
                    "safe_shutdown_evidence_refs": ["evidence/missing.txt"],
                }
            ],
        )


@pytest.mark.parametrize("check_id", ["cleanup.preserve-borrowed", "cleanup-preservation"])
def test_borrowed_preservation_pass_links_scoped_setup_and_end_evidence(tmp_path, check_id):
    result = check_result("run-1", "borrowed", check_id, "pass")
    run_root = write_run(tmp_path, "run-1", "required", [result], seal=False)
    with pytest.raises(triage.TriageError, match="lacks scoped_resource_identities"):
        triage.seal_run(run_root, run_root.parent / "suite")

    (run_root / "evidence/setup.txt").write_text("installation before run\n")
    (run_root / "evidence/end.txt").write_text("installation after run\n")
    result["borrowed_preservation"] = {
        "scoped_resource_identities": ["vivado-installation-1"],
        "setup_evidence_refs": ["evidence/setup.txt"],
        "end_evidence_refs": ["evidence/end.txt"],
    }
    write_jsonl(run_root / "check-results.jsonl", [result])
    with pytest.raises(triage.TriageError, match="evidence must be linked"):
        triage.seal_run(run_root, run_root.parent / "suite")

    result["evidence_refs"].extend(["evidence/setup.txt", "evidence/end.txt"])
    write_jsonl(run_root / "check-results.jsonl", [result])
    assert triage.seal_run(run_root, run_root.parent / "suite").manifest["run_id"] == "run-1"


def test_borrowed_preservation_unavailable_needs_pre_run_absence_assessment(tmp_path):
    result = check_result("run-1", "borrowed", "cleanup.preserve-borrowed", "unavailable")
    run_root = write_run(tmp_path, "run-1", "required", [result], seal=False)
    with pytest.raises(triage.TriageError, match="lacks pre_run_absence_assessment"):
        triage.seal_run(run_root, run_root.parent / "suite")

    (run_root / "evidence/absence.txt").write_text("No borrowed grants at admission\n")
    result["borrowed_preservation"] = {
        "pre_run_absence_assessment": "No borrowed resource granted at admission",
        "pre_run_absence_evidence_refs": ["evidence/absence.txt"],
    }
    result["evidence_refs"].append("evidence/absence.txt")
    write_jsonl(run_root / "check-results.jsonl", [result])
    with pytest.raises(triage.TriageError, match="recorded during admission"):
        triage.seal_run(run_root, run_root.parent / "suite")

    run = triage.read_json(run_root / "run.json")
    run["admission_evidence"] = ["evidence/absence.txt"]
    write_json(run_root / "run.json", run)
    assert triage.seal_run(run_root, run_root.parent / "suite").manifest["run_id"] == "run-1"


def test_borrowed_preservation_without_evidence_can_be_blocked(tmp_path):
    result = check_result("run-1", "borrowed", "cleanup.preserve-borrowed", "blocked")
    run_root = write_run(tmp_path, "run-1", "required", [result], seal=False)
    assert triage.seal_run(run_root, run_root.parent / "suite").manifest["run_id"] == "run-1"


def test_interrupted_cleanup_reconciliation_retries_without_duplicate_summary(
    tmp_path, monkeypatch
):
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")]
    )
    (run_root / "run-manifest.json").unlink()
    ledger = triage.read_json(run_root / "cleanup-ledger.json")
    ledger["resources"] = [
        {"identity": "scratch", "actual_disposition": None, "active_authority_possible": False}
    ]
    write_json(run_root / "cleanup-ledger.json", ledger)
    original = triage.atomic_json
    interrupted = False

    def fail_state_once(path, value):
        nonlocal interrupted
        if path.name == "operator-state.json" and not interrupted:
            interrupted = True
            raise OSError("interrupted")
        original(path, value)

    monkeypatch.setattr(triage, "atomic_json", fail_state_once)
    with pytest.raises(OSError, match="interrupted"):
        triage.seal_run(run_root, run_root.parent / "suite")
    sealed = triage.seal_run(run_root, run_root.parent / "suite")
    assert sealed.manifest["cleanup_status"] == "unverified"
    assert (run_root / "run-summary.md").read_text().count("## Cleanup reconciliation") == 1


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


def test_qualification_records_input_identities_decisions_and_reasons(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    run_root = write_run(
        tmp_path, "run-1", "required", [check_result("run-1", "ok", "check", "pass")]
    )
    triage_root = init_triage(tmp_path, suite)
    admit(triage_root, run_root)

    qualification = triage.finalize_session(triage_root, "finish")["qualification"]

    assert qualification["input_runs"] == [
        {
            "run_id": "run-1",
            "manifest_sha256": triage.sha256_file(run_root / "run-manifest.json"),
            "configured_scenario_id": "required",
            "suite_revision": "suite-v2",
            "suite_equivalence_reason": None,
            "qualification_role": "active",
            "superseded_by_run_id": None,
            "supersession_reason": None,
        }
    ]
    assert qualification["scenario_run_reasons"]["run-1"] == ["All selected Checks are satisfied."]


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
    suite = write_suite(tmp_path, [("required", True)])
    run_root = tmp_path / "run-1"
    (run_root / "evidence").mkdir(parents=True)
    (run_root / "evidence/log.txt").write_text("evidence\n")
    complete_run_files(run_root, [check_result("run-1", "ok", "check", "pass")])
    run = json.loads((run_root / "run.json").read_text())
    run["run_record_format_version"] = 1
    write_json(run_root / "run.json", run)

    with pytest.raises(triage.TriageError, match="2 was expected"):
        triage.seal_run(run_root, suite)


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
