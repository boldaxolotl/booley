"""Tests for the Mutation Flow — deterministic Mutants + sim sweep.

Covers argparse, the default scope and threshold, the baseline-scaled timeout,
the reproducibility identity and the sweep, with the real Mutant generator and
fake elaboration and simulation results.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.endpoint_events import _display_safe_event, _endpoint_end_event
from booley.flows.mutation.flow import (
    MutationFlow,
    MutationResult,
    MutationSpec,
    MutationSummary,
    MutationTestRun,
    UnsupportedSimTargetError,
    generate_results_markdown,
    generate_specs_markdown,
)
from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.build import PreparedSimulationBuild, SimulationBuildPreparationError
from booley.flows.sim.execution.composition import prepare_adapter_invocation
from booley.flows.target_test_suite import NoRunnableTestsError
from booley.fusesoc import fusesoc_registry
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    EndpointOutcome,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import IncompatibleTargetError
from tests.conftest import require_symlinks, symlink_or_skip
from tests.flows.sim.launched_work import launched_work
from tests.target_test_support import write_two_core_sim_project

# The parameters the fake ``.core`` Target declares; a test may add plusargs.
_TARGET_PARAMETERS: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("removed", ["--tb-top", "--dut-top", "--dut-files"])
def test_removed_topology_options_are_rejected(removed: str) -> None:
    endpoint = MutationFlow()
    with pytest.raises(SystemExit):
        endpoint.parse_args(["--target", "sim", "--scope", "rtl/design.sv", removed, "legacy"])


def _env_with_state(
    state_file: Path,
    logs_dir: Path,
    slug: str = "test-ticket",
) -> dict[str, str]:
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = slug
    env["BOOLEY_STATE_FILE"] = str(state_file)
    env["BOOLEY_LOGS_DIR"] = str(logs_dir)
    return env


def _make_state(tmp_path: Path) -> Path:
    """Create the state file used by mutation endpoint tests."""
    state_file = tmp_path / "state.json"
    state = DevelopmentState.load(state_file)
    state.save()
    return state_file


def _write_dut_top(tmp_path: Path, top: str = "design_top") -> Path:
    """Drop a minimal SV file containing ``module <top>``."""
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir(parents=True, exist_ok=True)
    f = rtl_dir / f"{top}.sv"
    f.write_text(
        f"module {top}(input logic clk, input logic rst);\n"
        f"  logic [3:0] r;\n"
        f"  always_ff @(posedge clk) r <= r + 1;\n"
        f"endmodule\n",
        encoding="utf-8",
    )
    return f


def _write_tests_toml(root: Path, text: str) -> None:
    """Give the checkout at *root* a Project directory with this ``tests.toml``."""
    project = root / ".booley"
    project.mkdir(exist_ok=True)
    (project / "booley.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (project / "tests.toml").write_text(text, encoding="utf-8")


def _make_endpoint(
    tmp_path: Path,
    monkeypatch=None,
    *,
    target: str = "lite",
    scope: str | None = "rtl/mod_a.sv,rtl/mod_b.sv",
    tb_top: str = "design_top_tb",
    count: int | str | None = 10,
    min_detected: int | None = None,
    seed: int | None = None,
    dut_top_module: str = "design_top",
    dut_files: tuple[str, ...] = ("rtl/design_top.sv",),
    extra_args: list[str] | None = None,
) -> MutationFlow:
    """Build a endpoint with parsed args and loaded state.

    When *monkeypatch* is supplied, the BOOLEY_LOGS_DIR env var is set for
    the lifetime of the test so the run's scratch files land in tmp_path.
    """
    state_file = _make_state(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    report_dir = tmp_path / "reports"
    argv = [
        "--work-dir",
        str(tmp_path),
        "--report-dir",
        str(report_dir),
        "--target",
        target,
    ]
    if scope is not None:
        argv.extend(["--scope", scope])
    if count is not None:
        argv.extend(["--count", str(count)])
    if min_detected is not None:
        argv.extend(["--min-detected", str(min_detected)])
    if seed is not None:
        argv.extend(["--seed", str(seed)])
    if extra_args:
        argv.extend(extra_args)
    env = _env_with_state(state_file, logs_dir)
    if monkeypatch is not None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    endpoint = MutationFlow()
    with patch.dict(os.environ, env):
        endpoint.parse_args(argv)
    # Most focused unit tests do not author a FuseSoC catalog. Preserve their
    # isolation while production code derives both values from --target.
    endpoint._dut_files = lambda: list(dut_files)  # type: ignore[method-assign]
    if monkeypatch is not None:
        monkeypatch.setattr(
            "booley.flows.mutation.flow.tb_top_for_target",
            lambda *_args, **_kwargs: tb_top,
        )
    del dut_top_module
    endpoint.read_state()
    return endpoint


def _sample_specs(n: int = 3, category: str = "operator_change") -> list[MutationSpec]:
    return [
        MutationSpec(
            index=i,
            category=category,
            file="rtl/mod_a.sv",
            line=i + 1,
            original_code=f"a + b_{i}",
            mutated_code=f"a - b_{i}",
            mutant_id=f"id{i}",
        )
        for i in range(1, n + 1)
    ]


def test_run_rejects_target_with_every_test_skipped(tmp_path: Path, monkeypatch) -> None:
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    _patch_resolve_target(monkeypatch)
    with (
        patch.object(endpoint, "_validate_scope_against_target", return_value=None),
        patch.object(endpoint, "_validate_target_runner"),
        patch.object(endpoint, "cocotb_target", return_value=None),
        patch(
            "booley.flows.mutation.flow.require_runnable_target_test_suite",
            side_effect=NoRunnableTestsError("sim", ("smoke", "corner")),
        ),
    ):
        result = endpoint._run()

    assert result.exit_code == EXIT_ERROR
    assert "no runnable tests" in result.report_text


def test_two_keys_for_one_target_refuse_naming_both(tmp_path: Path, monkeypatch) -> None:
    write_two_core_sim_project(
        tmp_path, '["y#sim"]\ntests = ["one"]\n["vendor:b:y#sim"]\ntests = ["two"]\n'
    )
    endpoint = _make_endpoint(tmp_path, monkeypatch, target="y#sim")

    result = endpoint._apply_campaign_defaults()

    assert result is not None and result.exit_code == EXIT_ERROR
    assert "[vendor:b:y#sim] and [y#sim]" in result.report_text


_SETTINGS = {"scope": ["x.v"], "total": 7, "min_detected": 5}
_CORE_A_FIRST = "vendor:a:x:1.0#first"
_TWO_CORE_TESTS = (
    f'["{_CORE_A_FIRST}"]\ntests = ["one"]\n["vendor:b:y:1.0#first"]\ntests = ["one"]\n'
)


def _two_core_endpoint(
    tmp_path: Path,
    monkeypatch,
    criteria: dict[str, dict[str, Any]],
    *,
    target: str = _CORE_A_FIRST,
    scope: str | None = "x.v",
    strict: bool = False,
) -> MutationFlow:
    """Endpoint on a two-core Project (both declare ``first``) holding *criteria*."""
    write_two_core_sim_project(tmp_path, _TWO_CORE_TESTS, target="first")
    endpoint = _make_endpoint(tmp_path, monkeypatch, target=target, scope=scope, count=None)
    endpoint.state.init_criteria(
        dict.fromkeys(criteria, False), criterion_params=criteria, strict=strict
    )
    return endpoint


@pytest.mark.parametrize("scope", ["x.v", None], ids=["explicit-scope", "criterion-scope"])
def test_qualified_target_applies_criterion_settings(
    tmp_path: Path, monkeypatch, scope: str | None
) -> None:
    """``--target vlnv#name`` reads the Target-name criterion, not a core-bound sibling."""
    endpoint = _two_core_endpoint(
        tmp_path,
        monkeypatch,
        {
            "mutation_score_first": dict(_SETTINGS),
            "mutation_score_otherfirst": {
                "target": "vendor:b:y:1.0#first",
                "scope": ["y.v"],
                "total": 3,
            },
        },
        scope=scope,
    )

    assert endpoint._apply_campaign_defaults() is None
    assert (endpoint.args.scope, endpoint.args.count, endpoint.args.min_detected) == (
        "x.v",
        7,
        5,
    )


@pytest.mark.parametrize("case", ["exact-key", "other-core-binding"])
def test_qualified_target_criterion_resolution_guards(
    tmp_path: Path, monkeypatch, case: str
) -> None:
    """The exact spelling still wins; a criterion bound to another core is skipped."""
    if case == "exact-key":
        endpoint = _two_core_endpoint(
            tmp_path,
            monkeypatch,
            {
                f"mutation_score_{_CORE_A_FIRST}": {"target": "same", **_SETTINGS},
                "mutation_score_first": {"scope": ["x.v"], "total": 3},
            },
            strict=True,
        )
        expected = ("x.v", 7, 5)
    else:
        endpoint = _two_core_endpoint(
            tmp_path,
            monkeypatch,
            {"mutation_score_first": {"target": _CORE_A_FIRST, **_SETTINGS}},
            target="vendor:b:y:1.0#first",
            scope="y.v",
            strict=True,
        )
        expected = ("y.v", 10, None)

    assert endpoint._apply_campaign_defaults() is None
    assert (endpoint.args.scope, endpoint.args.count, endpoint.args.min_detected) == expected


def test_qualified_target_without_any_scope_defaults_to_its_own_rtl(
    tmp_path: Path, monkeypatch
) -> None:
    """A criterion that declares no scope leaves the scope to the Target's own RTL."""
    endpoint = _two_core_endpoint(
        tmp_path, monkeypatch, {"mutation_score_first": {"total": 7}}, scope=None
    )

    assert endpoint._apply_campaign_defaults() is None
    assert (endpoint.args.scope, endpoint.args.count) == ("x.v", 7)


