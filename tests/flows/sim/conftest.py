"""Shared Simulation test fixtures: compile-surface doubles and Windows lock emulation."""

import sys
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


def _lock_is_held(path: Path) -> bool:
    """Probe through a second open file description, as another Windows handle would."""
    import fcntl

    with path.open("a+b") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False


def _is_lock_file(path: Path) -> bool:
    return path.name.endswith(".lock") and path.is_file()


@pytest.fixture
def mandatory_file_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emulate Windows mandatory lock semantics on POSIX for retention tests.

    While a lock is held, other handles cannot read its sentinel byte and its
    containing directory cannot be renamed. Windows already behaves this way.
    """
    if sys.platform == "win32":
        return
    from booley.flows.sim import campaign_retention

    monkeypatch.setattr(campaign_retention, "_MANDATORY_FILE_LOCKS", True)
    read_bytes = Path.read_bytes
    rename = Path.rename

    def guarded_read_bytes(path: Path) -> bytes:
        if _is_lock_file(path) and _lock_is_held(path):
            raise PermissionError(13, "Permission denied", str(path))
        return read_bytes(path)

    def guarded_rename(path: Path, target: Path) -> Path:
        if path.is_dir() and any(
            _is_lock_file(child) and _lock_is_held(child) for child in path.rglob("*.lock")
        ):
            raise PermissionError(13, "Access is denied", str(path))
        return rename(path, target)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "rename", guarded_rename)
