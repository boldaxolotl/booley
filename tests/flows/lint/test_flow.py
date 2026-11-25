"""Tests for LintFlow вЂ” warning parsing, dedup, scope, reports."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from booley.flows import artifacts as flow_artifacts
from booley.flows.base import SubprocessResult
from booley.flows.lint.flow import (
    LintWarning,
    deduplicate_warnings,
    filter_by_scope,
    parse_verible_warnings,
    parse_warnings,
)
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import MissingTargetToplevelError, TargetHandle
from tests.canned_process_support import canned_command
from tests.flows.lint import linter_run_fake
from tests.flows.lint.linter_run_fake import (
    LintRun,
    isolated_verible_fusesoc,
    real_fusesoc_setup,
    stub_resolved,
)
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


def _process(
    returncode: int = 0, stdout: str = "", stderr: str = "", timed_out: bool = False
) -> SubprocessResult:
    """A canned linter subprocess result."""
    return SubprocessResult(
        returncode=returncode, stdout=stdout, stderr=stderr, timed_out=timed_out, duration_s=0.1
    )


@pytest.fixture()
def run_lint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LintRun:
    """Run ``booley flow lint`` with the shared linter fake (see ``linter_run_fake.install``)."""
    return linter_run_fake.install(tmp_path, monkeypatch)


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
    run_lint: LintRun,
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
        return stub_resolved(configured)

    result, commands = run_lint(_process(), resolved=resolved)

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text.startswith(expected)
    assert commands == []
    assert ("[lint] lite         ERROR: lint setup failed: " in capsys.readouterr().out) == (
        "lint setup failed" in expected
    )


def test_lint_runs_make_over_the_resolved_build_root(run_lint: LintRun, tmp_path: Path) -> None:
    """Booley resolves the Target through FuseSoC, then `make -C <relpath>`."""
    resolved_build = tmp_path / ".booley/.runtime/edalize/lint/lite/lint_demo_0/lite"
    resolver_calls: list[tuple[str, Path]] = []

    def resolved(handle: TargetHandle, build_root: Path) -> object:
        resolver_calls.append((handle.selector, build_root))
        return stub_resolved(build_root=resolved_build)

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
    run_lint: LintRun,
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
    real_fusesoc_setup(
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


def test_failed_fusesoc_setup_is_recorded_as_lint_prepare(
    run_lint: LintRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Lint Flow's own setup runner measures the setup, whatever its outcome."""
    from booley.runtime import resource_history

    work_dir = tmp_path / "proj"
    (work_dir / "rtl").mkdir(parents=True)
    (work_dir / "rtl/top.sv").write_text("module top; endmodule\n", encoding="utf-8")
    (work_dir / "lint_demo.core").write_bytes(_LINT_CORE_TEXT.encode())
    monkeypatch.setattr(
        fusesoc_registry, "setup_command_for_handle", _REAL_SETUP_COMMAND_FOR_HANDLE
    )
    failing_setup = canned_command(SubprocessResult(returncode=1, stderr="setup broke"))

    def resolve(*args: object, **kwargs: object) -> object:
        return _REAL_RESOLVE_TARGET_HANDLE(*args, **{**kwargs, "fusesoc_cmd": failing_setup})

    monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", resolve)

    result, commands = run_lint(_process(), work_dir=work_dir, resolved=None)

    assert result.exit_code == EXIT_ERROR
    assert "failed (exit 1)" in result.outcome.report_text
    assert "setup broke" in result.outcome.report_text
    assert commands == []
    (record,) = resource_history.load_records(tmp_path)
    key = record["key"]
    assert (
        record["flow"],
        key["target"],
        key["phase"],
        record["outcome"],
        record["returncode"],
    ) == ("lint", "lite", "lint_prepare", "exit_nonzero", 1)


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
    run_lint: LintRun,
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
        return stub_resolved("verible")

    result, commands = run_lint(_process(), target="lint", resolved=resolved)

    assert result.exit_code == EXIT_ERROR
    assert "Unknown target 'lint'" in result.outcome.report_text
    assert resolved_handles == []
    assert commands == []


