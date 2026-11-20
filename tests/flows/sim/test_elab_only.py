"""Focused contracts for Simulation's elaboration-only mode."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import booley.flows.sim.build as build_module
from booley.core.file_lock import acquire_file_lock
from booley.flows import job_resources
from booley.flows.base import SubprocessResult
from booley.flows.progress_lifecycle import abort_open_progress
from booley.flows.sim.build import (
    PreparedSimulationBuild,
    SimulationBuildPreparationError,
    build_stage_script,
    classify_build_outcome,
)
from booley.flows.sim.build_session import simulation_build_slot
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.mode import SimulationMode
from booley.flows.sim.request import SimRequest
from booley.flows.source_paths import ProjectSourcePaths
from booley.fusesoc.fusesoc_registry import ResolvedFile
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.mcp.flow_adapter import flow_schema
from booley.runtime.booley_dir import reset_cache
from booley.runtime.pid import capture_process_identity
from booley.targets.catalog import TargetCatalog
from tests.memory_budget_support import fail_budget
from tests.subprocess_budget import run_bounded_child

_DEPRECATION = "--elab-only, --build-only, and --standalone are deprecated; use --mode"
_SOURCES = {"dut.sv": b"module dut; endmodule\n", "tb.sv": b"module tb; endmodule\n"}
_BUILD_TOKEN = re.compile(r"token=([0-9a-f]+)")
BuildLog = list[tuple[list[str], float, "job_resources.ResourceLabel | None"]]


def _result(output: str, *, rc: int = 0, **kwargs: object) -> SubprocessResult:
    return SubprocessResult(returncode=rc, stdout=output, **kwargs)


def _write_core(tmp_path: Path, targets: list[str]) -> None:
    entries = "".join(
        f"  {target}:\n    flow: sim\n    flow_options: {{tool: verilator}}\n"
        "    toplevel: tb_demo\n"
        for target in targets
    )
    body = "CAPI=2:\nname: ::sim:0\ntargets:\n" + entries
    (tmp_path / "sim.core").write_text(body, encoding="utf-8")


def _write_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, targets: list[str], files: dict[str, str]
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    _write_core(tmp_path, targets)
    for name, data in _SOURCES.items():
        (tmp_path / name).write_bytes(data)
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    if (tmp_path / ".booley").is_dir():
        monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
        reset_cache()


def elab_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    builds: dict[str, SubprocessResult | Exception],
    *argv: str,
    files: dict[str, str] | None = None,
    mode: str | None = "elab-only",
    on_build: Callable[[list[str]], None] | None = None,
) -> tuple[Any, BuildLog]:
    """Run ``SimulateFlow.execute_cli`` over one Target per key of *builds*.

    An exception value makes that Target's build preparation raise it; a
    ``SubprocessResult`` is what the build process answers, with its authenticated
    ``BOOLEY_BUILD_STAGE`` record. Returns the result and every build command seen as
    ``(command, timeout, resource label)``.
    """
    _write_project(tmp_path, monkeypatch, list(builds), files or {})
    seen: BuildLog = []
    current: list[str] = []

    def prepare(handle: Any, *, build_root: Path, **_kwargs: Any) -> PreparedSimulationBuild:
        build = builds[handle.selector]
        if isinstance(build, Exception):
            raise build
        current[:] = [handle.selector]
        inner = build_root / "inner"
        inner.mkdir(parents=True)
        image = inner / "Vtb_demo"
        image.write_bytes(b"image")
        image.chmod(0o755)
        resolved = SimpleNamespace(
            build_root=tmp_path,
            cocotb_module="",
            files=tuple(ResolvedFile(name, "systemVerilogSource") for name in _SOURCES),
        )
        return PreparedSimulationBuild(
            handle.selector,
            handle.identity,
            resolved,  # pyright: ignore[reportArgumentType]
            build_root,
            inner,
            "verilator",
            "tb_demo",
            ("make", "-C", "cache"),
            {"SIM_MODE": "build"},
            {},
        )

    def boundary(self: SimulateFlow, command: list[str], *, timeout: float, **_kwargs: Any) -> Any:
        del self
        seen.append((command, timeout, job_resources.current_label()))
        if on_build is not None:
            on_build(command)
        build = builds[current[0]]
        assert isinstance(build, SubprocessResult)
        token = _BUILD_TOKEN.search(command[2])
        assert token is not None
        record = f"BOOLEY_BUILD_STAGE token={token.group(1)} rc={build.returncode}\n"
        return SubprocessResult(
            returncode=build.returncode, stdout=build.stdout + record, stderr=build.stderr
        )

    # Private seam: the build process boundary has no public injection point.
    monkeypatch.setattr(SimulateFlow, "_execute_boundary", boundary)
    monkeypatch.setattr("booley.flows.sim.elab_only.prepare_simulation_build", prepare)
    job_resources.clear_label()
    argv_all = [
        "--work-dir",
        str(tmp_path),
        "--report-dir",
        str(tmp_path / "reports"),
        "--target",
        ",".join(builds),
        *(["--mode", mode] if mode else []),
        *argv,
    ]
    try:
        return SimulateFlow().execute_cli(argv_all), seen
    finally:
        job_resources.clear_label()


_PASS = SubprocessResult(returncode=0, stdout="complete compiler output\n")
_DESIGN_FAIL = SubprocessResult(returncode=1, stdout="%Error: rtl/top.sv:1: syntax error\n")
_SETUP_ERROR = SimulationBuildPreparationError("current setup exploded")
_MISSING_VERILATOR = SubprocessResult(
    returncode=2,
    stdout="/bin/sh: 1: verilator: not found\nmake: *** [Makefile:8: Vtop.mk] Error 127\n",
)


def _report(tmp_path: Path) -> Path:
    return (tmp_path / "reports/sim/1/report.json").resolve()


def _flow_with_state(tmp_path: Path, targets: list[str]) -> SimulateFlow:
    _write_core(tmp_path, targets)
    flow = SimulateFlow()
    with patch.dict(os.environ, {"BOOLEY_SLUG": "elab-only-test"}):
        flow.parse_args(
            [
                "--work-dir",
                str(tmp_path),
                "--report-dir",
                str(tmp_path / "reports"),
                "--target",
                ",".join(targets),
                "--mode",
                "elab-only",
            ]
        )
    return flow


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("simulate", SimulationMode.SIMULATE),
        ("elab-only", SimulationMode.ELAB_ONLY),
        ("elab_only_standalone", SimulationMode.ELAB_ONLY_STANDALONE),
        ("invalid", None),
    ],
)
def test_mode_spellings(tmp_path: Path, spelling: str, expected: SimulationMode | None) -> None:
    flow = SimulateFlow()
    argv = ["--work-dir", str(tmp_path), "--target", "sim_dut", "--mode", spelling]

    if expected is None:
        with pytest.raises(SystemExit):
            flow.parse_args(argv)
    else:
        flow.parse_args(argv)
        assert flow.args.mode is expected


@pytest.mark.parametrize(
    ("flags", "mode"),
    [
        (["--elab-only"], SimulationMode.ELAB_ONLY),
        (["--build-only"], SimulationMode.ELAB_ONLY),
        (["--elab-only", "--standalone"], SimulationMode.ELAB_ONLY_STANDALONE),
        (["--build-only", "--standalone"], SimulationMode.ELAB_ONLY_STANDALONE),
    ],
)
def test_legacy_elaboration_aliases_normalize(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, flags: list[str], mode: SimulationMode
) -> None:
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim_dut", *flags])

    assert flow.args.mode is mode
    assert caplog.messages == [_DEPRECATION]


def test_mcp_schema_exposes_only_canonical_property_and_description() -> None:
    flow = SimulateFlow()
    schema = flow_schema(flow)

    assert schema["properties"]["mode"] == {
        "type": "string",
        "enum": ["simulate", "elab_only", "elab_only_standalone"],
        "description": (
            "Execution mode: run tests, elaborate only, or elaborate then "
            "perform the standalone module sweep"
        ),
    }
    assert "_legacy_elab_only" not in schema["properties"]
    assert "_legacy_standalone" not in schema["properties"]
    assert "mode=elab_only" in flow.description
    assert "mode=elab_only_standalone" in flow.description


@pytest.mark.parametrize("mode", ["elab-only", "elab-only-standalone"])
@pytest.mark.parametrize(
    ("extra", "argument"),
    [
        (["--test", "smoke"], "--test"),
        (["--trace"], "--trace"),
        (["--no-kill"], "--no-kill"),
    ],
)
def test_elab_only_rejects_run_stage_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, extra: list[str], argument: str
) -> None:
    fail_budget(monkeypatch)
    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _PASS}, *extra, mode=mode)

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.detail["mode"] == mode.replace("-", "_")
    assert argument in result.outcome.report_text
    assert "use --mode simulate" in result.outcome.report_text
    assert "memory_admission_warning" not in result.outcome.detail


def test_standalone_requires_elab_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fail_budget(monkeypatch)
    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _PASS}, "--standalone", mode=None)

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.detail["mode"] == "simulate"
    assert "--standalone alone is invalid" in result.outcome.report_text
    assert "--mode elab-only-standalone" in result.outcome.report_text
    assert "memory_admission_warning" not in result.outcome.detail


def test_coverage_requires_simulation_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_budget(monkeypatch)
    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _PASS}, "--coverage")

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == "Coverage requires simulation mode"
    assert result.outcome.detail["mode"] == "elab_only"
    assert "memory_admission_warning" not in result.outcome.detail


@pytest.mark.parametrize("dry_run", [False, True])
def test_early_elab_error_names_available_report_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    result, seen = elab_cli(
        tmp_path,
        monkeypatch,
        {"sim_dut": _PASS},
        *(["--dry-run"] if dry_run else []),
        files={".booley/booley.toml": "[flows.sim]\nenabled = false\n"},
    )

    expected = "sim is disabled ([flows.sim].enabled = false)."
    if not dry_run:
        expected += f"\n  report: {_report(tmp_path)}"
    assert result.exit_code == EXIT_ERROR
    assert result.outcome.detail["mode"] == "elab_only"
    assert result.outcome.report_text == expected
    assert "manifest:" not in result.outcome.report_text
    assert seen == []


def test_elab_only_target_resolution_error_keeps_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim_dut", "--mode", "elab-only"])
    # Private seam: the endpoint refuses an unknown Target before the elaboration preflight, so only a stubbed resolver reaches its error branch.
    monkeypatch.setattr(
        flow,
        "_resolve_requested_targets",
        lambda: McpToolResult(exit_code=EXIT_ERROR, report_text="bad target"),
    )

    with flow.publication_resources:
        result = flow._run()

    assert result.exit_code == EXIT_ERROR
    assert result.detail["mode"] == "elab_only"


def test_explicit_mode_conflicts_with_legacy_flags(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        SimulateFlow().parse_args(
            [
                "--work-dir",
                str(tmp_path),
                "--target",
                "sim_dut",
                "--mode",
                "elab-only",
                "--standalone",
            ]
        )


# A Target with one RTL module, so the standalone sweep has something to probe.
_RTL_CORE = (
    "CAPI=2:\nname: ::sim:0\nfilesets:\n  rtl:\n    files: [dut.sv]\n"
    "    file_type: systemVerilogSource\ntargets:\n  sim_dut:\n    flow: sim\n"
    "    flow_options: {tool: verilator}\n    filesets: [rtl]\n    toplevel: tb_demo\n"
)


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every standalone probe command, answered as a clean compile."""
    seen: list[list[str]] = []

    def probe(_self: SimulateFlow, command: list[str], **_kwargs: Any) -> SubprocessResult:
        seen.append(command)
        return SubprocessResult(returncode=0)

    # Private seam: the sweep's probe compile needs a real toolchain; no public input replaces it.
    monkeypatch.setattr(SimulateFlow, "_execute", probe)
    return seen


