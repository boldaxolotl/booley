"""Preparation and classification for the Simulation build stage.

This module is the single authority for the untraced simulator image shared by
ordinary simulation and ``sim --mode elab-only``. Process execution remains owned
by :class:`booley.flows.base.BooleyFlow`; the helpers here only prepare the
command and turn one completed process into typed build evidence.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from booley.core.build_paths import work_root_for
from booley.flows.eda_failures import classify_eda_failure
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.runtime.project_dir import resolve_project_dir
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle, TargetInspection
from booley.targets.parameter_integrity import (
    ParameterIntegrityError,
    validate_top_parameter_intent,
)

from .. import edam as edam_layer
from ..base import SubprocessResult
from . import edam as sim_edam
from .build_parallelism import LaneKind, verilator_backend_arguments

BuildVerdict = Literal["pass", "fail"] | None
BuildFailureKind = Literal["design", "infrastructure"] | None


class SimulationBuildPreparationError(RuntimeError):
    """An expected Target/configuration failure before the build can run."""


def _validated_simulator(
    handle: TargetHandle,
    resolved: fusesoc_registry.ResolvedTarget,
) -> str:
    """Return the configured simulator after authenticating it against the Target."""
    declaration_help = (
        "declare either `flow: sim` with `flow_options.tool`, or legacy `default_tool`"
    )
    try:
        eda_tool = sim_edam.matching_eda_tool(
            handle.eda_tool,
            resolved.configured_eda_tool,
            target=handle.selector,
        )
    except ValueError as exc:
        raise SimulationBuildPreparationError(
            f"Simulation Target {handle.selector!r}: {exc}; {declaration_help}"
        ) from exc
    if eda_tool not in {"icarus", "verilator"}:
        raise SimulationBuildPreparationError(
            f"simulator {eda_tool!r} is not supported by the public sim Flow; "
            "select a Verilator or Icarus Target"
        )
    return eda_tool


_TERMINAL_RECORD_RE = re.compile(
    r"^BOOLEY_BUILD_STAGE token=(?P<token>[0-9a-f]+) rc=(?P<rc>-?\d+)"
    r"(?: duration_ms=(?P<duration_ms>\d+))?$",
    re.MULTILINE,
)
_VERILATOR_DESIGN_ERROR_RE = re.compile(r"^%Error(?:-[A-Z0-9_]+)?:", re.MULTILINE)
_IVERILOG_DESIGN_ERROR_RE = re.compile(
    r"^(?:[^\n:]+:)?\d+(?::\d+)?:\s*(?:syntax\s+)?error\b"
    r"|^error:\s*(?:unable to bind|unable to elaborate|unknown module type|"
    r"invalid module item|syntax error)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class PreparedSimulationBuild:
    """Everything needed to execute and report one simulator build."""

    target: str
    target_identity: str
    resolved: fusesoc_registry.ResolvedTarget
    work_root: Path
    build_root: Path
    eda_tool: str
    toplevel: str
    make_argv: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)
    fileset: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildOutcome:
    """Authenticated terminal state of one attempted Simulation build."""

    ran: bool
    verdict: BuildVerdict
    failure_kind: BuildFailureKind
    elapsed_s: float = 0.0
    output: str = ""
    returncode: int | None = None
    timed_out: bool = False
    peak_rss_mb: float | None = None
    oom_kill_delta: int = 0
    terminal_record: bool = False
    reason: str = ""
    cache_decision: str = ""

    @property
    def passed(self) -> bool:
        """Whether the build established a successful elaboration verdict."""
        return self.verdict == "pass"

    @property
    def design_failed(self) -> bool:
        """Whether a compiler diagnostic established a design rejection."""
        return self.verdict == "fail" and self.failure_kind == "design"


def prepare_simulation_build(
    handle: TargetHandle,
    *,
    variant: str = "",
    build_root: Path | None = None,
    resolution_vlnv: str | None = None,
    environment: Mapping[str, str] | None = None,
    lane_kind: LaneKind = "heavy",
) -> PreparedSimulationBuild:
    """Resolve and prepare the simulator image used by normal Simulation."""
    try:
        return _prepare_simulation_build(
            handle,
            variant=variant,
            build_root=build_root,
            resolution_vlnv=resolution_vlnv,
            environment=environment,
            lane_kind=lane_kind,
        )
    except (
        fusesoc_registry.FuseSocError,
        ParameterIntegrityError,
        selftest_overlay.SelftestOverlayError,
        OSError,
    ) as exc:
        raise SimulationBuildPreparationError(str(exc)) from exc


def _prepare_simulation_build(
    handle: TargetHandle,
    *,
    variant: str,
    build_root: Path | None,
    resolution_vlnv: str | None,
    environment: Mapping[str, str] | None,
    lane_kind: LaneKind,
) -> PreparedSimulationBuild:
    """Prepare one supported simulator Target after boundary normalization."""
    root, inspection, backend_arguments = _simulation_recipe_inputs(handle, lane_kind=lane_kind)
    target = handle.selector
    work_root = build_root or work_root_for(root, "sim", target, variant=variant)
    resolved = fusesoc_registry.resolve_target_handle(
        handle,
        build_root=work_root,
        resolution_vlnv=resolution_vlnv,
        backend_arguments=backend_arguments,
    )
    validate_top_parameter_intent(resolved, flow="sim")
    eda_tool = _validated_simulator(handle, resolved)
    _stage_doctor_overlay(root, resolved.build_root)
    fileset = {
        "rtl": tuple(inspection.rtl_files),
        "tb": tuple(inspection.tb_files),
    }
    rel = edam_layer.relpath_for_make(resolved.build_root, root)
    return PreparedSimulationBuild(
        target=target,
        target_identity=handle.identity,
        resolved=resolved,
        work_root=work_root,
        build_root=Path(resolved.build_root),
        eda_tool=eda_tool,
        toplevel=str(resolved.toplevel),
        make_argv=tuple(edam_layer.make_command(rel)),
        environment=dict(environment or {}),
        fileset=fileset,
    )


def _simulation_recipe_inputs(
    handle: TargetHandle,
    *,
    lane_kind: LaneKind,
    inspection: TargetInspection | None = None,
) -> tuple[Path, TargetInspection, tuple[str, ...]]:
    """Return validated Project, Target facts, and backend setup arguments."""
    root = fusesoc_registry.require_current_target_handle(handle)
    selected = inspection or TargetCatalog.build(root).inspect(handle)
    backend_arguments = verilator_backend_arguments(selected, lane_kind=lane_kind)
    return root, selected, backend_arguments


def simulation_setup_command(
    handle: TargetHandle,
    *,
    build_root: Path,
    resolution_vlnv: str | None = None,
    lane_kind: LaneKind = "heavy",
    inspection: TargetInspection | None = None,
) -> list[str]:
    """Preview the exact FuseSoC setup command used by Simulation preparation."""
    try:
        _, _, backend_arguments = _simulation_recipe_inputs(
            handle,
            lane_kind=lane_kind,
            inspection=inspection,
        )
    except (fusesoc_registry.FuseSocError, OSError) as exc:
        raise SimulationBuildPreparationError(str(exc)) from exc
    kwargs: dict[str, Any] = {"build_root": build_root}
    if resolution_vlnv is not None:
        kwargs["resolution_vlnv"] = resolution_vlnv
    if backend_arguments:
        kwargs["backend_arguments"] = backend_arguments
    return fusesoc_registry.setup_command_for_handle(handle, **kwargs)


def _stage_doctor_overlay(project_root: Path, build_root: Path) -> None:
    """Apply Doctor's conventional bad fixture to the resolved build root."""
    import os

    if os.environ.get(selftest_overlay.INTERNAL_KIND_ENV) != selftest_overlay.BAD_KIND:
        return
    project_dir = resolve_project_dir(project_root)
    copied = selftest_overlay.stage_bad_overlay(project_dir, "sim", build_root)
    if copied == 0:
        raise selftest_overlay.SelftestOverlayError(
            "Doctor requested a bad simulation fixture, but "
            f"{selftest_overlay.bad_overlay_dir(project_dir, 'sim')} is empty"
        )


