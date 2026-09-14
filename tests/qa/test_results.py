"""Behavior of compact observed public QA history."""

import json
from pathlib import Path

import pytest
from qa.triage import RunRecords

from qa import results


def sample_result(run_id: str, completed_at: str, checks: dict[str, str]) -> dict:
    """Build a synthetic tracked snapshot."""
    return {
        "format_version": 1,
        "run_id": run_id,
        "scenario_id": "sample",
        "configured_scenario_id": "sample-linux-cli",
        "completed_at": completed_at,
        "product_revision": "a" * 40,
        "suite_revision": "b" * 40,
        "run_manifest_sha256": "c" * 64,
        "execution_status": "completed",
        "cleanup_status": "complete",
        "checks": checks,
    }


def sealed_run(tmp_path: Path) -> RunRecords:
    """Build a minimal already-validated record object for reduction tests."""
    attempts = (
        {
            "check_result_id": "a",
            "check_id": "first",
            "status": "fail",
            "corrects_result_id": None,
        },
        {
            "check_result_id": "b",
            "check_id": "first",
            "status": "pass",
            "corrects_result_id": None,
        },
        {
            "check_result_id": "c",
            "check_id": "second",
            "status": "fail",
            "corrects_result_id": None,
        },
        {
            "check_result_id": "d",
            "check_id": "second",
            "status": "pass",
            "corrects_result_id": "c",
        },
        {
            "check_result_id": "e",
            "check_id": "third",
            "status": "blocked",
            "corrects_result_id": None,
        },
    )
    return RunRecords(
        tmp_path,
        "c" * 64,
        {
            "run_id": "run-1",
            "scenario_id": "sample",
            "configured_scenario_id": "sample-linux-cli",
            "product_revision": "a" * 40,
            "suite_revision": "b" * 40,
            "selected_check_ids": ["first", "second", "third"],
        },
        {
            "completed_at": "2026-09-14T10:00:00Z",
            "execution_status": "completed",
            "cleanup_status": "complete",
        },
        {},
        attempts,
        (),
        frozenset(),
    )


def test_tracked_results_are_valid():
    root = Path(__file__).resolve().parents[2] / "qa" / "results"
    results.load_history(root)


def test_sealed_attempts_preserve_uncorrected_failure(tmp_path):
    snapshot = results.from_sealed_run(sealed_run(tmp_path))
    assert snapshot["checks"] == {"first": "fail", "second": "pass", "third": "blocked"}
    assert snapshot["run_manifest_sha256"] == "c" * 64


def test_report_shows_trend_and_latest_and_ever_exercised(tmp_path):
    scenarios = tmp_path / "scenarios" / "sample"
    scenarios.mkdir(parents=True)
    (scenarios / "scenario.yaml").write_text(
        "scenario_id: sample\n"
        "check_sets:\n- id: core\n  checks: [first, second, third]\n"
        "configured_scenarios:\n- id: sample-linux-cli\n  check_sets: [core]\n",
        encoding="utf-8",
    )
    first = sample_result(
        "run-1",
        "2026-09-14T10:00:00Z",
        {"first": "fail", "second": "blocked", "third": "unavailable"},
    )
    second = sample_result(
        "run-2",
        "2026-09-14T11:00:00Z",
        {"first": "pass", "second": "blocked", "third": "unavailable"},
    )
    text = results.report([first, second], tmp_path / "scenarios", "sample-linux-cli")
    assert "| 0 | 1 | 1 | 1 |" in text
    assert "| 1 | 0 | 1 | 1 |" in text
    assert "| `first` | pass | yes | yes |" in text
    assert "| `second` | blocked | no | no |" in text
    assert "| `third` | unavailable | no | no |" in text


def test_publish_validates_seal_and_refuses_rewrite(tmp_path, monkeypatch):
    run = sealed_run(tmp_path / "sealed")
    checked = []

    def validate(path):
        checked.append(path)
        return run

    monkeypatch.setattr(results, "validate_run", validate)
    root = tmp_path / "results"
    destination = results.publish(run.root, root)
    assert checked == [run.root]
    assert results.load_history(root) == [results.from_sealed_run(run)]
    assert results.publish(run.root, root) == destination
    changed = dict(results.from_sealed_run(run), checks={"first": "pass"})
    destination.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="already has different results"):
        results.publish(run.root, root)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"run_id": "../escape"}, "invalid run_id"),
        ({"completed_at": "2026-09-14T10:00:00+00:00"}, "completed_at"),
        ({"run_manifest_sha256": "bad"}, "run_manifest_sha256"),
        ({"checks": {"first": "unknown"}}, "invalid Check status"),
    ],
)
def test_invalid_result_is_rejected(change, message):
    record = sample_result("run-1", "2026-09-14T10:00:00Z", {"first": "fail"})
    with pytest.raises(ValueError, match=message):
        results.validate_result(dict(record, **change))
