"""Protect the owned PicoRV32 Simulation Campaign QA fixture and validator."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "qa/scenarios/picorv32/fixtures/simulation-campaign"


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


def test_campaign_checks_are_isolated_to_representative_configuration() -> None:
    scenario = yaml.safe_load((ROOT / "qa/scenarios/picorv32/scenario.yaml").read_text())
    campaign_set = next(
        item for item in scenario["check_sets"] if item["id"] == "picorv32-simulation-campaign"
    )
    assert campaign_set["checks"] == [
        "campaign.exact-selection",
        "campaign.tests-file-normalization",
        "campaign.duplicate-selection-rejection",
        "campaign.manifest-authority",
        "campaign.interrupted-resume",
        "campaign.workload-mismatch",
        "campaign.criteria-scope",
    ]
    selected = [
        item["id"]
        for item in scenario["configured_scenarios"]
        if "picorv32-simulation-campaign" in item["check_sets"]
    ]
    assert selected == ["picorv32-ubuntu-codex-cli"]