def _eighty_percent(count: int) -> int:
    """``ceil(0.8 * count)`` in integers (float ``ceil(0.8 * 15)`` gives 13)."""
    return -(-4 * count // 5)


def _fake_proc(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class TestMutationSummary:
    def test_counts(self):
        s = MutationSummary(
            specs=_sample_specs(3),
            results=[
                MutationResult(index=1, detected=True),
                MutationResult(index=2, detected=False),
                MutationResult(index=3, detected=False, invalid=True),
            ],
        )
        assert s.detected_count == 1
        assert s.not_detected_count == 1
        assert s.invalid_count == 1

    def test_classify(self):
        s = MutationSummary(
            specs=_sample_specs(2),
            results=[MutationResult(index=1, detected=True)],
        )
        c = s.classify()
        assert c[0]["status"] == "detected"
        assert c[1]["status"] == "untested"


def test_dry_run_does_not_generate_or_build_anything(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scope = "rtl/mod_a.sv"
    _prepare_scope_files(tmp_path, [scope])
    endpoint = _make_endpoint(
        tmp_path,
        monkeypatch,
        scope=scope,
        extra_args=["--dry-run"],
    )
    endpoint._target_campaign = SimpleNamespace(
        execution_units=lambda: (),
    )
    with (
        patch.object(endpoint, "_apply_campaign_defaults", return_value=None),
        patch.object(endpoint, "_validate_scope_against_target", return_value=None),
        patch.object(endpoint, "_validate_target_runner"),
        patch.object(endpoint, "cocotb_target"),
        patch.object(endpoint, "_target_test_suite"),
        patch("booley.flows.mutation.flow.subprocess.run") as build_or_run,
    ):
        result = endpoint._run()

    assert result.exit_code == EXIT_SUCCESS
    assert result.detail["mode"] == "dry_run"
    build_or_run.assert_not_called()


def _run_count_preview(endpoint, monkeypatch, *, params=None):
    """Use real count resolution while isolating Target discovery and execution."""
    campaign = SimpleNamespace(
        scope_arg=endpoint.args.scope,
        params_for=lambda _name: params or {},
        execution_units=lambda: (),
    )
    monkeypatch.setattr(
        "booley.flows.mutation.flow.resolve_target_campaign", lambda *a, **kw: campaign
    )
    with (
        patch.object(endpoint, "_registered_tests", return_value={}),
        patch.object(endpoint, "_validate_scope_against_target", return_value=None),
        patch.object(endpoint, "_validate_target_runner"),
        patch.object(endpoint, "cocotb_target"),
        patch.object(endpoint, "_target_test_suite"),
        patch.object(endpoint, "emit_progress") as progress,
        patch("booley.flows.mutation.flow.subprocess.run") as build_or_run,
    ):
        before = set(endpoint.args.work_dir.rglob("*"))
        result = endpoint._run()
        assert set(endpoint.args.work_dir.rglob("*")) == before
    build_or_run.assert_not_called()
    return result, progress


def _assert_preview_lines(result, count):
    assert result.exit_code == EXIT_SUCCESS
    assert result.detail["mode"] == "dry_run"
    assert result.detail["count"] == count
    assert result.report_text == "\n".join(result.display_lines)
    assert len(result.display_lines) > 1
    assert all("\n" not in line for line in result.display_lines)
    assert result.display_lines[-1].startswith("mutation dry-run:")
    assert result.display_lines[-1].endswith(
        f"count={count}; min_detected={_eighty_percent(count)}; seed=0"
    )


@pytest.mark.parametrize(
    "sizes,timeout,count", [((4, 1), 1800, 3), ((100, 125), 1800, 12)], ids=["small", "time-cap"]
)
def test_auto_dry_run_explains_source_size_count(tmp_path, monkeypatch, sizes, timeout, count):
    for name, lines in zip(("mod_a.sv", "mod_b.sv"), sizes, strict=True):
        source = tmp_path / "rtl" / name
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(b"// source\n" * lines)
    endpoint = _make_endpoint(
        tmp_path,
        monkeypatch,
        count="auto",
        extra_args=["--dry-run", "--timeout-ms", str(timeout * 1000)],
    )
    result, _ = _run_count_preview(endpoint, monkeypatch)
    _assert_preview_lines(result, count)
    for name, lines in zip(("mod_a.sv", "mod_b.sv"), sizes, strict=True):
        assert f"rtl/{name}: {lines} lines ({10 * lines} bytes)" in result.report_text
    assert f"total: {sum(sizes)} lines" in result.report_text
    assert f"time cap: max(3, {timeout}s // 150) = 12" in result.report_text
    assert f") = {count}" in result.display_lines[-2]


def test_fixed_dry_run_identifies_given_count(tmp_path, monkeypatch):
    for name in ("mod_a.sv", "mod_b.sv"):
        source = tmp_path / "rtl" / name
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(b"// source\n")
    endpoint = _make_endpoint(tmp_path, monkeypatch, count=7, extra_args=["--dry-run"])
    result, _ = _run_count_preview(endpoint, monkeypatch)
    _assert_preview_lines(result, 7)
    assert "count source: fixed; count=7 (not auto-derived)" in result.report_text
    assert "source-size breakdown" not in result.report_text


def test_auto_dry_run_keeps_complete_long_scope_report(tmp_path, monkeypatch):
    paths = [f"rtl/module_{i:02d}_{'x' * 130}.sv" for i in range(20)]
    for path in paths:
        source = tmp_path / path
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(b"// source\n" * 4)
    endpoint = _make_endpoint(
        tmp_path, monkeypatch, count="auto", scope=",".join(paths), extra_args=["--dry-run"]
    )
    result, _ = _run_count_preview(endpoint, monkeypatch)
    event = _display_safe_event(_endpoint_end_event("mutation", "lite", result, 0.0, dry_run=True))
    assert event["display_lines_truncated"] > 0
    assert event["summary"] == result.display_lines[-1]


# ---------------------------------------------------------------------------
# Endpoint metadata
# ---------------------------------------------------------------------------


class TestToolMetadata:
    def test_name(self):
        assert MutationFlow().name == "mutation"

    def test_satisfies(self):
        assert "mutation_score" in MutationFlow().satisfies


# ---------------------------------------------------------------------------
# Artifact generation
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_specs_markdown(self):
        md = generate_specs_markdown(_sample_specs(2))
        assert "# Mutation Specifications" in md
        assert "operator_change" in md

    def test_results_link_mutated_rtl_and_escape_rtl_operators(self):
        spec = MutationSpec(
            index=1,
            category="operator_change",
            file="rtl/mod (wide).sv",
            line=12,
            original_code="a | b",
            mutated_code="a & b",
        )
        summary = MutationSummary(
            specs=[spec],
            results=[MutationResult(index=1, detected=False)],
        )

        md = generate_results_markdown(
            summary,
            1,
            variant_paths={1: "variants/mutant_1/rtl/mod (wide).sv"},
        )

        assert "[rtl/mod (wide).sv:12](variants/mutant_1/rtl/mod%20%28wide%29.sv)" in md
        assert "`a \\| b` → `a & b`" in md
        assert "not detected" in md

    def test_results_markdown_names_the_first_killing_test(self):
        spec = _sample_specs(1)[0]
        summary = MutationSummary(
            specs=[spec],
            results=[
                MutationResult(
                    index=1,
                    detected=True,
                    first_killing_test="test_rx_overrun",
                )
            ],
        )

        md = generate_results_markdown(summary, 1)

        assert "test_rx_overrun" in md


# ---------------------------------------------------------------------------
# Cold-start development with mocked sim
# ---------------------------------------------------------------------------


def _fake_inspection(work_dir) -> SimpleNamespace:
    """What the fake catalog reports: every ``rtl/*.sv`` is an untagged RTL input."""
    from booley.targets.domain import TargetInput

    paths = sorted(
        path.relative_to(work_dir).as_posix() for path in (Path(work_dir) / "rtl").glob("*.sv")
    )
    return SimpleNamespace(
        parameters=dict(_TARGET_PARAMETERS),
        rtl_files=tuple(paths),
        inputs=tuple(
            TargetInput(path, "acme:test:fixture:1", "systemVerilogSource", (), False, {})
            for path in paths
        ),
    )


_VERILATOR_VERSION = "Verilator 5.052 2025-01-01 rev v5.052"


def _patch_resolve_target(
    monkeypatch, *, eda_tool: str | None = "verilator", verilator: str = _VERILATOR_VERSION
):
    """Stub catalog selection and handle resolution — no real FuseSoC (Unit A.3).

    Returns a fake ResolvedTarget whose ``build_root`` is the requested build
    dir, so ``_run_elab`` derives the make/bin-dir from it and writes its
    marker without spawning the FuseSoC CLI.
    """
    import types

    def _fake_select(work_dir, target, *, for_flow=None):
        return types.SimpleNamespace(
            selector=target,
            name=target,
            identity=f"::{target}:0#{target}",
            vlnv=f"::{target}:0",
            project_root=Path(work_dir),
            eda_tool=eda_tool,
            cocotb_module=None,
        )

    def _fake_prepare(handle, *, build_root, lane_kind, **kwargs):
        assert lane_kind == "unreserved"
        import yaml

        root = Path(build_root)
        root.mkdir(parents=True, exist_ok=True)
        project = Path(handle.project_root)
        core_file = project / "fixture.core"
        sources = sorted((project / "rtl").glob("*.sv"))
        core_file.write_text(
            "CAPI=2:\n"
            + yaml.safe_dump(
                {
                    "name": "acme:test:fixture:1",
                    "filesets": {
                        "rtl": {"files": [str(path.relative_to(project)) for path in sources]}
                    },
                    "targets": {},
                }
            )
        )
        entries = []
        for source in sources:
            name = "src/acme_test_fixture_1/" + str(source.relative_to(project))
            staged = root / name
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(source.read_bytes())
            entries.append({"name": name, "core": "acme:test:fixture:1"})
            entries[-1]["file_type"] = "systemVerilogSource"
        edam_path = root / "fixture.eda.yml"
        edam_path.write_text(
            yaml.safe_dump(
                {
                    "cores": {"acme:test:fixture:1": {"core_file": str(core_file)}},
                    "files": entries,
                }
            )
        )
        resolved = types.SimpleNamespace(
            edam_path=edam_path,
            build_root=Path(build_root),
            toplevel="tb",
            eda_tool=eda_tool,
            configured_eda_tool=eda_tool,
            files=(),
        )
        return PreparedSimulationBuild(
            target=handle.selector,
            target_identity=getattr(
                handle, "identity", f"::{handle.selector}:0#{handle.selector}"
            ),
            resolved=resolved,
            work_root=Path(build_root),
            build_root=Path(build_root),
            eda_tool=eda_tool or "verilator",
            toplevel="tb",
            make_argv=("make", "-C", str(build_root)),
        )

    monkeypatch.setattr(
        TargetCatalog,
        "build",
        classmethod(
            lambda _cls, work_dir: type(
                "FakeCatalog",
                (),
                {
                    "select": lambda _self, target, **kwargs: _fake_select(
                        work_dir, target, **kwargs
                    ),
                    "core_closure": lambda _self, _handles: frozenset(),
                    "inspect": lambda _self, _handle: _fake_inspection(work_dir),
                },
            )()
        ),
    )
    monkeypatch.setattr(
        "booley.flows.mutation.flow.prepare_simulation_build",
        _fake_prepare,
    )
    # No faked test spawns a real `verilator`: the baseline build's version is `verilator`.
    monkeypatch.setattr(
        "booley.flows.mutation.flow.verilator_version",
        lambda _environment: verilator,
        raising=False,  # the node ids of the criterion tests must fail in their bodies on main
    )


@dataclass
class FakeSimulation:
    """What a faked run asked the simulator to do: builds and adapter runs, in order."""

    builds: list[int | None] = field(default_factory=list)  # Mutant index; None: baseline
    runs: list[int | None] = field(default_factory=list)
    run_timeouts: list[float] = field(default_factory=list)


def _mutant_index(text: str) -> int | None:
    """The Mutant a build directory or launched work names, or ``None`` for the baseline."""
    # Anchored on the builds directory: a test's own name may end in `mutant_<digit>`.
    match = re.search(r"builds[/\\]mutant_(\d+)", text)
    return int(match.group(1)) if match else None


def _write_fake_verilation(build_root: Path) -> None:
    """What a Verilator build leaves in its root: a dependency file and the RTL's C++.

    The C++ names each staged source by its absolute path, as ``VL_STOP_MT`` does for a
    design that calls ``$error``, and holds the source without the lines marked ``// dead``,
    the code a default assignment that every branch overwrites compiles away.
    """
    (build_root / "Vtb__ver.d").write_bytes(b"Vtb.cpp: deps\n")
    lines: list[bytes] = []
    for staged in sorted(build_root.glob("src/**/*.sv")):
        lines.append(f'VL_STOP_MT("{staged}", 1, "")'.encode())
        lines += [line for line in staged.read_bytes().split(b"\n") if b"// dead" not in line]
    (build_root / "Vtb___024root__0.cpp").write_bytes(b"\n".join(lines) + b"\n")


def _patch_fake_simulation(
    monkeypatch,
    *,
    run: Callable[[int | None], subprocess.CompletedProcess] | None = None,
    build: Callable[[int | None], subprocess.CompletedProcess] | None = None,
    eda_tool: str = "verilator",
    verilate: Callable[[Path], None] | None = None,
    verilator: str = _VERILATOR_VERSION,
) -> FakeSimulation:
    """Fake every build and simulator run of a mutation campaign.

    ``run(index)`` and ``build(index)`` return the process for the baseline
    (``index`` is ``None``) or Mutant ``index``; by default everything passes.
    A ``run`` may raise ``subprocess.TimeoutExpired``.  ``verilate(build_root)`` runs after
    a build that passes, to leave what Verilator would (``_write_fake_verilation``);
    ``verilator`` is the version the baseline build reports.
    """
    _patch_resolve_target(monkeypatch, eda_tool=eda_tool, verilator=verilator)
    fake = FakeSimulation()
    original = subprocess.run

    def _fake(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["make", "-C"]:
            index = _mutant_index(" ".join(cmd))
            fake.builds.append(index)
            built = build(index) if build else _fake_proc(rc=0, stdout="[make] ok")
            if verilate and built.returncode == 0:
                verilate(Path(cmd[2]))
            return built
        if isinstance(cmd, list) and f"booley.flows.sim.backends.{eda_tool}" in " ".join(cmd):
            index = _mutant_index(repr(launched_work(cmd)))
            fake.runs.append(index)
            fake.run_timeouts.append(kwargs["timeout"])
            return run(index) if run else _fake_proc(rc=0, stdout="[sim] ok")
        return original(cmd, *args, **kwargs)

    monkeypatch.setattr("booley.flows.mutation.flow.subprocess.run", _fake)
    return fake


def test_elab_and_sim_run_verilator_binary(tmp_path: Path, monkeypatch):
    """Each source variant is built, then run through the configured simulator."""
    captured: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr(
        "booley.flows.mutation.flow.subprocess.run",
        _fake_run,
    )

    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()

    endpoint._run_elab("default", tmp_path, build_dir)
    endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

    assert len(captured) == 2
    elab_cmd, sim_cmd = captured
    # Build half: `make -C <bin dir>` (the resolved build root).
    assert elab_cmd[:2] == ["make", "-C"]
    # _run_elab records the bin dir marker for the run-many loop to reuse.
    assert (build_dir / ".booley_edalize_bindir").exists()
    # Run half: the active Python drives the Verilator adapter with no mutation selector.
    assert sim_cmd[:3] == [sys.executable, "-m", "booley.flows.sim.backends.verilator"]
    assert not any("MUT_ID" in arg for arg in sim_cmd)
    work = launched_work(sim_cmd)
    assert work.top == "tb"
    assert not work.trace


def test_elab_uses_shared_preparer_and_its_exact_command(tmp_path: Path, monkeypatch) -> None:
    handle = SimpleNamespace(selector="default", identity="::demo:0#default")
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    prepared = PreparedSimulationBuild(
        target="default",
        target_identity=handle.identity,
        resolved=SimpleNamespace(build_root=build_dir / "nested", files=()),
        work_root=build_dir,
        build_root=build_dir / "nested",
        eda_tool="verilator",
        toplevel="tb",
        make_argv=("custom-build", "--exact-command"),
    )
    catalog = SimpleNamespace(select=lambda *args, **kwargs: handle)
    monkeypatch.setattr(TargetCatalog, "build", classmethod(lambda _cls, _root: catalog))
    prepare = MagicMock(return_value=prepared)
    monkeypatch.setattr("booley.flows.mutation.flow.prepare_simulation_build", prepare)
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "booley.flows.mutation.flow.subprocess.run",
        lambda command, **kwargs: (
            captured.append(command),
            _fake_proc(rc=0),
        )[1],
    )
    probed: list[dict[str, str]] = []
    monkeypatch.setattr(
        "booley.flows.mutation.flow.verilator_version",
        lambda environment: probed.append(dict(environment)) or "Verilator 5.052",
    )
    endpoint = _make_endpoint(tmp_path, monkeypatch)

    result = endpoint._run_elab("default", tmp_path, build_dir)

    prepare.assert_called_once_with(
        handle, build_root=build_dir, lane_kind="unreserved", environment={}
    )
    assert captured == [["custom-build", "--exact-command"]]
    assert result.returncode == 0
    # The baseline build records the version of the Verilator it builds with.
    assert (build_dir / ".booley_verilator_version").read_text(encoding="utf-8") == (
        "Verilator 5.052"
    )
    assert len(probed) == 1
    assert probed[0]["PATH"] == os.environ["PATH"]


@pytest.mark.parametrize(
    "message",
    (
        "Target parameter intent is inconsistent",
        "Doctor requested a bad simulation fixture but its overlay is empty",
    ),
)
def test_elab_surfaces_shared_preparation_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr(
        "booley.flows.mutation.flow.prepare_simulation_build",
        lambda *args, **kwargs: (_ for _ in ()).throw(SimulationBuildPreparationError(message)),
    )
    run = MagicMock()
    monkeypatch.setattr("booley.flows.mutation.flow.subprocess.run", run)
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()

    result = endpoint._run_elab("default", tmp_path, build_dir)

    assert result.returncode == 1
    assert message in result.stderr
    run.assert_not_called()


def test_build_and_adapter_parent_relocate_python_artifacts(tmp_path: Path, monkeypatch):
    captured: list[tuple[list[str], dict[str, str]]] = []
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime))

    def _fake_run(cmd, *args, **kwargs):
        captured.append((list(cmd), kwargs["env"]))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr("booley.flows.mutation.flow.subprocess.run", _fake_run)
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()

    endpoint._run_elab("default", tmp_path, build_dir)
    endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

    expected = runtime / "python-artifacts"
    assert len(captured) == 2
    for _cmd, environment in captured:
        assert environment["PYTHONPYCACHEPREFIX"] == str(expected / "bytecode")
        assert f"cache_dir={expected / 'pytest'}" in environment["PYTEST_ADDOPTS"]


def test_mutant_build_uses_managed_compiler_cache(tmp_path: Path, monkeypatch):
    """Mutant builds compile through the Project cache like ordinary Simulation."""
    from booley.flows.sim.compiler_cache import CompilerCachePolicy
    from booley.runtime.compiler_cache import COMPILER_CACHE_RELATIVE

    captured: list[dict[str, str]] = []
    policy = CompilerCachePolicy(True, tmp_path / "data" / COMPILER_CACHE_RELATIVE, "5G")

    def _fake_prepare(handle, *, build_root, lane_kind, **kwargs):
        return PreparedSimulationBuild(
            target=handle.selector,
            target_identity=handle.identity,
            resolved=SimpleNamespace(build_root=Path(build_root), toplevel="tb", files=()),
            work_root=Path(build_root),
            build_root=Path(build_root),
            eda_tool="verilator",
            toplevel="tb",
            make_argv=("make", "-C", str(build_root)),
            environment=policy.environment(),
            compiler_cache=policy,
        )

    def _fake_run(cmd, *args, **kwargs):
        captured.append(kwargs["env"])
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr("booley.flows.mutation.flow.prepare_simulation_build", _fake_prepare)
    monkeypatch.setattr("booley.flows.mutation.flow.subprocess.run", _fake_run)
    monkeypatch.setattr("booley.flows.sim.compiler_cache.shutil.which", lambda *a, **kw: "/ccache")
    (tmp_path / "data").mkdir()
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()

    endpoint._run_elab("default", tmp_path, build_dir)

    [environment] = captured
    assert environment["OBJCACHE"] == "ccache"
    assert environment["CCACHE_DIR"] == str(policy.root)
    assert policy.root.is_dir()


def test_sim_runs_every_configured_test_selector(tmp_path: Path, monkeypatch):
    captured: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr(
        "booley.flows.mutation.flow.subprocess.run",
        _fake_run,
    )
    _write_tests_toml(
        tmp_path,
        '[default]\ntests = ["coremark.elf", "smoke.elf"]\nselect = "--meminit=ram,{name}"\n',
    )

    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    endpoint._run_elab("default", tmp_path, build_dir)
    runs = endpoint._run_target_test_suite("default", tmp_path, build_dir, "tb")

    sim_cmds = captured[1:]
    assert [run.test_name for run in runs] == ["coremark.elf", "smoke.elf"]
    assert "--meminit=ram,coremark.elf" in launched_work(sim_cmds[0]).plusargs
    assert "--meminit=ram,smoke.elf" in launched_work(sim_cmds[1]).plusargs


def _mutation_sim_runs(tmp_path: Path, monkeypatch, tests_toml: str, eda_tool: str | None = None):
    """Run the declared suite of ``default`` once; return ``(argv, env)`` per sim."""
    captured: list[tuple[list[str], dict[str, str]]] = []

    def _fake_run(cmd, *args, **kwargs):
        captured.append((list(cmd), dict(kwargs.get("env") or {})))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr("booley.flows.mutation.flow.subprocess.run", _fake_run)
    _write_tests_toml(tmp_path, tests_toml)
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    if eda_tool is not None:
        monkeypatch.setattr(endpoint, "target_eda_tool", lambda *_args, **_kwargs: eda_tool)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    endpoint._run_elab("default", tmp_path, build_dir)
    endpoint._run_target_test_suite("default", tmp_path, build_dir, "tb")
    return captured[1:]


_MUTATION_VARIANTS = """
[default]
tests = ["reset", "stress"]
select = "+test={name}"
default_seed = 7

[default.variants.stress]
default = "short"
short = { args = ["+iters=1000"], env = { STRESS_MODE = "short" } }
soak = ["+iters=100000"]
"""


def test_sim_runs_the_default_variant_at_the_default_seed(tmp_path: Path, monkeypatch):
    """A mutant is graded by the same Test Runs a plain Simulation Flow run uses."""
    monkeypatch.delenv("STRESS_MODE", raising=False)
    runs = _mutation_sim_runs(tmp_path, monkeypatch, _MUTATION_VARIANTS, "verilator")

    plusargs = [launched_work(cmd).plusargs for cmd, _env in runs]
    assert plusargs == [
        ("test=reset", "verilator+seed+7"),
        ("test=stress", "iters=1000", "verilator+seed+7"),
    ]
    assert [env.get("STRESS_MODE") for _cmd, env in runs] == [None, "short"]


def test_sim_passes_the_target_plusargs_like_the_simulation_flow(tmp_path: Path, monkeypatch):
    """The Target's plusargs come first; ``select`` replaces one that sets its option."""
    monkeypatch.setitem(_TARGET_PARAMETERS, "depth", {"paramtype": "plusarg", "default": 4})
    monkeypatch.setitem(_TARGET_PARAMETERS, "test", {"paramtype": "plusarg", "default": "none"})
    runs = _mutation_sim_runs(tmp_path, monkeypatch, _MUTATION_VARIANTS, "verilator")

    plusargs = [launched_work(cmd).plusargs for cmd, _env in runs]
    assert plusargs == [
        ("depth=4", "test=reset", "verilator+seed+7"),
        ("depth=4", "test=stress", "iters=1000", "verilator+seed+7"),
    ]


def test_sim_refuses_a_variant_argument_the_target_owns(tmp_path: Path, monkeypatch):
    """The plan is built against the Target's declared plusargs, as in the Flow."""
    monkeypatch.setitem(_TARGET_PARAMETERS, "iters", {"paramtype": "plusarg", "default": 1})
    with pytest.raises(UnsupportedSimTargetError, match="overrides a plusarg the Target declares"):
        _mutation_sim_runs(tmp_path, monkeypatch, _MUTATION_VARIANTS, "verilator")


def test_mutant_builds_and_runs_under_the_target_environment(tmp_path: Path, monkeypatch):
    """Regression: the Target's tests.toml env table never reached a mutant.

    The Simulation Flow builds and launches under that table, so a mutant
    graded without it runs a different testbench configuration.
    """
    monkeypatch.delenv("FLAVOR", raising=False)
    monkeypatch.delenv("STRESS_MODE", raising=False)
    import booley.flows.mutation.flow as mutation_flow_module

    prepared_with: list[dict[str, str]] = []

    tests_toml = _MUTATION_VARIANTS.replace(
        "default_seed = 7", 'default_seed = 7\nenv = { FLAVOR = "wide" }'
    )
    _patch_resolve_target(monkeypatch)
    patched = mutation_flow_module.prepare_simulation_build

    def recording_prepare(handle, **kwargs):
        prepared_with.append(dict(kwargs.get("environment") or {}))
        return patched(handle, **kwargs)

    monkeypatch.setattr(mutation_flow_module, "prepare_simulation_build", recording_prepare)
    captured: list[dict[str, str]] = []

    def _fake_run(cmd, *args, **kwargs):
        captured.append(dict(kwargs.get("env") or {}))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    monkeypatch.setattr("booley.flows.mutation.flow.subprocess.run", _fake_run)
    _write_tests_toml(tmp_path, tests_toml)
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    endpoint._run_elab("default", tmp_path, build_dir)
    endpoint._run_target_test_suite("default", tmp_path, build_dir, "tb")

    assert prepared_with and all(env == {"FLAVOR": "wide"} for env in prepared_with)
    runs = captured[1:]
    assert [env.get("FLAVOR") for env in runs] == ["wide", "wide"]
    # The Test Variant's variable is added on top, for its own run only.
    assert [env.get("STRESS_MODE") for env in runs] == [None, "short"]


def test_sim_on_an_unseeded_target_passes_no_seed(tmp_path: Path, monkeypatch):
    runs = _mutation_sim_runs(
        tmp_path, monkeypatch, '[default]\ntests = ["reset"]\nselect = "+test={name}"\n', "icarus"
    )
    assert launched_work(runs[0][0]).plusargs == ("test=reset",)


def test_sim_selector_resolves_vlnv_qualified_target(tmp_path: Path, monkeypatch):
    """A VLNV-qualified --target still finds the bare tests.toml section.

    Regression for the raw ``TEST_NAMES.get(target)`` lookup: a qualified
    target (``lib:ip:core#default``) missed the bare ``[default]`` section, so
    no selector was rendered and every mutant ran the TB default test —
    the sweep read "survived" across the board with nothing said.
    """
    captured: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr(
        "booley.flows.mutation.flow.subprocess.run",
        _fake_run,
    )
    _write_tests_toml(
        tmp_path,
        '[default]\ntests = ["coremark.elf", "smoke.elf"]\nselect = "--meminit=ram,{name}"\n',
    )

    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    endpoint._run_elab("lib:ip:core#default", tmp_path, build_dir)
    endpoint._run_target_test_suite("lib:ip:core#default", tmp_path, build_dir, "tb")

    sim_cmds = captured[1:]
    assert "--meminit=ram,coremark.elf" in launched_work(sim_cmds[0]).plusargs
    assert "--meminit=ram,smoke.elf" in launched_work(sim_cmds[1]).plusargs


def test_target_suite_accepts_vlnv_qualified_target(tmp_path: Path, monkeypatch):
    _write_tests_toml(tmp_path, '[default]\ntests = ["test_a", "test_b"]\n')
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    _patch_resolve_target(monkeypatch)
    assert endpoint._target_test_suite("lib:ip:core#default").tests == (
        "test_a",
        "test_b",
    )


def test_sim_forwards_project_verdict_sentinels(tmp_path: Path, monkeypatch):
    captured: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _fake_proc(rc=0, stdout="[ok]", stderr="")

    _patch_resolve_target(monkeypatch)
    monkeypatch.setattr(
        "booley.flows.mutation.flow.subprocess.run",
        _fake_run,
    )
    monkeypatch.setattr(
        "booley.flows.sim.config._sim_config",
        lambda work_dir: {
            "pass_sentinels": ["Correct operation validated."],
            "fail_sentinels": ["ERROR!"],
        },
    )

    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    endpoint._run_elab("default", tmp_path, build_dir)
    endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

    work = launched_work(captured[-1])
    assert work.pass_sentinels == ("Correct operation validated.",)
    assert work.fail_sentinels == ("ERROR!",)


def test_mutant_launch_grades_from_the_document(tmp_path: Path, monkeypatch, capsys) -> None:
    for adapter in ("hdl", "cocotb"):
        for outcome in ("detected", "survived", "inconclusive"):
            _grade_mutant_launch(
                tmp_path / f"{adapter}-{outcome}", monkeypatch, capsys, adapter, outcome
            )


def _grade_mutant_launch(tmp_path: Path, monkeypatch, capsys, adapter: str, outcome: str) -> None:
    """A mutant is graded from its adapter's output; the fake decodes the launch document first."""
    from booley.flows.sim.adapter_contract import run_adapter_entry
    from booley.flows.sim.backends import cocotb_results as cr

    tmp_path.mkdir()
    cocotb = adapter == "cocotb"
    # The real rejected-document answer of an adapter that cannot decode its argument.
    assert run_adapter_entry(["not a document"], lambda _work: 0) == 2
    rejected = capsys.readouterr().out

    def cocotb_line(status: str) -> str:
        results = cr.CocotbResults(
            state=cr.STATE_OK, tests=(cr.CocotbTest("test_a", "tb", status),)
        )
        return cr.format_results_line(cr.results_payload(results))

    answers = {
        ("hdl", "detected"): _fake_proc(rc=1, stdout="[FAIL]"),
        ("hdl", "survived"): _fake_proc(rc=0, stdout="[PASS]"),
        ("hdl", "inconclusive"): _fake_proc(rc=2, stdout=rejected),
        ("cocotb", "detected"): _fake_proc(rc=1, stdout=cocotb_line(cr.VERDICT_FAIL)),
        ("cocotb", "survived"): _fake_proc(rc=0, stdout=cocotb_line(cr.VERDICT_PASS)),
        ("cocotb", "inconclusive"): _fake_proc(rc=0, stdout="no results line"),
    }
    launched: list[PreparedSimulationWork] = []

    def _fake_run(cmd, *args, **kwargs):
        if cmd[0] == "make":
            return _fake_proc(rc=0)
        launched.append(launched_work(cmd))
        return answers[(adapter, outcome)]

    _patch_resolve_target(monkeypatch)
    _patch_cocotb_target(monkeypatch, module="tb.test_x" if cocotb else None)
    monkeypatch.setattr("booley.flows.mutation.flow.subprocess.run", _fake_run)
    _write_tests_toml(tmp_path, '[default]\ntests = ["test_a"]\n')
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    endpoint._run_elab("default", tmp_path, build_dir)

    runs = endpoint._run_target_test_suite("default", tmp_path, build_dir, "tb")
    verdict = MutationFlow._classify_variant_suite(runs)

    [work] = launched
    assert work.adapter == ("cocotb" if cocotb else "verilator")
    assert work.tests == (("test_a",) if cocotb else ())
    assert verdict.detected is (outcome == "detected")
    assert bool(verdict.inconclusive_reason) is (outcome == "inconclusive")
    if adapter == "hdl" and outcome == "inconclusive":
        assert "work document rejected" in verdict.inconclusive_reason


def _prepare_scope_files(tmp_path: Path, scope: list[str]) -> None:
    for rel in scope:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(
                "module dummy;\n"
                + "".join(f"  assign x_{i} = a + b_{i};\n" for i in range(1, 31))
                + "endmodule\n",
                encoding="utf-8",
            )


def _assert_durable_campaign(tmp_path: Path, result: EndpointOutcome) -> None:
    """Assert that a successful campaign is self-contained and fully published."""
    results_path = tmp_path / result.detail["artifacts"]["results"]
    assert results_path.is_file()
    results = results_path.read_text(encoding="utf-8")
    first = result.detail["classified"][0]
    assert f"[rtl/mod_a.sv:{first['line']}](variants/mutant_1/rtl/mod_a.sv)" in results
    assert "not detected" in results
    assert "campaign/mutant-logs/mutant_1.log" in results
    assert result.detail["classified"][0]["log"].endswith("campaign/mutant-logs/mutant_1.log")

    manifest_path = tmp_path / result.detail["artifacts"]["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["target"] == "lite"
    assert manifest["eda_tool"] == "verilator"
    assert manifest["baseline"]["status"] == "passed"
    assert len(manifest["mutants"]) == 2
    assert all(row["status"] == "not_detected" for row in manifest["mutants"])
    campaign_dir = manifest_path.parent
    assert all((campaign_dir / row["log"]).is_file() for row in manifest["mutants"])
    assert all((campaign_dir / row["variant"]).is_file() for row in manifest["mutants"])
    assert not any(path.name.endswith(".tmp") for path in manifest_path.parents[1].iterdir())


# ---------------------------------------------------------------------------
# Deterministic campaign: real generator, fake builds and simulator runs
# ---------------------------------------------------------------------------

_SCOPE = "rtl/mod_a.sv"
_GENERATOR_OPERATORS = {"arithmetic-logical", "deletion"}  # what _prepare_scope_files offers


def _campaign(
    tmp_path: Path,
    monkeypatch,
    *,
    scope: str | None = _SCOPE,
    run: Callable[[int | None], subprocess.CompletedProcess] | None = None,
    build: Callable[[int | None], subprocess.CompletedProcess] | None = None,
    simulation: dict[str, Any] | None = None,
    **endpoint_args: Any,
) -> tuple[MutationFlow, EndpointOutcome, FakeSimulation]:
    """Run one whole campaign of the scope file ``rtl/mod_a.sv`` (30 sites) on fake results.

    ``scope=None`` leaves ``--scope`` out, so the Target's own RTL (that file) is the scope.
    Comma-separated scope entries are each prepared as a file.

    ``simulation`` holds the extra arguments of ``_patch_fake_simulation`` (the Target's
    simulator, whether builds leave Verilator C++, the Verilator version).
    """
    _prepare_scope_files(tmp_path, (scope or _SCOPE).split(","))
    fake = _patch_fake_simulation(monkeypatch, run=run, build=build, **(simulation or {}))
    endpoint = _make_endpoint(tmp_path, monkeypatch, scope=scope, **endpoint_args)
    return endpoint, endpoint._run(), fake


def _detects_every_mutant(index: int | None) -> subprocess.CompletedProcess:
    """The baseline passes; every Mutant's test fails."""
    return _fake_proc(rc=0 if index is None else 1, stdout="[sim] run")


def _results_markdown(tmp_path: Path, result: EndpointOutcome) -> str:
    return (tmp_path / result.detail["artifacts"]["results"]).read_text(encoding="utf-8")


def _mutant_keys(result: EndpointOutcome) -> list[tuple[Any, ...]]:
    return [
        (row["id"], row["category"], row["file"], row["line"], row["status"])
        for row in result.detail["classified"]
    ]


@pytest.mark.parametrize("removed", ["--steer", "--regen-lock"])
def test_removed_llm_options_are_rejected(removed: str) -> None:
    argv = ["--target", "sim", "--scope", "rtl/design.sv", removed]
    if removed == "--steer":
        argv.append("guidance")
    with pytest.raises(SystemExit):
        MutationFlow().parse_args(argv)


def test_campaign_runs_the_baseline_then_each_mutant(tmp_path: Path, monkeypatch) -> None:
    _, result, fake = _campaign(tmp_path, monkeypatch, count=2, min_detected=0)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert fake.runs == [None, 1, 2]  # the baseline, then each Mutant


def test_leftover_lock_file_is_ignored(tmp_path: Path, monkeypatch) -> None:
    _prepare_scope_files(tmp_path, [_SCOPE])
    digest = "sha256:" + hashlib.sha256((tmp_path / _SCOPE).read_bytes()).hexdigest()
    lock_dir = tmp_path / "logs" / ".runtime" / "mutation_tester" / "lock"
    lock_dir.mkdir(parents=True)
    legacy_mutations = [
        {
            "index": number,
            "category": "legacy-lock-only",
            "file": _SCOPE,
            "line": number + 1,
            "original_code": f"a + b_{number}",
            "mutated_code": f"a * b_{number}",
            "detectability_argument": "from an old lock",
        }
        for number in (1, 2)
    ]
    (lock_dir / "lock.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "created_at": "2026-01-01T00:00:00Z",
                "scope": [_SCOPE],
                "scope_hashes": {_SCOPE: digest},
                "count": 2,
                "mutations": legacy_mutations,
            }
        ),
        encoding="utf-8",
    )

    _, result, _ = _campaign(tmp_path, monkeypatch, count=2, min_detected=0)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert {row["category"] for row in result.detail["classified"]} <= _GENERATOR_OPERATORS
    assert not lock_dir.parent.exists()  # the retired Specialist's scratch tree is deleted


