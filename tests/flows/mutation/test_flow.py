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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.job_resources import ResourceLabel
from booley.flows.metric_declarations import MetricsUnavailable
from booley.flows.mutation.flow import (
    MutationFlow,
    MutationResult,
    MutationRunPlan,
    MutationSpec,
    MutationSummary,
    UnsupportedSimTargetError,
    generate_results_markdown,
)
from booley.flows.mutation.memory_claims import CampaignClaims
from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.build import PreparedSimulationBuild, SimulationBuildPreparationError
from booley.flows.target_test_suite import NoRunnableTestsError
from booley.fusesoc import fusesoc_registry
from booley.runtime import resource_history, runtime_context
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    EndpointOutcome,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import IncompatibleTargetError
from tests.canned_process_support import canned_command
from tests.conftest import require_symlinks, symlink_or_skip
from tests.flows.sim.launched_work import launched_work
from tests.target_test_support import write_two_core_sim_project

# The parameters the fake ``.core`` Target declares; a test may add plusargs.
_TARGET_PARAMETERS: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flow_env(logs_dir: Path, slug: str = "test-ticket") -> dict[str, str]:
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = slug
    env["BOOLEY_LOGS_DIR"] = str(logs_dir)
    return env


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
    extra_args: list[str] | None = None,
    state_file: bool = True,
) -> MutationFlow:
    """Build a endpoint with parsed args.

    ``state_file=True`` names an opaque ``BOOLEY_STATE_FILE``; ``False`` names none.

    When *monkeypatch* is supplied, the BOOLEY_LOGS_DIR env var is set for
    the lifetime of the test so the run's scratch files land in tmp_path.
    """
    state_path = tmp_path / "state.json"
    if state_file:
        state_path.write_bytes(b'{"opaque": true}\n')
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
    env = _flow_env(logs_dir)
    if state_file:
        env["BOOLEY_STATE_FILE"] = str(state_path)
    else:
        env.pop("BOOLEY_STATE_FILE", None)
    if monkeypatch is not None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        if not state_file:
            monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)
    endpoint = MutationFlow()
    with patch.dict(os.environ, env):
        endpoint.parse_args(argv)
    if monkeypatch is not None:
        monkeypatch.setattr(
            "booley.flows.mutation.flow.tb_top_for_target",
            lambda *_args, **_kwargs: tb_top,
        )
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
    monkeypatch.setattr(
        "booley.flows.mutation.flow.require_runnable_target_test_suite",
        MagicMock(side_effect=NoRunnableTestsError("sim", ("smoke", "corner"))),
    )

    _, result, _ = _campaign(tmp_path, monkeypatch)

    assert result.exit_code == EXIT_ERROR
    assert "no runnable tests" in result.report_text


def test_two_keys_for_one_target_refuse_naming_both(tmp_path: Path, monkeypatch) -> None:
    write_two_core_sim_project(
        tmp_path, '["y#sim"]\ntests = ["one"]\n["vendor:b:y#sim"]\ntests = ["two"]\n'
    )
    endpoint = _make_endpoint(tmp_path, monkeypatch, target="y#sim")

    result = endpoint.run_with_claims(CampaignClaims())

    assert result.exit_code == EXIT_ERROR
    assert "[vendor:b:y#sim] and [y#sim]" in result.report_text


