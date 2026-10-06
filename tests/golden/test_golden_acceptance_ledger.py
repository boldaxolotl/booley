"""Differential golden for the Ticket acceptance ledger's on-disk evidence tree.

The scenario drives only the public Ticket API
(``booley.ticket_board.acceptance_ledger``): V1 ``record_changes`` appends, a
V2 ``record_or_verify_transaction`` interrupted at a publication checkpoint,
a crash-left complete temporary record that recovery must promote, a replay
of the committed transaction, a re-selection into a state that lost it, a
frozen Criteria Satisfaction Record, and two rejected identities.

A second, parametrized scenario interrupts a V2 transaction once at every
publication checkpoint boundary, recovers it, and replays it; its declared
Criteria include the report-fenced ``_report_submitted``, so replay and the
projection check run Ticket report fencing.

Each golden snapshots every ledger file beneath the Ticket log directory byte
for byte, the checkpoint boundaries in order, the returned references, and
rejection messages. Two files are rendered platform-neutrally on purpose:
lock files show presence only (Windows lock acquisition writes a NUL byte),
and the text-mode ``booley_state.json`` is shown with LF line endings
(Windows text mode writes CRLF). The expected files were generated on main
before the ledger moved to ``booley.criteria.evidence_ledger``, so any drift
in canonical bytes, transaction hashes, paths, checkpoints, projection, or
error text shows up as a diff (see ``tests/golden/conftest.py`` for the
regeneration convention).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from booley.criteria.state import DevelopmentState
from booley.ticket_board.acceptance_ledger import (
    AcceptanceLedgerError,
    current_evidence_records,
    freeze_acceptance,
    record_changes,
    record_or_verify_transaction,
)

from .conftest import assert_matches_golden, normalize_work_dir

GOLDEN = "acceptance_ledger/ticket_evidence_tree.txt"
IDENTITY = {"generation": "d" * 32, "authored_sha256": "e" * 64}
FIXED_NOW = "2026-09-21T08:00:00Z"
CAMPAIGN_ID = "12345678-1234-4234-9234-123456789abc"


class _InterruptedError(Exception):
    """Simulated process death at one publication checkpoint."""


def _campaign_facts() -> dict[str, Any]:
    return {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": f"sha256:{'a' * 64}",
        "origin": {"execution_id": "b" * 32, "invocation_id": 7},
        "target": {"identity": "acme:lib:uart:1#sim_uart", "selector": "sim_uart"},
        "required_suite": {
            "names": ["test_tx"],
            "default_invocation": True,
            "source_sha256": f"sha256:{'c' * 64}",
        },
        "prerequisites": [],
        "consumed_results": [{"finished_at": "2026-09-21T10:00:00Z"}],
        "observations": [],
        "coverage_reference": None,
    }


def _checkpoint_recorder(
    log: list[str], *, interrupt_at: str | None = None, occurrence: int = 0
) -> Callable[[str], None]:
    """Record every boundary and optionally die at the Nth hit of one."""
    hits: dict[str, int] = {}

    def checkpoint(boundary: str) -> None:
        log.append(boundary)
        hits[boundary] = hits.get(boundary, 0) + 1
        if boundary == interrupt_at and hits[boundary] == occurrence:
            raise _InterruptedError(boundary)

    return checkpoint


def _transact(log_dir: Path, state: DevelopmentState, changes: list, checkpoint) -> Any:
    return record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(),
        ticket_identity=IDENTITY,
        publication_checkpoint=checkpoint,
    )


def _declared_state(log_dir: Path) -> DevelopmentState:
    state = DevelopmentState.load(log_dir / ".runtime" / "booley_state.json")
    state.slug = "fix-uart"
    state.ticket_type = "bugfix"
    state.init_criteria({"sim_pass_uart": True, "lint_clean": True}, strict=True)
    return state


def _v1_appends(log_dir: Path, state: DevelopmentState) -> list[dict[str, Any]]:
    lint = record_changes(
        log_dir,
        state,
        state.set_criterion("lint_clean", True, detail={"warnings": 0}),
        invocation_id="lint-1",
        producer="lint",
        execution_id="b" * 32,
        ticket_identity=IDENTITY,
        recorded_at="2026-09-21T09:00:00Z",
    )
    red = record_changes(
        log_dir,
        state,
        state.set_criterion("sim_pass_uart", False, detail={"failed_tests": ["test_tx"]}),
        invocation_id="sim-red",
        producer="sim",
        execution_id="b" * 32,
        ticket_identity=IDENTITY,
        recorded_at="2026-09-21T09:30:00Z",
    )
    return [asdict(ref) for ref in (*lint, *red)]


def _plant_recovered_temp(log_dir: Path, twin_root: Path, state_path: Path) -> None:
    """Leave the record a crashed publisher had staged but not yet renamed.

    A twin copy of the interrupted tree finishes the transaction; its final
    record for the missing ordinal is byte-identical to what the crashed
    process staged, so it is planted as the crash-left temporary.
    """
    twin = twin_root / "fix-uart"
    shutil.copytree(log_dir, twin)
    twin_state = DevelopmentState.load(twin / ".runtime" / "booley_state.json")
    _transact(twin, twin_state, [], None)
    evidence = log_dir / "acceptance" / "evidence"
    published = {path.name for path in evidence.iterdir()}
    staged = sorted(
        path
        for path in (twin / "acceptance" / "evidence").iterdir()
        if ".tx." in path.name and path.name not in published
    )
    assert len(staged) == 1, staged
    sequence, _separator, transaction_id = staged[0].name.partition(".tx.")
    temporary = evidence / (
        f".tmp.acceptance.tx-{transaction_id}.ord-00000001.seq-{sequence}.nonce-{'0' * 32}"
    )
    temporary.mkdir()
    shutil.copyfile(staged[0] / "record.json", temporary / "record.json")
    assert state_path.exists()


def _rejection(log_dir: Path, state: DevelopmentState, identity: dict[str, Any]) -> str:
    with pytest.raises(AcceptanceLedgerError) as caught:
        record_or_verify_transaction(
            log_dir,
            state,
            [],
            acceptance_facts=_campaign_facts(),
            ticket_identity=identity,
        )
    return str(caught.value)


def _run_scenario(root: Path) -> dict[str, Any]:
    log_dir = root / "logs" / "fix-uart"
    state_path = log_dir / ".runtime" / "booley_state.json"
    state = _declared_state(log_dir)
    result: dict[str, Any] = {"v1_refs": _v1_appends(log_dir, state)}
    state.save()

    changes = [
        *state.set_criterion("sim_pass_uart", True, detail={"passed_tests": ["test_tx"]}),
        *state.set_criterion("lint_clean", True, detail={"warnings": 0, "rerun": True}),
    ]
    state.save()
    interrupted: list[str] = []
    with pytest.raises(_InterruptedError):
        _transact(
            log_dir,
            state,
            changes,
            _checkpoint_recorder(
                interrupted, interrupt_at="before:acceptance_record", occurrence=2
            ),
        )
    result["interrupted_checkpoints"] = interrupted
    _plant_recovered_temp(log_dir, root / "twin", state_path)

    recovered_log: list[str] = []
    recovered_state = DevelopmentState.load(state_path)
    recovered = _transact(log_dir, recovered_state, changes, _checkpoint_recorder(recovered_log))
    result["recovered_checkpoints"] = recovered_log
    result["recovered"] = asdict(recovered)

    replay_log: list[str] = []
    replayed = _transact(log_dir, recovered_state, [], _checkpoint_recorder(replay_log))
    result["replay_checkpoints"] = replay_log
    result["replayed_equal"] = replayed == recovered

    lost = DevelopmentState.load(state_path)
    lost.acceptance_transactions = []
    lost.save()
    reselect_log: list[str] = []
    reselected = _transact(log_dir, lost, [], _checkpoint_recorder(reselect_log))
    result["reselect_checkpoints"] = reselect_log
    result["reselected_equal"] = reselected == recovered

    result["current_records"] = current_evidence_records(log_dir, lost, IDENTITY)
    frozen = freeze_acceptance(
        log_dir,
        lost,
        execution_id="b" * 32,
        ticket_identity=IDENTITY,
        participant_heads={"outer": "a" * 40},
        accepted_at="2026-09-21T11:00:00Z",
    )
    result["frozen_digest"] = frozen.digest
    result["rejections"] = [
        _rejection(log_dir, lost, {"generation": "xyz"}),
        _rejection(log_dir, lost, {"generation": "d" * 32, "bad": b"not json"}),
    ]
    return result


#: Files the state module writes in platform text mode (CRLF on Windows).
_TEXT_MODE_FILES = frozenset({".runtime/booley_state.json"})


def _render_tree(log_dir: Path) -> str:
    """Render every path beneath *log_dir*; ledger files byte for byte."""
    lines: list[str] = []
    for path in sorted(log_dir.rglob("*"), key=lambda item: item.relative_to(log_dir).as_posix()):
        relative = path.relative_to(log_dir).as_posix()
        if path.is_dir():
            lines.append(f"=== dir {relative}")
            continue
        if path.name.endswith(".lock"):
            # Lock files carry no evidence; on Windows acquisition writes a NUL byte.
            lines.append(f"=== lock {relative}")
            continue
        content = path.read_bytes()
        if relative in _TEXT_MODE_FILES:
            content = content.replace(b"\r\n", b"\n")
        lines.append(f"=== file {relative} ({len(content)} bytes)")
        lines.append(content.decode("utf-8"))
    return "\n".join(lines)


def test_ticket_evidence_tree_matches_golden(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("booley.criteria.state.utc_now_rfc3339", lambda: FIXED_NOW)
    result = _run_scenario(tmp_path)
    rendered = (
        json.dumps(result, indent=2, sort_keys=True)
        + "\n"
        + _render_tree(tmp_path / "logs" / "fix-uart")
    )
    assert_matches_golden(GOLDEN, normalize_work_dir(rendered, tmp_path))


#: Every publication checkpoint the V2 path names, in the order it emits them.
BOUNDARIES = (
    "before:acceptance_intent",
    "after:acceptance_intent",
    "before:acceptance_record",
    "after:acceptance_record",
    "before:acceptance_commit",
    "after:acceptance_commit",
    "before:acceptance_state",
    "after:acceptance_state",
)
REPORT_KEY = "_report_submitted"
#: A report candidate whose submission never committed: fencing masks it.
FENCED_REPORT_DETAIL = {"report_submission_id": "c" * 32, "report_sha256": "f" * 64}


def _report_fenced_state(log_dir: Path) -> tuple[DevelopmentState, list[Any]]:
    """Declare a report-fenced Criterion, append V1 evidence, and stage V2 changes."""
    state = DevelopmentState.load(log_dir / ".runtime" / "booley_state.json")
    state.slug = "fix-uart"
    state.ticket_type = "bugfix"
    state.init_criteria({"sim_pass_uart": True, "lint_clean": True, REPORT_KEY: True}, strict=True)
    record_changes(
        log_dir,
        state,
        state.set_criterion("lint_clean", True, detail={"warnings": 0}),
        invocation_id="lint-1",
        producer="lint",
        execution_id="b" * 32,
        ticket_identity=IDENTITY,
        recorded_at="2026-09-21T09:00:00Z",
    )
    changes = [
        *state.set_criterion("sim_pass_uart", True, detail={"passed_tests": ["test_tx"]}),
        *state.set_criterion(REPORT_KEY, True, detail=dict(FENCED_REPORT_DETAIL)),
    ]
    state.save()
    return state, changes


def _run_interrupted_scenario(root: Path, boundary: str) -> dict[str, Any]:
    log_dir = root / "logs" / "fix-uart"
    state_path = log_dir / ".runtime" / "booley_state.json"
    state, changes = _report_fenced_state(log_dir)
    result: dict[str, Any] = {"boundary": boundary}
    interrupted: list[str] = []
    with pytest.raises(_InterruptedError):
        _transact(
            log_dir,
            state,
            changes,
            _checkpoint_recorder(interrupted, interrupt_at=boundary, occurrence=1),
        )
    result["interrupted_checkpoints"] = interrupted

    recovered_log: list[str] = []
    recovered_state = DevelopmentState.load(state_path)
    recovered = _transact(log_dir, recovered_state, changes, _checkpoint_recorder(recovered_log))
    result["recovered_checkpoints"] = recovered_log
    result["recovered"] = asdict(recovered)
    result["recovered_report_met"] = recovered_state.criteria[REPORT_KEY].met

    replay_log: list[str] = []
    replayed = _transact(log_dir, recovered_state, [], _checkpoint_recorder(replay_log))
    result["replay_checkpoints"] = replay_log
    result["replayed_equal"] = replayed == recovered
    result["current_records"] = current_evidence_records(log_dir, recovered_state, IDENTITY)
    return result


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_interrupted_transaction_recovers_to_golden(
    tmp_path: Path, monkeypatch, boundary: str
) -> None:
    monkeypatch.setattr("booley.criteria.state.utc_now_rfc3339", lambda: FIXED_NOW)
    result = _run_interrupted_scenario(tmp_path, boundary)
    rendered = (
        json.dumps(result, indent=2, sort_keys=True)
        + "\n"
        + _render_tree(tmp_path / "logs" / "fix-uart")
    )
    golden = f"acceptance_ledger/interrupt_{boundary.replace(':', '_')}.txt"
    assert_matches_golden(golden, normalize_work_dir(rendered, tmp_path))
