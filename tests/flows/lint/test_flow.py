"""Tests for LintFlow вЂ” warning parsing, dedup, scope, reports."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
import yaml

from booley.flows import artifacts as flow_artifacts
from booley.flows.base import FlowMechanics, SubprocessResult
from booley.flows.lint.flow import (
    LintConfigResult,
    LintFlow,
    LintWarning,
    deduplicate_warnings,
    filter_by_scope,
    parse_verible_warnings,
    parse_warnings,
)
from booley.flows.lint.request import LintRequest
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.runtime.endpoint_execution import ExecutionResult
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import IncompatibleTargetError, MissingTargetToplevelError, TargetHandle
from tests.canned_process_support import canned_command
from tests.subprocess_budget import run_bounded_child
from tests.target_test_support import install_lenient_target_catalog, make_target_handle

_REAL_CATALOG_BUILD = TargetCatalog.build
_REAL_RESOLVE_TARGET_HANDLE = fusesoc_registry.resolve_target_handle
_REAL_SETUP_COMMAND_FOR_HANDLE = fusesoc_registry.setup_command_for_handle


def _target_handle(
    selector: str,
    *,
    project_root: Path | str | None = None,
    vlnv: str | None = None,
    flow: str | None = "lint",
    eda_tool: str | None = "verilator",
) -> TargetHandle:
    """Build a selected Target value for layer-focused Lint tests."""
    root = Path.cwd() if project_root is None else Path(project_root)
    target_vlnv = vlnv or f"::{selector}:0"
    return make_target_handle(
        root,
        selector,
        vlnv=target_vlnv,
        flow=flow,
        eda_tool=eda_tool,
        drivable_by=("lint",),
        core_file=root.resolve() / f"{selector}.core",
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
        fallback_handle=lambda root, token: _target_handle(token, project_root=root),
    )


SAMPLE_VERILATOR_OUTPUT = """\
%Warning-UNUSEDSIGNAL: rtl/mod_a.sv:42:5: Signal is not used: 'foo'
                       ... In instance 'design_top.mod_a_inst'
%Warning-UNDRIVEN: rtl/mod_a.sv:100:3: Signal is not driven: 'bar'
%Warning-WIDTH: rtl/mod_c.sv:55:10: Operator ASSIGN expects 32 bits on the LHS
"""


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set the slug env var and working directory; return the unused state path."""
    monkeypatch.setenv("BOOLEY_SLUG", "test-lint")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "state.json"


def _stub_resolved(
    eda_tool: str | None = "verilator",
    files: tuple[str, ...] = (),
    build_root: Path = Path("build"),
) -> object:
    """Minimal ResolvedTarget standing for what ``resolve_target_handle`` returns.

    Empty toplevel/files skip the coverage checks. ``files`` declares linted
    SystemVerilog sources (EDAM ``name``s).
    """
    from booley.fusesoc import fusesoc_registry

    return fusesoc_registry.ResolvedTarget(
        name="stub",
        vlnv="::stub_demo:0",
        toplevel="",
        eda_tool=eda_tool,
        configured_eda_tool=eda_tool,
        files=tuple(
            fusesoc_registry.ResolvedFile(name=name, file_type="systemVerilogSource")
            for name in files
        ),
        parameters={},
        build_root=build_root,
        edam_path=Path("build/stub_demo_0.eda.yml"),
    )


def _process(
    returncode: int = 0, stdout: str = "", stderr: str = "", timed_out: bool = False
) -> SubprocessResult:
    """A canned linter subprocess result."""
    return SubprocessResult(
        returncode=returncode, stdout=stdout, stderr=stderr, timed_out=timed_out, duration_s=0.1
    )


_LintRun = Callable[..., tuple[ExecutionResult, list[list[str]]]]


@pytest.fixture()
def run_lint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _LintRun:
    """Run ``booley flow lint`` through its CLI entry with a canned linter subprocess.

    ``run(*processes, args=(), target="lite", work_dir=None, resolved=...)``
    returns the result and the command of every linter run. The Targets run in
    order and take the processes in order (the last repeats). ``resolved(handle,
    build_root)`` stands for ``resolve_target_handle``; ``resolved=None`` keeps
    the resolver the test installed. ``measured=True`` runs each canned result as a
    real child process through the measured run, so a history record is written.
    """
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)

    def run(
        *processes: SubprocessResult,
        args: tuple[str, ...] = (),
        target: str = "lite",
        work_dir: Path | None = None,
        resolved: Callable[..., object] | None = lambda handle, build_root: _stub_resolved(
            build_root=tmp_path / handle.selector
        ),
        measured: bool = False,
    ) -> tuple[ExecutionResult, list[list[str]]]:
        commands: list[list[str]] = []

        def execute(_flow: LintFlow, cmd: list[str], **_kwargs: object) -> SubprocessResult:
            commands.append(cmd)
            process = processes[min(len(commands), len(processes)) - 1]
            if measured:  # the real measured run, which writes the history record
                return FlowMechanics._execute_local(_flow, canned_command(process))
            return process

        # Unit tests have no Verilator: the linter subprocess is the one
        # private seam the public Flow entry cannot replace.
        monkeypatch.setattr(LintFlow, "_execute", execute)
        if resolved is not None:
            monkeypatch.setattr(
                fusesoc_registry,
                "resolve_target_handle",
                lambda handle, build_root, **_kwargs: resolved(handle, build_root),
            )
        argv = ["--target", target, "--work-dir", str(work_dir or tmp_path), *args]
        return LintFlow().execute_cli(argv), commands

    return run


# A minimal CAPI2 .core whose `lint` Target carries the -Wall option that
# Booley's deleted _build_lint_edam used to inject. --lint-only is added by the
# lint flow itself, so the Target only needs the extra verilator option.
_LINT_CORE_TEXT = """\
CAPI=2:
name: ::lint_demo:0
description: lint slice fixture
filesets:
  rtl:
    files:
      - rtl/top.sv: {file_type: systemVerilogSource}
    file_type: systemVerilogSource
targets:
  default:
    filesets: [rtl]
  lite:
    default_tool: verilator
    flow: lint
    flow_options:
      tool: verilator
      verilator_options: [-Wall]
    filesets: [rtl]
    toplevel: top
"""


def _lenient_catalog_declaring(monkeypatch: pytest.MonkeyPatch, eda_tool: str | None) -> None:
    """Make every pass-through Target declare ``eda_tool`` as its linter."""
    install_lenient_target_catalog(
        monkeypatch,
        real_catalog_build=_REAL_CATALOG_BUILD,
        fallback_handle=lambda root, token: _target_handle(
            token, project_root=root, eda_tool=eda_tool
        ),
    )