def test_selected_doctor_target_keeps_its_core_and_parser(
    run_lint: LintRun,
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
        return stub_resolved("verible")

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
    run_lint: LintRun,
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
    run_lint: LintRun,
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


_LINT = "flow: lint, flow_options: {tool: verilator}"
_SV = "file_type: systemVerilogSource"
_UNDECLARED_TOP = (
    "{target}: lint toplevel '{top}' is not declared by any fileset file (1 HDL sources) "
    "\u2014 lint would be vacuous. Fix the Target's `toplevel` or add the declaring file to "
    "its filesets."
)
_SETUP_PROJECT = {
    "rtl/top.sv": "module top; endmodule\n",
    "rtl/flag_top.sv": "module flag_top; endmodule\n",
    "rtl/auto_top.sv": "module auto_top; endmodule\n",
    "gen.core": (
        "CAPI=2:\nname: ::gen:0\n"
        "generators:\n  mkgen: {interpreter: python3, command: gen.py}\n"
        f"filesets:\n  rtl:\n    files: [rtl/top.sv: {{{_SV}}}]\n"
        "generate:\n  g1: {generator: mkgen}\n"
        "targets:\n"
        "  default: {filesets: [rtl], generate: [g1]}\n"
        f"  declared: {{{_LINT}, filesets: [rtl], toplevel: top}}\n"
        f"  undeclared: {{{_LINT}, filesets: [rtl], toplevel: no_such_top}}\n"
        f"  generates: {{{_LINT}, filesets: [rtl], generate: [g1], toplevel: gen_top}}\n"
        f"  unrelated_generator: {{{_LINT}, filesets: [rtl], toplevel: gen_top}}\n"
    ),
    "user.core": (
        "CAPI=2:\nname: ::user:0\n"
        f"filesets:\n  rtl:\n    depend: ['::gen:0']\n    files: [rtl/top.sv: {{{_SV}}}]\n"
        f"targets:\n  dependency_generates: {{{_LINT}, filesets: [rtl], toplevel: gen_top}}\n"
    ),
    "setup.core": (
        "CAPI=2:\nname: ::setup:0\n"
        "filesets:\n  rtl:\n    files:\n"
        f"      - rtl/top.sv: {{{_SV}}}\n"
        f"      - owned ? (rtl/flag_top.sv): {{{_SV}}}\n"
        "      - rtl/auto_top.sv\n"
        "targets:\n"
        f"  flagged: {{{_LINT}, filesets: [rtl], flags: {{owned: true}}, toplevel: flag_top}}\n"
        f"  filtered: {{{_LINT}, filesets: [rtl], filters: [autotype], toplevel: auto_top}}\n"
    ),
}


@pytest.mark.parametrize(
    ("target", "failing_top"),
    [
        pytest.param("declared", None, id="declared"),
        pytest.param("undeclared", "no_such_top", id="undeclared"),
        pytest.param("generates", None, id="generates"),
        pytest.param("unrelated_generator", "gen_top", id="unrelated-generator"),
        pytest.param("dependency_generates", None, id="dependency-generates"),
        pytest.param("flagged", None, id="flagged"),
        pytest.param("filtered", None, id="filtered"),
    ],
)
def test_dry_run_judges_an_undeclared_toplevel_unless_setup_generates(
    run_lint: LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
    failing_top: str | None,
) -> None:
    """Planning judges the sources FuseSoC setup lists, except where setup can add or retype some."""
    for name, text in _SETUP_PROJECT.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(text.encode())
    monkeypatch.setattr(
        TargetCatalog, "build", classmethod(lambda _cls, root: _REAL_CATALOG_BUILD(root))
    )

    result, commands = run_lint(args=("--dry-run",), target=target)

    plan = _dry_run_plan(capsys)
    assert commands == []
    if failing_top is None:
        assert result.exit_code == EXIT_SUCCESS
        assert plan["aggregate_errors"] == []
        assert len(plan["work_units"]) == 1
    else:
        assert result.exit_code == EXIT_ERROR
        assert plan["aggregate_errors"] == [_UNDECLARED_TOP.format(target=target, top=failing_top)]
        assert plan["work_units"] == []


def test_real_run_rejects_an_undeclared_toplevel_at_planning(
    run_lint: LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real run stops on the planning verdict before FuseSoC setup runs."""
    for name, text in _SETUP_PROJECT.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(text.encode())
    monkeypatch.setattr(
        TargetCatalog, "build", classmethod(lambda _cls, root: _REAL_CATALOG_BUILD(root))
    )

    result, commands = run_lint(target="undeclared")

    assert result.exit_code == EXIT_ERROR
    assert (
        _UNDECLARED_TOP.format(target="undeclared", top="no_such_top")
        in result.outcome.report_text
    )
    assert commands == []


@pytest.mark.parametrize(
    ("conf", "exit_code"),
    [
        pytest.param(b"[main]\nfilters = autotype\n", EXIT_SUCCESS, id="config-filter"),
        pytest.param(b"[main\n", EXIT_SUCCESS, id="unreadable-config"),
        pytest.param(b"[main]\ncache_root = x\n", EXIT_ERROR, id="no-filter"),
    ],
)
def test_dry_run_defers_to_setup_when_fusesoc_config_filters_sources(
    run_lint: LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    conf: bytes,
    exit_code: int,
) -> None:
    """A filter in ``fusesoc.conf`` can retype sources, so planning leaves the toplevel to setup."""
    for name, text in _SETUP_PROJECT.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(text.encode())
    tmp_path.joinpath("fusesoc.conf").write_bytes(conf)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))
    monkeypatch.setattr(
        TargetCatalog, "build", classmethod(lambda _cls, root: _REAL_CATALOG_BUILD(root))
    )

    result, _commands = run_lint(args=("--dry-run",), target="undeclared")

    assert result.exit_code == exit_code
    assert (_dry_run_plan(capsys)["aggregate_errors"] == []) is (exit_code == EXIT_SUCCESS)


def test_dry_run_previews_fusesoc_setup_without_resolving(
    run_lint: LintRun,
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
    run_lint: LintRun, tmp_path: Path, process: SubprocessResult, outcome: str
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
    run_lint: LintRun,
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


def test_a_single_eda_tool_failure_decides_a_mixed_run(run_lint: LintRun) -> None:
    """An unusable linter makes the other Targets' verdicts untrustworthy."""
    design_fail = _process(2, stderr="%Error: f.sv:1:1: syntax error\n")
    missing_linter = _process(2, stderr="make: verilator: No such file or directory\n")

    result, commands = run_lint(design_fail, missing_linter, target="lite,lint_style")

    assert len(commands) == 2
    assert result.exit_code == EXIT_ERROR


@pytest.mark.parametrize("published", [True, False], ids=["published", "publish-failed"])
def test_hard_fail_cites_the_published_log(
    run_lint: LintRun,
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


def test_previous_runs_log_is_erased_before_the_run(run_lint: LintRun, tmp_path: Path) -> None:
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
        return stub_resolved(build_root=tmp_path / handle.selector)

    run_lint(_process(), resolved=resolved)

    (mid_run,) = seen
    assert "stale finding" not in mid_run
    assert mid_run.startswith("[BOOLEY RUN_LOG] ")
    assert "flow=lint target=lite" in mid_run


def _tools_by_target(
    monkeypatch: pytest.MonkeyPatch, tools: dict[str, str]
) -> Callable[..., object]:
    """Declare each pass-through Target's linter; return the matching resolver."""
    install_lenient_target_catalog(
        monkeypatch,
        real_catalog_build=_REAL_CATALOG_BUILD,
        fallback_handle=lambda root, token: _target_handle(
            token, project_root=root, eda_tool=tools[token]
        ),
    )
    return lambda handle, build_root: stub_resolved(
        tools[handle.selector], build_root=build_root / handle.selector
    )


def test_a_late_preparation_error_runs_no_linter(run_lint: LintRun, tmp_path: Path) -> None:
    """Every Target is prepared before the first linter runs."""

    def resolved(handle: TargetHandle, _build_root: Path) -> object:
        if handle.selector == "full":
            raise RuntimeError("broken full Target")
        return stub_resolved(build_root=tmp_path / handle.selector)

    result, commands = run_lint(_process(), target="lite,full", resolved=resolved)

    assert result.exit_code == EXIT_ERROR
    assert "full: lint setup failed: broken full Target" in result.outcome.report_text
    assert commands == []


_SHARED_ROW = {"rule": "SHARED", "file": "rtl/top.sv", "line": 4, "message": "message"}


@pytest.mark.parametrize(
    ("tools", "outputs", "row"),
    [
        pytest.param(
            {"lite": "verilator", "full": "verilator"},
            ["%Warning-SHARED: rtl/top.sv:4:2: message\n"] * 2,
            {**_SHARED_ROW, "targets": ["full", "lite"]},
            id="same-linter",
        ),
        pytest.param(
            {"lite": "verilator", "full": "verible"},
            ["%Warning-SHARED: rtl/top.sv:4:2: message\n", "rtl/top.sv:4:9: other [SHARED]\n"],
            {
                **_SHARED_ROW,
                "targets": ["full", "lite"],
                "eda_tools": {"full": "verible", "lite": "verilator"},
            },
            id="mixed-linters",
        ),
        pytest.param(
            {"lite": "verible", "full": "verible"},
            ["rtl/top.sv:4:11: message [SHARED]\n"] * 2,
            {**_SHARED_ROW, "targets": ["full", "lite"]},
            id="verible",
        ),
    ],
)
def test_a_warning_from_several_targets_is_one_report_row(
    run_lint: LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tools: dict[str, str],
    outputs: list[str],
    row: dict[str, object],
) -> None:
    """The (rule, file, line) key groups Targets; a row maps them to linters only when they differ."""
    resolved = _tools_by_target(monkeypatch, tools)
    result, _ = run_lint(
        *(_process(stdout=output) for output in outputs),
        target="lite,full",
        args=("--report-dir", str(tmp_path / "reports")),
        resolved=resolved,
    )

    report = json.loads((tmp_path / "reports/lint_report.json").read_bytes())
    assert report["warnings"] == [row]
    assert result.outcome.detail["total_warnings"] == 1
    assert "1 unique in-scope warning\n" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("target", "stdout", "summary"),
    [
        pytest.param(
            "lite",
            "%Warning-UNUSEDSIGNAL: f.sv:1:1: unused\n",
            "RESULT: WARN (1 warnings)\nSee {report} for the full warning list "
            "(rule/file:line/message).",
            id="warn",
        ),
        pytest.param("lite,full", "", "RESULT: PASS", id="pass"),
    ],
)
def test_report_lists_the_run_and_the_summary_points_at_it(
    run_lint: LintRun, tmp_path: Path, target: str, stdout: str, summary: str
) -> None:
    """The WARN summary names the report that holds rule/file:line/message; PASS names nothing."""
    reports = tmp_path / "reports"

    result, _ = run_lint(
        _process(stdout=stdout), target=target, args=("--report-dir", str(reports))
    )

    artifacts = result.outcome.detail["artifacts"]
    assert result.outcome.report_text == summary.format(report=tmp_path / artifacts["report"])
    data = json.loads((reports / "lint_report.json").read_bytes())
    selectors = target.split(",")
    assert (data["flow"], data["targets"]) == ("lint", selectors)
    assert (data["passed"], data["total_warnings"]) == (not stdout, int(bool(stdout)))
    assert [(w["rule"], w["targets"]) for w in data["warnings"]] == (
        [("UNUSEDSIGNAL", ["lite"])] if stdout else []
    )
    assert data["artifacts"] == artifacts
    assert [r["log"] for r in data["target_results"]] == [
        artifacts[f"log_{selector}"] for selector in selectors
    ]
    assert artifacts["log_lite"].endswith("lite/run.log")


def test_consecutive_runs_preserve_first_cited_evidence(run_lint: LintRun, tmp_path: Path) -> None:
    """Each run publishes a numbered report and log; a later run never rewrites them."""
    report_dir = tmp_path / "reports"
    args = ("--report-dir", str(report_dir))

    first, _ = run_lint(_process(stdout="%Warning-WIDTH: first.sv:1:1: first run\n"), args=args)
    first_report = tmp_path / first.outcome.detail["artifacts"]["report"]
    first_log = tmp_path / first.outcome.detail["artifacts"]["log_lite"]
    first_bytes = (first_report.read_bytes(), first_log.read_bytes())
    second, _ = run_lint(_process(stdout="%Warning-WIDTH: second.sv:2:2: second run\n"), args=args)
    second_report = tmp_path / second.outcome.detail["artifacts"]["report"]
    second_log = tmp_path / second.outcome.detail["artifacts"]["log_lite"]

    assert [path.relative_to(report_dir).parts[:2] for path in (first_report, first_log)] == [
        ("lint", "1")
    ] * 2
    assert [path.relative_to(report_dir).parts[:2] for path in (second_report, second_log)] == [
        ("lint", "2")
    ] * 2
    assert str(first_report) in first.outcome.report_text
    assert (first_report.read_bytes(), first_log.read_bytes()) == first_bytes
    assert b"first.sv" in first_bytes[1]
    stable = report_dir / "lint_report.json"
    assert json.loads(stable.read_bytes()) == json.loads(second_report.read_bytes())
    assert stable.stat().st_mode & 0o004


@pytest.mark.parametrize(
    ("scope", "files", "exit_code", "count"),
    [
        # A scope matched through a warning path is accepted.
        pytest.param("mod_a", (), EXIT_FAILURE, "1 unique in-scope warning\n", id="substring"),
        pytest.param(
            "rtl/ticket.sv",
            ("rtl/ticket.sv", "rtl/mod_c.sv"),
            EXIT_SUCCESS,
            "0 unique in-scope warnings",
            id="out-of-scope-baseline",
        ),
    ],
)
def test_scope_keeps_only_matching_warnings(
    run_lint: LintRun,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    scope: str,
    files: tuple[str, ...],
    exit_code: int,
    count: str,
) -> None:
    """Out-of-scope baseline warnings must not keep a scoped ticket dirty."""
    output = (
        "%Warning-UNUSEDSIGNAL: rtl/mod_a.sv:42:5: unused\n"
        "%Warning-WIDTH: rtl/mod_c.sv:10:1: width mismatch\n"
    )

    result, _ = run_lint(
        _process(stdout=output),
        args=("--scope", scope),
        resolved=lambda handle, _root: stub_resolved(files=files, build_root=tmp_path),
    )

    assert result.exit_code == exit_code
    assert count in capsys.readouterr().out


_MOD_A_WARNINGS = (
    "%Warning-UNUSEDSIGNAL: rtl/mod_a.sv:42:5: unused\n"
    "%Warning-WIDTH: rtl/mod_a.sv:50:1: width mismatch\n"
)


@pytest.mark.parametrize(
    ("scope", "process", "files", "present", "absent"),
    [
        pytest.param(
            "nonexistent/",
            _process(stdout=_MOD_A_WARNINGS),
            ("rtl/mod_a.sv", "rtl/mod_b.sv"),
            # The example comes from the file list: no warning names mod_b.
            ["RESULT: ERROR", "'nonexistent/'", "rtl/mod_b.sv"],
            ["RESULT: PASS"],
            id="unmatched",
        ),
        pytest.param(
            "rtl/mod_a.sv,nonexistent/",
            _process(stdout=_MOD_A_WARNINGS),
            ("rtl/mod_a.sv", "rtl/mod_b.sv"),
            ["'nonexistent/'"],
            ["'rtl/mod_a.sv'"],
            id="mixed",
        ),
        pytest.param(
            "nonexistent/",
            _process(1, stdout="%Error: rtl/mod_a.sv:3:1: syntax error\n"),
            ("rtl/mod_a.sv",),
            # A scope typo must not hide a design FAIL.
            ["lite [verilator]: %Error: rtl/mod_a.sv:3:1: syntax error"],
            [],
            id="design-failure",
        ),
        pytest.param(
            "edalize/lint",
            _process(),
            ("src/demo_core_0/rtl/counter.sv",),
            # Build-root path fragments are not linted file names: no vacuous pass.
            ["'edalize/lint'"],
            [],
            id="build-dir-fragment",
        ),
    ],
)
def test_unmatched_scope_is_an_error(
    run_lint: LintRun,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    scope: str,
    process: SubprocessResult,
    files: tuple[str, ...],
    present: list[str],
    absent: list[str],
) -> None:
    """A --scope entry that matches no linted file is an ERROR, not a PASS (#2801)."""
    build_root = tmp_path / ".booley/.runtime/edalize/lint/lite"

    result, _ = run_lint(
        process,
        args=("--scope", scope),
        resolved=lambda handle, _root: stub_resolved(files=files, build_root=build_root),
    )

    out = capsys.readouterr().out
    assert result.exit_code == EXIT_ERROR
    assert all(text in out for text in present)
    assert not any(text in out for text in absent)


def test_unmatched_scope_entries() -> None:
    from booley.flows.lint.flow import unmatched_scope_entries

    paths = ["rtl/mod_a.sv", "rtl/mod_b.sv"]
    assert unmatched_scope_entries("rtl/mod_a.sv, nope/ ,,mod_b", paths) == ["nope/"]
    assert unmatched_scope_entries("x/,y/", paths) == ["x/", "y/"]
    assert unmatched_scope_entries("", paths) == []
    assert unmatched_scope_entries("mod_a", []) == ["mod_a"]


@pytest.mark.parametrize(
    ("target", "message"),
    [
        pytest.param("", "Eligible Targets: (none)", id="omitted"),
        pytest.param(",", "lint: no Target selected.", id="no-selector"),
        pytest.param(" ", "lint: --target is required.", id="blank"),
    ],
)
def test_a_run_without_a_target_selects_nothing(
    run_lint: LintRun, monkeypatch: pytest.MonkeyPatch, target: str, message: str
) -> None:
    """An empty selection never sweeps every declared Target (ADR 0030)."""
    # The real catalog of an empty Project: the lenient one invents Targets.
    monkeypatch.setattr(TargetCatalog, "build", _REAL_CATALOG_BUILD)

    result, commands = run_lint(_process(), target=target)

    assert result.exit_code == EXIT_ERROR
    assert message in result.outcome.report_text
    assert commands == []


SAMPLE_VERIBLE_OUTPUT = """\
rtl/top.sv:4:11: Interface names must use lower_snake_case naming convention. [interface-name-style]
rtl/top.sv:10:1-5: Remove trailing spaces. [no-trailing-spaces]
rtl/other.sv:2:3: Explicitly define a storage type for every parameter. [explicit-parameter-storage-type]
"""

SAMPLE_VERIBLE_PARSE_ERROR = """\
rtl/top.sv:3:1: syntax error at token "endmodule"
"""


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        pytest.param(
            SAMPLE_VERIBLE_OUTPUT,
            [
                ("interface-name-style", "rtl/top.sv", 4, 11),
                ("no-trailing-spaces", "rtl/top.sv", 10, 1),  # a column range keeps its start
                ("explicit-parameter-storage-type", "rtl/other.sv", 2, 3),
            ],
            id="findings",
        ),
        # A syntax-error line has no [rule]: it is the rc!=0 error signal (QA-7).
        pytest.param(SAMPLE_VERIBLE_PARSE_ERROR, [], id="parse-error"),
    ],
)
def test_parse_verible_warnings(output: str, expected: list[tuple[str, str, int, int]]) -> None:
    warnings = parse_verible_warnings(output, "lint_style")

    assert [(w.rule, w.file, w.line, w.col) for w in warnings] == expected
    assert {w.target for w in warnings} <= {"lint_style"}