def _eighty_percent(count: int) -> int:
    """``ceil(0.8 * count)`` in integers (float ``ceil(0.8 * 15)`` gives 13)."""
    return -(-4 * count // 5)


def _fake_proc(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summary_classifies_every_result_state() -> None:
    summary = MutationSummary(
        specs=_sample_specs(4),
        results=[
            MutationResult(index=1, detected=True),
            MutationResult(index=2, detected=False),
            MutationResult(index=3, detected=False, inconclusive_reason="no observation"),
        ],
    )
    assert summary.detected_count == 1
    assert summary.not_detected_count == 1
    assert [row["status"] for row in summary.classify()] == [
        "detected",
        "not_detected",
        "inconclusive",
        "untested",
    ]


def _run_count_preview(endpoint, monkeypatch):
    """Dry-run *endpoint* on the fake Target: it creates no file and runs no build or run."""
    fake = _patch_fake_simulation(monkeypatch)
    before = set(endpoint.args.work_dir.rglob("*"))
    result = endpoint.run_with_claims(CampaignClaims())
    assert set(endpoint.args.work_dir.rglob("*")) == before
    assert fake.builds == fake.runs == []
    return result, fake


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
    assert any(f") = {count}" in line for line in result.display_lines)


def test_auto_count_is_case_insensitive_in_dry_run(tmp_path, monkeypatch):
    """``--count AUTO`` prints the same auto explanation as ``--count auto``."""
    for name in ("mod_a.sv", "mod_b.sv"):
        source = tmp_path / "rtl" / name
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(b"// source\n" * 4)
    extra_args = ["--dry-run", "--timeout-ms", "1800000"]
    baseline_endpoint = _make_endpoint(tmp_path, monkeypatch, count="auto", extra_args=extra_args)
    baseline, _ = _run_count_preview(baseline_endpoint, monkeypatch)
    assert "count (auto):" in baseline.report_text

    for spelling in ("AUTO", "Auto", " auto "):
        endpoint = _make_endpoint(tmp_path, monkeypatch, count=spelling, extra_args=extra_args)
        assert endpoint.args.count == "auto"
        result, _ = _run_count_preview(endpoint, monkeypatch)
        assert result.exit_code == EXIT_SUCCESS, spelling
        assert result.report_text == baseline.report_text, spelling


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
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    _patch_fake_simulation(monkeypatch)
    _make_endpoint(tmp_path, monkeypatch)  # the run's environment and Target top

    code = MutationFlow().main(
        [
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
            "--target",
            "lite",
            "--scope",
            ",".join(paths),
            "--count",
            "auto",
            "--dry-run",
        ]
    )

    events = [
        json.loads(line)
        for line in (runtime_dir / "display.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    [end] = [event for event in events if event["type"] == "endpoint_end"]
    assert code == EXIT_SUCCESS
    assert end["display_lines_truncated"] > 0
    assert end["summary"].startswith("mutation dry-run:")


# ---------------------------------------------------------------------------
# Artifact generation
# ---------------------------------------------------------------------------


def test_results_markdown_links_mutated_rtl_escapes_operators_and_names_the_killing_test() -> None:
    spec = MutationSpec(
        index=1,
        category="operator_change",
        file="rtl/mod (wide).sv",
        line=12,
        original_code="a | b",
        mutated_code="a & b",
    )
    summary = MutationSummary(
        specs=[spec, _sample_specs(2)[1]],
        results=[
            MutationResult(index=1, detected=False),
            MutationResult(index=2, detected=True, first_killing_test="test_rx_overrun"),
        ],
    )

    md = generate_results_markdown(
        summary, 1, variant_paths={1: "variants/mutant_1/rtl/mod (wide).sv"}
    )

    assert "[rtl/mod (wide).sv:12](variants/mutant_1/rtl/mod%20%28wide%29.sv)" in md
    assert "`a \\| b` → `a & b`" in md
    assert "not detected" in md
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
        toplevel="",
        eda_tool="verilator",
        rtl_files=tuple(paths),
        inputs=tuple(
            TargetInput(path, "acme:test:fixture:1", "systemVerilogSource", (), False, {})
            for path in paths
        ),
    )


_VERILATOR_VERSION = "Verilator 5.052 2025-01-01 rev v5.052"


def _patch_resolve_target(
    monkeypatch,
    *,
    eda_tool: str | None = "verilator",
    verilator: str = _VERILATOR_VERSION,
    cocotb_module: str | None = None,
    measured: bool = False,
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
            cocotb_module=cocotb_module,
        )

    def _fake_prepare(handle, *, build_root, lane_kind, environment=None, **kwargs):
        assert lane_kind == "unreserved"
        import yaml

        if measured and kwargs.get("setup_runner") is not None:
            # Stands for the FuseSoC setup the real preparer runs through this runner.
            kwargs["setup_runner"](
                canned_command(SubprocessResult(returncode=0)),
                cwd=str(handle.project_root),
                env=dict(os.environ),
                capture_output=True,
                text=True,
            )

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
            environment=dict(environment or {}),
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
    run_timeouts: list[float] = field(default_factory=list)  # the hard kill of each run
    run_limits: list[int] = field(default_factory=list)  # the limit the simulator was given
    build_environments: list[dict[str, str]] = field(default_factory=list)  # each build's env
    launches: list[tuple[PreparedSimulationWork, dict[str, str]]] = field(default_factory=list)


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
    run_work: Callable[[int | None, PreparedSimulationWork], subprocess.CompletedProcess]
    | None = None,
    build: Callable[[int | None], subprocess.CompletedProcess] | None = None,
    eda_tool: str = "verilator",
    cocotb_module: str | None = None,
    verilate: Callable[[Path], None] | None = None,
    verilator: str = _VERILATOR_VERSION,
    measured: bool = False,
) -> FakeSimulation:
    """Fake every build and simulator run of a mutation campaign.

    ``run(index)`` and ``build(index)`` return the process for the baseline
    (``index`` is ``None``) or Mutant ``index``; by default everything passes.
    A ``run`` may raise ``subprocess.TimeoutExpired``; ``run_work(index, work)`` answers
    with the launched work too (for a fake that answers per test).  ``verilate(build_root)`` runs after
    a build that passes, to leave what Verilator would (``_write_fake_verilation``);
    ``verilator`` is the version the baseline build reports. With *measured* each answer
    (and each FuseSoC setup) is replayed by a real child through the boundary that
    measures and records it.
    """
    _patch_resolve_target(
        monkeypatch,
        eda_tool=eda_tool,
        verilator=verilator,
        cocotb_module=cocotb_module,
        measured=measured,
    )
    fake = FakeSimulation()
    import booley.flows.mutation.flow as mutation_flow_module

    # The boundary the Flow starts every tool through; the tools' own ``subprocess.run`` on main.
    original = getattr(mutation_flow_module, "run_tool", subprocess.run)

    def _answer(answer: subprocess.CompletedProcess, kwargs: dict[str, Any]):
        """The canned answer, or with *measured* a real child that replays it through the boundary."""
        if not measured:
            return answer
        from booley.flows.mutation.tool_runs import run_tool as real_run_tool

        replay = SubprocessResult(answer.returncode, answer.stdout or "", answer.stderr or "")
        return real_run_tool(canned_command(replay), **kwargs)

    def _fake(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["make", "-C"]:
            index = _mutant_index(" ".join(cmd))
            fake.builds.append(index)
            fake.build_environments.append(dict(kwargs.get("env") or {}))
            built = build(index) if build else _fake_proc(rc=0, stdout="[make] ok")
            if verilate and built.returncode == 0:
                verilate(Path(cmd[2]))
            return _answer(built, kwargs)
        if isinstance(cmd, list) and "booley.flows.sim.backends." in " ".join(cmd):
            work = launched_work(cmd)
            index = _mutant_index(repr(work))
            fake.runs.append(index)
            fake.run_timeouts.append(kwargs["timeout"])
            fake.run_limits.append(work.timeout_s)
            fake.launches.append((work, dict(kwargs.get("env") or {})))
            if run_work:
                return run_work(index, work)
            return _answer(run(index) if run else _fake_proc(rc=0, stdout="[sim] ok"), kwargs)
        return original(cmd, *args, **kwargs)

    monkeypatch.setattr(
        "booley.flows.mutation.flow.run_tool",
        _fake,
        raising=False,  # the criterion test must fail in its body on main
    )
    return fake


_SMOKE_TESTS = '[default]\ntests = ["smoke"]\nselect = "+test={name}"\n'


def _wrap_preparer(monkeypatch, **replaced: Any) -> list[tuple[Any, dict[str, Any]]]:
    """Record each ``prepare_simulation_build`` call; the result carries *replaced* fields."""
    import booley.flows.mutation.flow as mutation_flow_module

    calls: list[tuple[Any, dict[str, Any]]] = []
    prepare = mutation_flow_module.prepare_simulation_build

    def recording_prepare(handle, **kwargs):
        calls.append((handle, kwargs))
        return replace(prepare(handle, **kwargs), **replaced)

    monkeypatch.setattr(mutation_flow_module, "prepare_simulation_build", recording_prepare)
    return calls


def _wrap_commands(
    monkeypatch, answers: dict[str, subprocess.CompletedProcess]
) -> list[list[str]]:
    """Record every command; one whose program is in *answers* gets that answer, the rest
    go to the simulation fake installed before."""
    import booley.flows.mutation.flow as mutation_flow_module

    commands: list[list[str]] = []
    fake_run = mutation_flow_module.run_tool

    def recording_run(cmd, *args, **kwargs):
        commands.append(list(cmd))
        if cmd[0] in answers:
            return answers[cmd[0]]
        return fake_run(cmd, *args, **kwargs)

    monkeypatch.setattr("booley.flows.mutation.flow.run_tool", recording_run)
    return commands


def test_baseline_builds_then_runs_the_verilator_binary(tmp_path: Path, monkeypatch):
    """The baseline is built, then run through the configured simulator."""
    launches, fake = _baseline_launches(tmp_path, monkeypatch, _SMOKE_TESTS)

    [(work, _environment)] = launches
    assert fake.builds[0] is None
    assert work.adapter == "verilator"
    assert work.top == "design_top_tb"
    assert not work.trace
    # The build records the bin dir marker for the run-many loop to reuse.
    [baseline_root] = [
        marker.parent
        for marker in tmp_path.rglob(".booley_edalize_bindir")
        if "mutant_" not in marker.parent.name
    ]
    assert baseline_root.is_dir()


def test_baseline_build_runs_the_exact_command_of_the_shared_preparer(
    tmp_path: Path, monkeypatch
) -> None:
    _prepare_scope_files(tmp_path, [_SCOPE])
    _patch_fake_simulation(monkeypatch)
    prepares = _wrap_preparer(monkeypatch, make_argv=("custom-build", "--exact-command"))
    commands = _wrap_commands(monkeypatch, {"custom-build": _fake_proc(rc=0)})
    probed: list[dict[str, str]] = []
    monkeypatch.setattr(
        "booley.flows.mutation.flow.verilator_version",
        lambda environment: probed.append(dict(environment)) or "Verilator 5.052",
    )
    endpoint = _make_endpoint(tmp_path, monkeypatch, scope=_SCOPE, count=1, min_detected=0)

    result = endpoint.run_with_claims(CampaignClaims())

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    _handle, kwargs = prepares[0]
    assert kwargs.pop("build_root").name == "baseline"
    assert callable(kwargs.pop("setup_runner"))  # the measured FuseSoC setup
    assert kwargs == {"lane_kind": "unreserved", "core_library": None, "environment": {}}
    assert commands[0] == ["custom-build", "--exact-command"]
    # The baseline build records the version of the Verilator it builds with.
    [version] = tmp_path.rglob(".booley_verilator_version")
    assert version.read_text(encoding="utf-8") == "Verilator 5.052"
    assert len(probed) == 1
    assert probed[0]["PATH"] == os.environ["PATH"]


@pytest.mark.parametrize(
    "message",
    (
        "Target parameter intent is inconsistent",
        "Doctor requested a bad simulation fixture but its overlay is empty",
        "Target 'default' declared 'icarus' but setup configured 'verilator'",
    ),
)
def test_baseline_surfaces_shared_preparation_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    def failing_prepare(*args, **kwargs):
        raise SimulationBuildPreparationError(message)

    _prepare_scope_files(tmp_path, [_SCOPE])
    fake = _patch_fake_simulation(monkeypatch)
    monkeypatch.setattr("booley.flows.mutation.flow.prepare_simulation_build", failing_prepare)
    endpoint = _make_endpoint(tmp_path, monkeypatch, scope=_SCOPE, count=1)

    result = endpoint.run_with_claims(CampaignClaims())

    assert result.exit_code == EXIT_ERROR
    assert "RESULT: ERROR (elaboration: pristine source did not elaborate)" in result.report_text
    assert message in result.report_text
    assert fake.builds == []


def test_build_and_adapter_parent_relocate_python_artifacts(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime))

    launches, fake = _baseline_launches(tmp_path, monkeypatch, _SMOKE_TESTS)

    expected = runtime / "python-artifacts"
    for environment in (fake.build_environments[0], launches[0][1]):
        assert environment["PYTHONPYCACHEPREFIX"] == str(expected / "bytecode")
        assert f"cache_dir={expected / 'pytest'}" in environment["PYTEST_ADDOPTS"]


def test_mutant_build_uses_managed_compiler_cache(tmp_path: Path, monkeypatch):
    """Mutant builds compile through the Project cache like ordinary Simulation."""
    from booley.flows.sim.compiler_cache import CompilerCachePolicy
    from booley.runtime.compiler_cache import COMPILER_CACHE_RELATIVE

    policy = CompilerCachePolicy(True, tmp_path / "data" / COMPILER_CACHE_RELATIVE, "5G")
    _prepare_scope_files(tmp_path, [_SCOPE])
    fake = _patch_fake_simulation(monkeypatch)
    _wrap_preparer(monkeypatch, environment=policy.environment(), compiler_cache=policy)
    monkeypatch.setattr("booley.flows.sim.compiler_cache.shutil.which", lambda *a, **kw: "/ccache")
    (tmp_path / "data").mkdir()
    endpoint = _make_endpoint(tmp_path, monkeypatch, scope=_SCOPE, count=1, min_detected=0)

    endpoint.run_with_claims(CampaignClaims())

    environment = fake.build_environments[0]
    assert environment["OBJCACHE"] == "ccache"
    assert environment["CCACHE_DIR"] == str(policy.root)
    assert policy.root.is_dir()


@pytest.mark.parametrize("target", ["default", "lib:ip:core#default"])
def test_sim_runs_every_configured_test_selector(tmp_path: Path, monkeypatch, target: str):
    """A VLNV-qualified --target still finds the bare tests.toml section.

    Regression for the raw ``TEST_NAMES.get(target)`` lookup: a qualified
    target (``lib:ip:core#default``) missed the bare ``[default]`` section, so
    no selector was rendered and every mutant ran the TB default test —
    the sweep read "survived" across the board with nothing said.
    """
    launches, _ = _baseline_launches(
        tmp_path,
        monkeypatch,
        '[default]\ntests = ["coremark.elf", "smoke.elf"]\nselect = "--meminit=ram,{name}"\n',
        target=target,
    )

    assert ["--meminit=ram,coremark.elf" in work.plusargs for work, _env in launches] == [
        True,
        False,
    ]
    assert ["--meminit=ram,smoke.elf" in work.plusargs for work, _env in launches] == [False, True]


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


def test_sim_passes_the_target_plusargs_like_the_simulation_flow(tmp_path: Path, monkeypatch):
    """The Target's plusargs come first; ``select`` replaces one that sets its option."""
    monkeypatch.setitem(_TARGET_PARAMETERS, "depth", {"paramtype": "plusarg", "default": 4})
    monkeypatch.setitem(_TARGET_PARAMETERS, "test", {"paramtype": "plusarg", "default": "none"})
    launches, _ = _baseline_launches(tmp_path, monkeypatch, _MUTATION_VARIANTS)

    assert [work.plusargs for work, _env in launches] == [
        ("depth=4", "test=reset", "verilator+seed+7"),
        ("depth=4", "test=stress", "iters=1000", "verilator+seed+7"),
    ]


def test_sim_refuses_a_variant_argument_the_target_owns(tmp_path: Path, monkeypatch):
    """The plan is built against the Target's declared plusargs, as in the Flow."""
    monkeypatch.setitem(_TARGET_PARAMETERS, "iters", {"paramtype": "plusarg", "default": 1})
    with pytest.raises(UnsupportedSimTargetError, match="overrides a plusarg the Target declares"):
        _baseline_launches(tmp_path, monkeypatch, _MUTATION_VARIANTS)


def test_mutant_builds_and_runs_under_the_target_environment(tmp_path: Path, monkeypatch):
    """A mutant runs the Test Runs of a plain Simulation Flow run: default variant and seed.

    Regression: the Target's tests.toml env table never reached a mutant.
    The Simulation Flow builds and launches under that table, so a mutant
    graded without it runs a different testbench configuration.
    """
    monkeypatch.delenv("FLAVOR", raising=False)
    monkeypatch.delenv("STRESS_MODE", raising=False)
    tests_toml = _MUTATION_VARIANTS.replace(
        "default_seed = 7", 'default_seed = 7\nenv = { FLAVOR = "wide" }'
    )

    launches, fake = _baseline_launches(tmp_path, monkeypatch, tests_toml)

    assert [work.plusargs for work, _env in launches] == [
        ("test=reset", "verilator+seed+7"),
        ("test=stress", "iters=1000", "verilator+seed+7"),
    ]
    assert fake.build_environments[0]["FLAVOR"] == "wide"
    assert [env.get("FLAVOR") for _work, env in launches] == ["wide", "wide"]
    # The Test Variant's variable is added on top, for its own run only.
    assert [env.get("STRESS_MODE") for _work, env in launches] == [None, "short"]


def test_sim_on_an_unseeded_target_passes_no_seed(tmp_path: Path, monkeypatch):
    launches, _ = _baseline_launches(
        tmp_path,
        monkeypatch,
        '[default]\ntests = ["reset"]\nselect = "+test={name}"\n',
        eda_tool="icarus",
    )
    assert launches[0][0].plusargs == ("test=reset",)


def test_mutant_launch_forwards_the_project_sim_settings(tmp_path: Path, monkeypatch):
    """Verdict sentinels, and the trace files the adapter can clear a stale pipe at."""
    _write_tests_toml(tmp_path, _SMOKE_TESTS)
    (tmp_path / ".booley" / "booley.toml").write_text(
        '[project]\nname = "demo"\n\n[flows.sim]\n'
        'pass_sentinels = ["Correct operation validated."]\n'
        'fail_sentinels = ["ERROR!"]\n'
        'trace_files = ["fpu.vcd"]\n',
        encoding="utf-8",
    )

    launches, _ = _baseline_launches(tmp_path, monkeypatch)

    [(work, _environment)] = launches
    assert work.pass_sentinels == ("Correct operation validated.",)
    assert work.fail_sentinels == ("ERROR!",)
    assert work.trace_files == ("fpu.vcd",)


@pytest.mark.parametrize("outcome", ["detected", "survived", "inconclusive"])
@pytest.mark.parametrize("adapter", ["hdl", "cocotb"])
def test_mutant_launch_grades_from_the_document(
    tmp_path: Path, monkeypatch, capsys, adapter: str, outcome: str
) -> None:
    """A mutant is graded from its adapter's output; the fake decodes the launch document first."""
    from booley.flows.sim.adapter_contract import run_adapter_entry
    from booley.flows.sim.backends import cocotb_results as cr

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
    _write_tests_toml(tmp_path, '[default]\ntests = ["test_a"]\n')

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        target="default",
        count=1,
        min_detected=0,
        simulation={
            "cocotb_module": "tb.test_x" if cocotb else None,
            "run_work": lambda index, _work: answers[
                (adapter, "survived" if index is None else outcome)
            ],
        },
    )

    [row] = result.detail["classified"]
    expected = {"detected": "detected", "survived": "not_detected", "inconclusive": "inconclusive"}
    assert row["status"] == expected[outcome]
    work = fake.launches[-1][0]
    assert work.adapter == ("cocotb" if cocotb else "verilator")
    assert work.tests == (("test_a",) if cocotb else ())
    if adapter == "hdl" and outcome == "inconclusive":
        assert "work document rejected" in row["sim_output_snippet"]


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
    specs = (campaign_dir / "mutation-specs.md").read_text(encoding="utf-8")
    assert "# Mutation Specifications" in specs
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
    return endpoint, endpoint.run_with_claims(CampaignClaims()), fake


def _baseline_launches(
    tmp_path: Path,
    monkeypatch,
    tests_toml: str | None = None,
    *,
    target: str = "default",
    **simulation: Any,
) -> tuple[list[tuple[PreparedSimulationWork, dict[str, str]]], FakeSimulation]:
    """Run a one-Mutant campaign of ``default``; return the baseline's launches and the fake.

    ``tests_toml`` (a ``[default]`` section) is written first; each launch is the work the
    simulator was given and the environment it ran under.
    """
    if tests_toml is not None:
        _write_tests_toml(tmp_path, tests_toml)
    _, _, fake = _campaign(
        tmp_path, monkeypatch, target=target, count=1, min_detected=0, simulation=simulation
    )
    return [(work, env) for work, env in fake.launches if _mutant_index(repr(work)) is None], fake


def _detects_every_mutant(index: int | None) -> subprocess.CompletedProcess:
    """The baseline passes; every Mutant's test fails."""
    return _fake_proc(rc=0 if index is None else 1, stdout="[sim] run")


_MUTATION_RUNS: dict[str, tuple[dict[str, Any], int]] = {
    "pass": ({"run": _detects_every_mutant}, EXIT_SUCCESS),
    "survivors": ({}, EXIT_FAILURE),
    "failing-test": (
        {"run": lambda index: _fake_proc(rc=1 if index is None else 0, stdout="[sim] failed")},
        EXIT_ERROR,
    ),
    "crash": (
        {"build": lambda index: _fake_proc(rc=1 if index is None else 0, stdout="[elab] boom")},
        EXIT_ERROR,
    ),
}


@pytest.mark.parametrize("with_state_file", [True, False], ids=["state-file", "no-state-file"])
@pytest.mark.parametrize("outcome", list(_MUTATION_RUNS))
def test_hand_run_mutation_records_no_criterion(
    outcome: str, with_state_file: bool, tmp_path: Path, monkeypatch
) -> None:
    """A hand run records no Criterion and writes no state file; the exit status is unchanged."""
    fakes, expected_exit = _MUTATION_RUNS[outcome]
    state_path = tmp_path / "state.json"
    _, result, _ = _campaign(tmp_path, monkeypatch, count=2, state_file=with_state_file, **fakes)
    before = b'{"opaque": true}\n'

    assert result.exit_code == expected_exit, result.report_text
    assert list(tmp_path.rglob("acceptance")) == []
    if with_state_file:
        assert state_path.read_bytes() == before
    else:
        assert list(tmp_path.rglob("state.json")) == []


def _results_markdown(tmp_path: Path, result: EndpointOutcome) -> str:
    return (tmp_path / result.detail["artifacts"]["results"]).read_text(encoding="utf-8")


def _mutant_keys(result: EndpointOutcome) -> list[tuple[Any, ...]]:
    return [
        (row["id"], row["category"], row["file"], row["line"], row["status"])
        for row in result.detail["classified"]
    ]


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


def test_a_smaller_count_drops_what_an_earlier_larger_run_left(
    tmp_path: Path, monkeypatch
) -> None:
    _campaign(tmp_path, monkeypatch, count=3, min_detected=0)
    campaign = tmp_path / "logs" / ".runtime" / "mutation" / "campaign"
    logs = campaign / "mutant_logs"
    assert sorted(path.name for path in logs.iterdir()) == [f"mutant_{i}.log" for i in (1, 2, 3)]
    stale = campaign / "builds" / "mutant_9"
    stale.mkdir(parents=True)
    (stale / "image").write_bytes(b"stale")

    _, result, _ = _campaign(tmp_path, monkeypatch, count=1, min_detected=0)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert not stale.exists()
    assert [path.name for path in logs.iterdir()] == ["mutant_1.log"]


def test_same_rtl_count_and_seed_reproduce_mutants_and_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """The seed defaults to 0, so an omitted seed reproduces ``--seed 0`` and not ``--seed 1``."""
    endpoint, first, _ = _campaign(tmp_path, monkeypatch, count=4, min_detected=0)
    _, second, _ = _campaign(tmp_path, monkeypatch, count=4, min_detected=0, seed=0)
    _, other_seed, _ = _campaign(tmp_path, monkeypatch, count=4, min_detected=0, seed=1)

    assert endpoint.args.seed == 0
    assert first.exit_code == second.exit_code == other_seed.exit_code == EXIT_SUCCESS
    assert _mutant_keys(first) == _mutant_keys(second)
    assert len({row[0] for row in _mutant_keys(first)}) == 4  # distinct Mutants
    assert first.detail["identity"] == second.detail["identity"]
    assert first.detail["artifacts"]["results"] != second.detail["artifacts"]["results"]
    assert _mutant_keys(first) != _mutant_keys(other_seed)
    assert [run.detail["identity"]["seed"] for run in (first, other_seed)] == [0, 1]


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
    real_catalog = TargetCatalog.build
    fake = _patch_fake_simulation(monkeypatch)
    monkeypatch.setattr(TargetCatalog, "build", real_catalog)  # the real .core names the RTL
    endpoint = _make_endpoint(
        tmp_path, monkeypatch, target="sim", scope=None, count=None, extra_args=["--dry-run"]
    )

    result = endpoint.run_with_claims(CampaignClaims())

    assert fake.builds == fake.runs == []
    # The testbench, the vendored and generated files, the include header and the
    # non-Verilog inputs are not mutable.
    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert result.detail["scope"] == ["rtl/a.sv", "rtl/b.v"]


@pytest.mark.parametrize("count, expected", [(10, 8), (15, 12), (5, 4)])
def test_default_threshold_is_eighty_percent_of_count(
    tmp_path: Path, monkeypatch, count: int, expected: int
) -> None:
    _prepare_scope_files(tmp_path, ["rtl/mod_a.sv", "rtl/mod_b.sv"])
    endpoint = _make_endpoint(tmp_path, monkeypatch, count=count, extra_args=["--dry-run"])

    result, _ = _run_count_preview(endpoint, monkeypatch)

    assert result.detail["min_detected"] == expected


def test_explicit_min_detected_overrides_the_default(tmp_path: Path, monkeypatch) -> None:
    _prepare_scope_files(tmp_path, ["rtl/mod_a.sv", "rtl/mod_b.sv"])
    omitted = _make_endpoint(tmp_path, monkeypatch, count=10, extra_args=["--dry-run"])
    from_cli = _make_endpoint(
        tmp_path, monkeypatch, count=10, min_detected=3, extra_args=["--dry-run"]
    )

    default_result, _ = _run_count_preview(omitted, monkeypatch)
    cli_result, _ = _run_count_preview(from_cli, monkeypatch)

    assert default_result.detail["min_detected"] == 8  # what the overrides replace
    rebase_line = (
        "min_detected: default 80% of --count; if fewer Mutants are scored than --count, "
        "it follows the number scored"
    )
    assert rebase_line in default_result.display_lines[:-1]  # before the summary line
    assert default_result.display_lines[-1].startswith("mutation dry-run:")
    assert rebase_line not in cli_result.display_lines
    assert cli_result.detail["min_detected"] == 3


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


def _failing_builds(*attempts: int) -> Callable[[int | None], subprocess.CompletedProcess]:
    """A ``build`` callback that fails the listed 1-based Mutant build attempts.

    The baseline build is not counted.  A replacement reuses its Mutant number, so the
    build index alone cannot name an attempt.
    """
    seen = 0

    def build(index: int | None) -> subprocess.CompletedProcess:
        nonlocal seen
        if index is None:
            return _fake_proc(rc=0)
        seen += 1
        return _fake_proc(rc=2 if seen in attempts else 0, stderr="error: does not elaborate")

    return build


def test_replaced_build_failures_leave_a_full_count_that_passes(
    tmp_path: Path, monkeypatch
) -> None:
    _prepare_scope_files(tmp_path, [_SCOPE])
    ranked = _candidates(tmp_path)
    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=10,
        run=_detects_every_mutant,
        build=_failing_builds(2, 5, 8),
    )

    # The 3 candidates that did not build are replaced: all 10 kept Mutants are scored
    # and the default threshold stays ceil(0.8 x 10) = 8.
    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert result.detail["min_detected"] == 8
    assert (result.detail["detected"], result.detail["total_scored"]) == (10, 10)
    assert result.detail["invalid_dropped"] == [ranked[i].id for i in (1, 4, 7)]
    assert "RESULT: PASS (10/10 detected)" in result.report_text
    assert "3 candidate Mutants did not build and were replaced" in result.report_text
    assert "invalid" not in result.detail
    assert "invalid" not in {row["status"] for row in result.detail["classified"]}
    assert "invalid" not in result.report_text.lower()
    assert "invalid" not in _results_markdown(tmp_path, result).lower()
    assert fake.builds == [None, 1, 2, 2, 3, 4, 4, 5, 6, 6, 7, 8, 9, 10]


