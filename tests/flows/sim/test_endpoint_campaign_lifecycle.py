"""Public lifecycle contract for prepared Simulation Campaign execution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows import endpoint_session
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.sim.flow import SimulateFlow
from booley.runtime.endpoint_execution import EndpointOutcome, ExecutionResult, execute_endpoint


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
    ) -> ExecutionResult:
        self.events.append("finish")
        return ExecutionResult(outcome.exit_code, outcome)


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


def _request(selector: str) -> SimpleNamespace:
    return SimpleNamespace(
        plan=SimpleNamespace(
            manifest=SimpleNamespace(
                document={
                    "target": {
                        "selector": selector,
                        "vlnv": "acme:demo:target:1.0",
                        "name": selector,
                    }
                }
            )
        )
    )


def _retained_campaign_outcome(outcomes: list[object]) -> EndpointOutcome:
    targets = {f"target-{index}": {"simulation": "pass"} for index, _ in enumerate(outcomes)}
    return EndpointOutcome(detail={"targets": targets}, report_text="campaign verdict")


def test_completed_campaign_survives_per_target_progress_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    outcomes = [object(), object()]
    campaign = SimpleNamespace(run=lambda _request: outcomes.pop(0))
    progress = SimpleNamespace(
        invocation_dir=tmp_path,
        checkpoint=lambda **_kwargs: (_ for _ in ()).throw(OSError("progress unavailable")),
    )
    monkeypatch.setattr(flow, "_campaign_endpoint_outcome", _retained_campaign_outcome)
    monkeypatch.setattr(
        "booley.flows.sim.flow._retain_coverage_campaign",
        lambda _progress, _outcome: None,
    )

    result = flow._execute_coverage_campaign_requests(
        campaign, [_request("first"), _request("second")], progress
    )

    assert result.exit_code == 2
    assert result.detail["targets"] == {"target-0": {"simulation": "pass"}}
    assert result.detail["pending_targets"] == ["second"]
    assert result.detail["completion_error"]["path"] == str(tmp_path / "progress.json")
    assert len(outcomes) == 1


def test_coverage_authentication_failure_returns_structured_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    flow._coverage_prepared = SimpleNamespace(targets=[])
    completed = [object(), object()]
    pending = list(completed)
    campaign = SimpleNamespace(run=lambda _request: pending.pop(0))
    progress = SimpleNamespace(invocation_dir=tmp_path, checkpoint=lambda **_kwargs: None)
    monkeypatch.setattr(flow, "_campaign_endpoint_outcome", _retained_campaign_outcome)
    monkeypatch.setattr(
        "booley.flows.sim.flow._retain_coverage_campaign",
        lambda _progress, _outcome: (_ for _ in ()).throw(ValueError("untrusted campaign")),
    )

    result = flow._execute_coverage_campaign_requests(
        campaign, [_request("first"), _request("second")], progress
    )

    assert result.exit_code == 2
    assert result.detail["campaigns"] == {}
    assert result.detail["targets"] == {}
    assert "untrusted campaign" in result.report_text
    assert len(pending) == 1


def test_all_campaigns_survive_terminal_progress_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    flow._args = SimpleNamespace(report_dir=tmp_path)
    flow._coverage_prepared = SimpleNamespace(
        targets=[
            SimpleNamespace(handle=SimpleNamespace(selector="first")),
            SimpleNamespace(handle=SimpleNamespace(selector="second")),
        ]
    )
    returned = [object(), object()]
    pending = list(returned)
    campaign = SimpleNamespace(run=lambda _request: pending.pop(0))
    requests = [_request("first"), _request("second")]

    progress = SimpleNamespace(invocation_dir=tmp_path, targets=("first", "second"), outcomes=[])
    terminal_phases: list[str] = []

    def checkpoint(*, complete: bool = False, phase: str | None = None) -> None:
        if complete:
            assert phase is not None
            terminal_phases.append(phase)
            raise OSError("terminal progress unavailable")

    progress.checkpoint = checkpoint
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: tmp_path)
    monkeypatch.setattr(
        flow,
        "_coverage_campaign_session",
        lambda _admission, _invocation: (campaign, requests),
    )
    monkeypatch.setattr("booley.flows.sim.flow.CoverageProgress", lambda *_args: progress)
    monkeypatch.setattr(flow, "_campaign_endpoint_outcome", _retained_campaign_outcome)
    monkeypatch.setattr(
        "booley.flows.sim.flow._retain_coverage_campaign",
        lambda retained, outcome: retained.outcomes.append(outcome),
    )

    result = flow._run_coverage_campaigns(
        AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False)
    )

    assert result.exit_code == 2
    assert result.detail["targets"] == {
        "target-0": {"simulation": "pass"},
        "target-1": {"simulation": "pass"},
    }
    assert result.detail["completion_error"]["operation"] == "publish coverage progress"
    assert terminal_phases == ["complete", "aborted"]
    assert pending == []


def test_legacy_result_survives_terminal_progress_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    flow.context._reserved_invocation_dir = tmp_path
    monkeypatch.setattr(flow, "_prepare_legacy_run", lambda *_args: (["PASS"], True))
    monkeypatch.setattr(
        flow,
        "_legacy_endpoint_outcome",
        lambda *_args: EndpointOutcome(detail={"targets": {"first": {"simulation": "pass"}}}),
    )
    monkeypatch.setattr(
        flow,
        "_write_progress_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("progress unavailable")),
    )

    result = flow._run_legacy_selected_mode(["first"], {}, 0.0, 0.0)

    assert result.exit_code == 2
    assert result.detail["targets"] == {"first": {"simulation": "pass"}}
    assert result.detail["completion_error"]["operation"] == "publish simulation progress"
    assert result.detail["completion_error"]["path"] == str(tmp_path / "progress.json")
