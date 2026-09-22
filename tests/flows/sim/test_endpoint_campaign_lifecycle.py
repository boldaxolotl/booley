"""Public lifecycle contract for prepared Simulation Campaign execution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from booley.flows import endpoint_session
from booley.flows.endpoint_session import PreparedExecution
from booley.runtime.endpoint_execution import EndpointOutcome, execute_endpoint


class _Flow:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def run_prepared_simulation(
        self, prepared: object, admission: object | None
    ) -> EndpointOutcome:
        self.events.append("invoke")
        assert prepared == ("target",)
        assert admission == "borrowed-admission"
        return EndpointOutcome(report_text="ok")


class _Endpoint:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.flow = _Flow(self.events)
        self.name = "sim"

    @contextmanager
    def admission(self, prepared: PreparedExecution) -> Iterator[object]:
        assert prepared.simulation == ("target",)
        self.events.append("admit")
        try:
            yield "borrowed-admission"
        finally:
            self.events.append("release")

    def invoke_endpoint(
        self,
        prepared: PreparedExecution,
        *,
        admission: object | None,
        started: float,
    ) -> EndpointOutcome:
        return endpoint_session.invoke_endpoint(
            self,
            prepared,
            admission=admission,
            started=started,  # type: ignore[arg-type]
        )

    def _adapt_outcome(self, outcome: EndpointOutcome) -> EndpointOutcome:
        return outcome

    def _finalize_result(self, outcome: EndpointOutcome) -> None:
        self.events.append("finalize")

    def record_acceptance(self, prepared: PreparedExecution, outcome: EndpointOutcome) -> None:
        self.events.append("acceptance")

    def finish_execution(
        self,
        prepared: PreparedExecution,
        outcome: EndpointOutcome,
        *,
        started: float | None,
        acceptance_recorded: bool,
    ) -> int:
        self.events.append("finish")
        return outcome.exit_code


def test_prepared_campaign_and_borrowed_admission_are_explicit_values() -> None:
    endpoint = _Endpoint()
    prepared = PreparedExecution(None, None, False, False, simulation=("target",))

    result = execute_endpoint(endpoint, prepared)

    assert result.outcome.report_text == "ok"
    assert endpoint.events == [
        "admit",
        "invoke",
        "finalize",
        "acceptance",
        "finish",
        "release",
    ]
    assert not hasattr(endpoint, "_simulation_prepared")
    assert not hasattr(endpoint, "_simulation_admission_context")
