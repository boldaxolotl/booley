"""Sandbox e2e for Verible lint (ADR 0033, plan Part F2) — the whole-chain proof.

Exercises what nothing else covers end to end: the image's
``verible-verilog-lint`` binary, the patched Edalize ``tools/verible.py``
node, FuseSoC Target resolution, Booley's Verible warning parser, the
verdict, and the ``lint_report.json`` gate — by driving
``python3 -m booley.flows.lint`` *inside* the sandbox image against a
fixture project with one known style violation:

* violation present → WARN (exit 1), report ``passed: false``;
* waiver file added to the Target's fileset → PASS (exit 0), ``passed: true``.

Skips (never fails) when the environment can't prove the chain: no docker,
no ``booley-sandbox`` image, or an image predating Verible support — rebuild
the image (``booley init``) to unskip. Marked ``slow``: two full in-container
FuseSoC resolutions.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from tests.docker.isolation.container_names import next_ci_container_name

_IMAGE = "booley-sandbox"

# One deliberate violation under Verible's default ruleset: a trailing space.
# (no-trailing-spaces is in the default set and is line-stable — immune to
# module/filename mapping done by the FuseSoC build-tree copy.)
_TOP_SV = (
    "module top;\n"
    "  logic clk;  \n"  # <- trailing spaces: [no-trailing-spaces]
    "endmodule\n"
)

_WAIVER = 'waive --rule=no-trailing-spaces --location=".*top\\.sv"\n'

_CORE_CLEAN_TMPL = """\
CAPI=2:
name: ::verible_e2e:0
filesets:
  rtl:
    files:
      - rtl/top.sv: {{file_type: systemVerilogSource}}
{waiver_files}
targets:
  default:
    filesets: [rtl]
  lint_style:
    flow: lint
    flow_options:
      tool: verible
    filesets: [rtl]
    toplevel: top
"""


def _docker() -> str | None:
    return shutil.which("docker")


def _run_in_sandbox(args: list[str], mounts: list[str] | None = None, timeout: int = 300):
    docker = _docker()
    cmd = [docker, "run", "--rm"]
    container_name = next_ci_container_name()
    if container_name:
        cmd += ["--name", container_name]
    for m in mounts or []:
        cmd += ["-v", m]
    cmd += [_IMAGE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _require_verible_sandbox() -> None:
    if _docker() is None:
        pytest.skip("docker not available")
    probe = subprocess.run(
        [_docker(), "image", "inspect", _IMAGE],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"{_IMAGE} image not built")
    have_bin = _run_in_sandbox(["sh", "-c", "command -v verible-verilog-lint"], timeout=120)
    if have_bin.returncode != 0:
        pytest.skip(
            f"{_IMAGE} image predates Verible support (ADR 0033) — rebuild it "
            "(booley init) to run this e2e",
        )
    have_node = _run_in_sandbox(
        ["python3", "-c", "import edalize.tools.verible"],
        timeout=120,
    )
    if have_node.returncode != 0:
        pytest.skip(
            f"{_IMAGE} image lacks the Edalize verible tool node patch "
            "(ADR 0033) — rebuild it (booley init) to run this e2e",
        )


def _write_fixture(root: Path, *, waived: bool) -> None:
    (root / "rtl").mkdir(parents=True, exist_ok=True)
    (root / "rtl" / "top.sv").write_text(_TOP_SV, encoding="utf-8")
    waiver_files = ""
    if waived:
        (root / "lint").mkdir(exist_ok=True)
        (root / "lint" / "waivers.txt").write_text(_WAIVER, encoding="utf-8")
        waiver_files = "      - lint/waivers.txt: {file_type: veribleLintWaiver}\n"
    (root / "verible_e2e.core").write_text(
        _CORE_CLEAN_TMPL.format(waiver_files=waiver_files),
        encoding="utf-8",
    )


def _lint_in_sandbox(project: Path):
    # The production image intentionally runs as UID 1000, while hosted CI
    # runners may own pytest's temporary directory with another UID. Grant the
    # image user access only to this disposable mount root so it can create its
    # FuseSoC work and report directories without changing the image identity.
    project.chmod(project.stat().st_mode | stat.S_IRWXO)
    return _run_in_sandbox(
        [
            "python3",
            "-m",
            "booley.flows.lint",
            "--target",
            "lint_style",
            "--work-dir",
            "/work",
            "--report-dir",
            "/work/rep",
        ],
        mounts=[f"{project.as_posix()}:/work"],
    )


@pytest.mark.slow()
def test_verible_violation_warns_then_waiver_passes(tmp_path: Path) -> None:
    _require_verible_sandbox()

    # Round 1: known violation → WARN, criterion-shaped report says failed.
    project = tmp_path / "proj"
    _write_fixture(project, waived=False)
    run = _lint_in_sandbox(project)
    combined = run.stdout + run.stderr
    assert run.returncode == 1, f"expected WARN (exit 1), got {run.returncode}:\n{combined}"
    assert "RESULT: WARN" in combined
    report = json.loads((project / "rep" / "lint_report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert any(w["rule"] == "no-trailing-spaces" for w in report["warnings"])

    # Round 2: same RTL, waiver declared in the Target's fileset → PASS.
    _write_fixture(project, waived=True)
    run = _lint_in_sandbox(project)
    combined = run.stdout + run.stderr
    assert run.returncode == 0, f"expected PASS (exit 0), got {run.returncode}:\n{combined}"
    assert "RESULT: PASS" in combined
    report = json.loads((project / "rep" / "lint_report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["total_warnings"] == 0


def test_sandbox_runs_use_unique_ci_container_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parallel CI can identify and clean every nested Verible container."""
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("BOOLEY_DOCKER_NAME_PREFIX", "booley-ci-123-1-verible")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake_run)

    _run_in_sandbox(["true"])
    _run_in_sandbox(["true"])

    names = [command[command.index("--name") + 1] for command in commands]
    assert len(set(names)) == 2
    assert all(name.startswith("booley-ci-123-1-verible-") for name in names)
