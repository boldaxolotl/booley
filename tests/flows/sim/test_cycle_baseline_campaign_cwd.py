"""A Simulation Campaign baseline builds and runs in the baseline checkout.

The prepared (campaign) ``sim`` path plans a Cycle Count baseline from a
checkout of the baseline revision. Its build and launch commands use paths
relative to that checkout, so they must also run there, not in the candidate
checkout the invocation was started from.
"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from booley.criteria.templates import CriteriaTemplate
from booley.flows import base
from tests.flows.sim.test_endpoint_campaign_lifecycle import _ordinary_public_hook_fixture

_REVISION = "b" * 40


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


def test_campaign_baseline_builds_and_runs_in_the_baseline_checkout(tmp_path, monkeypatch):
    from booley.flows.sim.request import SimRequest

    candidate, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    (candidate / ".booley/tests.toml").write_text('[sim_a]\ntests = ["a"]\n')
    _cycle_compiler(tmp_path / "bin")
    baseline = tmp_path / "baseline"
    shutil.copytree(candidate, baseline)
    (candidate / "tb.sv").write_text("module tb; // CYCLES 120\nendmodule\n")
    (baseline / "tb.sv").write_text("module tb; // CYCLES 100\nendmodule\n")

    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "cycle_count": [
                    {"target": "sim_a", "test": "a", "cycle_count_increase_at_most": "50%"}
                ]
            }
        }
    )
    session = type(flow.context)
    read_state = session.read_state

    def with_relative_criterion(context):
        state = read_state(context)
        state.init_criteria(template.expand([]), criterion_params=template.expand_params([]))
        for name in state.criteria:
            state.criteria[name].params["_baseline_ref"] = _REVISION
        return state

    monkeypatch.setattr(session, "read_state", with_relative_criterion)

    started: list[Path] = []
    start = base._start_local_process

    def record(cmd, cwd, env, scope):
        started.append(Path(cwd).resolve())
        return start(cmd, cwd, env, scope)

    @contextmanager
    def worktree(_project_root, revision, *, paired_project):
        del paired_project
        assert revision == _REVISION
        yield baseline

    monkeypatch.setattr(base, "_start_local_process", record)
    with (
        patch("booley.flows.sim.flow.git_full_sha", return_value=_REVISION),
        patch("booley.flows.sim.flow.baseline_worktree", worktree),
    ):
        result = flow.execute(
            SimRequest(target="sim_a", work_dir=candidate, report_dir=tmp_path / "reports")
        )

    assert result.exit_code == 0, result.outcome.report_text
    # Build, then launch: the baseline's in its checkout, then the candidate's in its own.
    assert started == [baseline.resolve()] * 2 + [candidate.resolve()] * 2
    assert _observed_cycles(tmp_path / "reports/sim/1/targets") == {
        "sim_a": [("a", 120)],
        f"sim_a%40baseline-{_REVISION[:12]}": [("a", 100)],
    }


def _observed_cycles(targets: Path) -> dict[str, list[tuple[str, int]]]:
    """Each campaign's observed (Test, cycles), read from its published results."""
    observed: dict[str, list[tuple[str, int]]] = {}
    for result in sorted(targets.glob("*/campaign/work-items/*/result.json")):
        document = json.loads(result.read_text())
        observed.setdefault(result.parents[3].name, []).extend(
            (item["test"], item["cycle_count"]) for item in document["observations"]
        )
    return observed
