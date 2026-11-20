"""Tests for the built-in fpga_impl Booley Flow."""

from __future__ import annotations

import copy
import dataclasses
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from booley.core.build_paths import work_root_for
from booley.evidence.timing import make_clock_timing
from booley.flows import edam as edam_layer
from booley.flows import endpoint_admission, run_evidence
from booley.flows.base import SubprocessResult
from booley.flows.edam import EdamFile
from booley.flows.fpga.backends.vivado import edam as fpga_edam
from booley.flows.fpga.backends.vivado.metrics import FpgaMetrics

# The run-outcome tests below stub preparation with it; nothing here does.
from booley.flows.fpga.flow import FpgaImplFlow, _PreparedFpgaCommand
from booley.flows.fpga.profiles import VIVADO_PROFILES
from booley.flows.implementation_comparison import target_pair_plans_for_handles
from booley.flows.job_estimates import JobRequest
from booley.flows.progress_lifecycle import abort_open_progress
from booley.flows.target_parameters import ParameterValue
from booley.fusesoc import fusesoc_registry
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.runtime import resource_history, runtime_context
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle
from tests.canned_process_support import canned_command
from tests.subprocess_budget import run_bounded_child
from tests.target_test_support import install_lenient_target_catalog, make_target_handle

_REAL_CATALOG_BUILD = TargetCatalog.build
# Captured before the autouse fixtures patch them, so the real-fusesoc run can
# restore the genuine resolver and setup command.
_REAL_RESOLVE_HANDLE = fusesoc_registry.resolve_target_handle
_REAL_SETUP_HANDLE = fusesoc_registry.setup_command_for_handle


def _layer_target_handle(project_root: Path | str, selector: str) -> TargetHandle:
    return make_target_handle(
        project_root,
        selector,
        flow="generic",
        eda_tool="vivado",
        drivable_by=("fpga",),
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


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return tmp_path / "state.json"


def _write_project_config(
    tmp_path: Path,
    *,
    execution_lines: str = "",
) -> None:
    """Write the fixture project config.

    *execution_lines* can inject a retired spelling to exercise the hard
    migration error.
    """
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir(exist_ok=True)
    (booley_dir / "booley.toml").write_text(
        "[flows.fpga]\n" + execution_lines,
        encoding="utf-8",
    )
    (booley_dir / "configs.toml").write_text(
        '[default]\ntop_module = "dut_top"\ndefines = ["CFG_DEFAULT"]\n',
        encoding="utf-8",
    )
    (tmp_path / "constraints").mkdir()
    (tmp_path / "constraints" / "timing.xdc").write_bytes(b"create_clock\n")
    (tmp_path / "rtl" / "include").mkdir(parents=True)
    (tmp_path / "rtl" / "include" / "defs.svh").write_bytes(b"`define FOO 1\n")
    (tmp_path / "rtl" / "top.sv").write_bytes(b"module dut_top; endmodule\n")
    (tmp_path / "rtl" / "legacy.v").write_bytes(b"module legacy; endmodule\n")
    (tmp_path / "rtl" / "modern.v").write_bytes(b"module modern; endmodule\n")


def _flow(tmp_path: Path, state_file: Path, *extra_args: str) -> FpgaImplFlow:
    return _fpga_flow(tmp_path, *extra_args)


def _collect_flow(work_dir: Path) -> FpgaImplFlow:
    """A minimally-parsed Flow whose ``work_dir`` anchors report relpaths.

    ``_artifact_dirs`` posix-relativizes each directory against
        ``self.args.work_dir``, so a bare ``FpgaImplFlow()`` (no ``parse_args``)
        would raise on ``self.args``. This gives just enough for that method.
    """
    flow = FpgaImplFlow()
    flow.parse_args(["--target", "default", "--work-dir", str(work_dir)])
    return flow


def _fake_fpga_resolved(
    work_dir: Path,
    *,
    config: str = "default",
    toplevel: str = "dut_top",
) -> ResolvedTarget:
    """A ResolvedTarget shaped like a real vivado-flow EDAM.

    ``build_root`` is *work_dir* so each ``ResolvedFile.absolute()`` lands on the
    real file ``_write_project_config`` created — the Edalize ``configure()`` in
    the integration tests then sees genuine sources. The header is an
    ``is_include_file`` (surfaced as an include dir, not a source); the TB file
    is ``tb``-tagged and excluded from the vivado source set.
    """
    build_root = Path(work_dir)
    files = (
        ResolvedFile(
            name="rtl/include/defs.svh",
            file_type="systemVerilogSource",
            is_include=True,
        ),
        ResolvedFile(name="rtl/top.sv", file_type="systemVerilogSource"),
        ResolvedFile(name="rtl/legacy.v", file_type="verilogSource"),
        # SystemVerilog in a .v file: the declared type, not the suffix, counts.
        ResolvedFile(name="rtl/modern.v", file_type="systemVerilogSource"),
        # ADR 0031: XDC travels with the Target as a file_type:xdc fileset (the
        # sole source of truth now that the legacy [flows.fpga].xdc key is
        # gone). TB-tagged constraints would be excluded, like sdc_files.
        ResolvedFile(name="constraints/timing.xdc", file_type="xdc"),
        ResolvedFile(name="tb/tb_top.sv", file_type="systemVerilogSource", tags=("tb",)),
    )
    params = {
        "WIDTH": {"datatype": "int", "paramtype": "vlogparam", "default": 8},
        "CFG_DEFAULT": {"datatype": "bool", "paramtype": "vlogdefine", "default": True},
    }
    return ResolvedTarget(
        name=config,
        vlnv="::fpga_demo:0",
        toplevel=toplevel,
        eda_tool="vivado",
        flow_options={"tool": "vivado", "part": "xc7a200tfbg484-1"},
        files=files,
        parameters=params,
        build_root=build_root,
        edam_path=build_root / "fpga_demo_0.eda.yml",
    )


@dataclasses.dataclass
class _Resolution:
    """What the stubbed FuseSoC resolution saw, and how a test bends its answer."""

    seen: list[tuple[str, Path, Path]] = dataclasses.field(default_factory=list)
    edit: Callable[[ResolvedTarget], ResolvedTarget] = lambda resolved: resolved
    errors: dict[str, Exception] = dataclasses.field(default_factory=dict)


def _with_options(**options: object) -> Callable[[ResolvedTarget], ResolvedTarget]:
    return lambda resolved: dataclasses.replace(
        resolved, flow_options={**resolved.flow_options, **options}
    )


def _with_parameters(
    **parameters: dict[str, object],
) -> Callable[[ResolvedTarget], ResolvedTarget]:
    return lambda resolved: dataclasses.replace(resolved, parameters=parameters)


@pytest.fixture(autouse=True)
def resolution(_adr0039_lenient_selection):
    """Default every test's FuseSoC resolution to a fake vivado-flow EDAM.

    Without it, preparation would shell out to a real ``fusesoc run --setup``
    against a project with no ``.core``. The stub resolves from the handle's
    Project Root, so a baseline worktree resolves its own files; tests bend the
    answer through the yielded ``_Resolution``.
    """
    resolution = _Resolution()

    def resolve(handle, *, build_root, **_kwargs):
        root = Path(handle.project_root)
        resolution.seen.append((handle.selector, root, Path(build_root)))
        if handle.selector in resolution.errors:
            raise resolution.errors[handle.selector]
        return resolution.edit(_fake_fpga_resolved(root, config=handle.selector))

    with patch.object(fusesoc_registry, "resolve_target_handle", side_effect=resolve):
        yield resolution


# A Python stand-in for Vivado: records each call, then leaves the routed
# reports and bitstream the route witness reads (nothing when it "crashes").
_FAKE_VIVADO = """\
import sys
from pathlib import Path
work_root, returncode, route = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "1"
with (work_root.parent / "vivado-calls").open("a") as calls:
    calls.write("x")
impl = work_root / "top.runs" / "impl_1"
impl.mkdir(parents=True, exist_ok=True)
if route:
    (impl / "runme.log").write_text("route_design completed successfully\\n")
    for name in ("utilization_placed", "timing_summary_routed", "drc_routed"):
        (impl / f"top_{name}.rpt").write_text(name)
    (impl / "top.bit").write_bytes(b"bit")
print(sys.argv[4])
sys.exit(returncode)
"""


@pytest.fixture
def vivado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run the whole FPGA Flow on real Edalize configure with a fake Vivado process.

    Yields the state a test bends (``parsed`` reports, process ``returncode``)
    and the ``recipe`` the Flow built.
    """
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(endpoint_admission, "required_seats", lambda _tools, _config: ())
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    script = tmp_path / "fake_vivado.py"
    script.write_bytes(_FAKE_VIVADO.encode())
    state: dict = {
        "parsed": {"status": "pass", "lut_count": 1, "ff_count": 1, "wns_ns": 0.1, "whs_ns": 0.1},
        "returncode": 0,
    }
    real_build = fpga_edam.build_fpga_edam

    def build(recipe, **kwargs):
        state["recipe"] = recipe
        return real_build(recipe, **kwargs)

    def run_command(work_root, _work_dir):
        route = state.get("route", state["returncode"] == 0)
        output = "routed"
        if "output" in state:
            build = Path(state["recipe"].resolved.build_root).resolve()
            output = state["output"].format(
                build=build.as_posix(), rel=os.path.relpath(build, work_root)
            )
        return [
            sys.executable,
            str(script),
            str(work_root),
            str(state["returncode"]),
            str(int(route)),
            output,
        ]

    monkeypatch.setattr(fpga_edam, "build_fpga_edam", build)
    monkeypatch.setattr(fpga_edam, "fpga_run_command", run_command)
    monkeypatch.setattr(fpga_edam, "parse_fpga_reports", lambda _text: dict(state["parsed"]))
    return state


def _fpga_flow(project: Path, *args: str, targets: str = "default") -> FpgaImplFlow:
    flow = FpgaImplFlow()
    flow.parse_args(
        [
            "--target",
            targets,
            "--work-dir",
            str(project),
            "--report-dir",
            str(project / "reports"),
            *args,
        ]
    )
    return flow


def _run_fpga(project: Path, *args: str, targets: str = "default"):
    """Run the Flow as the CLI does; return its outcome and ``fpga_default.json`` (or None)."""
    outcome = _fpga_flow(project, *args, targets=targets).execute_prepared().outcome
    report = project / "reports" / "fpga_default.json"
    return outcome, json.loads(report.read_text(encoding="utf-8")) if report.exists() else None


def _vivado_calls(project: Path) -> int:
    calls = project / ".booley" / ".runtime" / "edalize" / "fpga" / "vivado-calls"
    return len(calls.read_text(encoding="utf-8")) if calls.exists() else 0


def _project_tcl(project: Path) -> str:
    tcl = project / ".booley/.runtime/edalize/fpga/default/fpga_default.tcl"
    return tcl.read_text(encoding="utf-8")


# Edalize leaves its Tcl file handle unclosed when it regenerates a project, and a
# second run of the same Target does exactly that.
_EDALIZE_LEAKS_HANDLE = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)


@_EDALIZE_LEAKS_HANDLE
def test_cache_hit_omits_unauthenticated_shared_flow_log(tmp_path: Path, vivado: dict) -> None:
    _write_project_config(tmp_path)
    _run_fpga(tmp_path)
    work_root = work_root_for(tmp_path, "fpga", "default")
    (work_root / "run.log").write_text("stale unrelated output", encoding="utf-8")

    _, report = _run_fpga(tmp_path)

    assert report["cached"] is True
    assert report["metrics"]["log_path"] == ""
    assert _vivado_calls(tmp_path) == 1


@_EDALIZE_LEAKS_HANDLE
def test_cached_route_reused_only_within_latch_allowance(tmp_path: Path, vivado: dict) -> None:
    """A cached route with one latch is reused only while the allowance covers it."""
    _write_project_config(tmp_path, execution_lines="expected_latches = 1\n")
    vivado["parsed"]["latch_count"] = 1
    first, _ = _run_fpga(tmp_path)
    second, report = _run_fpga(tmp_path)
    assert (first.exit_code, second.exit_code, report["cached"]) == (EXIT_SUCCESS,) * 2 + (True,)
    assert _vivado_calls(tmp_path) == 1

    (tmp_path / ".booley" / "booley.toml").write_bytes(b"[flows.fpga]\n")
    third, report = _run_fpga(tmp_path)

    assert third.exit_code == EXIT_FAILURE
    assert report["cached"] is False
    assert _vivado_calls(tmp_path) == 2


@_EDALIZE_LEAKS_HANDLE
def test_numbered_log_publication_failure_does_not_skip_cache_store(
    tmp_path: Path, vivado: dict
) -> None:
    _write_project_config(tmp_path)
    with patch("booley.flows.fpga.flow.artifacts.publish_bytes", side_effect=OSError("disk full")):
        first, report = _run_fpga(tmp_path)
    assert first.exit_code == EXIT_SUCCESS
    assert report["metrics"]["log_path"] == ""

    _, report = _run_fpga(tmp_path)

    assert report["cached"] is True
    assert _vivado_calls(tmp_path) == 1


def test_fpga_label_carries_target_source_bytes(tmp_path: Path, vivado: dict) -> None:
    _write_project_config(tmp_path)
    # The Target's testbench file counts too: its size is known only if it exists.
    (tmp_path / "tb").mkdir()
    (tmp_path / "tb" / "tb_top.sv").write_bytes(b"module tb_top; endmodule\n")
    outcome, _ = _run_fpga(tmp_path)
    assert outcome.exit_code == EXIT_SUCCESS

    history = json.loads((tmp_path / "runtime/resources/history.json").read_text(encoding="utf-8"))
    (record,) = history["records"]
    assert record["key"]["target"] == "default"
    assert record["key"]["phase"] == "fpga_impl"
    assert record["source_bytes"] == 128  # the six fixture files resolved for the Target


@pytest.mark.parametrize(
    ("returncode", "route", "parsed", "exit_code", "outcome"),
    [
        (2, True, None, EXIT_SUCCESS, "exit_zero"),
        (1, False, {}, EXIT_ERROR, "exit_nonzero"),
        (0, False, {}, EXIT_ERROR, "exit_nonzero"),
    ],
    ids=["boardless", "no-route", "exit-zero-no-route"],
)
def test_fpga_impl_record_outcome_follows_route_completion(
    tmp_path: Path,
    vivado: dict,
    returncode: int,
    route: bool,
    parsed,
    exit_code: int,
    outcome: str,
) -> None:
    _write_project_config(tmp_path)
    vivado.update(returncode=returncode, route=route)
    if parsed is not None:
        vivado["parsed"] = parsed

    run, _ = _run_fpga(tmp_path)

    assert run.exit_code == exit_code
    history = json.loads((tmp_path / "runtime/resources/history.json").read_text(encoding="utf-8"))
    (record,) = history["records"]
    assert (record["outcome"], record["returncode"]) == (outcome, returncode)


@pytest.mark.parametrize("named_state", [True, False], ids=["state-file", "no-state-file"])
@pytest.mark.parametrize(
    ("wns_ns", "returncode", "exit_code", "verdict"),
    [(0.2, 0, EXIT_SUCCESS, 0), (-0.01, 0, EXIT_FAILURE, 0), (0.2, 1, EXIT_ERROR, 2)],
    ids=["pass", "fail", "crash"],
)
def test_fpga_run_records_no_criterion(
    tmp_path: Path,
    state_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    vivado: dict,
    wns_ns: float,
    returncode: int,
    exit_code: int,
    verdict: int,
    named_state: bool,
) -> None:
    """A hand-run FPGA Flow keeps its exit status and metric verdict, records no
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

    before = b'{"opaque": true}\n'
    if named_state:
        state_file.write_bytes(before)
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_file))
    _write_project_config(tmp_path)
    vivado["returncode"] = returncode
    # A crash before routing leaves no report to parse.
    vivado["parsed"] = (
        {}
        if returncode
        else {**vivado["parsed"], "lut_count": 1200, "ff_count": 700, "wns_ns": wns_ns}
    )

    result, report = _run_fpga(tmp_path)

    assert result.exit_code == exit_code
    if wns_ns < 0:
        assert report["passed"] is False
        assert report["implementation"]["status"]["grade"] == "fail"
    if named_state:
        assert state_file.read_bytes() == before
    else:
        assert list(tmp_path.rglob("state.json")) == []
    assert not (tmp_path / "logs").exists()
    rules = TableRules(
        "t",
        "vlnv:x#t",
        "fpga",
        (Bound("lut_count_max", "lut_count", "max", 1500, None, None, None),),
        (),
    )
    values = report_metric_values("fpga", result.detail).get("default")
    outcome = (
        FlowValues(values, values.clocks)
        if isinstance(values, FlowMetricValues)
        else FlowFailed("fpga returned no values")
    )
    judged = evaluate_table(rules, outcome)
    assert measure_exit_code(item.verdict for item in judged) == verdict