def test_standalone_mode_runs_target_elaboration_before_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[list[str]]
) -> None:
    probes_before_build: list[int] = []
    result, seen = elab_cli(
        tmp_path,
        monkeypatch,
        {"sim_dut": _PASS},
        files={"sim.core": _RTL_CORE},
        mode="elab-only-standalone",
        on_build=lambda _command: probes_before_build.append(len(probes)),
    )

    assert result.exit_code == EXIT_SUCCESS, result.outcome.report_text
    assert len(seen) == 1
    assert probes_before_build == [0]  # the Target build ran before the sweep's first probe
    assert len(probes) == 1
    assert "dut" in probes[0]
    assert "standalone" in result.outcome.detail


def test_build_stage_script_emits_record_before_run_half() -> None:
    script = build_stage_script(["make", "-C", "build"], "abc123", run_line="run-sim")

    assert script.index("make -C build") < script.index("token=abc123")
    assert script.index("token=abc123") < script.index("run-sim")
    assert "ERROR: Verilator elaboration failed" not in script


def test_build_stage_script_measures_both_halves_and_preserves_run_exit() -> None:
    script = build_stage_script(["true"], "abc123", run_line="sh -c 'exit 7'")

    completed = run_bounded_child(
        ["sh", "-c", script],
    )

    assert completed.returncode == 7
    assert re.search(
        r"^BOOLEY_BUILD_STAGE token=abc123 rc=0 duration_ms=\d+$",
        completed.stdout,
        re.MULTILINE,
    )
    assert re.search(
        r"^BOOLEY_RUN_STAGE token=abc123 rc=7 duration_ms=\d+$",
        completed.stdout,
        re.MULTILINE,
    )


