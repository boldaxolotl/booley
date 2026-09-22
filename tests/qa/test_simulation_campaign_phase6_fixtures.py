"""Contract tests for Phase 6 Simulation Campaign Public QA assets."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Callable
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


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _write(path: Path, value: object, *, pretty: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2) if pretty else _canonical(value).decode()
    path.write_text(text + "\n")
    return path


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _result(tmp_path: Path, campaign_id: str, manifest_sha256: str) -> Path:
    return _write(
        tmp_path / "result.json",
        {
            "$schema": "booley.simulation-result/v1",
            "campaign_id": campaign_id,
            "manifest_sha256": manifest_sha256,
            "state": "completed",
            "attempt_id": "6e8b91ae-93a3-4b3a-8813-e2f09c91bdc5",
            "work_item_id": "item:0000:test",
            "finished_at": "2026-09-22T01:02:03Z",
        },
    )


def _envelope(campaign_id: str, manifest_sha256: str) -> dict[str, object]:
    generation = "1" * 32
    return {
        "$schema": "booley.acceptance-transaction-envelope/v1",
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "origin": {"execution_id": "2" * 32, "invocation_id": 1},
        "producer": "simulation_campaign",
        "purpose": "ticket_acceptance",
        "ticket": {
            "slug": "qa-campaign",
            "identity": {"slug": "qa-campaign", "generation": generation},
            "generation": generation,
        },
        "recorded_at": "2026-09-22T01:02:03Z",
        "role_derivation": "booley.acceptance-role/v1",
        "changes": [
            {
                "key": key,
                "met": True,
                "reason": "outcome",
                "mandatory": True,
                "params": {},
                "detail": {"campaign_id": campaign_id, "ordinal": ordinal},
                "role": "candidate",
            }
            for ordinal, key in enumerate(("sim_pass_sim_toggle", "coverage_sim_toggle"))
        ],
    }


def _lookup(envelope: dict[str, object]) -> dict[str, object]:
    ticket = envelope["ticket"]
    assert isinstance(ticket, dict)
    return {
        "campaign_id": envelope["campaign_id"],
        "manifest_sha256": envelope["manifest_sha256"],
        "ticket_identity": ticket["identity"],
        "ticket_generation": ticket["generation"],
        "producer": "simulation_campaign",
        "purpose": "ticket_acceptance",
    }


def _facts(
    campaign_id: str, manifest_sha256: str, result_path: Path
) -> dict[str, object]:
    result = json.loads(result_path.read_text())
    raw = result_path.read_bytes()
    return {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "origin": {"execution_id": "2" * 32, "invocation_id": 1},
        "target": {"selector": "sim_toggle", "name": "sim_toggle"},
        "required_suite": {"names": ["half"], "default_invocation": False, "source_sha256": _sha256(b"suite")},
        "prerequisites": [],
        "consumed_results": [
            {
                "work_item_id": result["work_item_id"],
                "role": "candidate",
                "revision": "3" * 40,
                "target": {"selector": "sim_toggle"},
                "attempt_id": result["attempt_id"],
                "result": {
                    "path_base": "origin_invocation",
                    "path": "targets/sim_toggle/campaign/work-items/item/result.json",
                    "bytes": len(raw),
                    "sha256": _sha256(raw),
                    "kind": "simulation_result",
                    "owner": result["attempt_id"],
                },
                "finished_at": result["finished_at"],
            }
        ],
        "observations": [],
        "coverage_reference": None,
    }


def _record(
    envelope: dict[str, object], transaction_id: str, ordinal: int, sequence: int
) -> dict[str, object]:
    changes = envelope["changes"]
    origin = envelope["origin"]
    ticket = envelope["ticket"]
    assert isinstance(changes, list) and isinstance(origin, dict) and isinstance(ticket, dict)
    change = changes[ordinal]
    assert isinstance(change, dict)
    return {
        "$schema": "booley.acceptance-record/v2",
        "sequence": sequence,
        "transaction_id": transaction_id,
        "transaction_ordinal": ordinal,
        "transaction_size": len(changes),
        "envelope_sha256": _sha256(_canonical(envelope)),
        "ticket": ticket["slug"],
        "execution_id": origin["execution_id"],
        "purpose": envelope["purpose"],
        "producer": envelope["producer"],
        "invocation_id": origin["invocation_id"],
        "role": change["role"],
        "criterion": change["key"],
        "met": change["met"],
        "reason": change["reason"],
        "mandatory": change["mandatory"],
        "params": change["params"],
        "detail": change["detail"],
        "ticket_identity": ticket["identity"],
        "recorded_at": envelope["recorded_at"],
    }


def _states(
    tmp_path: Path, envelope: dict[str, object], transaction_id: str
) -> tuple[Path, Path, Path]:
    changes = envelope["changes"]
    assert isinstance(changes, list)
    criteria = {
        change["key"]: {"met": False, "mandatory": True}
        for change in changes
        if isinstance(change, dict)
    }
    archived_value = {
        "slug": "qa-campaign",
        "ticket_type": "bug-fix",
        "strict_criteria": True,
        "criteria": criteria,
        "category_map": {},
        "all_mandatory_met": False,
        "timeline": [],
        "acceptance_transactions": [],
        "authorized_zero_mandatory_basis_id": "",
        "last_updated": "2026-09-22T01:00:00Z",
    }
    archived = _write(tmp_path / "archived-state.json", archived_value, pretty=True)
    failed = tmp_path / "failed-state.json"
    failed.write_bytes(archived.read_bytes())
    recovered_value = json.loads(json.dumps(archived_value))
    recovered_value["acceptance_transactions"] = [transaction_id]
    recovered_value["all_mandatory_met"] = True
    recovered_value["last_updated"] = "2026-09-22T01:03:00Z"
    for change in changes:
        assert isinstance(change, dict)
        entry = recovered_value["criteria"][change["key"]]
        entry.update(
            met=change["met"], mandatory=change["mandatory"], params=change["params"],
            detail=change["detail"], ever_met=True,
        )
    recovered = _write(tmp_path / "recovered-state.json", recovered_value, pretty=True)
    return archived, failed, recovered


def _publish_records(
    root: Path, envelope: dict[str, object], transaction_id: str
) -> list[dict[str, object]]:
    references = []
    for ordinal, sequence in enumerate((4, 7)):
        record = _record(envelope, transaction_id, ordinal, sequence)
        _write(root / f"{sequence:09d}.tx.{transaction_id}" / "record.json", record)
        references.append(
            {
                "transaction_ordinal": ordinal,
                "sequence": sequence,
                "sha256": _sha256(_canonical(record) + b"\n"),
                "criterion": record["criterion"],
                "role": record["role"],
            }
        )
    return references


def _acceptance_evidence(tmp_path: Path) -> dict[str, object]:
    campaign_id = "c3fa3451-3a73-4a43-936c-9a2c40f88d30"
    manifest_value = {"$schema": "booley.simulation-campaign-manifest/v1", "campaign_id": campaign_id}
    manifest = _write(tmp_path / "manifest.json", manifest_value)
    manifest_sha256 = _sha256(_canonical(manifest_value))
    result = _result(tmp_path, campaign_id, manifest_sha256)
    envelope = _envelope(campaign_id, manifest_sha256)
    facts = _facts(campaign_id, manifest_sha256, result)
    lookup = _lookup(envelope)
    intent = _write(
        tmp_path / "acceptance" / "intents" / f"{hashlib.sha256(_canonical(lookup)).hexdigest()}.json",
        {
            "$schema": "booley.simulation-acceptance-intent/v1",
            "lookup_key": lookup,
            "acceptance_facts": facts,
            "acceptance_facts_sha256": _sha256(_canonical(facts)),
            "envelope": envelope,
        },
    )
    transaction_id = hashlib.sha256(_canonical(envelope)).hexdigest()
    evidence_root = tmp_path / "acceptance" / "evidence"
    references = _publish_records(evidence_root, envelope, transaction_id)
    transaction = _write(
        tmp_path / "acceptance" / "transactions" / f"{transaction_id}.json",
        {
            "$schema": "booley.acceptance-transaction/v1",
            "transaction_id": transaction_id,
            "envelope": envelope,
            "envelope_sha256": _sha256(_canonical(envelope)),
            "record_count": len(references),
            "records": references,
        },
    )
    archived, failed, recovered = _states(tmp_path, envelope, transaction_id)
    simulation = _write(
        tmp_path / "simulation.json",
        {"complete": True, "campaign_manifest": str(manifest), "passed": True},
    )
    return {
        "manifest": manifest, "results": [result], "intent": intent,
        "transaction": transaction, "evidence_root": evidence_root,
        "archived": archived, "failed": failed, "recovered": recovered,
        "simulation": simulation, "transaction_id": transaction_id,
    }


def _acceptance_args(evidence: dict[str, object]) -> tuple[object, ...]:
    return tuple(
        evidence[key]
        for key in (
            "manifest", "results", "intent", "transaction", "evidence_root",
            "archived", "failed", "recovered", "simulation",
        )
    )


def test_acceptance_recovery_validator_authenticates_real_v2_evidence(tmp_path: Path) -> None:
    _module().validate_acceptance_recovery(*_acceptance_args(_acceptance_evidence(tmp_path)))


def _mutate_json(path: Path, mutation: Callable[[dict[str, object]], None]) -> None:
    value = json.loads(path.read_text())
    mutation(value)
    _write(path, value)


def _add_extra_record(evidence: dict[str, object]) -> None:
    transaction_id = evidence["transaction_id"]
    extra = evidence["evidence_root"] / f"000000099.tx.{transaction_id}" / "record.json"
    _write(extra, {"unexpected": True})


def _add_duplicate_intent(evidence: dict[str, object]) -> None:
    intent = evidence["intent"]
    duplicate = intent.with_name("f" * 64 + ".json")
    duplicate.write_bytes(intent.read_bytes())


def _add_duplicate_commit(evidence: dict[str, object]) -> None:
    transaction = evidence["transaction"]
    duplicate = transaction.with_name("e" * 64 + ".json")
    duplicate.write_bytes(transaction.read_bytes())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda e: _mutate_json(e["intent"], lambda d: d.update(acceptance_facts_sha256="sha256:" + "0" * 64)), "facts digest"),
        (lambda e: _mutate_json(e["transaction"], lambda d: d.update(envelope_sha256="sha256:" + "0" * 64)), "envelope digest"),
        (lambda e: _mutate_json(e["transaction"], lambda d: d["records"][0].update(transaction_ordinal=1)), "record ordinal"),
        (lambda e: _mutate_json(e["transaction"], lambda d: d["records"][0].update(role="baseline")), "record role"),
        (lambda e: _mutate_json(e["transaction"], lambda d: d["records"][0].update(sha256="sha256:" + "0" * 64)), "record digest"),
        (lambda e: _mutate_first_record(e, lambda d: d.update(role="baseline")), "contradicts its envelope"),
        (lambda e: _mutate_first_record(e, lambda d: d.update(sequence=99)), "contradicts its envelope"),
        (_add_extra_record, "outside its commit"),
        (_add_duplicate_intent, "multiple acceptance intents"),
        (_add_duplicate_commit, "multiple transactions"),
        (lambda e: e["failed"].write_text(e["failed"].read_text() + " "), "archived state bytes"),
        (lambda e: _mutate_json(e["recovered"], lambda d: d["criteria"]["sim_pass_sim_toggle"]["detail"].update(extra=True)), "Criteria mutation"),
    ],
)
def test_acceptance_recovery_validator_rejects_hostile_mutations(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    evidence = _acceptance_evidence(tmp_path)
    mutation(evidence)
    with pytest.raises(ValueError, match=message):
        _module().validate_acceptance_recovery(*_acceptance_args(evidence))


def _mutate_first_record(
    evidence: dict[str, object], mutation: Callable[[dict[str, object]], None]
) -> None:
    transaction = json.loads(evidence["transaction"].read_text())
    sequence = transaction["records"][0]["sequence"]
    path = evidence["evidence_root"] / f"{sequence:09d}.tx.{evidence['transaction_id']}" / "record.json"
    _mutate_json(path, mutation)


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
        "picorv32": ("picorv32-simulation-campaign", "picorv32-ubuntu-codex-cli", 7),
        "taxi": ("taxi-simulation-campaign", "taxi-ubuntu-codex-cli", 6),
        "uart": ("uart-simulation-campaign", "uart-ubuntu-codex-cli", 4),
        "coverage-lifecycle": (
            "coverage-lifecycle-simulation-campaign", "coverage-lifecycle-ubuntu-codex-cli", 3,
        ),
    }
    campaign_checks = {}
    for scenario_name, (set_id, configured_id, count) in expected.items():
        scenario = yaml.safe_load((ROOT / f"qa/scenarios/{scenario_name}/scenario.yaml").read_text())
        dedicated = next(item for item in scenario["check_sets"] if item["id"] == set_id)
        assert len(dedicated["checks"]) == count
        selected = [item["id"] for item in scenario["configured_scenarios"] if set_id in item["check_sets"]]
        assert selected == [configured_id]
        steps = {
            check["id"]: (step, check)
            for step in scenario["steps"] for check in step["checks"]
            if check["id"] in dedicated["checks"]
        }
        assert list(steps) == dedicated["checks"]
        assert all(step.get("requires") for step, _check in steps.values())
        campaign_checks.update({key: check for key, (_step, check) in steps.items()})
    assert len(campaign_checks) == 20
    assert "CRITERION-EVIDENCE-BINDING" in campaign_checks["campaign.acceptance-recovery"]["capabilities"]
    capability = next(
        item for item in yaml.safe_load((ROOT / "qa/coverage.yaml").read_text())["capabilities"]
        if item["id"] == "SIMULATION-CAMPAIGN"
    )
    assert all(check_id in capability["contract"] for check_id in campaign_checks)
    for name in ("picorv32", "taxi", "uart", "coverage-lifecycle"):
        text = (ROOT / f"qa/scenarios/{name}/fixtures/simulation-campaign/RUNBOOK.md").read_text().lower()
        assert "pending" in text and ("recover" in text or "resume" in text)
        assert "cleanup" in text or "remove only" in text
    readme = (ROOT / "qa/README.md").read_text()
    for _name, (_set_id, configured_id, _count) in expected.items():
        assert configured_id in readme
