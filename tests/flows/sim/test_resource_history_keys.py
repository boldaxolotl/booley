"""Resource History keys name each Test Variant; records carry the source size (#3235).

Runs the real ``SimulateFlow`` campaign path against the fake-Verilator Project
of the build-reuse tests and reads the history file the Flow wrote.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.runtime import resource_history
from tests.flows.sim.test_verilator_build_reuse_campaign import (  # noqa: F401 -- fixture
    flow_project,
)

# The imported fake-Verilator Project is POSIX-only, like its home module.
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Verilator build reuse is proven in the POSIX Sandbox"
)

_TESTS_TOML = """\
[sim]
tests = ["first"]
select = "+test={name}"
seed = "+seed={seed}"

[sim.variants.first]
default = "fast"
fast = []
slow = ["+iters=9"]
"""


def _run(project: Path, reports: Path, selector: str) -> None:
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=project,
            report_dir=reports,
            test=(selector,),
            timeout_ms=30_000,
        )
    )
    assert result.exit_code == 0, result.outcome.report_text


def _source_bytes(project: Path) -> int:
    names = ("rtl/tb.sv", "inc/defs.svh", "dpi/helper.cpp")
    return sum((project / name).stat().st_size for name in names)


def test_sim_history_keys_name_variants_not_seeds(
    flow_project: Path,  # noqa: F811 -- shared fixture keeps its required name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booley_dir = flow_project / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    (booley_dir / "tests.toml").write_text(_TESTS_TOML, encoding="utf-8")
    reports = tmp_path / "reports"
    initial_bytes = _source_bytes(flow_project)

    _run(flow_project, reports, "first+slow@1")
    _run(flow_project, reports, "first+slow@2")
    _run(flow_project, reports, "first+fast")
    appended = b"// grown\n"
    with (flow_project / "rtl" / "tb.sv").open("ab") as handle:
        handle.write(appended)
    _run(flow_project, reports, "first+fast")

    records = resource_history.load_records(booley_dir)
    runs = [r for r in records if r["key"]["phase"] == "sim_run"]
    # Seeds share a key; the default Variant keeps the bare Test name (the old key).
    assert Counter(r["key"]["test_run"] for r in runs) == {"first+slow": 2, "first": 2}
    builds = [r for r in records if r["key"]["phase"] == "sim_build"]
    assert builds, "the changed source forces at least one measured build"
    assert {r["source_bytes"] for r in runs[:3] + builds[:1]} == {initial_bytes}
    assert runs[-1]["source_bytes"] == initial_bytes + len(appended)


def test_sim_request_maps_to_recorded_keys(
    flow_project: Path,  # noqa: F811 -- shared fixture keeps its required name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The estimator lists the keys a real ``SimulateFlow`` run writes, in run order."""
    from booley.flows.job_estimates import phase_keys, sim_job_request
    from booley.flows.target_test_plan_loading import load_checkout_target_test_plan

    booley_dir = flow_project / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    (booley_dir / "tests.toml").write_text(_TESTS_TOML, encoding="utf-8")
    selectors = ("first+slow@1", "first+fast")

    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=flow_project,
            report_dir=tmp_path / "reports",
            test=selectors,
            timeout_ms=30_000,
        )
    )
    assert result.exit_code == 0, result.outcome.report_text

    recorded: list[resource_history.HistoryKey] = []
    for record in resource_history.load_records(booley_dir):
        key = resource_history.history_key(record)
        if key not in recorded:
            recorded.append(key)
    # Nothing writes these, which is why the request never lists them.
    assert {key.phase for key in recorded} == {"sim_build", "sim_run"}
    plan = load_checkout_target_test_plan(flow_project, "sim")
    request = sim_job_request(plan, plan.resolve(selectors))
    assert list(phase_keys(request)) == recorded
    assert [key.test_run for key in recorded] == ["", "first+slow", "first"]