def test_a_smaller_count_drops_the_builds_of_an_earlier_larger_run(
    tmp_path: Path, monkeypatch
) -> None:
    stale = tmp_path / "logs" / ".runtime" / "mutation" / "campaign" / "builds" / "mutant_9"
    stale.mkdir(parents=True)
    (stale / "image").write_bytes(b"stale")

    _, result, _ = _campaign(tmp_path, monkeypatch, count=2, min_detected=0)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert not stale.exists()


def test_same_rtl_count_and_seed_reproduce_mutants_and_identity(
    tmp_path: Path, monkeypatch
) -> None:
    _, first, _ = _campaign(tmp_path, monkeypatch, count=4, min_detected=0, seed=3)
    _, second, _ = _campaign(tmp_path, monkeypatch, count=4, min_detected=0, seed=3)

    assert first.exit_code == second.exit_code == EXIT_SUCCESS, first.report_text
    assert _mutant_keys(first) == _mutant_keys(second)
    assert len({row[0] for row in _mutant_keys(first)}) == 4  # distinct Mutants
    assert first.detail["identity"] == second.detail["identity"]
    assert first.detail["artifacts"]["results"] != second.detail["artifacts"]["results"]


def test_seed_defaults_to_zero_and_changes_the_selection(tmp_path: Path, monkeypatch) -> None:
    endpoint, default_run, _ = _campaign(tmp_path, monkeypatch, count=3, min_detected=0)
    _, explicit_zero, _ = _campaign(tmp_path, monkeypatch, count=3, min_detected=0, seed=0)
    _, other_seed, _ = _campaign(tmp_path, monkeypatch, count=3, min_detected=0, seed=1)

    assert endpoint.args.seed == 0
    assert default_run.exit_code == other_seed.exit_code == EXIT_SUCCESS, default_run.report_text
    assert _mutant_keys(default_run) == _mutant_keys(explicit_zero)
    assert _mutant_keys(default_run) != _mutant_keys(other_seed)
    assert [run.detail["identity"]["seed"] for run in (default_run, other_seed)] == [0, 1]