@pytest.mark.parametrize(
    ("linter", "process", "exit_code", "summary"),
    [
        # --parse_fatal without --lint_fatal: findings arrive with rc 0 (ADR 0033 decision 5).
        pytest.param(
            "verible",
            _process(stdout=SAMPLE_VERIBLE_OUTPUT),
            EXIT_FAILURE,
            "RESULT: WARN (3 warnings)",
            id="verible-findings",
        ),
        # QA-7: a parse failure has no findings; it is a design FAIL, never a PASS (F-29).
        pytest.param(
            "verible",
            _process(1, stderr=SAMPLE_VERIBLE_PARSE_ERROR),
            EXIT_FAILURE,
            "RESULT: FAIL — lite [verible]: rtl/top.sv:3:1: syntax error",
            id="verible-parse-error",
        ),
        pytest.param(
            "verilator",
            _process(0, stdout="%Error: dut.sv:7: syntax error\n"),
            EXIT_FAILURE,
            "RESULT: FAIL — lite [verilator]: %Error: dut.sv:7: syntax error",
            id="verilator-exit-zero-error",
        ),
    ],
)
def test_verible_and_exit_zero_output_grade_the_run(
    run_lint: LintRun,
    monkeypatch: pytest.MonkeyPatch,
    linter: str,
    process: SubprocessResult,
    exit_code: int,
    summary: str,
) -> None:
    resolved = _tools_by_target(monkeypatch, {"lite": linter})

    result, _ = run_lint(process, resolved=resolved)

    assert result.exit_code == exit_code
    assert result.outcome.report_text.startswith(summary)