_BUILD_RECORD = "BOOLEY_BUILD_STAGE token=abc123"
_DESIGN_ERROR = "%Error: rtl/top.sv:4: syntax error\n"


@pytest.mark.parametrize(
    ("stdout", "rc", "extra", "verdict", "kind"),
    [
        (f"compiler output\n{_BUILD_RECORD} rc=0\nruntime failed", 1, {}, "pass", None),
        ("BOOLEY_BUILD_STAGE token=wrong rc=0\n", 0, {}, None, "infrastructure"),
        (f"{_BUILD_RECORD} rc=0\n{_BUILD_RECORD} rc=0\n", 0, {}, None, "infrastructure"),
        (f"{_DESIGN_ERROR}{_BUILD_RECORD} rc=1", 1, {}, "fail", "design"),
        (f"make stopped\n{_BUILD_RECORD} rc=1", 1, {}, None, "infrastructure"),
        (f"{_BUILD_RECORD} rc=1\n", 1, {"stderr": _DESIGN_ERROR}, "fail", "design"),
        (f"{_DESIGN_ERROR}{_BUILD_RECORD} rc=139", 139, {}, None, "infrastructure"),
        ("still compiling", -1, {"timed_out": True}, None, "infrastructure"),
        (f"{_BUILD_RECORD} rc=0\nruntime still active", -1, {"timed_out": True}, "pass", None),
    ],
    ids=[
        "success",
        "spoofed",
        "duplicate",
        "design",
        "ambiguous",
        "stderr-design",
        "signal-139",
        "timeout",
        "late-timeout",
    ],
)
def test_classifier_grades_the_authenticated_build_record(
    stdout: str, rc: int, extra: dict[str, Any], verdict: str | None, kind: str | None
) -> None:
    outcome = classify_build_outcome(_result(stdout, rc=rc, **extra), "abc123", timeout_s=7)

    assert outcome.passed is (verdict == "pass")
    assert outcome.verdict == verdict
    assert outcome.failure_kind == kind


