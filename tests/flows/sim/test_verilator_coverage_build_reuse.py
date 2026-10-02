"""Native-coverage build reuse through the unpatched key and read-closure path.

``VerilatorCoverageExecution.build`` runs end to end: the real coverage
overlay, real FuseSoC/Edalize preparation (stock flow-API ``Makefile`` plus
``.vc``), the real ``make`` build stage, and the production
``SimulationBuildSession`` reuse key (stock-recipe check, toolchain identity,
environment digest) and read closure (``build_reuse.collect_read_closure`` /
``verify_read_closure``). Nothing in the session is patched. Only the Verilator
toolchain is fake: the one from the Simulation engine reuse tests, whose
image prints the marker word (``OLD``/``NEW``) of the source it was built
from, so launching a stale image is visible.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim import build_reuse
from booley.flows.sim.build_session import simulation_build_slot
from booley.flows.sim.execution.contract import SimulationOptions
from booley.flows.sim.verilator_coverage import (
    PINNED_VERILATOR,
    VERILATOR_COVERAGE_INSTRUMENTATION,
    CoverageTarget,
    SelectedCoverageTest,
    SimulationBuildRequest,
    SimulationBuildResult,
    SimulationBuildVariant,
    SimulationRunRequest,
)
from booley.flows.sim.verilator_coverage_execution import VerilatorCoverageExecution
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle
from tests.flows.sim import test_verilator_build_reuse_engine as engine

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Verilator build reuse is proven in the POSIX Sandbox"
)

_ADAPTER_MODULE = "booley.flows.sim.backends.verilator"
_HIT = "verified Simulation build reuse (hit)"
_PLAIN = SimulationBuildVariant(trace=False, coverage=True)
_TRACED = SimulationBuildVariant(trace=True, coverage=True)
# Coverage builds add value-taking dump switches the engine-test fake does not
# know; teach it to consume their values instead of reading them as sources.
_VALUE_FLAGS = '("-CFLAGS", "-LDFLAGS", "--Mdir", "--prefix", "-o", "--trace-depth"'
_COVERAGE_VALUE_FLAGS = f'{_VALUE_FLAGS}, "--dumpi-tree-json", "--dumpi-V3Global"'


class _Invoker:
    """Run commands for real, answer the collector probe, record builds and launches."""

    def __init__(self, project: Path) -> None:
        source_root = Path(__file__).resolve().parents[3] / "src"
        self._cwd = project
        self._python_path = os.pathsep.join(
            part for part in (str(source_root), os.environ.get("PYTHONPATH", "")) if part
        )
        self.builds = 0
        self.launches: list[str] = []

    def __call__(self, command: list[str], *, timeout: int) -> SubprocessResult:
        if command == ["verilator", "--version"]:
            return SubprocessResult(returncode=0, stdout="Verilator 5.052 2026-09-05\n")
        started = time.monotonic()
        result = subprocess.run(
            command,
            cwd=self._cwd,
            env={**os.environ, "PYTHONPATH": self._python_path},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        self.builds += "BOOLEY_BUILD_STAGE" in command[-1]
        if _ADAPTER_MODULE in command[-1]:
            self.launches.append(result.stdout)
        return SubprocessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_s=time.monotonic() - started,
        )


@pytest.fixture
def coverage_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake toolchain on PATH and return the Project root."""
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    verilator_bin = engine._write_fake_verilator(tmp_path)
    wrapper = verilator_bin / "verilator"
    script = wrapper.read_text(encoding="utf-8")
    assert _VALUE_FLAGS in script, "engine fake Verilator changed shape"
    wrapper.write_text(script.replace(_VALUE_FLAGS, _COVERAGE_VALUE_FLAGS), encoding="utf-8")
    path = os.pathsep.join((str(verilator_bin), str(tmp_path / "toolbin"), os.environ["PATH"]))
    monkeypatch.setenv("PATH", path)
    for name in ("VERILATOR_ROOT", "MAKEFLAGS", "MFLAGS", "EDALIZE_LAUNCHER"):
        monkeypatch.delenv(name, raising=False)
    # The fake installation is written moments before the first build; the
    # production race guard would (correctly) refuse to trust its files.
    monkeypatch.setattr(build_reuse, "RACE_MARGIN_NS", 0)
    project = engine._write_project(tmp_path)
    (tmp_path / "BOOLEY-SOURCE.txt").write_text(
        f"release={PINNED_VERILATOR.tag}\nsource_revision={PINNED_VERILATOR.commit}\n",
        encoding="utf-8",
    )
    return project


def _handle(project: Path) -> TargetHandle:
    return TargetCatalog.build(project).select("sim", for_flow="sim")


def _execution(project: Path, invoker: _Invoker) -> VerilatorCoverageExecution:
    return VerilatorCoverageExecution(
        _handle(project),
        invoke=invoker,
        options=SimulationOptions(timeout_ms=10_000),
        provenance_path=project.parent / "BOOLEY-SOURCE.txt",
        pre_sim_commands=(),
    )


def _target(project: Path) -> CoverageTarget:
    handle = _handle(project)
    return CoverageTarget(handle.identity, handle.selector, "tb", "hdl_testbench", ())


def _build(
    execution: VerilatorCoverageExecution,
    project: Path,
    variant: SimulationBuildVariant = _PLAIN,
) -> SimulationBuildResult:
    result = execution.build(
        SimulationBuildRequest(_target(project), variant, VERILATOR_COVERAGE_INSTRUMENTATION)
    )
    assert result.success, result.output
    return result


