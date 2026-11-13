"""An ordinary Simulation Flow run plans its Targets once per invocation.

Planning previews each Target and decides which Cycle Count baseline Test Runs
the baseline revision can run. A real run and a dry run reuse that result, and a
planning error leaves no campaign state behind.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from booley.criteria.templates import CriteriaTemplate
from booley.flows import base
from booley.flows.endpoint_state import EndpointState
from booley.flows.sim.execution import SimulationExecution
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.fusesoc import fusesoc_registry
from tests.flows.sim.test_endpoint_campaign_lifecycle import _ordinary_public_hook_fixture

_REVISION = "b" * 40
_EXIT_ERROR = 2


def _cycle_compiler(bin_dir: Path) -> None:
    """Replace the fake compiler with one that prints its checkout's cycle count."""
    compiler = bin_dir / "iverilog"
    compiler.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, re, sys\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "source = next(pathlib.Path.cwd().rglob('tb.sv'))\n"
        "cycles = re.search(r'CYCLES (\\d+)', source.read_text()).group(1)\n"
        "output.write_text('[SIM_RESULT] PASSED\\n[SIM_CYCLES] a ' + cycles + '\\n')\n",
        encoding="utf-8",
    )


def cycle_baseline_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    baseline_core_version: int | None = None,
) -> tuple[Path, Path, SimulateFlow]:
    """A candidate project, a copy of it as the baseline, and a relative Cycle Count criterion.

    The candidate runs 120 cycles and the baseline 100, within the 50% the
    criterion allows. *baseline_core_version* renames the baseline's core, so
    its Target resolves to another identity than the candidate's.
    """
    candidate, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    (candidate / ".booley/tests.toml").write_text('[sim_a]\ntests = ["a"]\n')
    _cycle_compiler(tmp_path / "bin")
    baseline = tmp_path / "baseline"
    shutil.copytree(candidate, baseline)
    (candidate / "tb.sv").write_text("module tb; // CYCLES 120\nendmodule\n")
    (baseline / "tb.sv").write_text("module tb; // CYCLES 100\nendmodule\n")
    if baseline_core_version is not None:
        core = baseline / "multi.core"
        core.write_text(
            core.read_text().replace(
                "acme:lib:multi:1", f"acme:lib:multi:{baseline_core_version}"
            ),
            encoding="utf-8",
        )

    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "cycle_count": [
                    {"target": "sim_a", "test": "a", "cycle_count_increase_at_most": "50%"}
                ]
            }
        }
    )
    read_state = EndpointState.read_state

    def with_relative_criterion(context: Any) -> EndpointState:
        state = read_state(context)
        state.init_criteria(template.expand([]), criterion_params=template.expand_params([]))
        for name in state.criteria:
            state.criteria[name].params["_baseline_ref"] = _REVISION
        return state

    monkeypatch.setattr(EndpointState, "read_state", with_relative_criterion)
    return candidate, baseline, flow


@contextmanager
def baseline_checkout(baseline: Path) -> Iterator[Callable[[], int]]:
    """Serve *baseline* as the checkout of the pinned revision; yield its open count."""
    opened: list[str] = []

    @contextmanager
    def worktree(_project_root: Path, revision: str, *, paired_project: object) -> Iterator[Path]:
        del paired_project
        opened.append(revision)
        yield baseline

    with (
        patch("booley.flows.sim.flow.git_full_sha", return_value=_REVISION),
        patch("booley.flows.sim.flow.baseline_worktree", worktree),
    ):
        yield lambda: len(opened)


def _spy_previews(monkeypatch: pytest.MonkeyPatch) -> Counter[tuple[str, Path]]:
    """Count `SimulationExecution.preview` calls by (Target selector, checkout)."""
    counts: Counter[tuple[str, Path]] = Counter()
    preview = SimulationExecution.preview

    def counted(self: SimulationExecution, handle: Any, selection: Any) -> Any:
        counts[(handle.selector, Path(handle.project_root).resolve())] += 1
        return preview(self, handle, selection)

    monkeypatch.setattr(SimulationExecution, "preview", counted)
    return counts