@pytest.mark.parametrize("eda_tool", ["verilator", "icarus"])
def test_build_failure_is_replaced_by_the_next_candidate(
    tmp_path: Path, monkeypatch, eda_tool: str
) -> None:
    _prepare_scope_files(tmp_path, [_SCOPE])
    ranked = _candidates(tmp_path)
    pristine = (tmp_path / _SCOPE).read_bytes()

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=4,
        run=_detects_every_mutant,
        build=_failing_builds(2),
        simulation={"eda_tool": eda_tool, "verilate": _write_fake_verilation},
    )

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    kept = [ranked[i].id for i in (0, 2, 3, 4)]
    assert [(row["index"], row["id"]) for row in result.detail["classified"]] == list(
        enumerate(kept, start=1)
    )
    assert fake.builds == [None, 1, 2, 2, 3, 4]
    assert fake.runs == [None, 1, 2, 3, 4]  # the Mutant that did not build never ran
    assert result.detail["invalid_dropped"] == [ranked[1].id]
    assert result.detail["equivalent_dropped"] == []
    assert (tmp_path / _SCOPE).read_bytes() == pristine  # a Mutant never stays in the live RTL


@pytest.mark.parametrize("min_detected", [None, 3], ids=["default", "explicit"])
def test_build_failure_cap_scores_the_kept_mutants(
    tmp_path: Path, monkeypatch, min_detected: int | None
) -> None:
    _prepare_scope_files(tmp_path, [_SCOPE])
    ranked = _candidates(tmp_path)
    args: dict[str, Any] = {} if min_detected is None else {"min_detected": min_detected}

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=3,
        run=_detects_every_mutant,
        build=_failing_builds(1, 3, 4),
        **args,
    )

    # Three candidates did not build, which is the cap for count 3: Mutant 1 is all there is.
    assert fake.builds == [None, 1, 1, 2, 2]
    assert result.detail["invalid_dropped"] == [ranked[i].id for i in (0, 2, 3)]
    assert [row["id"] for row in result.detail["classified"]] == [ranked[1].id]
    assert (result.detail["total_scored"], result.detail["total_valid"]) == (1, 1)
    shortfall = "only 1 Mutants could be scored (3 requested)"
    assert result.detail["shortfall"] == shortfall
    assert f"[mutation_test] {shortfall}" in result.report_text
    if min_detected is None:
        assert result.exit_code == EXIT_SUCCESS, result.report_text
        assert result.detail["min_detected"] == 1
    else:
        assert result.exit_code == EXIT_FAILURE, result.report_text
        assert result.detail["min_detected"] == 3
        assert "(required: 3, explicit)" in result.report_text


