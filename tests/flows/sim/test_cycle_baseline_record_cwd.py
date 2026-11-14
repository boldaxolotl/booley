"""A Simulation Campaign baseline builds and runs in the baseline checkout.

The prepared (campaign) ``sim`` path plans a Cycle Count baseline from a
checkout of the baseline revision. Its build and launch commands use paths
relative to that checkout, so they must also run there, not in the candidate
checkout the invocation was started from.
"""

from __future__ import annotations

import json
from pathlib import Path

from booley.flows import base
from tests.flows.sim.test_ordinary_record_planning import (
    _REVISION,
    baseline_checkout,
    cycle_baseline_project,
)


def test_campaign_baseline_builds_and_runs_in_the_baseline_checkout(tmp_path, monkeypatch):
    from booley.flows.sim.request import SimRequest

    candidate, baseline, flow = cycle_baseline_project(tmp_path, monkeypatch)

    started: list[Path] = []
    start = base._start_local_process

    def record(cmd, cwd, env, scope):
        started.append(Path(cwd).resolve())
        return start(cmd, cwd, env, scope)

    monkeypatch.setattr(base, "_start_local_process", record)
    with baseline_checkout(baseline):
        result = flow.execute(
            SimRequest(target="sim_a", work_dir=candidate, report_dir=tmp_path / "reports")
        )

    assert result.exit_code == 0, result.outcome.report_text
    # FuseSoC setup, build, then launch: the baseline's in its checkout, then the
    # candidate's in its own.
    assert started == [baseline.resolve()] * 3 + [candidate.resolve()] * 3
    assert _observed_cycles(tmp_path / "reports/sim/1/targets") == {
        "sim_a": [("a", 120)],
        f"sim_a%40baseline-{_REVISION[:12]}": [("a", 100)],
    }


def _observed_cycles(targets: Path) -> dict[str, list[tuple[str, int]]]:
    """Each campaign's observed (Test, cycles), read from its published results."""
    observed: dict[str, list[tuple[str, int]]] = {}
    for result in sorted(targets.glob("*/record/work-items/*/result.json")):
        document = json.loads(result.read_text())
        observed.setdefault(result.parents[3].name, []).extend(
            (item["test"], item["cycle_count"]) for item in document["observations"]
        )
    return observed
