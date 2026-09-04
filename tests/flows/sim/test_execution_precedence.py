"""Build, wrapper, and adapter-result precedence tests."""

from pathlib import Path

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTransportIdentity,
    write_adapter_result,
)
from booley.flows.sim.build import BuildOutcome
from booley.flows.sim.execution.engine import (
    AdapterEvidenceError,
    read_completed_adapter_result,
)


def _identity(tmp_path: Path) -> AdapterTransportIdentity:
    return AdapterTransportIdentity(
        adapter="icarus",
        attempt_token="abc123",
        target_identity="acme:lib:core:1#sim",
        selected_tests=("smoke",),
        result_path=tmp_path / "adapter-result.json",
    )


def _build(*, passed: bool = True, design_failed: bool = False) -> BuildOutcome:
    return BuildOutcome(
        ran=True,
        verdict="pass" if passed else "fail",
        failure_kind=None if passed else "design" if design_failed else "infrastructure",
        terminal_record=True,
    )


def test_normal_completion_requires_terminal_transport(tmp_path: Path) -> None:
    process = SubprocessResult(returncode=0, dispatched_unix=10.0)

    with pytest.raises(AdapterEvidenceError, match="without authenticated"):
        read_completed_adapter_result(_identity(tmp_path), process, _build())


def test_timeout_precedes_missing_terminal_transport(tmp_path: Path) -> None:
    process = SubprocessResult(returncode=-9, timed_out=True, dispatched_unix=10.0)

    assert read_completed_adapter_result(_identity(tmp_path), process, _build()) is None


def test_design_rejection_does_not_require_adapter_transport(tmp_path: Path) -> None:
    process = SubprocessResult(returncode=1, dispatched_unix=10.0)

    assert (
        read_completed_adapter_result(
            _identity(tmp_path), process, _build(passed=False, design_failed=True)
        )
        is None
    )


def test_adapter_pass_cannot_override_nonzero_process_exit(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    write_adapter_result(
        identity,
        AdapterResult(True, False, 0, identity.selected_tests),
    )
    process = SubprocessResult(returncode=1, dispatched_unix=10.0)

    with pytest.raises(AdapterEvidenceError, match="contradicts"):
        read_completed_adapter_result(identity, process, _build())
