"""Contract tests for transport-independent endpoint execution sequencing."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass

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
        reject_prepare: bool = False,
        reject_gate: bool = False,
        reject_admission: bool = False,
    ) -> None:
        self.events: list[str] = []
        self.fail = fail
        self.reject_prepare = reject_prepare
        self.reject_gate = reject_gate
        self.reject_admission = reject_admission

    def prepare_execution(self, argv: list[str] | None) -> object:
        self.events.append(f"prepare:{argv!r}")
        if self.reject_prepare:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text="pre-state")
        return "prepared"

    def reject_execution(self, prepared: object) -> EndpointOutcome | None:
        assert prepared == "prepared"
        self.events.append("gate")
        if self.reject_gate:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text="unbound target")
        return None

    def admission(self, prepared: object) -> AbstractContextManager[None]:
        assert prepared == "prepared"
        if self.reject_admission:
            self.events.append("admission-rejected")
            raise EndpointRejectedError(
                EndpointOutcome(exit_code=EXIT_ERROR, report_text="queue full")
            )
        return _ReleaseWitness(self.events)

    def begin_invocation(self, prepared: object) -> None:
        assert prepared == "prepared"
        self.events.append("begin")

    def invoke_endpoint(self, prepared: object, *, started: float) -> EndpointOutcome:
        assert prepared == "prepared"
        assert started > 0
        self.events.append("invoke")
        if self.fail:
            raise RuntimeError("boom")
        return EndpointOutcome(exit_code=EXIT_SUCCESS, report_text="ok")

    def invocation_failed(self, exc: Exception) -> EndpointOutcome:
        assert str(exc) == "boom"
        self.events.append("normalize-error")
        return EndpointOutcome(exit_code=EXIT_ERROR)

    def finish_execution(
        self,
        prepared: object,
        outcome: EndpointOutcome,
        *,
        started: float | None,
    ) -> int:
        assert prepared == "prepared"
        if outcome.report_text not in {"queue full", "unbound target"}:
            assert started is not None
        self.events.append(f"finish:{outcome.exit_code}")
        return outcome.exit_code


def test_execute_endpoint_owns_prepare_gate_admit_invoke_finish_order() -> None:
    endpoint = _Endpoint()

    result = execute_endpoint(endpoint, ["--target", "lint_uart"])

    assert isinstance(result, ExecutionResult)
    assert result.exit_code == EXIT_SUCCESS
    assert result.outcome.report_text == "ok"
    assert endpoint.events == [
        "prepare:['--target', 'lint_uart']",
        "gate",
        "admit",
        "begin",
        "invoke",
        "finish:0",
        "release",
    ]


def test_execute_endpoint_normalizes_failure_and_releases_admission() -> None:
    endpoint = _Endpoint(fail=True)

    result = execute_endpoint(endpoint, [])

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.exit_code == EXIT_ERROR
    assert endpoint.events == [
        "prepare:[]",
        "gate",
        "admit",
        "begin",
        "invoke",
        "normalize-error",
        "finish:2",
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

    result = execute_endpoint(endpoint, [])

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == "queue full"
    assert endpoint.events == [
        "prepare:[]",
        "gate",
        "admission-rejected",
        "finish:2",
    ]


def test_preparation_rejection_skips_stateful_finish_and_admission() -> None:
    endpoint = _Endpoint(reject_prepare=True)

    result = execute_endpoint(endpoint, [])

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == "pre-state"
    assert endpoint.events == ["prepare:[]"]


def test_gate_rejection_finishes_before_admission() -> None:
    endpoint = _Endpoint(reject_gate=True)

    result = execute_endpoint(endpoint, [])

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == "unbound target"
    assert endpoint.events == ["prepare:[]", "gate", "finish:2"]