def test_classifier_uses_authenticated_build_duration() -> None:
    outcome = classify_build_outcome(
        _result(f"output\n{_BUILD_RECORD} rc=0 duration_ms=17\nruntime", duration_s=4.0), "abc123"
    )

    assert outcome.elapsed_s == 0.017


def test_timeout_before_terminal_record_names_the_budget() -> None:
    outcome = classify_build_outcome(
        _result("still compiling", rc=-1, timed_out=True), "abc123", timeout_s=7
    )

    assert outcome.reason == "build timed out after 7 s (raise [flows.sim].build_timeout_ms)"
    assert outcome.output == "still compiling"
    assert (
        build_module.build_failure_report(outcome, ProjectSourcePaths()).splitlines()[0]
        == outcome.reason
    )


def test_simulation_record_runs_every_target_and_reports_the_worst_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds: dict[str, SubprocessResult | Exception] = {
        "clean": _PASS,
        "broken": _DESIGN_FAIL,
        "infra": _SETUP_ERROR,
    }
    result, seen = elab_cli(tmp_path, monkeypatch, builds)

    outcome = result.outcome
    assert result.exit_code == EXIT_ERROR
    assert f"  report: {_report(tmp_path)}" in outcome.report_text
    assert "manifest:" not in outcome.report_text
    assert len(seen) == 2
    assert outcome.detail["mode"] == "elab_only"
    assert [entry["failure_class"] for entry in outcome.detail["targets"]] == [
        None,
        "design",
        "infrastructure",
    ]
    progress = (tmp_path / "reports/sim/1/progress.json").read_text(encoding="utf-8")
    assert '"mode": "elab_only"' in progress
    assert '"complete": true' in progress
    for target in builds:
        report = tmp_path / f"reports/sim/1/targets/{target}/simulation.json"
        assert '"mode": "elab_only"' in report.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("build", "expected"),
    [(_PASS, EXIT_SUCCESS), (_DESIGN_FAIL, EXIT_FAILURE), (_SETUP_ERROR, EXIT_ERROR)],
    ids=["pass", "design-failure", "setup-error"],
)
def test_simulation_record_exit_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build: SubprocessResult | Exception,
    expected: int,
) -> None:
    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": build})

    assert result.exit_code == expected