def _reported_values(result: EndpointOutcome) -> Any:
    """The typed values ``report.json`` gives back for the run's ``detail`` (a JSON round trip)."""
    from booley.measurement.flow_metrics import report_metric_values

    return report_metric_values("mutation", json.loads(json.dumps(result.detail)))


def test_campaign_report_gives_the_typed_mutation_metrics(tmp_path: Path, monkeypatch) -> None:
    """AC1: the score counts valid Mutants only; a candidate that did not build is on neither side."""

    def detects_the_first_three(index: int | None) -> subprocess.CompletedProcess:
        return _fake_proc(rc=0 if index is None or index > 3 else 1, stdout="[sim] run")

    _, result, _ = _campaign(
        tmp_path, monkeypatch, count=4, run=detects_the_first_three, build=_failing_builds(2)
    )

    values = _reported_values(result)
    assert result.exit_code == EXIT_FAILURE, result.report_text  # 3 of 4: below the default 4
    assert set(values) == {"lite"}
    assert values["lite"].plain == {
        "mutation_score": 75.0,
        "mutants_detected": 3,
        "mutants_survived": 1,
    }
    (survivor,) = [row for row in result.detail["classified"] if row["status"] == "not_detected"]
    (mutant,) = [m for m in _candidates(tmp_path) if m.id == survivor["id"]]
    original = " ".join(mutant.original_code.split())
    mutated = " ".join(mutant.mutated_code.split())
    assert values["lite"].counted_items == {
        "mutants_survived": [
            f"{mutant.id} {survivor['category']} {mutant.file}:{mutant.line}: "
            f"{original!r} -> {mutated!r}"
        ]
    }


def test_survivor_line_keeps_both_sides_of_a_long_source_line() -> None:
    """A survived Mutant on a long line is listed with its mutated text, not cut off before it."""
    from booley.flows.mutation.flow import _survivor_line

    short = MutationSpec(1, "operator_change", "rtl/a.sv", 7, "a  +\nb", "a - b", "abc123")
    assert _survivor_line(short) == "abc123 operator_change rtl/a.sv:7: 'a + b' -> 'a - b'"

    original = "x = " + " + ".join(["term"] * 80)
    long = MutationSpec(2, "operator_change", "rtl/a.sv", 9, original, original + " + 1", "def456")
    line = _survivor_line(long)
    assert len(line) <= 200
    assert line.startswith("def456 operator_change rtl/a.sv:9: 'x = term + term")
    assert "...' -> 'x = term + term" in line
    assert line.endswith("...'")


def test_run_with_no_buildable_candidate_has_nothing_to_score(tmp_path: Path, monkeypatch) -> None:
    _prepare_scope_files(tmp_path, [_SCOPE])
    ranked = _candidates(tmp_path)

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=3,
        run=_detects_every_mutant,
        build=lambda index: _fake_proc(rc=0 if index is None else 2, stderr="error: no"),
    )

    assert result.exit_code == EXIT_ERROR, result.report_text
    assert result.detail["phase"] == "mutant_selection"
    assert result.report_text.splitlines()[-1] == (
        "RESULT: ERROR (selection: no candidate Mutant changes what the Target builds)"
    )
    assert result.detail["invalid_dropped"] == [ranked[i].id for i in range(3)]
    assert "3 candidate Mutants did not build and were replaced" in result.report_text
    assert fake.runs == [None]
    assert _reported_values(result) == {"lite": MetricsUnavailable("no valid Mutant was scored")}


def test_inconclusive_mutant_after_a_replaced_build_failure_is_an_infrastructure_error(
    tmp_path: Path, monkeypatch
) -> None:
    _prepare_scope_files(tmp_path, [_SCOPE])
    ranked = _candidates(tmp_path)

    def run(index: int | None) -> subprocess.CompletedProcess:
        if index == 1:
            return _fake_proc(rc=1, stdout=_INFRA_OUT)
        return _fake_proc(rc=0 if index is None else 1, stdout="[sim] run")

    _, result, _ = _campaign(
        tmp_path, monkeypatch, count=2, min_detected=0, run=run, build=_failing_builds(1)
    )

    assert result.exit_code == EXIT_ERROR, result.report_text
    assert result.detail["phase"] == "variant_infrastructure"
    assert result.detail["invalid_dropped"] == [ranked[0].id]
    assert [(row["id"], row["status"]) for row in result.detail["classified"]] == [
        (ranked[1].id, "inconclusive"),
        (ranked[2].id, "detected"),
    ]
    assert "invalid" not in result.detail
    assert "1 candidate Mutant did not build and was replaced" in result.report_text


def test_generator_shortfall_lowers_the_default_threshold(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / _SCOPE).write_text("module m;\n  assign y = a + b;\nendmodule\n", encoding="utf-8")

    progress: list[str] = []
    monkeypatch.setattr(MutationFlow, "emit_progress", lambda _self, line: progress.append(line))

    _, result, _ = _campaign(tmp_path, monkeypatch, count=10, run=_detects_every_mutant)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert [line for line in progress if line.startswith("build and test Mutant")] == [
        "build and test Mutant 1/2",
        "build and test Mutant 2/2",
    ]
    assert (
        "  detected: 2/2 (required: 2, default 80% of 2 scored, 10 requested)"
        in result.report_text
    )
    expected = "only 2 Mutants could be scored (10 requested)"
    assert result.detail["shortfall"] == expected
    assert expected in result.report_text
    assert (result.detail["min_detected"], result.detail["total_valid"]) == (2, 2)
    assert (result.detail["total_requested"], result.detail["total_scored"]) == (10, 2)
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
        # No Mutation Operator site means no valid Mutant; a parse error has no values at all.
        expected = {"generation": {"lite": MetricsUnavailable("no valid Mutant was scored")}}
        if phase in ("parse", "generation"):
            assert _reported_values(result) == expected.get(phase, {}), phase


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


