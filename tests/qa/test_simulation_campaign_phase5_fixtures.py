"""Contract tests for Phase 5 Simulation Campaign Public QA assets."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return path


def test_phase5_check_sets_are_isolated_to_representative_configurations() -> None:
    expected = {
        "taxi": (
            "taxi-simulation-campaign",
            {"campaign.cocotb-batch-resume", "campaign.mcp-structured-pointers"},
            "taxi-ubuntu-codex-cli",
        ),
        "coverage-lifecycle": (
            "coverage-lifecycle-simulation-campaign",
            {"campaign.coverage-aggregate-resume"},
            "coverage-lifecycle-ubuntu-codex-cli",
        ),
    }
    for scenario_name, (set_id, checks, selected_id) in expected.items():
        path = ROOT / f"qa/scenarios/{scenario_name}/scenario.yaml"
        scenario = yaml.safe_load(path.read_text())
        check_set = next(item for item in scenario["check_sets"] if item["id"] == set_id)
        assert checks <= set(check_set["checks"])
        selected = [
            item["id"]
            for item in scenario["configured_scenarios"]
            if set_id in item["check_sets"]
        ]
        assert selected == [selected_id]


def _taxi_batch_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    manifest = _write(
        tmp_path / "manifest.json",
        {
            "work_items": [
                {
                    "kind": "cocotb_batch",
                    "work_item_id": "item:0000:batch",
                    "selection": {"kind": "named", "names": ["alpha", "beta"]},
                }
            ]
        },
    )
    interrupted_attempt = _write(
        tmp_path / "interrupted-attempt.json",
        {
            "$schema": "booley.simulation-attempt/v1",
            "work_item_id": "item:0000:batch",
            "attempt_id": "first",
        },
    )
    interrupted_result = tmp_path / "interrupted-result.json"
    resumed_value = {
        "work_item_id": "item:0000:batch",
        "attempt_id": "second",
        "state": "completed",
        "observations": [{"test": "alpha"}, {"test": "beta"}],
    }
    resumed = _write(tmp_path / "resumed.json", resumed_value)
    return manifest, interrupted_attempt, interrupted_result, resumed


def _mcp_response(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "mcp.json",
        {
            "content": [{"type": "text", "text": "EXIT_CODE: 0\nPASS"}],
            "structuredContent": {
                "reports": [
                    {
                        "exit_code": 0,
                        "detail": {
                            "campaigns": {
                                "taxi": {
                                    "manifest": "campaign/manifest.json",
                                    "summary": "campaign/summary.json",
                                    "simulation": "simulation.json",
                                    "coverage": None,
                                    "grade": "pass",
                                    "complete": True,
                                    "observations": [
                                        {
                                            "test": name,
                                            "execution": "completed",
                                            "functional": "pass",
                                            "assertions": "clean",
                                            "assertion_count": 0,
                                            "detail": {"text": name},
                                        }
                                        for name in ("alpha", "beta")
                                    ],
                                    "observation_total": 2,
                                    "observations_truncated": False,
                                    "observation_counts": {
                                        "execution": {"completed": 2},
                                        "functional": {"pass": 2},
                                        "assertions": {"clean": 2},
                                    },
                                }
                            }
                        },
                    }
                ],
            },
        },
    )


def test_taxi_phase5_validator_checks_batch_retry_and_mcp_bound(tmp_path: Path) -> None:
    module = _module(
        ROOT / "qa/scenarios/taxi/fixtures/simulation-campaign/validate_phase5.py",
        "taxi_phase5",
    )
    module.validate_cocotb_batch(*_taxi_batch_files(tmp_path))
    response = _mcp_response(tmp_path)
    module.validate_mcp_response(response, response.stat().st_size)
    with pytest.raises(ValueError, match="declared bound"):
        module.validate_mcp_response(response, response.stat().st_size - 1)
    document = json.loads(response.read_text())
    campaign = document["structuredContent"]["reports"][0]["detail"]["campaigns"]["taxi"]
    campaign["observations"] *= 17
    campaign["observation_total"] = 34
    campaign["observations_truncated"] = True
    for counts in campaign["observation_counts"].values():
        next_value = next(iter(counts))
        counts[next_value] = 34
    _write(response, document)
    with pytest.raises(ValueError, match="preview length"):
        module.validate_mcp_response(response, response.stat().st_size)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda campaign: campaign.update(observation_total=3), "preview length"),
        (lambda campaign: campaign.update(observations_truncated=True), "truncation flag"),
        (lambda campaign: campaign["observations"][0].pop("detail"), "fields differ"),
        (
            lambda campaign: campaign["observation_counts"]["functional"].update(fail=1),
            "counts differ from preview",
        ),
    ],
)
def test_taxi_mcp_validator_rejects_inconsistent_observation_preview(
    tmp_path: Path, mutation: Callable[[dict[str, object]], object], message: str
) -> None:
    module = _module(
        ROOT / "qa/scenarios/taxi/fixtures/simulation-campaign/validate_phase5.py",
        "taxi_phase5_mutation",
    )
    response = _mcp_response(tmp_path)
    document = json.loads(response.read_text())
    campaign = document["structuredContent"]["reports"][0]["detail"]["campaigns"]["taxi"]
    mutation(campaign)
    _write(response, document)
    with pytest.raises(ValueError, match=message):
        module.validate_mcp_response(response, response.stat().st_size)


def _coverage_attempt_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    manifest = _write(
        tmp_path / "manifest.json",
        {"work_items": [{"kind": "coverage_aggregate", "work_item_id": "item:0000:coverage"}]},
    )
    interrupted_attempt = _write(
        tmp_path / "interrupted-attempt.json",
        {
            "$schema": "booley.simulation-attempt/v1",
            "work_item_id": "item:0000:coverage",
            "attempt_id": "first",
        },
    )
    interrupted_result = tmp_path / "interrupted-result.json"
    resumed_value = {
        "work_item_id": "item:0000:coverage",
        "attempt_id": "second",
        "producer_invocation_id": 2,
        "state": "completed",
    }
    resumed = _write(tmp_path / "resumed.json", resumed_value)
    return manifest, interrupted_attempt, interrupted_result, resumed


def _coverage_reference_files(tmp_path: Path) -> tuple[Path, Path, list[Path], dict[str, object]]:
    coverage = _write(
        tmp_path / "coverage.json",
        {"campaign_id": "nested", "invocation": {"id": 1}},
    )
    coverage_raw = coverage.read_bytes()
    coverage_digest = "sha256:" + __import__("hashlib").sha256(coverage_raw).hexdigest()
    reference_value = {
        "$schema": "booley.coverage-campaign-reference/v1",
        "origin_invocation_id": 1,
        "producer_invocation_id": 2,
        "simulation_work_item_id": "item:0000:coverage",
        "simulation_attempt_id": "second",
        "coverage_campaign": {
            "campaign_id": "nested",
            "path_base": "origin_target",
            "bytes": len(coverage_raw),
            "sha256": coverage_digest,
        },
    }
    reference = _write(tmp_path / "coverage-reference.json", reference_value)
    projections = [
        _write(
            tmp_path / f"projection-{index}.json",
            {"coverage_campaign": "coverage.json", "coverage_campaign_base": "origin_target"},
        )
        for index in range(2)
    ]
    return coverage, reference, projections, reference_value


def test_coverage_phase5_validator_binds_origin_reference(tmp_path: Path) -> None:
    module = _module(
        ROOT
        / "qa/scenarios/coverage-lifecycle/fixtures/simulation-campaign/validate_aggregate.py",
        "coverage_phase5",
    )
    attempts = _coverage_attempt_files(tmp_path)
    coverage, reference, projections, reference_value = _coverage_reference_files(tmp_path)
    module.validate(
        *attempts,
        coverage,
        reference,
        projections,
    )

    reference_value["producer_invocation_id"] = 3
    _write(reference, reference_value)
    with pytest.raises(ValueError, match="producer invocation"):
        module.validate(
            *attempts,
            coverage,
            reference,
            projections,
        )
