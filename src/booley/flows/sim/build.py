"""Preparation and classification for the Simulation build stage.

This module is the single authority for the untraced simulator image shared by
ordinary simulation and ``sim --elab-only``.  Process execution remains owned
by :class:`booley.flows.base.BooleyFlow`; the helpers here only prepare the
command and turn one completed process into typed build evidence.
"""

from __future__ import annotations

import re
import secrets
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.runtime.project_dir import resolve_project_dir
from booley.targets.parameter_integrity import (
    ParameterIntegrityError,
    validate_top_parameter_intent,
)
from booley.targets.target import TargetHandle, inspect_target

from .. import edam as edam_layer
from ..base import SubprocessResult
from . import edam as sim_edam

BuildVerdict = Literal["pass", "fail"] | None
BuildFailureKind = Literal["design", "infrastructure"] | None


class SimulationBuildPreparationError(RuntimeError):
    """An expected Target/configuration failure before the build can run."""


_TERMINAL_RECORD_RE = re.compile(
    r"^BOOLEY_BUILD_STAGE token=(?P<token>[0-9a-f]+) rc=(?P<rc>-?\d+)$",
    re.MULTILINE,
)
_VERILATOR_DESIGN_ERROR_RE = re.compile(r"^%Error(?:-[A-Z0-9_]+)?:", re.MULTILINE)
_IVERILOG_DESIGN_ERROR_RE = re.compile(
    r"^(?:[^\n:]+:)?\d+(?::\d+)?:\s*(?:syntax\s+)?error\b"
    r"|^error:\s*(?:unable to bind|unable to elaborate|unknown module type|"
    r"invalid module item|syntax error)",
    re.IGNORECASE | re.MULTILINE,
)
_MISSING_TOOL_RE = re.compile(
    r"(?:command not found|No such file or directory|could not invoke fusesoc)",
    re.IGNORECASE,
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

    @property
    def passed(self) -> bool:
        """Whether the build established a successful elaboration verdict."""
        return self.verdict == "pass"

    @property
    def design_failed(self) -> bool:
        """Whether a compiler diagnostic established a design rejection."""
        return self.verdict == "fail" and self.failure_kind == "design"


def new_attempt_token() -> str:
    """Return an unpredictable token for one build execution attempt."""
    return secrets.token_hex(16)


def prepare_simulation_build(
    handle: TargetHandle,
    *,
    variant: str = "",
    resolution_vlnv: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> PreparedSimulationBuild:
    """Resolve and prepare the simulator image used by normal Simulation."""
    try:
        return _prepare_simulation_build(
            handle,
            variant=variant,
            resolution_vlnv=resolution_vlnv,
            environment=environment,
        )
    except (
        fusesoc_registry.FuseSocError,
        ParameterIntegrityError,
        selftest_overlay.SelftestOverlayError,
        FileNotFoundError,
    ) as exc:
        raise SimulationBuildPreparationError(str(exc)) from exc


def _prepare_simulation_build(
    handle: TargetHandle,
    *,
    variant: str,
    resolution_vlnv: str | None,
    environment: Mapping[str, str] | None,
) -> PreparedSimulationBuild:
    """Prepare one supported simulator Target after boundary normalization."""
    root = handle.project_root
    target = handle.selector
    work_root = edam_layer.work_root_for(root, "sim", target, variant=variant)
    resolved = fusesoc_registry.resolve_target_handle(
        handle,
        build_root=work_root,
        resolution_vlnv=resolution_vlnv,
    )
    validate_top_parameter_intent(resolved, flow="sim")
    eda_tool = sim_edam.normalize_eda_tool(resolved.eda_tool)
    if eda_tool not in {"icarus", "verilator"}:
        raise SimulationBuildPreparationError(
            f"simulator {eda_tool!r} is not supported by the public sim Flow; "
            "select a Verilator or Icarus Target"
        )
    _stage_doctor_overlay(root, resolved.build_root)
    try:
        inspection = inspect_target(root, handle)
        fileset = {
            "rtl": tuple(inspection.rtl_files),
            "tb": tuple(inspection.tb_files),
        }
    except Exception:  # noqa: BLE001 — report context cannot invalidate the build
        fileset = {}
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


def _stage_doctor_overlay(project_root: Path, build_root: Path) -> None:
    """Apply Doctor's internal known-bad simulation overlay when requested."""
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
    suffix = f"\n{run_line}" if run_line else ""
    return (
        f"{exports}"
        "_booley_build_start=$(date +%s)\n"
        f"{shlex.join(build_argv)}\n"
        "_booley_build_rc=$?\n"
        f'echo "BOOLEY_BUILD_STAGE token={token} rc=$_booley_build_rc"\n'
        'if [ "$_booley_build_rc" -ne 0 ]; then exit "$_booley_build_rc"; fi\n'
        'echo "BOOLEY_BUILD_SECONDS: $(( $(date +%s) - _booley_build_start ))"'
        f"{suffix}"
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
    build_output = result.stdout[: record.start()]
    if result.stderr:
        build_output += "\n" + result.stderr
    if build_rc == 0:
        return _successful_build_outcome(result, output, build_rc)
    return _failed_build_outcome(result, output, build_output, build_rc)


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
    if _MISSING_TOOL_RE.search(build_output):
        return _infrastructure_outcome(
            result,
            output,
            ran=True,
            returncode=build_rc,
            reason="required build tool or file was unavailable",
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