def _clock_with_baseline(monkeypatch, baseline_seconds: float) -> None:
    """Make the baseline test last *baseline_seconds*; later readings only move forward."""
    # Private seam: the module's own clock name is the narrow patch point for a duration
    # the public boundary cannot set; patching ``time.monotonic`` would reach every caller.
    readings = [0.0, baseline_seconds]  # the baseline test starts and ends
    later = itertools.count(1000)

    def clock() -> float:
        return readings.pop(0) if readings else float(next(later))

    monkeypatch.setattr("booley.flows.mutation.flow._monotonic", clock, raising=False)


@pytest.mark.parametrize(
    ("baseline_seconds", "limit", "hard_kill"),
    [(1.2, 30, 35), (12.5, 125, 130), (40, 300, 305)],
    ids=str,
)
def test_mutant_timeout_scales_with_the_tests_baseline_duration(
    tmp_path: Path, monkeypatch, baseline_seconds: float, limit: int, hard_kill: int
) -> None:
    _clock_with_baseline(monkeypatch, baseline_seconds)

    _, result, fake = _campaign(tmp_path, monkeypatch, count=1, min_detected=0)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    # The baseline keeps the 300 s cap; the Mutant's limit is max(10 x baseline, 30 s), capped.
    assert fake.run_limits == [300, limit]
    assert fake.run_timeouts == [305, hard_kill]  # the hard kill waits 5 s past the limit
    manifest = json.loads(
        (tmp_path / result.detail["artifacts"]["manifest"]).read_text(encoding="utf-8")
    )
    name = result.detail["tests"][0]
    assert manifest["baseline"]["test_runs"] == [
        {"run": name, "duration_s": baseline_seconds, "time_limit_s": limit}
    ]
    markdown = _results_markdown(tmp_path, result)
    assert "## Time limits" in markdown
    assert "| Test run | Baseline | Limit |" in markdown
    assert f"| `{name}` | {baseline_seconds:.2f} s | {limit} s |" in markdown


def test_timed_out_mutant_counts_as_detected_timeout(tmp_path: Path, monkeypatch) -> None:
    def timeouts(index: int | None) -> subprocess.CompletedProcess:
        if index == 1:  # the run was killed
            raise subprocess.TimeoutExpired(cmd="run", timeout=30)
        if index == 2:  # the adapter cut the run off first
            return _fake_proc(rc=1, stdout="ERROR: Verilator simulation timed out (30s)\n")
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


def test_mutation_run_records_its_setups_builds_and_simulations(
    tmp_path: Path, monkeypatch
) -> None:
    """Every setup, build and simulation of the baseline and a Mutant is in the history."""
    _write_tests_toml(tmp_path, _MUTATION_VARIANTS)

    def run(index: int | None) -> subprocess.CompletedProcess:
        if index is None:
            return _fake_proc(rc=0, stdout="[sim] ok")
        return _fake_proc(rc=1, stdout="ERROR: Verilator simulation timed out (30s)\n")

    _campaign(
        tmp_path,
        monkeypatch,
        target="default",
        count=1,
        min_detected=0,
        run=run,
        simulation={"measured": True},
    )

    records = resource_history.load_records(tmp_path)
    assert {record["flow"] for record in records} == {"mutation"}
    assert {record["key"]["target"] for record in records} == {"default"}
    assert [record["key"]["flags"] for record in records] == [[]] * len(records)
    assert [(r["key"]["phase"], r["key"]["test_run"], r["outcome"]) for r in records] == [
        ("sim_prepare", "", "exit_zero"),
        ("sim_build", "", "exit_zero"),
        ("sim_run", "reset", "exit_zero"),
        ("sim_run", "stress", "exit_zero"),
        ("sim_prepare", "", "exit_zero"),
        ("sim_build", "", "exit_zero"),
        ("sim_run", "reset", "timeout"),
        ("sim_run", "stress", "timeout"),
    ]
    shown = [
        (s["key"]["phase"], s["key"]["test_run"]) for s in resource_history.summarize(records)
    ]
    assert sorted(shown) == [
        ("sim_build", ""),
        ("sim_prepare", ""),
        ("sim_run", "reset"),
        ("sim_run", "stress"),
    ]


def _result_per_test(
    index: int | None, work: PreparedSimulationWork
) -> subprocess.CompletedProcess:
    """Answer a launch by its ``test=<name>`` plusarg: ``fail`` fails, ``slow`` times out."""
    name = next(arg.removeprefix("test=") for arg in work.plusargs if arg.startswith("test="))
    if name == "slow":
        raise subprocess.TimeoutExpired(cmd=[], timeout=1)
    if name == "broken":
        raise OSError("launch failed\nsecond line")
    return _fake_proc(
        rc=1 if name == "fail" else 0, stdout="[sim] failed" if name == "fail" else ""
    )


def _cocotb_results_output() -> str:
    from booley.flows.sim.backends import cocotb_results as cr

    results = cr.CocotbResults(
        state=cr.STATE_OK,
        tests=(
            cr.CocotbTest("test_a", "tb", cr.VERDICT_FAIL),
            cr.CocotbTest("test_b", "tb", cr.VERDICT_PASS),
        ),
    )
    return cr.format_results_line(cr.results_payload(results))


@pytest.mark.parametrize(
    ("tests", "cocotb_output", "last_line"),
    [
        (
            ("ok", "fail", "slow"),
            None,
            "RESULT: ERROR (baseline: test 'fail' failed on pristine source; "
            "test 'slow' inconclusive: simulation timed out)",
        ),
        (("ok", "fail"), None, "RESULT: ERROR (baseline: test 'fail' failed on pristine source)"),
        (
            ("broken", "fail"),
            None,
            "RESULT: ERROR (baseline: test 'broken' inconclusive: launch failed second line; "
            "test 'fail' failed on pristine source)",
        ),
        (
            ("test_a", "test_b"),
            "fail",
            "RESULT: ERROR (baseline: test 'test_a' failed on pristine source)",
        ),
        (
            ("test_a", "test_b"),
            "missing",
            "RESULT: ERROR (baseline: test '<cocotb-suite>' inconclusive: "
            "cocotb result line is missing or malformed)",
        ),
    ],
    ids=[
        "hdl-fail-and-timeout",
        "hdl-fail",
        "hdl-multiline-error",
        "cocotb-fail",
        "cocotb-missing",
    ],
)
def test_baseline_failure_result_names_each_failing_test(
    tmp_path: Path, monkeypatch, tests: tuple[str, ...], cocotb_output: str | None, last_line: str
) -> None:
    """The ``RESULT:`` line names every failing or inconclusive baseline test, on one line."""
    names = ", ".join(f'"{name}"' for name in tests)
    _write_tests_toml(tmp_path, f'[lite]\ntests = [{names}]\nselect = "+test={{name}}"\n')
    simulation: dict[str, Any] = {"run_work": _result_per_test}
    if cocotb_output is not None:
        output = "no results line" if cocotb_output == "missing" else _cocotb_results_output()
        simulation = {
            "cocotb_module": "tb.test_x",
            "run_work": lambda _index, _work: _fake_proc(
                rc=0 if cocotb_output == "missing" else 1, stdout=output
            ),
        }

    _, result, _ = _campaign(tmp_path, monkeypatch, count=1, simulation=simulation)

    assert result.exit_code == EXIT_ERROR
    assert result.report_text.splitlines()[-1] == last_line


def test_error_result_line_says_no_detail_for_a_blank_reason(tmp_path: Path, monkeypatch) -> None:
    """A Mutant generator error with a blank message still ends with a one-line ``RESULT:``."""
    from booley.flows.mutation.flow import MutantGeneratorError

    def blank_error(*_args: Any, **_kwargs: Any) -> None:
        raise MutantGeneratorError("  \n")

    monkeypatch.setattr("booley.flows.mutation.flow.generate_mutants", blank_error)

    _, result, _ = _campaign(tmp_path, monkeypatch, count=1)

    assert result.report_text.splitlines()[-1] == "RESULT: ERROR (generation: no detail)"


def test_baseline_that_times_out_is_inconclusive(tmp_path: Path, monkeypatch) -> None:
    def baseline_times_out(index: int | None) -> subprocess.CompletedProcess:
        return _fake_proc(rc=1, stdout="ERROR: Verilator simulation timed out (300s)\n")

    _, result, fake = _campaign(tmp_path, monkeypatch, count=1, run=baseline_times_out)

    assert result.exit_code == EXIT_ERROR
    assert "mutation baseline was inconclusive" in result.report_text
    assert "simulation timed out" in result.report_text
    assert fake.runs == [None]  # no Mutant ran