def test_missing_executable_is_typed_in_elab_only_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _MISSING_VERILATOR})

    outcome = result.outcome
    assert result.exit_code == EXIT_ERROR
    assert outcome.detail["mode"] == "elab_only"
    assert outcome.detail["eda_tool_error"] == "missing_executable"
    assert outcome.detail["missing_executable"] == "verilator"
    assert "required executable 'verilator'" in outcome.report_text
    assert f"  report: {_report(tmp_path)}" in outcome.report_text
    assert "manifest:" not in outcome.report_text


@pytest.mark.parametrize("signalled", [False, True])
def test_elab_only_terminalizes_after_later_target_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signalled: bool
) -> None:
    progress_path = tmp_path / "reports/sim/1/progress.json"
    published: list[dict[str, object]] = []

    class Builds(dict[str, Any]):
        def __getitem__(self, selector: str) -> Any:
            if signalled and selector == "sim_b":  # a signal ends the process here
                abort_open_progress()
                published.append(json.loads(progress_path.read_text(encoding="utf-8")))
            return super().__getitem__(selector)

    crash = RuntimeError("simulated elaboration interruption")
    result, _seen = elab_cli(tmp_path, monkeypatch, Builds(sim_a=_PASS, sim_b=crash))

    assert result.exit_code == EXIT_ERROR
    assert "elaboration interruption" in result.outcome.report_text
    progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text(encoding="utf-8"))
    assert progress["complete"] is True
    assert progress["phase"] == "aborted"
    assert progress["completed_targets"] == ["sim_a"]
    assert progress["pending_targets"] == ["sim_b"]
    if signalled:
        (snapshot,) = published
        assert (snapshot["phase"], snapshot["pending_targets"]) == ("aborted", ["sim_b"])


