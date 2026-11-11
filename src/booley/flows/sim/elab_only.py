"""Elaboration Check (`mode=elab_only`) for the Simulation Flow."""

from __future__ import annotations

import json
import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from booley.config.project_config import (
    lookup_target_section,
)
from booley.core.build_paths import work_root_for
from booley.flows.eda_failures import find_missing_executable, new_attempt_token
from booley.flows.job_resources import resolved_source_bytes
from booley.flows.plan import (
    CommandPlan,
    FlowPlan,
    WorkUnitPlan,
    normalize_plan_argv,
    normalize_plan_inputs,
    normalize_plan_path,
    plan_value_fingerprint,
    stable_unit_id,
)
from booley.flows.progress_lifecycle import (
    ProgressLifecycle,
    ProgressPublicationError,
    progress_document,
)
from booley.flows.run_log import RUN_LOG_NAME, write_run_log
from booley.flows.sim.campaign_reports import target_report_directory
from booley.flows.sim.live_progress import (
    attempt_scope,
    observe_stage,
    publish_checkpoint,
)
from booley.fusesoc import fusesoc_registry
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    EndpointOutcome,
    normalize_completion_error,
)
from booley.runtime.filesystem_utils import replace_file
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import (
    TargetHandle,
)

from .. import edam as edam_layer
from ..eda_parsers import extract_error_gist
from .build import (
    BuildOutcome,
    PreparedSimulationBuild,
    SimulationBuildPreparationError,
    classify_build_outcome,
    prepare_simulation_build,
    setup_failure_outcome,
    simulation_build_script,
    simulation_setup_command,
)
from .build_session import (
    SimulationBuildSession,
    SimulationBuildSlotError,
    preview_generation_root,
    project_compile_surface,
    resolve_target_compile_surface,
)
from .execution.artifacts import artifact_path_component as _artifact_path_component
from .standalone import _StandaloneOutcome, _StandalonePlanRecipe

logger = logging.getLogger(__name__)
from .test_selection import _get_test_envs


class MissingExecutableError(RuntimeError):
    """A required EDA binary is absent — a Flow error, never a test failure.

    Carries the binary name so :meth:`SimulateFlow._missing_executable_result`
    can name it in the exit-2 verdict instead of inventing a per-test FAIL row
    for a test that never ran.
    """

    def __init__(self, binary: str, context: str = "") -> None:
        super().__init__(f"required executable not found: {binary}")
        self.binary = binary
        self.context = context


@dataclass
class ElabOnlyTargetResult:
    """One Target's compile/elaborate/link result in Simulation namespace."""

    target: str
    target_identity: str = ""
    eda_tool: str = ""
    toplevel: str = ""
    compile_command: str = ""
    fileset: dict[str, list[str]] = field(default_factory=dict)
    outcome: BuildOutcome = field(
        default_factory=lambda: BuildOutcome(False, None, "infrastructure")
    )
    log_path: str = ""


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace *path* atomically so a poll never observes partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    replace_file(tmp, path)


