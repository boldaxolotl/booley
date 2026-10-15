"""Interactive recording preserves V1 evidence and refuses V2 transactions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.criteria import evidence_ledger
from booley.criteria import unbound_ledger as acceptance_ledger
from booley.criteria.state import DevelopmentState
from booley.flows.execution_persistence import (
    AcceptanceRecordingError,
    CriterionFreshness,
    done_findings_require_approval_for,
)
from booley.mcp.unbound_recorder import UnboundAcceptanceRecorder
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.mcp_tools.test_base import ConcreteMcpTool


@pytest.fixture(autouse=True)
def unbound_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BOOLEY_LOGS_DIR",
        "BOOLEY_STATE_FILE",
        "BOOLEY_RUN_ID",
        "BOOLEY_EXECUTION_ID",
        "BOOLEY_SLUG",
    ):
        monkeypatch.delenv(key, raising=False)


def test_unbound_v1_record_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = tmp_path / ".interactive_logs" / "session"
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    monkeypatch.setenv("BOOLEY_RUN_ID", "run-id")
    monkeypatch.setenv("BOOLEY_EXECUTION_ID", "execution-id")
    monkeypatch.setattr(evidence_ledger, "utc_now_rfc3339", lambda: "2026-10-10T12:00:00Z")
    state = DevelopmentState(slug="interactive")
    state.init_criteria({"sim_pass_uart": True})
    changes = state.set_criterion("sim_pass_uart", True, detail={"target": "sim_uart"})

    UnboundAcceptanceRecorder().record_changes(
        state, changes, invocation_id="ignored", producer="sim"
    )

    assert (logs / "acceptance/evidence/000000001/record.json").read_bytes() == (
        b'{"criterion":"sim_pass_uart","detail":{"target":"sim_uart"},'
        b'"execution_id":"execution-id","invocation_id":"run-id","mandatory":true,'
        b'"met":true,"params":{},"producer":"sim","purpose":"ticket_acceptance",'
        b'"reason":"outcome","recorded_at":"2026-10-10T12:00:00Z","role":"candidate",'
        b'"schema":1,"sequence":1,"ticket":"interactive","ticket_identity":{}}\n'
    )


def test_unbound_run_timeline_is_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_file = tmp_path / ".interactive_logs/session/booley_state.json"
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_file))
    endpoint = ConcreteMcpTool()
    endpoint.parse_args([])
    endpoint.read_state()

    endpoint._post_run(EndpointOutcome(exit_code=0), duration=1.25)

    saved = json.loads(state_file.read_bytes())
    assert saved["timeline"][-1]["mcp_tool"] == "test_endpoint"
    assert saved["timeline"][-1]["exit_code"] == 0
    assert saved["timeline"][-1]["duration_s"] == 1.25


def test_unbound_identity_and_reviewer_policies() -> None:
    recorder = UnboundAcceptanceRecorder()
    assert recorder.acceptance_identity() == {}
    assert not isinstance(recorder, CriterionFreshness)
    assert done_findings_require_approval_for(recorder) is False


def test_unbound_recording_without_logs_is_a_noop(tmp_path: Path) -> None:
    state = DevelopmentState()
    state.init_criteria({"sim_pass_uart": True})
    changes = state.set_criterion("sim_pass_uart", True)
    recorder = UnboundAcceptanceRecorder()
    recorder.record_changes(state, changes, invocation_id="1", producer="sim")
    with pytest.raises(AcceptanceRecordingError, match="unbound Interactive recording"):
        recorder.record_or_verify_transaction(state, changes, acceptance_facts={}, identity={})
    assert list(tmp_path.iterdir()) == []


def test_unbound_ledger_error_translation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise acceptance_ledger.AcceptanceLedgerError("ledger failed")

    monkeypatch.setattr(acceptance_ledger, "record_changes", fail)
    recorder = UnboundAcceptanceRecorder(log_dir=tmp_path)
    state = DevelopmentState()
    with pytest.raises(AcceptanceRecordingError, match="ledger failed") as caught:
        recorder.record_changes(state, [], invocation_id="1", producer="sim")
    assert isinstance(caught.value.__cause__, acceptance_ledger.AcceptanceLedgerError)


def test_unbound_v2_empty_identity_is_rejected(tmp_path: Path) -> None:
    facts = {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": "12345678-1234-4234-9234-123456789abc",
        "manifest_sha256": f"sha256:{'a' * 64}",
        "origin": {"execution_id": "b" * 32, "invocation_id": 1},
        "target": {"identity": "acme:lib:uart:1#sim_uart", "selector": "sim_uart"},
        "required_suite": {
            "names": ["test_tx"],
            "default_invocation": True,
            "source_sha256": f"sha256:{'c' * 64}",
        },
        "prerequisites": [],
        "consumed_results": [{"finished_at": "2026-10-10T12:00:00Z"}],
        "observations": [],
        "coverage_reference": None,
    }
    recorder = UnboundAcceptanceRecorder(log_dir=tmp_path)
    with pytest.raises(
        AcceptanceRecordingError,
        match="unbound Interactive recording cannot record a Simulation Campaign transaction",
    ):
        recorder.record_or_verify_transaction(
            DevelopmentState(), [], acceptance_facts=facts, identity={}
        )
    assert list(tmp_path.iterdir()) == []


def test_unbound_v2_nonempty_identity_is_rejected_without_writes(tmp_path):
    recorder = UnboundAcceptanceRecorder(log_dir=tmp_path)
    with pytest.raises(
        AcceptanceRecordingError,
        match="unbound Interactive recording cannot record a Simulation Campaign transaction",
    ):
        recorder.record_or_verify_transaction(
            DevelopmentState(),
            [],
            acceptance_facts={},
            identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
        )
    assert list(tmp_path.iterdir()) == []