def _spy_decisions(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record each Cycle Count baseline decision."""
    from booley.flows.sim import flow as flow_module

    decisions: list[object] = []
    decide = flow_module.decide_baseline_runs

    def decided(*args: Any, **kwargs: Any) -> Any:
        decisions.append(args)
        return decide(*args, **kwargs)

    monkeypatch.setattr(flow_module, "decide_baseline_runs", decided)
    return decisions


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
def test_ordinary_run_previews_and_decides_baselines_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    candidate, baseline, flow = cycle_baseline_project(tmp_path, monkeypatch)
    previews = _spy_previews(monkeypatch)
    decisions = _spy_decisions(monkeypatch)

    with baseline_checkout(baseline):
        result = flow.execute(
            SimRequest(
                target="sim_a",
                work_dir=candidate,
                report_dir=tmp_path / "reports",
                dry_run=dry_run,
            )
        )

    assert result.exit_code == 0, result.outcome.report_text
    assert previews == {
        ("sim_a", candidate.resolve()): 1,
        ("sim_a", baseline.resolve()): 1,
    }
    assert len(decisions) == 1
    if dry_run:
        _assert_dry_run_output(result.outcome)


def _assert_dry_run_output(outcome: Any) -> None:
    """The dry run lists the baseline campaign, then the candidate's, unchanged."""
    assert outcome.report_text == "sim_a: 1 pending work item(s)\nsim_a: 1 pending work item(s)"
    assert outcome.detail["mutated"] is False
    entries = outcome.detail["record_preview"]
    for entry in entries:
        del entry["target"]["project_identity"]  # a digest of the temporary project path
    revision = _REVISION
    target = {"display_name": "sim_a", "name": "sim_a", "selector": "sim_a"}
    target |= {"revision": revision, "vlnv": "acme:lib:multi:1"}
    assert entries == [
        {
            "target": {**target, "role": "cycle_count_baseline"},
            "work_items": 1,
            "build_variants": 1,
            "prerequisites": 0,
        },
        {
            "target": {**target, "role": "candidate"},
            "work_items": 1,
            "build_variants": 1,
            "prerequisites": 1,
        },
    ]


def _assert_refused_before_campaign_state(
    reserved: list[object],
    started: list[object],
    reports: Path,
    result: Any,
    expected_text: str,
) -> None:
    """A planning refusal: exit 2 and no reservation, build, progress or campaign manifest."""
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


def _record_campaign_state(monkeypatch: pytest.MonkeyPatch) -> tuple[list[object], list[object]]:
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


def test_baseline_identity_mismatch_refuses_before_any_campaign_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, baseline, flow = cycle_baseline_project(
        tmp_path, monkeypatch, baseline_core_version=2
    )
    reserved, started = _record_campaign_state(monkeypatch)

    with baseline_checkout(baseline):
        result = flow.execute(
            SimRequest(target="sim_a", work_dir=candidate, report_dir=tmp_path / "reports")
        )

    _assert_refused_before_campaign_state(
        reserved,
        started,
        tmp_path / "reports",
        result,
        "sim planning failed: sim_a: baseline resolves to 'acme:lib:multi:2#sim_a', "
        "expected 'acme:lib:multi:1#sim_a'",
    )


def test_unresolvable_target_refuses_before_any_campaign_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    reserved, started = _record_campaign_state(monkeypatch)

    def unresolvable(self: SimulationExecution, handle: Any, selection: Any) -> Any:
        raise fusesoc_registry.FuseSocError("core is not resolvable")

    monkeypatch.setattr(SimulationExecution, "preview", unresolvable)

    result = flow.execute(
        SimRequest(target="sim_a", work_dir=candidate, report_dir=tmp_path / "reports")
    )

    _assert_refused_before_campaign_state(
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
    reserved, started = _record_campaign_state(monkeypatch)

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
