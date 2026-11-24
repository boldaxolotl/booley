"""Tests for AsicSynthesizeFlow — parsing, delta computation, baseline flow, dry-run."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import itertools
import json
import logging
import math
import os
import re
import shutil
import time
import tomllib
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

import pytest

from booley.core.build_paths import work_root_for
from booley.flows import artifacts
from booley.flows.base import SubprocessResult
from booley.flows.baseline_worktree import baseline_worktree_path
from booley.flows.edam import work_root_lease
from booley.flows.progress_lifecycle import abort_open_progress
from booley.flows.synth.backends import configure
from booley.flows.synth.backends import pipeline as syn_make
from booley.flows.synth.backends.yosys.parsing import parse_stat
from booley.flows.synth.flow import (
    AsicSynthesizeFlow,
    SynthMetrics,
    _baseline_self_compare_warning,  # private: see test_baseline_self_compare_warning
    synth_target_report_slug,
)
from booley.flows.synth.mode import SynthMode
from booley.flows.synth.recipe import (
    default_recipe_args,
    synthesis_recipe_changes,
    synthesis_recipe_snapshot,
    synthesis_recipe_snapshot_fingerprint,
)
from booley.flows.synth.request import SynthRequest
from booley.flows.synth.timing import StaTimingConfig
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.presentation.report_results import present_job_report
from booley.runtime.endpoint_execution import EndpointOutcome, ExecutionResult
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle
from tests.canned_process_support import canned_command
from tests.conftest import symlink_or_skip
from tests.flows.synth.captured_yosys import load_capture
from tests.fusesoc_test_support import relocated_project
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child
from tests.target_test_support import install_lenient_target_catalog, make_target_handle
from tests.time_budget import hang_guard_s, scaled

_REAL_CATALOG_BUILD = TargetCatalog.build


class TestEdaFailureClassification:
    @pytest.mark.parametrize(
        ("stage", "executable"),
        (("sv2v", "sv2v"), ("yosys", "yosys"), ("openroad", "openroad")),
    )
    def test_missing_executable_is_infrastructure_error(self, stage: str, executable: str) -> None:
        from booley.flows.eda_failures import classify_eda_failure

        result = classify_eda_failure(
            SubprocessResult(
                returncode=2,
                stdout=f"/bin/sh: 1: {executable}: not found\n",
                stderr="",
            ),
            expected_stage=stage,
            expected_executable=executable,
        )
        assert result is not None
        assert result.kind == "infrastructure"
        assert f"missing tool: {executable}" in result.reason

    @pytest.mark.parametrize(
        ("stage", "subject"),
        (
            ("yosys", "read_slang"),
            ("yosys", "cells.lib"),
            ("openroad", "Nangate45_tech.lef"),
        ),
    )
    def test_missing_dependency_is_infrastructure_error(self, stage: str, subject: str) -> None:
        from booley.flows.eda_failures import classify_eda_failure, render_failure_marker

        token = "0123456789abcdef0123456789abcdef"
        marker = render_failure_marker(token, "missing_required_file", stage, subject)
        result = classify_eda_failure(
            SubprocessResult(returncode=2, stdout=marker, stderr=""),
            expected_token=token,
            expected_stage=stage,
        )
        assert result is not None
        assert result.kind == "infrastructure"
        assert subject in result.reason


def _layer_target_handle(project_root: Path | str, selector: str) -> TargetHandle:
    return make_target_handle(
        project_root,
        selector,
        flow="generic",
        eda_tool="yosys",
        drivable_by=("synth",),
    )


@pytest.fixture(autouse=True)
def _adr0039_lenient_selection(monkeypatch):
    """Pre-0039 pass-through target selection for these layer-focused tests.

    ADR 0039 made resolve_target_selection validate every token against a
    real .core surface. These unit tests mock the build/run layers and name
    ad-hoc targets (lite/full/...) without authoring cores, so give them the
    old pass-through selection — the .core-mandatory behavior itself is
    pinned in test_fusesoc_registry.py (test_no_core_rejects_any_token) and
    the .core-authoring integration tests.
    """

    install_lenient_target_catalog(
        monkeypatch,
        real_catalog_build=_REAL_CATALOG_BUILD,
        fallback_handle=_layer_target_handle,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def test_synth_target_report_slug_is_safe_and_collision_resistant():
    first = synth_target_report_slug("vendor:lib:core#asic")
    second = synth_target_report_slug("vendor_lib_core_asic")

    assert re.fullmatch(r"[A-Za-z0-9_.-]+", first)
    assert first != second
    assert second == "vendor_lib_core_asic"


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set the slug env var and return the unused state path."""
    monkeypatch.setenv("BOOLEY_SLUG", "test-ticket")
    return tmp_path / "state.json"


@pytest.fixture()
def flow_and_state(state_file: Path, tmp_path: Path):
    """Return (Flow, state_file) with common args parsed and state loaded."""
    flow = AsicSynthesizeFlow()
    flow.parse_args(
        [
            "--target",
            "lite",
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    return flow, state_file


def _write_execution_config(tmp_path: Path, body: str) -> None:
    """Write a ``[flows.synth]`` booley.toml with *body* lines."""
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir(exist_ok=True)
    (booley_dir / "booley.toml").write_text(
        f"[flows.synth]\n{body}",
        encoding="utf-8",
    )


# A minimal synth `.core` for the real-fusesoc resolution e2e. The `arch`
# flow_option is plumbing the edalize yosys backend's configure() requires; it
# is ignored — Booley reruns yosys its own way (the command-gen exception), so
# FuseSoC is used only to resolve sources/top/params. The header is an
# `is_include_file` (surfaced as an include dir, not a source); the TB file is
# `tb`-tagged and excluded from synthesis.
_SYN_CORE_TEXT = """\
CAPI=2:
name: ::syn_demo:0
description: asic_synthesize slice fixture
filesets:
  rtl:
    files:
      - rtl/include/defs.svh: {file_type: systemVerilogSource, is_include_file: true}
      - rtl/pkg.sv: {file_type: systemVerilogSource}
      - rtl/dut.sv: {file_type: systemVerilogSource}
    file_type: systemVerilogSource
  tb:
    files:
      - tb/tb_dut.sv: {file_type: systemVerilogSource}
    tags: [tb]
  constraints:
    files:
      - constraints/dut.sdc: {file_type: SDC}
parameters:
  WIDTH: {datatype: int, default: 8, paramtype: vlogparam}
  SYNTHESIS: {datatype: bool, paramtype: vlogdefine, default: true}
targets:
  default:
    filesets: [rtl]
  syn:
    default_tool: yosys
    flow: generic
    flow_options: {tool: yosys, arch: xilinx}
    filesets: [rtl, tb, constraints]
    parameters: [WIDTH, SYNTHESIS]
    toplevel: dut
"""


def _fake_synth_resolved(
    work_dir: Path,
    *,
    config: str = "lite",
    toplevel: str = "dut",
) -> fusesoc_registry.ResolvedTarget:
    """A ResolvedTarget shaped like a real synth EDAM, under asic_synthesize's work root."""
    build_root = (
        Path(work_dir)
        / ".booley"
        / ".runtime"
        / "edalize"
        / "synth"
        / config
        / "syn_demo_0"
        / "syn"
    )
    staged_sdc = build_root / "src" / "syn_demo_0" / "constraints" / "dut.sdc"
    staged_sdc.parent.mkdir(parents=True, exist_ok=True)
    staged_sdc.write_text(
        "create_clock -name clk -period 4.0 [get_ports clk]\n",
        encoding="utf-8",
    )
    files = (
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/rtl/include/defs.svh",
            file_type="systemVerilogSource",
            is_include=True,
        ),
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/rtl/pkg.sv",
            file_type="systemVerilogSource",
        ),
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/rtl/dut.sv",
            file_type="systemVerilogSource",
        ),
        # A file_type:SDC fileset is the post-ADR-0029 norm for a synth Target
        # and (ADR 0031) the thing whose absence is now a hard error — so the
        # default fake carries one, exercising the --sta-sdc forwarding path.
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/constraints/dut.sdc",
            file_type="SDC",
        ),
        fusesoc_registry.ResolvedFile(
            name="src/syn_demo_0/tb/tb_dut.sv",
            file_type="systemVerilogSource",
            tags=("tb",),
        ),
    )
    params = {
        "WIDTH": {"datatype": "int", "paramtype": "vlogparam", "default": 8},
        "SYNTHESIS": {"datatype": "bool", "paramtype": "vlogdefine", "default": True},
    }
    # Many boundary tests use booley.toml as a compact way to author the fake
    # Target recipe. Production never reads these keys there; Doctor has
    # separate migration tests that reject them in booley.toml.
    recipe: dict = {
        "tool": "yosys",
        "synth_mode": "logical",
    }  # upstream FuseSoC flow_options fields
    config_path = Path(work_dir) / ".booley" / "booley.toml"
    if config_path.is_file():
        with config_path.open("rb") as config_file:
            flow_cfg = tomllib.load(config_file).get("flows", {}).get("synth", {})
        for key in (
            "flatten",
            "frontend",
            "advanced_settings_openroad",
            "advanced_settings_yosys",
            "openroad",
            "ppa_profile",
            "slang_options",
            "synth_mode",
            "timing_engine",
            "yosys",
        ):
            if key in flow_cfg:
                recipe[key] = flow_cfg[key]
    return fusesoc_registry.ResolvedTarget(
        name=config,
        vlnv="::syn_demo:0",
        toplevel=toplevel,
        eda_tool="yosys",
        flow_options=recipe,
        files=files,
        parameters=params,
        build_root=build_root,
        edam_path=build_root / "syn_demo_0.eda.yml",
    )


def _with_synth_mode(
    resolved: fusesoc_registry.ResolvedTarget,
    mode: str,
) -> fusesoc_registry.ResolvedTarget:
    """Return a fake Target with an explicit synthesis mode."""
    return dataclasses.replace(
        resolved,
        flow_options={**resolved.flow_options, "synth_mode": mode},
    )


def _write_syn_demo_project(work_dir: Path) -> None:
    """Materialize the syn_demo fixture on disk: RTL + include + tb + SDC + core."""
    (work_dir / "rtl" / "include").mkdir(parents=True)
    (work_dir / "tb").mkdir(parents=True)
    (work_dir / "rtl" / "include" / "defs.svh").write_text(
        "`define FOO 1\n",
        encoding="utf-8",
    )
    (work_dir / "rtl" / "pkg.sv").write_text(
        "package pkg; endpackage\n",
        encoding="utf-8",
    )
    (work_dir / "rtl" / "dut.sv").write_text(
        "module dut #(parameter WIDTH=8)(input logic clk); endmodule\n",
        encoding="utf-8",
    )
    (work_dir / "tb" / "tb_dut.sv").write_text(
        "module tb_dut; dut d(.clk(1'b0)); endmodule\n",
        encoding="utf-8",
    )
    (work_dir / "constraints").mkdir()
    (work_dir / "constraints" / "dut.sdc").write_text(
        "create_clock -name clk -period 4.0 [get_ports clk]\n",
        encoding="utf-8",
    )
    (work_dir / "syn_demo.core").write_text(_SYN_CORE_TEXT, encoding="utf-8")


# The Target-handle entry points that fixtures here replace per test; the AC4
# test needs the genuine ones.
_REAL_RESOLVE_HANDLE = fusesoc_registry.resolve_target_handle
_REAL_SETUP_HANDLE = fusesoc_registry.setup_command_for_handle


def _fusesoc_cmd() -> list[str]:
    """Return the FuseSoC argv: the console script, else ``python -c`` without one."""
    import sys

    if shutil.which("fusesoc"):
        return list(fusesoc_registry.DEFAULT_FUSESOC_CMD)
    return [sys.executable, "-c", "from fusesoc.main import main; main()"]


def _real_resolve_handle(handle, **kwargs):
    """Run the genuine Target-handle resolver, honoring the build's library plan."""
    return _REAL_RESOLVE_HANDLE(handle, **{**kwargs, "fusesoc_cmd": _fusesoc_cmd()})


@dataclasses.dataclass
class _Resolution:
    """The FuseSoC resolver the autouse fake delegates to; a test swaps ``resolve``."""

    resolve: Callable[..., fusesoc_registry.ResolvedTarget]


@pytest.fixture(autouse=True)
def _stub_fusesoc_resolution(tmp_path: Path):
    """Default every test's FuseSoC resolution to a fake synth EDAM.

    A real ``fusesoc run --setup`` against a project with no ``.core`` fails, so
    the execution-path tests resolve through this fake. It yields the holder
    whose ``resolve`` a test replaces to shape the resolved Target; tests that
    exercise resolution itself re-patch ``resolve_target_handle`` inside a ``with``
    block, and the real-FuseSoC tests set ``resolve`` to ``_real_resolve_handle``.
    """
    resolution = _Resolution(
        resolve=lambda handle, **_kwargs: _fake_synth_resolved(tmp_path, config=handle.selector)
    )

    def resolve(*args, **kwargs):
        return resolution.resolve(*args, **kwargs)  # a test may have replaced it

    # Same seam as `_REAL_RESOLVE_HANDLE`: no FuseSoC run or `.core` exists on a test host.
    with patch.object(fusesoc_registry, "resolve_target_handle", side_effect=resolve):
        yield resolution


def _stub_plan(
    work_dir: Path,
    target: str,
    *,
    mode: str = "logical",
    design: str = "dut",
) -> syn_make.SynthPlan:
    """A minimal configured SynthPlan under *target*'s real build dir."""
    build_dir = Path(work_dir) / ".booley" / ".runtime" / "edalize" / "synth" / target / "synth"
    build_dir.mkdir(parents=True, exist_ok=True)
    spec = syn_make.SynthSpec(
        design_name=design,
        sources=(),
        inc_dirs=(),
        defines=(),
        params={},
        liberty=Path("/opt/pdk/fake.lib"),
        liberty_found=True,
        flatten=True,
        abc_recipe=None,
        frontend="sv2v",
        timing=StaTimingConfig(
            mode=mode,
        ),
    )
    return syn_make.SynthPlan(build_dir=build_dir, spec=spec)


@pytest.fixture(autouse=True)
def _stub_configure_synth():
    """Default the configure half (ADR 0037 §8) to a canned plan.

    The execution-path tests mock the boundary executor and feed it synthetic
    output; real script rendering would fail because the fake resolved sources
    never exist on disk. The spec argv still runs for real: the fixture records
    each Target's configure argv in the dict it yields and stubs only the render
    step. Tests that exercise rendering itself call ``run_yosys_syn.resolve_spec``
    / ``syn_make`` directly (see tests/flows/synth/backends/yosys/test_pipeline.py
    and the golden Makefile snapshots).
    """
    configured: dict[str, list[str]] = {}

    def fake_configure(self, target, cmd):
        configured[target] = list(cmd)
        mode = cmd[cmd.index("--synth-mode") + 1]
        return _stub_plan(Path(self.args.work_dir), target, mode=mode)

    # `_configure_synth` renders the build dir the EDA tools read, and no test
    # host has them; no public name reaches that step alone.
    with patch.object(AsicSynthesizeFlow, "_configure_synth", fake_configure):
        yield configured


# ===========================================================================
# Commands and invocation (through the Flow's CLI)
# ===========================================================================

_PASS_OUTPUT = SubprocessResult(
    returncode=0,
    stdout="Chip area for top module '\\dut': 6400.0\nNumber of cells: 100\n",
    stderr="",
    duration_s=1.0,
)
_SYNTH_ROOT = ".booley/.runtime/edalize/synth/lite"
_STAGED_SRC = f"{_SYNTH_ROOT}/syn_demo_0/syn/src/syn_demo_0"


@dataclasses.dataclass
class SynthCli:
    """Drive the ASIC Synthesis Flow through ``execute_cli`` against a temporary Project.

    The faked tool output carries no OpenROAD result, so a physical Target's run
    ends in a failed verdict; tests of the configure argv read ``configured``
    instead of the exit code.
    """

    project: Path
    resolution: _Resolution
    configured: dict[str, list[str]]
    tool_output: SubprocessResult | Callable[[list[str]], SubprocessResult] = dataclasses.field(
        default_factory=lambda: _PASS_OUTPUT
    )
    commands: list[list[str]] = dataclasses.field(default_factory=list)
    flow: AsicSynthesizeFlow | None = None

    def config(self, body: str) -> None:
        """Write ``[flows.synth]`` plus *body* into the Project's ``booley.toml``."""
        _write_execution_config(self.project, body)

    def run(self, *argv: str, target: str = "lite") -> EndpointOutcome:
        """Run the Flow on *target* through its CLI."""
        return self._drive(
            lambda flow: flow.execute_cli(
                ["--target", target, "--work-dir", str(self.project), *argv]
            )
        )

    def run_request(self, target: str = "lite") -> EndpointOutcome:
        """Run the Flow on a typed request, which keeps the Project path as spelled.

        The CLI resolves ``--work-dir``; a typed caller may pass a path behind a link.
        """
        return self._drive(
            lambda flow: flow.execute(SynthRequest(target=target, work_dir=self.project))
        )

    def _drive(self, invoke: Callable[[AsicSynthesizeFlow], ExecutionResult]) -> EndpointOutcome:
        def execute(_flow: AsicSynthesizeFlow, cmd: list[str], **_kwargs) -> SubprocessResult:
            self.commands.append(list(cmd))
            return self.tool_output(cmd) if callable(self.tool_output) else self.tool_output

        self.flow = AsicSynthesizeFlow()
        # No EDA tool runs on test hosts, so the Sandbox subprocess boundary is
        # the one faked seam.
        with patch.object(AsicSynthesizeFlow, "_execute", execute):
            return invoke(self.flow).outcome

    def plan(self, *argv: str, target: str = "lite") -> dict:
        """Dry-run *target* and return the published plan."""
        outcome = self.run("--dry-run", *argv, target=target)
        assert outcome.exit_code == EXIT_SUCCESS, outcome.report_text
        return outcome.detail

    @property
    def run_plan(self):
        """The plan the last run executed.

        A real run publishes no plan (only ``--dry-run`` does), and the dry/real
        equality is the behavior under test, so this reads the Flow's own record.
        """
        assert self.flow is not None
        return self.flow._flow_plan  # the public result carries no real-run plan

    def report(self, target: str = "lite") -> dict:
        """The per-Target JSON report the last run wrote."""
        path = self.project / ".booley" / "flow-reports" / f"synth_{target}.json"
        return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def synth_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pin_memory_budget: Callable[[int], None],
    _stub_fusesoc_resolution: _Resolution,
    _stub_configure_synth: dict[str, list[str]],
) -> SynthCli:
    # The in-process CLI runs only inside the Sandbox.
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    # An inherited runtime dir would send reports and events away from the Project.
    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    pin_memory_budget(64 * 1024)
    return SynthCli(tmp_path, _stub_fusesoc_resolution, _stub_configure_synth)


def _operands(argv: list[str], flag: str) -> list[str]:
    return [argv[index + 1] for index, arg in enumerate(argv) if arg == flag]


def _has_tokens(argv: list[str], *tokens: str) -> bool:
    """Whether *tokens* appear in *argv* as one adjacent run."""
    return any(argv[i : i + len(tokens)] == list(tokens) for i in range(len(argv)))


def _staged(
    resolved: fusesoc_registry.ResolvedTarget, data: bytes | None = None
) -> fusesoc_registry.ResolvedTarget:
    """Write the resolved files under the build root, as a FuseSoC setup does.

    Files that already exist keep their content unless *data* is given.
    """
    for item in resolved.files:
        path = item.absolute(resolved.build_root)
        if data is not None or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data or b"x")
    return resolved


def _resolving(make: Callable[[Path], fusesoc_registry.ResolvedTarget]):
    """A resolver that builds and stages its Target for the checkout it is asked about."""

    def resolve(handle, **_kwargs):
        return _staged(make(Path(handle.project_root)))

    return resolve


def test_dry_run_plans_every_selected_target(synth_cli: SynthCli) -> None:
    outcome = synth_cli.run("--dry-run", target="lite,full")

    assert outcome.exit_code == EXIT_SUCCESS
    assert "Dry run" in outcome.report_text
    assert [unit["selector"] for unit in outcome.detail["work_units"]] == ["lite", "full"]
    assert list((synth_cli.project / _SYNTH_ROOT).parent.glob(".plan-*")) == []


def test_dry_run_setup_writes_only_inside_the_booley_directory(synth_cli: SynthCli) -> None:
    mine = synth_cli.project / ".booley-synth-plan-mine" / "notes.txt"
    other = (synth_cli.project / _SYNTH_ROOT).parent / ".plan-other" / "notes.txt"
    for path in (mine, other):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"mine")
    before = sorted(synth_cli.project.iterdir())
    during: list[list[str]] = []
    resolve = synth_cli.resolution.resolve

    def watch(*args, **kwargs):
        during.append(sorted(synth_cli.project.iterdir()))
        return resolve(*args, **kwargs)

    synth_cli.resolution.resolve = watch

    assert synth_cli.run("--dry-run").exit_code == EXIT_SUCCESS
    assert synth_cli.run().exit_code == EXIT_SUCCESS
    assert during[0] == before
    assert [path.read_bytes() for path in (mine, other)] == [b"mine", b"mine"]


def test_dry_and_real_runs_plan_the_same_work(synth_cli: SynthCli) -> None:
    dry = synth_cli.plan()
    outcome = synth_cli.run()

    assert outcome.exit_code == EXIT_SUCCESS, outcome.report_text
    assert dry["semantic_plan_fingerprint"] == synth_cli.run_plan.semantic_plan_fingerprint


def test_physical_dry_run_discloses_runtime_clock_validation(
    synth_cli: SynthCli, tmp_path: Path
) -> None:
    synth_cli.resolution.resolve = lambda *_a, **_k: _with_synth_mode(
        _fake_synth_resolved(tmp_path), "physical"
    )

    recipe = synth_cli.plan()["work_units"][0]["recipe"]

    assert "clock creation is validated by OpenROAD at runtime" in recipe["clock_validation"]


