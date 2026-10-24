"""Mutation tester exposes Target-declared ``copyto`` runtime inputs (issue #2061).

A testbench that opens a data file reads it from the configured ``run_cwd``.
FuseSoC ``copyto`` only places the file in the build root, so the mutation
tester must stage it the way the Simulation Flow does: before each run, removed
afterwards, with a missing declared ``$readmemh`` input read as infrastructure.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import booley
from booley.flows.sim.build import PreparedSimulationBuild
from booley.fusesoc.fusesoc_registry import ResolvedFile
from booley.specialists.mutation_tester import MutationTesterSpecialist
from tests.mcp_tools.test_mutation_tester import (
    _fake_proc,
    _make_endpoint,
    _patch_cocotb_target,
    _patch_resolve_target,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stand-in V<top> is a POSIX shell script")

_PAYLOAD = "deadbeef\n"
_PASSING_BINARY = """#!/bin/sh
if [ "$(cat vectors.hex 2>/dev/null)" = "deadbeef" ]; then
  echo "[SIM_RESULT] PASSED"
  exit 0
fi
echo "ERROR: Can't open file vectors.hex"
exit 1
"""
# Reports a $readmemh failure for a file the binary itself removed, then hangs
# like a real simulator spinning on uninitialised memory.
_MISSING_READMEMH_BINARY = """#!/bin/sh
rm -f vectors.hex
echo '$readmemh: Unable to open vectors.hex for reading'
sleep 30
"""
_ADAPTERS = ("verilator", "icarus", "cocotb")


class _Project:
    """A mutation checkout whose Target declares ``vectors.hex`` as a copyto input."""

    def __init__(self, root: Path, endpoint: MutationTesterSpecialist, eda_tool: str) -> None:
        self.root = root
        self.endpoint = endpoint
        self.eda_tool = eda_tool
        self.run_dir = root / "run"
        self.build = root / "build"
        self.binary = _PASSING_BINARY

    @property
    def staged(self) -> Path:
        return self.run_dir / "vectors.hex"

    def elab(self) -> None:
        """Build for real (the stand-in make command succeeds) before a run is stubbed."""
        self.build.mkdir()
        self.endpoint._run_elab("default", self.root, self.build)

    def leftovers(self) -> list[Path]:
        """Overlay directories any run left behind under the checkout."""
        return list(self.root.rglob(".booley-runtime-inputs"))

    def plan(self) -> SimpleNamespace:
        return SimpleNamespace(target="default", work_dir=self.root, tb_top="tb")


def _prepare_with_copyto(project_ref: list[_Project]):
    """A ``prepare_simulation_build`` stand-in that, like FuseSoC, ``copyto``s the file."""

    def _prepare(handle, *, build_root, lane_kind, **_kwargs):
        project = project_ref[0]
        root = Path(build_root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "vectors.hex").write_text(_PAYLOAD, encoding="utf-8")
        binary = root / "Vtb"
        binary.write_text(project.binary, encoding="utf-8")
        binary.chmod(0o755)
        resolved = SimpleNamespace(
            build_root=root,
            toplevel="tb",
            eda_tool="verilator",
            files=(ResolvedFile("vectors.hex", "user"),),
        )
        return PreparedSimulationBuild(
            target=handle.selector,
            target_identity=handle.identity,
            resolved=resolved,
            work_root=root,
            build_root=root,
            eda_tool=project.eda_tool,
            toplevel="tb",
            make_argv=(sys.executable, "-c", ""),
        )

    return _prepare


def _make_project(tmp_path: Path, monkeypatch, adapter: str = "verilator") -> _Project:
    # The real run half is a child `python -m booley...` started in the checkout.
    source_root = str(Path(booley.__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", source_root)
    _patch_resolve_target(monkeypatch, eda_tool="icarus" if adapter == "icarus" else "verilator")
    if adapter == "cocotb":
        _patch_cocotb_target(monkeypatch, module="tb_test")
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley" / "booley.toml").write_text(
        '[project]\nname = "demo"\n\n[flows.sim]\nrun_cwd = "run"\n', encoding="utf-8"
    )
    (tmp_path / "run").mkdir()
    endpoint = _make_endpoint(tmp_path, monkeypatch, target="default")
    project = _Project(tmp_path, endpoint, "icarus" if adapter == "icarus" else "verilator")
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.prepare_simulation_build",
        _prepare_with_copyto([project]),
    )
    return project


def test_verilator_baseline_sees_copyto_input(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path, monkeypatch)
    result = project.endpoint._run_pristine_baseline(project.plan())

    assert result is None, result.report_text
    assert not project.staged.exists()
    assert project.leftovers() == []


def test_verilator_mutant_sees_copyto_input(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path, monkeypatch)
    endpoint = project.endpoint
    real_elab = endpoint._run_elab
    # Source materialization is irrelevant here: build the pristine tree.
    monkeypatch.setattr(
        endpoint,
        "_run_elab",
        lambda target, work_dir, build_path, _mutation=None: real_elab(
            target, work_dir, build_path
        ),
    )
    spec = SimpleNamespace(index=1)

    result, reason = endpoint._run_one_variant(project.plan(), spec, object())

    assert not result.invalid, result.sim_output_snippet
    assert not result.detected, result.sim_output_snippet
    assert reason == ""
    assert not project.staged.exists()
    assert project.leftovers() == []


@pytest.mark.parametrize("adapter", _ADAPTERS)
def test_pinned_runs_stage_inputs_for_every_adapter(
    tmp_path: Path, monkeypatch, adapter: str
) -> None:
    project = _make_project(tmp_path, monkeypatch, adapter)
    project.elab()
    seen: dict[str, object] = {}

    def _launch(cmd, *args, **kwargs):
        seen["cmd"] = list(cmd)
        seen["bytes"] = (
            project.staged.read_text(encoding="utf-8") if project.staged.is_file() else None
        )
        return _fake_proc(rc=1, stdout="failed")

    monkeypatch.setattr("booley.specialists.mutation_tester.subprocess.run", _launch)

    project.endpoint._run_sim_pinned("default", tmp_path, project.build, "tb")

    assert "--runtime-input=vectors.hex" in seen["cmd"]  # type: ignore[operator]
    assert seen["bytes"] == _PAYLOAD
    assert list(project.run_dir.iterdir()) == []
    assert project.leftovers() == []


def test_staged_input_is_removed_after_timeout(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path, monkeypatch)
    project.elab()
    visible: list[bool] = []

    def _hang(cmd, *args, **kwargs):
        visible.append(project.staged.is_file())
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr("booley.specialists.mutation_tester.subprocess.run", _hang)

    runs = project.endpoint._run_target_test_suite("default", tmp_path, project.build, "tb")

    assert [run.timed_out for run in runs] == [True]
    assert visible == [True]
    assert list(project.run_dir.iterdir()) == []


def test_declared_unstaged_readmemh_input_is_inconclusive(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path, monkeypatch)
    project.binary = _MISSING_READMEMH_BINARY
    project.elab()

    runs = project.endpoint._run_target_test_suite("default", tmp_path, project.build, "tb")

    reason = project.endpoint._suite_inconclusive_reason(runs)
    assert "declared runtime input 'vectors.hex' was not staged" in reason


def test_conflicting_destination_is_inconclusive_setup_failure(
    tmp_path: Path, monkeypatch
) -> None:
    project = _make_project(tmp_path, monkeypatch)
    project.staged.write_text("project data\n", encoding="utf-8")
    project.elab()
    launches: list[object] = []
    monkeypatch.setattr(
        "booley.specialists.mutation_tester.subprocess.run",
        lambda cmd, *a, **k: launches.append(cmd) or _fake_proc(),
    )

    runs = project.endpoint._run_target_test_suite("default", tmp_path, project.build, "tb")

    assert (
        "Simulation runtime input setup failed: "
        "run input conflicts with an existing path: vectors.hex"
    ) in project.endpoint._suite_inconclusive_reason(runs)
    assert project.staged.read_text(encoding="utf-8") == "project data\n"
    assert launches == []


def test_identical_existing_destination_is_left_untouched(tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path, monkeypatch)
    project.staged.write_text(_PAYLOAD, encoding="utf-8")
    inode = project.staged.stat().st_ino

    result = project.endpoint._run_pristine_baseline(project.plan())

    assert result is None, result.report_text
    assert project.staged.is_file() and not project.staged.is_symlink()
    assert project.staged.read_text(encoding="utf-8") == _PAYLOAD
    assert project.staged.stat().st_ino == inode
