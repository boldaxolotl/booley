"""Pieces shared by fresh and resumed Simulation Campaigns."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.test_runs import (
    TestRun,
)
from booley.runtime.endpoint_execution import (
    EndpointOutcome,
)
from booley.targets.domain import (
    TargetHandle,
)

from .execution import (
    SimulationExecution,
)


@dataclass(frozen=True, slots=True)
class _SimulationRecordSteps:
    """What one Campaign kind does inside the shared progress lifecycle."""

    terminal: Callable[[str], None]  # publishes the terminal progress phase
    run: Callable[[], EndpointOutcome]  # first checkpoint through the endpoint result
    finished: Callable[[EndpointOutcome], bool] = lambda _result: True


@dataclass(frozen=True, slots=True)
class _SimulationRecordRun:
    """What distinguishes one Campaign kind in the shared execution scaffold."""

    refusal: str  # report text when no admission context was borrowed
    refusal_detail: Mapping[str, object]
    progress_operation: str  # completion-error operation when terminal progress fails
    start: Callable[
        [AdmissionContext, Path, Callable[[Path], None]], _SimulationRecordSteps | EndpointOutcome
    ]
    failure: Callable[[Exception, Path], EndpointOutcome]  # an error before any result exists
    paths_on_success: bool = False


def _group_disclosures(
    execution: SimulationExecution,
    handle: TargetHandle,
    groups: Sequence[tuple[str, ...]],
    run_groups: Sequence[tuple[TestRun, ...]],
) -> tuple[dict[str, object], ...]:
    """Disclose the build of each group's Test Runs, not of their Tests' defaults."""
    return tuple(
        execution.plan_ordinary_group(handle, group, runs)
        for group, runs in zip(groups, run_groups, strict=True)
    )