def test_dry_run_records_evidence_for_projected_stealth_core(
    synth_cli: SynthCli, tmp_path: Path
) -> None:
    work_dir = tmp_path / "proj"
    _write_syn_demo_project(work_dir)
    booley_dir = work_dir / ".booley"
    cores = booley_dir / "cores"
    cores.mkdir(parents=True)
    (work_dir / "syn_demo.core").replace(cores / "syn_demo.core")
    (booley_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
    (booley_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
    synth_cli.project = work_dir
    synth_cli.resolution.resolve = _real_resolve_handle

    dry = synth_cli.plan(target="syn")
    synth_cli.run(target="syn")

    assert dry["work_units"][0]["selector"] == "syn"
    assert dry["semantic_plan_fingerprint"] == synth_cli.run_plan.semantic_plan_fingerprint
    assert synth_cli.report("syn")["run_evidence"]["source_sha256"]


@pytest.mark.skipif(
    shutil.which("git") is None or os.name == "nt",
    reason="`booley worktree new` runs only in the Sandbox on POSIX with git",
)
@pytest.mark.parametrize("isolation", ["isolated", "projected"])
def test_synth_plan_identity_matches_in_booley_worktree(
    isolation: str,
    synth_cli: SynthCli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same Target and inputs plan identically in the main and a worktree checkout.

    Isolated Projects name sources by Project-relative path, hiding the
    private source mirror; without isolation the FuseSoC export names stay.
    """
    import argparse

    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    from booley.harness import worktree_cmd

    # `booley worktree new` refuses a foreign ambient BOOLEY_DIR.
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "repo"
    _write_syn_demo_project(root)
    booley_dir = root / ".booley"
    (booley_dir / "cores").mkdir(parents=True)
    (root / "syn_demo.core").replace(booley_dir / "cores" / "syn_demo.core")
    isolate = "ignore_native_cores = true\n" if isolation == "isolated" else ""
    (booley_dir / "booley.toml").write_text(
        f"[stealth]\nenabled = true\n{isolate}", encoding="utf-8"
    )
    (booley_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
    for args in (
        ("init",),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.com"),
        ("add", "rtl", "tb", "constraints"),
        ("commit", "-m", "init"),
    ):
        run_bounded_child(["git", *args], cwd=root, check=True)
    # Created before any preparation, so the Snapshot holds no generated state.
    assert worktree_cmd.run(argparse.Namespace(worktree_command="new", name="wt"), root) == 0

    def plans(checkout: Path):
        synth_cli.project = checkout
        with (
            patch.object(
                fusesoc_registry, "resolve_target_handle", side_effect=_real_resolve_handle
            ),
            patch.object(fusesoc_registry, "setup_command_for_handle", _REAL_SETUP_HANDLE),
        ):
            dry = synth_cli.plan(target="syn")
            synth_cli.run(target="syn")
        return dry, synth_cli.run_plan

    main_dry, main_real = plans(root)
    wt_dry, wt_real = plans(booley_dir / "worktrees" / "wt")

    assert main_dry["semantic_plan_fingerprint"] == wt_dry["semantic_plan_fingerprint"]
    assert main_real.semantic_plan_fingerprint == wt_real.semantic_plan_fingerprint
    unit = main_dry["work_units"][0]
    configure = unit["commands"][0]["argv"]
    prefix = "" if isolation == "isolated" else "src/syn_demo_0/"
    assert unit["sources"] == [
        prefix + name
        for name in ("rtl/include/defs.svh", "rtl/pkg.sv", "rtl/dut.sv", "tb/tb_dut.sv")
    ]
    assert unit["constraints"] == [f"{prefix}constraints/dut.sdc"]
    assert _operands(configure, "--extra-rtl") == [f"{prefix}rtl/pkg.sv", f"{prefix}rtl/dut.sv"]
    assert _operands(configure, "--inc-dir") == [f"{prefix}rtl/include"]


_ABC_SCRIPT = '[flows.synth.advanced_settings_yosys]\nabc_script = "+strash;map"\n'
_ABC_RECIPE = '[flows.synth.advanced_settings_yosys]\nabc_recipe = "fast"\n'


def _recipe_argv_cases() -> list[Any]:
    return [
        pytest.param(
            "",
            [],
            [
                ("--ppa-profile", "balanced"),
                ("--frontend", "sv2v"),
                ("--flatten",),
                ("-w", "lite"),
            ],
            ["--no-flatten", "--timing-report-paths"],
            id="defaults",
        ),
        pytest.param(
            'ppa_profile = "compact"\nsynth_mode = "physical"\n'
            "[flows.synth.advanced_settings_yosys]\nabc_delay_ps = 3333\n"
            "[flows.synth.advanced_settings_openroad]\n"
            "placement_density = 0.72\nrepair_hold = true\n",
            [],
            [
                ("--synth-mode", "physical"),
                ("--ppa-profile", "compact"),
                ("--abc-delay-ps", "3333"),
                ("--placement-density", "0.72"),
                ("--repair-hold",),
            ],
            [],
            id="profile-and-backend-settings",
        ),
        pytest.param(
            'ppa_profile = "compact"\n' + _ABC_RECIPE,
            ["--ppa-profile", "balanced"],
            [("--ppa-profile", "balanced")],
            ["--abc-recipe"],
            id="cli-profile-resets-backend-settings",
        ),
        pytest.param(
            _ABC_SCRIPT,
            ["--abc-recipe", "fast"],
            [("--abc-recipe", "fast")],
            ["--abc-script"],
            id="cli-abc-recipe-replaces-script",
        ),
        pytest.param(
            _ABC_RECIPE,
            ["--abc-script", "+strash;map"],
            [("--abc-script", "+strash;map")],
            ["--abc-recipe"],
            id="cli-abc-script-replaces-recipe",
        ),
        pytest.param(
            "flatten = true\nsdc = true\n",
            [],
            [("--flatten",)],
            ["--sdc", "--no-flatten"],
            id="flatten-true-and-retired-sdc-flag",
        ),
        pytest.param(
            "flatten = true\n",
            ["--no-flatten"],
            [("--no-flatten",)],
            ["--flatten"],
            id="cli-no-flatten-beats-config",
        ),
        pytest.param(
            "flatten = false\n", [], [("--no-flatten",)], ["--flatten"], id="flatten-false"
        ),
        pytest.param(
            "", ["--frontend", "slang"], [("--frontend", "slang")], [], id="cli-frontend"
        ),
        pytest.param(
            'frontend = "slang"\nslang_options = ["--single-unit", "--allow-use-before-declare"]\n',
            [],
            [
                ("--frontend", "slang"),
                ("--slang-option=--single-unit",),
                ("--slang-option=--allow-use-before-declare",),
            ],
            [],
            id="target-frontend-and-slang-options",
        ),
    ]


def test_configure_argv_carries_the_recipe(synth_cli: SynthCli) -> None:
    for case in _recipe_argv_cases():
        config, args, present, absent = case.values
        synth_cli.config(config)

        unit = synth_cli.plan(*args)["work_units"][0]
        argv = unit["commands"][0]["argv"]

        for tokens in present:
            assert _has_tokens(argv, *tokens), (case.id, tokens, argv)
        for token in absent:
            assert token not in argv, (case.id, token, argv)
        parsed = configure.parse_configure_argv(argv)
        _profile, yosys, openroad = configure.resolve_ppa_settings(parsed)
        expected = {"yosys": dataclasses.asdict(yosys)}
        if SynthMode(parsed.synth_mode).runs_openroad:
            expected["openroad"] = dataclasses.asdict(openroad)
        assert unit["recipe"]["effective_ppa_settings"] == expected, case.id


@pytest.mark.parametrize(
    ("config", "message"),
    [
        pytest.param("synth_mode = true\n", "synth_mode", id="bool-synth-mode"),
        pytest.param('synth_mode = "placed"\n', "must be one of", id="unknown-synth-mode"),
        pytest.param(
            'timing_engine = "opensta"\n', "timing_engine is retired", id="timing-engine"
        ),
        pytest.param(
            "[flows.synth.yosys]\n", r"yosys is retired.*advanced_settings_yosys", id="yosys-table"
        ),
        pytest.param(
            "[flows.synth.openroad]\n",
            r"openroad is retired.*advanced_settings_openroad",
            id="openroad-table",
        ),
        pytest.param(
            'synth_mode = "logical"\n'
            "[flows.synth.advanced_settings_openroad]\nplacement_density = 0.72\n",
            r"cannot be set.*logical",
            id="openroad-settings-in-logical-mode",
        ),
        pytest.param('flatten = "yes"\n', "flatten", id="non-bool-flatten"),
        pytest.param('ppa_profile = "speed"\n', "ppa_profile", id="unknown-ppa-profile"),
        pytest.param(
            'synth_mode = "physical"\n'
            '[flows.synth.advanced_settings_openroad]\nrepair_hold = "yes"\n',
            "repair_hold",
            id="non-bool-repair-hold",
        ),
        pytest.param(
            '[flows.synth.advanced_settings_yosys]\nabc_recipe = "fast"\nabc_script = "+strash"\n',
            "cannot set both",
            id="both-abc-controls",
        ),
        pytest.param(
            "[flows.synth.advanced_settings_yosys]\nabc_delay_ps = 3333.5\n",
            "positive integer",
            id="fractional-abc-delay",
        ),
        pytest.param(
            '[flows.synth.advanced_settings_yosys]\nabc_recip = "fast"\n',
            "unknown setting",
            id="unknown-yosys-setting",
        ),
        pytest.param(
            'synth_mode = "physical"\n[flows.synth.advanced_settings_openroad]\n'
            "repair_setp = false\n",
            "unknown setting",
            id="unknown-openroad-setting",
        ),
        pytest.param(
            'synth_mode = "physical"\n[flows.synth.advanced_settings_openroad]\nmax_fanout = 50\n',
            "unknown setting",
            id="retired-openroad-setting",
        ),
        pytest.param('frontend = "verilator"\n', r"frontend must be one of", id="frontend"),
        pytest.param(
            'slang_options = "--single-unit"\n',
            r"slang_options must be a non-empty list",
            id="slang-options",
        ),
        pytest.param(
            "timing_report_paths = 0\n", r"^synth: .*timing_report_paths", id="report-paths"
        ),
        pytest.param('[flows.synth.timing]\nengine = "sta"\n', "'engine' is retired", id="key"),
        pytest.param(
            'synth_mode = "physical"\n[flows.synth.advanced_settings_openroad]\nutilization_pct = 150\n',
            r"candidate lite: utilization_pct must be between 0 and 100",
            id="ppa",
        ),
    ],
)
def test_invalid_recipe_fails_planning(synth_cli: SynthCli, config: str, message: str) -> None:
    synth_cli.config(config)

    for outcome in (synth_cli.run("--dry-run"), synth_cli.run()):
        assert outcome.exit_code == EXIT_ERROR
        assert re.search(message, outcome.report_text), outcome.report_text
    assert list((synth_cli.project / _SYNTH_ROOT).parent.glob(".plan-*")) == []


def test_report_size_reaches_the_backend_but_not_the_recipe(synth_cli: SynthCli) -> None:
    default_recipe = synth_cli.plan()["work_units"][0]["recipe"]
    synth_cli.config("timing_report_paths = 3\n")
    synth_cli.resolution.resolve = _resolving(_fake_synth_resolved)

    assert synth_cli.plan()["work_units"][0]["recipe"] == default_recipe
    assert synth_cli.run().exit_code == EXIT_SUCCESS
    argv = synth_cli.configured["lite"]
    assert _operands(argv, "--timing-report-paths") == ["3"]
    spec = configure.resolve_spec(
        configure.parse_configure_argv(argv), project_root=synth_cli.project, require_liberty=False
    )
    assert spec.timing.report_paths == 3
    # The standalone configure CLI resolves timing without a Project root (it
    # reads the working directory's config); no public name reaches that step
    # without also discovering the root.
    args = configure.parse_configure_argv(argv)
    _profile, _yosys, openroad = configure.resolve_ppa_settings(args)
    assert configure._resolve_syn_timing(args, openroad).report_paths == 3


def _physical_target_without_usable_sdc(case: str, project: Path):
    """A physical Target whose SDC is unusable in way *case*."""
    resolved = _with_synth_mode(_fake_synth_resolved(project), "physical")
    if case == "outside":
        outside = project.parent / "outside.sdc"
        outside.write_text("create_clock -period 4 [get_ports clk]\n", encoding="utf-8")
        return dataclasses.replace(
            resolved, files=(fusesoc_registry.ResolvedFile(name=str(outside), file_type="SDC"),)
        )
    if case == "missing":
        resolved.sdc_files[0].absolute(resolved.build_root).unlink()
    if case == "none":
        return dataclasses.replace(
            resolved, files=tuple(f for f in resolved.files if f.file_type != "SDC")
        )
    return resolved


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("outside", "escapes the selected checkout"),
        ("missing", "SDC file not found"),
        ("unreadable", "SDC file is not readable"),
        ("none", "no timing constraints"),
    ],
)
def test_unusable_sdc_fails_physical_planning(
    case: str,
    message: str,
    synth_cli: SynthCli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    synth_cli.project = project
    resolved = _physical_target_without_usable_sdc(case, project)
    synth_cli.resolution.resolve = lambda *_a, **_k: resolved
    if case == "unreadable":
        sdc = resolved.sdc_files[0].absolute(resolved.build_root)
        read_bytes = Path.read_bytes

        def read_bytes_or_deny(path: Path) -> bytes:
            if path == sdc:
                raise OSError("permission denied")
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", read_bytes_or_deny)

    outcome = synth_cli.run("--dry-run")

    assert outcome.exit_code == EXIT_ERROR
    assert message in outcome.report_text, outcome.report_text


def _linked_booley_project(tmp_path: Path) -> tuple[Path, Callable[[Path], object]]:
    """A Project whose ``.booley`` is a link, plus a resolver factory for one SDC path."""
    root = tmp_path / "project"
    data = tmp_path / "booley-data"
    elsewhere = tmp_path / "elsewhere"
    for directory in (root, data / "cores", elsewhere):
        directory.mkdir(parents=True)
    symlink_or_skip(root / ".booley", data)
    symlink_or_skip(data / "linked", elsewhere)
    for sdc in (data / "cores" / "top.sdc", elsewhere / "top.sdc"):
        sdc.write_text("create_clock -period 4 [get_ports clk]\n", encoding="utf-8")

    def resolver(sdc: Path):
        resolved = dataclasses.replace(
            _with_synth_mode(_fake_synth_resolved(root), "physical"),
            files=(fusesoc_registry.ResolvedFile(name=str(sdc), file_type="SDC"),),
        )
        return lambda *_a, **_k: resolved

    return root, resolver


def test_sta_sdc_follows_symlinked_booley_dir_entry(synth_cli: SynthCli, tmp_path: Path) -> None:
    """A stealth core's SDC under a linked entry is named through the entry."""
    root, resolver = _linked_booley_project(tmp_path)
    synth_cli.project = root

    synth_cli.resolution.resolve = resolver(root / ".booley" / "cores" / "top.sdc")
    synth_cli.run()
    assert _operands(synth_cli.configured["lite"], "--sta-sdc") == [".booley/cores/top.sdc"]

    # A link below the entry still leaves the selected checkout.
    synth_cli.resolution.resolve = resolver(root / ".booley" / "linked" / "top.sdc")
    outcome = synth_cli.run("--dry-run")
    assert outcome.exit_code == EXIT_ERROR
    assert "escapes the selected checkout" in outcome.report_text


def test_paths_resolve_from_a_worktree_behind_a_linked_ancestor(
    synth_cli: SynthCli, tmp_path: Path
) -> None:
    """A Codespace worktree sits under the symlinked ``.booley`` entry.

    ``/workspaces/picorv32/.booley/worktrees/zbb`` is physically
    ``/workspaces/booley-prj-picorv32/worktrees/zbb``. Resolved source
    paths are physical, so a relative path taken against the linked
    spelling climbed out of the checkout (``../../../../booley-prj-...``)
    and synthesis reported "Extra RTL file not found".
    """
    physical = tmp_path / "workspaces" / "booley-prj" / "worktrees" / "zbb"
    physical.mkdir(parents=True)
    project = tmp_path / "workspaces" / "picorv32"
    project.mkdir()
    symlink_or_skip(project / ".booley", physical.parent.parent)
    work_dir = project / ".booley" / "worktrees" / "zbb"
    resolved = _with_synth_mode(_fake_synth_resolved(work_dir), "physical")
    synth_cli.project = work_dir
    # The work root is wiped before each resolution, so the files are staged per call.
    synth_cli.resolution.resolve = lambda *_a, **_k: _staged(resolved)

    synth_cli.run_request()

    # Every path is inside the checkout and names the file the Target means,
    # as seen from the physical working directory the tools run in.
    argv = synth_cli.configured["lite"]
    expected = {item.absolute(resolved.build_root) for item in resolved.files}
    for flag in ("--extra-rtl", "--inc-dir", "--sta-sdc"):
        values = _operands(argv, flag)
        assert values, flag
        for value in values:
            assert not value.startswith("../"), (flag, value)
            target = (work_dir.resolve() / value).resolve()
            assert target in expected or any(p.parent == target for p in expected), value


def test_configure_argv_names_target_files_inside_the_checkout(
    synth_cli: SynthCli, tmp_path: Path
) -> None:
    """Sources, include dirs and SDC files are checkout-relative; headers and tb files are not sources."""

    def make(root: Path) -> fusesoc_registry.ResolvedTarget:
        base = _fake_synth_resolved(root)
        return _with_synth_mode(
            dataclasses.replace(
                base,
                files=(
                    *base.files,
                    fusesoc_registry.ResolvedFile(
                        name="src/syn_demo_0/sdc/core.sdc", file_type="SDC"
                    ),
                    fusesoc_registry.ResolvedFile(
                        name="src/syn_demo_0/tb/tb.sdc", file_type="SDC", tags=("tb",)
                    ),
                ),
            ),
            "physical",
        )

    synth_cli.resolution.resolve = _resolving(make)

    synth_cli.run()

    argv = synth_cli.configured["lite"]
    assert "-c" not in argv
    assert _operands(argv, "-t") == ["dut"]
    assert _operands(argv, "--extra-rtl") == [
        f"{_STAGED_SRC}/rtl/pkg.sv",
        f"{_STAGED_SRC}/rtl/dut.sv",
    ]
    assert _operands(argv, "--inc-dir") == [f"{_STAGED_SRC}/rtl/include"]
    assert _operands(argv, "--sta-sdc") == [
        f"{_STAGED_SRC}/constraints/dut.sdc",
        f"{_STAGED_SRC}/sdc/core.sdc",
    ]


def test_typed_parameters_reach_the_configure_argv(synth_cli: SynthCli, tmp_path: Path) -> None:
    parameters = {
        "BARE": {"paramtype": "vlogdefine", "default": True},
        "VALUED": {"paramtype": "vlogdefine", "default": 16},
        "OFF": {"paramtype": "vlogdefine", "default": False},
        "UNSET": {"paramtype": "vlogdefine"},
        "WIDTH": {"paramtype": "vlogparam", "default": 8},
        "ENABLED": {"datatype": "bool", "paramtype": "vlogparam", "default": True},
        "DISABLED": {"datatype": "bool", "paramtype": "vlogparam", "default": False},
        "ARG": {"paramtype": "plusarg", "default": "y"},
    }
    resolved = dataclasses.replace(_fake_synth_resolved(tmp_path), parameters=parameters)
    synth_cli.resolution.resolve = lambda *_a, **_k: resolved

    argv = synth_cli.plan()["work_units"][0]["commands"][0]["argv"]

    assert _operands(argv, "-d") == ["BARE", "VALUED=16"]
    assert _operands(argv, "-p") == ["WIDTH=8", "ENABLED=1", "DISABLED=0"]


@pytest.mark.parametrize(
    ("top", "defines", "expected"),
    [
        ("core_tb", {}, ["testbench"]),
        ("MY_TB", {}, ["testbench"]),
        ("u_tb_ctrl", {"SIMULATION_SPEED": 2}, []),
        ("dut", {"SIMULATION": True}, ["SIMULATION"]),
        ("dut", {"SIMULATION": 1}, ["SIMULATION"]),
        ("foo_tb", {"SIMULATION": True}, ["testbench", "SIMULATION"]),
    ],
)
def test_simulation_shaped_target_warns(
    synth_cli: SynthCli,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    top: str,
    defines: dict[str, object],
    expected: list[str],
) -> None:
    resolved = dataclasses.replace(
        _fake_synth_resolved(tmp_path, toplevel=top),
        parameters={
            name: {"paramtype": "vlogdefine", "default": value} for name, value in defines.items()
        },
    )
    synth_cli.resolution.resolve = lambda *_a, **_k: resolved

    with caplog.at_level(logging.WARNING):
        synth_cli.plan()

    warnings = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Synth lite: ")
    ]
    assert len(warnings) == len(expected), warnings
    for warning, fragment in zip(warnings, expected, strict=True):
        assert fragment in warning


@pytest.mark.parametrize(
    ("config", "args", "timeout_ms"),
    [
        ("", [], 1800000),
        ("timeout_ms = 5400000\n", [], 5400000),
        ("timeout_ms = 5400000\n", ["--timeout-ms", "900000"], 900000),
    ],
    ids=["default", "config", "cli-over-config"],
)
def test_timeout_precedence(
    synth_cli: SynthCli, config: str, args: list[str], timeout_ms: int
) -> None:
    synth_cli.config(config)

    assert synth_cli.plan(*args)["work_units"][0]["timeout_ms"] == timeout_ms


def test_resolution_gets_a_clean_isolated_build_root(synth_cli: SynthCli, tmp_path: Path) -> None:
    """The build root is wiped before resolution, so FuseSoC re-stages current RTL."""
    stale = work_root_for(tmp_path, "synth", "lite") / "core_0" / "lite" / "src" / "old.sv"
    stale.parent.mkdir(parents=True)
    stale.write_text("module old; endmodule\n", encoding="utf-8")
    seen: list[tuple[str, Path, str, str | None, bool]] = []

    def resolve(handle, *, build_root, **_kwargs):
        seen.append(
            (
                handle.selector,
                Path(handle.project_root).resolve(),
                Path(build_root).relative_to(handle.project_root).as_posix(),
                handle.vlnv,
                stale.exists(),
            )
        )
        return _fake_synth_resolved(tmp_path)

    synth_cli.resolution.resolve = resolve

    assert synth_cli.run().exit_code == EXIT_SUCCESS

    assert seen == [("lite", tmp_path.resolve(), _SYNTH_ROOT, "::test:0", False)]


def test_resolution_failure_is_an_infrastructure_error(synth_cli: SynthCli) -> None:
    def fail(*_args, **_kwargs):
        raise fusesoc_registry.TargetResolutionError("no such target")

    synth_cli.resolution.resolve = fail

    outcome = synth_cli.run()

    assert outcome.exit_code == EXIT_ERROR
    assert "infrastructure error" in outcome.report_text
    assert "no such target" in outcome.report_text


@pytest.mark.skipif(os.name != "posix", reason="the short root is the Linux Sandbox's /tmp")
@pytest.mark.parametrize(
    ("result", "physical"),
    [
        pytest.param(_PASS_OUTPUT, False, id="success"),
        pytest.param(SubprocessResult(returncode=2), False, id="failure"),
        pytest.param(SubprocessResult(returncode=-1, timed_out=True), False, id="timeout"),
        pytest.param(_PASS_OUTPUT, True, id="physical"),
    ],
)
def test_sandbox_make_gives_yosys_a_short_tmpdir_removed_after_the_run(
    synth_cli: SynthCli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: SubprocessResult,
    physical: bool,
) -> None:
    """Yosys's ABC hangs under a TMPDIR over 35 characters (#4730): it gets its own."""
    long_tmp = tmp_path / ("j" * 80)
    long_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(long_tmp))
    monkeypatch.setattr("tempfile.tempdir", str(long_tmp))
    live: list[list[str]] = []

    def record(cmd: list[str]) -> SubprocessResult:
        live.append(
            [
                arg
                for arg in cmd
                if arg.startswith("BOOLEY_YOSYS_TMPDIR=") and Path(arg.partition("=")[2]).is_dir()
            ]
        )
        return result

    synth_cli.tool_output = record
    if physical:
        synth_cli.resolution.resolve = lambda *_a, **_k: _with_synth_mode(
            _fake_synth_resolved(synth_cli.project), "physical"
        )
    synth_cli.run()

    assert synth_cli.commands[0][:3] == ["make", "-C", f"{_SYNTH_ROOT}/synth"]
    carried = [args for args in live if args]
    assert len(carried) == 1, "no make command gave Yosys its own TMPDIR"
    directory = Path(carried[0][0].partition("=")[2])
    assert directory.parent == Path("/tmp")
    assert len(str(directory)) <= 35
    assert not directory.exists()


def test_compatibility_argv_matches_the_planned_configure_argv(synth_cli: SynthCli) -> None:
    synth_cli.run()
    assert synth_cli.flow is not None

    # `_build_synth_cmd` is the compatibility argv tests/golden and
    # tests/flows/test_string_parameter_eda.py still call; no public path reaches it.
    assert synth_cli.flow._build_synth_cmd("lite") == synth_cli.configured["lite"]


def test_synth_label_carries_target_source_bytes(synth_cli: SynthCli) -> None:
    from booley.flows import job_resources

    labels: list[job_resources.ResourceLabel | None] = []

    def record_label(_cmd: list[str]) -> SubprocessResult:
        labels.append(job_resources.current_label())
        return _PASS_OUTPUT

    resolved = _fake_synth_resolved(synth_cli.project)
    synth_cli.tool_output = record_label
    synth_cli.resolution.resolve = lambda *_a, **_k: _staged(resolved, b"0123456789")

    assert synth_cli.run().exit_code == EXIT_SUCCESS

    assert labels == [
        job_resources.ResourceLabel(
            "lite", "synth", split=job_resources.SYNTH_PHASES, source_bytes=50
        )
    ]


def test_real_fusesoc_synth_setup(synth_cli: SynthCli, tmp_path: Path) -> None:
    """End-to-end: a real `fusesoc run --setup` leaves a resolved synth EDAM.

    Proves the RTL sources/top/typed-params resolve, the build dir is
    relocatable, and asic_synthesize feeds the resolved filelist + include
    dir + defines to the run_yosys_syn configure surface.
    """
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    work_dir = tmp_path / "proj"
    _write_syn_demo_project(work_dir)
    synth_cli.project = work_dir
    synth_cli.resolution.resolve = _real_resolve_handle

    synth_cli.run(target="syn")

    cmd = synth_cli.configured["syn"]
    # Configuration over the resolved sources (no legacy config selector).
    assert cmd[:4] == ["python3", "-m", "booley.flows.synth.backends.configure", "configure"]
    assert "-c" not in cmd
    assert _operands(cmd, "-t") == ["dut"]
    # Resolved RTL sources are forwarded as relative (sandbox-safe) paths.
    extra = _operands(cmd, "--extra-rtl")
    assert any(e.endswith("rtl/dut.sv") for e in extra)
    assert any(e.endswith("rtl/pkg.sv") for e in extra)
    assert not any("defs.svh" in e for e in extra)
    assert not any(Path(e).is_absolute() for e in extra)
    # The include header surfaces as an include dir, not a source.
    assert any(d.endswith("rtl/include") for d in _operands(cmd, "--inc-dir"))
    # The file_type:SDC fileset is forwarded as --sta-sdc (ADR 0029), sandbox-
    # safe relative path (ADR 0031: a no-SDC target would have hard-errored).
    sta = _operands(cmd, "--sta-sdc")
    assert any(s.endswith("constraints/dut.sdc") for s in sta)
    assert not any(Path(s).is_absolute() for s in sta)
    # Typed params reached the CLI.
    assert _operands(cmd, "-d") == ["SYNTHESIS"]
    assert _operands(cmd, "-p") == ["WIDTH=8"]
    # Resolved build dir lives under the worktree and is relocatable.
    edam = next((work_dir / ".booley" / ".runtime").rglob("*.eda.yml"))
    assert str(work_dir) not in edam.read_text(encoding="utf-8")


# How sv2v and the slang frontend are handed the staged copy of the syn_demo `rtl/` directory.
_RTL_COPY = "../syn_demo_0/syn/src/syn_demo_0/rtl"


@pytest.mark.parametrize(
    ("log", "error", "failure", "provenance"),
    [
        pytest.param(
            "sv2v",
            f"cannot read {_RTL_COPY}/dut.sv: invalid byte\n{_RTL_COPY}/gen.sv:1: note",
            ("sv2v: cannot read rtl/dut.sv: invalid byte\n", f"{_RTL_COPY}/gen.sv:1: note"),
            (),
            id="sv2v",
        ),
        pytest.param(
            "yosys",
            f"{_RTL_COPY}/dut.sv:2:3: error: expected expression",
            ("rtl/dut.sv:2:3: error: expected expression",),
            (),
            id="slang",
        ),
        pytest.param(
            "yosys",
            "ERROR: syntax error at sv2v_converted.v:2",
            ("ERROR: syntax error at sv2v_converted.v:2",),
            ("  - rtl/pkg.sv", "  - rtl/dut.sv"),
            id="converted",
        ),
    ],
)
def test_4996_synthesis_output_names_project_paths(
    synth_cli: SynthCli,
    tmp_path: Path,
    log: str,
    error: str,
    failure: tuple[str, ...],
    provenance: tuple[str, ...],
) -> None:
    """Issue #4996: warnings, failure text and the source list name Project paths.

    The tools read FuseSoC's staged copies, so they print paths no checkout has. A
    path that is no Target source (`gen.sv`, `sv2v_converted.v`) stays as printed,
    and `run.log` keeps the tools' own text.
    """
    pytest.importorskip("fusesoc")
    work_dir = tmp_path / "proj"
    _write_syn_demo_project(work_dir)
    (work_dir / ".booley").mkdir()
    synth_cli.project = work_dir
    synth_cli.resolution.resolve = _real_resolve_handle
    warning = f"WARNING: {_RTL_COPY}/dut.sv:1:1: ignored attribute"

    def configure(flow: AsicSynthesizeFlow, target: str, cmd: list[str]) -> syn_make.SynthPlan:
        plan = _stub_plan(work_dir, target)
        sources = tuple((work_dir / name).resolve() for name in _operands(cmd, "--extra-rtl"))
        return dataclasses.replace(plan, spec=dataclasses.replace(plan.spec, sources=sources))

    def make(_cmd: list[str]) -> SubprocessResult:
        build_dir = work_dir / ".booley" / ".runtime" / "edalize" / "synth" / "syn" / "synth"
        (build_dir / "sv2v.log").write_bytes((warning + "\n").encode())
        with (build_dir / f"{log}.log").open("a", encoding="utf-8", newline="\n") as stage_log:
            stage_log.write(error + "\n")
        # Windows file times lag the dispatch clock: date the logs past it so they read as fresh.
        future = time.time() + 60
        for name in {"sv2v", log}:
            os.utime(build_dir / f"{name}.log", (future, future))
        return SubprocessResult(returncode=2, stdout=f"BOOLEY_STAGE: {log}\n")

    synth_cli.tool_output = make
    # As the autouse fixture: no test host can render the build dir. This plan also
    # carries the sources the configure argv names, which the source list prints.
    with patch.object(AsicSynthesizeFlow, "_configure_synth", configure):
        outcome = synth_cli.run(target="syn")

    report = synth_cli.report("syn")
    (group,) = report["warning_summary"]["representatives"]
    assert group["message"] == "WARNING: rtl/dut.sv:1:1: ignored attribute"
    failure_text = report["infra_error"] + "\n" + report["failure_output"]
    for fragment in failure:
        assert fragment in failure_text
    assert "syn_demo_0/rtl/dut.sv" not in failure_text + outcome.report_text
    run_log = (
        work_dir / ".booley" / ".runtime" / "edalize" / "synth" / "syn" / "run.log"
    ).read_text(encoding="utf-8")
    assert warning in run_log
    for line in provenance:
        assert line in run_log


def test_failed_fusesoc_setup_is_recorded_as_synth_prepare(
    synth_cli: SynthCli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Synthesis Flow's own setup runner measures the setup, whatever its outcome."""
    from booley.runtime import resource_history

    work_dir = tmp_path / "proj"
    _write_syn_demo_project(work_dir)
    monkeypatch.setattr(TargetCatalog, "build", _REAL_CATALOG_BUILD)
    monkeypatch.setattr(fusesoc_registry, "setup_command_for_handle", _REAL_SETUP_HANDLE)
    failing_setup = canned_command(SubprocessResult(returncode=1, stderr="setup broke"))
    synth_cli.project = work_dir
    synth_cli.resolution.resolve = lambda handle, **kwargs: _REAL_RESOLVE_HANDLE(
        handle, **{**kwargs, "fusesoc_cmd": failing_setup}
    )

    outcome = synth_cli.run(target="syn")

    assert outcome.exit_code == EXIT_ERROR
    assert "failed (exit 1)" in outcome.report_text
    assert "setup broke" in outcome.report_text
    assert synth_cli.commands == []
    (record,) = resource_history.load_records(tmp_path)
    key = record["key"]
    assert (
        record["flow"],
        key["target"],
        key["phase"],
        record["outcome"],
        record["returncode"],
    ) == ("synth", "syn", "synth_prepare", "exit_nonzero", 1)


@pytest.mark.skipif(
    shutil.which("git") is None or os.name == "nt",
    reason="the baseline worktree runs only on POSIX with git",
)
@pytest.mark.parametrize("recipe_changed", [False, True])
def test_stable_constraint_identity_preserves_selfcompare_diagnostic(
    synth_cli: SynthCli, recipe_changed: bool
) -> None:
    """An unchanged recipe over identical stealth RTL keeps the no-op self-comparison warning.

    The baseline side's SDC lives under its own checkout; were the constraint's
    path part of the recipe identity, the two sides would differ and the warning
    would vanish even though nothing changed.
    """
    project = synth_cli.project
    _author_stealth_core(project, "module dut(input logic clk); endmodule\n")
    (project / "README.md").write_text("project\n", encoding="utf-8")
    for args in (
        ("init",),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.com"),
        ("add", "README.md"),
        ("commit", "-m", "init"),
    ):
        run_bounded_child(["git", *args], cwd=project, check=True)

    def resolve(handle, **_kwargs):
        project_root = handle.project_root
        resolved = _fake_synth_resolved(Path(project_root), config=handle.selector)
        if recipe_changed and Path(project_root).resolve() != project.resolve():
            resolved = dataclasses.replace(
                resolved, flow_options={**resolved.flow_options, "flatten": False}
            )
        return resolved

    synth_cli.resolution.resolve = resolve

    outcome = synth_cli.run("--baseline", "HEAD")

    assert outcome.exit_code == EXIT_SUCCESS, outcome.report_text
    assert ("no-op self-comparison" in outcome.report_text) is not recipe_changed


def test_recipe_identity_ignores_isolated_checkout_path(tmp_path: Path) -> None:
    left_root = tmp_path / "baseline"
    right_root = tmp_path / "candidate"
    constraint = Path("constraints/dut.sdc")
    for root in (left_root, right_root):
        path = root / constraint
        path.parent.mkdir(parents=True)
        path.write_text("create_clock -period 4 [get_ports clk]\n", encoding="utf-8")

    left = _fake_synth_resolved(tmp_path / "left-build")
    right = _fake_synth_resolved(tmp_path / "right-build")
    left = dataclasses.replace(
        left,
        files=(
            fusesoc_registry.ResolvedFile(
                name=str(left_root / constraint),
                file_type="SDC",
                core="::syn_demo:0",
            ),
        ),
    )
    right = dataclasses.replace(
        right,
        files=(
            fusesoc_registry.ResolvedFile(
                name=str(right_root / constraint),
                file_type="SDC",
                core="::syn_demo:0",
            ),
        ),
    )

    left_snapshot = synthesis_recipe_snapshot(left, default_recipe_args(), target="lite")
    right_snapshot = synthesis_recipe_snapshot(right, default_recipe_args(), target="lite")

    assert left_snapshot == right_snapshot
    assert synthesis_recipe_snapshot_fingerprint(
        left_snapshot
    ) == synthesis_recipe_snapshot_fingerprint(right_snapshot)
    assert left_snapshot["flow"] == "synth"
    assert left_snapshot["constraints"] == [
        {
            "core": "::syn_demo:0",
            "sha256": hashlib.sha256((left_root / constraint).read_bytes()).hexdigest(),
        }
    ]

    (right_root / constraint).write_text(
        "create_clock -period 5 [get_ports clk]\n", encoding="utf-8"
    )
    changed_snapshot = synthesis_recipe_snapshot(right, default_recipe_args(), target="lite")
    assert synthesis_recipe_snapshot_fingerprint(
        changed_snapshot
    ) != synthesis_recipe_snapshot_fingerprint(left_snapshot)


def test_recipe_schema_bumps_without_default_clock(tmp_path: Path):
    for mode, schema in [("logical", 4), ("physical", 5)]:
        snapshot = synthesis_recipe_snapshot(
            dataclasses.replace(_fake_synth_resolved(tmp_path), flow_options={"synth_mode": mode}),
            default_recipe_args(),
            target="lite",
        )
        assert snapshot["schema"] == schema
        assert "default_clock_ps" not in snapshot
        assert "ppa_profile_settings" not in snapshot

        previous = {**snapshot, "schema": 3}
        assert synthesis_recipe_snapshot_fingerprint(previous) != (
            synthesis_recipe_snapshot_fingerprint(snapshot)
        )
        assert synthesis_recipe_changes(previous, snapshot) == [
            {"path": "schema", "before": 3, "after": schema}
        ]


def test_physical_fanout_policy_invalidates_old_recipe(tmp_path):
    snapshot = synthesis_recipe_snapshot(
        dataclasses.replace(
            _fake_synth_resolved(tmp_path), flow_options={"synth_mode": "physical"}
        ),
        default_recipe_args(),
        target="lite",
    )
    previous = {
        **snapshot,
        "technology": {**snapshot["technology"], "physical_fanout_policy": "native-point-v1"},
    }
    assert synthesis_recipe_snapshot_fingerprint(previous) != (
        synthesis_recipe_snapshot_fingerprint(snapshot)
    )


def test_config_error_surfaces_as_infra_error(synth_cli: SynthCli) -> None:
    """The run path reports a config error like a resolution failure
    (returncode-2 infra error), not an unhandled crash of the Flow.

    A CLI run rejects a bad recipe while planning, so only a Target resolved at
    run time without a plan reaches the ``BoundaryError`` branch of
    ``AsicSynthesizeFlow._synthesize_target``, which only ``_run_single_config``
    drives without one.
    """
    synth_cli.config("synth_mode = true\n")
    flow = AsicSynthesizeFlow()
    flow.parse_args(["--target", "lite", "--work-dir", str(synth_cli.project)])

    metrics, output = flow._run_single_config("lite")

    assert metrics.returncode == 2
    assert "synth_mode" in (metrics.infra_error or "")
    assert "synth_mode" in output


def test_empty_selection_is_an_error(tmp_path: Path) -> None:
    """Through the CLI an empty ``--target`` turns into automatic Target selection
    (and the lenient test catalog has no ``list``), so only ``_run`` reaches the
    ``no Target selected`` refusal in ``ImplementationFlow._run``."""
    flow = AsicSynthesizeFlow()
    flow.parse_args(["--target", "", "--work-dir", str(tmp_path)])

    assert flow._run().exit_code == EXIT_ERROR


def _perclock_marker(name: str, period_ns, wns_ns, whs_ns, same_clock: bool = True) -> str:
    """Render one canonical STA_PERCLOCK marker line for tests.

    ``None`` is emitted as the ``NA`` sentinel the parser maps back to None.
    """

    def _tok(v):
        return "NA" if v is None else f"{v:.6f}"

    return (
        f"STA_PERCLOCK: name={name} period_ns={period_ns:.6f} "
        f"wns_ns={_tok(wns_ns)} whs_ns={_tok(whs_ns)} "
        f"setup_same_clock={int(same_clock)}\n"
    )


# ===========================================================================
# Yosys stat grammar and tool output (public parser and the Flow's own run)
# ===========================================================================

_TOKEN = "0123456789abcdef0123456789abcdef"
_CLEAN_CHECK = "Found and reported 0 problems.\n"


def _yosys_outputs(stat: str) -> dict[str, str]:
    """The files a finished Yosys stage leaves: the stat twice, clean checks, the netlists."""
    return {
        "stat_dut.txt": stat,
        "yosys.log": stat,
        "premap_check_dut.txt": _CLEAN_CHECK,
        "check_dut.txt": _CLEAN_CHECK,
        "synth_dut.v": "module dut; endmodule\n",
        "sv2v_converted.v": "module dut; endmodule\n",
    }


def _synthesize(
    synth_cli: SynthCli,
    files: dict[str, str] | None = None,
    *,
    mode: str = "logical",
    toml: str = "",
    stale: Collection[str] = (),
    plan: dict[str, Any] | None = None,
    **process: Any,
) -> tuple[EndpointOutcome, dict[str, Any]]:
    """Run the Flow once; the fake command leaves *files* under the build dir.

    Names in *stale* get an mtime older than the dispatch. *process* shapes the fake
    command's result. A *plan* dict runs under a configured plan with an attempt token
    and those spec fields.
    """
    synth_cli.config(f'synth_mode = "{mode}"\n{toml}')
    build = synth_cli.project / _SYNTH_ROOT / "synth"
    process.setdefault("returncode", 0)
    process.setdefault("stdout", "")
    process.setdefault("stderr", "")

    def tool_output(_argv: list[str]) -> SubprocessResult:
        fresh = time.time() + 1
        for name, text in (files or {}).items():
            path = build / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
            stamp = 0 if name in stale else fresh
            os.utime(path, (stamp, stamp))
        return SubprocessResult(**process)

    synth_cli.tool_output = tool_output
    if plan is None:
        return synth_cli.run(), synth_cli.report()
    stub = _stub_plan(synth_cli.project, "lite", mode=mode)
    full = dataclasses.replace(
        stub, attempt_token=_TOKEN, spec=dataclasses.replace(stub.spec, **plan)
    )
    # The attempt token is set only by the configure half, which renders real scripts
    # for EDA tools no test host has.
    with patch.object(AsicSynthesizeFlow, "_configure_synth", return_value=full):
        return synth_cli.run(), synth_cli.report()


def _report_view(outcome: EndpointOutcome, report: dict[str, Any]) -> dict[str, Any]:
    """The top-level report with its metrics, conditions, grade and exit code on one level."""
    implementation = report["implementation"]
    return {
        **report,
        **implementation["metrics"],
        **implementation["conditions"],
        "grade": implementation["status"]["grade"],
        "failure_reasons": implementation["status"]["failure_reasons"],
        "exit_code": outcome.exit_code,
    }


def _stat_view(text: str) -> dict[str, Any]:
    stat = parse_stat(text)
    return {
        "area": stat.area_um2,
        "cells": stat.cells,
        "wires": stat.wires,
        "processes": stat.processes,
        # None: no usable stat section, so the Flow counts `$dlatch` mentions instead.
        "latches": sum(stat.latch_types.values()) if stat.section is not None else None,
        "latch_types": stat.latch_types,
    }


_CAPTURED_YOSYS_068_STAT = (
    "3. Printing statistics.\n\n=== latch ===\n\n"
    "        +----------Local Count, excluding submodules.\n        | \n"
    "        5 wires\n        5 wire bits\n        5 public wires\n"
    "        5 public wire bits\n        4 ports\n        4 port bits\n"
    "        2 cells\n        1   $_DFF_P_\n        1   $_DLATCH_P_\n"
)
_CAPTURED_LIBERTY_STAT = (
    "4. Printing statistics.\n\n=== latch ===\n\n"
    "        +----------Local Count, excluding submodules.\n"
    "        |        +-Local Area, excluding submodules.\n        |        | \n"
    "        5        - wires\n        5        - wire bits\n"
    "        5        - public wires\n        5        - public wire bits\n"
    "        4        - ports\n        4        - port bits\n        2        2 cells\n"
    "        1        2   $_DFF_P_\n        1        -   $_DLATCH_P_\n\n"
    "   Area for cell type $_DLATCH_P_ is unknown!\n\n"
    "   Chip area for module '\\latch': 2.000000\n"
    "     of which used for sequential elements: 0.000000 (0.00%)\n"
)
_CAPTURED_HIERARCHY_STAT = (
    "3. Printing statistics.\n\n=== top ===\n\n"
    "        +----------Local Count, excluding submodules.\n        | \n"
    "        3 wires\n        4 wire bits\n        3 public wires\n"
    "        4 public wire bits\n        3 ports\n        4 port bits\n"
    "        2 submodules\n        2   child\n\n=== child ===\n\n"
    "        +----------Local Count, excluding submodules.\n        | \n"
    "        3 wires\n        3 wire bits\n        3 public wires\n"
    "        3 public wire bits\n        3 ports\n        3 port bits\n"
    "        1 cells\n        1   $_DLATCH_P_\n\n=== design hierarchy ===\n\n"
    "        +----------Count including submodules.\n        | \n"
    "        2 top\n        1 child\n\n"
    "        +----------Count including submodules.\n        | \n"
    "        9 wires\n       10 wire bits\n        9 public wires\n"
    "       10 public wire bits\n        9 ports\n       10 port bits\n"
    "        - memories\n        - memory bits\n        - processes\n"
    "        2 cells\n        2   $_DLATCH_P_\n        2 submodules\n        2   child\n"
)
_STAT_STAGE = "Printing statistics.\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "Chip area for top module '\\x': 12345\n", {"area": 12345.0}, id="integer-area"
        ),
        pytest.param(
            "Number of cells: 12345\nChip area for top module '\\m': 1000.0\n",
            {"area": 1000.0, "cells": 12345},
            id="cell-count",
        ),
        pytest.param("", {"area": None, "cells": None, "wires": 0, "processes": 0}, id="empty"),
        # #1078: a complete but malformed token leaves the area unknown.
        pytest.param("Chip area for top module dut: e+-\n", {"area": None}, id="token-e+-"),
        pytest.param("Number of wires:   450\n", {"wires": 450}, id="wires"),
        pytest.param("no wire info", {"wires": 0}, id="no-wires"),
        pytest.param("Number of processes:      3\n", {"processes": 3}, id="processes"),
        pytest.param("synthesis completed", {"processes": 0}, id="no-processes"),
        pytest.param(
            "3. Printing statistics.\n=== child ===\n  1 cells\n  1 $_DLATCH_P_\n"
            "=== design hierarchy ===\n  9 wires\n  - processes\n  2 cells\n  2 $_DLATCH_P_\n",
            {"latches": 2, "wires": 9, "processes": 0},
            id="hierarchy-inventory-is-aggregate-only",
        ),
        pytest.param(
            "1. Printing statistics.\n=== dut ===\n  Number of cells: 8\n"
            "  $_DLATCH_P_ 8\n  Chip area for top module dut: 10\n"
            "2. Printing statistics.\n=== dut ===\n  5 - wires\n  0 - processes\n"
            "  2 - cells\n  2 1e1 NAND2\n  Chip area for top module dut: 2e1\n"
            "3. Printing statistics.\n=== dut ===\n",
            {"latches": 0, "area": 20.0, "cells": 2, "wires": 5, "processes": 0},
            id="latest-modern-zero-overrides-legacy-and-truncated-tail",
        ),
        pytest.param(
            "Printing statistics.\n  9 cells\n  2 - $adlatch\n  3 - $_DLATCHSR_PPP_\n"
            "  2 - $_SR_PP_\n  2 - DLH_X1\n",
            {"latches": 5},
            id="variants-exclude-sr-and-mapped-names",
        ),
        pytest.param(
            _CAPTURED_YOSYS_068_STAT,
            {"area": None, "cells": 2, "wires": 5, "latches": 1, "processes": 0},
            id="captured-logical",
        ),
        pytest.param(
            _CAPTURED_LIBERTY_STAT,
            {"area": 2.0, "cells": 2, "wires": 5, "latches": 1, "processes": 0},
            id="captured-liberty",
        ),
        pytest.param(
            _CAPTURED_HIERARCHY_STAT,
            {"area": None, "cells": 2, "wires": 9, "latches": 2, "processes": 0},
            id="captured-hierarchy",
        ),
        pytest.param(
            "    \\DLH_X1 _DLATCH_P_ (.D( D), .G( E), .Q( Q));\n"
            "    \\DLL_X1 _DLATCH_N_ (.D( D), .GN( E), .Q( Q));\n"
            "Printing statistics.\n=== top ===\n  2 cells\n  2 child\n"
            "=== child ===\n  1 cells\n  1 2.66 DLH_X1\n"
            "=== design hierarchy ===\n  2 cells\n  2 5.32 DLH_X1\n"
            "End of script.\n--- openroad.log ---\n  99 2.66 DLL_X1\n",
            {"latches": 2},
            id="mapped-latches-count-hierarchy-once-and-ignore-unused-mapping",
        ),
        pytest.param(
            "Printing statistics.\n=== top ===\n  2 cells\n  1 DLH_X1\n  1 latch_buffer\n",
            {"latches": 0},
            id="latch-like-library-names-without-mapping",
        ),
        pytest.param(
            "Printing statistics.\n=== top ===\n  Number of cells: 2\n"
            "  Chip area for top module top: 20\n"
            "=== child ===\n  Number of cells: 1\n  $_DLATCH_P_ 1\n"
            "  Chip area for module child: 5\n",
            {"area": 20.0, "cells": 1},
            id="top-area-survives-module-inventory-selection",
        ),
        pytest.param(
            "Printing statistics.\n  8 cells\n  8 $_DLATCH_P_\nEnd of script.\n"
            "--- stat_dut.txt ---\n=== dut ===\n  2 2e1 cells\n  5 - wires\n"
            "  10 - wire bits\n  99 - public wires\n  99 - memory bits\n"
            "  2 - processes\n  2 2e1 NAND2\n"
            "--- openroad.log ---\n  99 $_DLATCH_P_\n",
            {"cells": 2, "wires": 5, "processes": 2, "latch_types": {}},
            id="standalone-final-stat-overrides-earlier-stage-and-lookalikes",
        ),
        pytest.param(
            "Warning: found $dlatch in cell A\n  9 $_DFF_P_\nPrinting statistics.\n=== dut ===\n",
            {"latches": None},
            id="unusable-stat-headers-ignore-pre-stat-tally-decoy",
        ),
        # F-19: a `$dlatch` named in a techmap trace is not an extra inferred latch.
        pytest.param(
            "Executing TECHMAP pass: mapping $dlatch cells\n"
            "Warning: found $dlatch in cell A\n"
            "Number of cells:                 42\n"
            "  $dlatch                          1\n"
            "  NAND2_X1                        41\n",
            {"latches": 1},
            id="stat-tally-over-occurrences",
        ),
        pytest.param(
            _STAT_STAGE + "     $_DLATCH_P_    3\nblah $dlatch blah\n",
            {"latches": 3},
            id="prefers-stat-tally",
        ),
        pytest.param(
            _STAT_STAGE + "        1   $_DLATCH_P_\n", {"latches": 1}, id="count-first-tally"
        ),
        pytest.param(
            (_STAT_STAGE + "        1   $_DLATCH_P_\n")
            + "Executing Verilog backend.\n"
            + (_STAT_STAGE + "        1   $_DLATCH_P_\n")
            + "--- stat_top.txt ---\n"
            + (_STAT_STAGE + "        1   $_DLATCH_P_\n"),
            {"latches": 1},
            id="repeated-stat-blocks-are-not-summed",
        ),
        pytest.param(
            _STAT_STAGE + "        2   $_DLATCH_P_\n     $_DLATCH_N_    3\n",
            {"latches": 5},
            id="variants-accumulate-in-the-final-block",
        ),
        pytest.param(
            "Warning: found $dlatch in cell A\n"
            + _STAT_STAGE
            + "        1   $_DLATCH_P_\n"
            + "Executing Verilog backend.\n"
            + _STAT_STAGE
            + "        4   $_DFF_PP0_\n",
            {"latches": 0},
            id="final-clean-stat-overrides-earlier-mentions",
        ),
        pytest.param(
            "13. Printing statistics.\n        1 cells\n        1   $_DLATCH_P_\n"
            "\nWarnings: 1 unique messages, 1 total\nEnd of script.\n"
            "ERROR: diagnostic excerpt follows:\n        9   $_DLATCH_N_\n",
            {"latches": 1},
            id="tally-shaped-appended-diagnostic-is-ignored",
        ),
        pytest.param(
            "Warning: found $dlatch in cell A\n13. Printing statistics.\n",
            {"latches": None},
            id="incomplete-stat-marker",
        ),
        # F-29: `$driver$...($dlatch)` helper cells that opt folds away leave no latch row.
        pytest.param(
            "Setting constant 0-bit at position 0 on $driver$x.fifo_ff[1] ($dlatch)\n" * 20
            + _STAT_STAGE
            + "     $_DFF_PP0_   4160\n",
            {"latches": 0},
            id="stat-ran-with-no-latch-row",
        ),
        pytest.param("proc $dlatch here\nanother $dlatch\n", {"latches": None}, id="no-stat"),
    ],
)
def test_final_stat_inventory(text: str, expected: dict[str, Any]) -> None:
    view = _stat_view(text)
    assert {key: view[key] for key in expected} == expected