def test_negative_seed_is_refused(tmp_path: Path, monkeypatch) -> None:
    _prepare_scope_files(tmp_path, [_SCOPE])
    endpoint = _make_endpoint(tmp_path, monkeypatch, count=2, seed=-1, extra_args=["--dry-run"])

    result, _ = _run_count_preview(endpoint, monkeypatch)

    assert result.exit_code == EXIT_ERROR
    assert "--seed must be a non-negative integer" in result.report_text


_DEFAULT_SCOPE_CORE = """\
CAPI=2:
name: ::demo_core:0
filesets:
  rtl:
    files:
      - rtl/a.sv: {file_type: systemVerilogSource}
      - rtl/b.v: {file_type: verilogSource}
      - rtl/defs.svh: {file_type: systemVerilogSource, is_include_file: true}
      - rtl/notes.cpp: {file_type: cppSource}
      - rtl/table.mem: {file_type: user}
  imported:
    files:
      - vendor/v.sv: {file_type: systemVerilogSource, tags: [vendored]}
  built:
    files: [gen/g.sv]
    file_type: systemVerilogSource
    tags: [generated]
  tb:
    files: [tb/tb_top.sv]
    file_type: systemVerilogSource
    tags: [tb]
targets:
  sim:
    default_tool: verilator
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl, imported, built, tb]
    toplevel: tb_top
"""


