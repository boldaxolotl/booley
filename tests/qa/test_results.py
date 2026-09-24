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


def compact_attempt(
    result_id: str, check_id: str, status: str, attempt: int, corrects: str | None = None
) -> dict:
    """Build the fields consumed by compact result projection."""
    return {
        "check_result_id": result_id,
        "check_id": check_id,
        "status": status,
        "corrects_result_id": corrects,
        "attempt": attempt,
    }


def sealed_run(tmp_path: Path) -> RunRecords:
    """Build a minimal already-validated record object for reduction tests."""
    attempts = (
        compact_attempt("a", "first", "fail", 1),
        compact_attempt("b", "first", "pass", 2),
        compact_attempt("c", "second", "fail", 1),
        compact_attempt("d", "second", "pass", 2, "c"),
        compact_attempt("e", "third", "blocked", 1),
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
        {"resources": []},
    )


def test_tracked_results_are_valid():
    root = Path(__file__).resolve().parents[2] / "qa" / "results"
    results.load_history(root)


def test_sealed_attempts_preserve_uncorrected_failure(tmp_path):
    run = sealed_run(tmp_path)
    run.state["completed_at"] = "2026-09-14T14:00:00.500+04:00"
    snapshot = results.from_sealed_run(run)
    assert snapshot["checks"] == {"first": "fail", "second": "pass", "third": "blocked"}
    assert snapshot["run_manifest_sha256"] == "c" * 64
    assert snapshot["completed_at"] == "2026-09-14T10:00:00Z"


def test_unverified_cleanup_survives_compact_history(tmp_path):
    run = sealed_run(tmp_path)
    run.state["cleanup_status"] = "unverified"
    assert results.from_sealed_run(run)["cleanup_status"] == "unverified"
    snapshot = sample_result("run-2", "2026-09-14T10:00:00Z", {"first": "pass"})
    snapshot["cleanup_status"] = "unverified"
    assert results.validate_result(snapshot)["cleanup_status"] == "unverified"


def test_report_shows_scenario_trend_and_latest_and_ever_exercised(tmp_path, monkeypatch):
    scenario = {
        "scenario_id": "sample",
        "check_sets": [{"id": "core", "checks": ["first", "second", "third"]}],
        "configured_scenarios": [
            {"id": "sample-linux-cli", "check_sets": ["core"]},
            {"id": "sample-windows-cli", "check_sets": ["core"]},
        ],
    }
    monkeypatch.setattr(results, "load_scenarios", lambda _root: {"sample": scenario})
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
    third = dict(
        sample_result(
            "run-3",
            "2026-09-14T12:00:00Z",
            {"first": "fail", "second": "pass", "third": "unavailable"},
        ),
        configured_scenario_id="sample-windows-cli",
    )
    text = results.report([first, second, third], tmp_path / "scenarios", None)
    assert "## Scenario: sample" in text
    assert "| 0 | -1 |" in text
    assert "sample-windows-cli" in text
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


def test_publish_cleans_temporary_file_after_link_failure(tmp_path, monkeypatch):
    run = sealed_run(tmp_path / "sealed")
    monkeypatch.setattr(results, "validate_run", lambda _path: run)

    def fail_link(_source, _destination):
        raise OSError("publication failed")

    monkeypatch.setattr(results.os, "link", fail_link)
    root = tmp_path / "results"
    with pytest.raises(OSError, match="publication failed"):
        results.publish(run.root, root)
    assert not list((root / "sample").iterdir())


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