@pytest.mark.parametrize("area", ["", "- ", "12 ", "12.5 ", "1.2e+2 "])
@pytest.mark.parametrize("cell_first", [False, True])
def test_modern_stat_latch_columns(area: str, cell_first: bool) -> None:
    row = f"$_DLATCH_P_ 8 {area}" if cell_first else f"8 {area}$_DLATCH_P_"
    view = _stat_view(f"13. Printing statistics.\n=== dut ===\n  16 {area}cells\n  {row}\n")
    assert (view["latches"], view["cells"]) == (8, 16)


@pytest.mark.parametrize("cell", ["DLH_X1", "DLL_X1", "custom_latch"])
@pytest.mark.parametrize("row", ["  {cell} 2", "  2 {cell}", "  2 5.32 {cell}"])
def test_mapped_latches_use_dfflibmap_types_and_final_inventory(cell: str, row: str) -> None:
    text = (
        f"    \\{cell} _DLATCH_P_ (.D( D), .G( E), .Q( Q));\n"
        "    \\DFF_X1 _DFF_P_ (.CK( C), .D( D), .Q( Q));\n"
        "Printing statistics.\n=== top ===\n  3 cells\n  3 $_DLATCH_P_\n"
        "End of script.\n--- stat_top.txt ---\n"
        "Printing statistics.\n=== top ===\n  4 10.0 cells\n"
        f"{row.format(cell=cell)}\n  1 2.66 $_DLATCH_N_\n  1 4.52 DFF_X1\n"
    )
    assert parse_stat(text).latch_types == {cell: 2, "$_DLATCH_N_": 1}