def test_one_target_executes_only_authenticated_make_and_resolves_its_build_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, seen = elab_cli(
        tmp_path,
        monkeypatch,
        {"sim_dut": _PASS},
        files={".booley/booley.toml": "[flows.sim]\nbuild_timeout_ms = 7000\n"},
    )

    assert result.exit_code == EXIT_SUCCESS
    ((command, timeout, label),) = seen
    assert command[:2] == ["sh", "-c"]
    assert "make -C cache" in command[2]
    assert "SIM_MODE" in command[2]
    assert "cocotb" not in command[2].lower()
    assert "pre_run" not in command[2].lower()
    assert timeout == 7
    assert label == job_resources.ResourceLabel("sim_dut", "sim_build", source_bytes=43)
    log = tmp_path / "reports/sim/1/artifacts/sim_sim_dut/run.log"
    assert log.read_text(encoding="utf-8").startswith("complete compiler output")
    # An elaboration-only build is never promoted, so its generation is pruned.
    slots = list(tmp_path.glob(".booley/.runtime/edalize/sim/*/lease.lock"))
    assert len(slots) == 1
    assert list((slots[0].parent / "g").iterdir()) == []


def test_elab_only_marks_the_pruned_generation_log_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: list[tuple[Path, bool]] = []

    observer = MagicMock()
    observer.log_path_known.side_effect = lambda path, *, target, temporary: registered.append(
        (path, temporary)
    )
    monkeypatch.setattr("booley.flows.terminal_progress.current_progress", lambda: observer)
    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _PASS})

    assert result.exit_code == EXIT_SUCCESS
    assert registered
    assert all(temporary and "/g/" in path.as_posix() for path, temporary in registered)


def test_elaboration_plan_discloses_build_budget_as_its_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elab_cli(
        tmp_path,
        monkeypatch,
        {"sim_dut": _PASS},
        "--dry-run",
        files={".booley/booley.toml": "[flows.sim]\nbuild_timeout_ms = 7000\n"},
    )

    plan = json.loads((tmp_path / "reports/sim/flow_plan.json").read_text(encoding="utf-8"))
    (unit,) = plan["work_units"]
    assert unit["timeout_ms"] == 7000
    assert unit["recipe"]["build_timeout_ms"] == 7000


def test_setup_failure_archives_current_error_without_reusing_old_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _flow_with_state(tmp_path, ["sim_dut"])
    old = tmp_path / "reports/sim/1/artifacts/sim_sim_dut/run.log"
    old.parent.mkdir(parents=True)
    old.write_text("old passing output", encoding="utf-8")

    def fail_setup(*_args: object, **_kwargs: object) -> PreparedSimulationBuild:
        raise _SETUP_ERROR

    monkeypatch.setattr("booley.flows.sim.elab_only.prepare_simulation_build", fail_setup)

    # Private seam: the stale log sits in the invocation directory only an in-flight attempt reuses; the CLI always reserves a fresh one.
    with flow.publication_resources:
        result = flow._run_one_elab_only("sim_dut")

    assert result.outcome.verdict is None
    assert result.outcome.failure_kind == "infrastructure"
    assert result.log_path
    current = (tmp_path / result.log_path).read_text(encoding="utf-8")
    assert "current setup exploded" in current
    assert "old passing output" not in current


def test_plan_keeps_non_shell_commands_unchanged(tmp_path: Path) -> None:
    flow = _flow_with_state(tmp_path, ["sim_dut"])
    command = ("make", "-C", ".booley_work/sim")

    # Private seam: the plan renders only shell commands through it; no public Target has another kind.
    assert flow._redact_plan_environment("sim_dut", command) == command


@pytest.mark.parametrize(
    ("build", "expected"), [(_DESIGN_FAIL, EXIT_FAILURE), (_MISSING_VERILATOR, EXIT_ERROR)]
)
def test_a_log_that_cannot_be_archived_leaves_its_pointer_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build: SubprocessResult, expected: int
) -> None:
    def block_archive(_command: list[str]) -> None:
        (tmp_path / "reports/sim/1/artifacts").write_text("not a directory", encoding="utf-8")

    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": build}, on_build=block_archive)

    assert result.exit_code == expected
    assert "artifacts" not in result.outcome.detail
    assert "(log:" not in result.outcome.report_text


@pytest.mark.skipif(os.name == "nt", reason="needs a symlinked report root")
def test_early_elab_error_keeps_diagnosis_when_report_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
    result, _seen = elab_cli(
        tmp_path,
        monkeypatch,
        {"sim_dut": _PASS},
        "--report-dir",
        str(tmp_path / "linked"),
        files={".booley/booley.toml": "[flows.sim]\nenabled = false\n"},
    )

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text.startswith("sim is disabled ([flows.sim].enabled = false).")
    assert "  report:" not in result.outcome.report_text


