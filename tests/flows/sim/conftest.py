"""Shared Simulation test fixtures: compile-surface and Target identity doubles, and host named-pipe support."""

from pathlib import Path

import pytest

from booley.bwave import waveform_store
from booley.flows.sim import elab_only, flow, verilator_coverage_execution
from booley.flows.sim.build_session import TargetCompileSurface
from booley.flows.sim.execution import engine
from booley.targets.domain import FuseSocError, TargetHandle
from booley.targets.selection import split_selector


@pytest.fixture(autouse=True)
def explicit_surface_for_lightweight_target_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give legacy handle doubles an explicit empty compile surface."""
    resolve = engine.resolve_target_compile_surface

    def resolve_or_fake(handle: TargetHandle) -> TargetCompileSurface:
        if not isinstance(handle, TargetHandle):
            return TargetCompileSurface(
                project_root=Path(handle.project_root).resolve(),
                authored_paths=(),
                operational_paths=(),
            )
        return resolve(handle)

    monkeypatch.setattr(engine, "resolve_target_compile_surface", resolve_or_fake)
    monkeypatch.setattr(elab_only, "resolve_target_compile_surface", resolve_or_fake)
    monkeypatch.setattr(
        verilator_coverage_execution, "resolve_target_compile_surface", resolve_or_fake
    )


@pytest.fixture(autouse=True)
def declared_trace_files_convert_after_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep declared-file tests host-independent: streaming off means no pipes."""
    monkeypatch.setattr(waveform_store, "can_stream_waveform", lambda: False)


@pytest.fixture(autouse=True)
def bare_identity_for_targets_without_a_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """Layer tests with no ``.core``: an undeclared Target's identity is its bare name."""
    # Private seam: no public input names a Target that has no .core; every record path calls this one method.
    identity = flow.SimulateFlow._target_identity

    def identity_or_bare(self: flow.SimulateFlow, target: str) -> str:
        try:
            return identity(self, target)
        except FuseSocError:
            return split_selector(target)[1]

    monkeypatch.setattr(flow.SimulateFlow, "_target_identity", identity_or_bare)