def test_omitted_scope_defaults_to_the_targets_own_rtl(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "design.core").write_text(_DEFAULT_SCOPE_CORE, encoding="utf-8")
    for rel in (
        "rtl/a.sv",
        "rtl/b.v",
        "rtl/defs.svh",
        "rtl/notes.cpp",
        "rtl/table.mem",
        "vendor/v.sv",
        "gen/g.sv",
        "tb/tb_top.sv",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("// stub\n", encoding="utf-8")
    _write_tests_toml(tmp_path, '[sim]\ntests = ["smoke"]\n')
    endpoint = _make_endpoint(tmp_path, monkeypatch, target="sim", scope=None, count=None)

    assert endpoint._apply_campaign_defaults() is None

    # The testbench, the vendored and generated files, the include header and the
    # non-Verilog inputs are not mutable.
    assert endpoint.args.scope == "rtl/a.sv,rtl/b.v"
    assert endpoint._validate_scope_against_target(endpoint._scope_files()) is None


def test_omitted_scope_with_a_declared_criterion_scope_uses_the_criterion(
    tmp_path: Path, monkeypatch
) -> None:
    endpoint = _two_core_endpoint(
        tmp_path, monkeypatch, {"mutation_score_first": {"scope": ["y.v"], "total": 3}}, scope=None
    )

    assert endpoint._apply_campaign_defaults() is None
    assert endpoint.args.scope == "y.v"


@pytest.mark.parametrize("count, expected", [(10, 8), (15, 12), (5, 4)])
def test_default_threshold_is_eighty_percent_of_count(
    tmp_path: Path, monkeypatch, count: int, expected: int
) -> None:
    _prepare_scope_files(tmp_path, ["rtl/mod_a.sv", "rtl/mod_b.sv"])
    endpoint = _make_endpoint(tmp_path, monkeypatch, count=count, extra_args=["--dry-run"])

    result, _ = _run_count_preview(endpoint, monkeypatch)

    assert result.detail["min_detected"] == expected
    assert (4 * count + 4) // 5 == expected


def test_explicit_min_detected_overrides_the_default(tmp_path: Path, monkeypatch) -> None:
    _prepare_scope_files(tmp_path, ["rtl/mod_a.sv", "rtl/mod_b.sv"])
    omitted = _make_endpoint(tmp_path, monkeypatch, count=10, extra_args=["--dry-run"])
    from_cli = _make_endpoint(
        tmp_path, monkeypatch, count=10, min_detected=3, extra_args=["--dry-run"]
    )
    from_criterion = _make_endpoint(tmp_path, monkeypatch, count=None, extra_args=["--dry-run"])

    default_result, _ = _run_count_preview(omitted, monkeypatch)
    cli_result, _ = _run_count_preview(from_cli, monkeypatch)
    criterion_result, _ = _run_count_preview(
        from_criterion, monkeypatch, params={"total": 7, "min_detected": 5}
    )

    assert default_result.detail["min_detected"] == 8  # what the overrides replace
    assert cli_result.detail["min_detected"] == 3
    assert (criterion_result.detail["count"], criterion_result.detail["min_detected"]) == (7, 5)


def test_survivors_are_not_detected_with_one_equivalence_note(tmp_path: Path, monkeypatch) -> None:
    def survive_first_three(index: int | None) -> subprocess.CompletedProcess:
        return _fake_proc(rc=0 if index in (None, 1, 2, 3) else 1, stdout="[sim] run")

    _, result, _ = _campaign(
        tmp_path, monkeypatch, count=5, min_detected=0, run=survive_first_three
    )

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    markdown = _results_markdown(tmp_path, result)
    assert markdown.count("## Not detected") == 1
    assert markdown.count("may be equivalent") == 1
    assert "| not detected |" in markdown
    assert "coverage gap" not in markdown.lower()
    assert result.report_text.count("NOT DETECTED [") == 3
    assert result.report_text.count("may be equivalent") == 1
    assert "3 not detected" in result.report_text

    _, all_killed, _ = _campaign(
        tmp_path, monkeypatch, count=3, min_detected=0, run=_detects_every_mutant
    )

    assert "may be equivalent" not in all_killed.report_text
    assert "may be equivalent" not in _results_markdown(tmp_path, all_killed)


def test_invalid_mutants_are_named_against_the_threshold(tmp_path: Path, monkeypatch) -> None:
    def three_do_not_build(index: int | None) -> subprocess.CompletedProcess:
        return _fake_proc(rc=2 if index in (2, 5, 8) else 0, stderr="error: does not elaborate")

    _, result, _ = _campaign(
        tmp_path, monkeypatch, count=10, run=_detects_every_mutant, build=three_do_not_build
    )

    # ceil(0.8 x 10) = 8 of the 10 Mutants run: the 3 invalid ones use up the margin.
    assert result.exit_code == EXIT_FAILURE, result.report_text
    assert result.detail["min_detected"] == 8
    assert (result.detail["detected"], result.detail["invalid"]) == (7, 3)
    assert "3 invalid Mutants could not be judged" in result.report_text
    assert "RESULT: FAIL (7/7 detected, need 8)" in result.report_text
    assert "3 invalid Mutants could not be judged" in _results_markdown(tmp_path, result)


def test_generator_shortfall_lowers_the_default_threshold(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / _SCOPE).write_text("module m;\n  assign y = a + b;\nendmodule\n", encoding="utf-8")

    _, result, _ = _campaign(tmp_path, monkeypatch, count=10, run=_detects_every_mutant)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert "only 2 candidate Mutants exist" in result.detail["shortfall"]
    assert "only 2 candidate Mutants exist" in result.report_text
    assert (result.detail["min_detected"], result.detail["total_valid"]) == (2, 2)
    assert result.detail["identity"]["count"] == 10  # what was asked for


def test_error_phases_end_with_a_result_error_line(tmp_path: Path, monkeypatch) -> None:
    """AC1: baseline, elaboration, parse and generation failures each end with the verdict line."""
    (tmp_path / "rtl").mkdir()

    def baseline_fails(index: int | None) -> subprocess.CompletedProcess:
        return _fake_proc(rc=1 if index is None else 0, stdout="[sim] failed")

    def baseline_does_not_elaborate(index: int | None) -> subprocess.CompletedProcess:
        return _fake_proc(rc=1 if index is None else 0, stdout="[elab] boom")

    cases = [
        (
            "baseline",
            "module m;\n  assign y = a + b;\nendmodule\n",
            {"run": baseline_fails},
            "RESULT: ERROR (baseline: test '<default>' failed on pristine source)",
            True,
        ),
        (
            "elaboration",
            "module m;\n  assign y = a + b;\nendmodule\n",
            {"build": baseline_does_not_elaborate},
            "RESULT: ERROR (elaboration: pristine source did not elaborate)",
            True,
        ),
        (
            "parse",
            "module m(;\n  assign = ;\nendmodule\n",
            {},
            "RESULT: ERROR (parse: cannot parse rtl/mod_a.sv)",
            False,
        ),
        (
            "generation",
            "module m;\nendmodule\n",
            {},
            "RESULT: ERROR (generation: no Mutation Operator site in the scope files)",
            False,
        ),
    ]
    for phase, source, fakes, last_line, builds in cases:
        (tmp_path / _SCOPE).write_text(source, encoding="utf-8")

        _, result, fake = _campaign(tmp_path, monkeypatch, count=3, **fakes)

        assert result.exit_code == EXIT_ERROR, phase
        assert result.report_text.splitlines()[-1] == last_line, phase
        if not builds:  # generation fails before any build
            assert result.detail["phase"] == "mutant_generation", phase
            assert fake.builds == [], phase


def test_missing_pyslang_refuses_with_its_name(tmp_path: Path, monkeypatch) -> None:
    from importlib import metadata

    def _not_installed(_name: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "version", _not_installed)

    _, result, fake = _campaign(tmp_path, monkeypatch, count=2)

    assert result.exit_code == EXIT_ERROR
    assert "pyslang is not installed" in result.report_text
    assert "hdl-parser" in result.report_text  # the install guidance reaches the user
    assert "--scope" not in result.report_text  # a scope cannot fix a missing parser
    assert fake.builds == []


@pytest.mark.parametrize(
    ("baseline_seconds", "limit"), [(1.2, 30), (12.5, 125), (40, 300)], ids=str
)
def test_mutant_timeout_scales_with_the_tests_baseline_duration(
    tmp_path: Path, monkeypatch, baseline_seconds: float, limit: int
) -> None:
    readings = [0.0, baseline_seconds]  # the baseline test starts and ends
    later = itertools.count(1000)

    def clock() -> float:
        return readings.pop(0) if readings else float(next(later))

    monkeypatch.setattr("booley.flows.mutation.flow._monotonic", clock, raising=False)

    _, result, fake = _campaign(tmp_path, monkeypatch, count=1, min_detected=0)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    # The baseline keeps the 300 s cap; the Mutant's limit is max(10 x baseline, 30 s), capped.
    assert fake.run_timeouts == [300, limit]


def test_timed_out_mutant_counts_as_detected_timeout(tmp_path: Path, monkeypatch) -> None:
    def timeouts(index: int | None) -> subprocess.CompletedProcess:
        if index == 1:  # the run was killed
            raise subprocess.TimeoutExpired(cmd="run", timeout=30)
        if index == 2:  # the adapter cut the run off first
            return _fake_proc(rc=1, stdout="ERROR: Verilator simulation timed out (25s)\n")
        return _fake_proc(rc=0 if index is None else 1, stdout="[sim] a test failed")

    _, result, _ = _campaign(tmp_path, monkeypatch, count=3, run=timeouts)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert result.detail["detected"] == 3  # a timed-out Mutant is detected
    assert result.detail.get("detected_timeout") == 2  # and reported separately
    assert [row["timed_out"] for row in result.detail["classified"]] == [True, True, False]
    markdown = _results_markdown(tmp_path, result)
    assert markdown.count("detected (timeout)") == 2
    assert "- Detected (timeout): 2" in markdown
    assert "3 detected (2 timeout)" in result.report_text


def _baseline_runs(*runs: MutationTestRun) -> MutationFlow:
    """An endpoint whose pristine baseline elaborates and runs exactly *runs*."""
    endpoint = MutationFlow()
    endpoint._run_elab = lambda *_args, **_kwargs: _fake_proc(rc=0)  # type: ignore[method-assign]
    endpoint._run_target_test_suite = lambda *_args, **_kwargs: list(runs)  # type: ignore[method-assign]
    endpoint._persist_baseline_log = lambda _output: ""  # type: ignore[method-assign]
    return endpoint


def test_baseline_failure_result_names_each_failing_test(tmp_path: Path, monkeypatch) -> None:
    """AC2: the ``RESULT:`` line names every failing or inconclusive baseline test."""
    from booley.flows.sim.backends import cocotb_results as cr

    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    plan = SimpleNamespace(target="sim", work_dir=tmp_path, tb_top="tb")
    ok = MutationTestRun("ok", process=_fake_proc(rc=0))
    fail = MutationTestRun("fail", process=_fake_proc(rc=1), output="[sim] failed")
    slow = MutationTestRun("slow", timed_out=True)
    results = cr.CocotbResults(
        state=cr.STATE_OK,
        tests=(
            cr.CocotbTest("test_a", "tb", cr.VERDICT_FAIL),
            cr.CocotbTest("test_b", "tb", cr.VERDICT_PASS),
        ),
    )
    cocotb_fail = MutationTestRun(
        "<cocotb-suite>",
        process=_fake_proc(rc=1),
        output=cr.format_results_line(cr.results_payload(results)),
        requires_cocotb_results=True,
    )
    cocotb_missing = MutationTestRun(
        "<cocotb-suite>",
        process=_fake_proc(rc=0),
        output="no results line",
        requires_cocotb_results=True,
    )
    cases = [
        (
            (ok, fail, slow),
            "RESULT: ERROR (baseline: test 'fail' failed on pristine source; "
            "test 'slow' inconclusive: simulation timed out)",
        ),
        ((ok, fail), "RESULT: ERROR (baseline: test 'fail' failed on pristine source)"),
        ((cocotb_fail,), "RESULT: ERROR (baseline: test 'test_a' failed on pristine source)"),
        (
            (cocotb_missing,),
            "RESULT: ERROR (baseline: test '<cocotb-suite>' inconclusive: "
            "cocotb result line is missing or malformed)",
        ),
    ]
    for runs, last_line in cases:
        result = _baseline_runs(*runs)._run_pristine_baseline(plan)  # type: ignore[arg-type]

        assert isinstance(result, EndpointOutcome)
        assert result.exit_code == EXIT_ERROR
        assert result.report_text.splitlines()[-1] == last_line
        assert "'ok'" not in last_line


def test_error_result_line_survives_blank_and_multiline_reasons(
    tmp_path: Path, monkeypatch
) -> None:
    """A blank reason says ``no detail``; a multi-line run error stays on one line, others kept."""
    from booley.flows.mutation.flow import _error_outcome

    blank = _error_outcome("generation", "  \n", report_text="mutation: cannot generate Mutants")
    assert blank.report_text.splitlines()[-1] == "RESULT: ERROR (generation: no detail)"

    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    plan = SimpleNamespace(target="sim", work_dir=tmp_path, tb_top="tb")
    broken = MutationTestRun("broken", error="launch failed\nsecond line")
    fail = MutationTestRun("fail", process=_fake_proc(rc=1), output="[sim] failed")

    result = _baseline_runs(broken, fail)._run_pristine_baseline(plan)  # type: ignore[arg-type]

    assert isinstance(result, EndpointOutcome)
    assert result.report_text.splitlines()[-1] == (
        "RESULT: ERROR (baseline: test 'broken' inconclusive: launch failed second line; "
        "test 'fail' failed on pristine source)"
    )


def test_baseline_that_times_out_is_inconclusive(tmp_path: Path, monkeypatch) -> None:
    def baseline_times_out(index: int | None) -> subprocess.CompletedProcess:
        return _fake_proc(rc=1, stdout="ERROR: Verilator simulation timed out (295s)\n")

    _, result, fake = _campaign(tmp_path, monkeypatch, count=1, run=baseline_times_out)

    assert result.exit_code == EXIT_ERROR
    assert "mutation baseline was inconclusive" in result.report_text
    assert "simulation timed out" in result.report_text
    assert fake.runs == [None]  # no Mutant ran


def test_identity_is_in_results_and_criterion_detail(tmp_path: Path, monkeypatch) -> None:
    from booley.flows.mutation.generator import GENERATOR_VERSION

    endpoint, result, _ = _campaign(tmp_path, monkeypatch, count=2, min_detected=0, seed=5)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    digest = "sha256:" + hashlib.sha256((tmp_path / _SCOPE).read_bytes()).hexdigest()
    expected = {
        "scope_hashes": {_SCOPE: digest},
        "generator_version": GENERATOR_VERSION,
        "pyslang_version": importlib.metadata.version("pyslang"),
        "seed": 5,
        "count": 2,
        "verilator_version": _VERILATOR_VERSION,  # a Verilator Target records its version
    }
    criterion = endpoint.state.criteria[result.criterion_key].detail
    manifest = json.loads((tmp_path / result.detail["artifacts"]["manifest"]).read_text("utf-8"))
    assert result.detail["identity"] == criterion["identity"] == manifest["identity"] == expected
    markdown = _results_markdown(tmp_path, result)
    for line in (
        "- Seed: 5",
        "- Count: 2",
        f"- Generator version: {GENERATOR_VERSION}",
        f"- pyslang version: {expected['pyslang_version']}",
        f"- Verilator version: {_VERILATOR_VERSION}",
        f"`{_SCOPE}`: {digest}",
    ):
        assert line in markdown
    assert (
        f"seed 5, count 2, generator {GENERATOR_VERSION}, "
        f"pyslang {expected['pyslang_version']}, {_VERILATOR_VERSION})"
    ) in result.report_text


def test_happy_path_publishes_campaign_artifacts(tmp_path: Path, monkeypatch) -> None:
    _, result, _ = _campaign(tmp_path, monkeypatch, count=2, min_detected=0)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert "worktree not clean after sim sweep" not in result.report_text
    variants = result.detail["variant_files"]
    assert variants == [
        {
            "index": index,
            "path": f"reports/mutation/1/campaign/variants/mutant_{index}/rtl/mod_a.sv",
        }
        for index in (1, 2)
    ]
    assert all((tmp_path / row["path"]).is_file() for row in variants)
    _assert_durable_campaign(tmp_path, result)
    assert "mutation variant: reports/mutation/1/campaign/variants/" in result.report_text
    assert result.criterion_key == "mutation_score_lite"


def test_mutant_logs_of_an_earlier_run_do_not_survive_a_smaller_run(
    tmp_path: Path, monkeypatch
) -> None:
    _, _, _ = _campaign(tmp_path, monkeypatch, count=3, min_detected=0)
    logs = tmp_path / "logs" / ".runtime" / "mutation" / "campaign" / "mutant_logs"
    assert sorted(path.name for path in logs.iterdir()) == [f"mutant_{i}.log" for i in (1, 2, 3)]

    _campaign(tmp_path, monkeypatch, count=1, min_detected=0)

    assert [path.name for path in logs.iterdir()] == ["mutant_1.log"]


def test_auto_count_publishes_source_size_budget(tmp_path: Path, monkeypatch) -> None:
    _, result, _ = _campaign(tmp_path, monkeypatch, count="auto", min_detected=0)

    budget = result.detail["source_size_budget"]
    assert budget["method"] == "language_neutral_source_size"
    manifest_path = tmp_path / result.detail["artifacts"]["manifest"]
    assert (manifest_path.parent / "source-size-budget.json").is_file()


def test_a_run_without_a_verdict_publishes_a_complete_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    def first_mutant_has_no_observation(index: int | None) -> subprocess.CompletedProcess:
        if index == 1:
            return _fake_proc(rc=1, stdout="[SIM_INFRA_ERROR] the harness broke\n")
        return _fake_proc(rc=0 if index is None else 1, stdout="[sim] run")

    _, result, _ = _campaign(
        tmp_path, monkeypatch, count=2, min_detected=0, run=first_mutant_has_no_observation
    )

    assert result.exit_code == EXIT_ERROR
    assert result.report_text.startswith("mutation campaign produced no trustworthy verdict")
    assert result.detail["phase"] == "variant_infrastructure"
    assert result.report_text.splitlines()[-1].startswith("RESULT: ERROR (sweep: ")
    assert len(result.detail["classified"]) == 2
    manifest = json.loads((tmp_path / result.detail["artifacts"]["manifest"]).read_text("utf-8"))
    assert [row["status"] for row in manifest["mutants"]] == ["invalid", "detected"]
    assert manifest["identity"]["count"] == 2


class TestResidueWarning:
    """The residue warning compares against the pre-run snapshot, not HEAD (#2777)."""

    WARNING = "WARNING: worktree not clean after sim sweep"

    def _run(self, tmp_path: Path, monkeypatch, *, edit_during_run: bool, edit_before: bool):
        """Run two Mutants; ``edit_during_run`` makes the second one's run append to the scope file."""
        _prepare_scope_files(tmp_path, [_SCOPE])
        if edit_before:
            with (tmp_path / _SCOPE).open("a", encoding="utf-8") as handle:
                handle.write("// uncommitted edit made before the run\n")

        def run(index: int | None) -> subprocess.CompletedProcess:
            if edit_during_run and index == 2:
                with (tmp_path / _SCOPE).open("a", encoding="utf-8") as handle:
                    handle.write("// edit left by the run\n")
            return _fake_proc(rc=0)

        return _campaign(tmp_path, monkeypatch, count=2, min_detected=0, run=run)[1]

    def test_run_ignores_scope_edit_that_predates_run(self, tmp_path: Path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, edit_during_run=False, edit_before=True)

        assert result.exit_code == EXIT_SUCCESS, result.report_text
        assert self.WARNING not in result.report_text

    def test_run_warns_naming_the_scope_file_it_changed(self, tmp_path: Path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, edit_during_run=True, edit_before=True)

        warnings = [line for line in result.report_text.splitlines() if self.WARNING in line]
        assert [line.strip() for line in warnings] == [f"{self.WARNING}: {_SCOPE}"]
        lines = result.report_text.splitlines()
        assert lines.index(warnings[0]) < next(
            i for i, line in enumerate(lines) if line.startswith("RESULT: ")
        )

    def test_error_run_warns_before_its_final_result_line(self, tmp_path: Path, monkeypatch):
        """A log tail may carry its own ``RESULT:`` line; the warning goes before the last."""
        _prepare_scope_files(tmp_path, [_SCOPE])

        def baseline_fails(index: int | None) -> subprocess.CompletedProcess:
            with (tmp_path / _SCOPE).open("a", encoding="utf-8") as handle:
                handle.write("// edit left by the run\n")
            return _fake_proc(rc=1, stdout="RESULT: FAIL (0/1)")

        _, result, _ = _campaign(tmp_path, monkeypatch, count=2, run=baseline_fails)

        assert result.exit_code == EXIT_ERROR
        lines = result.report_text.splitlines()
        warning = next(i for i, line in enumerate(lines) if self.WARNING in line)
        assert "RESULT: FAIL (0/1)" in lines[:warning]  # the log tail's own line stays above
        assert lines[warning + 1].startswith("RESULT: ERROR (baseline: ")
        assert warning + 2 == len(lines)

    def test_scope_residue_detects_deleted_scope_file(self, tmp_path: Path):
        from booley.flows.mutation import workspace as mutation_workspace

        _prepare_scope_files(tmp_path, [_SCOPE])
        baseline = mutation_workspace.compute_scope_hashes([_SCOPE], tmp_path)
        assert MutationFlow._scope_residue(baseline, tmp_path) == []

        (tmp_path / _SCOPE).unlink()

        assert MutationFlow._scope_residue(baseline, tmp_path) == [_SCOPE]


# ---------------------------------------------------------------------------
# Scope validation against the Target
# ---------------------------------------------------------------------------


_SCOPE_CORE_TEXT = (
    "CAPI=2:\n"
    "name: ::demo_core:0\n"
    "filesets:\n"
    "  rtl:\n"
    "    files:\n"
    "      - rtl/counter_pkg.sv: {file_type: systemVerilogSource}\n"
    "      - rtl/counter.sv: {file_type: systemVerilogSource}\n"
    "  tb:\n"
    "    files:\n"
    "      - tb/tb_counter.sv: {file_type: systemVerilogSource}\n"
    "    tags: [tb]\n"
    "targets:\n"
    "  sim:\n"
    "    default_tool: verilator\n"
    "    flow: sim\n"
    "    flow_options: {tool: verilator}\n"
    "    filesets: [rtl, tb]\n"
    "    toplevel: tb_counter\n"
)


def _author_scope_core(tmp_path: Path) -> None:
    (tmp_path / "design.core").write_text(_SCOPE_CORE_TEXT, encoding="utf-8")
    for rel in ("rtl/counter_pkg.sv", "rtl/counter.sv", "tb/tb_counter.sv"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// stub\n", encoding="utf-8")


class TestValidateScopeAgainstTarget:
    def test_resolved_source_is_accepted(self, tmp_path: Path):
        _author_scope_core(tmp_path)
        endpoint = _make_endpoint(tmp_path, target="sim")
        # rtl/counter.sv IS a resolved source of the sim Target → accepted.
        assert endpoint._validate_scope_against_target(["rtl/counter.sv"]) is None

    def test_stealth_mirror_path_rejected_with_basename_hint(self, tmp_path: Path):
        _author_scope_core(tmp_path)
        endpoint = _make_endpoint(tmp_path, target="sim")
        # The stealth-cores mirror path is NOT a resolved source; its basename
        # (counter.sv) matches rtl/counter.sv, so a "did you mean" hint fires.
        mirror = ".booley/cores/rtl/counter.sv"
        (tmp_path / mirror).parent.mkdir(parents=True)
        (tmp_path / mirror).write_text("// stub\n", encoding="utf-8")
        result = endpoint._validate_scope_against_target([mirror])
        assert result is not None
        assert result.exit_code == EXIT_ERROR
        assert "does not match" in result.report_text
        assert "Did you mean 'rtl/counter.sv'" in result.report_text

    def test_missing_mirror_path_says_it_does_not_exist_with_the_same_hint(self, tmp_path: Path):
        _author_scope_core(tmp_path)
        endpoint = _make_endpoint(tmp_path, target="sim")
        result = endpoint._validate_scope_against_target([".booley/cores/rtl/counter.sv"])
        assert result is not None
        assert result.exit_code == EXIT_ERROR
        assert "does not exist" in result.report_text
        assert "Did you mean 'rtl/counter.sv'" in result.report_text

    def test_refuses_when_target_unresolvable(self, tmp_path: Path):
        endpoint = _make_endpoint(tmp_path, target="sim")
        result = endpoint._validate_scope_against_target(["anything.sv"])
        assert result is not None
        assert "cannot inspect Target" in result.report_text


# ---------------------------------------------------------------------------
# SETUP-F-40 — Cocotb Targets run through the cocotb run-half
# ---------------------------------------------------------------------------


def _patch_cocotb_target(monkeypatch, *, module: str | None, eda_tool: str | None = "verilator"):
    """Make the .core reads report a Cocotb (or classic) Target."""

    def build(_cls, work_dir):
        def select(_self, target, **_kwargs):
            return type(
                "FakeHandle",
                (),
                {
                    "selector": target,
                    "name": target,
                    "identity": f"::{target}:0#{target}",
                    "vlnv": f"::{target}:0",
                    "project_root": Path(work_dir),
                    "cocotb_module": module,
                    "eda_tool": eda_tool,
                },
            )()

        def inspect(_self, _handle):
            return SimpleNamespace(parameters=dict(_TARGET_PARAMETERS))

        return type("FakeCatalog", (), {"select": select, "inspect": inspect})()

    monkeypatch.setattr(
        TargetCatalog,
        "build",
        classmethod(build),
    )


class TestCocotbSimDispatch:
    @pytest.mark.parametrize("eda_tool", (None, "mystery-sim"))
    def test_missing_or_unknown_simulator_never_defaults_to_verilator(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        eda_tool: str | None,
    ) -> None:
        _patch_cocotb_target(monkeypatch, module=None, eda_tool=eda_tool)
        endpoint = _make_endpoint(tmp_path, monkeypatch)

        with pytest.raises(UnsupportedSimTargetError, match="unsupported simulator metadata"):
            endpoint.target_eda_tool("default", tmp_path)

    def test_elab_rejects_declared_and_configured_simulator_disagreement(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_resolve_target(monkeypatch, eda_tool="icarus")
        monkeypatch.setattr(
            "booley.flows.mutation.flow.prepare_simulation_build",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                SimulationBuildPreparationError(
                    "Target 'default' declared 'icarus' but setup configured 'verilator'"
                )
            ),
        )
        endpoint = _make_endpoint(tmp_path, monkeypatch)
        build_dir = tmp_path / "build"
        build_dir.mkdir()

        result = endpoint._run_elab("default", tmp_path, build_dir)

        assert result.returncode == 1
        assert "declared 'icarus' but setup configured 'verilator'" in result.stderr

    def test_invalid_cocotb_target_selection_is_not_reclassified(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class InvalidCatalog:
            def select(self, target: str, *, for_flow: str):
                raise IncompatibleTargetError(f"{target} is not {for_flow}-compatible")

        monkeypatch.setattr(
            TargetCatalog, "build", classmethod(lambda cls, root: InvalidCatalog())
        )
        endpoint = _make_endpoint(tmp_path, monkeypatch)

        with pytest.raises(UnsupportedSimTargetError, match="cannot select Simulation Target"):
            endpoint.cocotb_target("default", tmp_path)

    def test_cached_simulator_marker_must_match_target_declaration(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_cocotb_target(monkeypatch, module=None, eda_tool="icarus")
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        (build_dir / ".booley_edalize_eda_tool").write_text("verilator", encoding="utf-8")
        endpoint = _make_endpoint(tmp_path, monkeypatch)

        with pytest.raises(UnsupportedSimTargetError, match="cached build metadata"):
            endpoint.target_eda_tool("default", tmp_path, build_dir)

    def test_cocotb_target_runs_cocotb_run_half(self, tmp_path: Path, monkeypatch):
        """A Cocotb Target must run with Cocotb's module/filter environment."""
        captured: list[list[str]] = []
        monkeypatch.setattr(
            "booley.flows.mutation.flow.subprocess.run",
            lambda cmd, *a, **k: (captured.append(list(cmd)), _fake_proc(rc=0))[1],
        )
        _patch_resolve_target(monkeypatch)
        _patch_cocotb_target(monkeypatch, module="tb.test_ravenoc")

        endpoint = _make_endpoint(tmp_path, monkeypatch)
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        endpoint._run_elab("default", tmp_path, build_dir)
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

        sim_cmd = captured[-1]
        work = launched_work(sim_cmd)
        assert sim_cmd == prepare_adapter_invocation(work, python=sys.executable)
        assert work.adapter == "cocotb"
        assert work.cocotb_module == "tb.test_ravenoc"
        assert work.eda_tool == "verilator"
        assert work.build_dir == "build"
        assert work.cocotb_seed == 1  # the Target declares no seed: the Flow default
        assert not any("MUT_ID" in arg for arg in sim_cmd)
        # Sentinels and a top do not apply to Cocotb Targets (ADR 0034 decision 6).
        assert not work.pass_sentinels and not work.fail_sentinels
        assert work.top == ""

    def test_cocotb_run_batches_whole_target_suite(self, tmp_path: Path, monkeypatch):
        captured: list[list[str]] = []
        monkeypatch.setattr(
            "booley.flows.mutation.flow.subprocess.run",
            lambda cmd, *a, **k: (captured.append(list(cmd)), _fake_proc(rc=0))[1],
        )
        _patch_resolve_target(monkeypatch)
        _patch_cocotb_target(monkeypatch, module="tb.test_noc")
        _write_tests_toml(tmp_path, '[default]\ntests = ["test_a", "test_b"]\n')

        endpoint = _make_endpoint(tmp_path, monkeypatch)
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        endpoint._run_elab("default", tmp_path, build_dir)
        endpoint._run_target_test_suite("default", tmp_path, build_dir, "tb")

        assert launched_work(captured[-1]).tests == ("test_a", "test_b")

    def test_cocotb_suite_whose_default_variants_differ_in_env_runs_in_batches(
        self, tmp_path: Path, monkeypatch
    ):
        """Regression: such a suite raised UnsupportedSimTargetError before any run.

        The Simulation Flow runs it as one process per environment; a mutant
        is graded the same way, each batch under its own Test Variant's
        environment and the Default Seed.
        """
        monkeypatch.delenv("MODE", raising=False)
        captured: list[tuple[list[str], dict[str, str]]] = []
        monkeypatch.setattr(
            "booley.flows.mutation.flow.subprocess.run",
            lambda cmd, *a, **k: (
                captured.append((list(cmd), dict(k.get("env") or {}))),
                _fake_proc(rc=0),
            )[1],
        )
        _patch_resolve_target(monkeypatch)
        _patch_cocotb_target(monkeypatch, module="tb.test_noc")
        _write_tests_toml(
            tmp_path,
            '[default]\ntests = ["test_a", "test_b", "test_c"]\ndefault_seed = 9\n'
            '[default.variants.test_a]\ndefault = "x"\nx = { env = { MODE = "A" } }\n'
            '[default.variants.test_b]\ndefault = "y"\ny = { env = { MODE = "B" } }\n',
        )
        endpoint = _make_endpoint(tmp_path, monkeypatch)
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        endpoint._run_elab("default", tmp_path, build_dir)

        runs = endpoint._run_target_test_suite("default", tmp_path, build_dir, "tb")

        launched = [(launched_work(cmd), env.get("MODE")) for cmd, env in captured[-3:]]
        assert [(work.tests, mode) for work, mode in launched] == [
            (("test_a",), "A"),
            (("test_b",), "B"),
            (("test_c",), None),
        ]
        assert [work.cocotb_seed for work, _mode in launched] == [9, 9, 9]
        assert [run.test_name for run in runs] == [
            "<cocotb-suite 1/3>",
            "<cocotb-suite 2/3>",
            "<cocotb-suite 3/3>",
        ]

    def test_individual_test_flag_was_removed(self, tmp_path: Path, monkeypatch):
        with pytest.raises(SystemExit):
            _make_endpoint(tmp_path, monkeypatch, extra_args=["--test", "test_b"])

    def test_classic_target_still_uses_verilator_run(self, tmp_path: Path, monkeypatch):
        captured: list[list[str]] = []
        monkeypatch.setattr(
            "booley.flows.mutation.flow.subprocess.run",
            lambda cmd, *a, **k: (captured.append(list(cmd)), _fake_proc(rc=0))[1],
        )
        _patch_resolve_target(monkeypatch)
        _patch_cocotb_target(monkeypatch, module=None)

        endpoint = _make_endpoint(tmp_path, monkeypatch)
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        endpoint._run_elab("default", tmp_path, build_dir)
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

        sim_cmd = captured[-1]
        work = launched_work(sim_cmd)
        assert sim_cmd == prepare_adapter_invocation(work, python=sys.executable)
        assert work.top == "tb"
        assert work.plusargs == ("verilator+seed+1",)
        assert not work.trace

    def test_classic_icarus_target_uses_iverilog_run(self, tmp_path: Path, monkeypatch):
        """F-6: a classic Icarus build must be executed as a vvp image."""
        captured: list[list[str]] = []
        monkeypatch.setattr(
            sys.modules[MutationFlow.__module__].subprocess,
            "run",
            lambda cmd, *a, **k: (captured.append(list(cmd)), _fake_proc(rc=0))[1],
        )
        _patch_resolve_target(monkeypatch, eda_tool="icarus")
        _patch_cocotb_target(monkeypatch, module=None, eda_tool="icarus")

        endpoint = _make_endpoint(tmp_path, monkeypatch)
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        endpoint._run_elab("default", tmp_path, build_dir)
        endpoint._run_sim_pinned("default", tmp_path, build_dir, "tb")

        sim_cmd = captured[-1]
        work = launched_work(sim_cmd)
        assert sim_cmd == prepare_adapter_invocation(work, python=sys.executable)
        assert work.build_dir == "build"
        assert not any("MUT_ID" in arg for arg in sim_cmd)

    def test_commercial_cocotb_target_fails_fast_with_reason(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """Unsupported => a typed error naming the reason, never a silent run."""
        _patch_cocotb_target(monkeypatch, module="tb.test_noc", eda_tool="xcelium")
        endpoint = _make_endpoint(tmp_path, monkeypatch, target="default")

        with pytest.raises(UnsupportedSimTargetError) as exc:
            endpoint.cocotb_target("default", tmp_path)
        assert "icarus and verilator only" in str(exc.value)

    def test_unsupported_target_aborts_before_any_build(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        scope = "rtl/mod_a.sv"
        _prepare_scope_files(tmp_path, [scope])
        _write_dut_top(tmp_path)
        _patch_cocotb_target(monkeypatch, module="tb.test_noc", eda_tool="vcs")
        endpoint = _make_endpoint(tmp_path, monkeypatch, target="default", scope=scope, count=1)
        # This test isolates runner compatibility; inspection refusal has its own tests.
        monkeypatch.setattr(endpoint, "_validate_scope_against_target", lambda _scope: None)
        with patch.object(endpoint, "_run_elab") as build:
            result = endpoint._run()

        assert result.exit_code == EXIT_ERROR
        assert "vcs" in result.report_text
        build.assert_not_called()


# ---------------------------------------------------------------------------
# SETUP-F-41 — infra failures are not the design's fault, and never a kill
# ---------------------------------------------------------------------------

_INFRA_OUT = (
    "ERROR: Verilator executable Vtb not found in build\n"
    "[SIM_INFRA_ERROR] Verilator executable Vtb not found in build\n"
)


def test_pristine_baseline_reports_missing_literal_run_cwd_before_elaboration(
    tmp_path: Path,
) -> None:
    """Issue #881: the baseline must not build before it knows run_cwd is usable."""
    state = tmp_path / ".booley"
    state.mkdir()
    (state / "booley.toml").write_text('[flows.sim]\nrun_cwd = "run"\n', encoding="utf-8")
    plan = SimpleNamespace(target="sim", work_dir=tmp_path, tb_top="tb")
    endpoint = MutationFlow()

    with patch.object(endpoint, "_run_elab", side_effect=AssertionError("built")) as elab:
        result = endpoint._run_pristine_baseline(plan)  # type: ignore[arg-type]

    assert result is not None
    assert result.exit_code == EXIT_ERROR
    assert "mutation baseline cannot run: literal run directory must already exist" in (
        result.report_text
    )
    assert ".gitkeep" in result.report_text
    assert result.detail["phase"] == "baseline_run_cwd"
    assert result.report_text.splitlines()[-1].startswith(
        "RESULT: ERROR (baseline: literal run directory must already exist"
    )
    elab.assert_not_called()


@pytest.mark.parametrize("layout", ["recursive", "declared"])
def test_scope_accepts_canonical_rtl_through_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    require_symlinks(tmp_path)
    _author_scope_core(tmp_path)
    if layout == "recursive":
        (tmp_path / "lib").mkdir()
        symlink_or_skip(tmp_path / "lib/self", Path(".."), target_is_directory=True)
        assert (tmp_path / "lib/self").is_dir()
        assert (tmp_path / "lib/self").resolve() == tmp_path.resolve()
        expected = "lib/self/rtl/counter.sv"
        original_plan = fusesoc_registry.prepare_core_library_plan

        def with_alias_library(project_root):
            plan = original_plan(project_root)
            return replace(
                plan,
                roots=(*plan.roots, tmp_path / "lib/self"),
                ignored_dirs=(*plan.ignored_dirs, frozenset({str((tmp_path / "lib").resolve())})),
            )

        # Real duplicate-core discovery, independent of platform traversal order.
        monkeypatch.setattr(fusesoc_registry, "prepare_core_library_plan", with_alias_library)
    else:
        symlink_or_skip(tmp_path / "design", "rtl", target_is_directory=True)
        core = tmp_path / "design.core"
        core.write_text(core.read_text().replace("rtl/", "design/"))
        expected = "design/counter.sv"
    catalog = TargetCatalog.build(tmp_path)
    inspection = catalog.inspect(catalog.select("sim"))
    assert expected in inspection.rtl_files
    endpoint = _make_endpoint(tmp_path, target="sim")
    assert endpoint._validate_scope_against_target(["rtl/counter.sv"]) is None
    testbench = endpoint._validate_scope_against_target(["tb/tb_counter.sv"])
    assert testbench is not None
    assert "is a testbench file" in testbench.report_text
    (tmp_path / "rtl/other.sv").write_text("// stub\n", encoding="utf-8")
    rejected = endpoint._validate_scope_against_target(["rtl/other.sv"])
    assert rejected is not None
    assert "does not match" in rejected.report_text
    assert expected in rejected.report_text


def test_scope_rejects_rtl_symlink_outside_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _author_scope_core(root)
    external = tmp_path / "external.sv"
    external.write_text("module counter; endmodule\n")
    source = root / "rtl/counter.sv"
    source.unlink()
    symlink_or_skip(source, external)
    endpoint = _make_endpoint(root, target="sim")
    rejected = endpoint._validate_scope_against_target([str(external)])
    assert rejected is not None
    assert "outside the Project" in rejected.report_text


def test_scope_refuses_when_identity_resolution_fails(tmp_path: Path) -> None:
    _author_scope_core(tmp_path)
    endpoint = _make_endpoint(tmp_path, target="sim")
    catalog = TargetCatalog.build(tmp_path)
    inspection = catalog.inspect(catalog.select("sim"))
    with (
        patch.object(TargetCatalog, "inspect", return_value=inspection),
        patch.object(
            fusesoc_registry, "canonical_project_path", side_effect=OSError("unreadable")
        ),
    ):
        result = endpoint._validate_scope_against_target(["rtl/counter.sv"])
        assert result is not None
        assert "unreadable" in result.report_text


def test_cpp_sim_harness_is_not_mutation_scope(tmp_path):
    _author_scope_core(tmp_path)
    core = tmp_path / "design.core"
    core.write_text(
        core.read_text()
        .replace(
            "  tb:\n",
            "  harness:\n    files: [tb/sim_main.cpp]\n    file_type: cppSource\n  tb:\n",
        )
        .replace("filesets: [rtl, tb]", "filesets: [rtl, tb, harness]")
    )
    (tmp_path / "tb/sim_main.cpp").write_text("int main() {}\n")
    endpoint = _make_endpoint(tmp_path, target="sim")
    result = endpoint._validate_scope_against_target(["tb/sim_main.cpp"])
    assert result is not None
    assert result.exit_code == EXIT_ERROR


def test_cpp_scope_refuses_before_generation_and_build(tmp_path):
    _author_scope_core(tmp_path)
    source = tmp_path / "tb/sim_main.cpp"
    source.write_text("int main() {}\n")
    endpoint = _make_endpoint(tmp_path, target="sim", scope="tb/sim_main.cpp")
    with (
        patch("booley.flows.mutation.flow.generate_mutants") as generate,
        patch.object(endpoint, "_run_elab") as build,
    ):
        result = endpoint._run()
    assert result.exit_code == EXIT_ERROR
    assert "not an HDL RTL mutation source" in result.report_text
    generate.assert_not_called()
    build.assert_not_called()


def test_default_scope_keeps_rtl_that_shares_a_directory_with_testbench_files(tmp_path):
    from tests.test_target_catalog import _category_sim_core

    _category_sim_core(tmp_path)
    endpoint = _make_endpoint(tmp_path, target="sim", scope=None)
    assert endpoint._validate_scope_against_target(["shared/dut.sv"]) is None
    assert endpoint._default_scope() == ["shared/dut.sv"]


@pytest.mark.parametrize("cocotb", [False, True])
@pytest.mark.parametrize("cached", [False, True])
def test_mutation_report_records_resolved_simulator(tmp_path, monkeypatch, cocotb, cached):
    import json

    tool = "verilator"

    _patch_cocotb_target(monkeypatch, module="tb_test" if cocotb else None, eda_tool=tool)
    endpoint = _make_endpoint(tmp_path, monkeypatch, target="default")
    build = tmp_path / "build"
    build.mkdir()
    if cached:
        (build / ".booley_edalize_eda_tool").write_text(tool)
    if cached:
        monkeypatch.setattr(
            endpoint,
            "_pinned_build",
            lambda *_args: SimpleNamespace(rel=".", runtime_inputs=()),
        )
        monkeypatch.setattr(
            endpoint,
            "_pinned_sim_command",
            lambda *_args, **_kwargs: ([tool], SimpleNamespace(environment={})),
        )
        monkeypatch.setattr("booley.flows.mutation.flow._target_environment", lambda *_args: {})
        monkeypatch.setattr("booley.flows.mutation.flow.launch_environment", lambda *_args: {})
    else:
        prepared = SimpleNamespace(
            eda_tool=tool,
            build_root=build,
            make_argv=("fake-build",),
            resolved=SimpleNamespace(files=()),
        )
        monkeypatch.setattr(endpoint, "_resolve_elab_target", lambda *_args: (object(), prepared))
        monkeypatch.setattr(
            "booley.flows.mutation.flow.simulation_build_environment", lambda *_args: {}
        )
    monkeypatch.setattr(
        "booley.flows.mutation.flow.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    if cached:
        endpoint._run_sim_pinned("default", tmp_path, build, "tb")
    else:
        endpoint._run_elab("default", tmp_path, build)
    path = endpoint.write_report(EndpointOutcome(report_text="mutation result"))
    assert json.loads(path.read_bytes()).get("eda_tool") == tool


def test_mutation_preflight_alone_does_not_report_eda_tool(tmp_path, monkeypatch):
    import json

    _patch_cocotb_target(monkeypatch, module="tb_test", eda_tool="verilator")
    endpoint = _make_endpoint(tmp_path, monkeypatch, target="default")
    endpoint.cocotb_target("default", tmp_path)
    endpoint._validate_target_runner("default", tmp_path)
    report = json.loads(endpoint.write_report(EndpointOutcome()).read_bytes())
    assert "eda_tool" not in report
    assert "eda_tools" not in report


def test_mutation_dir_falls_back_when_no_project_runtime_exists(monkeypatch) -> None:
    """Direct callers outside a Project get the relative scratch directory."""
    from booley.flows.mutation import workspace

    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)

    def _no_project() -> Path:
        raise FileNotFoundError

    monkeypatch.setattr("booley.runtime.booley_dir.runtime_dir", _no_project)
    assert workspace.mutation_dir() == Path("mutation_runtime") / "mutation"


def test_validation_errors_end_with_a_result_error_line(tmp_path: Path, monkeypatch) -> None:
    """AC3: each direct validation refusal reports its phase on a final ``RESULT:`` line."""
    for count, min_detected, seed, message in [
        (0, 0, 0, "--count must be positive"),
        (3, 0, -1, "--seed must be a non-negative integer"),
        (3, 4, 0, "--min-detected must be between 0 and --count"),
        (3, -1, 0, "--min-detected must be between 0 and --count"),
    ]:
        outcome = MutationFlow._validate_count(count, min_detected, seed)
        assert outcome is not None
        assert outcome.exit_code == EXIT_ERROR
        assert outcome.report_text == f"{message}\n\nRESULT: ERROR (options: {message})"
    assert MutationFlow._validate_count(3, 3, 0) is None

    endpoint = _make_endpoint(tmp_path, monkeypatch)
    outcome = endpoint._validate_run_inputs("a,b", tmp_path, [])
    assert outcome is not None
    message = "--target must select exactly one Target"
    assert outcome.report_text == f"mutation: {message}\n\nRESULT: ERROR (options: {message})"

    no_target = _make_endpoint(tmp_path, monkeypatch, target="")
    outcome = no_target._validate_scope_against_target([])
    assert outcome is not None
    message = "scope validation requires a Target"
    assert outcome.report_text == f"mutation: {message}\n\nRESULT: ERROR (scope: {message})"

    monkeypatch.setattr("booley.flows.mutation.flow.tb_top_for_target", lambda *_a, **_k: "")
    monkeypatch.setattr(endpoint, "_validate_run_inputs", lambda *_args: None)
    outcome = endpoint._prepare_run_plan()
    assert isinstance(outcome, EndpointOutcome)
    message = "selected Target does not declare a toplevel"
    assert outcome.report_text == f"mutation: {message}\n\nRESULT: ERROR (target: {message})"


def test_flow_overrides_run_so_the_template_hooks_are_inert(tmp_path: Path, monkeypatch) -> None:
    endpoint = _make_endpoint(tmp_path, monkeypatch)
    assert endpoint._build_command() == []
    outcome = endpoint._interpret_result(_fake_proc())
    assert outcome.exit_code == 0


# ---------------------------------------------------------------------------
# Equivalent Mutants: on a Verilator Target a Mutant that builds the baseline's
# C++ is replaced by the next candidate (ADR 0088, amendment 2026-11-13)
# ---------------------------------------------------------------------------

# `y = 4'd0` is a default every `case` branch overwrites: the fake build drops its line.
_DEAD_DEFAULT_RTL = """\
module m (input logic [1:0] sel, input logic [3:0] a, input logic [3:0] b, output logic [3:0] y);
  always_comb begin
    y = 4'd0; // dead
    case (sel)
      2'd0: y = a & b;
      2'd1: y = a | b;
      2'd2: y = a ^ b;
      default: y = ~a;
    endcase
  end
endmodule
"""
_DEAD_LINE = 3
_VERILATED = {"verilate": _write_fake_verilation}  # builds leave the C++ that is compared


def _write_scope(tmp_path: Path, text: str = _DEAD_DEFAULT_RTL) -> None:
    (tmp_path / "rtl").mkdir(exist_ok=True)
    (tmp_path / _SCOPE).write_bytes(text.encode())


def _candidates(tmp_path: Path, seed: int = 0) -> tuple[Any, ...]:
    """Every candidate Mutant of the scope in pick order: the run's selection is a prefix.

    Replacements for dropped Mutants follow that prefix in the same order.
    """
    from booley.flows.mutation.generator import generate_mutants

    return generate_mutants(tmp_path, [_SCOPE], 10_000, seed).mutants


def _expected_sweep(ranked: tuple[Any, ...], count: int) -> tuple[list[str], list[str]]:
    """The (kept, dropped) Mutant IDs when the Mutants on the dead line build the baseline's C++."""
    kept: list[str] = []
    dropped: list[str] = []
    for mutant in ranked:
        if len(kept) == count:
            break
        (dropped if mutant.line == _DEAD_LINE else kept).append(mutant.id)
    return kept, dropped


def _expected_builds(ranked: tuple[Any, ...], count: int) -> list[int | None]:
    """The Mutant numbers built in order (after the baseline's ``None``): a dropped
    candidate's number is built again by the next candidate."""
    builds: list[int | None] = [None]
    kept = 0
    for mutant in ranked:
        if kept == count:
            break
        builds.append(kept + 1)
        kept += mutant.line != _DEAD_LINE
    return builds


def test_verilator_equivalent_mutant_is_replaced_by_the_next_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    _write_scope(tmp_path)
    ranked = _candidates(tmp_path)
    kept, dropped = _expected_sweep(ranked, 4)
    assert dropped, "the fixture must put a dead-line Mutant among the first candidates"

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=4,
        seed=0,
        min_detected=0,
        run=_detects_every_mutant,
        simulation=_VERILATED,
    )

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert [row["id"] for row in result.detail["classified"]] == kept
    assert result.detail["total_requested"] == len(result.detail["classified"]) == 4
    assert "shortfall" not in result.detail
    # The dropped Mutant was built (its C++ was compared) but never simulated, and the
    # next candidate took its number and build directory.
    assert fake.runs == [None, 1, 2, 3, 4]
    assert fake.builds == _expected_builds(ranked, 4)
    assert len(fake.builds) == 1 + len(kept) + len(dropped)
    # Each build's C++ names its own private copy by absolute path, as `$error` does, and
    # the dropped Mutant still matched the baseline's.
    assert result.detail["equivalent_dropped"] == dropped


def test_verilator_mutant_that_changes_the_build_is_scored(tmp_path: Path, monkeypatch) -> None:
    _write_scope(tmp_path)
    live = _candidates(tmp_path)[0]
    assert live.line != _DEAD_LINE

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=1,
        seed=0,
        min_detected=1,
        run=_detects_every_mutant,
        simulation=_VERILATED,
    )

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert fake.builds == [None, 1]
    assert fake.runs == [None, 1]  # the Mutant's tests ran
    assert [(row["id"], row["status"]) for row in result.detail["classified"]] == [
        (live.id, "detected")
    ]
    assert result.detail["equivalent_dropped"] == []


def test_dropped_mutants_are_in_the_detail_not_the_report(tmp_path: Path, monkeypatch) -> None:
    _write_scope(tmp_path)
    _, dropped = _expected_sweep(_candidates(tmp_path), 4)
    assert dropped

    _, result, _ = _campaign(
        tmp_path, monkeypatch, count=4, seed=0, min_detected=0, simulation=_VERILATED
    )

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert result.detail["equivalent_dropped"] == dropped
    campaign = (tmp_path / result.detail["artifacts"]["manifest"]).parent
    texts = [
        result.report_text,
        _results_markdown(tmp_path, result),
        (campaign / "mutation-specs.md").read_text(encoding="utf-8"),
        (campaign / "manifest.json").read_text(encoding="utf-8"),
        json.dumps(result.detail["classified"]),
    ]
    for text in texts:
        for mutant_id in dropped:
            assert mutant_id not in text
        assert f"{_SCOPE}:{_DEAD_LINE})" not in text and f"{_SCOPE}:{_DEAD_LINE}]" not in text
        assert "y = 4'd0" not in text
    assert [row["index"] for row in result.detail["variant_files"]] == [1, 2, 3, 4]
    assert "equivalent_dropped" not in result.report_text


def test_running_out_during_replacement_reports_the_shortfall(tmp_path: Path, monkeypatch) -> None:
    _write_scope(tmp_path)
    ranked = _candidates(tmp_path)
    kept, dropped = _expected_sweep(ranked, len(ranked))
    assert dropped and len(kept) < len(ranked)

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=len(ranked),
        seed=0,
        run=_detects_every_mutant,
        simulation=_VERILATED,
    )

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    expected = (
        f"only {len(kept)} candidate Mutants exist; "
        f"returning all of them ({len(ranked)} requested)"
    )
    assert result.detail["shortfall"] == expected
    assert expected in result.report_text
    assert [row["id"] for row in result.detail["classified"]] == kept  # the Mutants it has
    assert result.detail["equivalent_dropped"] == dropped
    # The default threshold follows the Mutants kept, not the ones asked for.
    assert result.detail["min_detected"] == _eighty_percent(len(kept))
    assert result.detail["total_requested"] == len(kept)
    assert fake.runs == [None, *range(1, len(kept) + 1)]


def test_explicit_threshold_survives_a_shortfall_after_drops(tmp_path: Path, monkeypatch) -> None:
    _write_scope(tmp_path)
    count = len(_candidates(tmp_path))

    _, result, _ = _campaign(
        tmp_path,
        monkeypatch,
        count=count,
        seed=0,
        min_detected=count,
        run=_detects_every_mutant,
        simulation=_VERILATED,
    )

    assert result.exit_code == EXIT_FAILURE  # more were required than the run could score
    assert result.detail["min_detected"] == count


def test_icarus_target_simulates_every_mutant_without_comparing(
    tmp_path: Path, monkeypatch
) -> None:
    _write_scope(tmp_path)
    ranked = _candidates(tmp_path)
    assert any(mutant.line == _DEAD_LINE for mutant in ranked)

    def refuse(*_args: Any) -> None:
        pytest.fail("an Icarus Target must not compare builds")

    monkeypatch.setattr("booley.flows.mutation.flow.generated_cpp", refuse, raising=False)
    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=4,
        seed=0,
        min_detected=0,
        run=_detects_every_mutant,
        simulation={"eda_tool": "icarus", "verilate": _write_fake_verilation},
    )

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert fake.runs == [None, 1, 2, 3, 4]
    assert [row["id"] for row in result.detail["classified"]] == [m.id for m in ranked[:4]]
    assert result.detail["equivalent_dropped"] == []
    assert "verilator_version" not in result.detail["identity"]


def test_invalid_verilator_mutant_is_not_replaced(tmp_path: Path, monkeypatch) -> None:
    _write_scope(tmp_path)
    first = _candidates(tmp_path)[0]

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=1,
        seed=0,
        min_detected=0,
        build=lambda index: _fake_proc(rc=0 if index != 1 else 1, stdout="", stderr="syntax"),
        simulation=_VERILATED,
    )

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert [(row["id"], row["status"]) for row in result.detail["classified"]] == [
        (first.id, "invalid")
    ]
    assert fake.builds == [None, 1]  # candidates remain, but an invalid Mutant keeps its slot
    assert result.detail["equivalent_dropped"] == []


def test_verilator_version_separates_run_identities(tmp_path: Path, monkeypatch) -> None:
    identities = []
    for name, simulation in (
        ("old", {"verilator": "Verilator 5.052 2025-01-01"}),
        ("new", {"verilator": "Verilator 5.054 2025-06-01"}),
        ("icarus", {"eda_tool": "icarus"}),
    ):
        root = tmp_path / name
        root.mkdir()
        _write_scope(root)
        _, result, _ = _campaign(
            root, monkeypatch, count=2, seed=0, min_detected=0, simulation=simulation
        )
        assert result.exit_code == EXIT_SUCCESS, result.report_text
        identities.append(result.detail["identity"])

    old, new, icarus = identities
    assert old["verilator_version"] == "Verilator 5.052 2025-01-01"
    assert new["verilator_version"] == "Verilator 5.054 2025-06-01"
    assert old != new
    assert {**old, "verilator_version": None} == {**new, "verilator_version": None}
    assert "verilator_version" not in icarus


def test_inconclusive_run_after_a_drop_names_only_kept_mutants(
    tmp_path: Path, monkeypatch
) -> None:
    _write_scope(tmp_path)
    ranked = _candidates(tmp_path)
    kept, dropped = _expected_sweep(ranked, 3)
    assert dropped

    def run(index: int | None) -> subprocess.CompletedProcess:
        if index == 2:
            return _fake_proc(rc=1, stdout=_INFRA_OUT)
        return _fake_proc(rc=0 if index is None else 1, stdout="[sim] run")

    _, result, _ = _campaign(
        tmp_path, monkeypatch, count=3, seed=0, run=run, simulation=_VERILATED
    )

    assert result.exit_code == EXIT_ERROR, result.report_text
    assert result.detail["phase"] == "variant_infrastructure"
    assert result.detail["equivalent_dropped"] == dropped
    assert [mutation["id"] for mutation in result.detail["mutations"]] == kept
    assert [row["id"] for row in result.detail["classified"]] == kept
    assert [row["index"] for row in result.detail["variant_files"]] == [1, 2, 3]
    for mutant_id in dropped:
        assert mutant_id not in json.dumps(result.detail["mutations"])
    assert f"{_SCOPE}:{_DEAD_LINE}" not in result.report_text


def test_run_whose_every_candidate_builds_the_baseline_has_nothing_to_score(
    tmp_path: Path, monkeypatch
) -> None:
    _write_scope(tmp_path, "module m;\n  assign y = a + b; // dead\nendmodule\n")
    ranked = _candidates(tmp_path)

    _, result, fake = _campaign(tmp_path, monkeypatch, count=2, seed=0, simulation=_VERILATED)

    assert result.exit_code == EXIT_ERROR
    assert "no candidate Mutant changes what the Target builds" in result.report_text
    assert result.detail["phase"] == "mutant_selection"
    assert result.report_text.splitlines()[-1] == (
        "RESULT: ERROR (selection: no candidate Mutant changes what the Target builds)"
    )
    assert result.detail["equivalent_dropped"] == [m.id for m in ranked]
    assert fake.runs == [None]  # no Mutant was simulated


def test_mutant_whose_build_lacks_the_verilator_layout_is_still_simulated(
    tmp_path: Path, monkeypatch
) -> None:
    _write_scope(tmp_path)

    def only_the_baseline_has_a_layout(build_root: Path) -> None:
        if build_root.name == "baseline":
            _write_fake_verilation(build_root)

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=4,
        seed=0,
        min_detected=0,
        simulation={"verilate": only_the_baseline_has_a_layout},
    )

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert fake.runs == [None, 1, 2, 3, 4]  # nothing matches the baseline: all are simulated
    assert result.detail["equivalent_dropped"] == []
