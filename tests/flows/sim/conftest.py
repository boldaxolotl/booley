"""Shared Simulation test adaptations for lightweight TargetHandle doubles."""

from pathlib import Path

import pytest

from booley.flows.sim import flow, verilator_coverage_execution
from booley.flows.sim.build_session import TargetCompileSurface
from booley.flows.sim.execution import engine
from booley.targets.domain import TargetHandle


@pytest.fixture(autouse=True)
def explicit_surface_for_lightweight_target_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give legacy handle doubles an explicit empty compile surface."""
    resolve = engine.resolve_target_compile_surface

    def resolve_or_fake(handle: TargetHandle) -> TargetCompileSurface:
        if not hasattr(handle, "snapshot_id"):
            return TargetCompileSurface(
                project_root=Path(handle.project_root).resolve(),
                authored_paths=(),
                operational_paths=(),
            )
        return resolve(handle)

    monkeypatch.setattr(engine, "resolve_target_compile_surface", resolve_or_fake)
    monkeypatch.setattr(flow, "resolve_target_compile_surface", resolve_or_fake)
    monkeypatch.setattr(
        verilator_coverage_execution, "resolve_target_compile_surface", resolve_or_fake
    )