def test_identity_is_in_results_and_manifest(tmp_path: Path, monkeypatch) -> None:
    from booley.flows.mutation.generator import GENERATOR_VERSION

    progress: list[str] = []
    monkeypatch.setattr(MutationFlow, "emit_progress", lambda _self, line: progress.append(line))

    _, result, _ = _campaign(tmp_path, monkeypatch, count=2, min_detected=0, seed=5)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    digest = "sha256:" + hashlib.sha256((tmp_path / _SCOPE).read_bytes()).hexdigest()
    expected = {
        "scope_hashes": {_SCOPE: digest},
        "generator_version": GENERATOR_VERSION,
        "pyslang_version": importlib.metadata.version("pyslang"),
        "seed": 5,
        "count": 2,
        "verilator_version": _VERILATOR_VERSION,  # a Verilator Target records its version
        "elaboration": "per-file",  # the scope file alone does not elaborate: undeclared names
    }
    manifest = json.loads((tmp_path / result.detail["artifacts"]["manifest"]).read_text("utf-8"))
    identity = dict(result.detail["identity"])
    reason = identity.pop("elaboration_fallback")
    assert reason.startswith(f"{_SCOPE}:")
    assert f"Mutant generation: per-file elaboration ({reason})" in progress
    assert identity == expected
    assert manifest["identity"] == result.detail["identity"]
    markdown = _results_markdown(tmp_path, result)
    for line in (
        "- Seed: 5",
        f"- Generator version: {GENERATOR_VERSION}",
        f"- pyslang version: {expected['pyslang_version']}",
        f"- Verilator version: {_VERILATOR_VERSION}",
        f"`{_SCOPE}`: {digest}",
        f"- Elaboration: per file ({reason})",
    ):
        assert line in markdown
    # An explicit count is shown as the bare number and adds no auto fields.
    assert "- Count: 2\n" in markdown
    assert not {"auto", "formula_count", "budget_capped"} & manifest.keys()
    assert (
        f"seed 5, count 2, generator {GENERATOR_VERSION}, "
        f"pyslang {expected['pyslang_version']}, {_VERILATOR_VERSION})"
    ) in result.report_text
    assert (
        f"[mutation_test] Target elaboration failed, Mutants generated per file: {reason}"
    ) in result.report_text.splitlines()[1]


def test_generation_without_an_inspectable_target_falls_back(tmp_path: Path, monkeypatch) -> None:
    # Private seam: validation inspects the Target first and refuses when that fails, so a
    # run reaches generation's fallback only for a Target that turns uninspectable between
    # the two reads; no public path makes that happen.
    _prepare_scope_files(tmp_path, [_SCOPE])
    endpoint = _make_endpoint(tmp_path, monkeypatch, scope=_SCOPE, count=2)
    monkeypatch.setattr(MutationFlow, "_inspect_target", lambda _self: None)

    generated = endpoint._generate(_run_plan(tmp_path))

    assert generated.identity["elaboration_fallback"] == "the Target cannot be inspected"


_AUTO_COUNT_LABEL = "auto -> 3 (formula 6, budget cap 3)"


def test_auto_count_names_formula_and_cap_and_keeps_the_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """32 source lines give formula 6; a 450 s timeout caps the count at 3."""
    _, auto, _ = _campaign(
        tmp_path,
        monkeypatch,
        count="auto",
        min_detected=0,
        extra_args=["--timeout-ms", "450000"],
    )
    _, explicit, _ = _campaign(tmp_path, monkeypatch, count=3, min_detected=0)

    assert auto.exit_code == explicit.exit_code == EXIT_SUCCESS, auto.report_text
    assert f"count {_AUTO_COUNT_LABEL}, generator" in auto.report_text.splitlines()[0]
    assert f"- Count: {_AUTO_COUNT_LABEL}\n" in _results_markdown(tmp_path, auto)
    auto_manifest = json.loads(
        (tmp_path / auto.detail["artifacts"]["manifest"]).read_text("utf-8")
    )
    explicit_manifest = json.loads(
        (tmp_path / explicit.detail["artifacts"]["manifest"]).read_text("utf-8")
    )
    fields = ("auto", "formula_count", "budget_capped")
    assert (auto_manifest["auto"], auto_manifest["formula_count"]) == (True, 6)
    assert auto_manifest["budget_capped"] is True
    # The manifest mirrors the report detail and keeps the fields out of the identity.
    assert {key: auto_manifest[key] for key in fields} == {key: auto.detail[key] for key in fields}
    assert not set(fields) & auto_manifest["identity"].keys()
    assert auto_manifest["identity"] == explicit_manifest["identity"]
    assert not set(fields) & explicit_manifest.keys()


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
    campaign = (tmp_path / result.detail["artifacts"]["manifest"]).parent
    assert "# Mutation Specifications" in (campaign / "mutation-specs.md").read_text("utf-8")
    assert "mutation variant: reports/mutation/1/campaign/variants/" in result.report_text


def test_auto_count_publishes_source_size_budget(tmp_path: Path, monkeypatch) -> None:
    _, result, _ = _campaign(tmp_path, monkeypatch, count="auto", min_detected=0)

    budget = result.detail["source_size_budget"]
    assert budget["method"] == "language_neutral_source_size"
    # The default timeout caps at 12, above the formula count of 6: the cap did not bind.
    assert (result.detail["formula_count"], result.detail["budget_capped"]) == (6, False)
    assert f"count auto -> 6 (formula 6, budget cap {budget['budget_cap']}), generator" in (
        result.report_text
    )
    manifest_path = tmp_path / result.detail["artifacts"]["manifest"]
    assert (manifest_path.parent / "source-size-budget.json").is_file()


@pytest.mark.parametrize("count", [2, "auto"])
def test_a_run_without_a_verdict_publishes_a_complete_manifest(
    tmp_path: Path, monkeypatch, count: int | str
) -> None:
    """The fixed count adds no auto fields; the auto count keeps them in detail and manifest."""

    def first_mutant_has_no_observation(index: int | None) -> subprocess.CompletedProcess:
        if index == 1:
            return _fake_proc(rc=1, stdout="[SIM_INFRA_ERROR] the harness broke\n")
        return _fake_proc(rc=0 if index is None else 1, stdout="[sim] run")

    _, result, _ = _campaign(
        tmp_path, monkeypatch, count=count, min_detected=0, run=first_mutant_has_no_observation
    )

    assert result.exit_code == EXIT_ERROR
    assert result.report_text.startswith("mutation campaign produced no trustworthy verdict")
    assert result.detail["phase"] == "variant_infrastructure"
    assert result.report_text.splitlines()[-1].startswith("RESULT: ERROR (sweep: ")
    manifest = json.loads((tmp_path / result.detail["artifacts"]["manifest"]).read_text("utf-8"))
    assert [row["status"] for row in manifest["mutants"]][:2] == ["inconclusive", "detected"]
    auto_fields = ("auto", "formula_count", "budget_capped")
    if count == "auto":
        for fields in (result.detail, manifest):
            assert [fields[key] for key in auto_fields] == [True, 6, False]
    else:
        assert len(result.detail["classified"]) == manifest["identity"]["count"] == 2
        assert not set(auto_fields) & manifest.keys()


_RESIDUE_WARNING = "WARNING: worktree not clean after sim sweep"


@pytest.mark.parametrize(
    "during_run", [None, "append", "delete"], ids=["edit-before-run", "append", "delete"]
)
def test_residue_warning_compares_against_the_pre_run_snapshot(
    tmp_path: Path, monkeypatch, during_run: str | None
) -> None:
    """A scope edit that predates the run is no residue; one the run leaves names its file (#2777)."""
    _prepare_scope_files(tmp_path, [_SCOPE])
    with (tmp_path / _SCOPE).open("a", encoding="utf-8") as handle:
        handle.write("// uncommitted edit made before the run\n")

    def run(index: int | None) -> subprocess.CompletedProcess:
        if index == 2 and during_run == "append":
            with (tmp_path / _SCOPE).open("a", encoding="utf-8") as handle:
                handle.write("// edit left by the run\n")
        if index == 2 and during_run == "delete":
            (tmp_path / _SCOPE).unlink()
        return _fake_proc(rc=0)

    _, result, _ = _campaign(tmp_path, monkeypatch, count=2, min_detected=0, run=run)

    lines = result.report_text.splitlines()
    warnings = [line.strip() for line in lines if _RESIDUE_WARNING in line]
    if during_run is None:
        assert result.exit_code == EXIT_SUCCESS, result.report_text
        assert warnings == []
        return
    assert warnings == [f"{_RESIDUE_WARNING}: {_SCOPE}"]
    assert next(i for i, line in enumerate(lines) if _RESIDUE_WARNING in line) < next(
        i for i, line in enumerate(lines) if line.startswith("RESULT: ")
    )


def test_error_run_warns_before_its_final_result_line(tmp_path: Path, monkeypatch) -> None:
    """A log tail may carry its own ``RESULT:`` line; the warning goes before the last."""
    _prepare_scope_files(tmp_path, [_SCOPE])

    def baseline_fails(index: int | None) -> subprocess.CompletedProcess:
        with (tmp_path / _SCOPE).open("a", encoding="utf-8") as handle:
            handle.write("// edit left by the run\n")
        return _fake_proc(rc=1, stdout="RESULT: FAIL (0/1)")

    _, result, _ = _campaign(tmp_path, monkeypatch, count=2, run=baseline_fails)

    assert result.exit_code == EXIT_ERROR
    lines = result.report_text.splitlines()
    warning = next(i for i, line in enumerate(lines) if _RESIDUE_WARNING in line)
    assert "RESULT: FAIL (0/1)" in lines[:warning]  # the log tail's own line stays above
    assert lines[warning + 1].startswith("RESULT: ERROR (baseline: ")
    assert warning + 2 == len(lines)


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


def _dry_run_scope(
    root: Path, monkeypatch: pytest.MonkeyPatch, scope: str | None
) -> EndpointOutcome:
    """Dry-run the authored ``sim`` Target of *root* over *scope* (``None``: its own RTL)."""
    _write_tests_toml(root, '[sim]\ntests = ["smoke"]\n')
    endpoint = _make_endpoint(
        root, monkeypatch, target="sim", scope=scope, count=1, extra_args=["--dry-run"]
    )
    return endpoint.run_with_claims(CampaignClaims())


_MIRROR = ".booley/cores/rtl/counter.sv"


@pytest.mark.parametrize(
    ("scope", "exists", "refusal"),
    [
        ("rtl/counter.sv", True, None),
        (_MIRROR, True, "does not match"),
        (_MIRROR, False, "does not exist"),
    ],
    ids=["resolved-source", "stealth-mirror", "missing-mirror"],
)
def test_scope_is_validated_against_the_targets_resolved_sources(
    tmp_path: Path, monkeypatch, scope: str, exists: bool, refusal: str | None
) -> None:
    """The stealth-cores mirror is no resolved source; its basename hints the real one (#3946)."""
    _author_scope_core(tmp_path)
    if exists:
        (tmp_path / scope).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / scope).write_text("// stub\n", encoding="utf-8")

    result = _dry_run_scope(tmp_path, monkeypatch, scope)

    if refusal is None:
        assert result.exit_code == EXIT_SUCCESS, result.report_text
        return
    assert result.exit_code == EXIT_ERROR
    assert refusal in result.report_text
    assert "Did you mean 'rtl/counter.sv'?" in result.report_text