def _git(repo: Path, *args: str) -> str:
    return run_bounded_child(["git", *args], cwd=repo, check=True).stdout.strip()


def _commit_project(tmp_path: Path) -> None:
    """Commit the fixture Project so ``--baseline HEAD`` has a revision to check out."""
    _write_project_config(tmp_path)
    (tmp_path / ".gitignore").write_bytes(
        b".booley/.runtime/\nreports/\nruntime/\nfake_vivado.py\n"
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "project")


def test_baseline_runs_the_candidate_target_at_the_baseline_revision(
    tmp_path: Path, vivado: dict, resolution: _Resolution
) -> None:
    _commit_project(tmp_path)
    flow = _fpga_flow(tmp_path, "--baseline", "HEAD")

    outcome = flow.execute_prepared().outcome

    assert outcome.exit_code == EXIT_SUCCESS
    report = json.loads((tmp_path / "reports/fpga_default.json").read_text(encoding="utf-8"))
    assert (report["baseline_target"], report["candidate_target"]) == ("default", "default")
    assert report["baseline_metrics"]["lut_count"] == 1
    # Resolution saw the candidate Project, then the baseline worktree's copy.
    roots = [root.resolve() for selector, root, _ in resolution.seen if selector == "default"]
    assert roots[0] == tmp_path.resolve()
    assert any(root != tmp_path.resolve() for root in roots)
    assert (tmp_path / report["metrics"]["artifacts"]["live_dirs"]["build"]).is_dir()
    assert list(tmp_path.glob(".baseline-wt-*")) == []
    # The baseline recipe is planned at that revision before either side runs.
    plan, _ = _run_fpga(tmp_path, "--baseline", "HEAD", "--dry-run")
    baseline_units = [unit for unit in plan.detail["work_units"] if unit["role"] == "baseline"]
    assert [(unit["selector"], unit["revision"]) for unit in baseline_units] == [
        ("default", _git(tmp_path, "rev-parse", "HEAD"))
    ]
    assert flow.memory_job_requests((_layer_target_handle(tmp_path, "default"),)) == (
        JobRequest("fpga", "default"),
    )


def test_baseline_setup_records_keep_their_outcome_when_vivado_fails(
    tmp_path: Path, vivado: dict
) -> None:
    """Judging a Target's failed Vivado run must not rewrite its successful setups."""
    _commit_project(tmp_path)
    vivado.update(returncode=1, route=False)
    real_resolve = fusesoc_registry.resolve_target_handle  # the autouse fake

    def resolve(handle, **kwargs):
        # The real resolver runs this command as the setup, through the Flow's runner.
        kwargs.get("runner", subprocess.run)(
            canned_command(SubprocessResult(returncode=0)),
            cwd=handle.project_root,
            env=dict(os.environ),
        )
        return real_resolve(handle, **kwargs)

    with patch.object(fusesoc_registry, "resolve_target_handle", side_effect=resolve):
        outcome = _fpga_flow(tmp_path, "--baseline", "HEAD").execute_prepared().outcome

    assert outcome.exit_code == EXIT_ERROR
    outcomes: dict[str, list[str]] = {}
    for record in resource_history.load_records(tmp_path):
        outcomes.setdefault(record["key"]["phase"], []).append(record["outcome"])
    assert outcomes["fpga_prepare"] == ["exit_zero"] * len(outcomes["fpga_prepare"])
    assert len(outcomes["fpga_prepare"]) == 3
    assert set(outcomes["fpga_impl"]) == {"exit_nonzero"}


@pytest.mark.parametrize(
    ("args", "edit", "profile", "mapping"),
    [
        pytest.param([], None, "balanced", {"applied": False}, id="default-profile"),
        pytest.param(
            ["--ppa-profile", "max_frequency"],
            _with_options(ppa_profile="compact"),
            "max_frequency",
            {
                "synthesis_strategy": "Flow_PerfOptimized_high",
                "implementation_strategy": "Performance_ExplorePostRoutePhysOpt",
            },
            id="call-profile-wins",
        ),
    ],
)
def test_dry_run_reports_the_resolved_recipe(
    tmp_path: Path, vivado: dict, resolution: _Resolution, args, edit, profile, mapping
) -> None:
    _write_project_config(tmp_path)
    resolution.edit = edit or resolution.edit

    outcome, _ = _run_fpga(tmp_path, "--dry-run", *args)

    assert outcome.exit_code == EXIT_SUCCESS
    (unit,) = outcome.detail["work_units"]
    assert (unit["role"], unit["selector"], unit["eda_tool"], unit["timeout_ms"]) == (
        "candidate",
        "default",
        "vivado",
        7_200_000,
    )
    assert unit["constraints"] == ["constraints/timing.xdc"]
    assert unit["sources"] == ["rtl/top.sv", "rtl/legacy.v", "rtl/modern.v"]
    assert unit["recipe"]["flow_options"]["part"] == "xc7a200tfbg484-1"
    assert unit["recipe"]["ppa_profile"]["name"] == profile
    assert {key: unit["recipe"]["ppa_profile"]["mapping"][key] for key in mapping} == mapping
    assert list(tmp_path.glob(".booley-fpga-plan-*")) == []
    assert _vivado_calls(tmp_path) == 0


def test_dry_and_real_preparation_have_same_semantic_fingerprint(
    tmp_path: Path, vivado: dict
) -> None:
    _write_project_config(tmp_path)
    dry, _ = _run_fpga(tmp_path, "--dry-run", "--ppa-profile", "max_frequency")
    _, report = _run_fpga(tmp_path, "--ppa-profile", "max_frequency")

    assert dry.detail["work_units"][0]["recipe"] == report["recipe_snapshot"]


