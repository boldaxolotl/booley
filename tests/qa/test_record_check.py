"""A rejected Check Result must leave the append-only log unchanged."""

import json
import os
from pathlib import Path

import pytest
import yaml
from tests.qa.test_triage import check_result, write_json, write_run, write_suite

from qa import record_check, triage


def append_source(tmp_path, value):
    source = tmp_path / "candidate.json"
    source.write_text(json.dumps(value))
    return source


def assert_log_unchanged(log, before):
    assert log.read_bytes() == before
    assert list(log.parent.glob(".check-results.jsonl-*")) == []


def test_invalid_review_reason_is_rejected_before_append(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    log = root / "check-results.jsonl"
    before = log.read_bytes()
    result = check_result("run-1", "result-1", "check", "blocked")
    result["review_reasons"] = ["unsupported"]

    with pytest.raises(triage.TriageError, match="unsupported"):
        record_check.append_check_result(root, suite, append_source(tmp_path, result))
    assert log.read_bytes() == before


@pytest.mark.parametrize("status", ["blocked", "fail", "pass", "unavailable"])
def test_indirect_cause_is_rejected_before_append(tmp_path, status):
    suite = write_suite(tmp_path, [("required", True)])
    path = suite / "scenarios/sample/scenario.yaml"
    scenario = yaml.safe_load(path.read_text())
    scenario["steps"] = [
        {"id": "root-step", "checks": [{"id": "root"}], "requires": []},
        {"id": "middle-step", "checks": [{"id": "middle"}], "requires": ["root-step"]},
        {"id": "leaf-step", "checks": [{"id": "leaf"}], "requires": ["middle-step"]},
    ]
    path.write_text(yaml.safe_dump(scenario))
    first = check_result("run-1", "root-result", "root", "fail", step_id="root-step")
    root = write_run(tmp_path, "run-1", "required", [first], seal=False)
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["root", "leaf"]
    write_json(root / "run.json", run)
    log = root / "check-results.jsonl"
    before = log.read_bytes()
    result = check_result(
        "run-1",
        "leaf-result",
        "leaf",
        status,
        caused_by=["root-result"],
        step_id="leaf-step",
    )

    with pytest.raises(triage.TriageError, match="direct Scenario prerequisite"):
        record_check.append_check_result(root, suite, append_source(tmp_path, result))
    assert log.read_bytes() == before


def test_failed_atomic_publication_preserves_original_log(tmp_path, monkeypatch):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    log = root / "check-results.jsonl"
    before = log.read_bytes()
    result = check_result("run-1", "result-1", "check", "pass")
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["check"]
    write_json(root / "run.json", run)

    def fail_fsync(_descriptor):
        raise OSError("simulated write failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated write failure"):
        record_check.append_check_result(root, suite, append_source(tmp_path, result))
    assert log.read_bytes() == before
    assert list(root.glob(".check-results.jsonl-*")) == []


def test_failed_atomic_replace_preserves_original_log(tmp_path, monkeypatch):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    log = root / "check-results.jsonl"
    before = log.read_bytes()
    result = check_result("run-1", "result-1", "check", "pass")
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["check"]
    write_json(root / "run.json", run)
    original_replace = Path.replace

    def fail_replace(path, target):
        if target == log:
            raise OSError("simulated replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        record_check.append_check_result(root, suite, append_source(tmp_path, result))
    assert_log_unchanged(log, before)


def test_correction_appends_and_preserves_original_bytes(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    first = check_result("run-1", "result-1", "check", "blocked")
    root = write_run(tmp_path, "run-1", "required", [first], seal=False)
    log = root / "check-results.jsonl"
    before = log.read_bytes()
    corrected = check_result(
        "run-1", "result-2", "check", "blocked", corrects="result-1", attempt=2
    )
    corrected["review_reasons"].append("correction-chain")

    record_check.append_check_result(root, suite, append_source(tmp_path, corrected))

    assert log.read_bytes().startswith(before)
    assert [item["check_result_id"] for item in triage.read_jsonl(log)] == ["result-1", "result-2"]
    sealed = triage.seal_run(root, suite)
    assert [item["check_result_id"] for item in sealed.results] == ["result-1", "result-2"]


def test_correction_requires_review_reason_before_append(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    first = check_result("run-1", "result-1", "check", "blocked")
    root = write_run(tmp_path, "run-1", "required", [first], seal=False)
    log = root / "check-results.jsonl"
    before = log.read_bytes()
    corrected = check_result(
        "run-1", "result-2", "check", "blocked", corrects="result-1", attempt=2
    )

    with pytest.raises(triage.TriageError, match="correction-chain"):
        record_check.append_check_result(root, suite, append_source(tmp_path, corrected))
    assert log.read_bytes() == before


@pytest.mark.parametrize(
    "reference",
    [
        "/tmp/outside.txt",
        "evidence/../outside.txt",
        "../sibling/evidence/log.txt",
        "evidence/missing.txt",
        "evidence",
    ],
)
def test_invalid_evidence_path_is_rejected_before_append(tmp_path, reference):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    log = root / "check-results.jsonl"
    before = log.read_bytes()
    result = check_result("run-1", "result-1", "check", "pass")
    result["evidence_refs"] = [reference]
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["check"]
    write_json(root / "run.json", run)

    with pytest.raises(triage.TriageError, match="evidence"):
        record_check.append_check_result(root, suite, append_source(tmp_path, result))
    assert_log_unchanged(log, before)


def test_evidence_symlink_escape_is_rejected_before_append(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    (root / "evidence/escape.txt").symlink_to(outside)
    log = root / "check-results.jsonl"
    before = log.read_bytes()
    result = check_result("run-1", "result-1", "check", "pass")
    result["evidence_refs"] = ["evidence/escape.txt"]
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["check"]
    write_json(root / "run.json", run)

    with pytest.raises(triage.TriageError, match="evidence"):
        record_check.append_check_result(root, suite, append_source(tmp_path, result))
    assert_log_unchanged(log, before)


@pytest.mark.parametrize("reference", ["evidence/./log.txt", "evidence//log.txt"])
def test_noncanonical_evidence_path_is_rejected_before_append(tmp_path, reference):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    (root / "evidence/log.txt").write_text("evidence\n")
    log = root / "check-results.jsonl"
    before = log.read_bytes()
    result = check_result("run-1", "result-1", "check", "pass")
    result["evidence_refs"] = [reference]
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["check"]
    write_json(root / "run.json", run)

    with pytest.raises(triage.TriageError, match="noncanonical evidence"):
        record_check.append_check_result(root, suite, append_source(tmp_path, result))
    assert_log_unchanged(log, before)


@pytest.mark.parametrize(
    ("field", "nested"),
    [
        ("deviation", "evidence_refs"),
        ("borrowed_preservation", "setup_evidence_refs"),
        ("borrowed_preservation", "end_evidence_refs"),
        ("borrowed_preservation", "pre_run_absence_evidence_refs"),
        (None, "recovery_refs"),
    ],
)
def test_nested_evidence_path_is_validated_before_append(tmp_path, field, nested):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    log = root / "check-results.jsonl"
    before = log.read_bytes()
    result = check_result("run-1", "result-1", "check", "pass")
    if field == "deviation":
        result[field] = {
            "kind": "input",
            "declared": "declared",
            "actual": "actual",
            "reason": "reason",
            nested: ["evidence/missing.txt"],
        }
    elif field == "borrowed_preservation":
        result[field] = {nested: ["evidence/missing.txt"]}
    else:
        result[nested] = ["evidence/missing.txt"]
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["check"]
    write_json(root / "run.json", run)

    with pytest.raises(triage.TriageError, match="evidence"):
        record_check.append_check_result(root, suite, append_source(tmp_path, result))
    assert_log_unchanged(log, before)


def test_existing_contained_evidence_is_accepted(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    result = check_result("run-1", "result-1", "check", "pass")
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["check"]
    write_json(root / "run.json", run)

    record_check.append_check_result(root, suite, append_source(tmp_path, result))

    assert triage.read_jsonl(root / "check-results.jsonl") == [result]


def test_nested_evidence_is_linked_in_sealed_manifest(tmp_path):
    suite = write_suite(tmp_path, [("required", True)])
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    (root / "evidence/recovery.txt").write_text("recovered\n")
    result = check_result("run-1", "result-1", "check", "pass")
    result["recovery_refs"] = ["evidence/recovery.txt"]
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["check"]
    write_json(root / "run.json", run)

    record_check.append_check_result(root, suite, append_source(tmp_path, result))
    triage.seal_run(root, suite)

    manifest = triage.read_json(root / "evidence-manifest.json")
    entry = next(item for item in manifest["entries"] if item["path"] == "evidence/recovery.txt")
    assert entry["source_refs"] == ["result-1"]
