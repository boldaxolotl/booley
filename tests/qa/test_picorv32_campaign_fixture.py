"""Protect the owned PicoRV32 Simulation Campaign QA fixture and validator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
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
    # Flow-API Icarus recipe: the SystemVerilog TB needs -g2012 to build.
    assert target["flow"] == "sim"
    assert target["flow_options"] == {"tool": "icarus", "iverilog_options": ["-g2012"]}
    assert target["toplevel"] == "campaign_tb"
    assert (
        '[sim_campaign]\ntests = ["quick", "slow", "tail"]' in (FIXTURE / "tests.toml").read_text()
    )
    assert (FIXTURE / "reverse-tests.txt").read_text().splitlines()[-2:] == [
        "tail",
        "quick",
    ]


def test_fixture_testbench_runs_under_icarus_with_project_sentinel() -> None:
    testbench = (FIXTURE / "campaign_tb.sv").read_text()
    # Icarus has no $system; `slow` must spin simulation time instead.
    assert "$system(" not in testbench
    # The PicoRV32 Project's configured pass sentinel replaces [SIM_RESULT] PASSED.
    assert '$display("ALL TESTS PASSED.");' in testbench


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


def _campaign_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write a manifest, summary and v2 projection in the on-disk Target layout."""
    target = tmp_path / "targets" / "sim_campaign"
    manifest = target / "campaign" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest_document = _manifest(["tail", "quick"]) | {"campaign_id": "campaign-1"}
    manifest.write_text(json.dumps(manifest_document, sort_keys=True) + "\n")
    raw = manifest.read_bytes()
    summary = manifest.parent / "summary.json"
    canonical = "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()
    summary.write_text(
        json.dumps({"campaign_id": "campaign-1", "manifest_sha256": canonical}) + "\n"
    )
    projection = target / "simulation.json"
    reference = {
        "path_base": "origin_target",
        "path": "campaign/manifest.json",
        "kind": "simulation_campaign_manifest",
        "owner": "campaign-1",
        "bytes": len(raw),
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }
    projection.write_text(
        json.dumps({"campaign_id": "campaign-1", "campaign_manifest": reference}) + "\n"
    )
    return manifest, summary, projection


def test_validator_authenticates_manifest_backlinks(tmp_path: Path) -> None:
    manifest, summary, projection = _campaign_tree(tmp_path)
    digest = "sha256:" + hashlib.sha256(manifest.read_bytes().rstrip(b"\n")).hexdigest()

    assert _validator().validate_backlinks(manifest, summary, projection) == {
        "campaign_id": "campaign-1",
        "manifest_sha256": digest,
    }
    summary.write_text(
        json.dumps({"campaign_id": "campaign-1", "manifest_sha256": "sha256:bad"}) + "\n"
    )
    with pytest.raises(ValueError, match="summary manifest digest differs"):
        _validator().validate_backlinks(manifest, summary, projection)


def test_validator_rejects_stale_projection_manifest_reference(tmp_path: Path) -> None:
    manifest, summary, projection = _campaign_tree(tmp_path)
    document = json.loads(projection.read_text())
    document["campaign_manifest"]["sha256"] = "sha256:" + "0" * 64
    projection.write_text(json.dumps(document) + "\n")
    with pytest.raises(ValueError, match="manifest backlink digest differs"):
        _validator().validate_backlinks(manifest, summary, projection)
    # The pre-v2 string form is no longer a valid backlink.
    document["campaign_manifest"] = str(manifest)
    projection.write_text(json.dumps(document) + "\n")
    with pytest.raises(ValueError, match="not an artifact reference"):
        _validator().validate_backlinks(manifest, summary, projection)


def test_validator_cli_exposes_every_check(tmp_path: Path) -> None:
    manifest, summary, projection = _campaign_tree(tmp_path)
    script = str(FIXTURE / "validate_campaign.py")
    backlinks = subprocess.run(
        [
            sys.executable,
            script,
            "backlinks",
            str(manifest),
            "--summary",
            str(summary),
            "--projection",
            str(projection),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert backlinks.returncode == 0, backlinks.stderr
    assert json.loads(backlinks.stdout)["campaign_id"] == "campaign-1"
    before = tmp_path / "before.txt"
    after = tmp_path / "after.txt"
    diagnostic = tmp_path / "diagnostic.txt"
    before.write_text("0001\n")
    after.write_text("0001\n0002\n")
    diagnostic.write_text("duplicate test quick\n")
    rejection = subprocess.run(
        [
            sys.executable,
            script,
            "rejection",
            "--exit-code",
            "2",
            "--attempts-before",
            str(before),
            "--attempts-after",
            str(after),
            "--diagnostic",
            str(diagnostic),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejection.returncode == 2
    assert "rejection created an attempt" in rejection.stderr


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
