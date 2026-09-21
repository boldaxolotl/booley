"""Protect Phase 4 bounded-parallel Simulation Campaign QA assets."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TAXI = ROOT / "qa/scenarios/taxi/fixtures/simulation-campaign"
UART = ROOT / "qa/scenarios/uart/fixtures/simulation-campaign"
SHA = "sha256:" + "a" * 64


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _artifact_evidence(root: Path, attempt_id: str, token: str) -> dict[str, object]:
    evidence = {}
    for kind in ("output", "log", "trace", "runtime_input"):
        relative = Path("attempts") / attempt_id / f"{kind}.dat"
        retained = root / relative
        retained.parent.mkdir(parents=True, exist_ok=True)
        raw = (token + "\n").encode()
        retained.write_bytes(raw)
        evidence[kind] = {
            "path": relative.as_posix(),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "token": token,
        }
    return evidence


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
                {
                    "at_ns": 2,
                    "heavy_holders": ["outer", "child-a", "child-b"],
                    "heavy_waiters": ["child-c"],
                },
                {
                    "at_ns": 5,
                    "heavy_holders": ["outer", "child-b", "child-c"],
                    "heavy_waiters": [],
                },
            ],
            "final_slot_state": {"heavy_holders": [], "heavy_waiters": []},
            "claim_transitions": [
                {"at_ns": 0, "child_execution_id": "child-a", "state": "submitted"},
                {"at_ns": 1, "child_execution_id": "child-a", "state": "promoted"},
                {"at_ns": 1, "child_execution_id": "child-b", "state": "submitted"},
                {"at_ns": 2, "child_execution_id": "child-b", "state": "promoted"},
                {"at_ns": 2, "child_execution_id": "child-c", "state": "submitted"},
                {"at_ns": 5, "child_execution_id": "child-c", "state": "promoted"},
                {"at_ns": 5, "child_execution_id": "child-a", "state": "released"},
                {"at_ns": 6, "child_execution_id": "child-b", "state": "released"},
                {"at_ns": 9, "child_execution_id": "child-c", "state": "released"},
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
                    "attempt_id": "attempt-a",
                    "child_execution_id": "child-a",
                    "work_item_id": "item-0",
                    **_artifact_evidence(tmp_path, "attempt-a", "slow-first"),
                },
                {
                    "test": "slow-fail",
                    "start_ns": 2,
                    "end_ns": 6,
                    "run_directory": "run-b",
                    "relative_output": "qa-shared-name.txt",
                    "token": "slow-fail",
                    "output_text": "slow-fail",
                    "attempt_id": "attempt-b",
                    "child_execution_id": "child-b",
                    "work_item_id": "item-1",
                    **_artifact_evidence(tmp_path, "attempt-b", "slow-fail"),
                },
                {
                    "test": "slow-last",
                    "start_ns": 5,
                    "end_ns": 9,
                    "run_directory": "run-c",
                    "relative_output": "qa-shared-name.txt",
                    "token": "slow-last",
                    "output_text": "slow-last",
                    "attempt_id": "attempt-c",
                    "child_execution_id": "child-c",
                    "work_item_id": "item-2",
                    **_artifact_evidence(tmp_path, "attempt-c", "slow-last"),
                },
            ],
        },
    )
    assert validator.validate_heavy_cap(timeline, 3) == {
        "max_heavy": 3,
        "peak_simulators": 2,
    }
    validator.validate_attempt_isolation(timeline)

    retained_output = tmp_path / "attempts/attempt-a/output.dat"
    original_output = retained_output.read_bytes()
    retained_output.write_text("forged\n", encoding="utf-8")
    with pytest.raises(ValueError, match="retained digest differs"):
        validator.validate_attempt_isolation(timeline)
    retained_output.write_bytes(original_output)

    manifest = tmp_path / "manifest.json"
    summary = tmp_path / "summary.json"
    results = [tmp_path / f"result-{index}.json" for index in range(3)]
    tests = ["slow-first", "slow-fail", "slow-last"]
    _write_json(
        manifest,
        {
            "manifest_sha256": SHA,
            "work_items": [
                {"work_item_id": f"item-{index}", "selection": {"names": [name]}}
                for index, name in enumerate(tests)
            ],
        },
    )
    _write_json(
        summary,
        {
            "ordered_tests": tests,
            "strict_grade": "fail",
            "manifest_sha256": SHA,
            "completed": ["item-0", "item-1", "item-2"],
        },
    )
    for index, (path, name, grade) in enumerate(
        zip(results, tests, ["pass", "fail", "pass"], strict=True)
    ):
        _write_json(
            path,
            {
                "test": name,
                "grade": grade,
                "manifest_sha256": SHA,
                "work_item_id": f"item-{index}",
                "attempt_id": f"attempt-{index}",
                "result_sha256": SHA,
            },
        )
    validator.validate_continue_after_failure(manifest, summary, results)

    document = json.loads(timeline.read_text())
    complete_transitions = list(document["claim_transitions"])
    document["claim_transitions"] = [
        item
        for item in complete_transitions
        if not (
            item["child_execution_id"] == "child-b" and item["state"] == "promoted"
        )
    ]
    _write_json(timeline, document)
    with pytest.raises(ValueError, match="exact release"):
        validator.validate_heavy_cap(timeline, 3)
    document["claim_transitions"] = complete_transitions
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
