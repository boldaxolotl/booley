"""Real-simulator proof that coverage collects several Test Runs of one Test (ADR 0072).

Two Test Variants of one Test and a second seed each run in their own
Verilator process, launched with exactly the arguments Booley's plan renders,
and write a native database whose file name holds ``+`` and ``@``. The merged
coverage must be the union: a branch only one Test Variant reaches is covered
by that Variant's runs alone.

This runs where Verilator exists (the Booley Sandbox Image); elsewhere it
skips with the missing tool named. The always-on tests of the same path, with
the simulator substituted, live in ``tests/flows/sim/test_coverage_test_runs.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from tests.smoke.test_verilator_coverage_collector_smoke import _invoke, _write_core

from booley.flows.sim.execution.contract import SimulationOptions
from booley.flows.sim.verilator_coverage_execution import collect_target_coverage
from booley.flows.target_test_plan_loading import load_target_test_plan
from booley.flows.test_runs import MAX_SEED
from booley.targets.catalog import TargetCatalog

_MISSING = [tool for tool in ("verilator", "verilator_coverage") if shutil.which(tool) is None]
pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=f"needs {', '.join(_MISSING)} on PATH (the pinned Booley Sandbox Image)",
)

_TESTS_TOML = (
    '[sim]\ntests = ["smoke"]\n\n'
    '[sim.variants.smoke]\ndefault = "plain"\nplain = []\ndeep = ["+deep"]\n'
)


def _write_target(root: Path) -> None:
    """A counter whose ``deep`` branch only the ``deep`` Test Variant reaches."""
    (root / "rtl").mkdir()
    (root / "tb").mkdir()
    (root / "rtl" / "counter.sv").write_text(
        "module counter(input logic clk, input logic deep, output logic [3:0] value);\n"
        "  always_ff @(posedge clk) begin\n"
        "    if (deep)\n"
        "      value <= value + 4'd2;\n"
        "    else\n"
        "      value <= value + 4'd1;\n"
        "  end\nendmodule\n",
        encoding="utf-8",
    )
    (root / "tb" / "counter_tb.sv").write_text(
        "module counter_tb;\n  logic clk = 0; logic deep; logic [3:0] value;\n"
        "  counter dut(.clk(clk), .deep(deep), .value(value));\n  always #1 clk = ~clk;\n"
        '  initial begin deep = $test$plusargs("deep") != 0; repeat (4) @(posedge clk);\n'
        '    $display("[SIM_RESULT] PASSED"); $finish; end\nendmodule\n',
        encoding="utf-8",
    )
    _write_core(root, "tb/counter_tb.sv", "counter_tb", "[--timing, --main, --exe]", "")
    data = root / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text(_TESTS_TOML, encoding="utf-8")


def test_real_coverage_merges_two_variants_and_two_seeds_of_one_test(tmp_path: Path) -> None:
    _write_target(tmp_path)
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    plan = load_target_test_plan(tmp_path, "sim", identity="sim", adapter="verilator")
    runs = (plan.run("smoke"), plan.run("smoke", "deep"), plan.run("smoke", "deep", MAX_SEED))

    result = collect_target_coverage(
        handle,
        selected_tests=("smoke",),
        artifact_root=tmp_path / "campaign",
        invoke=_invoke,
        options=SimulationOptions(timeout_ms=30_000),
        test_runs=runs,
    )

    assert result.status == "complete", [
        (finding.code, finding.message) for finding in result.findings
    ]
    names = ["smoke+plain@1", "smoke+deep@1", f"smoke+deep@{MAX_SEED}"]
    assert [(run.test, run.test_run, run.simulation_verdict) for run in result.runs] == [
        ("smoke", name, "pass") for name in names
    ]
    ids = [f"run:{index:03d}:{name}" for index, name in enumerate(names, start=1)]
    assert [run.id for run in result.runs] == ids
    for index, name in enumerate(names, start=1):
        assert (tmp_path / "campaign/native/raw" / f"{index:03d}-{name}.dat").is_file()
    assert result.merge.status == "equivalent"
    hit_by = {frozenset(point.hits_by_run) for point in result.points if point.hits_by_run}
    # The union keeps what only one Test Variant reached, per run.
    assert frozenset(ids[1:]) in hit_by, "no point is covered by the deep runs alone"
    assert frozenset(ids[:1]) in hit_by, "no point is covered by the plain run alone"
    assert frozenset(ids) in hit_by, "no point is covered by every run"