@pytest.mark.parametrize(
    "cell", ["$dlatchsr", "$_DLATCH_NN0_", "$_DLATCH_NP1_", "$_DLATCH_PN0_", "$_DLATCH_PP1_"]
)
@pytest.mark.parametrize("cell_first", [False, True])
def test_reset_and_set_reset_latch_inventory(cell: str, cell_first: bool) -> None:
    row = f"{cell} 3 -" if cell_first else f"3 - {cell}"
    assert _stat_view(f"Printing statistics.\n  3 - cells\n  {row}\n")["latches"] == 3


_LOGICAL_TIMING = (
    "Chip area for top module '\\design_top': 52480.00\n"
    "Number of cells: 12345\n"
    "Number of wires: 42\n"
    # period 2.0 ns, wns +0.75 ns -> crit path (2.0-0.75)*1000 = 1250 ps, Fmax 800 MHz.
    "STA_PERCLOCK: name=clk period_ns=2.000000 wns_ns=0.750000 whs_ns=0.100000"
    " setup_same_clock=1\n"
    # A cross-clock worst path has a slack but no critical path or Fmax.
    "STA_PERCLOCK: name=x period_ns=8.000000 wns_ns=1.429488 whs_ns=0.200000"
    " setup_same_clock=0\n"
    "STA_WORST_SLACK_NS: 0.500000\n"
    "STA_WORST_SLACK_NS: -0.200000\n"
    "STA_REG2REG_SLACK_NS: -0.400000\n"
    "STA_REG2REG_FMAX_MHZ: 476.190\n"
    "YOSYS_ABC_LOGIC_DELAY_PS: 125.000\n"
    "YOSYS_ABC_LOGIC_DELAY_PS: 400.000\n"
    "OPENROAD_DESIGN_AREA_UM2: 640.250\n"
    "OPENROAD_INSTANCE_COUNT: 112\n"
)
_PHYSICAL_TIMING = (
    "Number of cells: 100\n"
    "Chip area for top module '\\top': 500.0\n"
    "Number of wires: 42\n"
    "YOSYS_ABC_LOGIC_DELAY_PS: 250.000\n"
    "OPENROAD_DESIGN_AREA_UM2: 640.250\n"
    "OPENROAD_INSTANCE_COUNT: 112\n"
    "STA_PERCLOCK: name=clk period_ns=4.000000 wns_ns=2.000000 whs_ns=0.100000"
    " setup_same_clock=1\n"
    "STA_PERCLOCK: name=slow period_ns=4.000000 wns_ns=NA whs_ns=NA setup_same_clock=1\n"
)


@pytest.mark.parametrize(
    ("mode", "stdout", "expected", "clocks"),
    [
        pytest.param(
            "logical",
            _LOGICAL_TIMING,
            {
                "area_um2": 52480.0,
                "area_kge": pytest.approx(65.7644),
                "area_source": "yosys_mapped",
                "cell_count": 12345,
                "cell_count_source": "yosys_mapped",
                "wire_count": 42,
                "wns_ns": -0.2,
                "reg2reg_slack_ns": -0.4,
                "reg2reg_fmax_mhz": 476.19,
                "estimated_fmax_mhz": pytest.approx(2500.0),
                "yosys_complete": True,
                "structural_checks_complete": True,
                "passed": True,
            },
            {
                "clk": {
                    "wns_ns": 0.75,
                    "whs_ns": 0.1,
                    "critical_path_ps": 1250.0,
                    "fmax_mhz": 800.0,
                },
                "x": {"wns_ns": 1.429488, "whs_ns": 0.2, "critical_path_ps": None},
            },
            id="logical",
        ),
        pytest.param(
            "logical",
            "Chip area for top module '\\t': 100.0\nYOSYS_ABC_LOGIC_DELAY_PS: 0.000\n",
            {
                "area_um2": 100.0,
                "wns_ns": None,
                "reg2reg_fmax_mhz": None,
                "estimated_fmax_mhz": None,
            },
            {},
            id="logical-no-timing",
        ),
        pytest.param(
            "physical",
            _PHYSICAL_TIMING,
            {
                "area_um2": 640.25,
                "area_source": "openroad_post_optimization",
                "cell_count": 112,
                "cell_count_source": "openroad_post_optimization",
                "wire_count": 42,
                "estimated_fmax_mhz": None,
            },
            {
                "clk": {"wns_ns": 2.0, "critical_path_ps": 2000.0},
                "slow": {"wns_ns": None, "whs_ns": None, "critical_path_ps": None},
            },
            id="physical",
        ),
        pytest.param(
            "physical",
            "Number of cells: 100\nChip area for top module '\\top': 500.0\n",
            {"cell_count": 100, "cell_count_source": "yosys_mapped"},
            {},
            id="physical-no-instance-count",
        ),
    ],
)
def test_tool_output_reaches_the_report(
    synth_cli: SynthCli, mode: str, stdout: str, expected: dict[str, Any], clocks: dict[str, Any]
) -> None:
    outcome, report = _synthesize(synth_cli, mode=mode, stdout=stdout)
    view = _report_view(outcome, report)
    # An absent value is omitted from the report; the table spells it None.
    assert {key: view.get(key) for key in expected} == expected
    per_clock = view["per_clock"]
    assert per_clock.keys() == clocks.keys()
    for clock, values in clocks.items():
        assert {key: per_clock[clock][key] for key in values} == pytest.approx(values)


_INLINE_AREA = "Chip area for module 'dut': 100.0\n"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        pytest.param(
            _INLINE_AREA + "Warning: found $dlatch in cell A\nWarning: found $dlatch in cell B\n",
            {
                "latches": 2,
                "failure_reasons": [
                    "2 latches (actual=2, expected=0, excess=2; types: unavailable)"
                ],
            },
            id="dlatch-mentions",
        ),
        pytest.param(
            "Executing TECHMAP pass: mapping $dlatch cells\n"
            "Warning: found $dlatch in cell A\n"
            "Number of cells:                 42\n"
            "  $dlatch                          1\n"
            "  NAND2_X1                        41\n" + _INLINE_AREA,
            {"latches": 1},
            id="stat-tally",
        ),
        pytest.param(
            _INLINE_AREA + "ERROR: Combinational loop detected\nWarning: multi-driven net \\sig\n",
            {"comb_loops": 1, "multi_driven": 1, "has_critical": True},
            id="loop-and-multi-driven",
        ),
        pytest.param(
            "Number of processes: 3\nNumber of cells: 10\n" + _INLINE_AREA,
            {
                "process_count": 3,
                "has_critical": True,
                "failure_reasons": ["critical structural conditions"],
            },
            id="processes",
        ),
        # F-29: `$driver$...($dlatch)` helper cells fold away; stat then prints no latch row.
        pytest.param(
            "Setting constant 0-bit at position 0 on $driver$x.fifo_ff[1] ($dlatch)\n"
            * 20
            + "Printing statistics.\n  4160 cells\n  4160 $_DFF_PP0_\n"
            "Chip area for top module dut: 100.0\n",
            {"latches": 0, "passed": True},
            id="stat-without-latches",
        ),
        pytest.param(
            _INLINE_AREA + "Warning: found $dlatch in cell A\n13. Printing statistics.\n",
            {"latches": 1},
            id="incomplete-stat-marker-keeps-the-mention-count",
        ),
        pytest.param(
            _INLINE_AREA + "synthesis completed OK\n",
            {
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "has_critical": False,
                "passed": True,
            },
            id="clean",
        ),
    ],
)
def test_inline_structural_output_decides_the_verdict(
    synth_cli: SynthCli, stdout: str, expected: dict[str, Any]
) -> None:
    outcome, report = _synthesize(synth_cli, stdout=stdout)
    view = _report_view(outcome, report)
    assert {key: view[key] for key in expected} == expected


_LOADER_LINE = "{exe}: error while loading shared libraries: libx.so: missing"
_PHYSICAL_FILES = {
    "openroad_dut.v": "module dut; endmodule\n",
    "reports/timing/overall.rpt": "timing report\n",
    "reports/timing/overall.csv.rpt": "",
}


@pytest.mark.parametrize("returncode", [0, 127])
@pytest.mark.parametrize(
    ("executable", "marker", "mode"),
    [("sv2v", "sv2v", "logical"), ("yosys", "yosys", "logical"), ("openroad", "sta", "physical")],
)
def test_authenticated_synth_loader_is_infrastructure(
    synth_cli: SynthCli, executable: str, marker: str, mode: str, returncode: int
) -> None:
    stat = "Printing statistics.\n=== dut ===\n  2 cells\n  Chip area for top module dut: 3.0\n"
    files = {**_yosys_outputs(stat), **(_PHYSICAL_FILES if mode == "physical" else {})}
    _outcome, report = _synthesize(
        synth_cli,
        files if returncode == 0 else None,
        mode=mode,
        plan={},
        returncode=returncode,
        stdout=f"BOOLEY_STAGE: {marker}\n",
        stderr=_LOADER_LINE.format(exe=executable),
    )
    assert report["returncode"] == 2
    assert report["termination"] == "infrastructure_error"
    assert f"{executable} startup failed" in report["infra_error"]
    assert "libx.so" in report["infra_error"]
    assert report["timing_complete"] is False
    assert report["passed"] is False
    if returncode:
        assert report["yosys_complete"] is False


_ECHOED_INPUT_ERROR = "    5 | // BOOLEY_INPUT_ERROR: echoed comment\n      | ^\n"


@pytest.mark.parametrize(
    ("mode", "plan", "process", "expected", "infra_has", "infra_lacks"),
    [
        pytest.param(
            "physical",
            None,
            {
                "returncode": 2,
                "stdout": "BOOLEY_INPUT_ERROR: synth Target 'lite' SDC files"
                " [constraints/a.sdc] created no clocks",
            },
            {"returncode": 2, "termination": "infrastructure_error"},
            ["created no clocks"],
            [],
            id="clockless-sdc",
        ),
        # #3673: an RTL comment echoed into an exit-0 log is not an input error.
        pytest.param(
            "logical",
            None,
            {"stdout": _INLINE_AREA + _ECHOED_INPUT_ERROR},
            {"returncode": 0, "passed": True},
            [],
            [],
            id="echoed-input-error",
        ),
        pytest.param(
            "logical",
            {},
            {"stdout": "Chip area for module 'dut': 1000.0"},
            {"yosys_complete": False, "structural_checks_complete": False},
            [],
            [],
            id="inline-decoy",
        ),
        # No stage marker and no stage log: no tool owns the failure.
        pytest.param(
            "logical",
            {},
            {"returncode": 2, "stdout": "make: *** Error 2\n"},
            {"returncode": 2, "termination": "infrastructure_error"},
            ["no diagnostic available"],
            [],
            id="no-stage-evidence",
        ),
    ]
    + [
        # #1075: a loader failure of a tool the Yosys stage does not run is not its startup.
        pytest.param(
            "logical",
            {},
            {
                "returncode": 1,
                "stdout": "BOOLEY_STAGE: yosys\n",
                "stderr": _LOADER_LINE.format(exe=executable),
            },
            {"termination": "infrastructure_error"},
            [],
            ["missing tool:", "startup failed"],
            id=f"unrelated-loader-{executable}",
        )
        for executable in ("vivado", "vvp", "verible-verilog-lint", "openroad")
    ],
)
def test_boundary_output_classifies_the_run(
    synth_cli: SynthCli,
    mode: str,
    plan: dict[str, Any] | None,
    process: dict[str, Any],
    expected: dict[str, Any],
    infra_has: list[str],
    infra_lacks: list[str],
) -> None:
    _outcome, report = _synthesize(synth_cli, mode=mode, plan=plan, **process)
    assert {key: report[key] for key in expected} == expected
    assert all(text in report["infra_error"] for text in infra_has)
    assert not any(text in report["infra_error"] for text in infra_lacks)


# ===========================================================================
# Results, baselines and reporting (through the public run)
# ===========================================================================

_PHYSICAL_STAT = (
    "Printing statistics.\n=== dut ===\n"
    "  16 36.176 cells\n  8 - NAND2\n  Chip area for top module dut: 36.176\n"
)
_OPENROAD_AREA = "Design area 52480 u^2 50% utilization.\nOPENROAD_INSTANCE_COUNT: 12345\n"
_BUILD_FILES = (
    "stat_soc_top.txt",
    "log_abc_soc_top.txt",
    "synth_soc_top.v",
    "sta_soc_top.v",
    "sta.log",
    "synth.ys",
)
_CORRUPT_RC = "ERROR: frontend rejected converted Verilog\n"


def _physical_files(openroad: str) -> dict[str, str]:
    """The files a finished physical pass leaves, with *openroad* as its OpenROAD log."""
    return {**_yosys_outputs(_PHYSICAL_STAT), "openroad.log": openroad, **_PHYSICAL_FILES}


def _physical(
    synth_cli: SynthCli, openroad: str, **process: Any
) -> tuple[EndpointOutcome, dict[str, Any]]:
    """Run a physical Target whose OpenROAD log is *openroad*."""
    return _synthesize(synth_cli, _physical_files(openroad), mode="physical", **process)


def _sta(
    worst: float,
    *clocks: tuple[str, float, float | None, float | None],
    reg2reg_slack: float | None = None,
    reg2reg_fmax: float | None = None,
) -> str:
    """An OpenROAD log with the area, the worst slack, per-clock markers and reg2reg results."""
    text = f"{_OPENROAD_AREA}STA_WORST_SLACK_NS: {worst:.6f}\n"
    if reg2reg_slack is not None:
        text += f"STA_REG2REG_SLACK_NS: {reg2reg_slack:.6f}\n"
    if reg2reg_fmax is not None:
        text += f"STA_REG2REG_FMAX_MHZ: {reg2reg_fmax:.3f}\n"
    return text + "".join(_perclock_marker(*clock) for clock in clocks)


def _stat_files(area: float, cells: int, log: str = "") -> dict[str, str]:
    """A finished Yosys stage with *area* and *cells*; *log* is appended to ``yosys.log``."""
    stat = f"Number of cells: {cells}\nChip area for top module dut: {area}\n"
    mapped = f"Chip area for top module '\\dut': {area}\nNumber of cells: {cells}\n"
    return {**_yosys_outputs(stat), "yosys.log": mapped + log}


def _leave(build: Path, files: dict[str, str]) -> None:
    """Write *files* under *build* as a run in progress would (newer than its dispatch)."""
    fresh = time.time() + 1
    for name, text in files.items():
        path = build / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        os.utime(path, (fresh, fresh))


_Run = SubprocessResult | tuple[SubprocessResult, dict[str, str]]


def _split(run: _Run) -> tuple[SubprocessResult, dict[str, str]]:
    return run if isinstance(run, tuple) else (run, {})


def _by_target(synth_cli: SynthCli, outputs: dict[str, _Run]) -> None:
    """Answer each Target's ``make`` from *outputs*; the build dir argv names gets its files."""

    def run(argv: list[str]) -> SubprocessResult:
        result, files = _split(outputs[Path(argv[2]).parent.name])
        _leave(synth_cli.project / argv[2], files)
        return result

    synth_cli.tool_output = run


def _git_project(project: Path) -> str:
    """Make *project* a Git repository with one commit and return its short revision."""

    def git(*args: str) -> str:
        identity = [
            "-c",
            "core.autocrlf=false",
            "-c",
            "user.name=Booley",
            "-c",
            "user.email=booley@example.invalid",
        ]
        argv = ["git", *identity, "-c", "commit.gpgsign=false", *args]
        return run_bounded_child(argv, cwd=project, check=True).stdout.strip()

    git("init", "-q")
    (project / ".git" / "info" / "exclude").write_bytes(b"/.booley\n")
    (project / "rtl.v").write_bytes(b"module dut; endmodule\n")
    git("add", "-A")
    git("commit", "-q", "-m", "baseline")
    return git("rev-parse", "--short", "HEAD")


def _baseline_then_candidate(
    synth_cli: SynthCli, baseline: _Run | dict[str, _Run], candidate: _Run | dict[str, _Run]
) -> list[str]:
    """Answer ``make`` from *baseline* while the baseline checkout exists, else *candidate*.

    Either may map Target names to their runs. The execution worktree lives only during
    the baseline pass, so the role comes from the real checkout, not from the order of
    the calls. Returns the roles of the runs, in order.
    """
    worktree = baseline_worktree_path(synth_cli.project, "HEAD")
    roles: list[str] = []

    def run(argv: list[str]) -> SubprocessResult:
        is_baseline = worktree.exists()
        roles.append("baseline" if is_baseline else "candidate")
        spec = baseline if is_baseline else candidate
        if isinstance(spec, dict):
            spec = spec[Path(argv[2]).parent.name]
        result, files = _split(spec)
        _leave((worktree if is_baseline else synth_cli.project) / argv[2], files)
        return result

    synth_cli.tool_output = run
    return roles


def _ok(stdout: str, **process: Any) -> SubprocessResult:
    return SubprocessResult(returncode=0, stdout=stdout, stderr="", duration_s=1.0, **process)


_SUMMARY_CASES: dict[str, dict[str, Any]] = {
    "pass": {
        "openroad": _sta(0.25, ("clk", 2.0, 0.75, 0.1), reg2reg_fmax=410.0),
        "has": [
            "RESULT: PASS",
            "kGE",
            "12,345 cells",
            "1,250 ps",
            "800 MHz",
            "+0.250 ns",
            "hold slack +0.100 ns, reg2reg Fmax 410 MHz",
        ],
        "lacks": ["I/O-bound"],
        "fields": {"passed": True, "io_bound_critical": False},
    },
    "multiclock": {
        "openroad": _sta(0.0, ("fast", 1.0, 0.5, 0.1), ("slow", 4.0, 0.0, 0.1)),
        "has": ["4,000 ps", "250 MHz [slow]"],
    },
    "setup-violated": {
        "openroad": _sta(-1.25, ("clk", 4.0, -1.25, 0.1)),
        "has": ["RESULT: WARN", "VIOLATED", "-1.250 ns"],
    },
    "hold-violated": {
        "openroad": _sta(0.25, ("clk", 2.0, 0.25, -0.125)),
        "has": ["RESULT: WARN", "hold slack -0.125 ns"],
    },
    "reg2reg-only": {
        "openroad": _sta(-0.5, reg2reg_slack=-0.5),
        "has": ["reg2reg slack -0.500 ns"],
    },
    "logical-estimate": {
        "stdout": "Chip area for top module '\\t': 6400.0\nYOSYS_ABC_LOGIC_DELAY_PS: 500.000\n",
        "has": [
            "logical estimate",
            "estimated Fmax 2000 MHz",
            "estimated Fmax is probably inaccurate",
            "excludes placement and wire delays",
            "RESULT: PASS",
        ],
        "lacks": ["-- ps", "no timing was reported"],
        "fields": {"synth_mode": "logical", "area_source": "yosys_mapped"},
    },
    "io-bound": {
        "openroad": _sta(-2.0, ("clk", 4.0, -2.0, 0.1), reg2reg_slack=-0.25),
        "has": [
            "-2.000 ns",
            "-0.250 ns",
            "file_type: SDC",
            "set_input_delay",
            "set_output_delay",
            "set_false_path",
        ],
        "lacks": ["period_ps", "[flows.synth.timing]"],
        "fields": {"io_bound_critical": True},
    },
    "io-bound-within-epsilon": {
        "openroad": _sta(-0.2505, ("clk", 4.0, -0.2505, 0.1), reg2reg_slack=-0.25),
        "lacks": ["I/O-bound"],
        "fields": {"io_bound_critical": False},
    },
    "latches-and-loop": {
        "stdout": (
            "Chip area for top module '\\t': 52480.0\n"
            "$dlatch cell A\n$dlatch cell B\nCombinational loop detected\n"
        ),
        "exit_code": EXIT_FAILURE,
        "has": ["FAIL", "CRITICAL", "2 latches", "1 comb loop"],
    },
    "physical-without-timing": {
        "stdout": "Chip area for top module '\\t': 6400.0\nNumber of cells: 100\n",
        "mode": "physical",
        "exit_code": EXIT_FAILURE,
        "has": ["RESULT: FAIL", "PARTIAL"],
    },
    "tool-error": {
        "stdout": "Yosys failed before stat output\n",
        "stderr": _CORRUPT_RC,
        "returncode": 1,
        "exit_code": EXIT_ERROR,
        "has": ["RESULT: ERROR", _CORRUPT_RC.strip()],
        "fields": {"passed": False, "has_metrics": False},
        "failure_output": _CORRUPT_RC.strip(),
    },
}