@pytest.mark.parametrize(
    ("linter", "process", "fragments"),
    [
        pytest.param(
            "verilator",
            _process(2, stderr="/bin/sh: 1: verilator: not found\nmake: *** [lint] Error 127\n"),
            ["missing tool: verilator", "Sandbox Image", "booley doctor"],
            id="verilator-missing",
        ),
        # An image predating the Verible binary names cause and fix (ADR 0033 decision 8).
        pytest.param(
            "verible",
            _process(127, stderr="make: verible-verilog-lint: No such file or directory\n"),
            ["missing tool: verible-verilog-lint", "Sandbox Image", "booley doctor"],
            id="verible-missing",
        ),
        *(
            pytest.param(
                linter,
                _process(rc, stderr=f"{binary}: error while loading shared libraries: libx.so"),
                ["libx.so"],
                id=f"{linter}-loader-rc{rc}",
            )
            for linter, binary in (
                ("verilator", "verilator_bin"),
                ("verible", "verible-verilog-lint"),
            )
            for rc in (0, 127)
        ),
    ],
)
def test_a_linter_that_cannot_start_is_an_error(
    run_lint: LintRun,
    monkeypatch: pytest.MonkeyPatch,
    linter: str,
    process: SubprocessResult,
    fragments: list[str],
) -> None:
    """No verdict was reached: ERROR, never a design FAIL or a clean PASS (035d2d9fd)."""
    resolved = _tools_by_target(monkeypatch, {"lite": linter})

    result, _ = run_lint(process, resolved=resolved, measured=True)

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text.startswith(f"RESULT: ERROR — lite [{linter}]: ")
    assert all(fragment in result.outcome.report_text for fragment in fragments)


