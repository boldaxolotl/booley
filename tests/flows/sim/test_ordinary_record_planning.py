"""An ordinary Simulation Flow run plans its Targets once per invocation.

Planning previews each Target. A planning error leaves no record state behind.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from booley.flows import base
from booley.flows.sim.execution import SimulationExecution
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.fusesoc import fusesoc_registry
from tests.flows.sim.test_endpoint_record_lifecycle import _ordinary_public_hook_fixture

_EXIT_ERROR = 2


def _assert_refused_before_simulation_record_state(
    reserved: list[object],
    started: list[object],
    reports: Path,
    result: Any,
    expected_text: str,
) -> None:
    """A planning refusal: exit 2 and no reservation, build, progress or record manifest."""
    assert result.exit_code == _EXIT_ERROR
    assert result.outcome.report_text == expected_text
    assert "plan" not in result.outcome.detail
    assert reserved == []
    assert started == []
    # The endpoint's final report is all that is written for the refused run.
    assert sorted(path.name for path in (reports / "sim/1").iterdir()) == ["report.json"]
    assert not list(reports.glob("**/progress.json"))
    assert not list(reports.glob("**/manifest.json"))
    assert json.loads((reports / "sim/1/report.json").read_text())["exit_code"] == _EXIT_ERROR


def _record_simulation_record_state(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[object], list[object]]:
    """Record invocation reservations and launched processes."""
    reserved: list[object] = []
    started: list[object] = []
    reserve = SimulateFlow.reserve_invocation_dir
    start = base._start_local_process

    def reserving(self: SimulateFlow) -> Path | None:
        reserved.append(self)
        return reserve(self)

    def launching(*args: Any, **kwargs: Any) -> Any:
        started.append(args)
        return start(*args, **kwargs)

    monkeypatch.setattr(SimulateFlow, "reserve_invocation_dir", reserving)
    monkeypatch.setattr(base, "_start_local_process", launching)
    return reserved, started


def test_unresolvable_target_refuses_before_any_simulation_record_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    reserved, started = _record_simulation_record_state(monkeypatch)

    def unresolvable(self: SimulationExecution, handle: Any, selection: Any) -> Any:
        raise fusesoc_registry.FuseSocError("core is not resolvable")

    monkeypatch.setattr(SimulationExecution, "preview", unresolvable)

    result = flow.execute(
        SimRequest(target="sim_a", work_dir=candidate, report_dir=tmp_path / "reports")
    )

    _assert_refused_before_simulation_record_state(
        reserved,
        started,
        tmp_path / "reports",
        result,
        "sim planning failed: sim_a: core is not resolvable",
    )


def test_unknown_test_is_refused_before_any_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    reserved, started = _record_simulation_record_state(monkeypatch)

    result = flow.execute(
        SimRequest(
            target="sim_a",
            test=("nope",),
            work_dir=candidate,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == _EXIT_ERROR
    assert result.outcome.report_text.startswith(
        "sim: unknown exact test(s) for target 'sim_a': nope. Declared tests: a, b."
    )
    assert reserved == []
    assert started == []
    assert not (tmp_path / "reports/sim").exists()