@pytest.mark.parametrize(
    ("declared", "configured", "resolution_error", "expected"),
    [
        pytest.param(
            "verilator",
            None,
            None,
            "lint planning failed: lite: lint setup failed: unknown lint EDA tool None",
            id="configured-missing",
        ),
        pytest.param(
            "verilator",
            "mystery-lint",
            None,
            "lint planning failed: lite: lint setup failed: unknown lint EDA tool 'mystery-lint'",
            id="configured-unknown",
        ),
        pytest.param(
            None,
            "verilator",
            None,
            "lint planning failed: lite: unknown lint EDA tool None",
            id="declared-missing",
        ),
        pytest.param(
            "mystery-lint",
            "verilator",
            None,
            "lint planning failed: lite: unknown lint EDA tool 'mystery-lint'",
            id="declared-unknown",
        ),
        pytest.param(
            "verible",
            "verilator",
            None,
            "lint planning failed: lite: lint setup failed: lint Target 'lite' declared "
            "'verible' but FuseSoC configured 'verilator'",
            id="declared-configured-disagree",
        ),
        pytest.param(
            "verilator",
            "verilator",
            fusesoc_registry.TargetResolutionError("boom"),
            "lint planning failed: lite: lint setup failed: boom",
            id="resolution-fails",
        ),
    ],
)
def test_lint_rejects_an_unusable_linter_before_running_it(
    run_lint: _LintRun,
    monkeypatch: pytest.MonkeyPatch,
    declared: str | None,
    configured: str | None,
    resolution_error: Exception | None,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _lenient_catalog_declaring(monkeypatch, declared)

    def resolved(_handle: TargetHandle, _build_root: Path) -> object:
        if resolution_error is not None:
            raise resolution_error
        return _stub_resolved(configured)

    result, commands = run_lint(_process(), resolved=resolved)

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text.startswith(expected)
    assert commands == []
    # A Target that fails before its linter is resolved prints its line untagged.
    assert ("[lint] lite         ERROR: lint setup failed: " in capsys.readouterr().out) == (
        "lint setup failed" in expected
    )


def test_lint_runs_make_over_the_resolved_build_root(run_lint: _LintRun, tmp_path: Path) -> None:
    """Booley resolves the Target through FuseSoC, then `make -C <relpath>`."""
    resolved_build = tmp_path / ".booley/.runtime/edalize/lint/lite/lint_demo_0/lite"
    resolver_calls: list[tuple[str, Path]] = []

    def resolved(handle: TargetHandle, build_root: Path) -> object:
        resolver_calls.append((handle.selector, build_root))
        return _stub_resolved(build_root=resolved_build)

    result, commands = run_lint(_process(), resolved=resolved)

    assert result.exit_code == EXIT_SUCCESS
    # The resolver gets the per-(EDA tool, config) Edalize dir, not the dir make runs in.
    assert resolver_calls == [("lite", tmp_path / ".booley/.runtime/edalize/lint/lite")]
    (command,) = commands
    assert command[:2] == ["sh", "-c"]
    # The authenticated preflight names the linter before make can turn a child 127 into exit 2.
    assert "command -v verilator" in command[2]
    assert re.search(r"BOOLEY_EDA_FAILURE token=[0-9a-f]{32}", command[2])
    assert "make -C .booley/.runtime/edalize/lint/lite/lint_demo_0/lite" in command[2]


_LEGACY_LINT_CORE_TEXT = """\
CAPI=2:
name: ::lint_demo:0
filesets:
  rtl:
    files: [rtl/top.sv]
    file_type: systemVerilogSource
targets:
  lite:
    default_tool: verilator
    tools:
      verilator: {mode: lint-only}
    filesets: [rtl]
    toplevel: top
"""


@pytest.mark.parametrize(
    ("core_text", "vc_options"),
    [
        pytest.param(_LINT_CORE_TEXT, ("--lint-only", "-Wall"), id="flow-options"),
        pytest.param(_LEGACY_LINT_CORE_TEXT, (), id="legacy-tools"),
    ],
)
def test_real_fusesoc_lint_setup(
    run_lint: _LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_text: str,
    vc_options: tuple[str, ...],
) -> None:
    """Real catalog-authorized setup emits a relocatable Verilator lint build."""
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    work_dir = tmp_path / "proj"
    (work_dir / "rtl").mkdir(parents=True)
    (work_dir / "rtl/top.sv").write_text("module top; endmodule\n", encoding="utf-8")
    (work_dir / "lint_demo.core").write_bytes(core_text.encode())
    _real_lint_setup(
        monkeypatch, [sys.executable, "-c", "from fusesoc.main import main; main()"], tmp_path
    )

    result, commands = run_lint(_process(), work_dir=work_dir, resolved=None)

    assert result.exit_code == EXIT_SUCCESS
    make_dir = work_dir / re.search(r"make -C (\S+)", commands[0][2]).group(1)
    assert (make_dir / "Makefile").is_file()
    vc = next(make_dir.glob("*.vc")).read_text(encoding="utf-8")
    assert all(option in vc for option in vc_options)
    # Relocatable: no absolute project/build paths baked into the .vc.
    assert str(work_dir) not in vc


def _dry_run_plan(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """The plan a ``--dry-run`` prints ahead of its one-line verdict."""
    out = capsys.readouterr().out
    plan: dict[str, Any] = json.JSONDecoder().raw_decode(out[out.index("{") :])[0]
    return plan


def _author_lint_core(
    root: Path,
    filename: str,
    vlnv: str,
    *,
    doctor_selftest: bool,
    target_name: str = "lint",
) -> None:
    root.joinpath(filename).write_bytes(
        (
            "CAPI=2:\n"
            f"name: {vlnv}\n"
            "targets:\n"
            f"  {target_name}:\n"
            "    flow: lint\n"
            "    flow_options:\n"
            "      tool: verible\n"
            f"      booley: {{doctor_selftest: {str(doctor_selftest).lower()}}}\n"
        ).encode()
    )


def test_public_lint_rejects_doctor_only_target_before_resolution(
    run_lint: _LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary Flow invocation cannot cross Doctor's private seam."""
    _author_lint_core(tmp_path, "doctor.core", "acme:ip:doctor:1.0", doctor_selftest=True)
    monkeypatch.delenv(selftest_overlay.INTERNAL_KIND_ENV, raising=False)
    monkeypatch.setattr(
        TargetCatalog, "build", classmethod(lambda _cls, root: _REAL_CATALOG_BUILD(root))
    )
    resolved_handles: list[TargetHandle] = []

    def resolved(handle: TargetHandle, _build_root: Path) -> object:
        resolved_handles.append(handle)
        return _stub_resolved("verible")

    result, commands = run_lint(_process(), target="lint", resolved=resolved)

    assert result.exit_code == EXIT_ERROR
    assert "Unknown target 'lint'" in result.outcome.report_text
    assert resolved_handles == []
    assert commands == []


def test_selected_doctor_target_keeps_its_core_and_parser(
    run_lint: _LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both cores name Target `lint`; Doctor's selection keeps its core and Verible parser."""
    _author_lint_core(tmp_path, "public.core", "acme:ip:public:1.0", doctor_selftest=False)
    _author_lint_core(tmp_path, "doctor.core", "acme:ip:doctor:1.0", doctor_selftest=True)

    def build_for_doctor(_cls: object, project_root: Path) -> TargetCatalog:
        monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
        try:
            return _REAL_CATALOG_BUILD(project_root)
        finally:
            monkeypatch.delenv(selftest_overlay.INTERNAL_KIND_ENV)

    monkeypatch.setattr(TargetCatalog, "build", classmethod(build_for_doctor))
    monkeypatch.setattr(
        fusesoc_registry, "setup_command_for_handle", _REAL_SETUP_COMMAND_FOR_HANDLE
    )

    preview, _ = run_lint(target="doctor#lint", args=("--dry-run",))
    setup_script = _dry_run_plan(capsys)["work_units"][0]["commands"][0]["argv"][2]
    assert preview.exit_code == EXIT_SUCCESS
    assert "--target lint acme:ip:doctor:1.0" in setup_script

    resolver_calls: list[tuple[str, Path]] = []

    def resolved(handle: TargetHandle, build_root: Path) -> object:
        resolver_calls.append((handle.vlnv, build_root))
        return _stub_resolved("verible")

    finding = "rtl/bad.sv:1:1: forbidden construct [module-filename]\n"
    result, _ = run_lint(_process(stdout=finding), target="doctor#lint", resolved=resolved)

    assert resolver_calls == [
        ("acme:ip:doctor:1.0", tmp_path / ".booley/.runtime/edalize/lint/doctor_lint")
    ]
    assert result.exit_code == EXIT_FAILURE
    assert result.outcome.detail["total_warnings"] == 1


@pytest.mark.parametrize(
    ("output", "target", "expected"),
    [
        pytest.param(
            SAMPLE_VERILATOR_OUTPUT,
            "lite",
            [
                ("UNUSEDSIGNAL", "rtl/mod_a.sv", 42, 5, "Signal is not used: 'foo'", "lite"),
                ("UNDRIVEN", "rtl/mod_a.sv", 100, 3, "Signal is not driven: 'bar'", "lite"),
                (
                    "WIDTH",
                    "rtl/mod_c.sv",
                    55,
                    10,
                    "Operator ASSIGN expects 32 bits on the LHS",
                    "lite",
                ),
            ],
            id="basic",
        ),
        pytest.param("", "lite", [], id="empty"),
        pytest.param("Compiling module...\nDone.\n", "lite", [], id="no-warnings"),
        pytest.param(
            "Compiling mod_b.sv\n"
            "%Warning-UNUSEDSIGNAL: rtl/mod_b.sv:88:7: Signal is not used: 'debug_flag'\n"
            "%Warning-UNUSEDSIGNAL: rtl/mod_a.sv:42:5: Signal is not used: 'foo'\n",
            "combo",
            [
                (
                    "UNUSEDSIGNAL",
                    "rtl/mod_b.sv",
                    88,
                    7,
                    "Signal is not used: 'debug_flag'",
                    "combo",
                ),
                ("UNUSEDSIGNAL", "rtl/mod_a.sv", 42, 5, "Signal is not used: 'foo'", "combo"),
            ],
            id="mixed-stderr",
        ),
    ],
)
def test_parse_warnings(
    output: str, target: str, expected: list[tuple[str, str, int, int, str, str]]
) -> None:
    warnings = parse_warnings(output, target)

    assert [(w.rule, w.file, w.line, w.col, w.message, w.target) for w in warnings] == expected


def test_dedup_same_warning_different_configs() -> None:
    w1 = LintWarning("UNUSEDSIGNAL", "mod_a.sv", 42, 5, "msg", "lite")
    w2 = LintWarning("UNUSEDSIGNAL", "mod_a.sv", 42, 5, "msg", "full")
    w3 = LintWarning("UNUSEDSIGNAL", "mod_a.sv", 42, 5, "msg", "combo")
    result = deduplicate_warnings(
        [w1, w2, w3],
        {"lite": "verilator", "full": "verilator", "combo": "verible"},
    )
    assert len(result) == 1
    assert result[0].target == "lite"  # first occurrence kept
    assert result[0].targets == ("combo", "full", "lite")
    assert result[0].eda_tools == (
        ("combo", "verible"),
        ("full", "verilator"),
        ("lite", "verilator"),
    )


def test_dedup_repeated_warning_from_one_target_lists_target_once() -> None:
    first = LintWarning("WIDTH", "mod_a.sv", 42, 5, "first", "lite")
    repeated = LintWarning("WIDTH", "mod_a.sv", 42, 9, "repeated", "lite")

    result = deduplicate_warnings([first, repeated], {"lite": "verilator"})

    assert len(result) == 1
    assert result[0].targets == ("lite",)
    assert result[0].eda_tools == (("lite", "verilator"),)
    assert result[0].col == 5
    assert result[0].message == "first"


def test_dedup_rejects_incomplete_eda_tool_provenance() -> None:
    warnings = [
        LintWarning("WIDTH", "mod_a.sv", 42, 5, "first", "lint_a"),
        LintWarning("WIDTH", "mod_a.sv", 42, 9, "second", "lint_b"),
    ]

    with pytest.raises(
        AssertionError,
        match=r"missing EDA-tool provenance for contributing Target.*lint_b",
    ):
        deduplicate_warnings(warnings, {"lint_a": "verilator"})


@pytest.mark.parametrize(
    ("warnings", "expected"),
    [
        pytest.param([], [], id="empty"),
        pytest.param(
            [
                LintWarning("A", "f.sv", 1, 1, "m", "lite"),
                LintWarning("A", "f.sv", 2, 1, "m", "lite"),
            ],
            [("A", "f.sv", 1, "lite"), ("A", "f.sv", 2, "lite")],
            id="other-line",
        ),
        pytest.param(
            [
                LintWarning("A", "f.sv", 1, 1, "m", "lite"),
                LintWarning("B", "f.sv", 1, 1, "m", "lite"),
            ],
            [("A", "f.sv", 1, "lite"), ("B", "f.sv", 1, "lite")],
            id="other-rule",
        ),
        pytest.param(
            [
                LintWarning("A", "f.sv", 1, 1, "m", "lite"),
                LintWarning("A", "g.sv", 1, 1, "m", "lite"),
            ],
            [("A", "f.sv", 1, "lite"), ("A", "g.sv", 1, "lite")],
            id="other-file",
        ),
        pytest.param(
            [
                LintWarning("A", "f.sv", 1, 1, "m", "lite"),
                LintWarning("B", "g.sv", 2, 1, "m", "full"),
                LintWarning("A", "f.sv", 1, 1, "m", "combo"),
            ],
            [("A", "f.sv", 1, "lite"), ("B", "g.sv", 2, "full")],
            id="first-of-each-group",
        ),
    ],
)
def test_dedup_keeps_first_warning_per_rule_file_and_line(
    warnings: list[LintWarning], expected: list[tuple[str, str, int, str]]
) -> None:
    result = deduplicate_warnings(warnings)

    assert [(w.rule, w.file, w.line, w.target) for w in result] == expected


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        pytest.param("mod_a", ["rtl/mod_a.sv"], id="substring"),
        pytest.param("mod_a,mod_c", ["rtl/mod_a.sv", "rtl/mod_c.sv"], id="several-paths"),
        pytest.param(
            "", ["rtl/mod_a.sv", "rtl/mod_c.sv", "rtl/design_top.sv"], id="empty-keeps-all"
        ),
        pytest.param("nonexistent", [], id="no-match"),
    ],
)
def test_filter_by_scope(scope: str, expected: list[str]) -> None:
    warnings = [
        LintWarning("A", "rtl/mod_a.sv", 1, 1, "m", "lite"),
        LintWarning("B", "rtl/mod_c.sv", 2, 1, "m", "lite"),
        LintWarning("C", "rtl/design_top.sv", 3, 1, "m", "lite"),
    ]

    assert [w.file for w in filter_by_scope(warnings, scope)] == expected


_BAD_CORE = (
    b"CAPI=2:\nname: acme:ip:bad:0\ntargets:\n"
    b"  lite:\n    flow: lint\n    flow_options: {tool: verilator}\n"
    b"    filesetz: [rtl]\n"
)
_UNPROVIDED_DEPENDENCY_CORE = (
    b"CAPI=2:\nname: acme:ip:top:0\nfilesets:\n  rtl:\n"
    b"    depend: ['>=::vlog_tb_utils:1.1']\n"
    b"targets:\n  lite:\n    flow: lint\n    flow_options: {tool: verilator}\n"
    b"    filesets: [rtl]\n"
)


@pytest.mark.parametrize(
    ("core", "fragments"),
    [
        pytest.param(_BAD_CORE, ("FuseSoC rejected", "bad.core", "filesetz"), id="rejected-core"),
        pytest.param(
            _UNPROVIDED_DEPENDENCY_CORE,
            (
                "\nno Project .core provides '>=::vlog_tb_utils:1.1': the Sandbox has no FuseSoC "
                "libraries and cannot fetch them; copy it into the repository as a .core, or "
                "drop the depend: (check first whether the testbench guards its use with an "
                "`ifdef` or defines the module itself)",
            ),
            id="unprovided-dependency",
        ),
        pytest.param(
            None, ("lite: ERROR: lint dry-run: Unknown target 'lite'",), id="unknown-target"
        ),
    ],
)
def test_dry_run_reports_planning_errors(
    run_lint: _LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    core: bytes | None,
    fragments: tuple[str, ...],
) -> None:
    """A Target FuseSoC cannot plan fails the dry run, naming the core and the reason."""
    if core is None:

        def unknown_target(_handle: TargetHandle, **_kwargs: object) -> list[str]:
            raise fusesoc_registry.TargetResolutionError("Unknown target 'lite'")

        monkeypatch.setattr(fusesoc_registry, "setup_command_for_handle", unknown_target)
    else:
        tmp_path.joinpath("bad.core").write_bytes(core)

    result, commands = run_lint(args=("--dry-run",))

    plan = _dry_run_plan(capsys)
    assert result.exit_code == EXIT_ERROR
    assert plan["work_units"] == []
    (error,) = plan["aggregate_errors"]
    assert all(fragment in error for fragment in fragments)
    assert commands == []


def test_dry_run_tolerates_a_typed_missing_toplevel(
    run_lint: _LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The type of the inspection error, not its wording, marks a legacy Target without top."""
    tmp_path.joinpath("lint.core").write_bytes(
        b"CAPI=2:\nname: ::lint_demo:0\ntargets:\n  lite:\n    flow: lint\n"
        b"    flow_options:\n      tool: verilator\n"
    )
    monkeypatch.setattr(
        TargetCatalog, "build", classmethod(lambda _cls, root: _REAL_CATALOG_BUILD(root))
    )

    def inspect(_catalog: TargetCatalog, _handle: TargetHandle) -> None:
        raise MissingTargetToplevelError("wording may change")

    monkeypatch.setattr(TargetCatalog, "inspect", inspect)

    result, _ = run_lint(args=("--dry-run",))

    plan = _dry_run_plan(capsys)
    assert result.exit_code == EXIT_SUCCESS
    assert plan["aggregate_errors"] == []
    assert plan["work_units"][0]["recipe"]["toplevel"] == ""


def test_dry_run_previews_fusesoc_setup_without_resolving(
    run_lint: _LintRun,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The preview comes from a cheap ``.core`` read; a resolver that fails proves no fusesoc run."""
    tmp_path.joinpath("lint.core").write_bytes(
        b"CAPI=2:\nname: ::lint_demo:0\ntargets:\n  lite:\n    flow: lint\n"
        b"    flow_options:\n      tool: verilator\n"
    )

    def resolved(_handle: TargetHandle, _build_root: Path) -> object:
        raise AssertionError("dry-run must not resolve (run fusesoc)")

    result, commands = run_lint(args=("--dry-run",), resolved=resolved)

    data = _dry_run_plan(capsys)
    assert result.exit_code == EXIT_SUCCESS
    assert "Dry run" in result.outcome.report_text
    assert data["flow"] == "lint"
    argv = data["work_units"][0]["commands"][0]["argv"]
    assert argv[:2] == ["sh", "-c"]
    script = argv[2]
    assert "run --build-root" in script and "--setup" in script
    assert "--target lite" in script
    assert "lint_demo" in script  # the resolved vlnv from the .core
    assert "make -C" in script
    assert commands == []


_WARNINGS_ONLY_STDERR = (
    "%Warning-IMPLICIT: rtl/ct_l2c_wb.v:251:25: Signal definition not "
    "found, creating implicitly: 'rfifo_full'\n"
    "%Error: Exiting due to 44 warning(s)\n"
)
_DESIGN_FAIL_STDERR = (
    "%Error: rtl/x.v:1:1: Can't find definition of variable: 'zzz'\n"
    "%Error: Exiting due to 1 error(s)\n"
)
_NON_BLOCKING_KNOB = "[flows.lint]\nwarnings_as_errors = false\n"


@pytest.mark.parametrize(
    ("process", "outcome"),
    [
        pytest.param(_process(2, stderr=_WARNINGS_ONLY_STDERR), "exit_zero", id="warnings-only"),
        pytest.param(
            _process(0, stderr=_DESIGN_FAIL_STDERR), "exit_nonzero", id="exit-zero-error"
        ),
    ],
)
def test_lint_record_outcome_follows_the_linter_error(
    run_lint: _LintRun, tmp_path: Path, process: SubprocessResult, outcome: str
) -> None:
    from booley.runtime import resource_history

    run_lint(process, measured=True)

    (record,) = resource_history.load_records(tmp_path)
    assert (record["outcome"], record["returncode"]) == (outcome, process.returncode)


@pytest.mark.parametrize(
    ("process", "knob", "exit_code", "report_fragment"),
    [
        pytest.param(_process(), False, EXIT_SUCCESS, "RESULT: PASS", id="pass"),
        pytest.param(
            _process(stdout=SAMPLE_VERILATOR_OUTPUT),
            False,
            EXIT_FAILURE,
            "RESULT: WARN (3 warnings)",
            id="finding",
        ),
        pytest.param(
            _process(2, stderr=_DESIGN_FAIL_STDERR),
            False,
            EXIT_FAILURE,
            "RESULT: FAIL — lite [verilator]: %Error: rtl/x.v:1:1: Can't find definition of variable: 'zzz'",
            id="design-fail",
        ),
        pytest.param(
            _process(2, stderr="make: verilator: No such file or directory\n"),
            False,
            EXIT_ERROR,
            "RESULT: ERROR — lite [verilator]: missing tool: verilator",
            id="missing-linter",
        ),
        pytest.param(
            _process(-1, "partial lint output", "partial timeout diagnostic", timed_out=True),
            False,
            EXIT_ERROR,
            "RESULT: ERROR — lite [verilator]: Timed out after",
            id="timeout",
        ),
        pytest.param(
            _process(2, stderr=_WARNINGS_ONLY_STDERR),
            False,
            EXIT_FAILURE,
            "RESULT: WARN (1 warnings)",
            id="warnings-only",
        ),
        pytest.param(
            _process(2, stderr=_WARNINGS_ONLY_STDERR),
            True,
            EXIT_SUCCESS,
            "RESULT: WARN (1 warnings) — non-blocking ([flows.lint].warnings_as_errors=false)",
            id="warnings-only-knob-off",
        ),
        pytest.param(
            _process(2, stderr=_DESIGN_FAIL_STDERR),
            True,
            EXIT_FAILURE,
            "RESULT: FAIL — lite [verilator]: %Error: rtl/x.v:1:1",
            id="design-fail-knob-off",
        ),
    ],
)
def test_linter_outcome_grades_the_run(
    run_lint: _LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process: SubprocessResult,
    knob: bool,
    exit_code: int,
    report_fragment: str,
) -> None:
    """Exit 2 means the linter reached no verdict; exit 1 means the RTL failed (F-29, QA-7)."""
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    if knob:
        (tmp_path / ".booley").mkdir()
        (tmp_path / ".booley/booley.toml").write_text(_NON_BLOCKING_KNOB, encoding="utf-8")

    result, _ = run_lint(process)

    assert result.exit_code == exit_code
    assert report_fragment in result.outcome.report_text
    run_log = (tmp_path / ".booley/.runtime/edalize/lint/lite/run.log").read_text(encoding="utf-8")
    assert process.stdout in run_log
    assert process.stderr in run_log
    # A hand run records no Criterion and writes no state file.
    assert not state_path.exists()
    assert list(tmp_path.rglob("acceptance")) == []


def test_a_single_eda_tool_failure_decides_a_mixed_run(run_lint: _LintRun) -> None:
    """An unusable linter makes the other Targets' verdicts untrustworthy."""
    design_fail = _process(2, stderr="%Error: f.sv:1:1: syntax error\n")
    missing_linter = _process(2, stderr="make: verilator: No such file or directory\n")

    result, commands = run_lint(design_fail, missing_linter, target="lite,lint_style")

    assert len(commands) == 2
    assert result.exit_code == EXIT_ERROR


@pytest.mark.parametrize("published", [True, False], ids=["published", "publish-failed"])
def test_hard_fail_cites_the_published_log(
    run_lint: _LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: bool,
) -> None:
    """The report cites the numbered run.log, and never a log that was not published."""
    real_publish = flow_artifacts.publish_bytes

    def publish_except_run_log(invocation_dir, parts, *args, **kwargs):
        # Fail only the per-Target run.log; the report still publishes.
        if parts[-1] == "run.log":
            raise OSError("disk full")
        return real_publish(invocation_dir, parts, *args, **kwargs)

    if not published:
        monkeypatch.setattr(flow_artifacts, "publish_bytes", publish_except_run_log)

    result, _ = run_lint(
        _process(2, stderr="%Error: rtl/x.v:1:1: rejected\n"),
        args=("--report-dir", str(tmp_path / "reports")),
    )

    text = result.outcome.report_text
    assert "%Error: rtl/x.v:1:1: rejected" in text
    assert ("reports/lint/1/artifacts/lite/run.log" in text) is published
    assert ("full log:" in text) is published


def test_previous_runs_log_is_erased_before_the_run(run_lint: _LintRun, tmp_path: Path) -> None:
    """F-26: lint's run.log is only written at the END of a run, so it is
    claimed (truncated to a run header) at the start — a tail during the
    run must never show the previous run's findings."""
    live_log = tmp_path / ".booley/.runtime/edalize/lint/lite/run.log"
    live_log.parent.mkdir(parents=True)
    live_log.write_text("%Warning: stale finding from an older run\n", encoding="utf-8")
    seen: list[str] = []

    def resolved(handle: TargetHandle, _build_root: Path) -> object:
        # Read the log back mid-run: by resolution time it must be claimed.
        seen.append(live_log.read_text(encoding="utf-8"))
        return _stub_resolved(build_root=tmp_path / handle.selector)

    run_lint(_process(), resolved=resolved)

    (mid_run,) = seen
    assert "stale finding" not in mid_run
    assert mid_run.startswith("[BOOLEY RUN_LOG] ")
    assert "flow=lint target=lite" in mid_run


# ---------------------------------------------------------------------------
# Full run (mocked subprocess)
# ---------------------------------------------------------------------------


class TestFullRun:
    @patch.object(LintFlow, "_execute")
    def test_late_preparation_error_prevents_every_linter_run(
        self,
        mock_exec,
        state_file: Path,
    ):
        flow = LintFlow()
        flow.parse_args(["--target", "lite,full"])
        with patch.object(
            LintFlow,
            "_prepare_lint_command",
            side_effect=[
                (["make", "-C", "lite"], _stub_resolved()),
                RuntimeError("broken full Target"),
            ],
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        assert "full: lint setup failed: broken full Target" in result.report_text
        mock_exec.assert_not_called()

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_clean_run(self, mock_cmd, mock_exec, state_file: Path, capsys):
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        result = flow._run()
        assert result.exit_code == 0
        captured = capsys.readouterr()
        assert "RESULT: PASS" in captured.out

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_warnings_exit_1(self, mock_cmd, mock_exec, state_file: Path, capsys):
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout=SAMPLE_VERILATOR_OUTPUT,
            stderr="",
            timed_out=False,
            duration_s=1.0,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        result = flow._run()
        assert result.exit_code == 1
        captured = capsys.readouterr()
        assert "RESULT: WARN" in captured.out

    def test_summary_points_at_report_on_warn(self):
        """WARN summary references lint_report.json so agents stay in the Flow."""
        warnings = [LintWarning("UNUSEDSIGNAL", "f.sv", 1, 5, "unused 'x'", "lite")]
        report_path = Path("/runtime/flow-reports/lint_report.json")
        exit_code, summary = LintFlow._build_summary(warnings, report_path)
        assert exit_code == EXIT_FAILURE
        assert str(report_path) in summary
        assert "rule/file:line/message" in summary

    def test_summary_no_pointer_without_report(self):
        """No report dir (Developer Agent session) вЂ” summary stays count-only."""
        warnings = [LintWarning("UNUSEDSIGNAL", "f.sv", 1, 5, "unused 'x'", "lite")]
        exit_code, summary = LintFlow._build_summary(warnings, None)
        assert exit_code == EXIT_FAILURE
        assert "lint_report.json" not in summary

    def test_summary_pass_has_no_pointer(self):
        """Clean run never references the report."""
        exit_code, summary = LintFlow._build_summary([], Path("x.json"))
        assert exit_code == EXIT_SUCCESS
        assert summary == "RESULT: PASS"

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_multi_config_dedup(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """Same warning from two configs counts once."""
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout="%Warning-UNUSEDSIGNAL: f.sv:42:5: unused 'x'\n",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite,full",
                "--report-dir",
                str(tmp_path / "reports"),
            ]
        )
        result = flow._run()
        captured = capsys.readouterr()
        assert "1 unique in-scope warning" in captured.out
        assert result.detail.get("total_warnings") == 1
        report = json.loads(
            (tmp_path / "reports" / "lint_report.json").read_text(encoding="utf-8")
        )
        assert report["total_warnings"] == 1
        assert len(report["warnings"]) == 1
        assert report["warnings"][0]["targets"] == ["full", "lite"]

    @patch.object(LintFlow, "_execute")
    def test_mixed_eda_tool_provenance_survives_full_run(
        self,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
    ):
        mock_exec.side_effect = [
            SubprocessResult(
                returncode=0,
                stdout="%Warning-SHARED: rtl/top.sv:4:2: message\n",
                stderr="",
                timed_out=False,
                duration_s=0.5,
            ),
            SubprocessResult(
                returncode=0,
                stdout="rtl/top.sv:4:9: other [SHARED]\n",
                stderr="",
                timed_out=False,
                duration_s=0.5,
            ),
        ]
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lint_structural,lint_style",
                "--report-dir",
                str(tmp_path / "reports"),
            ]
        )

        with patch.object(
            LintFlow,
            "_prepare_lint_command",
            side_effect=[
                (["verilator", "--lint-only"], _stub_resolved("Verilator")),
                (["verible-verilog-lint"], _stub_resolved("verible")),
            ],
        ):
            result = flow._run()

        report = json.loads(
            (tmp_path / "reports" / "lint_report.json").read_text(encoding="utf-8")
        )
        assert result.detail["total_warnings"] == 1
        assert report["warnings"][0]["targets"] == ["lint_structural", "lint_style"]
        assert report["warnings"][0]["eda_tools"] == {
            "lint_structural": "verilator",
            "lint_style": "verible",
        }

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_scope_filtering_in_run(self, mock_cmd, mock_exec, state_file: Path, capsys):
        """--scope filters warnings to matching files."""
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout=(
                "%Warning-UNUSEDSIGNAL: rtl/mod_a.sv:42:5: unused\n"
                "%Warning-WIDTH: rtl/mod_c.sv:10:1: width mismatch\n"
            ),
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--scope",
                "mod_a",
            ]
        )
        result = flow._run()
        captured = capsys.readouterr()
        # Only 1 warning in scope; a scope matched through a warning path is accepted
        assert "1 unique in-scope warning" in captured.out
        assert result.exit_code == EXIT_FAILURE

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(
            ["verilator", "--lint-only"],
            _stub_resolved(files=("rtl/ticket.sv", "rtl/baseline.sv")),
        ),
    )
    def test_out_of_scope_warnings_pass_a_scoped_run(
        self, mock_cmd, mock_exec, state_file: Path, capsys
    ):
        """Out-of-scope baseline warnings must not keep a scoped ticket dirty."""
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout="%Warning-WIDTH: rtl/baseline.sv:10:1: width mismatch\n",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(["--target", "lite", "--scope", "rtl/ticket.sv"])

        result = flow._run()

        assert result.exit_code == EXIT_SUCCESS
        assert "0 unique in-scope warnings" in capsys.readouterr().out