def test_refuses_when_target_unresolvable(tmp_path: Path):
    # Private seam: campaign resolution refuses an unknown Target ("Unknown target 'sim'")
    # before any scope check, so only a direct call reaches the inspection-failure refusal.
    endpoint = _make_endpoint(tmp_path, target="sim")
    result = endpoint._validate_scope_against_target(["anything.sv"])
    assert result is not None
    assert "cannot inspect Target" in result.report_text


# ---------------------------------------------------------------------------
# SETUP-F-40 — Cocotb Targets run through the cocotb run-half
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("eda_tool", (None, "mystery-sim"))
def test_missing_or_unknown_simulator_never_defaults_to_verilator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eda_tool: str | None
) -> None:
    _patch_resolve_target(monkeypatch, eda_tool=eda_tool)
    endpoint = _make_endpoint(tmp_path, monkeypatch)

    with pytest.raises(UnsupportedSimTargetError, match="unsupported simulator metadata"):
        endpoint.target_eda_tool("default", tmp_path)


def test_invalid_cocotb_target_selection_is_not_reclassified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InvalidCatalog:
        def select(self, target: str, *, for_flow: str):
            raise IncompatibleTargetError(f"{target} is not {for_flow}-compatible")

    monkeypatch.setattr(TargetCatalog, "build", classmethod(lambda cls, root: InvalidCatalog()))
    endpoint = _make_endpoint(tmp_path, monkeypatch)

    with pytest.raises(UnsupportedSimTargetError, match="cannot select Simulation Target"):
        endpoint.cocotb_target("default", tmp_path)


def test_cached_simulator_marker_must_match_target_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_resolve_target(monkeypatch, eda_tool="icarus")
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / ".booley_edalize_eda_tool").write_text("verilator", encoding="utf-8")
    endpoint = _make_endpoint(tmp_path, monkeypatch)

    with pytest.raises(UnsupportedSimTargetError, match="cached build metadata"):
        endpoint.target_eda_tool("default", tmp_path, build_dir)


def test_cocotb_target_runs_the_cocotb_run_half_over_the_whole_suite(
    tmp_path: Path, monkeypatch
) -> None:
    """A Cocotb Target runs Cocotb's module and test filter, one batch for the whole suite."""
    launches, _ = _baseline_launches(
        tmp_path,
        monkeypatch,
        '[default]\ntests = ["test_a", "test_b"]\n',
        cocotb_module="tb.test_ravenoc",
    )

    [(work, _env)] = launches
    assert work.adapter == "cocotb"
    assert work.cocotb_module == "tb.test_ravenoc"
    assert work.tests == ("test_a", "test_b")
    assert work.eda_tool == "verilator"
    assert work.build_dir.endswith("builds/baseline")
    assert work.cocotb_seed == 1  # the Target declares no seed: the Flow default
    # Sentinels and a top do not apply to Cocotb Targets (ADR 0034 decision 6).
    assert not work.pass_sentinels and not work.fail_sentinels
    assert work.top == ""


def test_cocotb_suite_whose_default_variants_differ_in_env_runs_in_batches(
    tmp_path: Path, monkeypatch
):
    """Regression: such a suite raised UnsupportedSimTargetError before any run.

    The Simulation Flow runs it as one process per environment; a mutant
    is graded the same way, each batch under its own Test Variant's
    environment and the Default Seed.
    """
    monkeypatch.delenv("MODE", raising=False)
    launches, _ = _baseline_launches(
        tmp_path,
        monkeypatch,
        '[default]\ntests = ["test_a", "test_b", "test_c"]\ndefault_seed = 9\n'
        '[default.variants.test_a]\ndefault = "x"\nx = { env = { MODE = "A" } }\n'
        '[default.variants.test_b]\ndefault = "y"\ny = { env = { MODE = "B" } }\n',
        cocotb_module="tb.test_noc",
    )

    assert [(work.tests, env.get("MODE")) for work, env in launches] == [
        (("test_a",), "A"),
        (("test_b",), "B"),
        (("test_c",), None),
    ]
    assert [work.cocotb_seed for work, _env in launches] == [9, 9, 9]


@pytest.mark.parametrize(
    ("eda_tool", "adapter", "plusargs"),
    [("verilator", "verilator", ("verilator+seed+1",)), ("icarus", "icarus", ())],
)
def test_classic_target_runs_its_simulators_adapter(
    tmp_path: Path, monkeypatch, eda_tool: str, adapter: str, plusargs: tuple[str, ...]
) -> None:
    """F-6: a classic Icarus build must be executed as a vvp image."""
    launches, _ = _baseline_launches(tmp_path, monkeypatch, eda_tool=eda_tool)

    [(work, _env)] = launches
    assert work.adapter == adapter
    assert work.plusargs == plusargs
    assert work.cocotb_module == "" and work.tests == ()
    assert not work.trace


def test_commercial_cocotb_target_fails_fast_with_reason(tmp_path: Path, monkeypatch):
    """Unsupported => a typed error naming the reason, never a silent run."""
    _patch_resolve_target(monkeypatch, eda_tool="xcelium", cocotb_module="tb.test_noc")
    endpoint = _make_endpoint(tmp_path, monkeypatch, target="default")

    with pytest.raises(UnsupportedSimTargetError) as exc:
        endpoint.cocotb_target("default", tmp_path)
    assert "icarus and verilator only" in str(exc.value)


def test_unsupported_target_aborts_before_any_build(tmp_path: Path, monkeypatch):
    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        target="default",
        count=1,
        simulation={"eda_tool": "vcs", "cocotb_module": "tb.test_noc"},
    )

    assert result.exit_code == EXIT_ERROR
    assert "vcs" in result.report_text
    assert fake.builds == []


# ---------------------------------------------------------------------------
# SETUP-F-41 — infra failures are not the design's fault, and never a kill
# ---------------------------------------------------------------------------

_INFRA_OUT = (
    "ERROR: Verilator executable Vtb not found in build\n"
    "[SIM_INFRA_ERROR] Verilator executable Vtb not found in build\n"
)


def test_pristine_baseline_reports_missing_literal_run_cwd_before_elaboration(
    tmp_path: Path, monkeypatch
) -> None:
    """Issue #881: the baseline must not build before it knows run_cwd is usable."""
    state = tmp_path / ".booley"
    state.mkdir()
    (state / "booley.toml").write_text('[flows.sim]\nrun_cwd = "run"\n', encoding="utf-8")

    _, result, fake = _campaign(tmp_path, monkeypatch, count=1)

    assert result.exit_code == EXIT_ERROR
    assert "mutation baseline cannot run: literal run directory must already exist" in (
        result.report_text
    )
    assert ".gitkeep" in result.report_text
    assert result.detail["phase"] == "baseline_run_cwd"
    assert result.report_text.splitlines()[-1].startswith(
        "RESULT: ERROR (baseline: literal run directory must already exist"
    )
    assert fake.builds == []


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
    assert _dry_run_scope(tmp_path, monkeypatch, "rtl/counter.sv").exit_code == EXIT_SUCCESS
    testbench = _dry_run_scope(tmp_path, monkeypatch, "tb/tb_counter.sv")
    assert "is a testbench file" in testbench.report_text
    (tmp_path / "rtl/other.sv").write_text("// stub\n", encoding="utf-8")
    rejected = _dry_run_scope(tmp_path, monkeypatch, "rtl/other.sv")
    assert "does not match" in rejected.report_text
    assert expected in rejected.report_text


def test_scope_rejects_rtl_symlink_outside_project(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _author_scope_core(root)
    external = tmp_path / "external.sv"
    external.write_text("module counter; endmodule\n")
    source = root / "rtl/counter.sv"
    source.unlink()
    symlink_or_skip(source, external)

    rejected = _dry_run_scope(root, monkeypatch, str(external))

    assert rejected.exit_code == EXIT_ERROR
    assert "outside the Project" in rejected.report_text


def test_scope_refuses_when_identity_resolution_fails(tmp_path: Path) -> None:
    # Private seam: the failure is injected after the scope was canonicalized by the code that
    # runs before the check (``_scope_files`` does not catch ``OSError``), so only a direct
    # call reaches the refusal.
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


def test_cpp_scope_refuses_before_generation_and_build(tmp_path, monkeypatch):
    """A cppSource scope, harness fileset or not, is no HDL RTL source: nothing is generated."""
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
    generate = MagicMock()
    build_or_run = MagicMock()
    monkeypatch.setattr("booley.flows.mutation.flow.generate_mutants", generate)
    monkeypatch.setattr("booley.flows.mutation.flow.run_tool", build_or_run)

    result = _dry_run_scope(tmp_path, monkeypatch, "tb/sim_main.cpp")

    assert result.exit_code == EXIT_ERROR
    assert "not an HDL RTL mutation source" in result.report_text
    generate.assert_not_called()
    build_or_run.assert_not_called()


def test_default_scope_keeps_rtl_that_shares_a_directory_with_testbench_files(
    tmp_path, monkeypatch
):
    from tests.test_target_catalog import _category_sim_core

    _category_sim_core(tmp_path)

    result = _dry_run_scope(tmp_path, monkeypatch, None)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert result.detail["scope"] == ["shared/dut.sv"]


@pytest.mark.parametrize("cocotb", [False, True])
def test_mutation_report_records_resolved_simulator(tmp_path, monkeypatch, cocotb):
    """A report names the simulator the build resolved, even when no simulation ran (#1605)."""
    endpoint, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        target="default",
        count=1,
        build=lambda _index: _fake_proc(rc=1, stdout="[elab] boom"),
        simulation={"cocotb_module": "tb_test" if cocotb else None},
    )

    assert result.exit_code == EXIT_ERROR
    assert fake.runs == []
    path = endpoint.write_report(result)

    assert json.loads(path.read_bytes()).get("eda_tool") == "verilator"