def build_stage_script(
    build_argv: list[str] | tuple[str, ...],
    token: str,
    *,
    run_line: str = "",
    environment: Mapping[str, str] | None = None,
) -> str:
    """Compose build and optional run halves with an authenticated boundary."""
    exports = "".join(
        f"export {name}={shlex.quote(value)}\n" for name, value in (environment or {}).items()
    )
    build = (
        f"{exports}"
        "_booley_build_start_ns=$(date +%s%N)\n"
        f"{shlex.join(build_argv)}\n"
        "_booley_build_rc=$?\n"
        "_booley_build_end_ns=$(date +%s%N)\n"
        "_booley_build_ms=$(((_booley_build_end_ns - _booley_build_start_ns) / 1000000))\n"
        f'echo "BOOLEY_BUILD_STAGE token={token} rc=$_booley_build_rc '
        'duration_ms=$_booley_build_ms"\n'
        'echo "BOOLEY_BUILD_MILLISECONDS: $_booley_build_ms"\n'
        'echo "BOOLEY_BUILD_SECONDS: $((_booley_build_ms / 1000))"\n'
        'if [ "$_booley_build_rc" -ne 0 ]; then exit "$_booley_build_rc"; fi\n'
    )
    if not run_line:
        return build.rstrip()
    return (
        f"{build}"
        "_booley_run_start_ns=$(date +%s%N)\n"
        f"{run_line}\n"
        "_booley_run_rc=$?\n"
        "_booley_run_end_ns=$(date +%s%N)\n"
        "_booley_run_ms=$(((_booley_run_end_ns - _booley_run_start_ns) / 1000000))\n"
        f'echo "BOOLEY_RUN_STAGE token={token} rc=$_booley_run_rc '
        'duration_ms=$_booley_run_ms"\n'
        'exit "$_booley_run_rc"'
    )