def test_timeout_names_the_requested_seconds(run_lint: LintRun) -> None:
    result, _ = run_lint(_process(-1, timed_out=True), args=("--timeout-ms", "60000"))

    assert "Timed out after 60s" in result.outcome.report_text


@pytest.mark.parametrize("target", ["lite", "lite,full"])
def test_run_names_its_linter(
    run_lint: LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
) -> None:
    """The linter comes from the resolved Target, so every output says which one ran."""
    selectors = target.split(",")
    resolved = _tools_by_target(monkeypatch, dict.fromkeys(selectors, "verible"))

    result, _ = run_lint(
        _process(),
        target=target,
        args=("--report-dir", str(tmp_path / "reports")),
        resolved=resolved,
    )

    by_target = dict.fromkeys(selectors, "verible")
    assert "[verible]" in capsys.readouterr().out
    assert "linter: verible" in result.outcome.display_lines
    assert result.outcome.detail["eda_tools"] == by_target
    report = json.loads((tmp_path / "reports/lint_report.json").read_bytes())
    assert report["eda_tools"] == by_target
    assert [r["eda_tool"] for r in report["target_results"]] == ["verible"] * len(selectors)
    invocation = json.loads(next((tmp_path / "reports/lint").glob("*/report.json")).read_bytes())
    expected = {"eda_tool": "verible"} if len(selectors) == 1 else {"eda_tools": by_target}
    assert {key: invocation[key] for key in ("eda_tool", "eda_tools") if key in invocation} == (
        expected
    )