def test_target_report_write_retries_sharing_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.sharing_error_support import hold_replaces

    path = tmp_path / "reports/sim/1/targets/sim_dut/simulation.json"
    attempts = hold_replaces(monkeypatch, {path: 1})
    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _PASS})

    assert result.exit_code == EXIT_SUCCESS
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "elab_only"
    assert attempts[path] == 2


def test_target_report_write_fails_after_lasting_sharing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.sharing_error_support import hold_replaces

    path = tmp_path / "reports/sim/1/targets/sim_dut/simulation.json"
    attempts = hold_replaces(monkeypatch, {path: 100})
    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _PASS})

    assert result.exit_code == EXIT_ERROR
    assert attempts[path] > 1


def test_standalone_sweep_log_cannot_collide_with_target_named_standalone(
    tmp_path: Path,
) -> None:
    # Private seam: the sweep log is written only by a real probe compile; no public path runs one probe without a toolchain.
    flow = _flow_with_state(tmp_path, ["standalone"])

    with flow.publication_resources:
        target_pointer = flow._persist_elab_only_log("standalone", "target output")
        sweep_pointer = flow._persist_standalone_log("sweep output")

    assert target_pointer != sweep_pointer
    assert (tmp_path / target_pointer).read_text(encoding="utf-8").endswith("target output")
    assert sweep_pointer is not None
    assert (tmp_path / sweep_pointer).read_text(encoding="utf-8").endswith("sweep output")


def test_elab_only_dry_run_renders_setup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise SimulationBuildPreparationError("cgroup denied")

    monkeypatch.setattr("booley.flows.sim.elab_only.simulation_setup_command", refuse)
    result, seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _PASS}, "--dry-run")

    assert result.exit_code == EXIT_ERROR
    assert "sim_dut: ERROR: sim elab-only dry-run: cgroup denied" in result.outcome.report_text
    assert seen == []


def test_elab_only_branch_skips_test_and_cocotb_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Private seam: a test that discovery never runs can only fail the run if discovery is replaced.
    for module in ("booley.flows.sim.flow", "booley.flows.sim.test_selection"):
        monkeypatch.setattr(
            f"{module}._get_test_names",
            lambda *_args, **_kwargs: pytest.fail("test discovery ran in elab-only mode"),
        )
    monkeypatch.setattr(
        SimulateFlow,
        "_validate_cocotb_targets",
        lambda *_args: pytest.fail("Cocotb validation ran in elab-only mode"),
    )

    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _PASS})

    assert result.exit_code == EXIT_SUCCESS


@pytest.mark.parametrize("mode", ["elab-only", "elab-only-standalone"])
def test_elab_cli_card_has_one_report_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    probes: list[list[str]],
    mode: str,
) -> None:
    result, _seen = elab_cli(
        tmp_path, monkeypatch, {"sim_dut": _PASS}, files={"sim.core": _RTL_CORE}, mode=mode
    )

    output = capsys.readouterr()
    assert result.exit_code == 0
    assert output.out.count(f"  report: {_report(tmp_path)}") == 1
    assert _report(tmp_path).is_file()
    assert output.out.count("RESULT: PASS") == 1
    assert "manifest:" not in output.out + output.err


def test_build_only_live_checkpoint_is_building_before_invoker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[dict[str, Any]] = []

    def check(_command: list[str]) -> None:
        payload = json.loads((tmp_path / "reports/sim/1/progress.json").read_text())
        observed.append(payload)

    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _PASS}, on_build=check)

    assert result.exit_code == EXIT_SUCCESS
    (payload,) = observed
    assert payload["phase"] == "starting"
    assert payload["complete"] is False
    assert {entry["stage"] for entry in payload["active"]} == {"building"}


