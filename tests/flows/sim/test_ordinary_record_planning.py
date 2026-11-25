"""An ordinary Simulation Flow run plans its Targets once per invocation.

Planning previews each Target. A planning error leaves no record state behind.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from booley.flows.sim.execution import SimulationExecution
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.fusesoc import fusesoc_registry
from tests.flows.sim.test_endpoint_record_lifecycle import _ordinary_public_hook_fixture

_EXIT_ERROR = 2


def _assert_refused_before_simulation_record_state(
    reserved: list[object], started: list[object], reports: Path, result: Any, expected_text: str
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
    """Record invocation reservations and every non-git process the run launches."""
    reserved: list[object] = []
    started: list[object] = []
    reserve = SimulateFlow.reserve_invocation_dir

    def reserving(self: SimulateFlow) -> Path | None:
        reserved.append(self)
        return reserve(self)

    class RecordingPopen(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
            # Planning legitimately asks git for the checkout revision; nothing else may launch.
            command = [args] if isinstance(args, str) else list(args)
            if Path(str(command[0])).name != "git":
                started.append(args)
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(SimulateFlow, "reserve_invocation_dir", reserving)
    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    return reserved, started


@pytest.mark.parametrize("refusal", ["unresolvable-target", "unknown-test"])
def test_planning_refusal_leaves_no_simulation_record_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    candidate, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    reserved, started = _record_simulation_record_state(monkeypatch)
    reports = tmp_path / "reports"

    def unresolvable(self: SimulationExecution, handle: Any, selection: Any) -> Any:
        raise fusesoc_registry.FuseSocError("core is not resolvable")

    if refusal == "unresolvable-target":
        monkeypatch.setattr(SimulationExecution, "preview", unresolvable)
    tests = ("nope",) if refusal == "unknown-test" else None

    result = flow.execute(
        SimRequest(target="sim_a", test=tests, work_dir=candidate, report_dir=reports)
    )

    if refusal == "unresolvable-target":
        _assert_refused_before_simulation_record_state(
            reserved,
            started,
            reports,
            result,
            "sim planning failed: sim_a: core is not resolvable",
        )
        return
    assert result.exit_code == _EXIT_ERROR
    assert result.outcome.report_text.startswith(
        "sim: unknown exact test(s) for target 'sim_a': nope. Declared tests: a, b."
    )
    assert reserved == []
    assert started == []
    assert not (reports / "sim").exists()