class TestUnmatchedScope:
    """A --scope entry that matches no linted file is an ERROR, not a PASS (#2801)."""

    _STDOUT: ClassVar[str] = (
        "%Warning-UNUSEDSIGNAL: rtl/mod_a.sv:42:5: unused\n"
        "%Warning-WIDTH: rtl/mod_a.sv:50:1: width mismatch\n"
    )

    @pytest.fixture(autouse=True)
    def _outside_sandbox_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Let ``main`` run on the host, as the CLI and MCP paths do in a Sandbox."""
        monkeypatch.setattr(LintFlow, "_pre_state_gate", lambda _self: None)

    @staticmethod
    def _lint_result() -> SubprocessResult:
        return SubprocessResult(
            returncode=0,
            stdout=TestUnmatchedScope._STDOUT,
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )

    @staticmethod
    def _resolved(*files: str) -> object:
        return _stub_resolved(files=files or ("rtl/mod_a.sv", "rtl/mod_b.sv"))

    def test_unmatched_scope_is_error_cli(self, state_file: Path, capsys):
        with (
            patch.object(
                LintFlow,
                "_prepare_lint_command",
                return_value=(["verilator", "--lint-only"], self._resolved()),
            ),
            patch.object(LintFlow, "_execute", return_value=self._lint_result()),
        ):
            code = LintFlow().main(["--target", "lite", "--scope", "nonexistent/"])

        out = capsys.readouterr().out
        assert code == EXIT_ERROR
        assert "RESULT: ERROR" in out
        assert "RESULT: PASS" not in out
        assert "'nonexistent/'" in out
        assert "rtl/mod_b.sv" in out  # example from the file list only, no warning there

    def test_mixed_scope_is_error(self, state_file: Path, capsys):
        with (
            patch.object(
                LintFlow,
                "_prepare_lint_command",
                return_value=(["verilator", "--lint-only"], self._resolved()),
            ),
            patch.object(LintFlow, "_execute", return_value=self._lint_result()),
        ):
            code = LintFlow().main(["--target", "lite", "--scope", "rtl/mod_a.sv,nonexistent/"])

        out = capsys.readouterr().out
        assert code == EXIT_ERROR
        assert "'nonexistent/'" in out
        assert "'rtl/mod_a.sv'" not in out

    def test_unmatched_scope_is_error_mcp(self, state_file: Path, tmp_path: Path, capsys):
        from booley.mcp.server import _params_to_argv

        argv = _params_to_argv({"target": "lite", "scope": "nonexistent/"})
        assert "--scope" in argv
        with (
            patch.object(
                LintFlow,
                "_prepare_lint_command",
                return_value=(["verilator", "--lint-only"], self._resolved()),
            ),
            patch.object(LintFlow, "_execute", return_value=self._lint_result()),
        ):
            code = LintFlow().main(argv)
            capsys.readouterr()
            outcome = LintFlow().execute(
                LintRequest(
                    target="lite",
                    scope="nonexistent/",
                    work_dir=tmp_path,
                    report_dir=tmp_path / "reports",
                )
            )

        assert code == EXIT_ERROR
        assert outcome.exit_code == EXIT_ERROR
        assert "nonexistent/" in outcome.outcome.report_text

    def test_unmatched_scope_still_reports_design_failure(self, state_file: Path, capsys):
        """A scope typo must not hide a design FAIL: the errored Target is reported."""
        failing = SubprocessResult(
            returncode=1,
            stdout="%Error: rtl/mod_a.sv:3:1: syntax error\n",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        with (
            patch.object(
                LintFlow,
                "_prepare_lint_command",
                return_value=(["verilator", "--lint-only"], self._resolved()),
            ),
            patch.object(LintFlow, "_execute", return_value=failing),
        ):
            code = LintFlow().main(["--target", "lite", "--scope", "nonexistent/"])

        assert code == EXIT_ERROR
        assert (
            "lite [verilator]: %Error: rtl/mod_a.sv:3:1: syntax error" in capsys.readouterr().out
        )

    def test_unmatched_scope_entries(self):
        from booley.flows.lint.flow import unmatched_scope_entries

        paths = ["rtl/mod_a.sv", "rtl/mod_b.sv"]
        assert unmatched_scope_entries("rtl/mod_a.sv, nope/ ,,mod_b", paths) == ["nope/"]
        assert unmatched_scope_entries("x/,y/", paths) == ["x/", "y/"]
        assert unmatched_scope_entries("", paths) == []
        assert unmatched_scope_entries("mod_a", []) == ["mod_a"]

    def test_build_dir_path_fragment_is_unmatched(self, state_file: Path, capsys):
        """Build-root path fragments are not linted file names: no vacuous pass."""
        with (
            patch.object(
                LintFlow,
                "_prepare_lint_command",
                return_value=(
                    ["verilator", "--lint-only"],
                    _stub_resolved(
                        files=("src/demo_core_0/rtl/counter.sv",),
                        build_root=Path(".booley/.runtime/edalize/lint/lite"),
                    ),
                ),
            ),
            patch.object(
                LintFlow,
                "_execute",
                return_value=SubprocessResult(
                    returncode=0, stdout="", stderr="", timed_out=False, duration_s=0.5
                ),
            ),
        ):
            code = LintFlow().main(["--target", "lite", "--scope", "edalize/lint"])

        assert code == EXIT_ERROR
        assert "'edalize/lint'" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Structured report
# ---------------------------------------------------------------------------


class TestStructuredReport:
    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_consecutive_runs_preserve_first_cited_evidence(
        self, mock_cmd, mock_exec, state_file: Path, tmp_path: Path
    ) -> None:
        report_dir = tmp_path / "reports"
        mock_exec.side_effect = [
            SubprocessResult(
                returncode=0,
                stdout="%Warning-WIDTH: first.sv:1:1: first run\n",
                stderr="",
                timed_out=False,
                duration_s=1.0,
            ),
            SubprocessResult(
                returncode=0,
                stdout="%Warning-WIDTH: second.sv:2:2: second run\n",
                stderr="",
                timed_out=False,
                duration_s=1.0,
            ),
        ]

        def run() -> object:
            flow = LintFlow()
            flow.parse_args(["--target", "lite", "--report-dir", str(report_dir)])
            return flow._run()

        first = run()
        first_report = tmp_path / first.detail["artifacts"]["report"]
        first_log = tmp_path / first.detail["artifacts"]["log_lite"]
        first_report_bytes = first_report.read_bytes()
        first_log_bytes = first_log.read_bytes()
        first_payload = json.loads(first_report_bytes)

        assert first_report.relative_to(report_dir).parts[:2] == ("lint", "1")
        assert first_log.relative_to(report_dir).parts[:2] == ("lint", "1")
        assert str(first_report) in first.report_text
        assert first_payload["artifacts"]["report"] == first.detail["artifacts"]["report"]
        assert first_payload["target_results"][0]["log"] == first.detail["artifacts"]["log_lite"]

        second = run()
        second_report = tmp_path / second.detail["artifacts"]["report"]
        second_log = tmp_path / second.detail["artifacts"]["log_lite"]

        assert second_report.relative_to(report_dir).parts[:2] == ("lint", "2")
        assert second_log.relative_to(report_dir).parts[:2] == ("lint", "2")
        assert first_report.read_bytes() == first_report_bytes
        assert first_log.read_bytes() == first_log_bytes
        assert json.loads((report_dir / "lint_report.json").read_bytes()) == json.loads(
            second_report.read_bytes()
        )
        assert (report_dir / "lint_report.json").stat().st_mode & 0o004

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_report_written(self, mock_cmd, mock_exec, state_file: Path, tmp_path: Path):
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout="%Warning-UNUSEDSIGNAL: f.sv:1:1: unused\n",
            stderr="",
            timed_out=False,
            duration_s=1.0,
        )
        report_dir = tmp_path / "reports"
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--report-dir",
                str(report_dir),
            ]
        )
        flow._run()

        report_path = report_dir / "lint_report.json"
        assert report_path.exists()
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["flow"] == "lint"
        assert data["targets"] == ["lite"]
        assert data["total_warnings"] == 1
        assert data["passed"] is False
        assert len(data["warnings"]) == 1
        assert data["warnings"][0]["rule"] == "UNUSEDSIGNAL"
        assert data["warnings"][0]["targets"] == ["lite"]
        # The parsed warnings carry normalized diagnostic and Target provenance;
        # the raw output (banner, include resolution, surrounding lines) only
        # exists in run.log, so the report has to name it.
        assert data["target_results"][0]["log"].endswith("run.log")
        assert data["artifacts"]["report"].endswith("lint_report.json")
        assert data["artifacts"]["log_lite"].endswith("run.log")

    def test_mixed_eda_tool_warning_row_maps_targets_to_eda_tools(self, tmp_path: Path):
        warnings = [
            LintWarning("SHARED", "rtl/top.sv", 4, 2, "message", "lint_structural"),
            LintWarning("SHARED", "rtl/top.sv", 4, 9, "other", "lint_style"),
        ]
        eda_tools = {"lint_structural": "verilator", "lint_style": "verible"}
        grouped = deduplicate_warnings(warnings, eda_tools)
        flow = LintFlow()
        report_dir = tmp_path / "reports"
        flow.parse_args(
            ["--target", "lint_structural,lint_style", "--report-dir", str(report_dir)]
        )
        flow._lint_invocation_dir = flow.reserve_invocation_dir()

        flow._write_lint_report(
            ["lint_structural", "lint_style"],
            grouped,
            0.1,
            target_results=[
                LintConfigResult(target="lint_structural", eda_tool="verilator"),
                LintConfigResult(target="lint_style", eda_tool="verible"),
            ],
        )

        report = json.loads((report_dir / "lint_report.json").read_text(encoding="utf-8"))
        assert report["warnings"] == [
            {
                "rule": "SHARED",
                "file": "rtl/top.sv",
                "line": 4,
                "message": "message",
                "targets": ["lint_structural", "lint_style"],
                "eda_tools": {
                    "lint_structural": "verilator",
                    "lint_style": "verible",
                },
            }
        ]

    def test_same_eda_tool_warning_row_omits_target_eda_tool_map(self, tmp_path: Path):
        warnings = [
            LintWarning("SHARED", "rtl/top.sv", 4, 2, "message", "lint_a"),
            LintWarning("SHARED", "rtl/top.sv", 4, 9, "other", "lint_b"),
        ]
        grouped = deduplicate_warnings(
            warnings,
            {"lint_a": "verilator", "lint_b": "verilator"},
        )
        flow = LintFlow()
        report_dir = tmp_path / "reports"
        flow.parse_args(["--target", "lint_a,lint_b", "--report-dir", str(report_dir)])
        flow._lint_invocation_dir = flow.reserve_invocation_dir()

        flow._write_lint_report(["lint_a", "lint_b"], grouped, 0.1)

        report = json.loads((report_dir / "lint_report.json").read_text(encoding="utf-8"))
        assert report["warnings"][0]["targets"] == ["lint_a", "lint_b"]
        assert "eda_tools" not in report["warnings"][0]

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_report_clean(self, mock_cmd, mock_exec, state_file: Path, tmp_path: Path):
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        report_dir = tmp_path / "reports"
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite,full",
                "--report-dir",
                str(report_dir),
            ]
        )
        flow._run()

        data = json.loads((report_dir / "lint_report.json").read_text(encoding="utf-8"))
        assert data["passed"] is True
        assert data["total_warnings"] == 0
        assert data["targets"] == ["lite", "full"]


# ---------------------------------------------------------------------------
# Config discovery
# ---------------------------------------------------------------------------


class TestConfigDiscovery:
    def test_split_comma_separated(self, state_file: Path):
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite,full,combo",
            ]
        )
        assert tuple(target.selector for target in flow._get_targets()) == (
            "lite",
            "full",
            "combo",
        )

    def test_single_config(self, state_file: Path):
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        assert tuple(target.selector for target in flow._get_targets()) == ("lite",)

    def test_empty_target_returns_nothing(self, state_file: Path):
        """An explicit empty target never sweeps every declared Target."""
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "",
            ]
        )
        assert flow._get_targets() == ()

    def test_omitted_target_rejects_empty_catalog_after_parsing(
        self, state_file: Path, monkeypatch
    ):
        flow = LintFlow()
        assert flow.parse_args([]).target == ""
        monkeypatch.setattr(TargetCatalog, "build", _REAL_CATALOG_BUILD)
        monkeypatch.setattr(flow, "_pre_state_gate", lambda: None)
        result = flow.execute(
            flow.request_type(
                target="", work_dir=state_file.parent, report_dir=state_file.parent / "reports"
            )
        )
        assert result.exit_code == EXIT_ERROR
        assert "Eligible Targets: (none)" in result.outcome.report_text

    @pytest.mark.parametrize("with_state_file", [False, True], ids=["no-state-file", "state-file"])
    def test_run_refuses_an_explicit_empty_target(self, tmp_path, request, with_state_file):
        if with_state_file:
            request.getfixturevalue("state_file")
        flow = LintFlow()
        flow.parse_args(["--target", "", "--work-dir", str(tmp_path)])
        result = flow._run()
        assert result.exit_code == EXIT_ERROR
        assert "--target is required" in result.report_text


# ---------------------------------------------------------------------------
# Verible Targets (ADR 0033) — EDA-tool-keyed parsing + verdict semantics
# ---------------------------------------------------------------------------

SAMPLE_VERIBLE_OUTPUT = """\
rtl/top.sv:4:11: Interface names must use lower_snake_case naming convention. [interface-name-style]
rtl/top.sv:10:1-5: Remove trailing spaces. [no-trailing-spaces]
rtl/other.sv:2:3: Explicitly define a storage type for every parameter. [explicit-parameter-storage-type]
"""

SAMPLE_VERIBLE_PARSE_ERROR = """\
rtl/top.sv:3:1: syntax error at token "endmodule"
"""


class TestLintFailureClassification:
    def test_verilator_missing_is_infrastructure_error(self) -> None:
        from booley.flows.lint.flow import LintConfigResult, _classify_lint_failure

        result = LintConfigResult(target="lint_default", returncode=2)
        _classify_lint_failure(
            result,
            "verilator",
            "/bin/sh: 1: verilator: not found\nmake: *** [lint] Error 127",
        )

        assert result.error_is_eda_tool_failure is True
        assert "missing tool: verilator" in result.error
        assert "Sandbox Image" in result.error
        assert "booley doctor" in result.error


# Two Verible lint Targets so cross-target dedup is exercised on the Verible
# parser path; the cheap .core read is what routes parsing to it.
_VERIBLE_CORE_TEXT = """\
CAPI=2:
name: ::style_demo:0
filesets:
  rtl:
    files:
      - rtl/top.sv: {file_type: systemVerilogSource}