def test_standalone_live_probe_has_module_and_build_only_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.live_progress import LiveProgressSink, install_progress
    from booley.flows.sim.standalone import _StandaloneProbe

    # Private seam: the live probe stage is written only by a real probe compile; no public path runs one probe without a toolchain.
    flow = _flow_with_state(tmp_path, ["sim_dut"])
    invocation = flow.reserve_invocation_dir()
    assert invocation is not None

    def execute(command, *, timeout):
        payload = json.loads((invocation / "progress.json").read_text())
        assert payload["phase"] == "starting"
        assert [
            (entry["target"], entry["stage"], entry["operation"]) for entry in payload["active"]
        ] == [("dut", "building", "standalone")]
        return _result("standalone compiled")

    monkeypatch.setattr(flow, "_execute", execute)
    monkeypatch.setattr(flow, "_standalone_compile_command", lambda *_args: ["compiler"])
    sink = LiveProgressSink(
        invocation / "progress.json", tmp_path, os.environ.get("BOOLEY_RUN_ID", "")
    )
    with flow.publication_resources, install_progress(sink):
        flow._write_elab_only_progress(["sim_dut"], [], phase="starting")
        finding, _log, error = flow._run_standalone_probe(
            _StandaloneProbe("dut", "dut.sv", "verilator", (), False, ()), timeout_s=5
        )
    assert finding is None
    assert error == ""


def test_elab_only_dry_run_writes_flow_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Elaboration Check keeps printing its elaboration FlowPlan (ordinary runs have none)."""
    from tests.flows.sim.test_endpoint_record_lifecycle import _ordinary_public_hook_fixture

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)

    result = flow.execute(
        SimRequest(
            target="sim_a",
            mode="elab_only",
            dry_run=True,
            work_dir=project,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == EXIT_SUCCESS, result.outcome.report_text
    plan = json.loads((tmp_path / "reports/sim/flow_plan.json").read_text(encoding="utf-8"))
    assert plan["mode"] == "elab_only"
    assert [unit["selector"] for unit in plan["work_units"]] == ["sim_a"]


def test_elab_only_lease_wait_is_bounded_by_the_build_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy slot ends the elaboration after the build timeout, not 60 s (#4843)."""
    files = {".booley/booley.toml": "[flows.sim]\nbuild_timeout_ms = 1000\n"}
    _write_project(tmp_path, monkeypatch, ["sim_dut"], files)
    handle = TargetCatalog.build(tmp_path).select("sim_dut", for_flow="sim")
    slot = simulation_build_slot(handle)
    slot.mkdir(parents=True)

    with (slot / "lease.lock").open("a+", encoding="utf-8") as holder:
        acquire_file_lock(holder)
        started = time.monotonic()
        result, seen = elab_cli(tmp_path, monkeypatch, {"sim_dut": _PASS}, files=files)
        waited = time.monotonic() - started

    assert result.exit_code == EXIT_ERROR
    assert "Simulation build slot failed" in result.outcome.report_text
    assert "busy for 1s" in result.outcome.report_text
    assert seen == []
    # A loose upper bound on a real 1 s wait; the old fixed wait was 60 s.
    assert waited < 30


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.skipif(
    capture_process_identity(os.getpid()) is None, reason="needs a process identity"
)
def test_elab_only_run_sweeps_a_cancelled_runs_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every Simulation Flow invocation, elaboration-only included, sweeps first."""
    from booley.flows.sim.trace_overlay import write_trace_overlay
    from booley.targets.catalog import TargetCatalog
    from tests.flows.sim.test_simulation_record_crash_matrix import _KILLED, _run_killed

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    _write_core(tmp_path, ["sim"])
    (tmp_path / ".booley").mkdir()
    handle = TargetCatalog.build(tmp_path).select("sim")

    def worker() -> None:
        write_trace_overlay(handle)
        os._exit(_KILLED)

    _run_killed(worker, tmp_path / "trace.txt")
    assert (tmp_path / "sim.booleytrace.core").exists()

    result, _seen = elab_cli(tmp_path, monkeypatch, {"sim": _PASS})

    assert result.exit_code == 0
    assert not (tmp_path / "sim.booleytrace.core").exists()
