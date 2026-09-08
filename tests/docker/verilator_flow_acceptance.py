"""Compare real ordinary/coverage Flow verdicts in the installed candidate."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from functools import partial
from pathlib import Path

from booley.flows.base import SubprocessResult
from booley.flows.sim.execution import NamedTests, SimulationExecution, SimulationOptions
from booley.flows.sim.verilator_coverage_execution import collect_target_coverage
from booley.targets.catalog import TargetCatalog

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "verilator_acceptance"))
from flow_fixture import write_project

_EXPECTED = {
    "pass": "pass",
    "fail": "fail",
    "compile": "elab_error",
    "timeout": "timeout",
    "signal": "fail",
}


def invoke(command: list[str], *, timeout: int, cwd: Path) -> SubprocessResult:
    """Allow compilation outside the adapter-enforced simulation timeout."""
    completed = subprocess.run(
        command,
        cwd=cwd,
        timeout=min(timeout + 120, 300),
        capture_output=True,
        text=True,
        check=False,
    )
    return SubprocessResult(
        returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


def check_case(root: Path, harness: str, outcome: str) -> dict:
    write_project(root, harness, outcome)
    handle = TargetCatalog.build(root).select("sim", for_flow="sim")
    options = SimulationOptions(timeout_ms=2000)
    ordinary = SimulationExecution(invoke=partial(invoke, cwd=root), options=options).run(
        handle, NamedTests(("check",))
    )
    coverage = collect_target_coverage(
        handle,
        selected_tests=("check",),
        artifact_root=root / "campaign",
        invoke=partial(invoke, cwd=root),
        options=options,
    )
    evidence = {
        "harness": harness,
        "outcome": outcome,
        "ordinary": [test.verdict for test in ordinary.tests],
        "diagnostics": list(ordinary.diagnostics),
        "infrastructure": str(ordinary.infrastructure_failure),
        "coverage": [test.simulation_verdict for test in coverage.runs],
        "collection": coverage.status,
        "findings": [{"code": item.code, "message": item.message} for item in coverage.findings],
    }
    (root / "verdicts.json").write_text(json.dumps(evidence, indent=2) + "\n")
    expected = (
        "inconclusive"
        if harness == "cocotb" and outcome in {"signal", "timeout"}
        else _EXPECTED[outcome]
    )
    assert evidence["ordinary"] == [expected], evidence
    assert evidence["coverage"] == evidence["ordinary"], evidence
    if outcome == "pass":
        assert coverage.status == "complete", evidence
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    failures = []
    for harness in ("generated", "custom", "cocotb"):
        for outcome in _EXPECTED:
            try:
                evidence = check_case(
                    args.work_dir.resolve() / f"{harness}-{outcome}", harness, outcome
                )
                print(json.dumps(evidence), flush=True)
            except (AssertionError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{harness}/{outcome}: {exc}")
    assert not failures, "\n".join(failures)
    print("15 real ordinary/coverage verdict pairs passed", flush=True)


if __name__ == "__main__":
    main()