targets:
  default:
    filesets: [rtl]
  lint_style:
    flow: lint
    flow_options:
      tool: verible
    filesets: [rtl]
    toplevel: top
  lint_style_alt:
    flow: lint
    flow_options:
      tool: verible
    filesets: [rtl]
    toplevel: top
"""


class TestVeribleParsing:
    def test_parse_basic_findings(self):
        warnings = parse_verible_warnings(SAMPLE_VERIBLE_OUTPUT, "lint_style")
        assert len(warnings) == 3
        assert warnings[0].rule == "interface-name-style"
        assert warnings[0].file == "rtl/top.sv"
        assert warnings[0].line == 4
        assert warnings[0].col == 11
        assert warnings[0].target == "lint_style"

    def test_parse_column_range_keeps_leading_col(self):
        warnings = parse_verible_warnings(SAMPLE_VERIBLE_OUTPUT, "lint_style")
        ranged = [w for w in warnings if w.rule == "no-trailing-spaces"]
        assert len(ranged) == 1
        assert ranged[0].line == 10
        assert ranged[0].col == 1

    def test_parse_error_lines_are_not_findings(self):
        """A syntax-error line has no [rule] — it must not count as a warning
        (it is the rc!=0 / EDA-tool-error signal, QA-7)."""
        assert parse_verible_warnings(SAMPLE_VERIBLE_PARSE_ERROR, "lint_style") == []


class TestVeribleTargets:
    """Full-run semantics for Targets whose flow_options.eda_tool is verible."""

    def _flow(self, tmp_path: Path, targets: str, extra: list[str] | None = None) -> LintFlow:
        (tmp_path / "style_demo.core").write_text(_VERIBLE_CORE_TEXT, encoding="utf-8")
        flow = LintFlow()
        flow.parse_args(
            ["--work-dir", str(tmp_path), "--target", targets, *(extra or [])],
        )
        return flow

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_findings_with_rc0_are_warn_not_error(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """--parse_fatal without --lint_fatal: findings arrive with rc 0 and
        must score WARN (ADR 0033 decision 5)."""
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout=SAMPLE_VERIBLE_OUTPUT,
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = self._flow(tmp_path, "lint_style")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        assert "RESULT: WARN" in capsys.readouterr().out

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_clean_run_is_pass(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = self._flow(tmp_path, "lint_style")
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert "RESULT: PASS" in capsys.readouterr().out

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_parse_error_is_error_never_pass(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """The QA-7 trap: a parse failure yields zero findings; without the
        rc!=0 branch it would score as a clean PASS.

        The linter ran and rejected the source, so this is a design FAIL, not
        a Flow ERROR — the same grading `elaborate` gives the identical source
        (F-29). What must never happen is a PASS."""
        mock_exec.return_value = SubprocessResult(
            returncode=1,
            stdout="",
            stderr=SAMPLE_VERIBLE_PARSE_ERROR,
            timed_out=False,
            duration_s=0.5,
        )
        flow = self._flow(tmp_path, "lint_style")
        result = flow._run()
        assert result.exit_code == EXIT_FAILURE
        out = capsys.readouterr().out
        assert "RESULT: FAIL" in out
        assert "RESULT: PASS" not in out
        assert "syntax error" in out

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_stale_image_names_rebuild_fix(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """An image predating the Verible binary must name cause + fix
        (ADR 0033 decision 8), not a generic spawn failure."""
        mock_exec.return_value = SubprocessResult(
            returncode=127,
            stdout="",
            stderr="make: verible-verilog-lint: No such file or directory\n",
            timed_out=False,
            duration_s=0.1,
        )
        flow = self._flow(tmp_path, "lint_style")
        result = flow._run()
        assert result.exit_code == EXIT_ERROR
        out = capsys.readouterr().out
        assert "rebuild the sandbox image" in out.lower()
        assert "booley doctor" in out

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_multi_target_cross_dedup(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """The (rule, file, line) dedup key works unchanged for Verible rules."""
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout="rtl/top.sv:4:11: Interface names. [interface-name-style]\n",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = self._flow(tmp_path, "lint_style,lint_style_alt")
        result = flow._run()
        assert "1 unique in-scope warning" in capsys.readouterr().out
        assert result.detail.get("total_warnings") == 1

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_report_carries_verible_rule_names(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
    ):
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout=SAMPLE_VERIBLE_OUTPUT,
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        report_dir = tmp_path / "reports"
        flow = self._flow(tmp_path, "lint_style", ["--report-dir", str(report_dir)])
        flow._run()
        data = json.loads(
            (report_dir / "lint_report.json").read_text(encoding="utf-8"),
        )
        assert data["passed"] is False
        assert data["total_warnings"] == 3
        rules = {w["rule"] for w in data["warnings"]}
        assert "interface-name-style" in rules

    def test_dry_run_shape_matches_verilator_path(
        self,
        tmp_path: Path,
        state_file: Path,
        capsys,
    ):
        """A3: a Verible Target's --dry-run shows the same
        ``fusesoc run --setup && make`` preview shape, never resolving."""
        from booley.fusesoc import fusesoc_registry

        (tmp_path / "style_demo.core").write_text(_VERIBLE_CORE_TEXT, encoding="utf-8")
        flow = LintFlow()
        flow.parse_args(
            ["--work-dir", str(tmp_path), "--target", "lint_style", "--dry-run"],
        )
        with patch.object(
            fusesoc_registry,
            "resolve_target_handle",
            side_effect=AssertionError("dry-run must not resolve (run fusesoc)"),
        ):
            result = flow._run()
        assert result.exit_code == 0
        data = _dry_run_plan(capsys)
        cmd = data["work_units"][0]["commands"][0]["argv"]
        assert cmd[:2] == ["sh", "-c"]
        script = cmd[2]
        assert "run --build-root" in script and "--setup" in script
        assert "--target lint_style" in script
        assert "style_demo" in script
        assert "make -C" in script


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_timeout_conversion(self, state_file: Path):
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
                "--timeout-ms",
                "60000",
            ]
        )
        assert flow._get_timeout() == 60

    def test_default_timeout(self, state_file: Path):
        flow = LintFlow()
        flow.parse_args(
            [
                "--target",
                "lite",
            ]
        )
        assert flow._get_timeout() == 120


# ---------------------------------------------------------------------------
# Observability: EDA tool identity, file coverage, flow check, per-run reports
# ---------------------------------------------------------------------------


class TestLintObservability:
    """The lint output must say WHAT ran and what it covered.

    The linter comes from the resolved Target, so without these signals a run
    can silently use the wrong EDA tool, lint a fileset that excludes the design's
    own toplevel, or clobber the previous run's report.
    """

    _PROC_CLEAN: ClassVar[dict] = {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "duration_s": 0.5,
    }

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["make", "-C", "x"], _stub_resolved("verible")),
    )
    def test_console_and_report_name_the_linter(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        mock_exec.return_value = SubprocessResult(**self._PROC_CLEAN)
        report_dir = tmp_path / "reports"
        flow = LintFlow()
        flow.parse_args(["--target", "lite", "--report-dir", str(report_dir)])
        result = flow._run()
        out = capsys.readouterr().out
        assert "[verible]" in out
        assert result.detail["eda_tools"] == {"lite": "verible"}
        assert "linter: verible" in result.display_lines
        data = json.loads((report_dir / "lint_report.json").read_text(encoding="utf-8"))
        assert data["eda_tools"] == {"lite": "verible"}
        assert data["target_results"][0]["eda_tool"] == "verible"

    @patch.object(LintFlow, "_execute")
    def test_toplevel_excluded_from_fileset_hard_fails(
        self,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        """A Target whose fileset excludes its own toplevel hard-fails.

        The run would lint nothing real and pass vacuously (a false green on
        the ``lint_clean`` gate), so this is an ERROR — no verdict about the
        design was reached — and the lint make never runs. Upgraded from the
        earlier loud-WARN, matching the ADR 0026 doctor hard-fail spirit.
        """
        from booley.fusesoc import fusesoc_registry
        from booley.fusesoc.fusesoc_registry import ResolvedFile

        (tmp_path / "other.sv").write_text("module other; endmodule\n", encoding="utf-8")
        resolved = fusesoc_registry.ResolvedTarget(
            name="style",
            vlnv="::demo:0",
            toplevel="design_top",  # declared by NO linted source
            eda_tool="verible",
            configured_eda_tool="verible",
            files=(ResolvedFile(name="other.sv", file_type="systemVerilogSource"),),
            parameters={},
            build_root=tmp_path,
            edam_path=tmp_path / "demo.eda.yml",
        )
        mock_exec.return_value = SubprocessResult(**self._PROC_CLEAN)
        flow = LintFlow()
        flow.parse_args(["--target", "lite"])
        with patch.object(
            LintFlow,
            "_prepare_lint_command",
            return_value=(["make", "-C", "x"], resolved),
        ):
            result = flow._run()
        out = capsys.readouterr().out
        assert result.exit_code == EXIT_ERROR
        assert "toplevel 'design_top' is not declared by any fileset file" in out
        assert "vacuous" in out
        # The lint make never ran — its verdict would be untrustworthy.
        mock_exec.assert_not_called()

    @patch.object(LintFlow, "_execute")
    def test_toplevel_in_fileset_stays_silent(
        self,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        capsys,
    ):
        from booley.fusesoc import fusesoc_registry
        from booley.fusesoc.fusesoc_registry import ResolvedFile

        (tmp_path / "top.sv").write_text("module design_top; endmodule\n", encoding="utf-8")
        resolved = fusesoc_registry.ResolvedTarget(
            name="style",
            vlnv="::demo:0",
            toplevel="design_top",
            eda_tool="verilator",
            configured_eda_tool="verilator",
            files=(ResolvedFile(name="top.sv", file_type="systemVerilogSource"),),
            parameters={},
            build_root=tmp_path,
            edam_path=tmp_path / "demo.eda.yml",
        )
        mock_exec.return_value = SubprocessResult(**self._PROC_CLEAN)
        flow = LintFlow()
        flow.parse_args(["--target", "lite"])
        with patch.object(
            LintFlow,
            "_prepare_lint_command",
            return_value=(["make", "-C", "x"], resolved),
        ):
            flow._run()
        out = capsys.readouterr().out
        assert "toplevel" not in out
        assert "1 files" in out  # coverage count still printed

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow, "_prepare_lint_command", return_value=(["make", "-C", "x"], _stub_resolved())
    )
    def test_non_lint_flow_target_is_rejected_before_setup(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
    ):
        """An explicit sim Target passed to lint fails before setup."""
        (tmp_path / "sim_demo.core").write_text(
            "CAPI=2:\n"
            "name: ::sim_demo:0\n"
            "filesets:\n"
            "  rtl:\n"
            "    files:\n"
            "      - rtl/top.sv: {file_type: systemVerilogSource}\n"
            "targets:\n"
            "  default:\n"
            "    filesets: [rtl]\n"
            "  smoke_sim:\n"
            "    flow: sim\n"
            "    flow_options:\n"
            "      tool: verilator\n"
            "    filesets: [rtl]\n"
            "    toplevel: top\n",
            encoding="utf-8",
        )
        mock_exec.return_value = SubprocessResult(**self._PROC_CLEAN)
        flow = LintFlow()
        flow.parse_args(["--work-dir", str(tmp_path), "--target", "smoke_sim"])
        with pytest.raises(IncompatibleTargetError, match="cannot be driven"):
            flow._run()
        mock_cmd.assert_not_called()
        mock_exec.assert_not_called()

    def test_summary_warnings_not_errors_exits_zero(self):
        """[flows.lint].warnings_as_errors=false: WARN text, exit 0."""
        warnings = [LintWarning("UNUSEDSIGNAL", "f.sv", 1, 5, "unused 'x'", "lite")]
        exit_code, summary = LintFlow._build_summary(warnings, None, warnings_as_errors=False)
        assert exit_code == EXIT_SUCCESS
        assert "RESULT: WARN" in summary
        assert "non-blocking" in summary

    @patch.object(LintFlow, "_execute")
    @patch.object(
        LintFlow,
        "_prepare_lint_command",
        return_value=(["verilator", "--lint-only"], _stub_resolved()),
    )
    def test_warnings_as_errors_false_in_run(
        self,
        mock_cmd,
        mock_exec,
        state_file: Path,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ):
        """End-to-end: the knob turns a warnings-only run into exit 0, while
        the run still reports the warning."""
        from booley.flows.lint import flow as lint_mod

        monkeypatch.setattr(lint_mod, "_lint_warnings_as_errors", lambda _wd: False)
        mock_exec.return_value = SubprocessResult(
            returncode=0,
            stdout="%Warning-UNUSEDSIGNAL: f.sv:1:1: unused\n",
            stderr="",
            timed_out=False,
            duration_s=0.5,
        )
        flow = LintFlow()
        flow.parse_args(["--target", "lite"])
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert "RESULT: WARN" in capsys.readouterr().out


@pytest.mark.parametrize(
    "family,executable", [("verilator", "verilator_bin"), ("verible", "verible-verilog-lint")]
)
@pytest.mark.parametrize("rc", [0, 127])
def test_lint_owned_loader_is_infrastructure(family, executable, rc):
    from booley.flows.lint.flow import LintConfigResult, _classify_lint_failure

    result = LintConfigResult(target="lint_default", returncode=rc)
    _classify_lint_failure(
        result, family, f"{executable}: error while loading shared libraries: libx.so: missing"
    )
    assert result.error_is_eda_tool_failure
    assert "libx.so" in result.error


def test_lint_silent_success_remains_clean():
    from booley.flows.lint.flow import LintConfigResult, _classify_lint_failure

    result = LintConfigResult(target="lint_default", returncode=0)
    _classify_lint_failure(result, "verilator", "")
    assert not result.error


def test_lint_rc0_design_error_remains_design():
    from booley.flows.lint.flow import LintConfigResult, _classify_lint_failure

    result = LintConfigResult(target="lint_default", returncode=0)
    _classify_lint_failure(result, "verilator", "%Error: dut.sv:7: syntax error")
    assert "syntax error" in result.error
    assert not result.error_is_eda_tool_failure


@pytest.mark.parametrize("family", ["verilator", "verible"])
def test_fake_rc0_linter_is_error_through_execution_boundary(tmp_path, family):
    from types import SimpleNamespace

    from booley.flows.lint.flow import _errored_verdict
    from booley.flows.source_paths import ProjectSourcePaths

    executable = "verilator" if family == "verilator" else "verible-verilog-lint"
    fake = tmp_path / f"{executable}.py"
    diagnostic = f"{executable}: error while loading shared libraries: libx.so: missing"
    fake.write_text(f"import sys\nprint({diagnostic!r}, file=sys.stderr)\n")
    flow = LintFlow()
    flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
    prepared = SimpleNamespace(
        result=LintConfigResult(target="lite"),
        command=[sys.executable, str(fake)],
        resolved=SimpleNamespace(configured_eda_tool=family, files=(), build_root=tmp_path),
        attempt_token="",
        project_paths=ProjectSourcePaths(),
    )
    result = flow._run_lint_target(_target_handle("lite"), prepared=prepared)
    exit_code, text = _errored_verdict([result])
    assert exit_code == EXIT_ERROR
    assert "RESULT: ERROR" in text
    assert "libx.so" in result.error


def _author_verible_alias_core(root: Path, spelling: str) -> None:
    (root / "rtl").mkdir()
    (root / "lint").mkdir()
    (root / "rtl/top.sv").write_text("module top; endmodule\n", encoding="utf-8")
    for filename in ("rules.cfg", "waivers.txt", "extra.txt"):
        (root / "lint" / filename).write_text("", encoding="utf-8")
    (root / "style.core").write_text(
        "CAPI=2:\nname: acme:ip:style:1.0\nfilesets:\n"
        "  rtl:\n    files: [rtl/top.sv]\n    file_type: systemVerilogSource\n"
        "  config:\n    files:\n"
        "      - lint/rules.cfg: {file_type: veribleLintRules}\n"
        "      - lint/waivers.txt: {file_type: veribleLintWaiver}\n"
        "      - lint/extra.txt: {file_type: veribleLintWaiver}\n"
        "targets:\n  lint_style:\n    flow: lint\n    flow_options:\n"
        f"      tool: {spelling}\n"
        "      ruleset: all\n      rules: [-module-filename, line-length]\n"
        "      verible_lint_args: [--show_diagnostic_context]\n"
        "    filesets: [rtl, config]\n    toplevel: top\n",
        encoding="utf-8",
    )


def _isolated_verible_fusesoc(root: Path, spelling: str) -> list[str]:
    import booley

    nodes = root / "nodes"
    nodes.mkdir()
    source = Path(booley.__file__).parent / "data/edalize/verible.py"
    for name in ("verible", "veriblelint"):
        shutil.copyfile(source, nodes / f"{name}.py")
    script = (
        "import edalize.tools; "
        f"edalize.tools.__path__ = [{str(nodes)!r}, *edalize.tools.__path__]; "
        "from edalize.flows.lint import Lint; "
        f"options = Lint.get_tool_options({{'tool': {spelling!r}}}); "
        "assert 'rules' in options and 'ruleset' in options; "
        "from fusesoc.main import main; main()"
    )
    return [sys.executable, "-c", script]


@pytest.mark.parametrize("spelling", ["verible", "veriblelint"])
def test_authored_verible_public_plan(tmp_path: Path, state_file: Path, capsys, spelling: str):
    _author_verible_alias_core(tmp_path, spelling)
    flow = LintFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "lint_style", "--dry-run"])
    result = flow._run()
    plan = _dry_run_plan(capsys)
    assert plan["aggregate_errors"] == []
    assert result.exit_code == EXIT_SUCCESS
    commands = plan["work_units"][0]["commands"]
    assert any("verible-verilog-lint" in str(command["argv"]) for command in commands)
    assert any(f"tool_{spelling}" in str(command["argv"]) for command in commands)


def _real_lint_setup(monkeypatch, command: list[str], root: Path) -> None:
    real_resolve = _REAL_RESOLVE_TARGET_HANDLE
    monkeypatch.setattr(
        fusesoc_registry, "setup_command_for_handle", _REAL_SETUP_COMMAND_FOR_HANDLE
    )
    config = root / "isolated-fusesoc.conf"
    config.write_text("[main]\n", encoding="utf-8")
    command = [*command, "--config", str(config)]
    env = dict(os.environ)
    env.pop("FUSESOC_CORES", None)
    for kind in ("CONFIG", "CACHE", "DATA"):
        env[f"XDG_{kind}_HOME"] = str(root / f"isolated-{kind.lower()}")

    def bounded_runner(*args, **kwargs):
        assert set(kwargs) <= {
            "cwd",
            "env",
            "capture_output",
            "text",
        }  # the helper captures text itself
        assert kwargs["env"] == env
        assert args[0][args[0].index("--config") + 1] == str(config)
        return run_bounded_child(args[0], cwd=kwargs.get("cwd"), env=kwargs["env"])

    def resolve(*args, **kwargs):
        return real_resolve(
            *args, **{**kwargs, "fusesoc_cmd": command, "runner": bounded_runner, "env": env}
        )

    monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", resolve)


def _assert_verible_rule_path(makefile: str, edam_path: Path) -> None:
    edam = yaml.safe_load(edam_path.read_text(encoding="utf-8"))
    rule_file = next(
        file["name"] for file in edam["files"] if file["file_type"] == "veribleLintRules"
    )
    assert Path(rule_file).parts[-2:] == ("lint", "rules.cfg")
    recipe = next(line for line in makefile.splitlines() if "verible-verilog-lint " in line)
    rules_flags = [part for part in recipe.split() if part.startswith("--rules_config=")]
    assert rules_flags == [f"--rules_config={rule_file}"]


@pytest.mark.parametrize("spelling", ["verible", "veriblelint"])
def test_authored_verible_public_run(
    tmp_path: Path, state_file: Path, monkeypatch, capsys, spelling
):
    _author_verible_alias_core(tmp_path, spelling)
    _real_lint_setup(monkeypatch, _isolated_verible_fusesoc(tmp_path, spelling), tmp_path)
    flow = LintFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "lint_style"])
    execute = MagicMock(
        return_value=SubprocessResult(
            returncode=0,
            stdout=SAMPLE_VERIBLE_OUTPUT,
            stderr="",
            timed_out=False,
            duration_s=0.1,
        )
    )
    monkeypatch.setattr(flow, "_execute", execute)
    result = flow._run()
    output = capsys.readouterr().out
    assert execute.call_count == 1, output
    assert "verible-verilog-lint" in str(execute.call_args)
    assert "interface-name-style" in output
    assert result.exit_code == EXIT_FAILURE
    assert "RESULT: WARN" in output
    assert result.detail["eda_tools"] == {"lint_style": spelling}
    makefile = next(tmp_path.rglob("Makefile")).read_text(encoding="utf-8")
    for flag in (
        "--parse_fatal",
        "--lint_fatal=false",
        "--ruleset=all",
        "--rules=-module-filename,line-length",
        "--show_diagnostic_context",
        "--waiver_files=",
    ):
        assert flag in makefile
    assert (
        "rtl/top.sv" in makefile
        and "lint/waivers.txt" in makefile
        and "lint/extra.txt" in makefile
    )
    edam_path = next(tmp_path.rglob("*.eda.yml"))
    _assert_verible_rule_path(makefile, edam_path)
    edam = edam_path.read_text(encoding="utf-8")
    assert f"tool: {spelling}" in edam
    assert result.detail["total_warnings"] > 0


@pytest.mark.parametrize("configured", ["verible", "veriblelint", "verilator"])
def test_alias_preparation_compares_parser_families(tmp_path, configured):
    flow = LintFlow()
    flow.parse_args(["--target", "lint_style", "--work-dir", str(tmp_path)])
    handle = _target_handle("lint_style", project_root=tmp_path, eda_tool="veriblelint")
    with patch.object(
        fusesoc_registry, "resolve_target_handle", return_value=_stub_resolved(configured)
    ):
        if configured == "verilator":
            with pytest.raises(ValueError, match=r"declared 'verible'.*configured 'verilator'"):
                flow._prepare_lint_command(handle)
        else:
            prepared = flow._prepare_lint_command(handle)
            assert "verible-verilog-lint" in str(prepared.command)


@pytest.mark.parametrize("spelling", ["verilator", "verible", "veriblelint"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_mixed_lint_rejects_before_preparation(
    tmp_path, state_file, monkeypatch, capsys, spelling, dry_run
):
    from booley.targets.domain import IncompatibleTargetError

    _author_verible_alias_core(tmp_path, spelling)
    core = tmp_path / "style.core"
    text = core.read_text().replace(
        f"    flow_options:\n      tool: {spelling}\n",
        f"    default_tool: {spelling}\n    flow_options:\n",
    )
    core.write_text(text)
    handle = TargetCatalog.build(tmp_path).select("lint_style")
    assert "lint" not in handle.drivable_by
    inspect = MagicMock(side_effect=AssertionError("incompatible Target inspected"))
    setup = MagicMock(side_effect=AssertionError("incompatible Target setup"))
    execute = MagicMock(side_effect=AssertionError("incompatible Target executed"))
    monkeypatch.setattr(TargetCatalog, "inspect", inspect)
    monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", setup)
    flow = LintFlow()
    argv = ["--work-dir", str(tmp_path), "--target", "lint_style"]
    flow.parse_args([*argv, *(["--dry-run"] if dry_run else [])])
    monkeypatch.setattr(flow, "_execute", execute)
    with pytest.raises(IncompatibleTargetError, match="default_tool does not supply it"):
        flow._run()
    inspect.assert_not_called()
    setup.assert_not_called()
    execute.assert_not_called()


def test_job_report_lint_findings_cap(tmp_path, state_file):
    for count in (0, 20, 21):
        output = "".join(f"%Warning-WIDTH: f.sv:{i + 1}:1: message {i}\n" for i in range(count))
        with (
            patch.object(
                LintFlow, "_prepare_lint_command", return_value=(["lint"], _stub_resolved())
            ),
            patch.object(
                LintFlow,
                "_execute",
                return_value=SubprocessResult(
                    returncode=0,
                    stdout=output,
                    stderr="",
                    timed_out=False,
                    duration_s=1.0,
                ),
            ),
        ):
            flow = LintFlow()
            flow.parse_args(["--target", "lite", "--report-dir", str(tmp_path / "reports")])
            result = flow._run()
        detail = result.detail
        assert "findings" in detail
        assert detail["findings"] == [
            {
                "severity": "warning",
                "rule": "WIDTH",
                "file": "f.sv",
                "line": i + 1,
                "message": f"message {i}",
                "target": "lite",
            }
            for i in range(min(count, 20))
        ]
        assert (detail["findings_total"], detail["findings_truncated"]) == (count, count > 20)
        assert detail["targets"] == ["lite"] and detail["total_warnings"] == count
        assert detail["eda_tools"] == {"lite": "verilator"}
        full = json.loads((tmp_path / detail["artifacts"]["report"]).read_text())
        assert len(full["warnings"]) == count and full["errors"] == []
        assert full["passed"] is (count == 0)
        assert result.exit_code == (EXIT_FAILURE if count else EXIT_SUCCESS)


def test_job_report_lint_findings_targets():
    from booley.flows.lint.flow import _lint_result_parts

    targets = ["first", "second", "empty"]
    for warning_count, error_count in ((1, 0), (19, 2), (20, 1), (0, 2)):
        warnings = [
            LintWarning("WIDTH", "rtl/a.sv", i, 1, str(i), target)
            for target in targets[:2]
            for i in range(warning_count)
        ]
        outside = LintWarning("WIDTH", "vendor/a.sv", 1, 1, "outside", "second")
        unique = filter_by_scope(deduplicate_warnings([*warnings, outside]), "rtl/")
        results = [
            LintConfigResult(t, error=f"error {i}" if i < error_count else "")
            for i, t in enumerate(targets)
        ]
        _, detail = _lint_result_parts(targets, unique, 1.25, results)
        assert "findings" in detail
        total = warning_count + error_count
        assert detail["findings_total"] == total
        assert detail["findings_truncated"] is (total > 20)
        assert len(detail["findings"]) == min(20, total)
        assert all(f["target"] == "first" for f in detail["findings"][:warning_count])
        assert detail["findings"][warning_count:] == [
            {
                "severity": "error",
                "rule": None,
                "file": None,
                "line": None,
                "message": f"error {i}",
                "target": targets[i],
            }
            for i in range(min(error_count, 20 - warning_count))
        ]
        assert detail["findings_by_target"] == {
            t: {"warnings": warning_count if i < 2 else 0, "errors": int(i < error_count)}
            for i, t in enumerate(targets)
        }


@pytest.mark.parametrize("target", ["lite", "lite,full"])
def test_invocation_report_records_tools_by_target(tmp_path, state_file, target):
    flow = LintFlow()
    flow.parse_args(["--target", target, "--report-dir", str(tmp_path / "reports")])
    with (
        patch.object(
            flow,
            "_prepare_lint_command",
            return_value=(["verilator", "--lint-only"], _stub_resolved("Verilator")),
        ),
        patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0, stdout="", stderr="", timed_out=False, duration_s=0.5
            ),
        ),
    ):
        result = flow._run()
    path = flow.write_report(result)
    report = json.loads(path.read_bytes())
    expected = (
        {"eda_tool": "Verilator"}
        if target == "lite"
        else {"eda_tools": {"lite": "Verilator", "full": "Verilator"}}
    )
    assert {key: report[key] for key in ("eda_tool", "eda_tools") if key in report} == expected


def test_lint_label_carries_target_source_bytes(tmp_path):
    from types import SimpleNamespace

    from booley.flows import job_resources
    from booley.flows.base import SubprocessResult
    from booley.flows.source_paths import ProjectSourcePaths
    from booley.fusesoc.fusesoc_registry import ResolvedFile

    (tmp_path / "top.sv").write_bytes(b"module top; endmodule\n")
    (tmp_path / "pkg.sv").write_bytes(b"package p; endpackage\n")
    expected_bytes = sum(len(path.read_bytes()) for path in tmp_path.glob("*.sv"))
    flow = LintFlow()
    flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
    prepared = SimpleNamespace(
        result=LintConfigResult(target="lite"),
        command=["true"],
        resolved=SimpleNamespace(
            configured_eda_tool="verilator",
            build_root=tmp_path,
            files=(
                ResolvedFile("top.sv", "systemVerilogSource"),
                ResolvedFile("pkg.sv", "systemVerilogSource"),
            ),
        ),
        attempt_token="",
        project_paths=ProjectSourcePaths(),
    )
    seen: list[job_resources.ResourceLabel | None] = []

    def execute(*_args, **_kwargs):
        seen.append(job_resources.current_label())
        return SubprocessResult(returncode=0)

    with patch.object(flow, "_execute_boundary", side_effect=execute):
        flow._run_lint_target(_target_handle("lite"), prepared=prepared)

    assert seen == [job_resources.ResourceLabel("lite", "lint", source_bytes=expected_bytes)]
    assert expected_bytes > 0


# ---------------------------------------------------------------------------
# Typed metrics (issue #3497)
# ---------------------------------------------------------------------------

_VERILATOR_FINDINGS = (
    "%Warning-UNUSEDSIGNAL: rtl/a.v:3:5: Signal is not used: 'x'\n"
    "%Warning-UNUSEDSIGNAL: rtl/a.v:3:5: Signal is not used: 'x'\n"
    "%Warning-WIDTH: rtl/b.v:9:1: Operator ASSIGNW expects 8 bits\n"
    "%Error: rtl/x.v:1:1: Can't find definition of variable: 'zzz'\n"
    "%Error: rtl/x.v:9:5: second diagnostic\n"
    "%Error: rtl/x.v:9:5: second diagnostic\n"
    "%Error: Exiting due to 3 error(s)\n"
)
_VERIBLE_FINDINGS = (
    "rtl/top.sv:4:11: Interface names must end with _if. [interface-name-style]\n"
    "rtl/top.sv:4:11: Interface names must end with _if. [interface-name-style]\n"
    "rtl/top.sv:9:1: Line length exceeds 100. [line-length]\n"
    'rtl/top.sv:3:1: syntax error at token "endmodule"\n'
    'rtl/top.sv:5:1: syntax error at token "foo"\n'
    'rtl/top.sv:5:7: syntax error at token "bar"\n'  # same line: counted once, first message
)


class TestFlowMetrics:
    """A Lint run returns its unique warning and error counts as typed metrics."""

    @staticmethod
    def _lint(
        tmp_path: Path, family: str, *, stdout: str, returncode: int, timed_out: bool = False
    ) -> LintConfigResult:
        """Run one Target through ``_run_lint_target`` against canned linter output."""
        with (
            patch.object(
                LintFlow,
                "_execute",
                return_value=SubprocessResult(
                    returncode=returncode,
                    stdout=stdout,
                    stderr="",
                    timed_out=timed_out,
                    duration_s=0.1,
                ),
            ),
            patch.object(
                LintFlow,
                "_prepare_lint_command",
                return_value=(["make", "-C", "x"], _stub_resolved(family)),
            ),
        ):
            flow = LintFlow()
            flow.parse_args(["--target", "lite", "--work-dir", str(tmp_path)])
            return flow._run_lint_target(_target_handle("lite", eda_tool=family))

    def test_findings_return_unique_warning_and_error_counts(
        self, tmp_path: Path, state_file: Path
    ) -> None:
        cases = [
            (
                "verilator",
                _VERILATOR_FINDINGS,
                2,
                {
                    "errors": [
                        "rtl/x.v:1: Can't find definition of variable: 'zzz'",
                        "rtl/x.v:9: second diagnostic",
                    ],
                    "warnings": [
                        "rtl/a.v:3 UNUSEDSIGNAL: Signal is not used: 'x'",
                        "rtl/b.v:9 WIDTH: Operator ASSIGNW expects 8 bits",
                    ],
                },
            ),
            (
                "verible",
                _VERIBLE_FINDINGS,
                1,
                {
                    "errors": [
                        'rtl/top.sv:3: syntax error at token "endmodule"',
                        'rtl/top.sv:5: syntax error at token "foo"',
                    ],
                    "warnings": [
                        "rtl/top.sv:4 interface-name-style: Interface names must end with _if.",
                        "rtl/top.sv:9 line-length: Line length exceeds 100.",
                    ],
                },
            ),
        ]
        for family, output, returncode, expected in cases:
            result = self._lint(tmp_path, family, stdout=output, returncode=returncode)

            values = result.flow_metrics()
            assert values.plain == {"warnings": 2, "errors": 2}, family
            assert values.counted_items == expected, family

    def test_listed_findings_are_capped_and_cut(self, tmp_path: Path, state_file: Path) -> None:
        output = "".join(
            f"%Warning-WIDTH: rtl/w.v:{line}:1: {'long ' * 60 if line == 1 else 'wide'}\n"
            for line in range(1, 26)
        )

        values = self._lint(tmp_path, "verilator", stdout=output, returncode=0).flow_metrics()

        assert values.plain["warnings"] == 25
        listed = values.counted_items["warnings"]
        assert len(listed) == 20
        assert listed[1] == "rtl/w.v:2 WIDTH: wide"
        assert len(listed[0]) == 200

    def test_clean_run_returns_zero_counts(self, tmp_path: Path, state_file: Path) -> None:
        result = self._lint(tmp_path, "verilator", stdout="", returncode=0)

        values = result.flow_metrics()
        assert values.plain == {"warnings": 0, "errors": 0}
        assert values.counted_items == {}

    def test_warnings_only_run_does_not_count_the_epilogue_as_an_error(
        self, tmp_path: Path, state_file: Path
    ) -> None:
        output = (
            "%Warning-WIDTH: rtl/b.v:9:1: Operator ASSIGNW expects 8 bits\n"
            "%Error: Exiting due to 1 warning(s)\n"
        )

        result = self._lint(tmp_path, "verilator", stdout=output, returncode=2)

        assert result.flow_metrics().plain == {"warnings": 1, "errors": 0}

    def test_linter_that_could_not_run_produces_no_metrics(
        self, tmp_path: Path, state_file: Path
    ) -> None:
        from booley.flows.metric_declarations import MetricsUnavailableError

        result = self._lint(tmp_path, "verilator", stdout="", returncode=-9, timed_out=True)

        with pytest.raises(MetricsUnavailableError, match="Timed out"):
            result.flow_metrics()

    def test_rejected_design_without_a_readable_error_produces_no_metrics(
        self, tmp_path: Path, state_file: Path
    ) -> None:
        from booley.flows.metric_declarations import MetricsUnavailableError

        result = self._lint(tmp_path, "verilator", stdout="boom\n", returncode=3)

        with pytest.raises(MetricsUnavailableError, match="exited 3"):
            result.flow_metrics()

    def test_lint_error_findings_verilator(self) -> None:
        from booley.flows.lint.flow import lint_error_findings

        cases = [
            # Two different errors on one line count twice; a rule-less, location-less one once.
            (
                "%Error: a.v:1:1: first\n%Error: a.v:1:1: other\n%Error: Internal Error\n",
                3,
            ),
            # A repeated line counts once; the same message under another rule counts again.
            (
                "%Error-UNSUPPORTED: a.v:2:1: nope\n%Error-UNSUPPORTED: a.v:2:1: nope\n"
                "%Error: a.v:2:1: nope\n",
                2,
            ),
            # Continuation lines of a multi-line message are not errors.
            (
                "%Error: a.v:3:1: bad\n    ... note\n   3 | wire x;\n      | ^\n",
                1,
            ),
            ("%Error: Exiting due to 1 error(s)\n", 0),
            ("", 0),
        ]
        for output, expected in cases:
            assert len(lint_error_findings(output, "verilator")) == expected, output


def _author_cross_core_project(root: Path) -> None:
    """Core ``a`` at the root and core ``b`` in ``ip/b`` both lint ``ip/b/rtl/top.sv``."""
    (root / "ip/b/rtl").mkdir(parents=True)
    (root / "ip/b/rtl/top.sv").write_bytes(b"module top; endmodule\n")
    (root / "a.core").write_bytes(
        b"CAPI=2:\nname: ::a:0\nfilesets:\n  rtl:\n"
        b"    files: [ip/b/rtl/top.sv]\n    file_type: systemVerilogSource\n"
        b"targets:\n  lint_a:\n    flow: lint\n    flow_options: {tool: verilator}\n"
        b"    filesets: [rtl]\n    toplevel: top\n"
    )
    (root / "ip/b/b.core").write_bytes(
        b"CAPI=2:\nname: ::b:0\nfilesets:\n  rtl:\n"
        b"    files: [rtl/top.sv]\n    file_type: systemVerilogSource\n"
        b"targets:\n  lint_b:\n    flow: lint\n    flow_options: {tool: verilator}\n"
        b"    filesets: [rtl]\n    toplevel: top\n"
        b"  style_b:\n    flow: lint\n    flow_options: {tool: verible}\n"
        b"    filesets: [rtl]\n    toplevel: top\n"
    )


def _author_alias_project(root: Path) -> None:
    """Core ``a`` lists the file through a symlink and a ``copyto`` named like an export."""
    _author_cross_core_project(root)
    try:
        (root / "alias").symlink_to(root / "ip/b/rtl", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    (root / "a.core").write_bytes(
        b"CAPI=2:\nname: ::a:0\nfilesets:\n  rtl:\n"
        b"    files:\n      - alias/top.sv\n"
        b"      - ip/b/rtl/top.sv: {copyto: src/a_0/staged.sv}\n"
        b"    file_type: systemVerilogSource\n"
        b"targets:\n  lint_a:\n    flow: lint\n    flow_options: {tool: verilator}\n"
        b"    filesets: [rtl]\n    toplevel: top\n"
    )


def _author_demo_project(root: Path) -> None:
    """The stealth demo layout: its core lives in ``.booley/cores`` and lists ``picorv32.v``."""
    (root / ".booley/cores").mkdir(parents=True)
    (root / "picorv32.v").write_bytes(b"module picorv32; endmodule\n")
    (root / ".booley/booley.toml").write_bytes(
        b"[stealth]\nenabled = true\nignore_native_cores = true\n"
    )
    (root / ".booley/cores/picorv32_impl.core").write_bytes(
        b"CAPI=2:\nname: booley::picorv32-impl:0\nfilesets:\n  rtl:\n"
        b"    files: [picorv32.v]\n    file_type: systemVerilogSource\n"
        b"targets:\n  lint_core:\n    flow: lint\n    flow_options: {tool: verilator}\n"
        b"    filesets: [rtl]\n    toplevel: picorv32\n"
    )


_UNUSED_B = "%Warning-UNUSEDSIGNAL: src/b_0/rtl/top.sv:1:8: Signal is not used: 'x'\n"
_UNUSED_A = "%Warning-UNUSEDSIGNAL: src/a_0/ip/b/rtl/top.sv:1:8: Signal is not used: 'x'\n"
_GENERATED = "%Warning-WIDTH: gen/generated.sv:2:3: Operator ASSIGN expects 8 bits\n"


@pytest.mark.parametrize(
    ("project", "target", "args", "processes", "exit_code", "rows", "texts"),
    [
        pytest.param(
            "cross",
            "lint_a,lint_b",
            (),
            (_process(stdout=_UNUSED_A + _GENERATED), _process(stdout=_UNUSED_B)),
            EXIT_FAILURE,
            [
                ("UNUSEDSIGNAL", "ip/b/rtl/top.sv", ["lint_a", "lint_b"]),
                ("WIDTH", "gen/generated.sv", ["lint_a"]),
            ],
            ["[lint]   %Warning-UNUSEDSIGNAL: ip/b/rtl/top.sv:1:8 Signal is not used: 'x'"],
            id="cross-core",
        ),
        pytest.param(
            "alias",
            "lint_a,lint_b",
            (),
            (
                _process(
                    stdout="%Warning-UNUSEDSIGNAL: src/a_0/alias/top.sv:1:8: Signal is not used: 'x'\n"
                    "%Warning-WIDTH: src/a_0/staged.sv:2:3: Operator ASSIGN expects 8 bits\n"
                ),
                _process(stdout=_UNUSED_B),
            ),
            EXIT_FAILURE,
            [
                ("UNUSEDSIGNAL", "ip/b/rtl/top.sv", ["lint_a", "lint_b"]),
                ("WIDTH", "src/a_0/staged.sv", ["lint_a"]),
            ],
            [],
            id="alias-and-copyto",
        ),
        pytest.param(
            "cross",
            "lint_b",
            ("--scope", "ip/b/"),
            (_process(),),
            EXIT_SUCCESS,
            [],
            ["RESULT: PASS"],
            id="scope",
        ),
        pytest.param(
            "demo",
            "lint_core",
            (),
            (
                _process(
                    stdout="%Warning-UNUSEDSIGNAL: src/booley__picorv32-impl_0/picorv32.v:10:3: "
                    "Signal is not used: 'y'\n"
                ),
            ),
            EXIT_FAILURE,
            [("UNUSEDSIGNAL", "picorv32.v", ["lint_core"])],
            [],
            id="demo",
        ),
        pytest.param(
            "cross",
            "style_b",
            (),
            (
                _process(
                    stdout="src/b_0/rtl/top.sv:4:11: Interface names must end with _if. "
                    "[interface-name-style]\n"
                ),
            ),
            EXIT_FAILURE,
            [("interface-name-style", "ip/b/rtl/top.sv", ["style_b"])],
            [
                "[lint]   ip/b/rtl/top.sv:4:11: Interface names must end with _if. "
                "[interface-name-style]"
            ],
            id="verible-finding",
        ),
        pytest.param(
            "cross",
            "lint_b,style_b",
            (),
            (
                _process(stdout=_UNUSED_B),
                _process(
                    stdout="src/b_0/rtl/top.sv:4:11: Interface names must end with _if. "
                    "[interface-name-style]\n"
                ),
            ),
            EXIT_FAILURE,
            [
                ("UNUSEDSIGNAL", "ip/b/rtl/top.sv", ["lint_b"]),
                ("interface-name-style", "ip/b/rtl/top.sv", ["style_b"]),
            ],
            [
                "[lint]   %Warning-UNUSEDSIGNAL: ip/b/rtl/top.sv:1:8 Signal is not used: 'x'",
                "[lint]   ip/b/rtl/top.sv:4:11: Interface names must end with _if. "
                "[interface-name-style]",
            ],
            id="mixed-linters",
        ),
        pytest.param(
            "cross",
            "lint_b",
            (),
            (
                _process(
                    1,
                    stderr="%Error: src/b_0/rtl/top.sv:3:1: syntax error, unexpected endmodule\n"
                    "%Error: Exiting due to 1 error(s)\n",
                ),
            ),
            EXIT_FAILURE,
            [],
            [
                "lint_b       [verilator] ERROR: %Error: ip/b/rtl/top.sv:3:1: syntax error",
                "RESULT: FAIL \u2014 lint_b [verilator]: %Error: ip/b/rtl/top.sv:3:1:",
            ],
            id="verilator-error",
        ),
        pytest.param(
            "cross",
            "style_b",
            (),
            (_process(1, stdout='src/b_0/rtl/top.sv:3:1: syntax error at token "endmodule"\n'),),
            EXIT_FAILURE,
            [],
            [
                "style_b      [verible] ERROR: ip/b/rtl/top.sv:3:1: syntax error",
                "RESULT: FAIL \u2014 style_b [verible]: ip/b/rtl/top.sv:3:1: syntax error",
            ],
            id="verible-error",
        ),
    ],
)
def test_lint_reports_project_paths(
    run_lint: _LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project: str,
    target: str,
    args: tuple[str, ...],
    processes: tuple[SubprocessResult, ...],
    exit_code: int,
    rows: list[tuple[str, str, list[str]]],
    texts: list[str],
) -> None:
    """Findings, the quoted error and the scope name Project paths, not FuseSoC's copies."""
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    {
        "demo": _author_demo_project,
        "alias": _author_alias_project,
        "cross": _author_cross_core_project,
    }[project](work_dir)
    _real_lint_setup(monkeypatch, _isolated_verible_fusesoc(tmp_path, "verible"), tmp_path)

    result, _ = run_lint(
        *processes,
        args=("--report-dir", str(tmp_path / "reports"), *args),
        target=target,
        work_dir=work_dir,
        resolved=None,
    )

    assert result.exit_code == exit_code
    report_path = tmp_path / "reports/lint_report.json"
    report: dict[str, Any] = {"warnings": [], "errors": []}
    if report_path.exists():
        report = json.loads(report_path.read_bytes())
    assert [(w["rule"], w["file"], w["targets"]) for w in report["warnings"]] == rows
    output = capsys.readouterr().out + result.outcome.report_text
    for text in texts:
        assert text in output
    for error in report["errors"]:
        assert "src/b_0" not in error["message"]
        assert "ip/b/rtl/top.sv:3:1:" in error["message"]
