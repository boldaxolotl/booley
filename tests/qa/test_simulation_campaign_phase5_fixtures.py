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
            "attempt_ordinal": 1,
        },
    )
    interrupted_result = tmp_path / "interrupted-result.json"
    resumed_value = {
        "$schema": "booley.simulation-result/v2",
        "work_item_id": "item:0000:batch",
        "attempt_id": "second",
        "attempt_ordinal": 2,
        "state": "completed",
        "grade": "pass",
        "observations": [{"test": "alpha"}, {"test": "beta"}],
    }
    resumed = _write(tmp_path / "resumed.json", resumed_value)
    return manifest, interrupted_attempt, interrupted_result, resumed


CAMPAIGN_ID = "c37cba48-7128-4aa8-a2a2-1a4220793161"


def _artifact(kind: str, path: str) -> dict[str, object]:
    return {
        "bytes": 2869,
        "kind": kind,
        "owner": CAMPAIGN_ID,
        "path": path,
        "path_base": "report_invocation",
        "sha256": "sha256:" + "5" * 64,
    }


def _mcp_response(tmp_path: Path, structured_key: str = "structured_content") -> Path:
    return _write(
        tmp_path / "mcp.json",
        {
            "meta": None,
            "content": [{"type": "text", "text": "EXIT_CODE: 0\nPASS"}],
            "is_error": False,
            structured_key: {
                "reports": [
                    {
                        "exit_code": 0,
                        "detail": {
                            "campaigns": {
                                "taxi": {
                                    "campaign_id": CAMPAIGN_ID,
                                    "manifest_sha256": "sha256:" + "b" * 64,
                                    "artifacts": {
                                        "manifest": _artifact(
                                            "simulation_campaign_manifest",
                                            "targets/taxi/campaign/manifest.json",
                                        ),
                                        "simulation": _artifact(
                                            "simulation_projection",
                                            "targets/taxi/simulation.json",
                                        ),
                                    },
                                    "grade": "pass",
                                    "complete": True,
                                    "observations": [
                                        {
                                            "test": name,
                                            "execution": "completed",
                                            "failure_class": None,
                                            "functional": "pass",
                                            "assertions": "clean",
                                            "assertion_count": 0,
                                            "cycle_count": None,
                                            "detail": {"reason": name},
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
                                    "termination_counts": {},
                                    "build_stage": [],
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
    campaign = document["structured_content"]["reports"][0]["detail"]["campaigns"]["taxi"]
    campaign["observations"] *= 17
    campaign["observation_total"] = 34
    campaign["observations_truncated"] = True
    for counts in campaign["observation_counts"].values():
        next_value = next(iter(counts))
        counts[next_value] = 34
    _write(response, document)
    with pytest.raises(ValueError, match="preview length"):
        module.validate_mcp_response(response, response.stat().st_size)
    camel_case = _mcp_response(tmp_path, "structuredContent")
    module.validate_mcp_response(camel_case, camel_case.stat().st_size)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda resumed: resumed.update(attempt_id="first"), "reused an attempt"),
        (lambda resumed: resumed.update(state="aborted"), "did not complete"),
        (lambda resumed: resumed["observations"].pop(), "no per-test observations"),
        (lambda resumed: resumed["observations"].reverse(), "differ from exact selection"),
        (lambda resumed: resumed.update(work_item_id="item:0001:other"), "one batch work item"),
    ],
)
def test_taxi_batch_validator_rejects_partial_or_reused_retry(
    tmp_path: Path, mutation: Callable[[dict[str, object]], object], message: str
) -> None:
    module = _module(
        ROOT / "qa/missions/taxi/fixtures/simulation-campaign/validate_phase5.py",
        "taxi_phase5_batch",
    )
    manifest, attempt, interrupted_result, resumed = _taxi_batch_files(tmp_path)
    document = json.loads(resumed.read_text())
    mutation(document)
    _write(resumed, document)
    with pytest.raises(ValueError, match=message):
        module.validate_cocotb_batch(manifest, attempt, interrupted_result, resumed)


def _retype_manifest(campaign) -> None:
    campaign["artifacts"]["manifest"]["kind"] = "simulation_projection"


def _escape_projection(campaign) -> None:
    campaign["artifacts"]["simulation"]["path"] = "../simulation.json"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda campaign: campaign["artifacts"].pop("manifest"), "manifest pointer is missing"),
        (
            lambda campaign: campaign["artifacts"].update(manifest="campaign/manifest.json"),
            "not a typed artifact reference",
        ),
        (_retype_manifest, "manifest pointer kind differs"),
        (
            lambda campaign: campaign["artifacts"]["simulation"].update(owner="other"),
            "simulation pointer owner differs",
        ),
        (_escape_projection, "simulation pointer path escapes"),
        (
            lambda campaign: campaign["artifacts"]["simulation"].update(sha256="sha256:0"),
            "simulation pointer digest is invalid",
        ),
        (
            lambda campaign: campaign["artifacts"]["manifest"].update(bytes=0),
            "manifest pointer size is invalid",
        ),
        (
            lambda campaign: campaign["artifacts"].update(summary=_artifact("summary", "s.json")),
            "artifacts are unexpected",
        ),
        (lambda campaign: campaign["observations"][0].pop("cycle_count"), "fields differ"),
        (
            lambda campaign: campaign["observations"][0].update(failure_class="flaky"),
            "failure class is invalid",
        ),
        (
            lambda campaign: campaign["observations"][0].update(cycle_count=-1),
            "cycle count is invalid",
        ),
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
    campaign = document["structured_content"]["reports"][0]["detail"]["campaigns"]["taxi"]
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