@pytest.mark.parametrize("cocotb", [False, True])
def test_mutation_report_records_the_simulator_of_a_cached_build(tmp_path, monkeypatch, cocotb):
    # Private seam: the run half records the simulator from the build's cached marker, and no
    # public path runs a simulation without a build of the same process first (every campaign
    # builds the baseline and each Mutant before running it).
    tool = "verilator"
    _patch_resolve_target(monkeypatch, eda_tool=tool, cocotb_module="tb_test" if cocotb else None)
    endpoint = _make_endpoint(tmp_path, monkeypatch, target="default")
    build = tmp_path / "build"
    build.mkdir()
    (build / ".booley_edalize_eda_tool").write_text(tool)
    monkeypatch.setattr(
        endpoint, "_pinned_build", lambda *_args: SimpleNamespace(rel=".", runtime_inputs=())
    )
    monkeypatch.setattr(
        endpoint,
        "_pinned_sim_command",
        lambda *_args, **_kwargs: ([tool], SimpleNamespace(environment={}), None),
    )
    monkeypatch.setattr("booley.flows.mutation.flow._target_environment", lambda *_args: {})
    monkeypatch.setattr("booley.flows.mutation.flow.launch_environment", lambda *_args: {})
    monkeypatch.setattr(
        "booley.flows.mutation.flow.run_tool",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )

    endpoint._run_sim_pinned(
        "default", tmp_path, build, "tb", label=ResourceLabel("default", "sim_run")
    )

    path = endpoint.write_report(EndpointOutcome(report_text="mutation result"))
    assert json.loads(path.read_bytes()).get("eda_tool") == tool


def test_mutation_preflight_alone_does_not_report_eda_tool(tmp_path, monkeypatch):
    _prepare_scope_files(tmp_path, ["rtl/mod_a.sv", "rtl/mod_b.sv"])
    endpoint = _make_endpoint(tmp_path, monkeypatch, target="default", extra_args=["--dry-run"])

    result, _ = _run_count_preview(endpoint, monkeypatch)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    report = json.loads(endpoint.write_report(result).read_bytes())
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


@pytest.mark.parametrize(
    ("endpoint_args", "message", "phase"),
    [
        ({"count": 3, "seed": -1}, "--seed must be a non-negative integer", "options"),
        (
            {"count": 3, "min_detected": 4},
            "--min-detected must be between 0 and --count",
            "options",
        ),
        (
            {"count": 3, "min_detected": -1},
            "--min-detected must be between 0 and --count",
            "options",
        ),
        ({"count": 3, "tb_top": ""}, "selected Target does not declare a toplevel", "target"),
    ],
    ids=["negative-seed", "min-detected-above-count", "negative-min-detected", "no-toplevel"],
)
def test_validation_errors_end_with_a_result_error_line(
    tmp_path: Path, monkeypatch, endpoint_args: dict[str, Any], message: str, phase: str
) -> None:
    """AC3: each validation refusal reports its phase on a final ``RESULT:`` line (#3979, #3962)."""
    _prepare_scope_files(tmp_path, ["rtl/mod_a.sv", "rtl/mod_b.sv"])
    endpoint = _make_endpoint(tmp_path, monkeypatch, extra_args=["--dry-run"], **endpoint_args)

    result, _ = _run_count_preview(endpoint, monkeypatch)

    assert result.exit_code == EXIT_ERROR
    assert result.report_text.endswith(f"\n\nRESULT: ERROR ({phase}: {message})")
    assert message in result.report_text.split("\n\n")[0]


def test_validation_refusals_without_a_public_path(tmp_path: Path, monkeypatch) -> None:
    # Private seam: argparse refuses a non-positive ``--count`` before the run
    # (``test_count_rejects_text_that_is_not_auto_or_an_integer``), and ``--target`` is required
    # by the parser. With ``--scope``, campaign resolution refuses an undeclared ``a,b`` as an
    # unknown Target first, so only a comma-named Target reaches the one-Target refusal; a
    # direct call reaches these three.
    message = "--count must be positive"
    outcome = MutationFlow._validate_count(0, 0, 0)
    assert outcome is not None
    assert outcome.report_text == f"{message}\n\nRESULT: ERROR (options: {message})"

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
    assert (result.detail["total_requested"], result.detail["total_scored"]) == (4, 4)
    assert len(result.detail["classified"]) == 4
    assert "shortfall" not in result.detail
    # The dropped Mutant was built (its C++ was compared) but never simulated, and the
    # next candidate took its number and build directory.
    assert fake.runs == [None, 1, 2, 3, 4]
    assert fake.builds == _expected_builds(ranked, 4)
    assert len(fake.builds) == 1 + len(kept) + len(dropped)
    # Each build's C++ names its own private copy by absolute path, as `$error` does, and
    # the dropped Mutant still matched the baseline's.
    assert result.detail["equivalent_dropped"] == dropped
    # A dropped Mutant is in the detail only: no report, artifact or row names it.
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


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit-threshold"])
def test_running_out_during_replacement_reports_the_shortfall(
    tmp_path: Path, monkeypatch, explicit: bool
) -> None:
    _write_scope(tmp_path)
    ranked = _candidates(tmp_path)
    kept, dropped = _expected_sweep(ranked, len(ranked))
    # Pin the fixture's counts as literals so the assertions below stay honest.
    assert (len(ranked), len(kept)) == (17, 15)
    assert dropped

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=17,
        seed=0,
        run=_detects_every_mutant,
        simulation=_VERILATED,
        **({"min_detected": 17} if explicit else {}),
    )

    expected = "only 15 Mutants could be scored (17 requested)"
    assert result.detail["shortfall"] == expected
    assert expected in result.report_text
    assert [row["id"] for row in result.detail["classified"]] == kept  # the Mutants it has
    assert result.detail["equivalent_dropped"] == dropped
    assert (result.detail["total_requested"], result.detail["total_scored"]) == (17, 15)
    assert fake.runs == [None, *range(1, len(kept) + 1)]
    if explicit:  # more were required than the run could score
        assert result.exit_code == EXIT_FAILURE
        assert result.detail["min_detected"] == 17
    else:  # the default threshold follows the Mutants kept, not the ones asked for
        assert result.exit_code == EXIT_SUCCESS, result.report_text
        assert result.detail["min_detected"] == _eighty_percent(len(kept))


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
    assert "did not build" not in result.report_text
    assert result.detail["invalid_dropped"] == [] and "invalid" not in result.detail
    assert "  4 detected (0 timeout), 0 not detected" in result.report_text


def test_equivalent_and_build_failure_drops_share_one_replacement_order(
    tmp_path: Path, monkeypatch
) -> None:
    _write_scope(tmp_path)
    ranked = _candidates(tmp_path)
    first_dead = next(i for i, mutant in enumerate(ranked) if mutant.line == _DEAD_LINE)
    # The candidate after the first equivalent drop is the one that does not build:
    # attempts are 1-based and every candidate is built once.
    failing = first_dead + 2
    kept: list[str] = []
    equivalent: list[str] = []
    invalid: list[str] = []
    for attempt, mutant in enumerate(ranked, start=1):
        if len(kept) == 4:
            break
        if attempt == failing:
            invalid.append(mutant.id)
        else:
            (equivalent if mutant.line == _DEAD_LINE else kept).append(mutant.id)
    assert equivalent and invalid

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=4,
        seed=0,
        min_detected=0,
        run=_detects_every_mutant,
        build=_failing_builds(failing),
        simulation=_VERILATED,
    )

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert [(row["index"], row["id"]) for row in result.detail["classified"]] == list(
        enumerate(kept, start=1)
    )
    assert result.detail["equivalent_dropped"] == equivalent
    assert result.detail["invalid_dropped"] == invalid
    assert fake.runs == [None, 1, 2, 3, 4]


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
    assert (result.detail["total_requested"], result.detail["total_scored"]) == (3, 3)
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


_ELABORATION_CORE = """CAPI=2:
name: ::demo:0
filesets:
  rtl:
    files: [rtl/leaf.sv]
    file_type: systemVerilogSource
  tb:
    files: [tb/tb_top.sv]
    file_type: systemVerilogSource
    tags: [tb]
parameters:
  W: {datatype: int, paramtype: vlogparam, default: 8}
  NAME: {datatype: str, paramtype: vlogparam, default: hi}
targets:
  sim:
    default_tool: verilator
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl, tb]
    toplevel: tb_top
    parameters: [W, NAME]
"""


def _run_plan(tmp_path: Path, scope: tuple[str, ...] = (_SCOPE,)) -> MutationRunPlan:
    """A plan that generates every candidate Mutant of ``scope``."""
    return MutationRunPlan(
        scope_files=list(scope),
        scope_hashes={},
        work_dir=tmp_path,
        target="sim",
        tb_top="tb_top",
        report_dir=None,
        min_detected=0,
        count=1000,
        auto_mode=False,
        formula_count=0,
        source_size_budget=None,
        seed=1,
    )


def test_generation_elaborates_the_target_with_its_parameters(tmp_path: Path, monkeypatch) -> None:
    """The testbench's default width is 1; the Target's `vlogparam` makes it 8, which allows `2`."""
    # Private seam: a campaign on the fake catalog elaborates per file (it reports no toplevel),
    # and the real catalog needs a real build for the run that follows generation, so only a
    # direct ``_generate`` call reaches the Target's own elaboration.
    (tmp_path / "design.core").write_text(_ELABORATION_CORE, encoding="utf-8")
    for rel, text in {
        "rtl/leaf.sv": (
            "module leaf #(parameter W = 1) (input [W-1:0] a, output [W-1:0] c);\n"
            "  assign c = 1;\nendmodule\n"
        ),
        "tb/tb_top.sv": (
            'module tb_top #(parameter int W = 1, parameter string NAME = "no") ();\n'
            "  logic [W-1:0] a, c;\n"
            "  leaf #(.W(W)) u (a, c);\nendmodule\n"
        ),
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    _write_tests_toml(tmp_path, '[sim]\ntests = ["smoke"]\n')
    endpoint = _make_endpoint(tmp_path, monkeypatch, target="sim", scope="rtl/leaf.sv")

    generated = endpoint._generate(_run_plan(tmp_path, ("rtl/leaf.sv",)))

    assert generated.identity["elaboration"] == "target"
    assert "elaboration_fallback" not in generated.identity
    assert ("1", "2") in {(m.original_code, m.mutated_code) for m in generated.ranked}
