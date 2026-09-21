"""Contract tests for Phase 6 Simulation Campaign Public QA assets."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "qa/scenarios/coverage-lifecycle/fixtures/simulation-campaign"


def _module():
    path = FIXTURE / "validate_recovery.py"
    spec = importlib.util.spec_from_file_location("campaign_recovery_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _acceptance_evidence(tmp_path: Path) -> tuple[object, ...]:
    manifest_value = {
        "$schema": "booley.simulation-campaign-manifest/v1",
        "campaign_id": "campaign-1",
    }
    manifest = _write(tmp_path / "manifest.json", manifest_value)
    manifest_sha256 = _sha256(
        json.dumps(manifest_value, sort_keys=True, separators=(",", ":")).encode()
    )
    result = _write(
        tmp_path / "result.json",
        {
            "$schema": "booley.simulation-result/v1",
            "campaign_id": "campaign-1",
            "manifest_sha256": manifest_sha256,
            "state": "completed",
            "attempt_id": "attempt-1",
            "work_item_id": "item:0000:test",
        },
    )
    envelope = {
        "$schema": "booley.acceptance-transaction-envelope/v1",
        "campaign_id": "campaign-1",
        "manifest_sha256": manifest_sha256,
        "changes": [{"key": "sim_pass"}],
    }
    envelope_raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    transaction_id = hashlib.sha256(envelope_raw).hexdigest()
    transaction = _write(
        tmp_path / f"{transaction_id}.json",
        {
            "$schema": "booley.acceptance-transaction/v1",
            "transaction_id": transaction_id,
            "envelope": envelope,
            "envelope_sha256": _sha256(envelope_raw),
            "record_count": 1,
            "records": [{"criterion": "sim_pass"}],
        },
    )
    before = _write(tmp_path / "before-state.json", {"acceptance_transactions": ["a" * 64]})
    failed = _write(tmp_path / "failed-state.json", {"acceptance_transactions": ["a" * 64]})
    recovered = _write(
        tmp_path / "recovered-state.json",
        {"acceptance_transactions": ["a" * 64, transaction_id]},
    )
    simulation = _write(
        tmp_path / "simulation.json",
        {
            "complete": True,
            "campaign_manifest": str(manifest),
            "passed": True,
        },
    )
    return manifest, [result], [transaction], before, failed, recovered, simulation


def test_acceptance_recovery_validator_requires_one_selected_transaction(tmp_path: Path) -> None:
    module = _module()
    evidence = _acceptance_evidence(tmp_path)
    module.validate_acceptance_recovery(*evidence)

    recovered = evidence[-2]
    document = json.loads(recovered.read_text())
    document["acceptance_transactions"].append(document["acceptance_transactions"][-1])
    _write(recovered, document)
    with pytest.raises(ValueError, match="selected exactly once"):
        module.validate_acceptance_recovery(*evidence)


def test_acceptance_recovery_validator_rejects_contradictory_transaction(tmp_path: Path) -> None:
    module = _module()
    evidence = _acceptance_evidence(tmp_path)
    transaction = evidence[2][0]
    document = json.loads(transaction.read_text())
    document["envelope"]["campaign_id"] = "other"
    _write(transaction, document)
    with pytest.raises(ValueError, match="transaction ID differs"):
        module.validate_acceptance_recovery(*evidence)


def _corrupt_evidence(tmp_path: Path) -> tuple[Path, Path, Path]:
    valid = b'{"state":"completed","work_item_id":"item:0000:test"}\n'
    archived = tmp_path / "archived-result.json"
    restored = tmp_path / "restored-result.json"
    archived.write_bytes(valid)
    restored.write_bytes(valid)
    rejection = _write(
        tmp_path / "rejection.json",
        {
            "exit_code": 2,
            "diagnostic": "Simulation Campaign integrity error: result digest mismatch",
            "attempts_before": ["0001-attempt"],
            "attempts_after": ["0001-attempt"],
            "attempts_after_restore": ["0001-attempt"],
            "control_exit_code": 0,
            "control_complete": True,
            "eda_processes_started": [],
            "valid_sha256": _sha256(valid),
            "corrupt_sha256": "sha256:" + "b" * 64,
            "restored_sha256": _sha256(valid),
        },
    )
    return archived, restored, rejection


def test_corrupt_terminal_validator_requires_fail_closed_restore(tmp_path: Path) -> None:
    module = _module()
    evidence = _corrupt_evidence(tmp_path)
    module.validate_corrupt_terminal(*evidence)

    rejection = evidence[-1]
    document = json.loads(rejection.read_text())
    document["attempts_after"].append("0002-new")
    _write(rejection, document)
    with pytest.raises(ValueError, match="created an attempt"):
        module.validate_corrupt_terminal(*evidence)


def test_phase6_checks_and_pending_run_contract_are_complete() -> None:
    expected = {
        "picorv32": (
            "picorv32-simulation-campaign",
            "picorv32-ubuntu-codex-cli",
            7,
        ),
        "taxi": ("taxi-simulation-campaign", "taxi-ubuntu-codex-cli", 6),
        "uart": ("uart-simulation-campaign", "uart-ubuntu-codex-cli", 4),
        "coverage-lifecycle": (
            "coverage-lifecycle-simulation-campaign",
            "coverage-lifecycle-ubuntu-codex-cli",
            3,
        ),
    }
    campaign_checks = {}
    for scenario_name, (set_id, configured_id, count) in expected.items():
        scenario = yaml.safe_load(
            (ROOT / f"qa/scenarios/{scenario_name}/scenario.yaml").read_text()
        )
        dedicated = next(item for item in scenario["check_sets"] if item["id"] == set_id)
        assert len(dedicated["checks"]) == count
        selected = [
            item["id"]
            for item in scenario["configured_scenarios"]
            if set_id in item["check_sets"]
        ]
        assert selected == [configured_id]
        steps = {
            check["id"]: (step, check)
            for step in scenario["steps"]
            for check in step["checks"]
            if check["id"] in dedicated["checks"]
        }
        assert list(steps) == dedicated["checks"]
        assert all(step.get("requires") for step, _check in steps.values())
        campaign_checks.update({check_id: check for check_id, (_step, check) in steps.items()})

    assert len(campaign_checks) == 20
    assert "CRITERION-EVIDENCE-BINDING" in campaign_checks[
        "campaign.acceptance-recovery"
    ]["capabilities"]
    capability = next(
        item
        for item in yaml.safe_load((ROOT / "qa/coverage.yaml").read_text())["capabilities"]
        if item["id"] == "SIMULATION-CAMPAIGN"
    )
    assert all(check_id in capability["contract"] for check_id in campaign_checks)

    runbooks = [
        ROOT / f"qa/scenarios/{name}/fixtures/simulation-campaign/RUNBOOK.md"
        for name in ("picorv32", "taxi", "uart", "coverage-lifecycle")
    ]
    for runbook in runbooks:
        text = runbook.read_text()
        assert "pending" in text.lower()
        assert "recover" in text.lower() or "resume" in text.lower()
        assert "cleanup" in text.lower() or "remove only" in text.lower()

    readme = (ROOT / "qa/README.md").read_text()
    for configured_id in (
        "picorv32-ubuntu-codex-cli",
        "taxi-ubuntu-codex-cli",
        "uart-ubuntu-codex-cli",
        "coverage-lifecycle-ubuntu-codex-cli",
    ):
        assert configured_id in readme
