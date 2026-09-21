"""Protect Phase 4 bounded-parallel Simulation Campaign QA assets."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TAXI = ROOT / "qa/scenarios/taxi/fixtures/simulation-campaign"
UART = ROOT / "qa/scenarios/uart/fixtures/simulation-campaign"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def test_taxi_parallel_validator_covers_cap_isolation_and_continuation(
    tmp_path: Path,
) -> None:
    validator = _module("taxi_parallel_validator", TAXI / "validate_parallel.py")
    timeline = tmp_path / "timeline.json"
    _write_json(
        timeline,
        {
            "outer_execution_id": "outer",
            "slot_samples": [
                {"at_ns": 1, "heavy_holders": ["outer", "child-a", "child-b"]},
                {"at_ns": 5, "heavy_holders": ["outer", "child-c"]},
            ],
            "intervals": [
                {
                    "test": "slow-first",
                    "start_ns": 1,
                    "end_ns": 5,
                    "run_directory": "run-a",
                    "relative_output": "qa-shared-name.txt",
                    "token": "slow-first",
                    "output_text": "slow-first",
                },
                {
                    "test": "slow-fail",
                    "start_ns": 2,
                    "end_ns": 6,
                    "run_directory": "run-b",
                    "relative_output": "qa-shared-name.txt",
                    "token": "slow-fail",
                    "output_text": "slow-fail",
                },
                {
                    "test": "slow-last",
                    "start_ns": 5,
                    "end_ns": 9,
                    "run_directory": "run-c",
                    "relative_output": "qa-shared-name.txt",
                    "token": "slow-last",
                    "output_text": "slow-last",
                },
            ],
        },
    )
    assert validator.validate_heavy_cap(timeline, 3) == {
        "max_heavy": 3,
        "peak_simulators": 2,
    }
    validator.validate_attempt_isolation(timeline)

    manifest = tmp_path / "manifest.json"
    summary = tmp_path / "summary.json"
    results = [tmp_path / f"result-{index}.json" for index in range(3)]
    tests = ["slow-first", "slow-fail", "slow-last"]
    _write_json(
        manifest,
        {"work_items": [{"selection": {"names": [name]}} for name in tests]},
    )
    _write_json(summary, {"ordered_tests": tests, "strict_grade": "fail"})
    for path, name, grade in zip(results, tests, ["pass", "fail", "pass"], strict=True):
        _write_json(path, {"test": name, "grade": grade})
    validator.validate_continue_after_failure(manifest, summary, results)

    document = json.loads(timeline.read_text())
    document["slot_samples"][0]["heavy_holders"].append("child-over-cap")
    _write_json(timeline, document)
    with pytest.raises(ValueError, match="exceed max_heavy"):
        validator.validate_heavy_cap(timeline, 3)


def test_uart_literal_cwd_validator_proves_scoped_serialization(tmp_path: Path) -> None:
    validator = _module("uart_phase4_validator", UART / "validate_campaign.py")
    manifest = tmp_path / "manifest.json"
    timeline = tmp_path / "timeline.json"
    _write_json(manifest, {"workload": {"run_cwd": {"kind": "literal"}}})
    _write_json(
        timeline,
        {
            "shared_intervals": [
                {"run_directory": "shared", "start_ns": 1, "end_ns": 4},
                {"run_directory": "shared", "start_ns": 4, "end_ns": 8},
            ],
            "unrelated_intervals": [
                {"run_directory": "isolated", "start_ns": 2, "end_ns": 6}
            ],
        },
    )
    validator.validate_literal_cwd_serialization(manifest, timeline)
    document = json.loads(timeline.read_text())
    document["shared_intervals"][1]["start_ns"] = 3
    _write_json(timeline, document)
    with pytest.raises(ValueError, match="attempts overlap"):
        validator.validate_literal_cwd_serialization(manifest, timeline)


def test_phase4_checks_are_dedicated_and_pending_fresh_runs() -> None:
    expectations = {
        "taxi": (
            "taxi-simulation-campaign",
            [
                "campaign.heavy-cap",
                "campaign.attempt-isolation",
                "campaign.continue-after-failure",
            ],
            "taxi-ubuntu-codex-cli",
        ),
        "uart": (
            "uart-simulation-campaign",
            ["campaign.literal-cwd-serialization"],
            "uart-ubuntu-codex-cli",
        ),
    }
    for scenario_name, (set_id, checks, configured_id) in expectations.items():
        scenario = yaml.safe_load(
            (ROOT / f"qa/scenarios/{scenario_name}/scenario.yaml").read_text()
        )
        dedicated = next(item for item in scenario["check_sets"] if item["id"] == set_id)
        assert all(check in dedicated["checks"] for check in checks)
        selected = [
            item["id"]
            for item in scenario["configured_scenarios"]
            if set_id in item["check_sets"]
        ]
        assert selected == [configured_id]
        step_ids = {item["id"] for item in scenario["steps"]}
        assert set(checks) <= step_ids


def test_phase4_fixture_policy_is_explicit() -> None:
    assert 'run_cwd = ".booley-qa/campaign/{campaign}/{test}/{attempt}"' in (
        TAXI / "parallel.toml"
    ).read_text()
    assert 'run_cwd = ".booley-qa/campaign/literal-shared"' in (
        UART / "literal-cwd.toml"
    ).read_text()
    taxi_source = (TAXI / "campaign_main.cpp").read_text()
    assert "milliseconds(250)" in taxi_source
    assert 'ofstream marker("qa-shared-name.txt"' in taxi_source
    uart_source = (UART / "record_literal_cwd.py").read_text()
    assert "time.sleep(0.25)" in uart_source
    assert "time.monotonic_ns()" in uart_source
