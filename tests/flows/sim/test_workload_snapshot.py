from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from booley.dev_support.cycle_count import PROVENANCE_LIMITATION, workload_changes
from booley.flows.sim.flow import SimulateFlow, TargetResult
from booley.flows.sim.flow import TestResult as SimTestResult
from booley.flows.sim.workload import build_workload_snapshot
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget


def _resolved(root: Path) -> ResolvedTarget:
    return ResolvedTarget(
        name="sim_core",
        vlnv="::core:0",
        toplevel="tb_core",
        eda_tool="verilator",
        files=(
            ResolvedFile("rtl.sv", "systemVerilogSource"),
            ResolvedFile("tb.sv", "systemVerilogSource", tags=("tb",)),
            ResolvedFile("vectors.hex", "user"),
        ),
        parameters={"WIDTH": 32},
        build_root=root,
        edam_path=root / "core.eda.yml",
        flow_options={"tool": "verilator"},
    )


def _write_inputs(root: Path, *, vector: str = "00\n") -> None:
    root.mkdir(parents=True)
    (root / "rtl.sv").write_text("module core; endmodule\n", encoding="utf-8")
    (root / "tb.sv").write_text("module tb_core; endmodule\n", encoding="utf-8")
    (root / "vectors.hex").write_text(vector, encoding="utf-8")


def test_workload_snapshot_preserves_input_roles_and_is_path_stable(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_inputs(left)
    _write_inputs(right)

    first = build_workload_snapshot(left, "sim_core", "coremark", _resolved(left))
    second = build_workload_snapshot(right, "sim_core", "coremark", _resolved(right))

    assert first["fingerprint"] == second["fingerprint"]
    assert [row["role"] for row in first["inputs"]] == ["rtl", "tb", "workload"]
    assert first["provenance_limitation"] == PROVENANCE_LIMITATION


def test_configured_run_cwd_is_normalized_for_workload_snapshot(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_inputs(project_root)
    config_dir = project_root / ".booley_project"
    config_dir.mkdir()
    (config_dir / "booley.toml").write_text(
        '[flows.sim]\nrun_cwd = "tests/work"\n', encoding="utf-8"
    )
    (project_root / "tests" / "work").mkdir(parents=True)

    flow = object.__new__(SimulateFlow)
    flow._args = SimpleNamespace(work_dir=project_root)
    flow._resolved_targets = {"sim_core": _resolved(project_root)}
    flow._target_sim_env = MagicMock(return_value={})
    result = TargetResult(
        target="sim_core",
        passed=True,
        tests=[SimTestResult(name="coremark", passed=True)],
    )

    flow._attach_workload_snapshots(result)

    assert result.passed is True
    assert result.tests[0].workload_snapshot is not None
    assert result.tests[0].workload_snapshot["controls"]["run_cwd"] == "tests/work"


def test_workload_changes_identify_changed_declared_path(tmp_path: Path) -> None:
    baseline_root = tmp_path / "baseline"
    current_root = tmp_path / "current"
    _write_inputs(baseline_root, vector="00\n")
    _write_inputs(current_root, vector="01\n")

    baseline = build_workload_snapshot(
        baseline_root, "sim_core", "coremark", _resolved(baseline_root)
    )
    current = build_workload_snapshot(
        current_root, "sim_core", "coremark", _resolved(current_root)
    )

    assert workload_changes(baseline, current) == [
        {
            "path": "vectors.hex",
            "role": "workload",
            "status": "modified",
            "baseline_sha256": baseline["inputs"][2]["sha256"],
            "current_sha256": current["inputs"][2]["sha256"],
        }
    ]
