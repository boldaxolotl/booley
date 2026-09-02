from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPORT = Path(__file__).parent / "report.py"


def test_report_prints_deterministic_graph_diagnostics(tmp_path: Path) -> None:
    package = tmp_path / "booley"
    for name in ("alpha", "beta"):
        owner = package / name
        owner.mkdir(parents=True)
        (owner / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "alpha" / "edge.py").write_text("import booley.beta\n", encoding="utf-8")
    harness = package / "harness"
    harness.mkdir()
    (harness / "__init__.py").write_text("", encoding="utf-8")
    (harness / "doctor.py").write_text("import booley.beta\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_REPORT), "--source-root", str(package), "--top", "1"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "Parsed Python modules: 6\n"
        "Normalized dependency facts: 2\n"
        "Unique normalized edges: 2\n"
        "\nCyclic top-level package groups:\n"
        "\nMutual top-level package pairs:\n"
        "\nTop 1 file fan-out:\n"
        "- booley.alpha.edge: 1\n"
        "\nNamed composition hotspot fan-out (diagnostic only):\n"
        "- booley.harness.doctor: 1\n"
        "- booley.harness.booley: 0\n"
        "- booley.harness.init_cmd: 0\n"
        "- booley.harness.developer: 0\n"
        "- booley.flows.sim.flow: 0\n"
        "- booley.flows.synth.flow: 0\n"
        "- booley.mcp.server: 0\n"
        "- booley.flows.fpga.flow: 0\n"
        "- booley.specialists.mutation_tester: 0\n"
        "- booley.specialists.coverage_analyst: 0\n"
    )


def test_report_rejects_non_positive_top_count(tmp_path: Path) -> None:
    package = tmp_path / "booley"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_REPORT), "--source-root", str(package), "--top", "0"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 2
    assert "must be a positive integer" in result.stderr
