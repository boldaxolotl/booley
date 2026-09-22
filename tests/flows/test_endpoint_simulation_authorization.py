"""Strict Ticket authorization for prepared Simulation Campaign Targets."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest

from booley.criteria.state import DevelopmentState
from booley.evidence.acceptance import AcceptanceTargetBinding, ResolvedFlowAcceptance
from booley.flows import endpoint_acceptance
from booley.flows.endpoint_admission import authorize_simulation_targets
from booley.flows.sim.flow import PreparedSimulationEndpoint, SimulateFlow, SimulationMode
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle


def _handle(selector: str, identity: str, root: Path) -> TargetHandle:
    return cast(
        TargetHandle,
        SimpleNamespace(
            selector=selector,
            name=selector,
            identity=identity,
            project_root=root,
        ),
    )


class _StrictEndpoint:
    def __init__(self, authorized: set[str]) -> None:
        self.state = SimpleNamespace(strict_criteria=True)
        self.args = SimpleNamespace(diagnostic=False)
        self.authorized = authorized
        self.observed: list[TargetHandle] = []

    def _bound_criterion_keys_for_target(self, target: TargetHandle) -> list[str]:
        self.observed.append(target)
        return ["criterion"] if target.identity in self.authorized else []


class _BoundEndpoint:
    name = "sim"
    satisfies: ClassVar[list[str]] = ["cycle_count"]

    def __init__(self, binding: AcceptanceTargetBinding) -> None:
        self.state = SimpleNamespace(
            strict_criteria=True,
            criteria={},
            flow_key_aliases={},
        )
        self.args = SimpleNamespace(diagnostic=False, test=None)
        self.flow_acceptance = ResolvedFlowAcceptance(
            bindings=(binding,),
            ticket_backed=True,
        )

    def _bound_criterion_keys_for_target(self, target: TargetHandle) -> list[str]:
        return endpoint_acceptance._bound_criterion_keys_for_target(self, target)  # type: ignore[arg-type]


def test_strict_authorization_preserves_candidate_then_historical_baseline() -> None:
    candidate = _handle("sim", "acme:lib:dut:2#sim", Path("/candidate"))
    baseline = _handle("sim_old", "acme:lib:dut:1#sim_old", Path("/baseline"))
    endpoint = _StrictEndpoint({candidate.identity, baseline.identity})

    rejection = authorize_simulation_targets(endpoint, (candidate, baseline))  # type: ignore[arg-type]

    assert rejection is None
    assert endpoint.observed == [candidate, baseline]


def test_selection_rejection_precedes_target_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _handle("sim", "acme:lib:dut:1#sim", tmp_path)
    flow = SimulateFlow()
    flow._state = DevelopmentState()
    flow._args = SimpleNamespace(mode=SimulationMode.SIMULATE)
    monkeypatch.setattr(flow.context, "_criterion_binding_gate", lambda: None)
    monkeypatch.setattr(
        flow,
        "_prepare_campaign_targets",
        lambda: ((handle,), None, ("sim",), {"sim": ["smoke"]}),
    )
    rejected = EndpointOutcome(exit_code=2, report_text="unknown exact test")
    monkeypatch.setattr(flow, "_validate_prepared_selection", lambda *_args: rejected)
    authorized: list[tuple[TargetHandle, ...]] = []
    monkeypatch.setattr(
        "booley.flows.endpoint_admission.authorize_simulation_targets",
        lambda _context, targets: authorized.append(targets),
    )

    assert flow.prepare_simulation_endpoint() is rejected
    assert authorized == []


def test_strict_authorization_rejects_unbound_distinct_baseline() -> None:
    candidate = _handle("sim", "acme:lib:dut:2#sim", Path("/candidate"))
    baseline = _handle("sim_old", "acme:lib:dut:1#sim_old", Path("/baseline"))
    endpoint = _StrictEndpoint({candidate.identity})

    rejection = authorize_simulation_targets(endpoint, (candidate, baseline))  # type: ignore[arg-type]

    assert rejection is not None
    assert rejection.exit_code == 2
    assert "sim_old" in rejection.report_text
    assert endpoint.observed == [candidate, baseline]


def test_ticket_binding_authorizes_exact_historical_baseline_identity() -> None:
    binding = AcceptanceTargetBinding(
        flow="sim",
        criterion="criteria.mandatory.cycle_count",
        baseline="acme:lib:dut:1#sim_old",
        candidate="acme:lib:dut:2#sim",
        baseline_selector="sim_old",
        candidate_selector="sim",
    )
    endpoint = _BoundEndpoint(binding)
    candidate = _handle("sim", binding.candidate, Path("/candidate"))
    baseline = _handle("sim_old", binding.baseline, Path("/historical-baseline"))

    assert authorize_simulation_targets(endpoint, (candidate, baseline)) is None  # type: ignore[arg-type]


def test_ticket_binding_rejects_baseline_identity_drift() -> None:
    binding = AcceptanceTargetBinding(
        flow="sim",
        criterion="criteria.mandatory.cycle_count",
        baseline="acme:lib:dut:1#sim_old",
        candidate="acme:lib:dut:2#sim",
        baseline_selector="sim_old",
        candidate_selector="sim",
    )
    endpoint = _BoundEndpoint(binding)
    candidate = _handle("sim", binding.candidate, Path("/candidate"))
    drifted = _handle("sim_old", "acme:lib:other:1#sim_old", Path("/baseline"))

    rejection = authorize_simulation_targets(endpoint, (candidate, drifted))  # type: ignore[arg-type]

    assert rejection is not None
    assert "sim_old" in rejection.report_text


def test_campaign_preflight_orders_candidate_before_historical_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate_root = tmp_path / "candidate"
    baseline_root = tmp_path / "historical"
    candidate = _handle("sim", "acme:lib:dut:2#sim", candidate_root)
    baseline = _handle("sim_old", "acme:lib:dut:1#sim_old", baseline_root)
    flow = SimulateFlow()
    flow._state = DevelopmentState()
    flow._args = SimpleNamespace(
        resume_from=None,
        work_dir=candidate_root,
        diagnostic=False,
        mode=SimulationMode.SIMULATE,
    )
    monkeypatch.setattr(flow.context, "_criterion_binding_gate", lambda: None)
    monkeypatch.setattr(flow, "_resolve_requested_targets", lambda: ["sim"])
    monkeypatch.setattr(flow, "_validate_interactive_args", lambda _targets: None)
    monkeypatch.setattr(flow, "_validate_prepared_selection", lambda *_args: None)
    monkeypatch.setattr(flow, "_target_handle", lambda _selector: candidate)
    monkeypatch.setattr(
        flow,
        "_cycle_baseline_selection",
        lambda _targets: ("a" * 40, ["sim_old"], None),
    )

    @contextmanager
    def historical_worktree(*_args, **_kwargs):
        yield baseline_root

    class Catalog:
        def select(self, selector: str, *, for_flow: str) -> TargetHandle:
            assert (selector, for_flow) == ("sim_old", "sim")
            return baseline

    monkeypatch.setattr("booley.flows.sim.flow.baseline_worktree", historical_worktree)
    monkeypatch.setattr(TargetCatalog, "build", lambda root: Catalog())

    prepared = flow.prepare_simulation_endpoint()

    assert isinstance(prepared, PreparedSimulationEndpoint)
    assert prepared.targets == (candidate, baseline)
    assert prepared.targets[1].project_root == baseline_root