def _run_request(project: Path) -> SimulationRunRequest:
    raw_path = project.parent / "artifacts" / "raw.dat"
    return SimulationRunRequest(
        target=_target(project),
        test=SelectedCoverageTest("smoke"),
        run_id="run:001:smoke",
        raw_path=raw_path,
        hook_evidence_path=None,
        trace=False,
        argv_suffix=(f"+verilator+coverage+file+{raw_path}",),
        environment={"BOOLEY_COVERAGE_RUN_ID": "run:001:smoke"},
    )


def _current_build_root(project: Path, variant: SimulationBuildVariant) -> Path:
    slot = simulation_build_slot(_handle(project), variant.name)
    pointer = json.loads((slot / "current.json").read_text(encoding="utf-8"))
    return slot / "g" / pointer["generation"] / pointer["build_root"]


def _manifest(project: Path, variant: SimulationBuildVariant) -> dict[str, object]:
    path = _current_build_root(project, variant) / ".booley-build-manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_unchanged_coverage_target_reuses_its_verified_image(coverage_env: Path) -> None:
    invoker = _Invoker(coverage_env)
    execution = _execution(coverage_env, invoker)
    first = _build(execution, coverage_env)
    first_root, first_artifacts = execution.authenticated_image()
    assert _HIT not in first.output
    manifest = _manifest(coverage_env, _PLAIN)
    assert manifest["reusable"] is True, manifest["reason"]
    closure = manifest["read_closure"]
    assert isinstance(closure, dict)
    assert any(key.endswith("rtl/tb.sv") for key in closure)
    assert any(key.endswith("inc/defs.svh") for key in closure)
    assert any(key.endswith("dpi/helper.cpp") for key in closure)

    second = _build(execution, coverage_env)
    run = execution.run(_run_request(coverage_env))

    assert _HIT in second.output
    assert invoker.builds == 1
    root, artifacts = execution.authenticated_image()
    assert root == first_root
    assert artifacts == first_artifacts
    assert len(list(root.parent.parent.iterdir())) == 1  # unused candidate discarded
    assert run.verdict == "pass", run.output
    assert len(invoker.launches) == 1 and "IMAGE=OLD" in invoker.launches[0]


def test_coverage_variants_each_reuse_their_own_slot(coverage_env: Path) -> None:
    invoker = _Invoker(coverage_env)
    execution = _execution(coverage_env, invoker)
    sequence = (_PLAIN, _TRACED, _PLAIN, _TRACED)
    results, roots = [], []
    for variant in sequence:
        results.append(_build(execution, coverage_env, variant))
        roots.append(execution.authenticated_image()[0])

    assert invoker.builds == 2
    assert [_HIT in result.output for result in results] == [False, False, True, True]
    assert roots[2] == roots[0] and roots[3] == roots[1]
    handle = _handle(coverage_env)
    plain_slot = simulation_build_slot(handle, _PLAIN.name)
    traced_slot = simulation_build_slot(handle, _TRACED.name)
    assert plain_slot != traced_slot
    assert roots[0].is_relative_to(plain_slot) and roots[1].is_relative_to(traced_slot)
    assert _current_build_root(coverage_env, _PLAIN) == roots[0]
    assert _current_build_root(coverage_env, _TRACED) == roots[1]


@pytest.mark.parametrize(
    ("name", "text", "marker"),
    [
        ("rtl/tb.sv", '`include "defs.svh"\nmodule tb; // NEW\nendmodule\n', "IMAGE=NEW"),
        ("inc/defs.svh", "// DEFS v2\n", "DEFS v2"),
        ("dpi/helper.cpp", "// HELPER v2\n", "HELPER v2"),
    ],
    ids=["rtl", "included-header", "dpi-source"],
)
def test_coverage_source_edit_rebuilds_and_never_launches_old_image(
    coverage_env: Path, name: str, text: str, marker: str
) -> None:
    invoker = _Invoker(coverage_env)
    execution = _execution(coverage_env, invoker)
    _build(execution, coverage_env)
    old_root = execution.authenticated_image()[0]
    engine._edit_keeping_mtime(coverage_env / name, text)

    rebuilt = _build(execution, coverage_env)
    new_root, artifacts = execution.authenticated_image()
    run = execution.run(_run_request(coverage_env))

    assert _HIT not in rebuilt.output
    assert invoker.builds == 2
    assert new_root != old_root
    assert marker in artifacts[0].read_text(encoding="utf-8")
    assert run.verdict == "pass", run.output
    assert len(invoker.launches) == 1 and marker in invoker.launches[0]
    assert _current_build_root(coverage_env, _PLAIN) == new_root


def test_tampered_retained_read_closure_file_rebuilds(coverage_env: Path) -> None:
    invoker = _Invoker(coverage_env)
    execution = _execution(coverage_env, invoker)
    _build(execution, coverage_env)
    old_root = execution.authenticated_image()[0]
    # A generated header lies outside the staged-input snapshot; only the
    # read closure recorded from the dependency files can notice the edit.
    (old_root / "Vtb.h").write_text("// tampered generated header\n", encoding="utf-8")

    rebuilt = _build(execution, coverage_env)

    assert _HIT not in rebuilt.output
    assert invoker.builds == 2
    assert execution.authenticated_image()[0] != old_root