def test_run_rejects_non_fpga_axis_before_setup(tmp_path: Path, vivado: dict) -> None:
    """The FPGA endpoint must enforce the compatibility shown by target discovery."""
    _write_project_config(tmp_path)
    (tmp_path / "design.core").write_bytes(
        b"CAPI=2:\nname: ::design:0\n"
        b"filesets:\n  rtl:\n    files:\n"
        b"      - rtl/top.sv: {file_type: systemVerilogSource}\n"
        b"targets:\n  synth_core:\n    flow: generic\n"
        b"    flow_options: {tool: yosys}\n    filesets: [rtl]\n    toplevel: dut_top\n"
    )

    outcome, _ = _run_fpga(tmp_path, "--dry-run", targets="synth_core")

    assert outcome.exit_code == EXIT_ERROR
    assert "booley targets --for fpga" in outcome.report_text
    assert _vivado_calls(tmp_path) == 0


@pytest.mark.parametrize("args", [["--dry-run"], []], ids=["dry", "real"])
def test_source_inspection_fault_stops_dry_and_real_runs(
    tmp_path: Path, vivado: dict, args: list[str]
) -> None:
    """Dry-run and dispatch must cross the same source-inspection boundary."""
    _write_project_config(tmp_path)
    fault = fusesoc_registry.FuseSocError("source inspection rejected")

    with patch.object(run_evidence, "capture_flow_source_evidence", side_effect=fault):
        outcome, _ = _run_fpga(tmp_path, *args)

    assert outcome.exit_code == EXIT_ERROR
    assert "source inspection rejected" in outcome.report_text
    for leaked in ("target=default", "part=", "top=", "xdc=", "sv_files=", "v_files="):
        assert leaked not in outcome.report_text
    assert _vivado_calls(tmp_path) == 0
    if args:
        assert outcome.detail["work_units"] == []
        assert outcome.detail["aggregate_errors"] == [
            "candidate default: source inspection rejected"
        ]


def test_dry_run_reports_no_target_metadata_when_later_target_setup_fails(
    tmp_path: Path, vivado: dict, resolution: _Resolution
) -> None:
    _write_project_config(tmp_path)
    resolution.errors["bad"] = fusesoc_registry.FuseSocError("setup rejected bad")

    outcome, _ = _run_fpga(tmp_path, "--dry-run", targets="good,bad")

    assert outcome.exit_code == EXIT_ERROR
    assert "setup rejected bad" in outcome.report_text
    assert [unit["selector"] for unit in outcome.detail["work_units"]] == ["good"]
    assert outcome.detail["aggregate_errors"] == ["candidate bad: setup rejected bad"]
    assert list(tmp_path.glob(".booley-fpga-plan-*")) == []


@pytest.mark.parametrize(
    ("noun", "actual", "declared", "allowed", "excess"),
    [
        pytest.param("latch", 2, "2", 2, 0, id="latch-within"),
        pytest.param("latch", 3, "2", 2, 1, id="latch-excess"),
        pytest.param("loop", 2, "2", 2, 0, id="loop-within"),
        pytest.param("loop", 3, "2", 2, 1, id="loop-excess"),
        pytest.param("latch", 1, None, 0, 1, id="unset-latch"),
        pytest.param("loop", 1, None, 0, 1, id="unset-loop"),
        pytest.param("latch", 1, "true", 0, 1, id="malformed"),
    ],
)
def test_structural_allowance_gates_latches_and_loops(
    tmp_path: Path, vivado: dict, noun, actual, declared, allowed, excess
) -> None:
    """Pass at the declared count, fail above it, and stay strict when it is unset or malformed."""
    plural = {"latch": "latches", "loop": "comb_loops"}[noun]
    count_key = {"latch": "latch_count", "loop": "comb_loop_count"}[noun]
    knob = f"expected_{plural}"
    _write_project_config(tmp_path, execution_lines=f"{knob} = {declared}\n" if declared else "")
    vivado["parsed"] = {**vivado["parsed"], count_key: actual}

    result, report = _run_fpga(tmp_path)

    assert result.exit_code == (EXIT_FAILURE if excess else EXIT_SUCCESS)
    conditions = report["implementation"]["conditions"]
    assert conditions[knob] == allowed
    assert conditions[f"unexpected_{plural}"] == excess
    assert conditions["has_critical"] is bool(excess)
    assert report["passed"] is not bool(excess)
    if excess:
        detail = f"actual={actual}, expected={allowed}, excess={excess}"
        assert detail in result.report_text
        reasons = report["implementation"]["status"]["failure_reasons"]
        assert any(detail in reason for reason in reasons)


def test_profile_patch_precedes_out_of_context_and_is_idempotent(tmp_path: Path) -> None:
    """Each Tcl patch lands once however often it is applied, the profile first."""
    tcl = tmp_path / "fpga_t.tcl"
    tcl.write_text("create_project fpga_t -force\n", encoding="utf-8")

    fpga_edam.apply_ppa_profile(tmp_path, "fpga_t", VIVADO_PROFILES["compact"])
    fpga_edam.apply_ppa_profile(tmp_path, "fpga_t", VIVADO_PROFILES["compact"])
    fpga_edam.enable_out_of_context(tmp_path, "fpga_t")
    fpga_edam.enable_out_of_context(tmp_path, "fpga_t")

    content = tcl.read_text(encoding="utf-8")
    assert content.count("portable FPGA ppa_profile = compact") == 1
    assert content.count("-mode out_of_context") == 1
    assert "set_property strategy Flow_AreaOptimized_high [get_runs synth_1]" in content
    assert "set_property strategy Area_Explore [get_runs impl_1]" in content
    assert content.index("set_property strategy") < content.index(
        "STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS"
    )


def test_balanced_profile_leaves_generated_tcl_byte_identical(tmp_path: Path) -> None:
    tcl = tmp_path / "fpga_t.tcl"
    original = b"create_project fpga_t -force\n"
    tcl.write_bytes(original)

    fpga_edam.apply_ppa_profile(tmp_path, "fpga_t", VIVADO_PROFILES["balanced"])

    assert tcl.read_bytes() == original


@pytest.mark.parametrize(
    ("patch_tcl", "message"),
    [
        (lambda root: fpga_edam.enable_out_of_context(root, "fpga_missing"), "out_of_context"),
        (
            lambda root: fpga_edam.apply_ppa_profile(
                root, "fpga_missing", VIVADO_PROFILES["compact"]
            ),
            r"ppa_profile 'compact'.*not found",
        ),
    ],
    ids=["out-of-context", "profile"],
)
def test_missing_project_tcl_raises(tmp_path: Path, patch_tcl, message: str) -> None:
    """A missing project Tcl is a hard setup error (surfaces as infra_error)."""
    with pytest.raises(FileNotFoundError, match=message):
        patch_tcl(tmp_path)


def test_resolution_forwards_sources_top_and_defines(
    tmp_path: Path, vivado: dict, resolution: _Resolution
) -> None:
    """The resolved RTL sources, include dirs, top, and typed defines reach
    ``build_fpga_edam`` from the one resolved Target."""
    _write_project_config(tmp_path)

    outcome, _ = _run_fpga(tmp_path)

    assert outcome.exit_code == EXIT_SUCCESS
    # The first resolution is the Flow's own: Target, Project Root and an
    # isolated build dir distinct from the Vivado work root.
    selector, root, build_root = resolution.seen[0]
    assert (selector, root) == ("default", tmp_path)
    assert build_root.as_posix().endswith("fpga/default-fusesoc")
    recipe = vivado["recipe"]
    assert recipe.top == "dut_top"
    # Sources keep Target order and declared types, whatever the suffix; the
    # include header is surfaced as a dir, never a compiled source. Compare in
    # POSIX form so a Windows host's backslashes do not fail the checks.
    assert [
        (path.relative_to(tmp_path.resolve()).as_posix(), file_type)
        for path, file_type in recipe.sources
    ] == [
        ("rtl/top.sv", "systemVerilogSource"),
        ("rtl/legacy.v", "verilogSource"),
        ("rtl/modern.v", "systemVerilogSource"),
    ]
    assert [p.relative_to(tmp_path.resolve()).as_posix() for p in recipe.include_dirs] == [
        "rtl/include"
    ]
    assert recipe.defines == ("CFG_DEFAULT",)
    assert dict(recipe.vlogparams) == {"WIDTH": ParameterValue("int", "8")}
    assert recipe.part == "xc7a200tfbg484-1"
    assert [p.relative_to(tmp_path.resolve()).as_posix() for p in recipe.xdc_files] == [
        "constraints/timing.xdc"
    ]


_DEFINES = {
    "SYNTH": {"paramtype": "vlogdefine", "default": True},
    "WIDTH": {"paramtype": "vlogdefine", "default": 16},
    "OFF": {"paramtype": "vlogdefine", "default": False},
    "NONE": {"paramtype": "vlogdefine"},
    "P": {"datatype": "int", "paramtype": "vlogparam", "default": 4},
    "X": {"paramtype": "plusarg", "default": "y"},
}


@pytest.mark.parametrize(
    ("parameters", "lines"),
    [
        pytest.param(
            _DEFINES,
            (
                "set_property verilog_define {SYNTH=1 WIDTH=16 } [get_filesets sources_1]",
                "set_property generic {P=4 } [get_filesets sources_1]",
            ),
            id="mixed",
        ),
        pytest.param({}, (), id="none"),
    ],
)
def test_vlogdefine_parameters_reach_the_project_tcl(
    tmp_path: Path, vivado: dict, resolution: _Resolution, parameters, lines
) -> None:
    """Only true or valued vlogdefine parameters become Verilog defines; a vlogparam
    is a generic."""
    _write_project_config(tmp_path)
    resolution.edit = _with_parameters(**parameters)

    outcome, _ = _run_fpga(tmp_path)

    assert outcome.exit_code == EXIT_SUCCESS
    tcl = _project_tcl(tmp_path)
    for line in lines:
        assert line in tcl.splitlines()
    if not lines:
        assert "verilog_define" not in tcl


def test_materialization_applies_profile_before_out_of_context(
    tmp_path: Path, vivado: dict, resolution: _Resolution
) -> None:
    _write_project_config(tmp_path)
    resolution.edit = _with_options(out_of_context=True)

    outcome, _ = _run_fpga(tmp_path, "--ppa-profile", "compact")

    assert outcome.exit_code == EXIT_SUCCESS
    tcl = _project_tcl(tmp_path)
    assert tcl.index("set_property strategy") < tcl.index("-mode out_of_context")


def _without_xdc(resolved: ResolvedTarget) -> ResolvedTarget:
    files = tuple(f for f in resolved.files if f.file_type != "xdc")
    return dataclasses.replace(resolved, files=files)


def _refuse_configure(*_args, **_kwargs):
    raise RuntimeError("edalize rejected")