@pytest.mark.parametrize("name", list(_SUMMARY_CASES))
def test_run_result_reaches_the_summary(synth_cli: SynthCli, name: str) -> None:
    case = _SUMMARY_CASES[name]
    if "openroad" in case:
        outcome, report = _physical(synth_cli, case["openroad"])
    else:
        outcome, report = _synthesize(
            synth_cli,
            mode=case.get("mode", "logical"),
            stdout=case["stdout"],
            stderr=case.get("stderr", ""),
            returncode=case.get("returncode", 0),
        )
    view = _report_view(outcome, report)

    assert outcome.exit_code == case.get("exit_code", EXIT_SUCCESS), outcome.report_text
    for text in case.get("has", []):
        assert text in outcome.report_text
    for text in case.get("lacks", []):
        assert text not in outcome.report_text
    fields = case.get("fields", {})
    assert {key: view.get(key) for key in fields} == fields
    # The report's own top-level export, which the view's condition would otherwise mask.
    assert report.get("io_bound_critical", False) is fields.get("io_bound_critical", False)
    assert case.get("failure_output", "") in view.get("failure_output", "")


@pytest.mark.parametrize("case", ["pass", "fail", "physical"])
def test_run_publishes_report_log_and_build_dir(synth_cli: SynthCli, case: str) -> None:
    files = dict.fromkeys(_BUILD_FILES, "x\n")
    failed = case == "fail"
    if case == "physical":
        outcome, report = _physical(synth_cli, _sta(0.5, ("clk", 4.0, 0.5, 0.1)), stdout="out\n")
    else:
        outcome, report = _synthesize(
            synth_cli,
            files,
            returncode=1 if failed else 0,
            stdout="Yosys died before stat output\n" if failed else _YOSYS_AREA,
            stderr=_CORRUPT_RC if failed else "",
        )
    published = report["artifacts"]
    live = synth_cli.project / published["live_dirs"]["build"]
    published_log = synth_cli.project / published["log"]

    assert outcome.exit_code == (EXIT_ERROR if failed else EXIT_SUCCESS)
    assert (report["flow"], report["target"], report["passed"]) == ("synth", "lite", not failed)
    # No per-file map: the entry points plus the directories holding everything else.
    assert set(published) == {"report", "log", "live_dirs"} | (
        {"timing"} if case == "physical" else set()
    )
    assert ("timing" in published["live_dirs"]) is (case == "physical")
    assert published["report"] == ".booley/flow-reports/synth/1/targets/lite.json"
    assert published["live_dirs"]["build"].endswith("synth/lite/synth")
    assert not published["live_dirs"]["build"].startswith("/")
    if case != "physical":
        assert set(files) <= {path.name for path in live.iterdir()}
    log_text = published_log.read_text(encoding="utf-8")
    assert (_CORRUPT_RC in log_text) is failed
    assert ("Yosys died before stat output" in log_text) is failed
    assert (_YOSYS_AREA in log_text) is (case == "pass")
    assert f"lite: log: {published['log']}" in outcome.report_text
    if case == "pass":
        assert (report["cell_count"], report["area_kge"]) == (100, pytest.approx(8.02005))

    # The published copy is a snapshot: the live log changing later does not change it.
    live_log = work_root_for(synth_cli.project, "synth", "lite") / "run.log"
    live_log.write_text("rewritten later\n", encoding="utf-8")
    assert published_log.read_text(encoding="utf-8") == log_text
    if case == "physical":
        live_report = synth_cli.project / published["live_dirs"]["timing"] / "overall.rpt"
        live_report.write_text("rewritten later\n", encoding="utf-8")
        copied = synth_cli.project / published["timing"] / "overall.rpt"
        assert copied.read_text(encoding="utf-8") == "timing report\n"


def test_numbered_log_publication_failure_is_best_effort(synth_cli: SynthCli) -> None:
    # A full evidence disk cannot be produced on a test host, so the write itself fails.
    with patch(
        "booley.flows.synth.flow.artifacts.publish_bytes",
        side_effect=OSError("evidence disk full"),
    ):
        outcome = synth_cli.run()

    assert outcome.exit_code == EXIT_SUCCESS
    assert "log" not in outcome.detail["lite"]["artifacts"]
    assert work_root_for(synth_cli.project, "synth", "lite").joinpath("run.log").is_file()


def test_timing_snapshot_failure_cleans_partial_evidence(synth_cli: SynthCli) -> None:
    real_publish = artifacts.publish_file
    timing_calls = 0

    def fail_second_timing_file(invocation_dir, components, source, **kwargs):
        nonlocal timing_calls
        if "timing" in components:
            timing_calls += 1
            if timing_calls == 2:
                # A full evidence disk cannot be produced on a test host.
                raise OSError("evidence disk full")
        return real_publish(invocation_dir, components, source, **kwargs)

    with patch(
        "booley.flows.synth.flow.artifacts.publish_file", side_effect=fail_second_timing_file
    ):
        _outcome, report = _physical(synth_cli, _sta(0.5, ("clk", 4.0, 0.5, 0.1)))

    assert timing_calls == 2
    assert "timing" not in report["artifacts"]
    published = synth_cli.project / ".booley/flow-reports/synth/1/artifacts/synth_lite"
    assert not (published / "timing").exists()


class TestSingleConfigRun:
    """Concurrency, lease and publication failures of one Target's run."""

    def test_configure_render_error_is_infrastructure_error(self, synth_cli: SynthCli) -> None:
        # The autouse fixture already replaces the configure half, and the render error
        # is raised from it: no public name reaches that step alone.
        with patch.object(
            AsicSynthesizeFlow, "_configure_synth", side_effect=OSError("disk full")
        ):
            outcome = synth_cli.run()

        lite = outcome.detail["lite"]
        assert outcome.exit_code == EXIT_ERROR
        assert (lite["returncode"], lite["termination"]) == (2, "infrastructure_error")
        assert "failed to render synthesis build dir: disk full" in outcome.report_text

    def test_concurrent_dry_run_cannot_delete_live_target_workspace(
        self, synth_cli: SynthCli
    ) -> None:
        started = Event()
        release = Event()
        marker = work_root_for(synth_cli.project, "synth", "lite") / "synth" / "owner.marker"

        def held_eda_run(_argv: list[str]) -> SubprocessResult:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("live\n", encoding="utf-8")
            started.set()
            if not release.wait(hang_guard_s()):
                raise TimeoutError("test did not release held synthesis")
            return _PASS_OUTPUT

        synth_cli.tool_output = held_eda_run
        dry_run = AsicSynthesizeFlow()

        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(synth_cli.run)
            assert started.wait(hang_guard_s())
            try:
                preview = dry_run.execute_cli(
                    ["--target", "lite", "--work-dir", str(synth_cli.project), "--dry-run"]
                )
                assert marker.read_text(encoding="utf-8") == "live\n"
                assert preview.exit_code == EXIT_SUCCESS
            finally:
                release.set()
            outcome = running.result(timeout=hang_guard_s())

        assert outcome.exit_code == EXIT_SUCCESS

    def test_captured_output_publication_does_not_copy_shared_log(
        self, synth_cli: SynthCli
    ) -> None:
        with patch.object(shutil, "copy2", wraps=shutil.copy2) as copy2:
            outcome = synth_cli.run()

        assert outcome.exit_code == EXIT_SUCCESS
        copy2.assert_not_called()

    def test_workspace_lease_timeout_is_an_infrastructure_error(self, synth_cli: SynthCli) -> None:
        synth_cli.config(f"timeout_ms = {math.ceil(scaled(1)) * 1000}\n")
        build_root = work_root_for(synth_cli.project, "synth", "lite")

        with work_root_lease(build_root, timeout_s=hang_guard_s()):
            outcome = synth_cli.run()

        assert outcome.exit_code == EXIT_ERROR
        error = outcome.detail["lite"]["infra_error"]
        assert "cannot lease" in error
        assert str(build_root) in error

    def test_workspace_lease_release_failure_is_an_infrastructure_error(
        self, synth_cli: SynthCli
    ) -> None:
        # The lease lives in the filesystem lock; an unlock failure cannot be provoked otherwise.
        with patch("booley.flows.edam.release_file_lock", side_effect=OSError("unlock failed")):
            outcome = synth_cli.run()

        assert outcome.exit_code == EXIT_ERROR
        assert "unlock failed" in outcome.detail["lite"]["infra_error"]


_RUN_PASS = SubprocessResult(
    returncode=0,
    stdout="Chip area for top module '\\t': 52480.0\nNumber of cells: 100\n",
    stderr="",
    duration_s=1.0,
)
_RUN_LATCH = SubprocessResult(
    returncode=0,
    stdout="Chip area for top module '\\t': 52480.0\n$dlatch cell A\nCombinational loop detected\n",
    stderr="",
    duration_s=1.0,
)
_RUN_CRASH = SubprocessResult(returncode=1, stdout="", stderr="yosys crashed", duration_s=1.0)


@pytest.mark.parametrize("named_state", [True, False], ids=["state-file", "no-state-file"])
@pytest.mark.parametrize(
    ("process", "exit_code", "verdict"),
    [(_RUN_PASS, EXIT_SUCCESS, 0), (_RUN_LATCH, EXIT_FAILURE, 0), (_RUN_CRASH, EXIT_ERROR, 2)],
    ids=["pass", "fail", "crash"],
)
def test_synthesis_run_records_no_criterion(
    synth_cli: SynthCli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process: SubprocessResult,
    exit_code: int,
    verdict: int,
    named_state: bool,
) -> None:
    """A hand-run Synthesis keeps its exit status and metric verdict, records no
    Criterion and writes no development-state or evidence file."""
    from booley.flows.metric_declarations import FlowMetricValues
    from booley.measurement.flow_metrics import report_metric_values
    from booley.measurement.metrics_file import Bound, TableRules
    from booley.measurement.verdicts import (
        FlowFailed,
        FlowValues,
        evaluate_table,
        measure_exit_code,
    )

    state_path = tmp_path / "state.json"
    monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)
    if named_state:
        state_path.write_bytes(b'{"opaque": true}\n')
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
        before = state_path.read_bytes()
    synth_cli.tool_output = process

    result = synth_cli.run()

    assert result.exit_code == exit_code
    if named_state:
        assert state_path.read_bytes() == before
    else:
        assert list(tmp_path.rglob("state.json")) == []
    assert not (tmp_path / "logs").exists()
    rules = TableRules(
        "t",
        "vlnv:x#t",
        "synth",
        (Bound("area_um2_max", "area_um2", "max", 60000, None, None, None),),
        (),
    )
    values = report_metric_values("synth", result.detail).get("lite")
    outcome = (
        FlowValues(values, values.clocks)
        if isinstance(values, FlowMetricValues)
        else FlowFailed("synthesis returned no values")
    )
    judged = evaluate_table(rules, outcome)
    assert measure_exit_code(item.verdict for item in judged) == verdict


def test_timed_out_synthesis_returns_no_values_and_names_the_timeout_on_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement.metrics_file import Bound, TableRules
    from booley.measurement.verdicts import FlowFailed, evaluate_table
    from tests.harness.test_checkout_flow_metrics import RUN_ID, checkout_result

    monkeypatch.setenv("BOOLEY_RUN_ID", RUN_ID)
    outer = tmp_path / "out"
    flow = AsicSynthesizeFlow()
    flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
    flow.args.report_dir = outer / "flow-reports"
    timed_out = SubprocessResult(
        returncode=-9, timed_out=True, stdout="Number of cells: 11954\n", duration_s=1.0
    )
    with patch.object(flow, "_execute", return_value=timed_out):
        outcome = flow._run()
    report = outer / "flow-reports" / "synth" / "1" / "report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {"run_id": RUN_ID, "detail": outcome.detail, "report_text": outcome.report_text}
        ),
        encoding="utf-8",
    )
    result = checkout_result(outer, "synth", outcome.exit_code)
    assert result.metrics == {}
    assert result.unavailable == {"lite": "synthesis execution timeout"}
    assert result.excerpt is not None
    bounds = tuple(
        Bound(name, metric, "max", 99999, None, None, None)
        for name, metric in (("cells", "cell_count"), ("area", "area_um2"))
    )
    rules = TableRules("t", "vlnv:x#t", "synth", bounds, ())
    verdicts = evaluate_table(rules, FlowFailed(result.excerpt))
    assert [v.verdict.value for v in verdicts] == ["error"] * 2
    assert all(v.detail.startswith("lite: synthesis execution timeout") for v in verdicts)


# ===========================================================================
# Stage files (results come from files under the build dir, stale-gated by dispatch time)
# ===========================================================================

_FINAL_LOOP_WARNING = "Warning: found logic loop in module dut:\n    wire \\feedback\n"
_UNKNOWN_AREA_STAT = (
    "13. Printing statistics.\n"
    "\n=== dut ===\n"
    "\n        1 cells\n"
    "        1   $_DLATCH_P_\n"
    "\n   Area for cell type $_DLATCH_P_ is unknown!\n"
)
_YOSYS_AREA = "Chip area for top module '\\dut': 6400.0\nNumber of cells: 100\n"
_STAT_AREA = "Number of cells: 100\nChip area for top module dut: 6400.0\n"
_STA_CLOCK = (
    "STA_PERCLOCK: name=clk period_ns=4.000000 wns_ns={wns} whs_ns={whs} setup_same_clock=1\n"
)


def _final_structural_failure(returncode: int) -> dict[str, str]:
    """Completed structural evidence, followed by a tool crash when *returncode* is set."""
    yosys_log = _FINAL_LOOP_WARNING + _YOSYS_AREA
    if returncode:
        yosys_log += "ERROR: unknown tool exception after final outputs\n"
    return {
        "yosys.log": yosys_log,
        "stat_dut.txt": "Number of cells: 100\n",
        # The loop survives optimisation, so the pre-mapping check owns it; the
        # final check, run after mapping, sees only the multi-driven net.
        "premap_check_dut.txt": _FINAL_LOOP_WARNING + "Found and reported 1 problems.\n",
        "check_dut.txt": "Warning: multiple conflicting drivers for dut.sig:\n"
        "    port Q[0] of cell $procdff$1 ($dff)\n"
        "Found and reported 1 problems.\n",
        "synth_dut.v": "module dut; endmodule\n",
    }


def _report_log(synth_cli: SynthCli, report: dict[str, Any]) -> str:
    return (synth_cli.project / report["artifacts"]["log"]).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    (
        "mode",
        "files",
        "stale",
        "returncode",
        "text_has",
        "text_lacks",
        "log_has",
        "expected",
    ),
    [
        pytest.param(
            "logical",
            _yosys_outputs(_UNKNOWN_AREA_STAT),
            (),
            0,
            ["synthesis log reports an error despite exit 0"],
            [],
            ["Area for cell type $_DLATCH_P_ is unknown!"],
            {
                "exit_code": EXIT_FAILURE,
                "passed": False,
                "returncode": 1,
                "termination": "design_failure",
                "ppa_complete": False,
                "area_um2": None,
                "latches": 1,
                "unexpected_latches": 1,
                "has_critical": True,
            },
            id="unknown-area",
        ),
        pytest.param(
            "logical",
            {
                **_yosys_outputs(_STAT_AREA),
                "yosys.log": _YOSYS_AREA + "ABC: Warning: The network has multiple outputs.\n",
            },
            (),
            0,
            ["RESULT: WARN", "1 EDA warning"],
            [],
            [],
            {"exit_code": EXIT_SUCCESS, "passed": True, "total_warnings": 1, "grade": "warn"},
            id="advisory-warning",
        ),
        # A leftover log predating the dispatch is never a fresh result.
        pytest.param(
            "logical",
            {"yosys.log": _YOSYS_AREA.replace("6400.0", "52480.0").replace("100", "12345")},
            ("yosys.log",),
            0,
            ["no metrics"],
            ["12,345"],
            [],
            {"exit_code": EXIT_FAILURE, "failure_reasons": ["no metrics"]},
            id="stale-log",
        ),
        # yosys and ABC can print ERROR: lines yet exit 0.
        pytest.param(
            "logical",
            {"yosys.log": _YOSYS_AREA + "ERROR: ABC gave up on this netlist\n"},
            (),
            0,
            ["RESULT: ERROR"],
            [],
            ["despite exit 0"],
            {"exit_code": EXIT_ERROR},
            id="error-despite-exit-0",
        ),
        # A clock label without numeric setup/hold evidence cannot complete PPA.
        pytest.param(
            "physical",
            {
                "yosys.log": _YOSYS_AREA,
                "openroad.log": _STA_CLOCK.format(wns="NA", whs="NA")
                + "Design area 7000 u^2 50% utilization.\n",
            },
            (),
            0,
            ["PARTIAL"],
            [],
            [],
            {"exit_code": EXIT_FAILURE, "timing_complete": False, "ppa_complete": False},
            id="all-na-clock",
        ),
    ],
)
def test_stage_files_decide_the_verdict(
    synth_cli: SynthCli,
    mode: str,
    files: dict[str, str],
    stale: tuple[str, ...],
    returncode: int,
    text_has: list[str],
    text_lacks: list[str],
    log_has: list[str],
    expected: dict[str, Any],
) -> None:
    outcome, report = _synthesize(synth_cli, files, mode=mode, stale=stale, returncode=returncode)
    view = _report_view(outcome, report)
    assert {key: view[key] for key in expected} == expected
    assert all(text in outcome.report_text for text in text_has)
    assert not any(text in outcome.report_text for text in text_lacks)
    run_log = _report_log(synth_cli, report)
    assert all(text in run_log for text in log_has)


@pytest.mark.parametrize("returncode", [0, 2])
def test_final_check_structural_findings_fail_and_reach_every_report_surface(
    synth_cli: SynthCli, returncode: int
) -> None:
    outcome, report = _synthesize(
        synth_cli, _final_structural_failure(returncode), returncode=returncode
    )

    assert outcome.exit_code == (EXIT_ERROR if returncode else EXIT_FAILURE)
    assert "1 comb loop" in outcome.report_text
    assert "1 multi-driven" in outcome.report_text
    target = outcome.detail["lite"]
    assert target["total_warnings"] == 2
    assert target["warning_summary"]["by_disposition"] == {"structural": 2}
    assert target["comb_loops"] == 1
    assert target["multi_driven"] == 1
    assert report["termination"] == ("infrastructure_error" if returncode else "design_failure")
    assert report["returncode"] == (2 if returncode else 1)
    if returncode:
        assert "unknown tool exception" in report["infra_error"]
        assert "RESULT: ERROR" in outcome.report_text
    assert report["total_warnings"] == 2
    assert report["implementation"]["conditions"]["warning_summary"]["total_warnings"] == 2


def _constraint_check_block(scenario: str) -> str:
    """The block run_openroad.tcl prints: every captured check_setup line
    carries the prefix so OpenSTA's "Warning:" text is not an EDA warning."""
    if scenario == "unchecked":
        return ""
    check_lines: list[str] = []
    if scenario == "clock_only":
        inputs = [f"in{i}" for i in range(10)] + ["data[3]", "end"]
        endpoints = [f"out{i}" for i in range(13)] + ["r1/D"]
        check_lines = [
            "Warning: There are 12 input ports missing set_input_delay.",
            *(f"  {name}" for name in inputs),
            "Warning: There is 1 output port missing set_output_delay.",
            "  y",
            "Warning: There are 14 unconstrained endpoints.",
            *(f"  {name}" for name in endpoints),
        ]
    return "".join(f"STA_CONSTRAINT_CHECK: {line}\n" for line in ["begin", *check_lines, "end"])


def _assert_constraint_outcome(
    synth_cli: SynthCli, outcome: EndpointOutcome, report: dict[str, Any], scenario: str
) -> None:
    if scenario == "unchecked":
        # No block means no check ran (logical mode, or OpenROAD died first):
        # no key and no NOTE, not zero counts.
        assert "unconstrained" not in report
        assert "unconstrained" not in outcome.detail["lite"]
        assert "constrained paths" not in outcome.report_text
        return
    detail = report["unconstrained"]
    assert outcome.detail["lite"]["unconstrained"] == detail
    if scenario == "constrained":
        assert "timing covers only constrained paths" not in outcome.report_text
        assert detail == {
            key: {"count": 0, "sample": []}
            for key in ("endpoints", "inputs_without_delay", "outputs_without_delay")
        }
        return
    note = next(line for line in outcome.report_text.splitlines() if "constrained paths" in line)
    assert "NOTE" in note
    assert "14 unconstrained endpoints" in note
    assert "12 inputs without set_input_delay (in0, in1, in2, +9 more)" in note
    assert "1 output without set_output_delay (y)" in note
    assert detail["inputs_without_delay"] == {"count": 12, "sample": [f"in{i}" for i in range(10)]}
    assert detail["endpoints"]["count"] == 14
    assert len(detail["endpoints"]["sample"]) == 10
    assert detail["outputs_without_delay"] == {"count": 1, "sample": ["y"]}
    # The bounded sample drops names the run log keeps.
    run_log = _report_log(synth_cli, report)
    assert "data[3]" in run_log
    assert "r1/D" in run_log


@pytest.mark.parametrize("scenario", ["clock_only", "constrained", "unchecked"])
def test_timing_markers_rederived_from_openroad_log(synth_cli: SynthCli, scenario: str) -> None:
    """The Python-derived Fmax/critical-path markers (legacy in-process
    prints) are reconstructed from the STA log file at interpret time, and
    the constraint check block becomes a NOTE that never moves the verdict."""
    files = {
        **_yosys_outputs(_STAT_AREA),
        "yosys.log": _YOSYS_AREA,
        "openroad.log": "STA_WORST_SLACK_NS: 2.000000\n"
        + _STA_CLOCK.format(wns="2.000000", whs="0.1")
        + "Design area 7000 u^2 50% utilization.\n"
        + _constraint_check_block(scenario),
    }
    outcome, report = _synthesize(synth_cli, files, mode="physical")

    assert outcome.exit_code == EXIT_SUCCESS
    # period 4 ns, wns 2 ns -> crit path 2000 ps -> Fmax 500 MHz, derived
    # at interpret time from the log file (not from process stdout).
    assert "2,000 ps" in outcome.report_text
    assert "500 MHz" in outcome.report_text
    assert "+2.000 ns" in outcome.report_text
    # The check never changes the verdict or exit code (the raw OpenSTA
    # "Warning:" form would have turned this PASS into WARN).
    assert "RESULT: PASS" in outcome.report_text
    _assert_constraint_outcome(synth_cli, outcome, report, scenario)


# ===========================================================================
# Baseline comparison flow
# ===========================================================================


@contextmanager
def _fake_baseline_worktree(project_root: Path, ref: str, *, paired_project):
    del paired_project
    """Stand-in for ``baseline_worktree``: yields a real dir under the project
    (so path derivations work) without touching git. Records nothing itself;
    tests that need enter/exit tracking define their own."""
    wt = Path(project_root) / ".booley" / f".baseline-wt-fake-{ref}"
    wt.mkdir(parents=True, exist_ok=True)
    yield wt


