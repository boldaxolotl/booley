"""Contract tests for Phase 6 Simulation Campaign Public QA assets."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from booley.criteria.evidence_ledger import AcceptanceTransaction, record_or_verify_transaction
from booley.criteria.state import DevelopmentState
from booley.goals.model import (
    GoalRecord,
    GoalState,
    RecordedGoal,
    WorktreeIdentity,
    parse_goal_args,
)
from booley.goals.recorder import GOAL_SCOPE, GoalProjection, goal_identity
from booley.goals.store import GoalStore
from booley.goals.translate import translate_goals

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "qa/shared/coverage/simulation-campaign"


@dataclass(frozen=True)
class TransactionEvidence:
    intent: Path
    transaction: Path
    evidence_root: Path
    archived: Path
    failed: Path
    recovered: Path
    transaction_id: str


@dataclass(frozen=True)
class RecoveryEvidence:
    manifest: Path
    results: list[Path]
    simulation: Path
    transaction: TransactionEvidence


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
    path.write_bytes(text.encode() + b"\n")
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


def _facts(campaign_id: str, manifest_sha256: str, result_path: Path) -> dict[str, Any]:
    result = json.loads(result_path.read_text())
    raw = result_path.read_bytes()
    return {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "origin": {"execution_id": "2" * 32, "invocation_id": 1},
        "target": {"selector": "sim_toggle", "name": "sim_toggle"},
        "required_suite": {
            "names": ["half"],
            "default_invocation": False,
            "source_sha256": _sha256(b"suite"),
        },
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


def _goal_state(tmp_path: Path) -> tuple[DevelopmentState, GoalProjection, dict[str, Any]]:
    specs = translate_goals(
        parse_goal_args(
            [
                {"family": "sim", "target": "sim_toggle"},
                {
                    "family": "coverage",
                    "target": "sim_toggle",
                    "tests": ["half"],
                    "metrics": {"toggle": 50},
                },
            ]
        )
    ).goals
    record_id = "qa-campaign-20260922T010000Z"
    identity = WorktreeIdentity("c3fa3451-3a73-4a43-936c-9a2c40f88d30", "worktrees/qa")
    record = GoalRecord(
        id=record_id,
        state=GoalState.ENTERING,
        worktree=identity,
        worktree_path=str(tmp_path / "worktree"),
        branch="goal/qa-campaign-20260922",
        original_ref="main",
        base_sha="3" * 40,
        entered_at="2026-09-22T01:00:00Z",
        goals=tuple(RecordedGoal(spec, 1) for spec in specs),
    )
    store = GoalStore(tmp_path / "project")
    with store.worktree_lock(identity) as lock:
        record = store.create(lock, record)
    with store.record_lock(record.id) as lock:
        store.save(lock, replace(record, state=GoalState.ACTIVE))
    state = DevelopmentState(slug=record_id, strict_criteria=True)
    state.init_criteria(
        {spec.key: True for spec in specs},
        criterion_params={spec.key: spec.params for spec in specs},
        strict=True,
    )
    group = goal_identity(record_id, {spec.key: 1 for spec in specs})
    return state, GoalProjection(store, record.id), group


def _recover_transaction(
    tmp_path: Path, facts: dict[str, Any], *, coverage_met: bool
) -> TransactionEvidence:
    state, projection, identity = _goal_state(tmp_path)
    archived = _write(tmp_path / "archived-state.json", state.to_dict(), pretty=True)
    shadow = DevelopmentState.from_json_object(state.to_dict())
    changes = [
        change
        for ordinal, key in enumerate(state.criteria)
        for change in shadow.set_criterion(
            key,
            coverage_met if key == "coverage_sim_toggle" else True,
            detail={"campaign_id": facts["campaign_id"], "ordinal": ordinal},
        )
    ]
    logs = tmp_path / "logs"

    def fail_save(checkpoint: str) -> None:
        if checkpoint == "before:acceptance_state":
            raise OSError("controlled state save failure")

    def record(
        checkpoint: Callable[[str], None] | None = None,
    ) -> AcceptanceTransaction:
        return record_or_verify_transaction(
            logs,
            state,
            changes,
            scope=GOAL_SCOPE,
            projection=projection,
            acceptance_facts=facts,
            identity=identity,
            publication_checkpoint=checkpoint,
        )

    with pytest.raises(OSError, match="controlled state save failure"):
        record(fail_save)
    failed = _write(tmp_path / "failed-state.json", state.to_dict(), pretty=True)
    transaction = record()
    # A third call proves product recovery does not select the transaction twice.
    replay = record()
    assert replay == transaction
    recovered = _write(tmp_path / "recovered-state.json", state.to_dict(), pretty=True)
    return TransactionEvidence(
        intent=next((logs / "acceptance/intents").glob("*.json")),
        transaction=logs / "acceptance/transactions" / f"{transaction.transaction_id}.json",
        evidence_root=logs / "acceptance/evidence",
        archived=archived,
        failed=failed,
        recovered=recovered,
        transaction_id=transaction.transaction_id,
    )


def _acceptance_evidence(tmp_path: Path, *, coverage_met: bool = True) -> RecoveryEvidence:
    campaign_id = "c3fa3451-3a73-4a43-936c-9a2c40f88d30"
    manifest_value = {
        "$schema": "booley.simulation-campaign-manifest/v1",
        "campaign_id": campaign_id,
    }
    manifest = _write(tmp_path / "manifest.json", manifest_value)
    manifest_sha256 = _sha256(_canonical(manifest_value))
    result = _result(tmp_path, campaign_id, manifest_sha256)
    facts = _facts(campaign_id, manifest_sha256, result)
    recovered = _recover_transaction(tmp_path, facts, coverage_met=coverage_met)
    simulation = _write(
        tmp_path / "simulation.json",
        {
            "complete": True,
            "campaign_manifest": {
                "path_base": "origin_target",
                "path": manifest.name,
                "kind": "simulation_campaign_manifest",
                "owner": campaign_id,
                "bytes": len(manifest.read_bytes()),
                "sha256": _sha256(manifest.read_bytes()),
            },
            "passed": True,
        },
    )
    return RecoveryEvidence(manifest, [result], simulation, recovered)


def _acceptance_args(evidence: RecoveryEvidence) -> tuple[object, ...]:
    transaction = evidence.transaction
    return (
        evidence.manifest,
        evidence.results,
        transaction.intent,
        transaction.transaction,
        transaction.evidence_root,
        transaction.archived,
        transaction.failed,
        transaction.recovered,
        evidence.simulation,
    )


@pytest.mark.parametrize("coverage_met", [True, False])
def test_acceptance_recovery_validator_authenticates_real_v2_evidence(
    tmp_path: Path, coverage_met: bool
) -> None:
    _module().validate_acceptance_recovery(
        *_acceptance_args(_acceptance_evidence(tmp_path, coverage_met=coverage_met))
    )


def _mutate_json(path: Path, mutation: Callable[[dict[str, Any]], None]) -> None:
    value = json.loads(path.read_text())
    mutation(value)
    _write(path, value)


def _add_extra_record(evidence: RecoveryEvidence) -> None:
    transaction_id = evidence.transaction.transaction_id
    extra = evidence.transaction.evidence_root / f"000000099.tx.{transaction_id}" / "record.json"
    _write(extra, {"unexpected": True})


def _add_duplicate_intent(evidence: RecoveryEvidence) -> None:
    intent = evidence.transaction.intent
    duplicate = intent.with_name("f" * 64 + ".json")
    duplicate.write_bytes(intent.read_bytes())


def _add_duplicate_commit(evidence: RecoveryEvidence) -> None:
    transaction = evidence.transaction.transaction
    duplicate = transaction.with_name("e" * 64 + ".json")
    duplicate.write_bytes(transaction.read_bytes())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda e: _mutate_json(
                e.transaction.intent,
                lambda d: d.update(acceptance_facts_sha256="sha256:" + "0" * 64),
            ),
            "facts digest",
        ),
        (
            lambda e: _mutate_json(
                e.transaction.transaction, lambda d: d.update(envelope_sha256="sha256:" + "0" * 64)
            ),
            "envelope digest",
        ),
        (
            lambda e: _mutate_json(
                e.transaction.transaction, lambda d: d["records"][0].update(transaction_ordinal=1)
            ),
            "record ordinal",
        ),
        (
            lambda e: _mutate_json(
                e.transaction.transaction, lambda d: d["records"][0].update(role="baseline")
            ),
            "record role",
        ),
        (
            lambda e: _mutate_json(
                e.transaction.transaction,
                lambda d: d["records"][0].update(sha256="sha256:" + "0" * 64),
            ),
            "record digest",
        ),
        (
            lambda e: _mutate_first_record(e, lambda d: d.update(role="baseline")),
            "contradicts its envelope",
        ),
        (
            lambda e: _mutate_first_record(e, lambda d: d.update(sequence=99)),
            "contradicts its envelope",
        ),
        (_add_extra_record, "outside its commit"),
        (_add_duplicate_intent, "multiple acceptance intents"),
        (_add_duplicate_commit, "multiple transactions"),
        (
            lambda e: e.transaction.failed.write_text(e.transaction.failed.read_text() + " "),
            "archived state bytes",
        ),
        (
            lambda e: _mutate_json(
                e.transaction.recovered,
                lambda d: d["criteria"]["sim_pass_sim_toggle"]["detail"].update(extra=True),
            ),
            "Criteria mutation",
        ),
    ],
)
def test_acceptance_recovery_validator_rejects_hostile_mutations(
    tmp_path: Path,
    mutation: Callable[[RecoveryEvidence], None],
    message: str,
) -> None:
    evidence = _acceptance_evidence(tmp_path)
    mutation(evidence)
    with pytest.raises(ValueError, match=message):
        _module().validate_acceptance_recovery(*_acceptance_args(evidence))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("kind", "simulation_result", "kind differs"), ("bytes", 0, "size differs")],
)
def test_acceptance_recovery_rejects_invalid_manifest_reference(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    evidence = _acceptance_evidence(tmp_path)
    _mutate_json(
        evidence.simulation,
        lambda document: document["campaign_manifest"].update({field: value}),
    )
    with pytest.raises(ValueError, match=message):
        _module().validate_acceptance_recovery(*_acceptance_args(evidence))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda d: d["envelope"]["goal"].update(record_id="other-20260922T010000Z"),
            "record ID differs",
        ),
        (lambda d: d["envelope"]["goal"].update(record_id="invalid"), "record ID is invalid"),
        (
            lambda d: d["envelope"]["goal"]["identity"].update(purpose="diagnostic"),
            "identity purpose",
        ),
        (lambda d: d["envelope"]["goal"]["identity"].update(goal_keys=[]), "keys are invalid"),
        (
            lambda d: d["envelope"]["goal"]["identity"]["goal_keys"].reverse(),
            "sorted and distinct",
        ),
        (
            lambda d: d["envelope"]["goal"]["identity"]["spec_revisions"].update(
                coverage_sim_toggle=True
            ),
            "revision is invalid",
        ),
        (
            lambda d: d["envelope"]["goal"]["identity"]["spec_revisions"].pop(
                "coverage_sim_toggle"
            ),
            "revisions fields differ",
        ),
        (
            lambda d: d["lookup_key"]["goal_identity"]["spec_revisions"].update(
                coverage_sim_toggle=2
            ),
            "lookup key differs",
        ),
    ],
)
def test_recovery_rejects_invalid_goal_identity(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    evidence = _acceptance_evidence(tmp_path)
    _mutate_json(evidence.transaction.intent, mutation)
    with pytest.raises(ValueError, match=message):
        _module().validate_acceptance_recovery(*_acceptance_args(evidence))


def test_recovery_rejects_state_from_another_goal(tmp_path: Path) -> None:
    evidence = _acceptance_evidence(tmp_path)
    transaction = evidence.transaction
    for path in (transaction.archived, transaction.failed, transaction.recovered):
        _mutate_json(path, lambda d: d.update(slug="other-20260922T010000Z"))
    with pytest.raises(ValueError, match="state Goal record ID differs"):
        _module().validate_acceptance_recovery(*_acceptance_args(evidence))


def _mutate_first_record(
    evidence: RecoveryEvidence, mutation: Callable[[dict[str, Any]], None]
) -> None:
    transaction = json.loads(evidence.transaction.transaction.read_text())
    sequence = transaction["records"][0]["sequence"]
    path = (
        evidence.transaction.evidence_root
        / f"{sequence:09d}.tx.{evidence.transaction.transaction_id}"
        / "record.json"
    )
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