@pytest.mark.parametrize(
    ("edit", "error", "configure", "message"),
    [
        pytest.param(
            _with_options(out_of_context="false"),
            None,
            None,
            "out_of_context must be a boolean",
            id="string-ooc",
        ),
        pytest.param(
            _with_options(part=123),
            None,
            None,
            "FPGA part must be a non-empty string",
            id="non-string-part",
        ),
        pytest.param(_without_xdc, None, None, "no FPGA constraints", id="missing-xdc"),
        pytest.param(
            None,
            fusesoc_registry.TargetResolutionError("no such target"),
            None,
            "infrastructure error",
            id="resolution-error",
        ),
        pytest.param(
            None,
            None,
            _refuse_configure,
            "fpga setup failed: edalize rejected",
            id="configure-error",
        ),
    ],
)
def test_invalid_target_recipe_fails_before_vivado(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vivado: dict,
    resolution: _Resolution,
    edit,
    error,
    configure,
    message: str,
) -> None:
    """Wrong-typed or missing Target build inputs fail loudly at the trust boundary.

    A string ``"false"`` is truthy and would otherwise *enable* out-of-context mode.
    """
    _write_project_config(tmp_path)
    resolution.edit = edit or resolution.edit
    if error:
        resolution.errors["default"] = error
    if configure:
        monkeypatch.setattr(edam_layer, "configure", configure)

    outcome, _ = _run_fpga(tmp_path)

    assert outcome.exit_code == EXIT_ERROR
    assert message in outcome.report_text
    assert _vivado_calls(tmp_path) == 0


def test_top_parameter_intent_is_checked_before_vivado(
    tmp_path: Path, vivado: dict, resolution: _Resolution
) -> None:
    """An enabled vlogdefine that the top hides behind a zero-default parameter of
    the same name is refused before Vivado runs."""
    _write_project_config(tmp_path)
    (tmp_path / "rtl" / "top.sv").write_bytes(
        b"module dut_top #(parameter WIDTH = 0); endmodule\n"
    )
    resolution.edit = _with_parameters(WIDTH={"paramtype": "vlogdefine", "default": 16})

    outcome, _ = _run_fpga(tmp_path)

    assert outcome.exit_code == EXIT_ERROR
    assert "enables WIDTH" in outcome.report_text
    assert _vivado_calls(tmp_path) == 0


def test_xdc_resolves_from_fileset(tmp_path: Path, vivado: dict, resolution: _Resolution) -> None:
    """ADR 0031: the Target's file_type:xdc fileset is the XDC source of truth,
    and its files resolve absolute against the build root."""
    _write_project_config(tmp_path)
    resolution.edit = lambda resolved: dataclasses.replace(
        resolved,
        files=(*resolved.files, ResolvedFile(name="constraints/pins.xdc", file_type="xdc")),
    )

    outcome, _ = _run_fpga(tmp_path)

    assert outcome.exit_code == EXIT_SUCCESS
    xdc = vivado["recipe"].xdc_files
    assert [p.name for p in xdc] == ["timing.xdc", "pins.xdc"]
    assert all(p.is_absolute() for p in xdc)


def test_non_string_parameter_value_renders_as_its_value() -> None:
    """The Yosys slang ``-G NAME=VALUE`` encoder formats a non-string parameter with ``str()``."""
    assert str(ParameterValue("int", "8")) == "8"


def test_unlocatable_top_module_does_not_block_the_run(
    tmp_path: Path, vivado: dict, resolution: _Resolution
) -> None:
    """A top absent from the sources is left to the EDA tool, not refused up front."""
    _write_project_config(tmp_path)
    resolution.edit = lambda resolved: dataclasses.replace(resolved, toplevel="elsewhere")

    outcome, _ = _run_fpga(tmp_path)

    assert outcome.exit_code == EXIT_SUCCESS
    assert vivado["recipe"].top == "elsewhere"


def _real_fusesoc_fpga_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the Flow on a project whose Target a real `fusesoc run --setup` resolves."""
    pytest.importorskip("fusesoc")
    import shutil

    monkeypatch.setattr(TargetCatalog, "build", _REAL_CATALOG_BUILD)
    monkeypatch.setattr(fusesoc_registry, "setup_command_for_handle", _REAL_SETUP_HANDLE)
    project = tmp_path / "proj"
    sources = {
        "rtl/include/defs.svh": "`define FOO 1\n",
        "rtl/pkg.sv": "package pkg; endpackage\n",
        "rtl/dut.sv": "module dut_top #(parameter WIDTH=8)(input logic clk); endmodule\n",
        "rtl/legacy.v": "module legacy; endmodule\n",
        "tb/tb_dut.sv": "module tb_dut; dut_top d(.clk(1'b0)); endmodule\n",
        "constraints/timing.xdc": "create_clock\n",
        "fpga_demo.core": _FPGA_CORE_TEXT,
        ".booley/booley.toml": "[flows.fpga]\n",
    }
    for name, text in sources.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_bytes(text.encode())
    if shutil.which("fusesoc"):
        fusesoc_cmd = list(fusesoc_registry.DEFAULT_FUSESOC_CMD)
    else:
        fusesoc_cmd = [sys.executable, "-c", "from fusesoc.main import main; main()"]

    def resolve(handle, **kwargs):
        return _REAL_RESOLVE_HANDLE(handle, **kwargs, fusesoc_cmd=fusesoc_cmd)

    with patch.object(fusesoc_registry, "resolve_target_handle", side_effect=resolve):
        outcome = _fpga_flow(project, targets="fpga").execute_prepared().outcome
    return project, outcome


def test_real_fusesoc_fpga_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vivado: dict):
    """End-to-end: a real `fusesoc run --setup` leaves a resolved vivado EDAM.

    Proves the RTL sources/top/typed-params resolve, the build dir is
    relocatable, and fpga_impl feeds the resolved filelist + include dir +
    defines into the vivado EDAM builder.
    """
    project, outcome = _real_fusesoc_fpga_outcome(tmp_path, monkeypatch)

    assert outcome.exit_code == EXIT_SUCCESS
    recipe = vivado["recipe"]
    assert recipe.top == "dut_top"
    # Compare in POSIX form so a Windows host's backslashes do not fail.
    names = [path.as_posix() for path, _ in recipe.sources]
    for expected in ("rtl/dut.sv", "rtl/pkg.sv", "rtl/legacy.v"):
        assert any(name.endswith(expected) for name in names)
    # The include header surfaces as a dir, the TB file is excluded from sources.
    assert not any("defs.svh" in name or "tb_dut.sv" in name for name in names)
    assert any(p.as_posix().endswith("rtl/include") for p in recipe.include_dirs)
    assert recipe.defines == ("SYNTH",)
    assert dict(recipe.vlogparams) == {"WIDTH": ParameterValue("int", "8")}
    assert recipe.ppa_profile.name == "compact"
    # The resolved build dir lives under the project and is relocatable: no
    # absolute project path (in either separator form) leaked into the EDAM.
    edam = next((project / ".booley" / ".runtime").rglob("*.eda.yml"))
    edam_text = edam.read_text(encoding="utf-8")
    assert str(project) not in edam_text
    assert project.as_posix() not in edam_text


def test_vivado_failure_text_names_project_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vivado: dict
) -> None:
    """A Vivado error names a source or constraint file by Project path in each spelling."""
    vivado["returncode"] = 1
    pad = "x" * 450
    vivado["output"] = "\n".join(
        (
            "# read_verilog -sv {{{rel}/src/fpga_demo_0/rtl/dut.sv}}",
            f"ERROR: [Common 17-55] {pad} [{{build}}/src/fpga_demo_0/rtl/dut.sv:3]",
            "ERROR: [Common 17-55] bad object [{build}/src/fpga_demo_0/constraints/timing.xdc:2]",
            "ERROR: [Common 17-55] bad object [{build}/src/fpga_demo_0/rtl/generated.sv:1]",
            "ERROR: [Common 17-55] bad object [/opt/ip/pins.xdc:2]",
        )
    )
    project, _ = _real_fusesoc_fpga_outcome(tmp_path, monkeypatch)

    report = json.loads((project / "reports" / "fpga_fpga.json").read_text(encoding="utf-8"))
    # The build-tree path starts before the 500-character diagnostic cap and ends after it.
    assert report["infra_error"] == (
        f"Vivado did not reach route_design: ERROR: [Common 17-55] {pad} [rtl/dut.sv:3]"
    )
    tail = report["metrics"]["failure_output"].split("Log tail:\n", 1)[1].splitlines()
    build = Path(vivado["recipe"].resolved.build_root).resolve().as_posix()
    assert tail == [
        "# read_verilog -sv {rtl/dut.sv}",
        f"ERROR: [Common 17-55] {pad} [rtl/dut.sv:3]",
        "ERROR: [Common 17-55] bad object [constraints/timing.xdc:2]",
        f"ERROR: [Common 17-55] bad object [{build}/src/fpga_demo_0/rtl/generated.sv:1]",
        "ERROR: [Common 17-55] bad object [/opt/ip/pins.xdc:2]",
    ]
    log = (project / report["artifacts"]["log"]).read_text(encoding="utf-8")
    assert f"[{build}/src/fpga_demo_0/rtl/dut.sv:3]" in log


def test_failed_fusesoc_setup_is_recorded_as_fpga_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vivado: dict
) -> None:
    """The FPGA Flow's own setup runner measures the setup, whatever its outcome."""
    monkeypatch.setattr(TargetCatalog, "build", _REAL_CATALOG_BUILD)
    monkeypatch.setattr(fusesoc_registry, "setup_command_for_handle", _REAL_SETUP_HANDLE)
    project = tmp_path / "proj"
    for name, text in {
        "rtl/include/defs.svh": "`define FOO 1\n",
        "rtl/pkg.sv": "package pkg; endpackage\n",
        "rtl/dut.sv": "module dut_top(input logic clk); endmodule\n",
        "rtl/legacy.v": "module legacy; endmodule\n",
        "tb/tb_dut.sv": "module tb_dut; endmodule\n",
        "constraints/timing.xdc": "create_clock\n",
        "fpga_demo.core": _FPGA_CORE_TEXT,
        ".booley/booley.toml": "[flows.fpga]\n",
    }.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_bytes(text.encode())
    failing_setup = canned_command(SubprocessResult(returncode=1, stderr="setup broke"))

    def resolve(handle, **kwargs):
        return _REAL_RESOLVE_HANDLE(handle, **kwargs, fusesoc_cmd=failing_setup)

    with patch.object(fusesoc_registry, "resolve_target_handle", side_effect=resolve):
        outcome = _fpga_flow(project, targets="fpga").execute_prepared().outcome

    assert outcome.exit_code == EXIT_ERROR
    assert "failed (exit 1)" in outcome.report_text
    assert "setup broke" in outcome.report_text
    assert _vivado_calls(project) == 0
    (record,) = resource_history.load_records(tmp_path)
    key = record["key"]
    assert (
        record["flow"],
        key["target"],
        key["phase"],
        record["outcome"],
        record["returncode"],
    ) == ("fpga", "fpga", "fpga_prepare", "exit_nonzero", 1)


_CLOCKS = {
    "clk": {
        "period_ns": 10.0,
        "wns_ns": 0.25,
        "whs_ns": 0.1,
        "critical_path_ps": 9750.0,
        "fmax_mhz": 102.5641,
    },
    "clk2": {
        "period_ns": 5.0,
        "wns_ns": -0.1,
        "whs_ns": 0.05,
        "critical_path_ps": 5100.0,
        "fmax_mhz": 196.0784,
    },
}