@pytest.mark.parametrize(
    ("module", "exit_code"),
    [
        pytest.param("other", EXIT_ERROR, id="toplevel-excluded"),
        pytest.param("design_top", EXIT_SUCCESS, id="toplevel-linted"),
        # A source that cannot be read counts as declaring it: no invented failure.
        pytest.param(None, EXIT_SUCCESS, id="unreadable-source"),
    ],
)
def test_a_target_must_lint_its_toplevel(
    run_lint: LintRun,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    module: str | None,
    exit_code: int,
) -> None:
    """A fileset without the toplevel would lint nothing real: ERROR, and no linter runs."""
    if module is not None:
        (tmp_path / "top.sv").write_text(f"module {module}; endmodule\n", encoding="utf-8")

    result, commands = run_lint(
        _process(),
        resolved=lambda handle, _root: stub_resolved(
            files=("top.sv",), build_root=tmp_path, toplevel="design_top"
        ),
    )

    out = capsys.readouterr().out
    assert result.exit_code == exit_code
    vacuous = "toplevel 'design_top' is not declared by any fileset file" in out
    assert vacuous is (exit_code == EXIT_ERROR)
    assert len(commands) == int(not vacuous)
    assert ("1 files" in out) is not vacuous


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


@pytest.mark.parametrize("spelling", ["verible", "veriblelint"])
def test_authored_verible_public_plan(
    run_lint: LintRun, tmp_path: Path, capsys: pytest.CaptureFixture[str], spelling: str
) -> None:
    _author_verible_alias_core(tmp_path, spelling)

    result, _ = run_lint(target="lint_style", args=("--dry-run",))

    plan = _dry_run_plan(capsys)
    assert plan["aggregate_errors"] == []
    assert result.exit_code == EXIT_SUCCESS
    commands = plan["work_units"][0]["commands"]
    assert any("verible-verilog-lint" in str(command["argv"]) for command in commands)
    assert any(f"tool_{spelling}" in str(command["argv"]) for command in commands)


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
    run_lint: LintRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
) -> None:
    _author_verible_alias_core(tmp_path, spelling)
    real_fusesoc_setup(monkeypatch, isolated_verible_fusesoc(tmp_path, spelling), tmp_path)

    result, commands = run_lint(
        _process(stdout=SAMPLE_VERIBLE_OUTPUT), target="lint_style", resolved=None
    )

    output = capsys.readouterr().out
    (command,) = commands
    assert "verible-verilog-lint" in command[2], output
    assert "interface-name-style" in output
    assert result.exit_code == EXIT_FAILURE
    assert "RESULT: WARN" in output
    assert result.outcome.detail["eda_tools"] == {"lint_style": spelling}
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
    assert all(path in makefile for path in ("rtl/top.sv", "lint/waivers.txt", "lint/extra.txt"))
    edam_path = next(tmp_path.rglob("*.eda.yml"))
    _assert_verible_rule_path(makefile, edam_path)
    assert f"tool: {spelling}" in edam_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("configured", ["verible", "veriblelint"])
