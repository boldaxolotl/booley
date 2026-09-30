"""Contract tests for Phase 5 Simulation Campaign Public QA assets."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path

import pytest

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
        ROOT / "qa/missions/taxi/fixtures/simulation-campaign/validate_phase5.py",
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
        ROOT / "qa/missions/taxi/fixtures/simulation-campaign/validate_phase5.py",
        "taxi_phase5_mutation",
    )
    response = _mcp_response(tmp_path)
    document = json.loads(response.read_text())
    campaign = document["structuredContent"]["reports"][0]["detail"]["campaigns"]["taxi"]
    mutation(campaign)
    _write(response, document)
    with pytest.raises(ValueError, match=message):
        module.validate_mcp_response(response, response.stat().st_size)


def _coverage_refusal_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
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
    rejection = _write(
        tmp_path / "rejection.json",
        {
            "exit_code": 2,
            "dry_run_exit_code": 2,
            "diagnostic": "start a new run: booley flow sim --target sim_0 --coverage",
            "attempts_before": ["first"],
            "attempts_after": ["first"],
            "eda_launches": 0,
            "control_exit_code": 0,
        },
    )
    return manifest, interrupted_attempt, tmp_path / "interrupted-result.json", rejection


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(exit_code=0), "did not exit 2"),
        (lambda value: value.update(dry_run_exit_code=0), "dry-run did not exit 2"),
        (lambda value: value.update(attempts_after=["first", "second"]), "attempt inventory"),
        (
            lambda value: (value.pop("attempts_before"), value.pop("attempts_after")),
            "omitted the attempt inventory",
        ),
        (lambda value: value.update(diagnostic="resume failed"), "new coverage run"),
        (lambda value: value.update(eda_launches=1), "launched EDA"),
    ],
)
def test_coverage_phase5_validator_requires_resume_refusal(
    tmp_path: Path, mutation: Callable[[dict[str, object]], object], message: str
) -> None:
    module = _module(
        ROOT / "qa/shared/coverage/simulation-campaign/validate_aggregate.py",
        "coverage_phase5",
    )
    manifest, attempt, result, rejection = _coverage_refusal_files(tmp_path)
    module.validate(manifest, attempt, result, rejection)

    document = json.loads(rejection.read_text())
    mutation(document)
    _write(rejection, document)
    with pytest.raises(ValueError, match=message):
        module.validate(manifest, attempt, result, rejection)

    _write(rejection, json.loads(_coverage_refusal_files(tmp_path)[3].read_text()))
    result.write_text("{}")
    with pytest.raises(ValueError, match="terminal result"):
        module.validate(manifest, attempt, result, rejection)