@pytest.mark.parametrize("per_clock", [_CLOCKS, {}], ids=["two-clocks", "no-clock"])
def test_report_carries_per_clock_timing_without_flat_scalars(
    tmp_path: Path, vivado: dict, per_clock: dict
) -> None:
    """Timing is a per_clock map; the removed flat Fmax and critical-path scalars stay gone."""
    _write_project_config(tmp_path)
    vivado["parsed"] = {**vivado["parsed"], "per_clock": per_clock}

    _, report = _run_fpga(tmp_path)

    metrics = report["metrics"]
    assert metrics["per_clock"] == per_clock
    assert "critical_path_ps" not in metrics
    assert "fmax_mhz" not in metrics
    # The aggregate whole-design scalars ride alongside per_clock.
    assert (metrics["wns_ns"], metrics["whs_ns"]) == (0.1, 0.1)


# A minimal vivado-flow `.core` for the real-fusesoc resolution e2e. Part and
# constraints ride the Target. The header
# is an `is_include_file`; the TB file is `tb`-tagged and excluded from synthesis.
_FPGA_CORE_TEXT = """\
CAPI=2:
name: ::fpga_demo:0
description: fpga_impl slice fixture
filesets:
  rtl:
    files:
      - rtl/include/defs.svh: {file_type: systemVerilogSource, is_include_file: true}
      - rtl/pkg.sv: {file_type: systemVerilogSource}
      - rtl/dut.sv: {file_type: systemVerilogSource}
      - rtl/legacy.v: {file_type: verilogSource}
    file_type: systemVerilogSource
  tb:
    files:
      - tb/tb_dut.sv: {file_type: systemVerilogSource}
    tags: [tb]
  constraints:
    files:
      - constraints/timing.xdc: {file_type: xdc}
parameters:
  WIDTH: {datatype: int, default: 8, paramtype: vlogparam}
  SYNTH: {datatype: bool, paramtype: vlogdefine, default: true}
targets:
  default:
    filesets: [rtl]
  fpga:
    default_tool: vivado
    flow: generic
    flow_options: {tool: vivado, part: xc7a200tfbg484-1, ppa_profile: compact}
    filesets: [rtl, tb, constraints]
    parameters: [WIDTH, SYNTH]
    toplevel: dut_top
"""


# ===========================================================================
# Route-report age gating (stale "cached" result trap) + stderr surfacing
# ===========================================================================


def _write_route_report(work_root: Path, name: str, text: str, mtime: float) -> Path:
    impl_dir = work_root / "fpga.runs" / "impl_1"
    impl_dir.mkdir(parents=True, exist_ok=True)
    rpt = impl_dir / name
    rpt.write_text(text, encoding="utf-8")
    os.utime(rpt, (mtime, mtime))
    return rpt


class TestRouteReportAgeGating:
    """A run that produced nothing must not parse a previous stale report."""

    def test_stale_report_predating_dispatch_is_skipped(self, tmp_path: Path) -> None:
        work_root = tmp_path / "wr"
        # Report last written an hour before the dispatch instant.
        rpt = _write_route_report(
            work_root,
            "top_timing_summary_routed.rpt",
            "old metrics\n",
            mtime=time.time() - 3600,
        )
        collected = FpgaImplFlow()._collect_route_reports(
            work_root,
            min_mtime=time.time(),
        )
        assert collected == ""
        assert rpt.exists()  # not deleted, just ignored

    def test_fresh_report_after_dispatch_is_collected(self, tmp_path: Path) -> None:
        work_root = tmp_path / "wr"
        dispatch = time.time()
        _write_route_report(
            work_root,
            "top_timing_summary_routed.rpt",
            "fresh metrics\n",
            mtime=dispatch + 5,
        )
        flow = _collect_flow(tmp_path)
        collected = flow._collect_route_reports(work_root, min_mtime=dispatch)
        assert "fresh metrics" in collected

    def test_no_min_mtime_collects_everything(self, tmp_path: Path) -> None:
        """Back-compat: without a dispatch instant, age gating is disabled."""
        work_root = tmp_path / "wr"
        _write_route_report(
            work_root,
            "top_timing_summary_routed.rpt",
            "any\n",
            mtime=time.time() - 99999,
        )
        collected = _collect_flow(tmp_path)._collect_route_reports(work_root)
        assert "any" in collected


class TestFailureTailSurfacesStderr:
    """make's real error lands on stderr; the failure report must include it."""

    def test_stderr_included_in_tail(self) -> None:
        tail = FpgaImplFlow._failure_tail(
            "make: Entering directory '/workspace'\nmake: Leaving directory '/workspace'\n",
            "Makefile:12: *** No rule to make target. Stop.\n",
        )
        assert "No rule to make target" in tail
        assert "stderr tail" in tail
        assert "Entering directory" in tail

    def test_empty_stderr_omits_section(self) -> None:
        tail = FpgaImplFlow._failure_tail("Vivado crashed\n", "")
        assert "Vivado crashed" in tail
        assert "stderr tail" not in tail


class TestVivadoFailureClassification:
    def test_passing_route_ignores_incidental_missing_program_text(self) -> None:
        metrics = FpgaMetrics(returncode=0, lut_count=10, ff_count=5, route_completed=True)
        process = SubprocessResult(
            returncode=0,
            stdout="/bin/sh: lsb_release: command not found\n",
            stderr="",
        )

        FpgaImplFlow._apply_vivado_failure(
            FpgaImplFlow(), metrics, process, "", process.stdout, process.stderr
        )

        assert metrics.returncode == 0
        assert metrics.infra_error == ""

    def test_fresh_synth_runlog_syntax_error_is_design_failure(
        self, tmp_path: Path, state_file: Path
    ) -> None:
        flow = _flow(tmp_path, state_file)
        work_root = tmp_path / "wr"
        synth_dir = work_root / "dut.runs" / "synth_1"
        synth_dir.mkdir(parents=True)
        runlog = synth_dir / "runme.log"
        diagnostic = "ERROR: [Synth 8-2715] syntax error near 'endmodule' [dut.sv:9]"
        runlog.write_text(diagnostic + "\n", encoding="utf-8")
        now = time.time()
        os.utime(runlog, (now, now))

        evidence = flow._collect_vivado_evidence(work_root, min_mtime=now - 1)
        failure = flow._classify_vivado_failure(
            SubprocessResult(returncode=1, stdout="", stderr=""), evidence
        )

        assert failure is not None
        assert failure.kind == "design"
        assert diagnostic in failure.diagnostic


# ===========================================================================
# Timeout resolution (unified with asic_synthesize, change #2)
# ===========================================================================


class TestTimeoutResolution:
    """--timeout-ms (ms) > [flows.fpga].timeout_ms > 7200000 default.

    Only the resolution *mechanism* mirrors asic; the fallback VALUE stays
    FPGA's larger 2h default (impl runs are legitimately longer than synth).
    """

    def test_cli_timeout_wins(self, tmp_path: Path, state_file: Path) -> None:
        _write_project_config(
            tmp_path,
            execution_lines="timeout_ms = 300000\n",
        )
        flow = _flow(tmp_path, state_file, "--timeout-ms", "5000")
        assert flow._timeout_ms() == 5000
        assert flow._get_timeout() == 5  # whole seconds

    def test_toml_timeout_ms_fallback(self, tmp_path: Path, state_file: Path) -> None:
        _write_project_config(
            tmp_path,
            execution_lines="timeout_ms = 300000\n",
        )
        flow = _flow(tmp_path, state_file)  # no --timeout
        assert flow._timeout_ms() == 300000
        assert flow._get_timeout() == 300

    def test_default_when_neither_set(self, tmp_path: Path, state_file: Path) -> None:
        _write_project_config(tmp_path)  # no timeout_ms in [flows.fpga]
        flow = _flow(tmp_path, state_file)  # no --timeout
        assert flow._timeout_ms() == 7_200_000
        assert flow._get_timeout() == 7200


# ===========================================================================
# Debuggability capture: failure_output / log_path / reports (change #4)
# ===========================================================================


class TestFailureCapture:
    """A route-not-reached failure keeps infra_error concise while surfacing the
    log/stderr tail via failure_output, the read report files via reports, and a
    persisted run.log via log_path (mirrors asic_synthesize)."""

    def test_route_not_reached_populates_capture_fields(
        self,
        tmp_path: Path,
        state_file: Path,
    ) -> None:
        _write_project_config(tmp_path)
        flow = _flow(tmp_path, state_file)

        # A fresh route report on disk (future mtime beats the dispatch instant
        # _execute_boundary stamps) so _collect_route_reports records its path.
        work_root = tmp_path / "wr"
        _write_route_report(
            work_root,
            "top_utilization_placed.rpt",
            "utilization data\n",
            mtime=time.time() + 3600,
        )

        with (
            patch.object(
                FpgaImplFlow,
                "_prepare_fpga_command",
                return_value=_PreparedFpgaCommand(
                    ["make", "-C", "wr"], work_root, "", False, None
                ),
            ),
            patch.object(
                FpgaImplFlow,
                "_execute_local",
                return_value=SubprocessResult(
                    returncode=1,
                    stdout="make: Entering directory\nmake: Leaving directory\n",
                    stderr="ERROR: no rule to make target. Stop.\n",
                ),
            ),
            # No status:pass => route not completed => the exit code (1) fails it.
            patch(
                "booley.flows.fpga.backends.vivado.edam.parse_fpga_reports",
                return_value={"lut_count": 10, "ff_count": 5},
            ),
        ):
            result = flow._run()

        assert result.exit_code == EXIT_ERROR
        report = json.loads(
            (tmp_path / "reports" / "fpga_default.json").read_text(encoding="utf-8")
        )
        # infra_error stays the concise reason — the tail is NOT concatenated in.
        assert "Vivado did not reach route_design" in report["infra_error"]
        assert "no rule to make target" in report["infra_error"]
        # ...the bulky tail moved to failure_output.
        assert "no rule to make target" in report["metrics"]["failure_output"]
        assert "stderr tail" in report["metrics"]["failure_output"]
        # The impl run dir holding the route reports is named (the report points
        # at directories, not a per-file inventory).
        assert report["metrics"]["artifacts"]["live_dirs"]["impl"].endswith("impl_1")
        # A run.log was persisted for this PRIMARY run.
        assert report["metrics"]["log_path"].endswith("run.log")
        assert (tmp_path / report["metrics"]["log_path"]).is_file()
        assert report["implementation"]["schema_version"] == 1
        assert report["implementation"]["status"]["grade"] == "error"
        assert report["implementation"]["metrics"]["lut_count"] == 10
        progress_paths = list((tmp_path / "reports" / "fpga").glob("*/progress.json"))
        assert len(progress_paths) == 1
        progress = json.loads(progress_paths[0].read_text(encoding="utf-8"))
        assert progress["complete"] is True
        assert progress["completed_targets"] == ["default"]
        # The display/report text shows the concise reason and the separate
        # subprocess-output + log lines.
        assert "did not reach route_design" in result.report_text
        assert "subprocess output:" in result.report_text
        assert "default: log:" in result.report_text


