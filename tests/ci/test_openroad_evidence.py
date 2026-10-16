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
if [[ "$1" == */verify_openroad_fanout.py ]]; then
  test "$2" == --pdk-root && test "$4" == --work-dir || exit 23
  mkdir -p "$5/default"
  echo 'native fanout diagnostics' > "$5/default/native.log"
  if [[ "$PROBE_FAILURE" == fanout ]]; then exit 29; fi
  echo 'openroad.fanout: passed actual10k and setting/SDC/library native arms'
  exit 0
fi
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
@pytest.mark.parametrize("failure,expected", [("synthesis", 1), ("placement", 19), ("fanout", 29)])
def test_openroad_later_failures_export_primary_diagnostics(
    tmp_path: Path, failure: str, expected: int
) -> None:
    result, evidence = _run_staged_probe(tmp_path, failure, False)
    assert result.returncode == expected, result.stdout + result.stderr
    if failure == "synthesis":
        assert (evidence / "check_dut.txt").read_text() == "primary synthesis diagnostic\n"
    elif failure == "fanout":
        assert (
            evidence / "fanout/default/native.log"
        ).read_text() == "native fanout diagnostics\n"
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
        assert (evidence / "fanout/default/native.log").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell runtime probe")
def test_successful_openroad_probe_requires_all_export_groups(tmp_path: Path) -> None:
    result, _ = _run_staged_probe(tmp_path, "missing-export", False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Could not retain all OpenROAD runtime evidence" in result.stderr


@pytest.fixture
def fanout_helper():
    import importlib.util

    specification = importlib.util.spec_from_file_location(
        "reviewed_fanout_helper", _ROOT / ".github/scripts/verify_openroad_fanout.py"
    )
    assert specification is not None and specification.loader is not None
    helper = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(helper)
    return helper


def test_slim_fanout_runtime_uses_scalar_native_limit(tmp_path, monkeypatch, fanout_helper):
    import sys
    from types import SimpleNamespace

    pdk = tmp_path / "pdk"
    for name in (
        "cell/lib/NangateOpenCellLibrary_typical_ccs.lib",
        "nangate45/Nangate45_tech.lef",
        "nangate45/Nangate45_stdcell.lef",
        "nangate45/Nangate45.rc",
    ):
        path = pdk / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("library (fixture) {\n}")
    monkeypatch.setattr(
        sys, "argv", ["verify", "--pdk-root", str(pdk), "--work-dir", str(tmp_path / "work")]
    )
    monkeypatch.setattr(fanout_helper.shutil, "which", lambda _: "openroad")

    def native(command, **kwargs):
        path = Path(kwargs["cwd"])
        limits = {
            "default": 1e20,
            "setting50": 50,
            "design100": 100,
            "scoped25": 25,
            "scoped100": 50,
            "library25": 25,
        }
        text = "\n".join(
            f"BOOLEY_STAGE: {stage}"
            for stage in (
                "floorplan",
                "global_placement",
                "repair_design",
                "detailed_placement",
                "sta_report",
            )
        )
        text += f"\nOPENROAD_FANOUT_LIMIT: {limits.get(path.name, 50)}\nREPAIR_ELAPSED_MS: 10\nTOTAL_ELAPSED_MS: 20\nPOST_REPAIR_FANOUT_VIOLATIONS: 0\n"
        kwargs["stdout"].write(text)
        (path / "openroad_dut.v").write_text("BUF_X1 inserted (.A(a), .Z(b));")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(fanout_helper.subprocess, "run", native)
    failure = None
    try:
        fanout_helper.main()
    except (RuntimeError, FileNotFoundError) as error:
        failure = str(error)
    assert failure is None, f"scalar native runtime rejected: {failure}"
    assert (tmp_path / "work/default/native.log").is_file()


@pytest.mark.parametrize(
    "failure,match",
    [
        ("slow", "repair exceeded"),
        ("stage", "missing native physical stage"),
        ("rsz", "native fanout arm failed"),
        ("buffers", "inserted no structural buffers"),
        ("status", "native fanout arm failed"),
        ("nan", "native exact limit differs"),
    ],
)
def test_native_fanout_checker_requires_complete_evidence(
    tmp_path, monkeypatch, fanout_helper, failure, match
):
    from types import SimpleNamespace

    script = tmp_path / "run.tcl"
    script.touch()

    def native(*args, **kwargs):
        stages = (
            "floorplan",
            "global_placement",
            "repair_design",
            "detailed_placement",
            "sta_report",
        )
        text = "\n".join(
            f"BOOLEY_STAGE: {stage}"
            for stage in stages
            if failure != "stage" or stage != "sta_report"
        )
        limit = "NaN" if failure == "nan" else "50"
        text += f"\nREPAIR_ELAPSED_MS: {15001 if failure == 'slow' else 10}\nOPENROAD_FANOUT_LIMIT: {limit}\n"
        if failure == "rsz":
            text += "RSZ-0035\n"
        kwargs["stdout"].write(text)
        (tmp_path / "openroad_dut.v").write_text(
            "" if failure == "buffers" else "BUF_X1 inserted (.A(a), .Z(b));"
        )
        return SimpleNamespace(returncode=1 if failure == "status" else 0)

    monkeypatch.setattr(fanout_helper.subprocess, "run", native)
    with pytest.raises(RuntimeError, match=match):
        fanout_helper.run_case(script, "openroad", 50, failure != "rsz")
    assert (tmp_path / "native.log").read_text()


def test_native_fanout_timeout_keeps_partial_log(tmp_path, monkeypatch, fanout_helper):
    script = tmp_path / "run.tcl"
    script.touch()

    def stalled(command, **kwargs):
        assert kwargs["timeout"] == 120
        kwargs["stdout"].write("partial native diagnostic\n")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(fanout_helper.subprocess, "run", stalled)
    with pytest.raises(subprocess.TimeoutExpired):
        fanout_helper.run_case(script, "openroad", 1e20, False)
    assert (tmp_path / "native.log").read_text() == "partial native diagnostic\n"
