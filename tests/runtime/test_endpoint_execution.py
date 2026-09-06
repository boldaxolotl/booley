"""Contract tests for transport-independent endpoint execution sequencing."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass

import pytest

from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_SUCCESS,
    EndpointOutcome,
    EndpointRejectedError,
    ExecutionResult,
    execute_endpoint,
)


@dataclass
class _ReleaseWitness(AbstractContextManager[None]):
    events: list[str]

    def __enter__(self) -> None:
        self.events.append("admit")

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

    def admission(self, prepared: object) -> AbstractContextManager[None]:
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
        return _ReleaseWitness(self.events)

    def invoke_endpoint(self, prepared: object, *, started: float) -> EndpointOutcome:
        assert prepared == "prepared"
        assert started > 0
        self.events.append("invoke")
        if self.unexpected_failure:
            raise RuntimeError("unexpected adapter failure")
        if self.fail:
            self.events.append("normalize-error")
            return EndpointOutcome(exit_code=EXIT_ERROR)
        return EndpointOutcome(exit_code=EXIT_SUCCESS, report_text="ok")

    def finish_execution(
        self,
        prepared: object,
        outcome: EndpointOutcome,
        *,
        started: float | None,
        acceptance_recorded: bool,
    ) -> int:
        assert prepared == "prepared"
        if outcome.report_text not in {"queue full", "unbound target"}:
            assert started is not None
        self.events.append(f"finish:{outcome.exit_code}:{acceptance_recorded}")
        return outcome.exit_code

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


def test_endpoint_outcome_keeps_the_existing_structured_verdict_fields() -> None:
    outcome = EndpointOutcome(
        exit_code=EXIT_SUCCESS,
        criterion_key="lint_clean_lint_uart",
        criterion_met=True,
        detail={"warnings": 0},
        report_text="RESULT: PASS",
        display_lines=["0 warnings"],
        summary="lint clean",
    )

    assert outcome.detail == {"warnings": 0}
    assert outcome.display_lines == ["0 warnings"]
    assert outcome.summary == "lint clean"


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

    with pytest.raises(RuntimeError, match="acceptance failed"):
        execute_endpoint(endpoint, "prepared")

    assert endpoint.events == [
        "admit",
        "invoke",
        "acceptance:0",
        "finish:0:False",
        "release",
    ]


def test_gate_rejection_finishes_before_admission() -> None:
    endpoint = _Endpoint(reject_gate=True)

    result = execute_endpoint(endpoint, "prepared")

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == "unbound target"
    assert endpoint.events == ["gate-rejected", "acceptance:2", "finish:2:True"]
