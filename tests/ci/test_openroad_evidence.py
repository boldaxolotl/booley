"""OpenROAD diagnostics survive early failure without changing its status."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell runtime probe")
@pytest.mark.parametrize("blocked_export", [False, True])
def test_openroad_failure_exports_partial_logs_and_removes_scratch(
    tmp_path: Path, blocked_export: bool
) -> None:
    result, evidence = _run_staged_probe(tmp_path, "gui", blocked_export)
    assert result.returncode == 17, result.stdout + result.stderr
    if blocked_export:
        assert "Could not retain all OpenROAD runtime evidence" in result.stderr
    else:
        assert (evidence / "openroad-gui.log").read_text() == "probe failed with diagnostics\n"


_FAKE_PYTHON = r"""#!/usr/bin/env bash
work="$2"
mkdir -p "$work/abc-control" "$work/collision-preserve" "$work/collision-attribute"
mkdir -p "$work/repair-off/reports" "$work/repair-on/reports"
for path in synth.ys synth-undriven.ys abc-control/synth.ys collision-preserve/synth.ys collision-attribute/synth.ys run_openroad-repair-off.tcl run_openroad-repair-on.tcl repair-off/run_openroad.tcl repair-on/run_openroad.tcl; do
  echo generated > "$work/$path"
done
"""
_FAKE_YOSYS = r"""#!/usr/bin/env bash
work="$(dirname "$3")"
case "$work" in
  */abc-control)
    echo 'ABC: Warning: Detected 2 multi-output cells (for example, "FA_X1").' > "$work/log_abc_dut.txt"
    ;;
  */collision-preserve)
    echo 'Found and reported 0 problems.' > "$work/check_collision_preserve.txt"
    printf 'module BUF_X1\nassign Z = A;\n' > "$work/synth_collision_preserve.v"
    ;;
  */collision-attribute)
    echo 'Assertion failed'
    exit 1
    ;;
  *)
    echo healthy > "$work/log_abc_dut.txt"
    if [[ "$PROBE_FAILURE" == synthesis ]]; then
      echo 'primary synthesis diagnostic' > "$work/check_dut.txt"
    elif [[ "$3" == *synth-undriven.ys ]]; then
      echo 'Warning: Wire dut.\intentional_undriven is used but has no driver.' > "$work/check_dut.txt"
    else
      echo 'Found and reported 0 problems.' > "$work/check_dut.txt"
    fi
    ;;
esac
"""
_FAKE_OPENROAD = r"""#!/usr/bin/env bash
if [[ "$1" == -gui ]]; then
  if [[ "$PROBE_FAILURE" == gui ]]; then
    echo 'probe failed with diagnostics'
    exit 17
  fi
  echo 'GUI runtime loaded'
  exit 0
fi
echo 'placement diagnostic' > reports/placement.txt
echo 'placed netlist' > openroad_dut.v
if [[ "$PROBE_FAILURE" == placement ]]; then
  echo 'placement failed'
  exit 19
fi
printf 'BOOLEY_STAGE: global_placement\nBOOLEY_STAGE: detailed_placement\nBOOLEY_STAGE: repair_timing\nDesign area 1 um^2\n[INFO RSZ-0026] Removed 1 buffers.\n'
if [[ "$PROBE_FAILURE" == missing-export ]]; then
  rm -f ../check_dut_driven.txt ../check_dut_undriven.txt
fi
"""


def _run_staged_probe(
    tmp_path: Path, failure: str, blocked_export: bool
) -> tuple[subprocess.CompletedProcess[str], Path]:
    pdk = tmp_path / "pdk"
    for filename in (
        "cell/lib/NangateOpenCellLibrary_typical_ccs.lib",
        "nangate45/Nangate45_tech.lef",
        "nangate45/Nangate45_stdcell.lef",
        "nangate45/Nangate45.rc",
    ):
        path = pdk / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, script in (
        ("python", _FAKE_PYTHON),
        ("yosys", _FAKE_YOSYS),
        ("openroad", _FAKE_OPENROAD),
    ):
        command = binaries / name
        command.write_text(script)
        command.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    evidence = tmp_path / "evidence"
    if blocked_export:
        evidence.touch()
    result = subprocess.run(
        [
            "bash",
            str(_ROOT / ".github/scripts/verify_openroad_runtime.sh"),
            str(pdk),
            str(evidence),
        ],
        env={
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "TMPDIR": str(scratch),
            "PROBE_FAILURE": failure,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert not list(scratch.iterdir())
    return result, evidence


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell runtime probe")
@pytest.mark.parametrize("failure,expected", [("synthesis", 1), ("placement", 19)])
def test_openroad_later_failures_export_primary_diagnostics(
    tmp_path: Path, failure: str, expected: int
) -> None:
    result, evidence = _run_staged_probe(tmp_path, failure, False)
    assert result.returncode == expected, result.stdout + result.stderr
    if failure == "synthesis":
        assert (evidence / "check_dut.txt").read_text() == "primary synthesis diagnostic\n"
    else:
        assert (
            evidence / "repair-off/reports/placement.txt"
        ).read_text() == "placement diagnostic\n"
        assert (evidence / "repair-off/openroad_dut.v").read_text() == "placed netlist\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell runtime probe")
@pytest.mark.parametrize("blocked_export", [False, True])
def test_successful_openroad_probe_requires_successful_export(
    tmp_path: Path, blocked_export: bool
) -> None:
    result, evidence = _run_staged_probe(tmp_path, "none", blocked_export)
    assert result.returncode == int(blocked_export), result.stdout + result.stderr
    if not blocked_export:
        assert (evidence / "check_dut_driven.txt").is_file()
        assert (evidence / "check_dut_undriven.txt").is_file()
        assert (evidence / "repair-on/reports/placement.txt").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell runtime probe")
def test_successful_openroad_probe_requires_all_export_groups(tmp_path: Path) -> None:
    result, _ = _run_staged_probe(tmp_path, "missing-export", False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Could not retain all OpenROAD runtime evidence" in result.stderr