class TestBaselineFlow:
    """Baseline comparison synthesizes the ``--baseline`` ref in a throwaway git
    worktree — never the caller's tree — so it works in both Ticket and
    a Developer Agent session (see also test_interactive_smoke)."""

    def test_baseline_dual_run(self, synth_cli: SynthCli, capsys: pytest.CaptureFixture[str]):
        sha = _git_project(synth_cli.project)
        synth_cli.config('synth_mode = "physical"\n')
        synth_cli.resolution.resolve = _resolving(
            lambda root: _with_synth_mode(_fake_synth_resolved(root), "physical")
        )
        advisory = "ABC: Warning: The network has multiple outputs.\n"

        def sta(wns: float, area: float) -> dict[str, str]:
            # period 4 ns: wns 2.0 -> 2,000 ps critical path, wns 1.5 -> 2,500 ps.
            return {
                "openroad.log": f"STA_WORST_SLACK_NS: {wns:.6f}\n"
                + _STA_CLOCK.format(wns=f"{wns:.6f}", whs="0.1")
                + f"Design area {area} u^2 50% utilization.\n"
            }

        roles = _baseline_then_candidate(
            synth_cli,
            baseline={
                "lite": (_ok(""), {**_stat_files(51200.0, 100, advisory), **sta(2.0, 7000.0)}),
                "full": (_ok(""), {**_stat_files(6400.0, 100), **sta(2.0, 7000.0)}),
            },
            candidate={
                "lite": (_ok(""), {**_stat_files(52480.0, 110), **sta(1.5, 7175.0)}),
                "full": (_ok(""), {**_stat_files(6300.0, 100), **sta(2.0, 6890.0)}),
            },
        )

        outcome = synth_cli.run(
            "--baseline", "HEAD", "--report-dir", "reports", target="lite,full"
        )

        assert outcome.exit_code == EXIT_SUCCESS
        text = outcome.report_text
        assert f"baseline: {sha}" in text
        assert "RESULT: PASS" in text
        _, rows = TestSummaryTable._table(text)
        # Area delta, then critical-path delta, per row.
        assert [re.findall(r"[+-]\d+\.\d%", row) for row in rows] == [
            ["+2.5%", "+25.0%"],
            ["-1.6%", "+0.0%"],
        ]
        assert "(delta +2.5%)" in capsys.readouterr().err
        assert [(unit.role, unit.selector) for unit in synth_cli.run_plan.work_units] == [
            ("baseline", "lite"),
            ("baseline", "full"),
            ("candidate", "lite"),
            ("candidate", "full"),
        ]
        # The baseline ran in the throwaway worktree first and the worktree is gone.
        assert [role for role, _ in itertools.groupby(roles)] == ["baseline", "candidate"]
        assert not baseline_worktree_path(synth_cli.project, "HEAD").exists()
        assert synth_cli.flow is not None
        assert Path(synth_cli.flow.args.work_dir) == synth_cli.project

        report = json.loads((synth_cli.project / "reports/synth_lite.json").read_text("utf-8"))
        assert report["baseline"]["ref"] == sha
        assert outcome.detail["baseline_ref"] == sha
        # Each Target's own metrics, not the first Target's, ride in the run detail.
        assert [outcome.detail[name]["cell_count"] for name in ("lite", "full")] == [110, 100]
        assert report["delta_pct"] == pytest.approx(2.5, rel=0.1)
        assert report["cell_count"] == 110
        assert report["baseline"]["cell_count"] == 100
        assert "cells" not in report["baseline"]
        comparison = report["implementation"]["comparison"]
        assert comparison["deltas"]["cell_count"]["delta_pct"] == pytest.approx(10.0)
        assert comparison["baseline"]["conditions"]["warning_summary"]["total_warnings"] == 1
        assert outcome.detail["lite"]["baseline_metrics"]["total_warnings"] == 1

    def test_worktree_setup_failure_rejected(self, synth_cli: SynthCli):
        """A worktree that cannot be created (bad ref) is surfaced as an error
        rather than crashing the run."""
        _git_project(synth_cli.project)

        outcome = synth_cli.run("--baseline", "no-such-ref")

        assert outcome.exit_code == EXIT_ERROR
        assert "synth:" in outcome.report_text
        assert "no-such-ref" in outcome.report_text
        assert synth_cli.commands == []

    def test_baseline_resolution_failure_is_published_with_canonical_error(
        self, synth_cli: SynthCli
    ):
        _git_project(synth_cli.project)
        message = "FuseSoC could not resolve submodule source vendor/ip/top.sv"
        resolve = synth_cli.resolution.resolve
        baseline_calls: dict[str, int] = {}

        def failing_baseline_execution(handle, **kwargs):
            # Planning resolves the baseline once; the failure comes on its execution.
            if ".baseline-wt-" in str(handle.project_root) and handle.selector != "ok":
                baseline_calls[handle.selector] = baseline_calls.get(handle.selector, 0) + 1
                if baseline_calls[handle.selector] > 1:
                    raise fusesoc_registry.TargetResolutionError(message)
            return resolve(handle, **kwargs)

        synth_cli.resolution.resolve = failing_baseline_execution
        _by_target(
            synth_cli,
            {
                "broken": _ok(_YOSYS_AREA + "$dlatch cell A\nCombinational loop detected\n"),
                "ok": _PASS_OUTPUT,
                "slow": _ok(_YOSYS_AREA + "STA_WORST_SLACK_NS: -0.100000\n"),
            },
        )

        outcome = synth_cli.run(
            "--baseline", "HEAD", "--report-dir", "reports", target="broken,ok,slow"
        )

        text = outcome.report_text
        assert outcome.exit_code == EXIT_ERROR
        assert synth_cli.commands, "planning passed: the candidate ran"
        assert "baseline broken: ERROR" in text
        assert message in text
        assert TestSummaryTable._status_by_target(text) == {
            "broken": "ERROR (baseline)",
            "ok": "PASS",
            "slow": "ERROR (baseline)",
        }
        assert TestSummaryTable._result_line(text).startswith("RESULT: ERROR")
        assert outcome.detail["broken"]["baseline_metrics"]["infra_error"] == message
        reports = synth_cli.project / "reports"
        report = json.loads((reports / "synth_broken.json").read_text(encoding="utf-8"))
        assert report["implementation"]["status"]["grade"] == "error"
        progress = json.loads(next(reports.glob("synth/*/progress.json")).read_text("utf-8"))
        assert progress["complete"] is True

    def test_baseline_without_a_full_revision_names_the_short_one(self, synth_cli: SynthCli):
        sha = _git_project(synth_cli.project)

        # Git always resolves a ref the baseline checkout was made from, so the lookup
        # failing is simulated.
        with patch("booley.flows.implementation_flow.git_full_sha", return_value=None):
            outcome = synth_cli.run("--baseline", "HEAD", "--report-dir", "reports")

        assert outcome.exit_code == EXIT_SUCCESS
        report = json.loads((synth_cli.project / "reports/synth_lite.json").read_text("utf-8"))
        assert report["baseline"]["ref"] == sha

    def test_worktree_cleaned_up_and_workdir_restored_on_crash(self, synth_cli: SynthCli):
        """Even if baseline synth crashes, the worktree is removed and ``work_dir`` is
        restored to the real project tree."""
        _git_project(synth_cli.project)
        worktree = baseline_worktree_path(synth_cli.project, "HEAD")

        def crash(_argv: list[str]) -> SubprocessResult:
            assert worktree.exists()
            raise RuntimeError("synthesis exploded")

        synth_cli.tool_output = crash

        outcome = synth_cli.run("--baseline", "HEAD")

        assert outcome.exit_code == EXIT_ERROR
        assert "synthesis exploded" in outcome.report_text
        assert not worktree.exists()
        listing = run_bounded_child(
            ["git", "worktree", "list", "--porcelain"], cwd=synth_cli.project, check=True
        ).stdout
        assert listing.count("worktree ") == 1
        assert synth_cli.flow is not None
        assert Path(synth_cli.flow.args.work_dir) == synth_cli.project


# ===========================================================================
# Build command
# ===========================================================================


# ===========================================================================
# Flow-config trust boundary (plan P2 2026-07-05)
# ===========================================================================


# ===========================================================================
# Typed-parameter mapping (vlogdefine/vlogparam -> run_yosys_syn CLI)
# ===========================================================================


# ===========================================================================
# Synth-target sanity warnings (testbench top / SIMULATION define)
# ===========================================================================


# ===========================================================================
# FuseSoC resolution slice (ADR 0022 Phase 2)
# ===========================================================================


# ===========================================================================
# Truncation-resilient output (demoted logger noise)
# ===========================================================================


def test_command_echo_demoted_to_debug(synth_cli: SynthCli, caplog: pytest.LogCaptureFixture):
    """The full argv (one --extra-rtl per source) must not hit INFO: it sprayed into
    captured stderr and ate the MCP output budget."""
    with caplog.at_level(logging.DEBUG, logger="booley.flows.synth.flow"):
        synth_cli.run()

    info_and_up = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert not any("--extra-rtl" in m for m in info_and_up)
    # The full command stays reachable at DEBUG.
    debug_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("--extra-rtl" in m for m in debug_msgs)


def test_failure_logger_is_single_line_with_log_pointer(
    synth_cli: SynthCli, caplog: pytest.LogCaptureFixture
):
    """The no-metrics failure logs ONE single-line ERROR pointing at the persisted log:
    the multi-line excerpt already lives in the report text."""
    synth_cli.tool_output = SubprocessResult(
        returncode=1, stdout="Yosys died\n", stderr=_CORRUPT_RC
    )

    with caplog.at_level(logging.ERROR, logger="booley.flows.synth.flow"):
        synth_cli.run()

    errors = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and r.name == "booley.flows.synth.flow"
    ]
    assert len(errors) == 1
    assert "\n" not in errors[0]  # single line, no excerpt dump
    assert "run.log" in errors[0]  # pointer to the durable copy
    assert "rc=1" in errors[0]