class ElabOnlyMixin:
    """Elaboration Check execution for `SimulateFlow`."""

    def _target_sim_env(self, target: str) -> dict[str, str]:
        """*target*'s declared simulator environment (tests.toml ``env``, F-5).

        The Booley answer to an env-parameterized testbench — a cocotb module
        that branches on ``os.getenv("FLAVOR")``, or an SV TB whose C++ main
        reads a config var. Neither existing mechanism covers it:
        ``pre_run_commands`` run in a *separate* shell whose exports die with
        it, and the test filter selects tests, not configuration. Without this
        knob a port has to edit a testbench it does not own.

        Per-Target because that is where the variance lives: the same cocotb
        module run under two RTL flavours is two Targets, and each declares its
        own value. ``{}`` when the Target declares none — the run then inherits
        the ambient environment exactly as before.
        """
        return dict(
            lookup_target_section(
                _get_test_envs(self.args.work_dir), self._target_identity(target)
            )
            or {}
        )

    def _sim_env_preview_lines(self, target: str) -> list[str]:
        """Shell export lines for the Target's configured simulator environment."""
        return [
            f"export {name}={shlex.quote(value)}"
            for name, value in self._target_sim_env(target).items()
        ]

    def _redact_plan_environment(
        self,
        target: str,
        command: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Retain configured variable names while excluding their values."""
        if len(command) != 3 or command[:2] != ("sh", "-c"):
            return command
        script = command[2]
        for name, value in self._target_sim_env(target).items():
            script = script.replace(
                f"export {name}={shlex.quote(value)}",
                f"export {name}=<configured>",
            )
        return (*command[:2], script)

    def _run_elab_only(self) -> EndpointOutcome:
        """Validate the build-only selection before reserving execution resources."""
        preflight = self._elab_only_preflight()
        if isinstance(preflight, EndpointOutcome):
            return (
                self._with_campaign_paths(preflight)
                if preflight.exit_code != EXIT_SUCCESS
                else preflight
            )
        return self._with_live_progress(lambda: self._execute_elab_only(preflight))

    def _execute_elab_only(self, targets: list[str]) -> EndpointOutcome:
        """Compile, elaborate, and link selected Simulation Targets without tests."""
        results: list[ElabOnlyTargetResult] = []
        result: EndpointOutcome | None = None
        self._elab_progress_results = results
        lifecycle = ProgressLifecycle(
            lambda phase: self._write_elab_only_progress(
                targets, self._elab_progress_results, phase=phase, complete=True
            )
        )
        try:
            with lifecycle:
                results = self._run_elab_only_campaign(targets)
                self._elab_progress_results = results
                missing = self._elab_only_missing_executable(results)
                if missing is not None:
                    target, exc = missing
                    result = self._missing_executable_result(exc, target)
                    result.detail["mode"] = self._mode.value
                    result.detail["targets"] = [
                        self._elab_only_detail(target_result) for target_result in results
                    ]
                    artifacts = {
                        target_result.target: {"log": target_result.log_path}
                        for target_result in results
                        if target_result.log_path
                    }
                    if artifacts:
                        result.detail["artifacts"] = artifacts
                    lifecycle.complete()
                    return result
                exit_code, standalone = self._run_optional_standalone(targets, results)
                result = self._elab_only_result(targets, results, exit_code, standalone)
                lifecycle.complete()
                return result
        except ProgressPublicationError as exc:
            if result is not None:
                failure = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
                invocation = self._reserved_invocation_dir
                return normalize_completion_error(
                    result,
                    failure,
                    "publish elaboration progress",
                    path=invocation / "progress.json" if invocation is not None else None,
                )
            return self._with_campaign_paths(
                EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    detail={"progress_error": str(exc)},
                    report_text=f"sim: progress publication failed: {exc}",
                )
            )

    @staticmethod
    def _elab_only_missing_executable(
        results: list[ElabOnlyTargetResult],
    ) -> tuple[str, MissingExecutableError] | None:
        """Return the first absent binary from an inconclusive build attempt."""
        for result in results:
            if result.outcome.failure_kind != "infrastructure":
                continue
            binary = find_missing_executable(result.outcome.output)
            if binary is not None:
                return result.target, MissingExecutableError(
                    binary,
                    context=result.outcome.output,
                )
        return None

    def _elab_only_preflight(self) -> list[str] | EndpointOutcome:
        """Validate compile-only mode and return its selected Targets."""
        if not self._flow_enabled():
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="sim is disabled ([flows.sim].enabled = false).",
                detail={"mode": self._mode.value},
            )
        targets_or_error = self._resolve_requested_targets()
        if isinstance(targets_or_error, EndpointOutcome):
            targets_or_error.detail["mode"] = self._mode.value
            return targets_or_error
        targets = targets_or_error
        target_error = self._validate_interactive_args(targets)
        if target_error is not None:
            target_error.detail["mode"] = self._mode.value
            return target_error
        plan = self._plan_elaboration(targets)
        self._flow_plan = plan
        if self.args.dry_run:
            return self._handle_elab_only_dry_run(targets)
        if plan.aggregate_errors:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="sim planning failed: " + "; ".join(plan.aggregate_errors),
                detail={"mode": self._mode.value, "plan": plan.as_dict()},
            )
        return targets

    def _plan_elaboration(self, targets: list[str]) -> FlowPlan:
        """Resolve every elaboration unit, including one cumulative standalone sweep."""
        units: list[WorkUnitPlan] = []
        errors: list[str] = []
        for target in targets:
            try:
                units.append(self._elaboration_work_unit(target))
            except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
                errors.append(f"{target}: {exc}")
        standalone, standalone_error = self._planned_standalone_unit(targets)
        if standalone is not None:
            units.append(standalone)
        if standalone_error is not None:
            errors.append(standalone_error)

        return FlowPlan(
            flow="sim",
            mode=self._mode.value,
            work_units=tuple(units),
            aggregate_errors=tuple(errors),
        )

    def _elaboration_work_unit(self, target: str) -> WorkUnitPlan:
        """Normalize one resolved elaboration recipe."""
        handle = self._target_handle(target)
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        command = tuple(self._elab_only_dry_command(target))
        if len(command) == 1 and command[0].startswith("ERROR:"):
            raise ValueError(command[0])
        sources, constraints = normalize_plan_inputs(inspection.inputs, self.args.work_dir)
        build_root = preview_generation_root(handle)
        recipe = {
            "build_timeout_ms": self._effective_build_timeout_ms(),
            "environment_fingerprint": plan_value_fingerprint(self._target_sim_env(target)),
            "flow_options": inspection.flow_options,
            "mode": self._mode.value,
            "toplevel": inspection.toplevel,
        }
        return WorkUnitPlan(
            unit_id=stable_unit_id("sim", target, ("elaboration",)),
            role="ordinary",
            revision=None,
            selector=target,
            target_identity=handle.identity,
            test_or_module_scope=("elaboration",),
            eda_tool=inspection.eda_tool,
            timeout_ms=self._effective_build_timeout_ms(),
            sources=sources,
            constraints=constraints,
            parameters=inspection.parameters,
            recipe=recipe,
            commands=(
                CommandPlan(
                    normalize_plan_argv(
                        self._redact_plan_environment(target, command),
                        self.args.work_dir,
                    ),
                    cwd=".",
                    template=True,
                ),
            ),
            expected_artifacts=(
                normalize_plan_path(build_root / RUN_LOG_NAME, self.args.work_dir),
            ),
        )

    def _planned_standalone_unit(
        self,
        targets: list[str],
    ) -> tuple[WorkUnitPlan | None, str | None]:
        """Plan the optional standalone sweep and preserve its execution recipe."""
        if not self._standalone_requested():
            return None, None
        try:
            standalone = self._plan_standalone_check(targets)
        except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
            return None, f"standalone: {exc}"
        self._standalone_plan_recipe = standalone
        return self._standalone_work_unit(targets, standalone), None

    def _standalone_work_unit(
        self,
        targets: list[str],
        standalone: _StandalonePlanRecipe,
    ) -> WorkUnitPlan:
        modules = tuple(module for module, _path in standalone.modules)
        sources = tuple(
            dict.fromkeys([path for _module, path in standalone.modules] + list(standalone.shared))
        )
        identities = [self._target_handle(target).identity for target in targets]
        return WorkUnitPlan(
            unit_id=stable_unit_id("sim", "standalone", modules, role="standalone"),
            role="standalone",
            revision=None,
            selector=",".join(targets),
            target_identity="+".join(identities),
            test_or_module_scope=modules,
            eda_tool=standalone.frontend_label,
            timeout_ms=self._effective_timeout_ms(),
            sources=tuple(normalize_plan_path(path, self.args.work_dir) for path in sources),
            parameters={},
            recipe={
                "frontend": standalone.frontend_label,
                "frontends": list(standalone.frontends),
                "mode": self._mode.value,
                "module_sources": [
                    {
                        "module": probe.module,
                        "source": probe.file,
                        "frontend": probe.frontend,
                        "dialect_flags": list(probe.flags),
                        "fallback": probe.fallback,
                        "shared_sources": list(probe.prereqs),
                    }
                    for probe in standalone.probes
                ],
                "shared_sources": list(standalone.shared),
            },
            commands=tuple(
                CommandPlan(
                    normalize_plan_argv(command, self.args.work_dir),
                    cwd=".",
                )
                for command in standalone.commands
            ),
            expected_artifacts=(
                normalize_plan_path(
                    work_root_for(
                        self.args.work_dir,
                        "sim",
                        "standalone",
                        variant="sweep",
                    )
                    / RUN_LOG_NAME,
                    self.args.work_dir,
                ),
            ),
        )

    def _run_elab_only_campaign(
        self,
        targets: list[str],
    ) -> list[ElabOnlyTargetResult]:
        """Run and checkpoint each requested build-only Target."""
        self.reserve_invocation_dir()
        results = self._elab_progress_results
        self._write_elab_only_progress(targets, results, phase="starting")
        for target in targets:
            with attempt_scope(target, operation="elaboration"):
                result = self._run_one_elab_only(target)
            self._record_elab_only_criterion(result)
            self._write_elab_only_target_report(result)
            if self.state._file_path is not None:
                self.state.save()
            results.append(result)
            self._write_elab_only_progress(targets, results, phase="running")
        return results

    def _run_optional_standalone(
        self,
        targets: list[str],
        results: list[ElabOnlyTargetResult],
    ) -> tuple[int, _StandaloneOutcome | None]:
        """Merge the optional module sweep into the campaign exit status."""
        exit_code = self._elab_only_exit_code(results)
        if not self._standalone_requested():
            return exit_code, None
        with attempt_scope(targets[0], operation="standalone"):
            standalone = self._run_standalone_check(
                targets,
                primary_ok=all(result.outcome.passed for result in results),
            )
        if standalone.eda_tool_failed:
            exit_code = EXIT_ERROR
        elif not standalone.passed and exit_code == EXIT_SUCCESS:
            exit_code = EXIT_FAILURE
        return exit_code, standalone

    def _elab_only_result(
        self,
        targets: list[str],
        results: list[ElabOnlyTargetResult],
        exit_code: int,
        standalone: _StandaloneOutcome | None,
    ) -> EndpointOutcome:
        """Compose the final compile-only report and MCP result."""
        passed = sum(result.outcome.passed for result in results)
        verdict = {EXIT_SUCCESS: "PASS", EXIT_FAILURE: "FAIL", EXIT_ERROR: "ERROR"}[exit_code]
        lines = [self._elab_only_result_line(result) for result in results]
        if standalone is not None:
            lines.extend(standalone.lines)
        lines += ["", f"RESULT: {verdict} ({passed}/{len(results)})"]
        report_text = self._with_campaign_paths(
            EndpointOutcome(report_text="\n".join(lines))
        ).report_text
        print(report_text)
        for result in results:
            self._record_eda_tool(result.target, result.eda_tool)
        detail: dict[str, Any] = {
            "mode": self._mode.value,
            "targets": [self._elab_only_detail(result) for result in results],
        }
        artifacts = {
            result.target: {"log": result.log_path} for result in results if result.log_path
        }
        if artifacts:
            detail["artifacts"] = artifacts
        display_lines = [
            f"{result.target}: {self._elab_only_status(result)}" for result in results
        ]
        if standalone is not None:
            detail["standalone"] = standalone.detail
            display_lines.append(standalone.display)
        return EndpointOutcome(
            exit_code=exit_code,
            criterion_key=(f"elab_pass_{targets[0]}" if len(targets) == 1 else ""),
            criterion_met=len(results) == 1 and results[0].outcome.passed,
            display_lines=display_lines,
            detail=detail,
            report_text=report_text,
        )

    def _run_one_elab_only(self, target: str) -> ElabOnlyTargetResult:
        """Run one canonical untraced Simulation build and archive its output."""
        from booley.flows.terminal_progress import announce_unit

        observe_stage(target, "preparing")
        announce_unit("configure elaboration-only", target=target)
        started = monotonic()
        handle = self._target_handle(target)
        try:
            compile_surface = resolve_target_compile_surface(handle)
            sources_before = project_compile_surface(compile_surface)
            with SimulationBuildSession(handle) as session:
                candidate = session.new_generation()
                self._open_run_log(target, candidate)
                prepared = self._prepare_elab_only_target(
                    target, started, handle=handle, build_root=candidate
                )
                if isinstance(prepared, ElabOnlyTargetResult):
                    return prepared
                if project_compile_surface(compile_surface) != sources_before:
                    raise SimulationBuildSlotError(
                        "Project compile inputs changed during elaboration preparation"
                    )
                self._register_prepared_build(target, prepared)
                inputs = session.capture_inputs(prepared)
                result = self._execute_elab_only_build(target, prepared)
                if result.outcome.passed:
                    session.authorize_fresh_image(prepared, inputs)
                return result
        except SimulationBuildSlotError as exc:
            outcome = setup_failure_outcome(
                f"Simulation build slot failed: {exc}",
                elapsed_s=monotonic() - started,
            )
            return ElabOnlyTargetResult(target=target, outcome=outcome)

    def _prepare_elab_only_target(
        self,
        target: str,
        started: float,
        *,
        handle: TargetHandle,
        build_root: Path,
    ) -> PreparedSimulationBuild | ElabOnlyTargetResult:
        """Prepare one Target or return its expected setup-error result."""
        try:
            return prepare_simulation_build(
                handle,
                build_root=build_root,
                environment=self._target_sim_env(target),
            )
        except SimulationBuildPreparationError as exc:
            logger.debug("sim elab-only setup failed for %s", target, exc_info=True)
            outcome = setup_failure_outcome(
                f"setup failed: {exc}",
                elapsed_s=monotonic() - started,
            )
            result = ElabOnlyTargetResult(target=target, outcome=outcome)
            result.log_path = self._persist_elab_only_log(target, outcome.output)
            return result

    def _register_prepared_build(
        self,
        target: str,
        prepared: PreparedSimulationBuild,
    ) -> None:
        """Register prepared Target state used by reports and optional sweeps."""
        self._open_run_log(target, Path(prepared.build_root))
        self._record_eda_tool(target, prepared.eda_tool)

    def _execute_elab_only_build(
        self,
        target: str,
        prepared: PreparedSimulationBuild,
    ) -> ElabOnlyTargetResult:
        """Execute and classify one already-prepared build-only Target."""
        token = new_attempt_token()
        command = [
            "sh",
            "-c",
            simulation_build_script(prepared, token),
        ]
        from booley.flows.terminal_progress import announce_unit

        observe_stage(
            target,
            "building",
            evidence_root=prepared.build_root,
            attempt_token=token,
            source_bytes=resolved_source_bytes(prepared.resolved),
        )
        announce_unit("elaboration-only build", target=target)
        timeout_s = max(1, self._effective_build_timeout_ms() // 1000)
        proc = self._execute_boundary(command, timeout=timeout_s)
        outcome = classify_build_outcome(proc, token, timeout_s=timeout_s)
        result = ElabOnlyTargetResult(
            target=target,
            target_identity=prepared.target_identity,
            eda_tool=prepared.eda_tool,
            toplevel=prepared.toplevel,
            compile_command=shlex.join(command),
            fileset={name: list(paths) for name, paths in prepared.fileset.items()},
            outcome=outcome,
        )
        result.log_path = self._persist_elab_only_log(target, outcome.output)
        return result

    def _persist_elab_only_log(self, target: str, output: str) -> str:
        """Archive complete build output outside the mutable shared cache."""
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is not None:
            log_dir = invocation_dir / "artifacts" / _artifact_path_component(f"sim_{target}")
        else:
            token = new_attempt_token()[:12]
            log_dir = work_root_for(self.args.work_dir, "sim", target) / f"elab-only-{token}"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            path = write_run_log(log_dir, output, max_bytes=None)
        except OSError:
            logger.debug("could not persist elab-only log for %s", target, exc_info=True)
            return ""
        return posix_relpath(path, self.args.work_dir)

    def _record_elab_only_criterion(self, result: ElabOnlyTargetResult) -> None:
        """Write elaboration evidence only when a real design verdict exists."""
        if result.outcome.verdict is None:
            return
        self.set_criterion(
            f"elab_pass_{result.target}",
            result.outcome.passed,
            source_target=result.target,
            detail={
                "mode": self._mode.value,
                "target": result.target,
                "elapsed_s": round(result.outcome.elapsed_s, 3),
                "error_gist": (
                    extract_error_gist(result.outcome.output)
                    if result.outcome.design_failed
                    else ""
                ),
            },
        )

    @staticmethod
    def _elab_only_exit_code(results: list[ElabOnlyTargetResult]) -> int:
        if any(result.outcome.failure_kind == "infrastructure" for result in results):
            return EXIT_ERROR
        if any(result.outcome.design_failed for result in results):
            return EXIT_FAILURE
        return EXIT_SUCCESS

    @staticmethod
    def _elab_only_status(result: ElabOnlyTargetResult) -> str:
        if result.outcome.passed:
            return "PASS"
        return "FAIL" if result.outcome.design_failed else "ERROR"

    def _elab_only_result_line(self, result: ElabOnlyTargetResult) -> str:
        status = self._elab_only_status(result)
        line = f"[sim:elab-only] {result.target} {status} {result.outcome.elapsed_s:.1f}s"
        if not result.outcome.passed and result.outcome.reason:
            line += f" — {result.outcome.reason}"
        if result.log_path:
            line += f" (log: {result.log_path})"
        return line

    def _elab_only_detail(self, result: ElabOnlyTargetResult) -> dict[str, Any]:
        return {
            "target": result.target,
            "target_identity": result.target_identity,
            "eda_tool": result.eda_tool,
            "toplevel": result.toplevel,
            "compile_command": result.compile_command,
            "fileset": result.fileset,
            "elapsed_s": round(result.outcome.elapsed_s, 3),
            "passed": result.outcome.passed,
            "verdict": result.outcome.verdict,
            "failure_class": result.outcome.failure_kind,
            "reason": result.outcome.reason,
            "log": result.log_path,
        }

    def _write_elab_only_target_report(self, result: ElabOnlyTargetResult) -> None:
        report_dir = self.args.report_dir
        if report_dir is None:
            return
        report = {
            "flow": "sim",
            "mode": self._mode.value,
            "timestamp": utc_now_rfc3339(),
            **self._elab_only_detail(result),
        }
        report_dir.mkdir(parents=True, exist_ok=True)
        invocation_dir = self.reserve_invocation_dir()
        assert invocation_dir is not None
        path = target_report_directory(invocation_dir, result.target) / "simulation.json"
        _atomic_write_json(path, report)

    def _write_elab_only_progress(
        self,
        targets: list[str],
        results: list[ElabOnlyTargetResult],
        *,
        phase: str,
        complete: bool = False,
    ) -> None:
        """Checkpoint an Elaboration Check campaign after every Target."""
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is None:
            return
        completed = [result.target for result in results]
        payload = progress_document(
            flow=self.name,
            run_id=os.environ.get("BOOLEY_RUN_ID", ""),
            phase=phase,
            targets=targets,
            completed_targets=completed,
            detail={result.target: self._elab_only_detail(result) for result in results},
            extra={
                "mode": self._mode.value,
            },
        )
        if complete != bool(payload["complete"]):
            raise ValueError("elaboration progress complete and phase disagree")
        publish_checkpoint(invocation_dir / "progress.json", payload)

    def _handle_elab_only_dry_run(self, targets: list[str]) -> EndpointOutcome:
        del targets
        return self._dry_run_result(self._flow_plan)

    def _elab_only_dry_command(self, target: str) -> list[str]:
        try:
            handle = self._target_handle(target)
            build_root = preview_generation_root(handle)
            setup = simulation_setup_command(
                handle,
                build_root=build_root,
            )
        except (
            fusesoc_registry.TargetResolutionError,
            SimulationBuildPreparationError,
        ) as exc:
            return [f"ERROR: sim elab-only dry-run: {exc}"]
        rel = edam_layer.relpath_for_make(build_root, self.args.work_dir)
        parts = [
            *self._sim_env_preview_lines(target),
            shlex.join(setup),
            shlex.join(edam_layer.make_command(rel)),
        ]
        return ["sh", "-c", " && ".join(parts)]

    def _missing_executable_result(
        self,
        exc: MissingExecutableError,
        target: str,
    ) -> EndpointOutcome:
        """Grade an absent EDA binary as a Flow error (exit 2), not a failure.

        The verdict channel must not say "test FAIL" when no test ever ran
        (F-32): with ``fusesoc`` hidden from PATH the old path printed
        ``run_test_001 FAIL 0.0s``, and with ``verilator`` hidden it printed
        ``sim_basic FAIL (0/1 tests)`` under an "elaboration failed" banner
        that named the wrong stage entirely. Both read, to a triage agent, as a
        broken design. The criterion is deliberately left unset — a run that
        observed nothing may not grade anything.
        """
        message = (
            f"sim: required executable {exc.binary!r} was not found in the "
            f"Sandbox while running Target {target!r}. No "
            "simulation ran, so there is no pass/fail verdict about the design. "
            f"Install {exc.binary!r} (or put it on PATH) in the Sandbox "
            "and re-run; `booley doctor` checks the toolchain."
        )
        tail = "\n".join(exc.context.strip().splitlines()[-15:])
        report_text = self._with_campaign_paths(
            EndpointOutcome(
                report_text=f"{message}\n\n--- output tail ---\n{tail}" if tail else message
            )
        ).report_text
        print(report_text)
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            display_lines=[f"Flow error: {exc.binary} not found"],
            detail={
                "eda_tool_error": "missing_executable",
                "missing_executable": exc.binary,
                "target": target,
            },
            report_text=report_text,
        )
