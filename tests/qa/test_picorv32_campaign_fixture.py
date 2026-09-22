"""Protect the owned PicoRV32 Simulation Campaign QA fixture and validator."""

from __future__ import annotations

import hashlib
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


def test_public_checks_have_automated_product_regression_backlinks() -> None:
    backlinks = {
        "campaign.exact-selection": (
            "tests/flows/sim/test_campaign_flow_planning.py",
            "test_resolved_target_plans_one_private_serial_item_per_exact_test",
        ),
        "campaign.tests-file-normalization": (
            "tests/flows/sim/test_exact_selection.py",
            "test_tests_file_ignores_comments_and_blanks",
        ),
        "campaign.duplicate-selection-rejection": (
            "tests/flows/sim/test_exact_selection.py",
            "test_invalid_selection_fails_in_cli_normalization",
        ),
        "campaign.manifest-authority": (
            "tests/flows/sim/test_campaign_manifest_codec.py",
            "test_manifest_exact_codec_recomputes_all_component_digests",
        ),
        "campaign.interrupted-resume": (
            "tests/flows/sim/test_campaign_crash_matrix.py",
            "test_serial_publication_boundary_resume_matrix",
        ),
        "campaign.workload-mismatch": (
            "tests/flows/sim/test_campaign_manifest_codec.py",
            "test_resume_preview_reports_all_workload_mismatches",
        ),
        "campaign.criteria-scope": (
            "tests/flows/sim/test_campaign_phase2.py",
            "test_passing_subset_does_not_change_target_level_simulation_criterion",
        ),
    }
    for _check, (relative, test_name) in backlinks.items():
        source = ROOT / relative
        assert source.is_file()
        assert f"def {test_name}" in source.read_text(encoding="utf-8")


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