@pytest.mark.parametrize("case", ["published", "absent", "publish-fails"])
def test_baseline_timing_reports_are_published_beside_candidate(
    synth_cli: SynthCli, case: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The baseline worktree is deleted on exit, so a published copy has to outlive it."""
    _git_project(synth_cli.project)
    # The baseline checkout holds no Booley config, so the physical recipe comes from here.
    synth_cli.resolution.resolve = _resolving(
        lambda root: _with_synth_mode(_fake_synth_resolved(root), "physical")
    )
    timing = _sta(0.5, ("clk", 4.0, 0.5, 0.1))
    baseline = {**_physical_files(timing), "reports/timing/overall.rpt": "baseline timing"}
    if case == "absent":
        baseline = {k: v for k, v in baseline.items() if not k.startswith("reports/")}
    candidate = {**_physical_files(timing), "reports/timing/overall.rpt": "candidate timing"}
    _baseline_then_candidate(synth_cli, (_ok(""), baseline), (_ok(""), candidate))
    real_publish = artifacts.publish_file

    def failing_publish(invocation_dir, components, source, **kwargs):
        if "baseline_timing" in components:
            # A full evidence disk cannot be produced on a test host.
            raise OSError("disk full")
        return real_publish(invocation_dir, components, source, **kwargs)

    with (
        caplog.at_level("WARNING", logger="booley.flows.synth.flow"),
        patch.object(
            artifacts, "publish_file", failing_publish if case == "publish-fails" else real_publish
        ),
    ):
        outcome = synth_cli.run("--baseline", "HEAD", "--report-dir", "reports")

    project = synth_cli.project
    report = json.loads((project / "reports/synth_lite.json").read_text(encoding="utf-8"))
    published = project / "reports/synth/1/artifacts/synth_lite"
    pointer = report["artifacts"].get("baseline_timing")
    assert outcome.exit_code == EXIT_SUCCESS
    assert report["implementation"]["status"]["grade"] == "pass"
    assert report["artifacts"]["timing"] == "reports/synth/1/artifacts/synth_lite/timing"
    assert (published / "timing" / "overall.rpt").read_text("utf-8") == "candidate timing"
    if case == "published":
        assert pointer == "reports/synth/1/artifacts/synth_lite/baseline_timing"
        copied = published / "baseline_timing" / "overall.rpt"
        assert copied.read_text(encoding="utf-8") == "baseline timing"
        assert "timing_unavailable" not in report["baseline"]
        return
    assert pointer is None
    assert not (published / "baseline_timing").exists()
    assert (
        report["baseline"]["timing_unavailable"]
        == {
            "absent": "baseline run produced no timing reports",
            "publish-fails": "baseline timing reports could not be published",
        }[case]
    )
    assert any("baseline synth timing" in r.getMessage() for r in caplog.records) is (
        case == "publish-fails"
    )


# A minimal stealth `.core` (authored under .booley/cores/) whose single
# rtl file is what compute_source_fingerprint hashes into the "rtl" digest.
_STEALTH_CORE_TEXT = (
    "CAPI=2:\n"
    "name: ::stealth:0\n"
    "filesets:\n"
    "  rtl:\n"
    "    files:\n"
    "      - rtl/dut.sv: {file_type: systemVerilogSource}\n"
    "targets:\n"
    "  default:\n"
    "    filesets: [rtl]\n"
)


def _author_stealth_core(root: Path, rtl_body: str, booley_dir: str = ".booley") -> None:
    """Author a stealth .core + its rtl source under ``root/<booley_dir>/cores/``."""
    cores = root / booley_dir / "cores"
    cores.mkdir(parents=True, exist_ok=True)
    (cores / "design.core").write_text(_STEALTH_CORE_TEXT, encoding="utf-8")
    rtl = cores / "rtl" / "dut.sv"
    rtl.parent.mkdir(parents=True, exist_ok=True)
    rtl.write_text(rtl_body, encoding="utf-8")


@pytest.mark.parametrize("case", ["identical", "relocated", "changed", "no-stealth-cores"])
def test_baseline_self_compare_warning(synth_cli: SynthCli, tmp_path: Path, case: str) -> None:
    body = "module dut(input logic clk); endmodule\n"
    if case in ("relocated", "changed"):
        # The run mirrors the live Booley Directory into its baseline checkout, so both
        # sides are identical by construction there; trees that differ or sit in a
        # relocated Booley Directory only meet in the guard itself.
        current, baseline = tmp_path / "cur", tmp_path / "wt"
        for root in (current, baseline):
            if case == "relocated":
                relocated_project(root)
        directory = "state" if case == "relocated" else ".booley"
        _author_stealth_core(current, body, directory)
        _author_stealth_core(
            baseline, body if case == "relocated" else body.replace("clk", "clk, rst"), directory
        )
        # Private: a real run mirrors the Booley Directory into its baseline checkout, so
        # only the guard itself can see two trees that differ or sit in a relocated one.
        warning = _baseline_self_compare_warning(current, baseline)
        if case == "relocated":
            assert not (current / ".booley").exists()
            assert warning is not None
            assert "self-comparison" in warning
        else:
            assert warning is None
        return
    _git_project(synth_cli.project)
    if case == "identical":
        _author_stealth_core(synth_cli.project, body)
    _by_target(
        synth_cli,
        {"core": _PASS_OUTPUT, "slow": _ok(_YOSYS_AREA + "STA_WORST_SLACK_NS: -0.100000\n")},
    )

    outcome = synth_cli.run("--baseline", "HEAD", target="core,slow")

    statuses = TestSummaryTable._status_by_target(outcome.report_text)
    result_line = TestSummaryTable._result_line(outcome.report_text)
    if case == "identical":
        # The self-comparison outranks the timing violation of the second Target.
        assert statuses == {"core": "WARN (baseline)", "slow": "WARN (baseline, timing)"}
        assert result_line.startswith("RESULT: WARN -- baseline delta not meaningful")
    else:
        assert statuses == {"core": "PASS", "slow": "WARN (timing)"}
        assert result_line.startswith("RESULT: WARN -- timing VIOLATED")


_TIMING_POLICY_CASES = {
    "default": ("", -2.633, EXIT_SUCCESS, "warn", {"lite": "WARN (timing)", "ok": "PASS"}),
    "on": (
        "fail_on_timing_violation = true\n",
        -2.633,
        EXIT_FAILURE,
        "fail",
        {"lite": "FAIL (timing)", "ok": "PASS"},
    ),
    "clean": (
        "fail_on_timing_violation = true\n",
        0.4,
        EXIT_SUCCESS,
        "pass",
        {"lite": "PASS", "ok": "PASS"},
    ),
}


@pytest.mark.parametrize("case", [*_TIMING_POLICY_CASES, "wrong-type"])
def test_fail_on_timing_violation_policy(synth_cli: SynthCli, case: str) -> None:
    """[flows.synth].fail_on_timing_violation: a -2.633 ns design printed
    ``RESULT: WARN -- timing VIOLATED`` and exited 0, so an rc-only consumer read it
    as success. The default stays 0, but a project with real constraints can make
    negative slack a design FAIL."""
    if case == "wrong-type":
        # A `"yes"` string must not quietly leave the gate disarmed.
        synth_cli.config('fail_on_timing_violation = "yes"\n')
        outcome = synth_cli.run()
        assert outcome.exit_code == EXIT_ERROR
        assert "fail_on_timing_violation" in outcome.report_text
        return
    toml, slack, exit_code, grade, statuses = _TIMING_POLICY_CASES[case]
    synth_cli.config(toml)
    lite = f"{_YOSYS_AREA}STA_WORST_SLACK_NS: {slack:.6f}\n" + _perclock_marker(
        "clk", 4.0, slack, None
    )
    _by_target(synth_cli, {"lite": _ok(lite), "ok": _PASS_OUTPUT})

    outcome = synth_cli.run(target="lite,ok")

    text = outcome.report_text
    assert outcome.exit_code == exit_code
    assert TestSummaryTable._status_by_target(text) == statuses
    report = synth_cli.report()
    assert report["implementation"]["status"]["grade"] == grade
    assert report["passed"] is (grade != "fail")
    if case == "default":
        assert "RESULT: WARN -- timing VIOLATED" in text
    if case == "on":
        # The verdict names the policy so nobody hunts for a structural failure that
        # does not exist.
        assert "RESULT: FAIL" in text
        assert "fail_on_timing_violation = true" in text
        assert "-2.633" in text
    if case == "clean":
        assert "RESULT: PASS" in text


_ADVISORY = "Warning: something advisory\n"
_SLANG = (Path(__file__).parent / "fixtures/slang_warning_yosys_0_70.txt").read_text("utf-8")
_ACTIONABLE_REASON = "1 actionable EDA warning occurrence(s) across 1 unique diagnostic(s)"


@pytest.mark.parametrize(
    ("case", "yosys_logs", "openroad_log"),
    [
        pytest.param(
            "benign",
            [""],
            "[WARNING STA-0503] find_timing_paths -group_count is deprecated. "
            "Use -group_path_count instead.\n",
            id="benign",
        ),
        pytest.param("frontend", [_SLANG], "", id="frontend"),
        pytest.param(
            "actionable", [_ADVISORY, _ADVISORY + _SLANG], "", id="actionable-and-frontend"
        ),
    ],
)
def test_eda_warnings_reach_the_verdict(
    synth_cli: SynthCli, case: str, yosys_logs: list[str], openroad_log: str
) -> None:
    statuses = []
    for yosys_log in yosys_logs:
        files = _physical_files(_sta(0.5, ("clk", 4.0, 0.5, 0.1)) + openroad_log)
        files["yosys.log"] += yosys_log
        outcome, report = _synthesize(synth_cli, files, mode="physical")
        status = report["implementation"]["status"]
        statuses.append({key: value for key, value in status.items() if key != "elapsed_s"})
        by_disposition = report["implementation"]["conditions"]["warning_summary"][
            "by_disposition"
        ]
        text = outcome.report_text
        assert outcome.exit_code == EXIT_SUCCESS
        if case == "actionable":
            assert "WARNING --" in text
            assert "1 actionable EDA warning(s)" in text
            assert status["grade"] == "warn"
            assert _ACTIONABLE_REASON in " ".join(status["warning_reasons"])
            continue
        assert "RESULT: PASS" in text
        assert status["grade"] == "pass"
        if case == "benign":
            assert "NOTE -- 1 EDA warning" in text
            assert by_disposition == {"benign": 1}
        else:
            assert "NOTE -- 2 EDA warnings" in text
            assert "actionable EDA warning" not in text
            assert by_disposition == {"informational": 2}
    # A frontend warning next to an actionable one leaves the status unchanged.
    assert all(status == statuses[0] for status in statuses)


class TestIncompleteResourceResults:
    def test_partial_structural_counts_are_not_critical(self):
        metrics = SynthMetrics(
            area_um2=100.0,
            cells=10,
            latches=512,
            termination="timeout",
            structural_checks_complete=False,
            ppa_complete=False,
        )
        assert metrics.has_metrics
        assert not metrics.has_critical
        assert not metrics.passed

    def test_baseline_failure_returns_before_current_matrix(self, tmp_path: Path):
        # This guards the execution-time return in ``ImplementationFlow._run_planned``:
        # it needs the baseline pass to fail after planning succeeded, and a real
        # checkout cannot make the second worktree creation fail after the first.
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--work-dir",
                str(tmp_path),
                "--report-dir",
                str(tmp_path / "reports"),
                "--target",
                "asic",
            ]
        )
        failure = EndpointOutcome(exit_code=EXIT_ERROR, report_text="baseline failed")

        with patch.object(flow, "_run_baseline_configs", return_value=(failure, None)):
            result = flow._run()

        assert result is failure

    @pytest.mark.parametrize("signalled", [False, True])
    def test_completed_target_survives_later_matrix_crash(
        self, synth_cli: SynthCli, signalled: bool
    ):
        report_dir = synth_cli.project / "reports"
        calls = 0
        published: list[dict[str, object]] = []

        def run_one(_argv: list[str]) -> SubprocessResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _PASS_OUTPUT
            if signalled:  # a signal ends the process here, without unwinding
                abort_open_progress()
                published.append(
                    json.loads(next(report_dir.glob("synth/*/progress.json")).read_text())
                )
            raise RuntimeError("simulated outer interruption")

        synth_cli.tool_output = run_one

        outcome = synth_cli.run("--report-dir", str(report_dir), target="asic_a,asic_b")

        assert outcome.exit_code == EXIT_ERROR
        assert "outer interruption" in outcome.report_text
        assert (report_dir / "synth_asic_a.json").is_file()
        invocation_dirs = sorted((report_dir / "synth").iterdir())
        progress = json.loads((invocation_dirs[-1] / "progress.json").read_text())
        assert progress["complete"] is True
        assert progress["phase"] == "aborted"
        assert progress["completed_targets"] == ["asic_a"]
        assert progress["pending_targets"] == ["asic_b"]
        assert (invocation_dirs[-1] / "targets" / "asic_a.json").is_file()
        if signalled:
            (snapshot,) = published
            assert (snapshot["phase"], snapshot["pending_targets"]) == ("aborted", ["asic_b"])


@pytest.mark.parametrize(
    ("metrics", "reason"),
    [
        (SynthMetrics(returncode=1), "rc=1, no metrics"),
        (SynthMetrics(returncode=1, cells=2, area_kge=1.0), "rc=1"),
    ],
)
def test_failure_reason_names_the_return_code(metrics: SynthMetrics, reason: str) -> None:
    assert metrics.failure_reason() == reason


_OOM_KILL = SubprocessResult(returncode=2, oom_kill_delta=1, oom_killed=True)


@pytest.mark.parametrize(
    ("mode", "results", "termination"),
    [
        pytest.param(
            "logical",
            [SubprocessResult(returncode=2, stderr="make: Error 137")],
            "resource_killed",
            id="ambiguous-137",
        ),
        pytest.param("logical", [_OOM_KILL], "oom", id="own-oom"),
        # Confirmed OOM outranks the ambiguous kill text the same run printed.
        pytest.param(
            "logical",
            [dataclasses.replace(_OOM_KILL, stderr="make: Error 137")],
            "oom",
            id="oom-with-kill-text",
        ),
        # The container's OOM counter moved, but this command's own process was not killed.
        pytest.param(
            "logical",
            [SubprocessResult(returncode=1, stdout=_YOSYS_AREA, oom_kill_delta=1)],
            "infrastructure_error",
            id="other-jobs-oom",
        ),
        pytest.param(
            "physical",
            [SubprocessResult(returncode=0), _OOM_KILL],
            "oom",
            id="openroad-oom-after-yosys",
        ),
    ],
)
def test_tool_termination_is_classified(
    synth_cli: SynthCli, mode: str, results: list[SubprocessResult], termination: str
) -> None:
    pending = list(results)
    synth_cli.config(f'synth_mode = "{mode}"\n')
    synth_cli.tool_output = lambda _argv: pending.pop(0)

    synth_cli.run()

    assert synth_cli.report()["termination"] == termination
    assert not pending


@pytest.mark.parametrize("cell", ["$_DLATCH_P_", "$_DLATCH_PN0_"])
@pytest.mark.parametrize("mode", ["logical", "physical"])
@pytest.mark.parametrize("frontend", ["sv2v", "slang"])
def test_public_flow_modern_latch_design_failure(synth_cli: SynthCli, mode, frontend, cell):
    result, report = _run_latch_case(
        synth_cli,
        {
            "mode": mode,
            "frontend": frontend,
            "cell": cell,
            "openroad": f"[ERROR ORD-2013] instance latch LEF master {cell} not found."
            if mode == "physical"
            else "",
            "process": {"returncode": 2 if mode == "physical" else 0},
        },
    )
    assert result.exit_code == EXIT_FAILURE
    assert report["returncode"] == 1
    assert report["termination"] == "design_failure"
    assert report["implementation"]["conditions"]["latches"] == 8
    assert report["implementation"]["conditions"]["unexpected_latches"] == 8
    assert report["implementation"]["conditions"]["has_critical"] is True
    assert cell in report["implementation"]["status"]["diagnostic_excerpt"]
    assert "8 latches" in result.report_text
    assert report["yosys_complete"] is True
    if mode == "physical":
        assert report["timing_complete"] is False
        assert report["ppa_complete"] is False


_LATCH_ORD = "[ERROR ORD-2013] instance latch LEF master $_DLATCH_P_ not found."


def _run_latch_case(
    synth_cli: SynthCli, case: dict[str, Any]
) -> tuple[EndpointOutcome, dict[str, Any]]:
    """Run a Target whose Yosys stat holds 8 of ``case["cell"]``; the other keys shape the run."""
    cell = case.get("cell", "$_DLATCH_P_")
    stat = (
        "Printing statistics.\n=== dut ===\n"
        f"  16 36.176 cells\n  8 - {cell}\n  Chip area for top module dut: 36.176\n"
    )
    if case.get("unknown_area"):
        stat += f"Area for cell type {cell} is unknown!\n"
    stat += case.get("yosys_decoy", "")
    files = _yosys_outputs(stat)
    if case.get("openroad"):
        files["openroad.log"] = case["openroad"]
    files.update(case.get("artifacts", {}))
    for flag, name in (
        ("missing_check", "check_dut.txt"),
        ("missing_premap", "premap_check_dut.txt"),
        ("missing_netlist", "synth_dut.v"),
    ):
        if case.get(flag):
            del files[name]
    stale = {
        name
        for flag, name in (
            ("stale", "openroad.log"),
            ("stale_check", "check_dut.txt"),
            ("stale_premap", "premap_check_dut.txt"),
        )
        if case.get(flag)
    }
    return _synthesize(
        synth_cli,
        files,
        mode=case.get("mode", "logical"),
        toml=f"expected_latches = {case.get('expected', 0)}\n" + case.get("extra_toml", ""),
        stale=stale,
        plan={"frontend": case.get("frontend", "sv2v")},
        **case.get("process", {}),
    )


@pytest.mark.parametrize("expected", [0, 1])
def test_captured_mapped_latch_physical_report(synth_cli: SynthCli, expected):
    # Mapping rows and final stat captured from the one-latch physical probe
    # using bundled Yosys 0.70+ (28ba3cb92) and pinned Nangate45 Liberty.
    captured = (Path(__file__).parent / "fixtures/mapped_latch_yosys_0_70.txt").read_text()
    yosys, stat = captured.split("--- stat_latch_probe.txt ---\n")
    result, report = _run_latch_case(
        synth_cli,
        {
            "mode": "physical",
            "expected": expected,
            "openroad": (
                "STA_WORST_SLACK_NS: 0.000000\n"
                "STA_PERCLOCK: name=clk period_ns=100.000000 wns_ns=0.000000 whs_ns=0.1 setup_same_clock=1\n"
                "Design area 63 u^2 50% utilization.\n"
                "OPENROAD_INSTANCE_COUNT: 23\n"
            ),
            "artifacts": {
                "yosys.log": yosys,
                "stat_dut.txt": stat,
                "openroad_dut.v": "module dut; endmodule\n",
                "reports/timing/overall.rpt": "timing report\n",
                "reports/timing/overall.csv.rpt": "",
            },
        },
    )
    assert result.exit_code == (EXIT_SUCCESS if expected else EXIT_FAILURE)
    conditions = report["implementation"]["conditions"]
    assert conditions["latches"] == 1
    assert conditions["expected_latches"] == expected
    assert conditions["unexpected_latches"] == 1 - expected
    assert conditions["has_critical"] is (not expected)
    assert report["yosys_complete"] is True
    assert report["structural_checks_complete"] is True
    assert report["passed"] is bool(expected)
    if expected:
        assert report["timing_complete"] is True
        assert report["ppa_complete"] is True
        assert report["cell_count"] == 23
        assert report["cell_count_source"] == "openroad_post_optimization"


@pytest.mark.parametrize("expected", [8, 9])
def test_public_latch_allowance_clean_logical_pass(synth_cli: SynthCli, expected):
    result, report = _run_latch_case(synth_cli, {"expected": expected})
    assert result.exit_code == EXIT_SUCCESS
    assert report["implementation"]["conditions"]["latches"] == 8
    assert report["implementation"]["conditions"]["unexpected_latches"] == 0
    assert report["implementation"]["conditions"]["has_critical"] is False


def test_public_latch_allowance_excess_is_design_failure(synth_cli: SynthCli):
    result, report = _run_latch_case(synth_cli, {"expected": 7})
    assert result.exit_code == EXIT_FAILURE
    assert report["termination"] == "design_failure"
    assert report["implementation"]["conditions"]["unexpected_latches"] == 1
    assert (
        "actual=8, expected=7, excess=1"
        in report["implementation"]["status"]["diagnostic_excerpt"]
    )
    reason = "8 latches (actual=8, expected=7, excess=1; types: $_DLATCH_P_=8)"
    assert report["implementation"]["status"]["failure_reasons"] == [reason]
    job = present_job_report(
        {"detail": result.detail}, endpoint="synth", exit_code=result.exit_code, argv=[]
    )
    assert ("Failure reason", reason) in [
        (row.label.strip(), row.value) for row in job.result_rows
    ]


def test_expected_latch_unknown_area_still_fails_mapping(synth_cli: SynthCli):
    result, report = _run_latch_case(synth_cli, {"expected": 8, "unknown_area": True})
    assert result.exit_code == EXIT_FAILURE
    assert report["termination"] == "design_failure"
    assert report["implementation"]["conditions"]["has_critical"] is False
    assert report["ppa_complete"] is False
    excerpt = report["implementation"]["status"]["diagnostic_excerpt"]
    assert "actual=8, expected=8, excess=0" in excerpt
    assert "Area for cell type $_DLATCH_P_ is unknown!" in excerpt


def test_expected_physical_unmapped_latch_is_noncritical_design_failure(synth_cli: SynthCli):
    result, report = _run_latch_case(
        synth_cli,
        {"mode": "physical", "expected": 8, "openroad": _LATCH_ORD, "process": {"returncode": 2}},
    )
    assert result.exit_code == EXIT_FAILURE
    assert report["returncode"] == 1
    assert report["termination"] == "design_failure"
    assert report["implementation"]["conditions"]["has_critical"] is False
    assert report["yosys_complete"] is True
    assert report["timing_complete"] is False
    assert report["ppa_complete"] is False
    assert "actual=8, expected=8, excess=0" in result.report_text
    assert "$_DLATCH_P_" in report["implementation"]["status"]["diagnostic_excerpt"]


_NO_PATH_OPENROAD_LOG = (
    "[INFO GPL-0028] Bin count (X, Y):           4 ,      4\n"
    "STA_PERCLOCK: name=clk period_ns=10.000000 wns_ns=NA whs_ns=NA setup_same_clock=1\n"
    "Design area 63 u^2 50% utilization.\n"
)


def _no_path_physical_case(expected: int, **extra) -> dict:
    """Physical latch case whose STA found no paths (empty CSV, GPL-0028 in the log)."""
    return {
        "mode": "physical",
        "expected": expected,
        "openroad": _NO_PATH_OPENROAD_LOG,
        "artifacts": {
            "openroad_dut.v": "module dut; endmodule\n",
            "reports/timing/overall.rpt": "No paths found.\n",
            "reports/timing/overall.csv.rpt": "",
        },
        **extra,
    }


def test_no_path_latch_failure_reports_no_slack_and_names_design_check(
    synth_cli: SynthCli, caplog
):
    with caplog.at_level(logging.ERROR, logger="booley.flows.synth.flow"):
        result, report = _run_latch_case(synth_cli, _no_path_physical_case(0))
    assert report["termination"] == "design_failure"
    assert result.detail["lite"]["wns_ns"] is None
    assert "wns_ns" not in report
    assert report["per_clock"]["clk"]["wns_ns"] is None
    run_log = (synth_cli.project / report["artifacts"]["log"]).read_text()
    assert "STA_WORST_SLACK_NS" not in run_log
    assert "STA_FMAX_MHZ" not in run_log
    assert "+4.000" not in result.report_text
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("design check failed: 8 latches" in m for m in messages)
    assert not any("rc=" in m for m in messages)


def test_no_path_completed_run_has_no_timing_evidence(synth_cli: SynthCli):
    """Accepted rule (#2804 Q1): no STA path means no numeric timing evidence."""
    result, report = _run_latch_case(synth_cli, _no_path_physical_case(8))
    assert result.detail["lite"]["wns_ns"] is None
    assert report["timing_complete"] is False
    assert report["ppa_complete"] is False
    assert result.exit_code == EXIT_FAILURE
    assert "+4.000" not in result.report_text


@pytest.mark.parametrize(
    ("process", "expected"),
    [
        ({"returncode": 139}, "rc=139, timed_out=False"),
        ({"returncode": -9, "timed_out": True}, "rc=-9, timed_out=True"),
    ],
)
def test_tool_failure_log_keeps_exit_code_and_timeout_state(
    synth_cli: SynthCli, caplog, process, expected
):
    with caplog.at_level(logging.ERROR, logger="booley.flows.synth.flow"):
        _run_latch_case(synth_cli, {"mode": "physical", "expected": 8, "process": process})
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(expected in m for m in messages)
    assert not any("design check failed" in m for m in messages)


@pytest.mark.parametrize(
    ("diagnostic", "log", "process"),
    [
        ("ERROR: syntax error at rtl.sv:3", "ERROR: syntax error at rtl.sv:3\n", None),
        (
            "sv2v_converted.v:3936: ERROR: syntax error, unexpected '['",
            (Path(__file__).parent / "fixtures/frontend_error_banner_yosys.txt").read_text(),
            {"returncode": 2, "stdout": "BOOLEY_STAGE: yosys\n"},
        ),
        (
            "Area for cell type $_DFF_P_ is unknown!",
            "top.sv:8: Warning: unknown character escape sequence '\\q'\n"
            'ERROR: assert property (a != 4\'hf) else $display("\\q");\n'
            "                                                ^\n"
            "Area for cell type $_DFF_P_ is unknown!\n",
            None,
        ),
    ],
    ids=["exit-zero-header", "banner-before-error", "echoed-error-before-unknown-area"],
)
def test_design_failure_reason_is_the_tool_error_line(
    synth_cli: SynthCli, caplog, diagnostic, log, process
):
    """The reason is the tool error line: not the exit-0 header, nor (issue #4897) Yosys's
    banner or an echoed source line."""
    with caplog.at_level(logging.ERROR, logger="booley.flows.synth.flow"):
        result, report = _run_latch_case(
            synth_cli,
            {
                "cell": "$_DFF_P_",
                "artifacts": {"yosys.log": log},
                **({"process": process} if process else {}),
            },
        )
    assert report["termination"] == "design_failure"
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(f"design check failed: {diagnostic}" in m for m in messages), messages
    assert not any("despite exit 0" in m for m in messages), messages
    # The status cell and RESULT line show the same reason, not the header.
    assert f"FAIL ({diagnostic})" in result.report_text
    assert f"RESULT: FAIL (lite: {diagnostic})" in result.report_text
    assert report["implementation"]["status"]["failure_reasons"] == [diagnostic]


@pytest.mark.parametrize(
    "openroad",
    [
        "[ERROR ORD-2013] instance gate LEF master NAND2_X1 not found.",
        "[ERROR STA-9999] failed unrelated timing command.",
    ],
)
def test_unrelated_openroad_failure_preserves_tool_status_and_latch_evidence(
    synth_cli: SynthCli, openroad
):
    result, report = _run_latch_case(
        synth_cli,
        {"mode": "physical", "openroad": openroad, "process": {"returncode": 2}},
    )
    assert result.exit_code == EXIT_ERROR
    assert report["returncode"] == 2
    assert report["termination"] == "infrastructure_error"
    assert report["implementation"]["conditions"]["unexpected_latches"] == 8
    assert openroad in report["infra_error"]
    assert (
        "actual=8, expected=0, excess=8"
        in report["implementation"]["status"]["diagnostic_excerpt"]
    )


def test_nonlatch_generic_master_failure_does_not_manufacture_latch_evidence(synth_cli: SynthCli):
    diagnostic = "[ERROR ORD-2013] instance flop LEF master $_DFF_P_ not found."
    result, report = _run_latch_case(
        synth_cli,
        {
            "mode": "physical",
            "cell": "$_DFF_P_",
            "openroad": diagnostic,
            "process": {"returncode": 2},
        },
    )
    assert result.exit_code == EXIT_FAILURE
    assert report["termination"] == "design_failure"
    assert report["implementation"]["conditions"]["latches"] == 0
    assert report["implementation"]["conditions"]["has_critical"] is False
    assert "unmapped generic cell" in result.report_text
    assert "latches" not in report["implementation"]["status"]["diagnostic_excerpt"]
    assert report["implementation"]["status"]["failure_reasons"] == ["unmapped generic cell"]


@pytest.mark.parametrize("stale", [False, True])
def test_incidental_or_stale_ord_cannot_reclassify_unrelated_error(synth_cli: SynthCli, stale):
    result, report = _run_latch_case(
        synth_cli,
        {
            "mode": "physical",
            "openroad": _LATCH_ORD if stale else "[ERROR STA-9999] unrelated.",
            "stale": stale,
            "yosys_decoy": _LATCH_ORD + "\n",
            "process": {"returncode": 2, "stdout": "BOOLEY_STAGE: sta\n" + _LATCH_ORD},
        },
    )
    assert result.exit_code == EXIT_ERROR
    assert report["returncode"] == 2
    assert report["termination"] == "infrastructure_error"


@pytest.mark.parametrize(
    ("case", "termination", "reasons"),
    [
        ({"process": {"returncode": 2, "timed_out": True}}, "timeout", ["timeout"]),
        (
            {"process": {"returncode": 2, "oom_kill_delta": 1, "oom_killed": True}},
            "oom",
            ["OOM"],
        ),
        ({"process": {"returncode": 137}}, "resource_killed", ["resource-killed"]),
        (
            {
                "process": {
                    "returncode": 2,
                    "stderr": "openroad: error while loading shared libraries: libx.so: missing",
                }
            },
            "infrastructure_error",
            [],
        ),
        (
            {
                "process": {
                    "returncode": 2,
                    "stdout": f"BOOLEY_EDA_FAILURE token={_TOKEN} kind=missing_eda_tool stage=openroad subject=openroad",
                }
            },
            "infrastructure_error",
            [],
        ),
        (
            {"mode": "logical", "missing_netlist": True, "process": {"returncode": 0}},
            "infrastructure_error",
            [],
        ),
    ],
)
def test_latch_and_fresh_ord_do_not_override_stronger_execution_failure(
    synth_cli: SynthCli, case, termination, reasons
):
    result, report = _run_latch_case(
        synth_cli, {"mode": "physical", "openroad": _LATCH_ORD, **case}
    )
    assert report["termination"] == termination
    assert report["implementation"]["status"]["failure_reasons"] == reasons
    assert report["implementation"]["conditions"]["unexpected_latches"] == 8
    assert result.exit_code == EXIT_ERROR
    assert report["returncode"] == 2
    assert report["infra_error"]
    assert report["implementation"]["status"]["grade"] == "error"
    assert "RESULT: ERROR —" in result.report_text
    assert report["ppa_complete"] is False
    assert report["timing_complete"] is False


def _fake_latch_eda_bin(tmp_path):
    fake_bin = tmp_path / "fake-eda"
    fake_bin.mkdir()
    scripts = {
        "sv2v": '#!/bin/sh\necho "module dut; endmodule" > sv2v_converted.v\n',
        "yosys": "#!/bin/sh\ncat > stat_dut.txt <<'STAT'\n13. Printing statistics.\n=== dut ===\n  16 36.176 cells\n  8 - $_DLATCH_P_\n  Area for cell type $_DLATCH_P_ is unknown!\nSTAT\ncat stat_dut.txt\necho 'Found and reported 0 problems.' > premap_check_dut.txt\necho 'Found and reported 0 problems.' > check_dut.txt\necho 'module dut; endmodule' > synth_dut.v\n",
        "openroad": "#!/bin/sh\necho '[ERROR ORD-2013] instance latch LEF master $_DLATCH_P_ not found.'\nexit 1\n",
    }
    for name, script in scripts.items():
        executable = fake_bin / name
        executable.write_text(script)
        executable.chmod(0o755)
    return fake_bin


def _run_generated_latch_case(
    synth_cli: SynthCli, monkeypatch: pytest.MonkeyPatch, frontend: str, mode: str
) -> tuple[EndpointOutcome, dict[str, Any]]:
    tmp_path = synth_cli.project
    liberty = tmp_path / "cells.lib"
    liberty.write_text("library(cells) {}")
    sdc = tmp_path / "timing.sdc"
    sdc.write_text("create_clock -name clk -period 4 [get_ports clk]\n")
    source = tmp_path / "dut.sv"
    source.write_text("module dut(input clk); endmodule\n")
    # The generated make recipe probes the Nangate45 physical files under $PRJ_LIB_DIR.
    pdk = tmp_path / "pdk" / "nangate45"
    pdk.mkdir(parents=True)
    for name in ("Nangate45_tech.lef", "Nangate45_stdcell.lef", "Nangate45.rc"):
        (pdk / name).write_text("")
    monkeypatch.setenv("PRJ_LIB_DIR", str(tmp_path / "pdk"))
    fake_bin = _fake_latch_eda_bin(tmp_path)

    def configure(target, _cmd):
        stub = _stub_plan(tmp_path, target, mode=mode)
        spec = dataclasses.replace(
            stub.spec,
            sources=(source,),
            liberty=liberty,
            frontend=frontend,
            timing=StaTimingConfig(mode=mode, sdc=(sdc,)),
        )
        return syn_make.configure_synthesis(spec, stub.build_dir)

    def run_make(cmd: list[str]) -> SubprocessResult:
        process = run_bounded_child(
            cmd,
            cwd=tmp_path,
            env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        return SubprocessResult(
            returncode=process.returncode, stdout=process.stdout, stderr=process.stderr
        )

    synth_cli.config(f'synth_mode = "{mode}"\n')
    synth_cli.tool_output = run_make
    # The real configure half needs sources and a Liberty file the stub Target does not stage.
    with patch.object(AsicSynthesizeFlow, "_configure_synth", side_effect=configure):
        outcome = synth_cli.run()
    return outcome, synth_cli.report()


@pytest.mark.skipif(os.name == "nt", reason="generated Makefile requires POSIX shell")
@pytest.mark.parametrize("frontend", ["sv2v", "slang"])
@pytest.mark.parametrize("mode", ["logical", "physical"])
def test_public_generated_make_latch_failure(
    synth_cli: SynthCli, monkeypatch: pytest.MonkeyPatch, frontend, mode
):
    result, report = _run_generated_latch_case(synth_cli, monkeypatch, frontend, mode)
    assert result.exit_code == EXIT_FAILURE
    assert report["returncode"] == 1
    assert report["termination"] == "design_failure"
    assert report["implementation"]["status"]["grade"] == "fail"
    assert report["implementation"]["conditions"]["latches"] == 8
    assert report["implementation"]["conditions"]["has_critical"] is True
    assert report["yosys_complete"] is True
    assert report["ppa_complete"] is False
    assert report["timing_complete"] is False
    assert "actual=8, expected=0, excess=8" in result.report_text
    assert "$_DLATCH_P_" in report["implementation"]["status"]["diagnostic_excerpt"]


def test_nonlatch_generic_failure_with_expected_latches_names_actual_failure(synth_cli: SynthCli):
    diagnostic = "[ERROR ORD-2013] instance flop LEF master $_DFF_P_ not found."
    result, report = _run_latch_case(
        synth_cli,
        {"mode": "physical", "expected": 8, "openroad": diagnostic, "process": {"returncode": 2}},
    )
    assert report["implementation"]["conditions"]["has_critical"] is False
    assert "unmapped generic cell" in result.report_text
    assert "actual=8, expected=8, excess=0" in result.report_text
    # The status cell and the RESULT line both separate the expected latches from the failure.
    reason = "unmapped generic cell; 8 latches (actual=8, expected=8, excess=0"
    assert f"FAIL ({reason}" in result.report_text
    assert f"RESULT: FAIL (lite: {reason}" in result.report_text


@pytest.mark.parametrize(
    ("tool", "diagnostic", "expected_exit", "termination"),
    [
        ("sv2v", "sv2v: unrecognized option --bogus", EXIT_ERROR, "infrastructure_error"),
        (
            "sv2v",
            "dut.sv:2:3: Parse error: unexpected token 'killed'",
            EXIT_FAILURE,
            "design_failure",
        ),
        (
            "yosys",
            "dut.sv:2:3: error: include file missing.vh not found",
            EXIT_ERROR,
            "infrastructure_error",
        ),
    ],
)
def test_1095_public_frontend_failure(
    synth_cli: SynthCli, tool, diagnostic, expected_exit, termination
):
    result, report = _synthesize(
        synth_cli,
        {f"{tool}.log": diagnostic + "\n"},
        plan={},
        returncode=2,
        stdout=f"BOOLEY_STAGE: {tool}\nmake: *** Error 1",
    )
    assert result.exit_code == expected_exit
    assert report["termination"] == termination
    assert diagnostic in result.report_text
    assert report["implementation"]["status"]["grade"] == (
        "error" if expected_exit == EXIT_ERROR else "fail"
    )
    if expected_exit == EXIT_ERROR:
        # Issue #2808: the tool prefix appears exactly once, never doubled.
        expected_reason = (
            diagnostic if diagnostic.startswith(f"{tool}:") else f"{tool}: {diagnostic}"
        )
        assert report["infra_error"] == expected_reason
        assert f"{tool}: {tool}:" not in result.report_text
        assert f"RESULT: ERROR — lite: {report['infra_error']}" in result.report_text
    else:
        assert not report["infra_error"]
        # No line is a tool error line: the reason stays the first excerpt line.
        assert f"RESULT: FAIL (lite: {diagnostic})" in result.report_text


def test_2808_missing_sv2v_reason_skips_make_and_hides_marker(
    synth_cli: SynthCli, caplog: pytest.LogCaptureFixture
):
    """Issue #2808: a missing sv2v reports its remediation, not a make line or marker."""
    marker = f"BOOLEY_EDA_FAILURE token={_TOKEN} kind=missing_eda_tool stage=sv2v subject=sv2v"
    stdout = (
        "make: Entering directory '/work/b'\n"
        "BOOLEY_STAGE: sv2v\n"
        f"{marker}\n"
        "make: *** [Makefile:12: sv2v] Error 127\n"
        "make: Leaving directory '/work/b'\n"
    )
    with caplog.at_level(logging.ERROR, logger="booley.flows.synth.flow"):
        result, report = _synthesize(synth_cli, plan={}, returncode=2, stdout=stdout)
    assert result.exit_code == EXIT_ERROR
    assert report["implementation"]["status"]["grade"] == "error"
    assert report["termination"] == "infrastructure_error"
    assert report["infra_error"].startswith("missing tool: sv2v")
    assert "RESULT: ERROR — lite: missing tool: sv2v. Rebuild" in result.report_text
    assert report["infra_error"] in result.report_text
    failed = [r.getMessage() for r in caplog.records if "boundary run failed" in r.getMessage()]
    assert len(failed) == 1
    assert "missing tool: sv2v" in failed[0]
    assert "make: Entering" not in failed[0]
    assert "BOOLEY_EDA_FAILURE" not in result.report_text
    assert "BOOLEY_EDA_FAILURE" not in report["failure_output"]
    assert "BOOLEY_EDA_FAILURE" not in caplog.text


@pytest.mark.parametrize("tool", ["sv2v", "openroad"])
def test_1095_first_diagnostic_survives_long_stage_tail(synth_cli: SynthCli, tool):
    diagnostic = (
        "sv2v: unrecognized option --bogus"
        if tool == "sv2v"
        else "[ERROR STA-9999] unknown timing failure"
    )
    marker = "sv2v" if tool == "sv2v" else "sta"
    log = (
        "Errors: 0\nReading error_model.v\nunknown cells: 0\n"
        + diagnostic
        + "\n"
        + "\n".join(f"context {i}" for i in range(40))
    )
    result, report = _synthesize(
        synth_cli,
        {f"{tool}.log": log},
        mode="logical" if tool == "sv2v" else "physical",
        plan={},
        returncode=2,
        stdout=f"BOOLEY_STAGE: {marker}\nmake: *** Error 1",
    )
    assert result.exit_code == EXIT_ERROR
    assert diagnostic in report["infra_error"]
    assert diagnostic in result.report_text
    assert "context 39" in result.report_text
    assert "context 27" not in result.report_text
    assert "make: ***" not in report["failure_output"]
    assert "PPA_PROFILE" not in report["failure_output"]


@pytest.mark.parametrize("returncode", [0, 2])
def test_1095_public_parameter_mismatch_is_design(synth_cli: SynthCli, returncode):
    files = {
        **_yosys_outputs("Number of cells: 2\n"),
        "effective_params_dut.il": "module \\dut\n  parameter \\ENABLE_ZBB 1'0\nend\n",
    }
    result, report = _synthesize(
        synth_cli,
        files,
        plan={"defines": ("ENABLE_ZBB=1",)},
        returncode=returncode,
        stdout="BOOLEY_STAGE: yosys\n",
    )
    assert result.exit_code == EXIT_FAILURE
    assert report["termination"] == "design_failure"
    assert report["implementation"]["status"]["grade"] == "fail"
    assert "effective top-level parameter mismatch" in result.report_text
    assert not report["infra_error"]


@pytest.mark.parametrize("kind", ["comparison", "fallback"])
def test_1095_aggregate_error_reasons_match_exit(flow_and_state, kind):
    from booley.flows.implementation_report import ImplementationReport

    # A comparison-basis error and an error grade with no failing Target come only from an
    # implementation report that the faked configure half cannot produce, so the
    # aggregate is built from one directly. The other aggregate error reasons (a mixed
    # design/infrastructure failure, a broken baseline) are pinned through the run in
    # TestSummaryTable and TestBaselineFlow.
    flow, _ = flow_and_state
    good = SynthMetrics(cells=2, area_kge=1)
    report = flow._implementation_report("lite", good, None, None)
    canonical = {
        **report.canonical,
        "status": {**report.canonical["status"], "grade": "error", "passed": False},
        "comparison": {
            "basis_errors": ["comparison recipe evidence missing"] if kind == "comparison" else []
        },
    }
    flow._implementation_reports = {"lite": ImplementationReport(canonical)}
    result = flow._aggregate_results(["lite"], {"lite": good}, {}, None)
    assert result.exit_code == EXIT_ERROR
    assert "RESULT: ERROR —" in result.report_text
    assert (
        "aggregate infrastructure error"
        if kind == "fallback"
        else "comparison recipe evidence missing"
    ) in result.report_text


@pytest.mark.parametrize("topology", ["standalone", "linked"])
def test_synth_baseline_materializes_project_topologies(
    synth_cli: SynthCli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, topology: str
) -> None:
    from tests.flows.test_baseline_worktree import (
        _assert_topology_baseline,
        _git,
        _project_topology_checkout,
    )

    root, revision = _project_topology_checkout(tmp_path, monkeypatch, topology)
    _git(root, "reset", "--mixed", revision)
    monkeypatch.setattr(TargetCatalog, "build", _REAL_CATALOG_BUILD)
    roots: list[Path] = []
    resolve = synth_cli.resolution.resolve

    def resolve_on_historical_checkout(handle, **kwargs):
        checkout = Path(handle.project_root)
        if checkout != root:
            _assert_topology_baseline(checkout, root, handle.selector)
            roots.append(checkout)
        return resolve(handle, **kwargs)

    synth_cli.resolution.resolve = resolve_on_historical_checkout
    cli = SynthCli(root, synth_cli.resolution, synth_cli.configured)

    outcome = cli.run("--baseline", "HEAD", target="synth_core")

    assert outcome.exit_code == EXIT_SUCCESS, outcome.report_text
    assert roots and all(not path.exists() for path in roots)
    assert [(unit.role, unit.selector) for unit in cli.run_plan.work_units] == [
        ("baseline", "synth_core"),
        ("candidate", "synth_core"),
    ]
    assert cli.flow is not None
    assert Path(cli.flow.args.work_dir) == root


@pytest.mark.parametrize(
    ("degraded", "final"),
    [
        ("final_check", "Found and reported 0 problems.\n"),
        ("final_check", "Checking module dut...\n"),
        ("final_check", None),
        ("final_check", "stale"),
        ("premap_check", "Checking module dut...\n"),
        ("premap_check", None),
        ("premap_check", "stale"),
    ],
)
def test_earlier_loop_physical_native_boundary(synth_cli: SynthCli, degraded, final):
    """Either structural check missing, stale, or unterminated gives the same outcome."""
    loop = (
        "Warning: found logic loop in module dut:\n    cell $add$rtl.sv:7$1\n    wire \\feedback\n"
    )
    artifacts = {
        "yosys.log": loop,
        "stat_dut.txt": "=== dut ===\n  16 36.176 cells\n  16 36.176 NAND2\n",
        "openroad_dut.v": "module dut; endmodule\n",
        "reports/timing/overall.rpt": "timing report\n",
        "reports/timing/overall.csv.rpt": "",
    }
    artifact_name = "check_dut.txt" if degraded == "final_check" else "premap_check_dut.txt"
    if final is not None:
        artifacts[artifact_name] = (
            "Found and reported 0 problems.\n" if final == "stale" else final
        )
    prefix = "check" if degraded == "final_check" else "premap"
    result, report = _run_latch_case(
        synth_cli,
        {
            "mode": "physical",
            "cell": "NAND2",
            "artifacts": artifacts,
            f"missing_{prefix}": final is None,
            f"stale_{prefix}": final == "stale",
            "openroad": "STA_WORST_SLACK_NS: 0.000000\nSTA_PERCLOCK: name=clk period_ns=100.000000 wns_ns=0.000000 whs_ns=0.1 setup_same_clock=1\nDesign area 63 u^2 50% utilization.\n",
        },
    )
    clean = final == "Found and reported 0 problems.\n"
    available = final not in (None, "stale")
    assert report["yosys_complete"] is available
    assert report["timing_complete"] is available
    assert report["ppa_complete"] is available
    assert report["structural_checks_complete"] is clean
    assert report["passed"] is clean
    assert result.exit_code == (
        EXIT_SUCCESS if clean else EXIT_FAILURE if available else EXIT_ERROR
    )
    assert (
        "completed structural checks find no combinational loops" in result.report_text
    ) is clean
    if clean:
        assert report["implementation"]["status"]["grade"] == "warn"
        assert "RESULT: WARN" in result.report_text
        assert report["implementation"]["conditions"]["has_critical"] is False
        assert "CRITICAL" not in result.report_text
        for evidence in ("$add$rtl.sv:7$1", "\\feedback"):
            assert evidence in result.report_text


@pytest.mark.parametrize(
    ("fixture", "exit_code", "comb_loops"),
    [
        ("comb_loop_yosys_0_70.txt", EXIT_FAILURE, 1),
        ("transient_loop_yosys_0_70.txt", EXIT_SUCCESS, 0),
    ],
    ids=["comb_loop", "transient_loop"],
)
def test_captured_comb_loop_cases_reach_verdict(
    synth_cli: SynthCli, fixture, exit_code, comb_loops
):
    # Real Yosys 0.70+ output for a loop that survives optimisation (the run
    # fails) and for a loop that optimisation removes (#1417; the run warns).
    captured = load_capture(fixture)
    result, report = _run_latch_case(
        synth_cli,
        {"cell": "NAND2", "artifacts": captured},
    )

    assert result.exit_code == exit_code
    assert report["structural_checks_complete"] is True
    conditions = report["implementation"]["conditions"]
    assert conditions["comb_loops"] == comb_loops
    assert conditions["has_critical"] is bool(comb_loops)
    if comb_loops:
        assert report["passed"] is False
        assert "CRITICAL" in result.report_text
        assert "1 comb loop" in result.report_text
        # Without the allowance knob a single loop fails, reported against expected 0.
        assert conditions["expected_comb_loops"] == 0
        assert conditions["unexpected_comb_loops"] == 1
        assert "actual=1, expected=0, excess=1" in result.report_text
        assert report["implementation"]["status"]["failure_reasons"] == [
            "1 comb loops (actual=1, expected=0, excess=1)"
        ]
    else:
        assert report["passed"] is True
        assert report["implementation"]["status"]["grade"] == "warn"
        assert "completed structural checks find no combinational loops" in (result.report_text)


@pytest.mark.parametrize(
    ("declared", "grade", "label"), [(8, "pass", "NOTE"), (7, "warn", "WARNING")]
)
def test_declared_removed_loops_pass_and_one_more_warns(
    synth_cli: SynthCli, declared, grade, label
):
    # The captured transient loop (#1417) is 8 earlier-pass warnings. Declaring
    # all 8 with expected_comb_loops ends PASS with notes; 7 still warns (#4866).
    result, report = _run_latch_case(
        synth_cli,
        {
            "cell": "NAND2",
            "extra_toml": f"expected_comb_loops = {declared}\n",
            "artifacts": load_capture("transient_loop_yosys_0_70.txt"),
        },
    )

    assert result.exit_code == EXIT_SUCCESS
    assert report["implementation"]["status"]["grade"] == grade
    assert f"RESULT: {grade.upper()}" in result.report_text
    loop_lines = [line for line in result.report_text.splitlines() if "found logic loop" in line]
    assert loop_lines
    assert all(f"{label} --" in line for line in loop_lines)
    assert all(f"(this run 8, declared {declared})" in line for line in loop_lines)


@pytest.mark.parametrize("mode", ["logical", "physical"])
@pytest.mark.parametrize("huge", [False, True])
def test_earlier_loop_report_survives_representative_saturation(
    synth_cli: SynthCli, mode: str, huge: bool
) -> None:
    padding = " x" * 2000 if huge else ""
    loop = f"Warning: found logic loop in module dut:\n    cell $add$rtl.sv:7$1{padding}\n    wire \\feedback\n"
    files = (
        _physical_files(_sta(0.25, ("clk", 2.0, 0.75, 0.1)))
        if mode == "physical"
        else _yosys_outputs(_PHYSICAL_STAT)
    )
    files["yosys.log"] = loop + "".join(f"ABC: Warning: unrelated {n}\n" for n in range(12))

    result, report = _synthesize(synth_cli, files, mode=mode, stdout=loop)

    assert result.exit_code == EXIT_SUCCESS
    assert "RESULT: WARN" in result.report_text
    assert "CRITICAL" not in result.report_text
    assert "$add$rtl.sv:7$1" in result.report_text
    assert "completed structural checks find no combinational loops" in result.report_text
    assert report["artifacts"]["log"] in result.report_text
    groups = report["implementation"]["conditions"]["warning_summary"]["representatives"]
    assert len(groups) == 8
    loop_group = groups[0]
    assert loop_group["category"] == "combinational_loop"
    assert loop_group["truncated"] is huge
    assert len(loop_group["message"].encode()) <= 2048
    assert "\\feedback" in _report_log(synth_cli, report)
    if not huge:
        assert "\\feedback" in result.report_text
    assert report["implementation"]["status"]["grade"] == "warn"


# ===========================================================================
# Per-Target summary table (#2428)
# ===========================================================================

_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_AMBER = "\x1b[38;5;208m"


class _TtyOut(io.StringIO):
    """In-memory stdout that claims to be a terminal (never read from the host)."""

    def isatty(self) -> bool:
        return True


_LONG_NAME = "a_very_long_target_name_" * 3
_VIOLATED = _ok(_YOSYS_AREA + "STA_WORST_SLACK_NS: -0.100000\n")
_ADVISORY_FILES = _stat_files(6400.0, 100, "ABC: Warning: The network has multiple outputs.\n")
_PARSE_ERROR = "dut.sv:2:3: Parse error: unexpected token 'killed'"
_DESIGN_FAILURE = (
    SubprocessResult(returncode=2, stdout="BOOLEY_STAGE: sv2v\nmake: *** Error 1"),
    {"sv2v.log": f"{_PARSE_ERROR}\n"},
)
_INFRA_FAILURE = (
    SubprocessResult(returncode=2, stdout="BOOLEY_STAGE: sv2v\nmake: *** Error 1"),
    {"sv2v.log": "sv2v: unrecognized option --bogus\n"},
)


class TestSummaryTable:
    @staticmethod
    def _force_color(monkeypatch: pytest.MonkeyPatch) -> None:
        from booley.presentation import colors

        monkeypatch.setattr(colors, "COLORS_ENABLED", True)

    @staticmethod
    def _table(report_text: str) -> tuple[str, list[str]]:
        """Return the header line and the lines under its rule up to the first blank."""
        lines = report_text.splitlines()
        starts = [i for i, line in enumerate(lines) if line.lstrip().startswith("Target ")]
        assert starts, f"no table header in:\n{report_text}"
        header = lines[starts[0]]
        assert set(lines[starts[0] + 1].replace(" ", "")) == {"─"}, "no rule under the header"
        rows: list[str] = []
        for line in lines[starts[0] + 2 :]:
            if not line.strip():
                break
            rows.append(line)
        return header, rows

    @staticmethod
    def _status_by_target(report_text: str) -> dict[str, str]:
        """Map each table row's Target to its Status cell (the row's trailing text)."""
        _, rows = TestSummaryTable._table(report_text)
        statuses: dict[str, str] = {}
        for row in rows:
            target = row.split()[0]
            # Status is the last column; its text starts at PASS/WARN/FAIL/ERROR.
            marker = min(
                (i for word in ("PASS", "WARN", "FAIL", "ERROR") if (i := row.find(word)) >= 0),
            )
            statuses[target] = row[marker:].rstrip()
        return statuses

    @staticmethod
    def _result_line(report_text: str) -> str:
        return next(ln for ln in report_text.splitlines() if ln.startswith("RESULT:"))

    @pytest.mark.parametrize("targets", [("lite",), ("lite", _LONG_NAME)], ids=["one", "two"])
    def test_targets_share_one_aligned_table(
        self, synth_cli: SynthCli, targets: tuple[str, ...]
    ) -> None:
        runs: dict[str, _Run] = {"lite": _PASS_OUTPUT}
        runs[_LONG_NAME] = _ok(_YOSYS_AREA.replace("6400.0", "1200.0"))
        _by_target(synth_cli, runs)

        outcome = synth_cli.run(target=",".join(targets))

        header, rows = self._table(outcome.report_text)
        assert len(rows) == len(targets)
        area_col = header.index("Area")
        assert rows[0][area_col:].startswith("8.0 kGE")
        assert "Time" in header and "Status" in header
        assert "Δ" not in header, "no baseline, no delta columns"
        if len(targets) == 2:
            assert rows[1][area_col:].startswith("1.5 kGE")
            name_cell = rows[1][:area_col].strip()
            assert len(name_cell) <= 40 and name_cell.endswith("…")
        # The invocation report records the EDA tool, or the tool of each Target.
        invocation = json.loads(
            (synth_cli.project / "flow-reports/synth.json").read_text(encoding="utf-8")
        )
        expected = (
            {"eda_tool": "yosys"}
            if len(targets) == 1
            else {"eda_tools": dict.fromkeys(targets, "yosys")}
        )
        assert {
            key: invocation[key] for key in ("eda_tool", "eda_tools") if key in invocation
        } == (expected)

    def test_tty_table_is_coloured_once_and_record_is_plain(
        self, synth_cli: SynthCli, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.presentation.colors import strip_ansi

        self._force_color(monkeypatch)
        out = _TtyOut()
        monkeypatch.setattr("sys.stdout", out)
        _by_target(
            synth_cli,
            {
                "lite": _PASS_OUTPUT,
                "slow": _VIOLATED,
                "latched": _ok(
                    _YOSYS_AREA + "$dlatch cell A\n$dlatch cell B\nCombinational loop detected\n"
                ),
                "dead": SubprocessResult(returncode=2, stdout="", stderr="boom"),
            },
        )

        outcome = synth_cli.run(target="lite,slow,latched,dead")

        printed = out.getvalue()
        text = outcome.report_text
        statuses = self._status_by_target(text)
        assert (statuses["lite"], statuses["slow"]) == ("PASS", "WARN (timing)")
        assert statuses["latched"].startswith("FAIL") and statuses["dead"] == "ERROR"
        _, rows = self._table(text)
        assert "—" in rows[3]
        assert f"{_GREEN}PASS\x1b[0m" in printed
        assert f"{_AMBER}WARN (timing)\x1b[0m" in printed
        assert f"{_RED}ERROR\x1b[0m" in printed
        assert f"{_RED}CRITICAL\x1b[0m" in printed
        assert "\x1b" not in text
        # The console record is the report text up to the memory-admission note.
        assert strip_ansi(printed).rstrip() == text.partition("No memory measurement")[0].rstrip()
        assert printed.count("RESULT:") == 1
        # Notes follow the table, without the Flow prefix, up to the RESULT line.
        lines = text.splitlines()
        assert not [line for line in lines if line.lstrip().startswith("[synth]")]
        notes = lines[
            lines.index(rows[-1]) + 1 : max(
                i for i, ln in enumerate(lines) if ln.startswith("RESULT:")
            )
        ]
        assert any("latched: CRITICAL --" in line for line in notes)
        pointer = "flow-reports/synth/1/artifacts/synth_latched/run.log"
        assert any(line.strip() == f"latched: log: {pointer}" for line in notes)

    @pytest.mark.parametrize(
        ("runs", "expected_rows", "verdict"),
        [
            pytest.param(
                {"core": _PASS_OUTPUT, "edge": _PASS_OUTPUT},
                {"core": "PASS", "edge": "PASS"},
                "PASS",
                id="pass",
            ),
            pytest.param(
                {
                    "core": _PASS_OUTPUT,
                    "slow": _VIOLATED,
                    "noisy": (_PASS_OUTPUT, _ADVISORY_FILES),
                },
                {"core": "PASS", "slow": "WARN (timing)", "noisy": "WARN (EDA warnings)"},
                "WARN",
                id="warn",
            ),
            pytest.param(
                {"core": _PASS_OUTPUT, "broken": _DESIGN_FAILURE},
                {"core": "PASS", "broken": f"FAIL ({_PARSE_ERROR})"},
                "FAIL",
                id="fail",
            ),
            pytest.param(
                {"broken": _DESIGN_FAILURE, "slow": _VIOLATED},
                {"broken": f"FAIL ({_PARSE_ERROR})", "slow": "WARN (timing)"},
                "FAIL",
                id="fail-with-warn-elsewhere",
            ),
            pytest.param(
                {"lite": _DESIGN_FAILURE, "full": _INFRA_FAILURE},
                {"lite": f"FAIL ({_PARSE_ERROR})", "full": "ERROR"},
                "ERROR",
                id="mixed-error",
            ),
        ],
    )
    def test_row_statuses_agree_with_result_reasons(
        self,
        synth_cli: SynthCli,
        runs: dict[str, _Run],
        expected_rows: dict[str, str],
        verdict: str,
    ) -> None:
        _by_target(synth_cli, runs)

        outcome = synth_cli.run(target=",".join(runs))

        statuses = self._status_by_target(outcome.report_text)
        assert statuses == expected_rows
        result_line = self._result_line(outcome.report_text)
        assert result_line.startswith(f"RESULT: {verdict}")
        for target, status in statuses.items():
            if f"{target}:" in result_line and verdict != "ERROR":
                # A Target the RESULT line names carries the RESULT verdict word.
                assert status.startswith(verdict), (target, status, result_line)
            if status == "PASS":
                assert f"{target}:" not in result_line
        assert outcome.detail["targets"] == list(runs)
        assert {name: outcome.detail[name]["passed"] for name in runs} == {
            name: status.startswith(("PASS", "WARN")) for name, status in statuses.items()
        }
        if verdict == "ERROR":
            assert "lite:" in result_line and "full:" in result_line
            assert "unrecognized option" in result_line


def _loop_check_artifacts(loops: int) -> dict[str, str]:
    """Pre-mapping and final check text reporting *loops* distinct logic loops."""
    records = "".join(
        f"Warning: found logic loop in module dut:\n    cell $not$loop{n} ($not)\n"
        f"      A[0] --> Y[0]\n    wire \\feedback{n}\n"
        for n in range(loops)
    )
    check = f"Checking module dut...\n{records}Found and reported {loops} problems.\n"
    return {"premap_check_dut.txt": check, "check_dut.txt": check}


def test_declared_comb_loop_allowance_passes_and_excess_fails(synth_cli: SynthCli):
    """``expected_comb_loops = N`` passes N loops; N + 1 fails naming the excess."""
    result, report = _run_latch_case(
        synth_cli,
        {
            "cell": "NAND2",
            "extra_toml": "expected_comb_loops = 2\n",
            "artifacts": _loop_check_artifacts(2),
        },
    )
    assert result.exit_code == EXIT_SUCCESS
    assert report["passed"] is True
    conditions = report["implementation"]["conditions"]
    assert conditions["comb_loops"] == 2
    assert conditions["expected_comb_loops"] == 2
    assert conditions["unexpected_comb_loops"] == 0
    assert conditions["has_critical"] is False
    # A declared loop still emits the Yosys warning, so the run grades WARN.
    assert report["implementation"]["status"]["grade"] == "warn"

    result, report = _run_latch_case(
        synth_cli,
        {
            "cell": "NAND2",
            "extra_toml": "expected_comb_loops = 1\n",
            "artifacts": _loop_check_artifacts(2),
        },
    )
    assert result.exit_code == EXIT_FAILURE
    assert report["termination"] == "design_failure"
    conditions = report["implementation"]["conditions"]
    assert conditions["unexpected_comb_loops"] == 1
    assert conditions["has_critical"] is True
    assert "actual=2, expected=1, excess=1" in result.report_text
    assert (
        "actual=2, expected=1, excess=1"
        in report["implementation"]["status"]["diagnostic_excerpt"]
    )

    # The verdict uses the larger of the two checks: a loop only the pre-mapping
    # check reports still counts against the allowance.
    artifacts = {
        "premap_check_dut.txt": _loop_check_artifacts(2)["premap_check_dut.txt"],
        "check_dut.txt": _loop_check_artifacts(0)["check_dut.txt"],
    }
    result, report = _run_latch_case(
        synth_cli,
        {"cell": "NAND2", "extra_toml": "expected_comb_loops = 1\n", "artifacts": artifacts},
    )
    assert result.exit_code == EXIT_FAILURE
    assert "actual=2, expected=1, excess=1" in result.report_text


@pytest.mark.parametrize("case", ["plain", "baseline"])
def test_synth_output_matches_golden(
    case: str, state_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One Target's report, progress record, RESULT line and exit code stay as pinned."""
    # The golden pins the shared implementation-parity output for fixed metrics and a
    # fixed revision (`a` * 40): a real run's metrics and revision would change it, so
    # the run is driven below the public boundary, with these private names.
    from tests.flows.implementation_golden_support import render_implementation_output
    from tests.golden.conftest import assert_matches_golden

    monkeypatch.setenv("BOOLEY_RUN_ID", "golden-run")
    flow = AsicSynthesizeFlow()
    args = ["--target", "lite", "--work-dir", str(tmp_path)]
    args += ["--report-dir", str(tmp_path / "reports")]
    if case == "baseline":
        args += ["--baseline", "v1.0"]
    flow.parse_args(args)
    baseline = SynthMetrics(area_kge=12.0, cells=100, wns_ns=0.5, elapsed_s=3.0)
    current = SynthMetrics(area_kge=12.3, cells=110, wns_ns=0.4, elapsed_s=4.0)

    def run_one(_target: str) -> tuple[SynthMetrics, str]:
        return (baseline if flow._execution_role == "baseline" else current), "done"

    with (
        patch("booley.flows.synth.flow.baseline_worktree", _fake_baseline_worktree),
        patch("booley.flows.implementation_flow.baseline_worktree", _fake_baseline_worktree),
        patch("booley.flows.implementation_flow.git_short_sha", return_value="abc1234"),
        patch("booley.flows.synth.flow.git_full_sha", return_value="a" * 40),
        patch("booley.flows.implementation_flow.git_full_sha", return_value="a" * 40),
        patch.object(flow, "_run_single_config", side_effect=run_one),
    ):
        outcome = flow._run()

    text = render_implementation_output(flow, "synth_lite.json", outcome, tmp_path)
    assert_matches_golden(f"implementation/synth_{case}.txt", text)