def classify_build_outcome(result: SubprocessResult, token: str) -> BuildOutcome:
    """Classify current-attempt build evidence, failing closed on ambiguity."""
    output = result.stdout + ("\n" + result.stderr if result.stderr else "")
    records = [
        match for match in _TERMINAL_RECORD_RE.finditer(result.stdout) if match["token"] == token
    ]
    if len(records) != 1:
        return _infrastructure_outcome(
            result,
            output,
            ran=bool(records),
            reason="missing or duplicate authenticated terminal build record",
            terminal_record=False,
        )
    record = records[0]
    build_rc = int(record["rc"])
    duration_ms = record["duration_ms"]
    build_result = (
        replace(result, duration_s=int(duration_ms) / 1000) if duration_ms is not None else result
    )
    build_output = result.stdout[: record.start()]
    if result.stderr:
        build_output += "\n" + result.stderr
    if build_rc == 0:
        return _successful_build_outcome(build_result, output, build_rc)
    return _failed_build_outcome(build_result, output, build_output, build_rc)


def _successful_build_outcome(
    result: SubprocessResult,
    output: str,
    build_rc: int,
) -> BuildOutcome:
    """Return authenticated success while retaining later run-stage evidence."""
    return BuildOutcome(
        ran=True,
        verdict="pass",
        failure_kind=None,
        elapsed_s=result.duration_s,
        output=output,
        returncode=build_rc,
        timed_out=result.timed_out,
        peak_rss_mb=result.peak_rss_mb,
        oom_kill_delta=result.oom_kill_delta,
        terminal_record=True,
    )


def _failed_build_outcome(
    result: SubprocessResult,
    output: str,
    build_output: str,
    build_rc: int,
) -> BuildOutcome:
    """Classify one authenticated nonzero build result."""
    if result.timed_out or result.oom_kill_delta > 0 or build_rc < 0 or build_rc >= 128:
        return _infrastructure_outcome(
            result,
            output,
            ran=True,
            returncode=build_rc,
            reason="abnormal build termination",
        )
    failure = classify_eda_failure(
        replace(result, returncode=build_rc, stdout=build_output, stderr=""),
        authenticated_build=True,
    )
    if failure is not None and failure.kind == "infrastructure":
        return _infrastructure_outcome(
            result,
            output,
            ran=True,
            returncode=build_rc,
            reason=failure.reason,
        )
    if _recognized_design_diagnostic(build_output):
        return BuildOutcome(
            ran=True,
            verdict="fail",
            failure_kind="design",
            elapsed_s=result.duration_s,
            output=output,
            returncode=build_rc,
            peak_rss_mb=result.peak_rss_mb,
            terminal_record=True,
            reason="compiler or elaborator rejected the design",
        )
    return _infrastructure_outcome(
        result,
        output,
        ran=True,
        returncode=build_rc,
        reason="nonzero build exit without a recognized design diagnostic",
    )


def setup_failure_outcome(message: str, *, elapsed_s: float = 0.0) -> BuildOutcome:
    """Return a no-verdict outcome for a failure before process execution."""
    return BuildOutcome(
        ran=False,
        verdict=None,
        failure_kind="infrastructure",
        elapsed_s=elapsed_s,
        output=message,
        reason=message,
    )


def _recognized_design_diagnostic(output: str) -> bool:
    """Whether compiler output proves rejection of the HDL design."""
    return bool(
        _VERILATOR_DESIGN_ERROR_RE.search(output) or _IVERILOG_DESIGN_ERROR_RE.search(output)
    )


def _infrastructure_outcome(
    result: SubprocessResult,
    output: str,
    *,
    ran: bool,
    reason: str,
    returncode: int | None = None,
    terminal_record: bool = True,
) -> BuildOutcome:
    return BuildOutcome(
        ran=ran,
        verdict=None,
        failure_kind="infrastructure",
        elapsed_s=result.duration_s,
        output=output,
        returncode=result.returncode if returncode is None else returncode,
        timed_out=result.timed_out,
        peak_rss_mb=result.peak_rss_mb,
        oom_kill_delta=result.oom_kill_delta,
        terminal_record=terminal_record,
        reason=reason,
    )