def test_alias_preparation_compares_parser_families(
    run_lint: LintRun, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    """A Target declaring one Verible spelling may resolve to the other."""
    _lenient_catalog_declaring(monkeypatch, "veriblelint")

    result, commands = run_lint(_process(), resolved=lambda *_: stub_resolved(configured))

    assert result.exit_code == EXIT_SUCCESS
    (command,) = commands
    assert "command -v verible-verilog-lint" in command[2]


_SIM_CORE = (
    "CAPI=2:\nname: ::sim_demo:0\n"
    "filesets:\n  rtl:\n    files:\n      - rtl/top.sv: {file_type: systemVerilogSource}\n"
    "targets:\n  lint_style:\n    flow: sim\n    flow_options:\n      tool: verilator\n"
    "    filesets: [rtl]\n    toplevel: top\n"
)


@pytest.mark.parametrize(
    ("spelling", "reason"),
    [
        pytest.param(None, "cannot be driven by the 'lint' Flow", id="sim-target"),
        *(
            pytest.param(spelling, "default_tool does not supply it", id=spelling)
            for spelling in ("verilator", "verible", "veriblelint")
        ),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_incompatible_target_is_rejected_before_setup(
    run_lint: LintRun, tmp_path: Path, spelling: str | None, reason: str, dry_run: bool
) -> None:
    if spelling is None:
        (tmp_path / "sim_demo.core").write_text(_SIM_CORE, encoding="utf-8")
    else:
        _author_verible_alias_core(tmp_path, spelling)
        core = tmp_path / "style.core"
        core.write_text(
            core.read_text(encoding="utf-8").replace(
                f"    flow_options:\n      tool: {spelling}\n",
                f"    default_tool: {spelling}\n    flow_options:\n",
            ),
            encoding="utf-8",
        )

    def resolved(*_args: object) -> object:
        raise AssertionError("incompatible Target set up")

    result, commands = run_lint(
        _process(), target="lint_style", args=("--dry-run",) * dry_run, resolved=resolved
    )

    assert result.exit_code == EXIT_ERROR
    assert reason in result.outcome.report_text
    assert commands == []


def _width_warnings(count: int, path: str = "f.sv") -> str:
    return "".join(f"%Warning-WIDTH: {path}:{i + 1}:1: message {i}\n" for i in range(count))


@pytest.mark.parametrize("count", [0, 20, 21])
def test_job_report_lint_findings_cap(run_lint: LintRun, tmp_path: Path, count: int) -> None:
    result, _ = run_lint(
        _process(stdout=_width_warnings(count)), args=("--report-dir", str(tmp_path / "reports"))
    )

    detail = result.outcome.detail
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
    assert (detail["targets"], detail["total_warnings"]) == (["lite"], count)
    full = json.loads((tmp_path / detail["artifacts"]["report"]).read_bytes())
    assert (len(full["warnings"]), full["errors"], full["passed"]) == (count, [], count == 0)
    assert result.exit_code == (EXIT_FAILURE if count else EXIT_SUCCESS)


@pytest.mark.parametrize("warning_count", [1, 19, 20])
def test_job_report_lists_warnings_then_errors_by_target(
    run_lint: LintRun, warning_count: int
) -> None:
    """Scoped warnings fill the bounded list first; every Target's warnings and errors are counted."""
    warnings = _width_warnings(warning_count, "rtl/a.sv")
    outside = "%Warning-WIDTH: vendor/a.sv:1:1: outside\n"
    result, _ = run_lint(
        _process(stdout=warnings + outside),
        _process(1, stdout=warnings + "%Error: rtl/b.sv:1:1: bad\n"),
        _process(1, stdout="%Error: rtl/c.sv:1:1: worse\n"),
        target="first,second,empty",
        args=("--scope", "rtl/"),
    )

    detail = result.outcome.detail
    total = warning_count + 2
    assert (detail["findings_total"], detail["findings_truncated"]) == (total, total > 20)
    listed = detail["findings"]
    assert len(listed) == min(20, total)
    assert {f["target"] for f in listed[:warning_count]} == {"first"}
    log = "(full log: flow-reports/lint/1/artifacts/{}/run.log)"
    errors = [(f["severity"], f["target"], f["message"]) for f in listed[warning_count:]]
    assert (
        errors
        == [
            ("error", "second", "%Error: rtl/b.sv:1:1: bad " + log.format("second")),
            ("error", "empty", "%Error: rtl/c.sv:1:1: worse " + log.format("empty")),
        ][: 20 - warning_count]
    )
    assert detail["findings_by_target"] == {
        "first": {"warnings": warning_count, "errors": 0},
        "second": {"warnings": warning_count, "errors": 1},
        "empty": {"warnings": 0, "errors": 1},
    }


def test_lint_label_carries_target_source_bytes(run_lint: LintRun, tmp_path: Path) -> None:
    """The linter's resource-history record carries the Target's source size."""
    from booley.runtime import resource_history

    (tmp_path / "top.sv").write_bytes(b"module top; endmodule\n")
    (tmp_path / "pkg.sv").write_bytes(b"package p; endpackage\n")

    run_lint(
        _process(),
        measured=True,
        resolved=lambda *_: stub_resolved(files=("top.sv", "pkg.sv"), build_root=tmp_path),
    )

    (record,) = resource_history.load_records(tmp_path)
    assert (record["key"]["target"], record["source_bytes"]) == ("lite", 44)


def test_linter_output_relays_project_paths(
    run_lint: LintRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relayed linter lines name Project paths, not FuseSoC's build-tree copies (#5315)."""
    import io

    from booley.flows.terminal_progress import TerminalProgress

    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    _author_cross_core_project(work_dir)
    real_fusesoc_setup(monkeypatch, isolated_verible_fusesoc(tmp_path, "verible"), tmp_path)
    # --quiet leaves this tool-output sink in place of the CLI's own progress display.
    sink = TerminalProgress("lint", "lint_b", work_dir, stream=io.StringIO(), tool_output=True)

    with sink.installed():
        run_lint(
            _process(stdout=_UNUSED_B),
            args=("--quiet",),
            target="lint_b",
            work_dir=work_dir,
            resolved=None,
            measured=True,
        )

    assert "ip/b/rtl/top.sv:1:8" in sink.stream.getvalue()
    assert "src/b_0/" not in sink.stream.getvalue()


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


@pytest.mark.parametrize(
    ("linter", "process", "expected"),
    [
        pytest.param(
            "verilator",
            _process(2, stdout=_VERILATOR_FINDINGS),
            {
                "plain": {"warnings": 2, "errors": 2},
                "counted_items": {
                    "errors": [
                        "rtl/x.v:1: Can't find definition of variable: 'zzz'",
                        "rtl/x.v:9: second diagnostic",
                    ],
                    "warnings": [
                        "rtl/a.v:3 UNUSEDSIGNAL: Signal is not used: 'x'",
                        "rtl/b.v:9 WIDTH: Operator ASSIGNW expects 8 bits",
                    ],
                },
            },
            id="verilator-findings",
        ),
        pytest.param(
            "verible",
            _process(1, stdout=_VERIBLE_FINDINGS),
            {
                "plain": {"warnings": 2, "errors": 2},
                "counted_items": {
                    "errors": [
                        'rtl/top.sv:3: syntax error at token "endmodule"',
                        'rtl/top.sv:5: syntax error at token "foo"',
                    ],
                    "warnings": [
                        "rtl/top.sv:4 interface-name-style: Interface names must end with _if.",
                        "rtl/top.sv:9 line-length: Line length exceeds 100.",
                    ],
                },
            },
            id="verible-findings",
        ),
        pytest.param(
            "verilator",
            _process(),
            {"plain": {"warnings": 0, "errors": 0}, "counted_items": {}},
            id="clean",
        ),
        pytest.param(
            "verilator",
            _process(
                2,
                stdout="%Warning-WIDTH: rtl/b.v:9:1: Operator ASSIGNW expects 8 bits\n"
                "%Error: Exiting due to 1 warning(s)\n",
            ),
            {"plain": {"warnings": 1, "errors": 0}},
            id="warnings-only-epilogue",
        ),
        pytest.param(
            "verilator",
            _process(-9, timed_out=True),
            {"unavailable": "Timed out after 120s"},
            id="could-not-run",
        ),
        pytest.param(
            "verilator",
            _process(3, stdout="boom\n"),
            {
                "unavailable": "lint eda_tool exited 3 (full log: flow-reports/lint/1/artifacts/lite/run.log)"
            },
            id="rejected-without-readable-error",
        ),
    ],
)
def test_run_returns_unique_warning_and_error_counts(
    run_lint: LintRun,
    monkeypatch: pytest.MonkeyPatch,
    linter: str,
    process: SubprocessResult,
    expected: dict[str, object],
) -> None:
    """A Lint run's typed metrics count unique warnings and errors, or say why there are none."""
    resolved = _tools_by_target(monkeypatch, {"lite": linter})

    result, _ = run_lint(process, resolved=resolved)

    metrics = result.outcome.detail["metrics"]["lite"]
    assert {key: metrics[key] for key in expected} == expected


def test_listed_findings_are_capped_and_cut(run_lint: LintRun) -> None:
    output = "".join(
        f"%Warning-WIDTH: rtl/w.v:{line}:1: {'long ' * 60 if line == 1 else 'wide'}\n"
        for line in range(1, 26)
    )

    result, _ = run_lint(_process(stdout=output))

    metrics = result.outcome.detail["metrics"]["lite"]
    assert metrics["plain"]["warnings"] == 25
    listed = metrics["counted_items"]["warnings"]
    assert len(listed) == 20
    assert listed[1] == "rtl/w.v:2 WIDTH: wide"
    assert len(listed[0]) == 200


def test_lint_error_findings_verilator() -> None:
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
    run_lint: LintRun,
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
    real_fusesoc_setup(monkeypatch, isolated_verible_fusesoc(tmp_path, "verible"), tmp_path)

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
