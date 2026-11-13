"""Contract tests for transport-independent endpoint execution sequencing."""

from __future__ import annotations

import os
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.flows import job_resources
from booley.flows.base import FlowMechanics
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_SUCCESS,
    EndpointOutcome,
    EndpointRejectedError,
    ExecutionResult,
    execute_endpoint,
    normalize_completion_error,
)


@dataclass
class _ReleaseWitness(AbstractContextManager[object]):
    events: list[str]
    borrowed: object

    def __enter__(self) -> object:
        self.events.append("admit")
        return self.borrowed

    def __exit__(self, *exc_info: object) -> None:
        self.events.append("release")


class _Endpoint:
    """Small fake exposing the lifecycle operations owned outside the coordinator."""

    def __init__(
        self,
        *,
        fail: bool = False,
        reject_gate: bool = False,
        reject_admission: bool = False,
        unexpected_failure: bool = False,
        acceptance_failure: bool = False,
    ) -> None:
        self.events: list[str] = []
        self.fail = fail
        self.reject_gate = reject_gate
        self.reject_admission = reject_admission
        self.unexpected_failure = unexpected_failure
        self.acceptance_failure = acceptance_failure
        self.borrowed = object()

    def admission(self, prepared: object) -> AbstractContextManager[object]:
        assert prepared == "prepared"
        if self.reject_gate:
            self.events.append("gate-rejected")
            raise EndpointRejectedError(
                EndpointOutcome(exit_code=EXIT_ERROR, report_text="unbound target")
            )
        if self.reject_admission:
            self.events.append("admission-rejected")
            raise EndpointRejectedError(
                EndpointOutcome(exit_code=EXIT_ERROR, report_text="queue full")
            )
        return _ReleaseWitness(self.events, self.borrowed)

    def invoke_endpoint(
        self, prepared: object, *, admission: object, started: float
    ) -> EndpointOutcome:
        assert prepared == "prepared"
        assert admission is self.borrowed
        assert started > 0
        self.events.append("invoke")
        if self.unexpected_failure:
            raise RuntimeError("unexpected adapter failure")
        if self.fail:
            self.events.append("normalize-error")
            return EndpointOutcome(exit_code=EXIT_ERROR)
        return EndpointOutcome(
            exit_code=EXIT_SUCCESS,
            criterion_key="sim_pass_demo" if self.acceptance_failure else "",
            criterion_met=self.acceptance_failure,
            report_text="ok",
        )

    def finish_execution(
        self,
        prepared: object,
        outcome: EndpointOutcome,
        *,
        started: float | None,
        acceptance_recorded: bool,
    ) -> ExecutionResult:
        assert prepared == "prepared"
        if outcome.report_text not in {"queue full", "unbound target"}:
            assert started is not None
        self.events.append(f"finish:{outcome.exit_code}:{acceptance_recorded}")
        return ExecutionResult(exit_code=outcome.exit_code, outcome=outcome)

    def record_acceptance(self, prepared: object, outcome: EndpointOutcome) -> None:
        assert prepared == "prepared"
        self.events.append(f"acceptance:{outcome.exit_code}")
        if self.acceptance_failure:
            raise RuntimeError("acceptance failed")


def test_execute_endpoint_owns_admit_invoke_acceptance_finish_order() -> None:
    endpoint = _Endpoint()

    result = execute_endpoint(endpoint, "prepared")

    assert isinstance(result, ExecutionResult)
    assert result.exit_code == EXIT_SUCCESS
    assert result.outcome.report_text == "ok"
    assert endpoint.events == [
        "admit",
        "invoke",
        "acceptance:0",
        "finish:0:True",
        "release",
    ]


def test_execute_endpoint_normalizes_failure_and_releases_admission() -> None:
    endpoint = _Endpoint(fail=True)

    result = execute_endpoint(endpoint, "prepared")

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.exit_code == EXIT_ERROR
    assert endpoint.events == [
        "admit",
        "invoke",
        "normalize-error",
        "acceptance:2",
        "finish:2:True",
        "release",
    ]


def test_completion_error_message_is_bounded() -> None:
    outcome = EndpointOutcome()

    normalize_completion_error(outcome, RuntimeError("x" * 600), "publish report")

    message = outcome.detail["completion_error"]["message"]
    assert len(message) == 500
    assert message.endswith("…")


def test_admission_rejection_finishes_without_invoking_endpoint() -> None:
    endpoint = _Endpoint(reject_admission=True)

    result = execute_endpoint(endpoint, "prepared")

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == "queue full"
    assert endpoint.events == [
        "admission-rejected",
        "acceptance:2",
        "finish:2:True",
    ]


def test_unexpected_adapter_failure_propagates_and_releases_admission() -> None:
    endpoint = _Endpoint(unexpected_failure=True)

    with pytest.raises(RuntimeError, match="unexpected adapter failure"):
        execute_endpoint(endpoint, "prepared")

    assert endpoint.events == ["admit", "invoke", "release"]


def test_acceptance_failure_runs_non_persisting_finish_and_releases_admission() -> None:
    endpoint = _Endpoint(acceptance_failure=True)

    result = execute_endpoint(endpoint, "prepared")

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.criterion_key == "sim_pass_demo"
    assert result.outcome.criterion_met is True
    assert result.outcome.detail["completion_error"] == {
        "operation": "record acceptance and projections",
        "type": "RuntimeError",
        "message": "acceptance failed",
    }
    assert result.outcome.report_text == (
        "ok\nCompletion failure (record acceptance and projections): "
        "RuntimeError: acceptance failed"
    )
    assert endpoint.events == [
        "admit",
        "invoke",
        "acceptance:0",
        "finish:2:False",
        "release",
    ]


def test_gate_rejection_finishes_before_admission() -> None:
    endpoint = _Endpoint(reject_gate=True)

    result = execute_endpoint(endpoint, "prepared")

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == "unbound target"
    assert endpoint.events == ["gate-rejected", "acceptance:2", "finish:2:True"]


class _CommandEndpoint(_Endpoint):
    """Endpoint whose Job runs one command through a Flow's local executor."""

    def __init__(self, flow: FlowMechanics, code: str) -> None:
        super().__init__()
        self.flow, self.code = flow, code

    def invoke_endpoint(
        self, prepared: object, *, admission: object, started: float
    ) -> EndpointOutcome:
        self.flow._execute_local([sys.executable, "-c", self.code])
        return EndpointOutcome(exit_code=EXIT_SUCCESS)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_job_outcome_records_only_its_own_oom_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.flows.test_flow_base import EchoFlow, _env_with_state

    from booley.criteria.state import DevelopmentState

    state_file = tmp_path / "state.json"
    DevelopmentState.load(state_file).save()
    flow = EchoFlow()
    with patch.dict(os.environ, _env_with_state(state_file)):
        flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
    monkeypatch.setenv("BOOLEY_DEV_MEMORY_ADMISSION", "1")
    # The container's OOM-kill counter moves during both Jobs' commands.
    counts = iter([0, 1, 1, 2])
    monkeypatch.setattr(job_resources, "cgroup_oom_kill_count", lambda: next(counts))
    killed = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"

    own_kill = execute_endpoint(_CommandEndpoint(flow, killed), "prepared")
    other_failure = execute_endpoint(_CommandEndpoint(flow, "raise SystemExit(1)"), "prepared")

    assert own_kill.outcome.oom_killed is True
    assert other_failure.outcome.oom_killed is False
