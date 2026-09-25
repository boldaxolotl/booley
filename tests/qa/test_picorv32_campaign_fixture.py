"""Protect the owned PicoRV32 Simulation Campaign QA fixture and validator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "qa/missions/picorv32/fixtures/simulation-campaign"


def _validator():
    spec = importlib.util.spec_from_file_location(
        "picorv32_campaign_validator", FIXTURE / "validate_campaign.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(names: list[str]) -> dict[str, object]:
    return {
        "$schema": "booley.simulation-campaign-manifest/v1",
        "required_suite": {"names": ["quick", "slow", "tail"]},
        "fingerprints": {"workload_sha256": "sha256:" + "1" * 64},
        "work_items": [{"selection": {"kind": "named", "names": [name]}} for name in names],
    }


def test_fixture_declares_owned_three_test_icarus_target() -> None:
    core = yaml.safe_load((FIXTURE / "campaign.core").read_text().split("\n", 1)[1])
    target = core["targets"]["sim_campaign"]
    assert target["default_tool"] == "icarus"
    assert target["toplevel"] == "campaign_tb"
    assert (
        '[sim_campaign]\ntests = ["quick", "slow", "tail"]' in (FIXTURE / "tests.toml").read_text()
    )
    assert (FIXTURE / "reverse-tests.txt").read_text().splitlines()[-2:] == [
        "tail",
        "quick",
    ]


def test_validator_preserves_requested_work_item_order(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(["tail", "quick"]), sort_keys=True) + "\n")
    result = _validator().validate(manifest, ["tail", "quick"])
    assert result["selection"] == ["tail", "quick"]
    assert result["work_items"] == 2


def test_validator_rejects_catalog_reordering(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(["quick", "tail"]), sort_keys=True) + "\n")
    with pytest.raises(ValueError, match="work-item order differs"):
        _validator().validate(manifest, ["tail", "quick"])


def test_validator_rejects_duplicate_expected_selection(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(["quick"]), sort_keys=True) + "\n")
    with pytest.raises(ValueError, match="duplicate expected"):
        _validator().validate(manifest, ["quick", "quick"])


def test_validator_authenticates_manifest_backlinks(tmp_path: Path) -> None:
    manifest_document = _manifest(["tail", "quick"]) | {"campaign_id": "campaign-1"}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(manifest_document, sort_keys=True) + "\n")
    digest = "sha256:" + hashlib.sha256(manifest.read_bytes().rstrip(b"\n")).hexdigest()
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"campaign_id": "campaign-1", "manifest_sha256": digest}) + "\n")
    projection = tmp_path / "simulation.json"
    projection.write_text(
        json.dumps(
            {
                "campaign_manifest": str(manifest),
                "campaign_summary": str(summary),
            }
        )
        + "\n"
    )

    assert _validator().validate_backlinks(manifest, summary, projection) == {
        "campaign_id": "campaign-1",
        "manifest_sha256": digest,
    }
    summary.write_text(
        json.dumps({"campaign_id": "campaign-1", "manifest_sha256": "sha256:bad"}) + "\n"
    )
    with pytest.raises(ValueError, match="summary manifest digest differs"):
        _validator().validate_backlinks(manifest, summary, projection)


def test_validator_checks_resume_rejection_and_criteria_journal(tmp_path: Path) -> None:
    before = tmp_path / "before-result.json"
    after = tmp_path / "after-result.json"
    before.write_bytes(b'{"grade":"pass"}\n')
    after.write_bytes(before.read_bytes())
    validator = _validator()
    validator.validate_interrupted_resume(
        before,
        after,
        completed_attempts_before=1,
        completed_attempts_after=1,
        interrupted_attempts_before=1,
        interrupted_attempts_after=2,
    )
    validator.validate_rejection(
        exit_code=2,
        attempts_before=["0001"],
        attempts_after=["0001"],
        simulator_started=False,
        diagnostic="duplicate test quick",
    )
    validator.validate_criteria_journal(
        required_suite=["quick", "slow", "tail"],
        observed_tests=["quick"],
        transaction_ids_before=[],
        transaction_ids_after=[],
    )
    validator.validate_criteria_journal(
        required_suite=["quick", "slow", "tail"],
        observed_tests=["quick", "slow", "tail"],
        transaction_ids_before=[],
        transaction_ids_after=["transaction"],
    )
    with pytest.raises(ValueError, match="rejection created an attempt"):
        validator.validate_rejection(
            exit_code=2,
            attempts_before=["0001"],
            attempts_after=["0001", "0002"],
            simulator_started=False,
            diagnostic="workload mismatch",
        )