@pytest.mark.parametrize("signalled", [False, True])
def test_completed_target_survives_later_matrix_crash(
    tmp_path: Path, state_file: Path, signalled: bool
) -> None:
    flow = FpgaImplFlow()
    flow.parse_args(
        [
            "--target",
            "fpga_a,fpga_b",
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    flow._target_pairs = ()
    metrics = FpgaMetrics(lut_count=10, ff_count=5)
    calls = 0
    published: list[dict[str, object]] = []

    def run_one(_target: str) -> FpgaMetrics:
        nonlocal calls
        calls += 1
        if calls == 1:
            return metrics
        if signalled:  # a signal ends the process here, without unwinding
            abort_open_progress()
            path = next((tmp_path / "reports/fpga").glob("*/progress.json"))
            published.append(json.loads(path.read_text(encoding="utf-8")))
        raise RuntimeError("simulated outer interruption")

    with (
        patch.object(flow, "_prepare_target_pairs", return_value=None),
        patch.object(
            flow,
            "_plan_implementation",
            return_value=SimpleNamespace(aggregate_errors=[]),
        ),
        patch.object(flow, "_flow_enabled", return_value=True),
        patch.object(flow, "_run_baseline_configs", return_value=({}, None)),
        patch.object(flow, "_run_single_target", side_effect=run_one),
        patch.object(flow, "_persist_target_outcome"),
        pytest.raises(RuntimeError, match="outer interruption"),
    ):
        flow._run()

    progress_path = next((tmp_path / "reports/fpga").glob("*/progress.json"))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["complete"] is True
    assert progress["phase"] == "aborted"
    assert progress["completed_targets"] == ["fpga_a"]
    assert progress["pending_targets"] == ["fpga_b"]
    if signalled:
        (snapshot,) = published
        assert (snapshot["phase"], snapshot["pending_targets"]) == ("aborted", ["fpga_b"])


class TestArtifactPointers:
    """The ``artifacts`` block: present for the run that owns the files,
    absent for the baseline whose files no longer exist."""

    def test_baseline_metrics_carry_no_artifacts(self):
        """A baseline runs in a throwaway worktree that is deleted on exit,
        and its report paths were relativized against THAT root — so the same
        string read against the real Project Root resolves to the CURRENT
        run's report files. Silently wrong, not merely dangling: a
        baseline-vs-current comparison would read the same numbers twice.
        """
        from booley.flows.fpga.backends.vivado.metrics import FpgaMetrics, _metrics_detail

        metrics = FpgaMetrics(
            lut_count=10,
            ff_count=5,
            log_path="",  # already blanked upstream for baseline runs
            dirs={"impl": "build/proj.runs/impl_1"},
        )

        current = _metrics_detail(metrics)
        baseline = _metrics_detail(metrics, baseline=True)

        assert current["artifacts"]["live_dirs"]["impl"] == "build/proj.runs/impl_1"
        assert "artifacts" not in baseline
        # The parsed numbers stay either way — only the pointer block goes.
        assert baseline["lut_count"] == 10

    def test_run_detail_carries_per_target_artifacts(self):
        """fpga_impl returned no ``detail`` at all, so its pointers reached
        state.json and the per-target JSON but never the MCP structuredContent
        an agent actually reads."""
        from booley.flows.fpga.backends.vivado.metrics import FpgaMetrics, _metrics_detail

        metrics = FpgaMetrics(log_path="build/run.log", dirs={"impl": "build/impl_1"})
        detail = _metrics_detail(metrics)

        assert detail["artifacts"] == {
            "log": "build/run.log",
            "live_dirs": {"impl": "build/impl_1"},
        }


class TestArtifactDirs:
    """The report names directories, not a file inventory."""

    def _seed(self, work_root: Path) -> None:
        impl = work_root / "fpga.runs" / "impl_1"
        synth = work_root / "fpga.runs" / "synth_1"
        impl.mkdir(parents=True, exist_ok=True)
        synth.mkdir(parents=True, exist_ok=True)
        for p in (
            impl / "soc_top_power_routed.rpt",
            synth / "runme.log",
            work_root / "vivado.log",
        ):
            p.write_text("body\n", encoding="utf-8")

    def test_names_build_impl_and_synth_dirs(self, tmp_path: Path):
        work_root = tmp_path / "wr"
        self._seed(work_root)
        flow = _collect_flow(tmp_path)

        dirs = flow._artifact_dirs(work_root)

        assert dirs["build"] == "wr"
        assert dirs["impl"] == "wr/fpga.runs/impl_1"
        assert dirs["synth"] == "wr/fpga.runs/synth_1"
        # Three entries, not a per-file listing: everything Vivado wrote is
        # reachable by listing these, and no key hardcodes a filename that a
        # Vivado version bump could rename out from under it.
        assert set(dirs) == {"build", "impl", "synth"}

    def test_absent_run_dirs_are_omitted(self, tmp_path: Path):
        """A run that never reached implementation names only what exists."""
        work_root = tmp_path / "wr"
        (work_root / "fpga.runs" / "synth_1").mkdir(parents=True)
        flow = _collect_flow(tmp_path)

        dirs = flow._artifact_dirs(work_root)

        assert set(dirs) == {"build", "synth"}

    def test_dirs_are_work_dir_relative(self, tmp_path: Path):
        work_root = tmp_path / "wr"
        self._seed(work_root)
        flow = _collect_flow(tmp_path)

        for key, rel in flow._artifact_dirs(work_root).items():
            assert not rel.startswith("/"), f"{key} must not be absolute"
            assert (tmp_path / rel).is_dir(), f"{key} does not resolve"


@pytest.mark.parametrize(
    "diagnostic", ["", "ERROR: Tcl initialization failed: libncurses.so.5 unavailable"]
)
def test_rc0_startup_without_route_is_infrastructure(diagnostic):
    metrics = FpgaMetrics(returncode=0)
    proc = SubprocessResult(returncode=0, stdout=diagnostic)
    FpgaImplFlow._apply_vivado_failure(FpgaImplFlow(), metrics, proc, "", diagnostic, "")
    assert metrics.returncode == 2
    assert "Vivado" in metrics.infra_error
    assert diagnostic in metrics.infra_error


def test_rc0_stderr_syntax_rejection_is_design():
    diagnostic = "ERROR: [Synth 8-2715] syntax error near endmodule"
    metrics = FpgaMetrics(returncode=0)
    proc = SubprocessResult(returncode=0, stderr=diagnostic)
    FpgaImplFlow._apply_vivado_failure(FpgaImplFlow(), metrics, proc, "", "", diagnostic)
    assert metrics.returncode == 1
    assert not metrics.infra_error


@pytest.mark.parametrize(
    "diagnostic,expected",
    [
        ("ERROR: Tcl initialization failed: libncurses.so.5 unavailable", 2),
        ("", 2),
        ("ERROR: [Synth 8-2715] syntax error near endmodule", 1),
    ],
)
@pytest.mark.parametrize("profile", ["balanced", "max_frequency"])
def test_fake_vivado_generated_command_startup(
    tmp_path, state_file, monkeypatch, diagnostic, expected, profile
):
    _write_project_config(tmp_path)
    flow = _flow(tmp_path, state_file)
    root = tmp_path / "generated"
    root.mkdir()
    fake = root / "vivado"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_DIAGNOSTIC"\nexit 0\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(root) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_DIAGNOSTIC", diagnostic)
    from booley.flows import edam as edam_layer
    from booley.flows.fpga.backends.vivado import edam as fpga_edam
    from booley.flows.fpga.profiles import VIVADO_PROFILES
    from booley.flows.fpga.recipe import ResolvedFpgaRecipe

    recipe = ResolvedFpgaRecipe(
        target="probe",
        resolved=None,
        part="xc7a35tcpg236-1",
        top="dut_top",
        xdc_files=(tmp_path / "constraints/timing.xdc",),
        sources=(EdamFile(tmp_path / "rtl/top.sv", "systemVerilogSource"),),
        include_dirs=(),
        defines=(),
        vlogparams={},
        out_of_context=False,
        ppa_profile=VIVADO_PROFILES[profile],
        recipe_snapshot={},
        recipe_fingerprint="",
        source_evidence=run_evidence.FlowSourceEvidence("rev", "sha"),
    )
    edam = fpga_edam.build_fpga_edam(
        recipe,
        name="fpga_probe",
        workspace_root=tmp_path,
        work_root=root,
    )
    edam_layer.configure("vivado", edam, root)
    prepared = _PreparedFpgaCommand(
        fpga_edam.fpga_run_command(root, tmp_path),
        root,
        "",
        False,
        None,
        ppa_profile=VIVADO_PROFILES[profile],
    )
    monkeypatch.setattr(flow, "_prepare_fpga_command", lambda target: prepared)
    result = flow._run()
    assert result.exit_code == expected
    report = json.loads((tmp_path / "reports/fpga_default.json").read_text())
    assert report["implementation"]["status"]["completion"]["route"] is False
    if expected == 2:
        assert "Vivado" in report["infra_error"]
        assert diagnostic in report["infra_error"]
        assert "RESULT: ERROR" in result.report_text


def test_vivado_run_logs_are_the_commands_kill_logs(tmp_path, state_file):
    from booley.flows import job_resources

    runs = tmp_path / "wr" / "dut.runs"
    logs = (
        tmp_path / "wr" / "vivado.log",
        runs / "synth_1" / "runme.log",
        runs / "impl_1" / "runme.log",
    )
    prepared = _PreparedFpgaCommand(["make"], tmp_path / "wr", "", False, None, console_logs=logs)
    # The top-level Vivado log is the command's own output, not a run script's.
    assert prepared.kill_logs == logs[1:]

    flow = _flow(tmp_path, state_file)
    seen: list[tuple[Path, ...]] = []

    def execute(cmd, **_kwargs):
        label = job_resources.current_label()
        assert label is not None
        seen.append(label.kill_logs)
        return SubprocessResult(returncode=0)

    flow._execute_boundary = execute
    flow._run_vivado("fpga_default", ["make"], None, prepared.kill_logs)

    assert seen == [logs[1:]]


@pytest.mark.parametrize("decoy", [True, False])
def test_route_witness_only_from_fresh_impl_runlog(tmp_path, state_file, decoy):
    flow = _flow(tmp_path, state_file)
    impl = tmp_path / "wr/dut.runs/impl_1"
    impl.mkdir(parents=True)
    name = "top_timing_summary_routed.rpt" if decoy else "runme.log"
    (impl / name).write_text("route_design completed successfully\n")
    os.utime(impl / name, (100, 100))
    completed, error = flow._route_evidence(tmp_path / "wr", min_mtime=99)
    assert completed is not decoy
    if not decoy:
        assert "required report" in error
    completed, _ = flow._route_evidence(tmp_path / "wr", min_mtime=101)
    assert not completed


@pytest.mark.parametrize(
    "report", ["top_utilization_placed.rpt", "top_timing_summary_routed.rpt", "top_drc_routed.rpt"]
)
@pytest.mark.parametrize("state", ["missing", "empty", "stale"])
def test_completed_route_missing_required_report_retains_route_fact(
    tmp_path, state_file, monkeypatch, report, state
):
    from booley.flows.fpga.implementation_report import _run

    flow = _flow(tmp_path, state_file)
    root = tmp_path / "wr"
    impl = root / "top.runs/impl_1"
    impl.mkdir(parents=True)
    for name in [
        "runme.log",
        "top_utilization_placed.rpt",
        "top_timing_summary_routed.rpt",
        "top_drc_routed.rpt",
    ]:
        (impl / name).write_text(
            "route_design completed successfully" if name == "runme.log" else "data"
        )
        os.utime(impl / name, (100, 100))
    selected = impl / report
    if state == "missing":
        selected.unlink()
    elif state == "empty":
        selected.write_text("")
    else:
        os.utime(selected, (0, 0))
    monkeypatch.setattr(
        flow,
        "_prepare_fpga_command",
        lambda target: _PreparedFpgaCommand(["make"], root, "", False, None),
    )
    monkeypatch.setattr(
        flow,
        "_execute_boundary",
        lambda argv, **kwargs: SubprocessResult(returncode=0, dispatched_unix=99),
    )
    metrics = flow._run_single_target("default")
    assert metrics.returncode == 2
    assert report.removeprefix("top") in metrics.infra_error
    assert _run(metrics).completion["route"] is True


@pytest.mark.parametrize(
    "process",
    [
        SubprocessResult(returncode=137),
        SubprocessResult(returncode=-9),
        SubprocessResult(returncode=0, oom_kill_delta=1, oom_killed=True),
        SubprocessResult(returncode=0, timed_out=True),
    ],
)
def test_route_fact_does_not_hide_abnormal_termination(process):
    metrics = FpgaMetrics(route_completed=True, returncode=0)
    FpgaImplFlow._apply_vivado_failure(FpgaImplFlow(), metrics, process, "", "", "")
    assert metrics.returncode == 2
    assert metrics.infra_error
    assert metrics.route_completed


def test_ordinary_vivado_failure_during_another_jobs_oom_kill_is_not_abnormal(monkeypatch):
    metrics = FpgaMetrics(route_completed=True, returncode=0)
    process = SubprocessResult(returncode=1, oom_kill_delta=1)

    FpgaImplFlow._apply_vivado_failure(FpgaImplFlow(), metrics, process, "", "", "")

    assert metrics.returncode == 0
    assert not metrics.infra_error


def test_own_vivado_oom_kill_is_abnormal_termination_under_memory_admission(monkeypatch):
    process = SubprocessResult(returncode=1, oom_kill_delta=1, oom_killed=True)

    failure = FpgaImplFlow._classify_vivado_failure(process, "")

    assert failure is not None and failure.failure_kind == "abnormal_termination"


@pytest.mark.parametrize(
    "diagnostic",
    [
        "ERROR: Tcl initialization failed: libncurses.so.5 unavailable",
        "application-specific initialization failed: libncurses.so.5 unavailable",
    ],
)
def test_startup_cause_skips_success_summary_and_echoed_tcl(diagnostic):
    from booley.flows.eda_failures import first_tool_diagnostic

    text = '0 Critical Warnings and 0 Errors encountered.\nputs "error"\n' + diagnostic
    assert first_tool_diagnostic(text, default="no route completion evidence") == diagnostic


_FPGA_MARKER = (
    "BOOLEY_EDA_FAILURE token=0123456789abcdef0123456789abcdef "
    "kind=missing_eda_tool stage=vivado subject=vivado"
)


def _run_failing_fpga(tmp_path: Path, state_file: Path, stdout: str, stderr: str):
    """Run fpga_impl with a mocked failing make invocation; return (result, report)."""
    _write_project_config(tmp_path)
    flow = _flow(tmp_path, state_file)
    work_root = tmp_path / "wr"
    with (
        patch.object(
            FpgaImplFlow,
            "_prepare_fpga_command",
            return_value=_PreparedFpgaCommand(["make", "-C", "wr"], work_root, "", False, None),
        ),
        patch.object(
            FpgaImplFlow,
            "_execute_local",
            return_value=SubprocessResult(returncode=1, stdout=stdout, stderr=stderr),
        ),
        patch(
            "booley.flows.fpga.backends.vivado.edam.parse_fpga_reports",
            return_value={"lut_count": 10, "ff_count": 5},
        ),
    ):
        result = flow._run()
    report = json.loads((tmp_path / "reports" / "fpga_default.json").read_text(encoding="utf-8"))
    return result, report


@pytest.mark.parametrize(
    ("diagnostic_lines", "cause"),
    [
        (
            "ERROR: [Common 17-69] Command failed: bogus option",
            "ERROR: [Common 17-69] Command failed: bogus option",
        ),
        ("vivado: unrecognized option -bogus", "vivado: unrecognized option -bogus"),
    ],
    ids=["error-line", "unrecognized"],
)
def test_2808_fpga_failure_text_follows_shared_rules(
    tmp_path: Path, state_file: Path, diagnostic_lines: str, cause: str
) -> None:
    """Issue #2808: FPGA cause skips noise lines and no marker reaches failure text."""
    result, report = _run_failing_fpga(
        tmp_path,
        state_file,
        stdout=(
            "make: Entering directory '/wr'\n"
            f"{_FPGA_MARKER}\n"
            "/bin/sh: lsb_release: command not found\n"
            "WARNING: [Vivado 12-584] port foo not found\n"
        ),
        stderr=f"{diagnostic_lines}\nmake: *** [Makefile:9: impl] Error 1\n",
    )
    assert result.exit_code == EXIT_ERROR
    assert report["infra_error"] == f"Vivado did not reach route_design: {cause}"
    assert "BOOLEY_EDA_FAILURE" not in report["metrics"]["failure_output"]
    assert "BOOLEY_EDA_FAILURE" not in result.report_text


def test_2808_fpga_infrastructure_diagnostic_hides_marker(
    tmp_path: Path, state_file: Path
) -> None:
    """Issue #2808: an infrastructure-classified Vivado failure shows no marker line."""
    result, report = _run_failing_fpga(
        tmp_path,
        state_file,
        stdout=f"{_FPGA_MARKER}\n",
        stderr="make: vivado: No such file or directory\n",
    )
    assert result.exit_code == EXIT_ERROR
    assert report["infra_error"].startswith("missing tool: vivado")
    assert "BOOLEY_EDA_FAILURE" not in report["metrics"]["failure_output"]
    assert "BOOLEY_EDA_FAILURE" not in result.report_text


@pytest.mark.parametrize("topology", ["standalone", "linked"])
def test_fpga_baseline_materializes_project_topologies(tmp_path, monkeypatch, topology):
    from tests.flows.test_baseline_worktree import (
        _assert_topology_baseline,
        _baseline_implementation_unit,
        _project_topology_checkout,
    )

    root, revision = _project_topology_checkout(tmp_path, monkeypatch, topology)
    monkeypatch.setattr(TargetCatalog, "build", _REAL_CATALOG_BUILD)
    flow = FpgaImplFlow()
    flow.parse_args(["--work-dir", str(root), "--target", "fpga_core", "--baseline", revision])
    handle = TargetCatalog.build(root).select("fpga_core", for_flow="fpga")
    flow._target_handles = {handle.selector: handle}
    flow._target_execution_refs = {}
    flow._target_pairs = target_pair_plans_for_handles((handle,), flow="fpga")
    candidate_handles = flow._target_handles
    roots = []

    def recipe(target):
        baseline = Path(flow.args.work_dir)
        _assert_topology_baseline(baseline, root, target)
        assert flow._target_handle(target).identity == handle.identity
        roots.append(baseline)
        return object()

    def run(target):
        recipe(target)
        return FpgaMetrics(lut_count=7)

    monkeypatch.setattr(flow, "_resolve_fpga_recipe", recipe)
    monkeypatch.setattr(flow, "_fpga_work_unit", _baseline_implementation_unit)
    monkeypatch.setattr(flow, "_run_single_target", run)
    units, errors = flow._plan_fpga_baselines(revision)
    assert errors == []
    assert units[0].revision == revision
    results, sha = flow._run_baseline_configs(flow._target_pairs)
    assert sha
    assert results["fpga_core"].lut_count == 7
    assert flow.args.work_dir == root
    assert flow._target_handles is candidate_handles
    assert roots and all(not path.exists() for path in roots)


@pytest.mark.parametrize("targets", ["default", "default,other"])
def test_invocation_report_records_tools_by_target(tmp_path, state_file, targets):
    _write_project_config(tmp_path)
    flow = _flow(tmp_path, state_file)
    flow.parse_args(
        [
            "--target",
            targets,
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    with patch.object(
        flow,
        "_run_single_target",
        return_value=FpgaMetrics(lut_count=1, ff_count=1, wns_ns=0.1, whs_ns=0.1),
    ):
        result = flow._run()
    path = flow.write_report(result)
    report = json.loads(path.read_bytes())
    expected = (
        {"eda_tool": "vivado"}
        if targets == "default"
        else {"eda_tools": {"default": "vivado", "other": "vivado"}}
    )
    assert {key: report[key] for key in ("eda_tool", "eda_tools") if key in report} == expected


# ---------------------------------------------------------------------------
# Per-Target summary table (#2428)
# ---------------------------------------------------------------------------

_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_DIM = "\x1b[2m"


class _TtyOut(io.StringIO):
    """In-memory stdout that claims to be a terminal (never read from the host)."""

    def isatty(self) -> bool:
        return True


def _passing_fpga(**overrides) -> FpgaMetrics:
    fields = {
        "lut_count": 12034,
        "ff_count": 8100,
        "bram_count": 4,
        "dsp_count": 2,
        "wns_ns": 0.123,
        "whs_ns": 0.01,
        "elapsed_s": 125.0,
        "per_clock": {"clk": make_clock_timing("clk", 6.0, 0.123, 0.01)},
    }
    fields.update(overrides)
    return FpgaMetrics(**fields)


def _fpga_table(report_text: str) -> tuple[str, list[str]]:
    """Return the header line and the lines under its rule up to the first blank."""
    lines = report_text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.lstrip().startswith("Target ")]
    assert starts, f"no table header in:\n{report_text}"
    assert set(lines[starts[0] + 1].replace(" ", "")) == {"─"}, "no rule under the header"
    rows: list[str] = []
    for line in lines[starts[0] + 2 :]:
        if not line.strip():
            break
        rows.append(line)
    return lines[starts[0]], rows


def test_summary_table_aligns_targets_and_cuts_long_names(
    tmp_path: Path, state_file: Path
) -> None:
    flow = _flow(tmp_path, state_file)
    long_name = "a_very_long_target_name_" * 3
    result = flow._aggregate_results(
        ["default", long_name],
        {"default": _passing_fpga(), long_name: _passing_fpga(lut_count=77)},
        {},
        None,
    )
    header, rows = _fpga_table(result.report_text)
    assert len(rows) == 2
    lut_col = header.index("LUT")
    assert rows[0][lut_col:].startswith("12,034")
    assert rows[1][lut_col:].startswith("77")
    name_cell = rows[1][:lut_col].strip()
    assert len(name_cell) <= 40 and name_cell.endswith("…")
    for column in ("FF", "BRAM", "DSP", "WNS", "WHS", "Fmax", "Time", "Status"):
        assert column in header
    assert "Δ" not in result.report_text, "no baseline, no delta row"


def test_summary_table_tty_colors_only_the_console_copy(
    tmp_path: Path, state_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.presentation import colors

    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    out = _TtyOut()
    monkeypatch.setattr("sys.stdout", out)
    flow = _flow(tmp_path, state_file)
    result = flow._aggregate_results(["default"], {"default": _passing_fpga()}, {}, None)
    printed = out.getvalue()
    assert f"{_GREEN}PASS\x1b[0m" in printed
    assert "\x1b" not in result.report_text
    assert colors.strip_ansi(printed).rstrip("\n") == result.report_text


def test_summary_table_tty_failure_prints_once_on_stdout(
    tmp_path: Path,
    state_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from booley.flows.endpoint_reporting import _publish_console_report, _StdoutWitness
    from booley.presentation import colors

    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    flow = _flow(tmp_path, state_file)
    capsys.readouterr()  # argument-parsing notices are not the report under test
    sink = _TtyOut()
    witness = _StdoutWitness(sink)
    monkeypatch.setattr("sys.stdout", witness)
    failing = _passing_fpga(returncode=1, wns_ns=-0.5)
    result = flow._aggregate_results(["default"], {"default": failing}, {}, None)
    endpoint = SimpleNamespace(
        _console_publication_requested=True,
        _stdout_witness=witness,
        endpoint_kind="flow",
        _automatic_target_notice="",
        _automatic_target_notice_published=False,
        announce_success_report=False,
    )
    _publish_console_report(endpoint, result)
    printed = sink.getvalue()
    assert f"{_RED}FAIL\x1b[0m" in printed
    assert printed.count("RESULT: FAIL") == 1
    assert capsys.readouterr().err == ""


def test_summary_non_tty_failure_prints_once_on_stdout(
    tmp_path: Path,
    state_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from booley.flows.endpoint_reporting import _publish_console_report, _StdoutWitness

    flow = _flow(tmp_path, state_file)
    capsys.readouterr()  # argument-parsing notices are not the report under test
    sink = io.StringIO()
    witness = _StdoutWitness(sink)
    monkeypatch.setattr("sys.stdout", witness)
    failing = _passing_fpga(returncode=1, wns_ns=-0.5)
    result = flow._aggregate_results(["default"], {"default": failing}, {}, None)
    endpoint = SimpleNamespace(
        _console_publication_requested=True,
        _stdout_witness=witness,
        endpoint_kind="flow",
        _automatic_target_notice="",
        _automatic_target_notice_published=False,
        announce_success_report=True,
    )
    _publish_console_report(endpoint, result)
    printed = sink.getvalue()
    assert "RESULT: FAIL" in printed
    assert printed.count("RESULT: FAIL") + capsys.readouterr().err.count("RESULT: FAIL") == 1


def test_planning_error_lists_each_target_error(tmp_path: Path, state_file: Path) -> None:
    _write_project_config(tmp_path)
    flow = _flow(tmp_path, state_file)

    with patch.object(
        run_evidence,
        "capture_flow_source_evidence",
        side_effect=fusesoc_registry.FuseSocError("projected core identity mismatch"),
    ):
        result = flow._run()

    assert result.exit_code == EXIT_ERROR
    assert "infrastructure error during planning" in result.report_text
    assert result.detail["default"] == {"infra_error": "projected core identity mismatch"}


def test_summary_record_keeps_every_metric_with_delta_sub_row(
    tmp_path: Path, state_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.presentation import colors

    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    out = _TtyOut()
    monkeypatch.setattr("sys.stdout", out)
    flow = _flow(tmp_path, state_file)
    flow.args.baseline = "abc1234"
    current = _passing_fpga(
        lut_count=1010,
        ff_count=200,
        per_clock={
            "fast": make_clock_timing("fast", 2.0, 0.5, 0.1),
            "slow": make_clock_timing("slow", 6.0, 0.123, 0.01),
        },
    )
    baseline = _passing_fpga(lut_count=1000, ff_count=160, bram_count=2, dsp_count=4)
    current = _passing_fpga(
        lut_count=1010,
        ff_count=200,
        per_clock=current.per_clock,
        bram_count=4,
        dsp_count=2,
    )
    result = flow._aggregate_results(
        ["default"], {"default": current}, {"default": baseline}, "abc1234"
    )
    text = result.report_text
    assert "\x1b" not in text
    assert "baseline: abc1234" in text
    header, rows = _fpga_table(text)
    assert len(rows) == 2, "one Target row plus one delta sub-row"
    for column, shown in (
        ("LUT", "Δ +1.0%"),
        ("FF", "Δ +25.0%"),
        ("BRAM", "Δ +100.0%"),
        ("DSP", "Δ -50.0%"),
    ):
        assert rows[1][header.index(column) :].startswith(shown), column
    assert "(LUTs" not in text
    for token in ("125.0s", "0.123 ns", "0.010 ns", "170.2 MHz [slow]", "PASS"):
        assert token in rows[0], token
    assert f"{_DIM}" in out.getvalue(), "delta sub-row is dimmed on the console"


@pytest.mark.parametrize(
    ("baseline_error", "current_wns", "expected_rows", "expected_result"),
    [
        pytest.param(
            False,
            -0.3,
            {"default": "PASS", "tight": "FAIL"},
            "RESULT: FAIL (tight: timing not met",
            id="timing-violation-is-fail",
        ),
        pytest.param(
            True,
            0.5,
            {"default": "ERROR (baseline)", "tight": "PASS"},
            "RESULT: ERROR (baseline default: ",
            id="baseline-error",
        ),
        pytest.param(
            True,
            -0.3,
            {"default": "ERROR (baseline)", "tight": "PASS"},
            "RESULT: ERROR (baseline default: ",
            id="failed-target-with-baseline-error",
        ),
    ],
)
def test_summary_rows_agree_with_result_reasons(
    tmp_path: Path,
    state_file: Path,
    baseline_error: bool,
    current_wns: float,
    expected_rows: dict[str, str],
    expected_result: str,
) -> None:
    """Each row's Status agrees with the RESULT line; the FPGA Flow has no WARN verdict."""
    flow = _flow(tmp_path, state_file)
    if baseline_error:
        flow.args.baseline = "abc1234"
        current = {"default": _passing_fpga(wns_ns=current_wns), "tight": _passing_fpga()}
        baseline = {
            "default": FpgaMetrics(infra_error="baseline broke", returncode=2),
            "tight": _passing_fpga(),
        }
    else:
        current = {"default": _passing_fpga(), "tight": _passing_fpga(wns_ns=current_wns)}
        baseline = {}
    result = flow._aggregate_results(list(current), current, baseline, "abc1234")
    _, rows = _fpga_table(result.report_text)
    statuses = {
        row.split()[0]: row[
            min(i for w in ("PASS", "FAIL", "ERROR") if (i := row.find(w)) >= 0) :
        ].rstrip()
        for row in rows
        if not row.lstrip().startswith("Δ")
    }
    assert statuses == expected_rows
    assert expected_result in result.report_text


def _result_line(report_text: str) -> str:
    return next(line for line in report_text.splitlines() if line.startswith("RESULT:"))


def test_baseline_infrastructure_error_is_named_in_result(
    tmp_path: Path, state_file: Path
) -> None:
    flow = _flow(tmp_path, state_file)
    flow.args.baseline = "abc1234"
    base_error = FpgaMetrics(infra_error="baseline broke", returncode=2)

    result = flow._aggregate_results(
        ["default"], {"default": _passing_fpga()}, {"default": base_error}, "abc1234"
    )

    line = _result_line(result.report_text)
    assert line.startswith("RESULT: ERROR (")
    assert "baseline default: baseline broke" in line
    assert "baseline default: infrastructure error" not in line
    assert result.exit_code == EXIT_ERROR


def test_comparison_error_message_is_named_in_result(tmp_path: Path, state_file: Path) -> None:
    flow = _flow(tmp_path, state_file)
    flow.args.baseline = "abc1234"

    result = flow._aggregate_results(
        ["default"],
        {"default": _passing_fpga()},
        {"default": _passing_fpga(lut_count=None)},
        "abc1234",
    )

    line = _result_line(result.report_text)
    assert "comparison default: baseline required metric 'lut_count' is unavailable" in line
    assert "invalid evidence" not in line
    assert result.exit_code == EXIT_ERROR


def test_summary_notes_drop_the_flow_prefix(
    tmp_path: Path, state_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.presentation import colors

    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    out = _TtyOut()
    monkeypatch.setattr("sys.stdout", out)
    flow = _flow(tmp_path, state_file)
    flow.args.baseline = "abc1234"
    broken = _passing_fpga(
        latches=1,
        failure_output="line one\nline two",
        log_path="reports/fpga/1/artifacts/default/run.log",
    )
    base_error = FpgaMetrics(infra_error="baseline broke", returncode=2)
    result = flow._aggregate_results(
        ["default"], {"default": broken}, {"default": base_error}, "abc1234"
    )
    lines = result.report_text.splitlines()
    assert not [line for line in lines if line.lstrip().startswith("[fpga]")]
    _, rows = _fpga_table(result.report_text)
    after_table = lines[lines.index(rows[-1]) + 1 :]
    result_at = max(i for i, line in enumerate(after_table) if line.startswith("RESULT:"))
    notes = after_table[:result_at]
    assert any("default: CRITICAL --" in line for line in notes)
    assert any("baseline default: ERROR -- baseline broke" in line for line in lines)
    assert any(
        line.strip() == "default: log: reports/fpga/1/artifacts/default/run.log" for line in notes
    )
    assert "default: subprocess output:" in "\n".join(notes)
    printed = out.getvalue()
    assert f"{_RED}CRITICAL\x1b[0m" in printed
    assert f"{_RED}ERROR\x1b[0m" in printed


@pytest.mark.parametrize("case", ["plain", "baseline"])
def test_fpga_output_matches_golden(
    case: str, tmp_path: Path, state_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One Target's report, progress record, RESULT line and exit code stay as pinned."""
    from tests.flows.implementation_golden_support import render_implementation_output
    from tests.golden.conftest import assert_matches_golden

    @contextmanager
    def fake_worktree(project_root, ref, *, paired_project):
        del paired_project
        wt = Path(project_root) / ".booley" / f".baseline-wt-fake-{ref}"
        wt.mkdir(parents=True, exist_ok=True)
        yield wt

    monkeypatch.setenv("BOOLEY_RUN_ID", "golden-run")
    flow = _flow(tmp_path, state_file, *(["--baseline", "v1.0"] if case == "baseline" else []))
    baseline = _passing_fpga(lut_count=12000, ff_count=8000)
    current = _passing_fpga()

    def run_one(_target: str) -> FpgaMetrics:
        return copy.deepcopy(baseline if flow._execution_role == "baseline" else current)

    with (
        patch("booley.flows.fpga.flow.baseline_worktree", fake_worktree),
        patch("booley.flows.implementation_flow.baseline_worktree", fake_worktree),
        patch("booley.flows.implementation_flow.git_short_sha", return_value="abc1234"),
        patch("booley.flows.fpga.flow.git_full_sha", return_value="a" * 40),
        patch("booley.flows.implementation_flow.git_full_sha", return_value="a" * 40),
        patch.object(
            flow, "_plan_implementation", return_value=SimpleNamespace(aggregate_errors=[])
        ),
        patch.object(flow, "_flow_enabled", return_value=True),
        patch.object(flow, "_run_single_target", side_effect=run_one),
    ):
        outcome = flow._run()

    text = render_implementation_output(flow, "fpga_default.json", outcome, tmp_path)
    assert_matches_golden(f"implementation/fpga_{case}.txt", text)
