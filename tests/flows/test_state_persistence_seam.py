"""A recorder-supplied persistence strategy reaches every state the endpoint saves.

``EndpointState.read_state`` asks the endpoint's acceptance recorder for a
``StatePersistence`` and hands it to the loaded state. These tests drive real
writers (Criterion acceptance, run-state persistence, Coverage publication)
and check each save arrives at the injected strategy.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from unittest import mock

import pytest

from booley.criteria.state import AtomicStateFile, CriterionEntry, DevelopmentState
from booley.flows.execution_persistence import (
    NoAcceptanceRecorder,
    StandaloneFlowExecution,
    state_persistence_for,
)
from booley.ticket_board.flow_execution import TicketAcceptanceRecorder, TicketBoardFlowExecution
from tests.mcp_tools.test_base import ConcreteMcpTool, SimLikeMcpTool, _env_with_state


class RecordingPersistence(AtomicStateFile):
    """Default file write that also records which writer called ``save()``."""

    def __init__(self) -> None:
        self.writers: list[str] = []
        self.loaded_states: list[DevelopmentState] = []

    def loaded(self, state: DevelopmentState) -> None:
        self.loaded_states.append(state)

    def save(self, state: DevelopmentState) -> None:
        # Frame 0 is this method, 1 is DevelopmentState.save, 2 is the writer.
        self.writers.append(inspect.stack()[2].function)
        super().save(state)


class PersistingRecorder(TicketAcceptanceRecorder):
    """Ticket recorder that also supplies a state persistence strategy."""

    def __init__(self, persistence: RecordingPersistence) -> None:
        super().__init__()
        self._persistence = persistence

    def state_persistence(self) -> RecordingPersistence:
        return self._persistence


def _with_recorder(endpoint: ConcreteMcpTool, persistence: RecordingPersistence):
    endpoint._acceptance_recorder = PersistingRecorder(persistence)
    return endpoint


@pytest.mark.parametrize(
    "recorder",
    [
        NoAcceptanceRecorder(),
        StandaloneFlowExecution(),
        TicketAcceptanceRecorder(),
        TicketBoardFlowExecution(),
    ],
    ids=lambda recorder: type(recorder).__name__,
)
def test_existing_recorders_keep_the_default_strategy(recorder, tmp_path: Path) -> None:
    endpoint = ConcreteMcpTool()
    endpoint._acceptance_recorder = recorder
    with mock.patch.dict(os.environ, _env_with_state(tmp_path / "state.json")):
        endpoint.parse_args([])

    assert state_persistence_for(recorder) is None
    assert endpoint.read_state()._persistence is None


def test_read_state_attaches_strategy_with_and_without_a_state_file(tmp_path: Path) -> None:
    persistence = RecordingPersistence()
    endpoint = _with_recorder(ConcreteMcpTool(), persistence)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("BOOLEY_")}
    with mock.patch.dict(os.environ, environment, clear=True):
        endpoint.parse_args([])
    in_memory = endpoint.read_state()
    assert in_memory._persistence is persistence
    assert in_memory._file_path is None
    assert persistence.loaded_states == [in_memory]

    with mock.patch.dict(os.environ, _env_with_state(tmp_path / "state.json")):
        endpoint.parse_args([])
    loaded = endpoint.read_state()
    assert loaded._persistence is persistence
    assert persistence.loaded_states == [in_memory, loaded]


def test_acceptance_and_run_state_saves_reach_the_strategy(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state = DevelopmentState.load(state_file)
    state.init_criteria({"sim_pass_default": True})
    state.save()
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    persistence = RecordingPersistence()
    endpoint = _with_recorder(SimLikeMcpTool(), persistence)

    with mock.patch.dict(os.environ, _env_with_state(state_file)):
        exit_code = endpoint.main(["--work-dir", str(tmp_path)])

    assert exit_code == 0
    assert persistence.writers == ["set_criterion", "_persist_run_state"]
    saved = DevelopmentState.load(state_file)
    assert saved.criteria["sim_pass_default"].met is True
    assert saved.timeline[-1]["mcp_tool"] == "sim"


def test_coverage_publication_shadow_saves_through_the_strategy(tmp_path: Path) -> None:
    from booley.flows.sim.coverage_acceptance import CoverageAcceptance
    from booley.flows.sim.coverage_campaign import DurableTargetIdentity
    from booley.flows.sim.coverage_invocation import (
        CoverageInvocationRequest,
        prepare_coverage_invocation,
    )
    from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold
    from booley.flows.sim.coverage_transaction import run_coverage_target
    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution, Progress

    context = project(tmp_path)
    state_file = tmp_path / "state.json"
    persistence = RecordingPersistence()
    endpoint = _with_recorder(ConcreteMcpTool(), persistence)
    with mock.patch.dict(os.environ, _env_with_state(state_file)):
        endpoint.parse_args([])
    state = endpoint.read_state()
    state.criteria = {"coverage_sim_0": CriterionEntry(), "sim_pass_sim_0": CriterionEntry()}
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        None,
    )
    prepared = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",)),
        replace(context, criteria={"coverage_sim_0": criterion}),
    )
    plan = replace(
        prepared.plan.targets[0],
        invocation_dir=tmp_path / "reports/sim/1",
        acceptance=CoverageAcceptance(state, endpoint._acceptance_recorder),
    )

    run_coverage_target(plan, NativeExecution(verdict="fail"), Progress())

    assert persistence.writers == ["publish"]
    saved = DevelopmentState.load(state_file)
    assert saved.criteria["coverage_sim_0"].met is True
    assert saved.criteria["sim_pass_sim_0"].met is False
