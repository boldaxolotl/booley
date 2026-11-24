"""FpgaImplFlow - run FPGA implementation per config via the Edalize vivado flow.

Invocation is delegated to Edalize (ADR 0019): ``fusesoc run --setup`` resolves
the design-description, ``build_fpga_edam`` + Edalize ``configure()`` materialize
the Vivado project + Tcl in the Sandbox, and the resolved ``make``
command runs there via ``BooleyFlow._execute_boundary``. Interpretation
stays in Booley: report collection, metric parsing
(:func:`fpga_edam.parse_fpga_reports`), and baseline comparison.

There is one builder (the built-in Flow) and no execution-location knob. The
FPGA Flow owns its fixed Vivado backend; the resolved Target's EDA-selection
field controls FuseSoC resolution inputs, not the implementation backend.

``--baseline <ref>`` re-implements the design at a past commit in a throwaway
``git worktree`` (see :mod:`booley.flows.baseline_worktree`) rather than checking the ref
out in place, so it never disturbs the caller's tree (ADR 0012).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar

from booley.core.boundary import (
    as_float,
    as_int,
    as_str,
    require_bool,
)
from booley.core.build_paths import work_root_for
from booley.evidence.timing import per_clock_from_json, worst_clock
from booley.flows.cli_help import CliHelp
from booley.flows.eda_failures import (
    EdaFailure,
    classify_eda_failure,
    first_tool_diagnostic,
    strip_failure_markers,
)
from booley.flows.edam import EdamFile
from booley.flows.flow_config import expected_count
from booley.flows.fpga.cli import FpgaArguments
from booley.flows.fpga.request import FpgaRequest
from booley.flows.job_resources import (
    judged_commands,
    resolved_source_bytes,
    resource_phase,
    unjudged_commands,
)
from booley.flows.plan import (
    CommandPlan,
    FlowPlan,
    WorkUnitPlan,
    WorkUnitRole,
    normalize_plan_path,
    stable_unit_id,
)
from booley.flows.reservation_phases import report_phase_end
from booley.flows.result_table import (
    MISSING,
    TARGET_WIDTH,
    note_line,
    status_cell,
)
from booley.flows.source_paths import ProjectSourcePaths
from booley.flows.target_parameters import vlogparam_specs
from booley.flows.terminal_progress import ProgressText, observe_source_names
from booley.flows.tool_runs import setup_runner
from booley.fusesoc import fusesoc_registry
from booley.fusesoc.core_projection import native_cores_ignored
from booley.presentation import colors
from booley.presentation.terminal_table import render_table
from booley.runtime.filesystem_utils import make_directories
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import format_duration, utc_now_rfc3339
from booley.targets.parameter_integrity import validate_top_parameter_intent

from .. import artifacts, run_evidence
from .. import edam as edam_layer
from ..base import SubprocessResult
from ..baseline_worktree import (
    BaselineWorktreeError,
    baseline_worktree,
    git_full_sha,
)
from ..implementation_comparison import (
    ImplementationComparisonError,
    baseline_execution_context,
    resolve_target_execution_ref,
)
from ..implementation_flow import ImplementationFlow
from ..implementation_publication import (
    target_report_slug,
)
from ..implementation_report import (
    ImplementationAggregate,
    ImplementationContext,
    ImplementationReport,
)
from ..invocation import resolve_timeout_ms
from .backends.vivado import cache as fpga_cache
from .backends.vivado import edam as fpga_edam
from .backends.vivado.edam import validate_vivado_parameter_types
from .backends.vivado.metrics import (
    FpgaMetrics,
    _delta_pct,
    _first_valid_display,
    _metrics_detail,
    _resolved_sources,
    _unique_strings,
    _vlogdefine_args,
)
from .implementation_report import (
    build_fpga_implementation_report,
)
from .profiles import VIVADO_PROFILES, VivadoProfile, resolve_fpga_profile
from .recipe import (
    ResolvedFpgaRecipe,
    fpga_recipe_snapshot,
    fpga_recipe_snapshot_fingerprint,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PreparedFpgaCommand:
    """Materialized command plus the exact inputs governing cache reuse."""

    run_cmd: list[str]
    work_root: Path
    fingerprint: str
    require_bitstream: bool
    # Size of the Target's resolved source files; ``None`` when unknown.
    source_bytes: int | None
    recipe_snapshot: dict[str, Any] = field(default_factory=dict)
    recipe_fingerprint: str = ""
    run_evidence: dict[str, Any] = field(default_factory=dict)
    ppa_profile: VivadoProfile = VIVADO_PROFILES["balanced"]
    console_logs: tuple[Path, ...] = ()
    project_paths: ProjectSourcePaths = field(default_factory=ProjectSourcePaths)

    @property
    def kill_logs(self) -> tuple[Path, ...]:
        """The run scripts' own logs: ISEWrap writes a killed step's bash report there only."""
        return tuple(path for path in self.console_logs if path.name == "runme.log")


def _resolve_fpga_timeout_ms(work_dir: Path | None, requested: Any = None) -> int:
    """Resolve the per-target FPGA implementation budget for MCP and Flow callers."""
    return resolve_timeout_ms("fpga", work_dir, requested)


def _float_metric(data: dict[str, Any], key: str) -> float | None:
    """Report metric as float, or None when absent/non-numeric (bool rejected)."""
    return as_float(data.get(key), None)


def _int_metric(data: dict[str, Any], key: str) -> int | None:
    """Report metric as int, or None when absent/non-numeric (bool rejected)."""
    return as_int(data.get(key), None)


class FpgaImplFlow(ImplementationFlow[FpgaRequest, FpgaMetrics]):
    """Run FPGA implementation for one or more Targets with optional baseline comparison.

    The description deliberately does not name Vivado: which EDA Flow backs a
    Booley Flow is the Target's and booley.toml's business, not part of the Booley Flow's
    identity, and the supported set moves (SUPPORTED-EDA-TOOLS.md owns that list).
    ``asic_synthesize`` reads the same way.
    """

    request_type = FpgaRequest
    argument_adapter = FpgaArguments

    name: str = "fpga"
    description: str = (
        "Run FPGA implementation for one or more Targets with optional baseline comparison"
    )
    cli_help: ClassVar[CliHelp | None] = CliHelp(
        summary=(
            "Run FPGA implementation for one or more Targets and report the results. "
            "Use it to see how the design implements on the Target's FPGA device. "
            "Add --baseline to compare the results with another git revision."
        ),
        example="booley flow fpga --target fpga_soc --baseline main",
    )
    code_modifying: bool = False
    default_timeout: ClassVar[int] = 7200
    # F-14: a passing route otherwise prints nothing on the CLI (its verdict
    # lives in display_lines, which a bare CLI run drops). Surface the RESULT:
    # summary on stdout so PASS is never indistinguishable from a no-op.
    announce_success_report: bool = True
    conventional_families: ClassVar[frozenset[str]] = frozenset({"fpga_impl_ok"})

    def _licensed_tools(self) -> tuple[str, ...]:
        """One Job runs its Vivado work units in series: one Vivado seat.

        A disabled Flow runs no Vivado; it reaches its own refusal in ``_run``
        instead of being refused for a missing Seat Cap.
        """
        return ("vivado",) if self._flow_enabled() else ()

    def _begin_run(self, targets: list[str], /) -> None:
        """Record Vivado as each Target's EDA tool before the run."""
        for target in targets:
            self.record_eda_tool(target, "vivado")

    def _run_target(self, target: str, publish: Callable[[FpgaMetrics], None], /) -> FpgaMetrics:
        """Implement one Target and publish its outcome."""
        metrics = self._run_single_target(target)
        publish(metrics)
        return metrics

    def _result_text(
        self,
        targets: list[str],
        current: dict[str, FpgaMetrics],
        baseline: dict[str, FpgaMetrics],
        reports: dict[str, ImplementationReport],
        aggregate: ImplementationAggregate,
        /,
    ) -> tuple[list[str], list[str], str]:
        """Build the FPGA table, notes and ``RESULT:`` headline."""
        notes, failures = self._baseline_error_notes(baseline, reports)
        self._append_target_notes(notes, failures, targets, current)
        verdict = {0: "PASS", 1: "FAIL", 2: "ERROR"}[aggregate.exit_code]
        suffix = f" ({'; '.join(failures)})" if failures else ""
        table = self._summary_table_lines(targets, current, baseline, reports)
        return table, notes, f"RESULT: {verdict}{suffix}"

    def _result_detail(
        self,
        targets: list[str],
        current: dict[str, FpgaMetrics],
        baseline: dict[str, FpgaMetrics],
        short_sha: str | None,
        reports: dict[str, ImplementationReport],
        aggregate: ImplementationAggregate,
        /,
    ) -> dict[str, Any]:
        return self._aggregate_detail(targets, current, aggregate)

    def _display_lines(self, targets: list[str], current: dict[str, FpgaMetrics], /) -> list[str]:
        return _first_valid_display(targets, current)

    def _fpga_work_unit(
        self,
        target: str,
        recipe: ResolvedFpgaRecipe,
        *,
        role: WorkUnitRole,
        revision: str | None,
    ) -> WorkUnitPlan:
        """Project one resolved FPGA recipe into the shared plan contract."""
        root = Path(self.args.work_dir)
        work_root = work_root_for(root, "fpga", target)
        handle = self._target_handle(target)
        return WorkUnitPlan(
            unit_id=stable_unit_id(
                "fpga",
                target,
                (target,),
                role=role,
                revision=revision,
            ),
            role=role,
            revision=revision,
            selector=target,
            target_identity=handle.identity,
            test_or_module_scope=(target,),
            eda_tool="vivado",
            timeout_ms=self._timeout_ms(),
            sources=tuple(
                self._fpga_plan_input_path(path, recipe, root)
                for path, _file_type in recipe.sources
            ),
            constraints=tuple(
                self._fpga_plan_input_path(path, recipe, root) for path in recipe.xdc_files
            ),
            parameters=recipe.resolved.parameters,
            recipe=recipe.recipe_snapshot,
            commands=(
                CommandPlan(
                    ("make", "-C", normalize_plan_path(work_root, root)),
                    cwd=".",
                    template=True,
                ),
            ),
            expected_artifacts=(normalize_plan_path(work_root / "run.log", root),),
        )

    @staticmethod
    def _fpga_plan_input_path(
        path: Path,
        recipe: ResolvedFpgaRecipe,
        root: Path,
    ) -> str:
        """Render staged inputs by their stable Target-owned relative names."""
        try:
            return path.resolve().relative_to(recipe.resolved.build_root.resolve()).as_posix()
        except ValueError:
            return normalize_plan_path(path, root)

    def _plan_implementation(self, targets: list[str]) -> FlowPlan:
        """Resolve candidate and baseline recipes before either revision runs."""
        candidate_units: list[WorkUnitPlan] = []
        baseline_units: list[WorkUnitPlan] = []
        errors: list[str] = []
        candidate_revision = git_full_sha("HEAD", Path(self.args.work_dir))
        candidate_recipes: dict[str, ResolvedFpgaRecipe] = {}
        for target in targets:
            try:
                if self.args.dry_run:
                    with self._dry_run_build_root(target) as build_root:
                        recipe = self._resolve_fpga_recipe(target, build_root=build_root)
                else:
                    recipe = self._resolve_fpga_recipe(target)
                    candidate_recipes[target] = recipe
                candidate_units.append(
                    self._fpga_work_unit(
                        target,
                        recipe,
                        role="candidate",
                        revision=candidate_revision,
                    )
                )
            except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
                errors.append(f"candidate {target}: {exc}")
        self._planned_candidate_fpga_recipes = candidate_recipes

        baseline_ref = self.args.baseline
        if baseline_ref:
            baseline_units, baseline_errors = self._plan_fpga_baselines(baseline_ref)
            errors.extend(baseline_errors)
        return FlowPlan(
            flow="fpga",
            mode="implementation",
            work_units=(*baseline_units, *candidate_units),
            aggregate_errors=tuple(errors),
            planning_disclosures=(
                "FuseSoC setup may run in invocation-owned disposable scratch; "
                "EDA commands are not executed.",
            ),
        )

    def _plan_fpga_baselines(
        self,
        baseline_ref: str,
    ) -> tuple[list[WorkUnitPlan], list[str]]:
        """Resolve each unique baseline Target in a disposable checkout."""
        units: list[WorkUnitPlan] = []
        errors: list[str] = []
        project_root = Path(self.args.work_dir)
        revision = git_full_sha(baseline_ref, project_root) or baseline_ref
        candidate_handles = self._target_handles
        candidate_refs = self._target_execution_refs
        try:
            with baseline_worktree(
                project_root,
                baseline_ref,
                paired_project=self._paired_project_baseline,
            ) as worktree:
                self.args.work_dir = worktree
                self._target_handles, self._target_execution_refs = baseline_execution_context(
                    self._target_pairs,
                    worktree,
                )
                for pair in self._target_pairs:
                    target = pair.baseline.selector
                    try:
                        recipe = self._resolve_fpga_recipe(target)
                        units.append(
                            self._fpga_work_unit(
                                target,
                                recipe,
                                role="baseline",
                                revision=revision,
                            )
                        )
                    except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
                        errors.append(f"baseline {target}: {exc}")
        except (BaselineWorktreeError, ImplementationComparisonError) as exc:
            errors.append(f"baseline: {exc}")
        finally:
            self.args.work_dir = project_root
            self._target_handles = candidate_handles
            self._target_execution_refs = candidate_refs
        return units, errors

    def _resolve_fpga_recipe(
        self,
        target: str,
        *,
        build_root: Path | None = None,
    ) -> ResolvedFpgaRecipe:
        """Resolve and validate every Target input shared with a real dispatch."""
        work_dir = Path(self.args.work_dir)
        resolved = self._resolve_fpga_target(target, build_root=build_root)
        validate_top_parameter_intent(resolved, flow="fpga")
        validate_vivado_parameter_types(vlogparam_specs(resolved.parameters))
        part = self._resolve_part(resolved.flow_options)
        xdc_files = tuple(self._resolve_xdc_files(resolved, target))
        top = str(resolved.toplevel or "")
        if not top:
            raise ValueError(
                f"fpga: top module not found for target {target!r} (set the Target toplevel)"
            )
        sources, include_dirs = _resolved_sources(resolved)
        out_of_context = require_bool(
            resolved.flow_options,
            "out_of_context",
            field=f"Target {target!r} flow_options.out_of_context",
        )
        ppa_profile = resolve_fpga_profile(
            resolved.flow_options,
            override=getattr(self.args, "ppa_profile", None),
            target=target,
        )
        snapshot = fpga_recipe_snapshot(resolved, target=target, profile=ppa_profile)
        return ResolvedFpgaRecipe(
            target=target,
            resolved=resolved,
            part=part,
            top=top,
            xdc_files=xdc_files,
            sources=tuple(sources),
            include_dirs=tuple(include_dirs),
            defines=tuple(_unique_strings(_vlogdefine_args(resolved.parameters))),
            vlogparams=vlogparam_specs(resolved.parameters),
            out_of_context=out_of_context,
            ppa_profile=ppa_profile,
            recipe_snapshot=snapshot,
            recipe_fingerprint=fpga_recipe_snapshot_fingerprint(snapshot),
            source_evidence=run_evidence.capture_flow_source_evidence(work_dir, target),
            reachable_dirs=self._reachable_dirs(target, resolved, sources, include_dirs),
        )

    def _reachable_dirs(
        self,
        target: str,
        resolved: Any,
        sources: Sequence[EdamFile],
        include_dirs: Sequence[Path],
    ) -> tuple[Path, ...]:
        """Folders whose files must join the cache key; empty outside isolated Projects.

        The whole build root is included: it holds only FuseSoC output of listed
        files, so a test-only header a source reaches through ``../`` counts too.
        """
        if not native_cores_ignored(self._target_handle(target).project_root):
            return ()
        folders = {
            Path(resolved.build_root),
            *include_dirs,
            *(source.path.parent for source in sources),
        }
        return tuple(sorted(folders))

    def _resolve_fpga_target(self, target: str, *, build_root: Path | None) -> Any:
        """Resolve one Target at the selected candidate or baseline revision."""
        work_dir = Path(self.args.work_dir)
        selected_root = build_root or work_root_for(
            work_dir,
            self.name,
            target,
            variant="fusesoc",
        )
        handle = self._target_handle(target)
        runner = setup_runner(self.name, target, "fpga_prepare")
        ref = getattr(self, "_target_execution_refs", {}).get(target)
        # The Target's judgment covers only Vivado: the setup keeps its own outcome.
        with unjudged_commands():
            if ref is not None:
                return resolve_target_execution_ref(
                    handle, ref, build_root=selected_root, runner=runner
                )
            return fusesoc_registry.resolve_target_handle(
                handle,
                build_root=selected_root,
                library_plan=fusesoc_registry.build_library_plan(
                    handle.project_root, selected_root
                ),
                runner=runner,
            )

    def _prepare_fpga_command(
        self,
        target: str,
    ) -> _PreparedFpgaCommand:
        """Materialize the validated Vivado recipe and return its executable command."""
        from booley.flows.terminal_progress import announce_unit

        announce_unit(f"{getattr(self, '_execution_role', 'candidate')}: configure", target=target)
        work_dir = Path(self.args.work_dir)
        recipe = self._fpga_recipe_for_execution(target)
        work_root = work_root_for(work_dir, "fpga", target)
        edam = self._materialize_fpga_project(recipe, work_root)
        fingerprint = fpga_cache.input_fingerprint(
            recipe.resolved,
            edam,
            out_of_context=recipe.out_of_context,
            recipe_sha256=recipe.recipe_fingerprint,
            reachable_dirs=recipe.reachable_dirs,
        )
        dispatch_evidence = run_evidence.build_flow_run_evidence(
            flow=self.name,
            target=target,
            recipe_sha256=recipe.recipe_fingerprint,
            work_dir=work_dir,
            source_evidence=recipe.source_evidence,
        )
        return _PreparedFpgaCommand(
            console_logs=(
                work_root / "vivado.log",
                work_root / f"{edam['name']}.runs" / "synth_1" / "runme.log",
                work_root / f"{edam['name']}.runs" / "impl_1" / "runme.log",
            ),
            run_cmd=fpga_edam.fpga_run_command(work_root, work_dir),
            work_root=work_root,
            fingerprint=fingerprint,
            require_bitstream=not recipe.out_of_context,
            source_bytes=resolved_source_bytes(recipe.resolved),
            recipe_snapshot=recipe.recipe_snapshot,
            recipe_fingerprint=recipe.recipe_fingerprint,
            run_evidence=dispatch_evidence.as_dict(),
            ppa_profile=recipe.ppa_profile,
            project_paths=ProjectSourcePaths.for_target(
                self._target_handle(target), recipe.resolved
            ).spelled_from(recipe.resolved.build_root, work_root),
        )

    def _fpga_recipe_for_execution(self, target: str) -> ResolvedFpgaRecipe:
        """Reuse a planned candidate recipe or resolve the active execution revision."""
        if getattr(self, "_execution_role", "candidate") == "candidate":
            planned = getattr(self, "_planned_candidate_fpga_recipes", {}).get(target)
            if planned is not None:
                return planned
        return self._resolve_fpga_recipe(target)

    def _materialize_fpga_project(
        self,
        recipe: ResolvedFpgaRecipe,
        work_root: Path,
    ) -> dict[str, Any]:
        """Generate Vivado Tcl, then apply profile and out-of-context patches in order."""
        edam = fpga_edam.build_fpga_edam(
            recipe,
            name=f"fpga_{recipe.target}",
            workspace_root=self.args.work_dir,
            work_root=work_root,
        )
        project_name = str(edam["name"])
        edam_layer.configure("vivado", edam, work_root)
        fpga_edam.validate_vivado_parameter_contract(
            work_root,
            project_name,
            recipe.vlogparams,
        )
        # Vivado resets synthesis step properties when a strategy is assigned.
        # Apply the profile first so the OOC patch below remains authoritative.
        fpga_edam.apply_ppa_profile(work_root, project_name, recipe.ppa_profile)
        # QoR-gate targets whose bare toplevel out-ports the package (e.g. an
        # engine block never meant for pin mapping) opt into OOC synthesis so
        # placement does not fail on IO-buffer overutilization. Strictly typed:
        # a string ``"false"`` is truthy and would silently enable OOC, so a
        # non-bool raises (BoundaryError is a ValueError → infra error upstream).
        if recipe.out_of_context:
            fpga_edam.enable_out_of_context(work_root, project_name)
        return edam

    def _resolve_part(self, flow_options: Any) -> str:
        """Validate and resolve the FPGA part from Target ``flow_options``."""
        part = as_str(flow_options.get("part"))
        if not part:
            raise ValueError(
                "fpga: FPGA part must be a non-empty string (set flow_options.part on the Target)"
            )
        return part

    def _resolve_xdc_files(
        self,
        resolved: Any,
        target: str,
    ) -> list[Path]:
        """Resolve the per-target XDC constraint files (ADR 0031).

        An XDC carries pin placement *and* ``create_clock``/false-paths — a
        **design constraint**, not a board knob — so it travels with the design
        as a ``file_type: xdc`` fileset on the FuseSoC Target, mirroring
        ADR 0029's SDC. The fileset is the sole source of truth: its paths
        resolve absolute under the FuseSoC build root, so they ride into the
        vivado EDAM the same way the RTL sources do. A Target with no
        ``file_type: xdc`` fileset is a hard error — XDC stays mandatory.
        """
        fileset = [f.absolute(resolved.build_root) for f in resolved.xdc_files]
        if fileset:
            return fileset
        raise ValueError(
            f"fpga: Target {target!r} has no FPGA constraints. Add a "
            "`file_type: xdc` fileset (create_clock / set_property PACKAGE_PIN / "
            "false-paths) to the Target — an XDC is a design constraint, so it "
            "lives with the design, not on [flows.fpga]."
        )

    def _announce_vivado_run(self, prepared, target: str) -> None:
        from booley.flows.terminal_progress import announce_unit, observe_logs

        role = getattr(self, "_execution_role", "candidate")
        observe_source_names(prepared.project_paths)
        self._open_run_log(target, prepared.work_root)
        announce_unit(f"{role}: Vivado running", target=target)
        observe_logs(
            list(getattr(prepared, "console_logs", ())),
            target=target,
            temporary=role == "baseline",
        )

    def _run_vivado(
        self,
        target: str,
        run_cmd: list[str],
        source_bytes: int | None,
        kill_logs: tuple[Path, ...] = (),
    ) -> SubprocessResult:
        """Run the FPGA implementation command under its resource-history label."""
        with resource_phase(target, "fpga_impl", source_bytes=source_bytes, kill_logs=kill_logs):
            return self._execute_boundary(run_cmd, timeout=self._get_timeout())

    def _run_single_target(self, target: str) -> FpgaMetrics:
        """Implement one Target and report its ``fpga_impl`` phase ended.

        A phase that is skipped (a setup failure or a fingerprint cache hit) ends
        at once. A baseline run reports nothing: its Target may be the
        candidate's, whose phase it must not count out of the Job's Memory
        Reservation.
        """
        try:
            with judged_commands() as commands:
                metrics = self._implement_target(target)
                commands.judge(metrics.returncode == 0)
            return metrics
        finally:
            if getattr(self, "_execution_role", "candidate") != "baseline":
                report_phase_end(target, "fpga_impl")

    def _implement_target(self, target: str) -> FpgaMetrics:
        """Configure, run, and interpret Vivado inside the Sandbox."""
        try:
            prepared = self._prepare_fpga_command(target)
            run_cmd = prepared.run_cmd
            work_root = prepared.work_root
        except Exception as exc:  # isolate arbitrary adapter/configure failures
            logger.debug("fpga_impl EDAM/configure failed for %s", target, exc_info=True)
            return FpgaMetrics(returncode=2, infra_error=f"fpga setup failed: {exc}")

        fingerprint = getattr(prepared, "fingerprint", "")
        require_bitstream = getattr(prepared, "require_bitstream", False)
        if fingerprint and not getattr(self.args, "no_cache", False):
            cached = self._load_cached_metrics(
                target,
                work_root,
                fingerprint,
                require_bitstream=require_bitstream,
            )
            if cached is not None:
                return self._attach_recipe_evidence(cached, prepared)

        # A cache miss must execute the recipe even if Make's timestamps claim
        # it is current. Otherwise a no-op leaves only old reports, and there is
        # no evidence that those artifacts correspond to this fingerprint.
        if fingerprint and "-B" not in run_cmd and "--always-make" not in run_cmd:
            run_cmd = [*run_cmd, "-B"]

        # F-26: _persist_fpga_log only lands at the END of a long P&R run, so
        # claim the log now — a tail during the wait must not read the
        # previous run's utilization/timing tail as this run's progress.
        # The command is not path-remapped: ``make -C <rel>`` resolves from the
        # shared Sandbox workspace.
        self._announce_vivado_run(prepared, target)
        result = self._run_vivado(target, run_cmd, prepared.source_bytes, prepared.kill_logs)
        # The edalize project-mode vivado flow (launch_runs/wait_on_run) writes
        # its utilization/timing/DRC reports to *files*, not stdout — unlike the
        # legacy non-project tcl that printed report_* to the console. So the
        # log alone has no metrics; concatenate the Vivado-generated route
        # report files (read off the shared workspace — interpretation stays in
        # Booley, ADR 0019) before the post-processor parses them.
        log_text = result.stdout
        # make's real error lands on stderr, not stdout, so a failed `make`
        # leaves stdout with only the "Entering/Leaving directory" chatter.
        # Keep stderr for the failure tail.
        stderr_text = result.stderr
        # Age-gate the on-disk reports against the dispatch instant: only reports
        # written by THIS run count. Otherwise a run that did nothing (e.g. an
        # empty/detached workspace bind-mount) lets the previous run's stale
        # *_routed.rpt files parse into a bogus "cached" pass with old metrics,
        # masking the real infra failure.
        report_text = self._collect_vivado_evidence(work_root, min_mtime=result.dispatched_unix)
        metric_dict = fpga_edam.parse_fpga_reports(
            log_text + "\n" + report_text if report_text else log_text
        )

        # A boardless soft IP cannot write a
        # bitstream (write_bitstream's NSTD-1/UCIO-1 DRC precondition fails with
        # no pinout), so ``make`` exits non-zero even when synth+place+route fully
        # succeed. fpga_impl is a QoR Flow — route completion (the report files +
        # the route-done marker parse_fpga_reports keys ``status`` on) defines
        # success, NOT the bitstream/make exit code. Only when route did *not*
        # complete do we surface the boundary command's exit code as the failure.
        # Profiles requiring post-route work must also pass the completion gate below.
        route_completed, report_error = self._route_evidence(
            work_root, min_mtime=result.dispatched_unix
        )
        metric_dict["exit_code"] = 0 if route_completed else result.returncode
        metrics = self._metrics_with_allowances(metric_dict, result.duration_s)
        metrics.route_completed = route_completed
        metrics.cache_fingerprint = fingerprint
        self._apply_route_report_error(metrics, report_error)
        completion_error = fpga_edam.profile_completion_error(
            log_text + "\n" + report_text, prepared.ppa_profile
        )
        if completion_error and route_completed:
            metrics.returncode = 2
            metrics.infra_error = completion_error
        if result.timed_out:
            metrics.timed_out = True
        self._report_vivado_failure(metrics, result, report_text, prepared.project_paths)
        # Where this run's artifacts live, for the reader to list.
        metrics.dirs = self._artifact_dirs(work_root)
        # Persist the combined run log (route reports + make log/stderr) on pass
        # AND fail, but only for the PRIMARY run: a baseline run lives in a
        # throwaway worktree (self.args.work_dir swapped to it in
        # _run_baseline_configs) that is deleted on context exit, so a log path
        # under it would dangle — leave log_path="" there.
        if Path(self.args.work_dir) == getattr(self, "_project_root", None):
            combined = (
                log_text + "\n" + stderr_text + (("\n" + report_text) if report_text else "")
            )
            self._persist_fpga_log(target, combined)
            metrics.log_path = self._publish_numbered_fpga_log(target, combined)
        if metrics.passed and fingerprint:
            fpga_cache.store(
                work_root,
                fingerprint,
                require_bitstream=require_bitstream,
                min_mtime=result.dispatched_unix,
                producer_evidence=getattr(prepared, "run_evidence", {}),
            )
        return self._attach_recipe_evidence(metrics, prepared)

    @staticmethod
    def _attach_recipe_evidence(
        metrics: FpgaMetrics,
        prepared: Any,
    ) -> FpgaMetrics:
        """Attach the recipe materialized for this run to its metrics."""
        metrics.recipe_snapshot = getattr(prepared, "recipe_snapshot", {})
        metrics.recipe_fingerprint = getattr(prepared, "recipe_fingerprint", "")
        current_evidence = getattr(prepared, "run_evidence", {})
        if metrics.cached:
            metrics.cache_consumer_run_id = str(current_evidence.get("run_id", ""))
        else:
            metrics.run_evidence = current_evidence
        return metrics

    @staticmethod
    def _apply_route_report_error(metrics: FpgaMetrics, report_error: str) -> None:
        """A route witness cannot substitute for the evidence needed to grade it."""
        if metrics.route_completed and (
            report_error
            or not metrics.has_primary_metrics
            or metrics.wns_ns is None
            or metrics.whs_ns is None
        ):
            metrics.returncode = 2
            metrics.infra_error = (
                report_error or "Vivado required reports lack usable utilization/timing evidence"
            )

    def _report_vivado_failure(
        self,
        metrics: FpgaMetrics,
        result: SubprocessResult,
        evidence: str,
        project_paths: ProjectSourcePaths,
    ) -> None:
        """Classify a failed Vivado run, then name Project paths in the text it reports."""
        # Map before classifying: the diagnostic line is capped, which would cut a
        # long build-tree path before it could be replaced.
        project = replace(
            result,
            stdout=project_paths.project_text(result.stdout),
            stderr=project_paths.project_text(result.stderr),
        )
        self._apply_vivado_failure(
            metrics, project, project_paths.project_text(evidence), project.stdout, project.stderr
        )

    def _apply_vivado_failure(
        self,
        metrics: FpgaMetrics,
        result: SubprocessResult,
        evidence: str,
        stdout: str,
        stderr: str,
    ) -> None:
        """Apply design-vs-infrastructure ownership to a failed Vivado run."""
        abnormal = (
            result.timed_out
            or result.oom_blamed
            or result.returncode < 0
            or result.returncode >= 128
        )
        if metrics.infra_error and not abnormal:
            metrics.failure_output = self._failure_tail(stdout + "\n" + evidence, stderr).strip()
            return
        if metrics.route_completed and not abnormal:
            return
        failure = self._classify_vivado_failure(result, evidence)
        if failure is not None:
            metrics.failure_output = strip_failure_markers(failure.diagnostic)
            metrics.returncode = 2 if failure.kind == "infrastructure" else 1
            if failure.kind == "infrastructure":
                metrics.infra_error = failure.reason
            return
        text = "\n".join((stdout, stderr, evidence))
        cause = first_tool_diagnostic(text, default="no route completion evidence")
        metrics.returncode = 2
        metrics.infra_error = f"Vivado did not reach route_design: {cause}"
        metrics.failure_output = self._failure_tail(stdout + "\n" + evidence, stderr).strip()

    def _route_evidence(
        self, work_root: Path, *, min_mtime: float | None = None
    ) -> tuple[bool, str]:
        """Accept only an implementation run's fresh route witness and reports."""
        patterns = ("*_utilization_placed.rpt", "*_timing_summary_routed.rpt", "*_drc_routed.rpt")
        for impl_dir in sorted(work_root.glob("*.runs/impl_1")):
            runlog = impl_dir / "runme.log"
            try:
                if self._is_stale_artifact(runlog, min_mtime):
                    continue
                text = runlog.read_text(errors="replace")
            except OSError:
                continue
            if not re.search(
                r"Implementation completed successfully|route_design completed successfully", text
            ):
                continue
            for pattern in patterns:
                valid = False
                for report in impl_dir.glob(pattern):
                    with suppress(OSError):
                        valid |= not self._is_stale_artifact(report, min_mtime) and bool(
                            report.read_bytes()
                        )
                if not valid:
                    return True, f"Vivado missing fresh nonempty required report: {pattern}"
            return True, ""
        return False, ""

    def _load_cached_metrics(
        self,
        target: str,
        work_root: Path,
        fingerprint: str,
        *,
        require_bitstream: bool,
    ) -> FpgaMetrics | None:
        """Re-parse a byte-validated hit so current verdict logic still applies."""
        hit = fpga_cache.load(
            work_root,
            fingerprint,
            require_bitstream=require_bitstream,
        )
        if hit is None:
            return None
        route_completed, report_error = self._route_evidence(work_root)
        if not route_completed or report_error:
            return None
        parsed = fpga_edam.parse_fpga_reports(hit.report_text)
        parsed["exit_code"] = 0
        metrics = self._metrics_with_allowances(parsed, 0.0)
        if not metrics.passed:
            return None
        metrics.route_completed = True
        metrics.cached = True
        metrics.cache_fingerprint = hit.fingerprint
        metrics.run_evidence = hit.producer_evidence
        metrics.dirs = self._artifact_dirs(work_root)
        from booley.flows.terminal_progress import announce_unit

        announce_unit("verified build reuse", target=target)
        return metrics

    def _persist_fpga_log(self, target: str, text: str) -> str:
        """Write *target*'s full combined run log to its Edalize work dir.

        Lands as ``run.log`` in the per-target Edalize work root the FuseSoC
        resolution already uses. Reuses the Flow-neutral :func:`write_run_log`
        for its tail-cap + atomic-write semantics (mirrors asic's
        ``_persist_synth_log``). Returns the project-relative path, or ``""``
        when the write failed — a log-write problem must never fail an
        otherwise-finished run.
        """
        from booley.flows.run_log import write_run_log

        log_dir = work_root_for(self.args.work_dir, self.name, target)
        try:
            make_directories(log_dir)
            log_path = write_run_log(log_dir, text)
        except OSError:
            logger.debug(
                "fpga_impl %s: failed to persist run.log in %s", target, log_dir, exc_info=True
            )
            return ""
        return posix_relpath(log_path, self.args.work_dir)

    def _publish_numbered_fpga_log(self, target: str, text: str) -> str:
        """Publish captured output as best-effort invocation evidence."""
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is None:
            return ""
        try:
            return artifacts.publish_bytes(
                invocation_dir,
                ("artifacts", f"fpga_{target_report_slug(target)}", "run.log"),
                text.encode(),
                work_dir=Path(self.args.work_dir),
            )
        except OSError:
            logger.warning("could not publish numbered FPGA log for %s", target, exc_info=True)
            return ""

    @staticmethod
    def _failure_tail(log_text: str, stderr_text: str) -> str:
        """Build the log/stderr tail appended to an fpga_impl infra error.

        make's real error lands on stderr (the executor separates the streams),
        so surface both — swallowing stderr is exactly
        what made the original "Entering/Leaving directory"-only failures
        undiagnosable.
        """
        sections = []
        stdout_tail = "\n".join(strip_failure_markers(log_text).splitlines()[-40:])
        if stdout_tail.strip():
            sections.append(f" Log tail:\n{stdout_tail}")
        stderr_tail = "\n".join(strip_failure_markers(stderr_text).splitlines()[-40:])
        if stderr_tail.strip():
            sections.append(f" stderr tail:\n{stderr_tail}")
        return "".join(sections)

    def _collect_vivado_evidence(self, work_root: Path, *, min_mtime: float | None = None) -> str:
        """Concatenate fresh Vivado synthesis and implementation evidence.

        The edalize vivado flow runs project-mode (``launch_runs impl_1``), which
        emits its reports as files under ``<project>.runs/impl_1/`` rather than to
        stdout. Gather the routed utilization / timing-summary / DRC reports plus
        the impl run log (its ``route_design completed successfully`` marker is the
        authoritative route-success signal). Best-effort: a missing/unreadable
        report just contributes nothing, so a genuinely failed route yields no
        success marker and the caller falls back to the exit code.

        When *min_mtime* is given (the dispatch instant), files last modified
        before it are skipped: they belong to a previous run, not this one, and
        parsing them would fabricate a stale "cached" result when the current
        boundary command produced no fresh reports.

        Returns the concatenated text only. Pointers to the individual files
        are no longer derived here — the report names the *directories* these
        live in (:meth:`_artifact_dirs`) and lets the reader list them, which
        cannot go stale as Vivado's report set changes between versions.
        """
        patterns = (
            "*_utilization_placed.rpt",
            "*_timing_summary_routed.rpt",
            "*_drc_routed.rpt",
        )
        parts: list[str] = []
        for impl_dir in sorted(work_root.glob("*.runs/impl_1")):
            for pattern in patterns:
                for rpt in sorted(impl_dir.glob(pattern)):
                    if self._is_stale_artifact(rpt, min_mtime):
                        continue
                    try:
                        parts.append(rpt.read_text(errors="replace"))
                    except OSError:
                        logger.debug("fpga_impl: could not read %s", rpt, exc_info=True)
            runlog = impl_dir / "runme.log"
            if runlog.is_file() and not self._is_stale_artifact(runlog, min_mtime):
                try:
                    parts.append(runlog.read_text(errors="replace"))
                except OSError:
                    logger.debug("fpga_impl: could not read %s", runlog, exc_info=True)
        for synth_dir in sorted(work_root.glob("*.runs/synth_1")):
            runlog = synth_dir / "runme.log"
            if runlog.is_file() and not self._is_stale_artifact(runlog, min_mtime):
                try:
                    parts.append(runlog.read_text(errors="replace"))
                except OSError:
                    logger.debug("fpga_impl: could not read %s", runlog, exc_info=True)
        return "\n".join(parts)

    def _collect_route_reports(self, work_root: Path, *, min_mtime: float | None = None) -> str:
        """Compatibility alias for callers collecting all Vivado evidence."""
        return self._collect_vivado_evidence(work_root, min_mtime=min_mtime)

    @staticmethod
    def _classify_vivado_failure(result: SubprocessResult, evidence: str) -> EdaFailure | None:
        """Separate narrowly evidenced HDL rejection from execution failure."""
        combined = SubprocessResult(
            returncode=result.returncode,
            stdout="\n".join(part for part in (result.stdout, evidence) if part),
            stderr=result.stderr,
            timed_out=result.timed_out,
            oom_kill_delta=result.oom_kill_delta,
            oom_killed=result.oom_killed,
        )
        infrastructure = classify_eda_failure(
            combined,
            expected_stage="vivado",
            expected_executable="vivado",
        )
        if infrastructure is not None:
            return infrastructure
        match = re.search(
            r"^ERROR:\s*\[Synth\s+8-\d+\]"
            r"(?=[^\r\n]*(?:syntax|parse|unexpected\s+token|port))[^\r\n]*$",
            combined.stdout + "\n" + combined.stderr,
            re.IGNORECASE | re.MULTILINE,
        )
        if match is not None:
            diagnostic = match.group(0).strip()
            return EdaFailure("design", "design", "vivado", "", diagnostic, diagnostic)
        return None

    def _artifact_dirs(self, work_root: Path) -> dict[str, str]:
        """The directories holding this run's Vivado artifacts, by role.

        ``build`` is the Edalize work root (``vivado.log``, ``run.log``, the
        ``.xpr``); ``impl`` and ``synth`` are the project-mode run dirs holding
        every report, log and checkpoint each stage produced.

        Directories rather than a file inventory on purpose. Vivado writes a
        dozen-plus reports per run and renames them between versions; the
        enumerated version of this cost 90% of the report's bytes to restate
        filenames the reader could have listed, and its kind-derivation logic
        was the source of two bugs (a DRC/methodology-DRC collision and a
        half-stripped design prefix). A directory listing cannot be wrong.
        """
        dirs: dict[str, Path] = {"build": work_root}
        for run_dir in sorted(work_root.glob("*.runs/*")):
            if not run_dir.is_dir():
                continue
            role = "impl" if run_dir.name.startswith("impl") else "synth"
            dirs.setdefault(role, run_dir)
        return artifacts.artifacts_block(self.args.work_dir, dirs=dirs).get("dirs", {})  # type: ignore[return-value]

    def _metrics_with_allowances(self, raw: dict[str, Any], elapsed_s: float) -> FpgaMetrics:
        """Parse *raw* reports and attach the declared ``[flows.fpga]`` allowances.

        Used before the cache-store decision and on every cache reload, so a
        cached route is reused only while it passes under the current allowance.
        """
        metrics = self._metrics_from_parsed_reports(raw, elapsed_s)
        metrics.expected_latches = expected_count("fpga", self.args.work_dir, "expected_latches")
        metrics.expected_comb_loops = expected_count(
            "fpga", self.args.work_dir, "expected_comb_loops"
        )
        return metrics

    @staticmethod
    def _metrics_from_parsed_reports(raw: dict[str, Any], elapsed_s: float) -> FpgaMetrics:
        if not isinstance(raw, dict):
            return FpgaMetrics(returncode=2, infra_error="parsed reports were not an object")
        # Fmax/critical-path are inherently per-clock (parse_fpga_reports joins
        # Vivado's Clock Summary periods with the Intra Clock Table slacks); the
        # aggregate wns_ns/whs_ns below stay as honest whole-design worst-case.
        metrics = FpgaMetrics(
            lut_count=_int_metric(raw, "lut_count"),
            ff_count=_int_metric(raw, "ff_count"),
            bram_count=_float_metric(raw, "bram_count"),
            dsp_count=_int_metric(raw, "dsp_count"),
            wns_ns=_float_metric(raw, "wns_ns"),
            whs_ns=_float_metric(raw, "whs_ns"),
            per_clock=per_clock_from_json(raw.get("per_clock")),
            elapsed_s=elapsed_s,
            latches=_int_metric(raw, "latch_count") or 0,
            comb_loops=_int_metric(raw, "comb_loop_count") or 0,
            multi_driven=_int_metric(raw, "multi_driven_count") or 0,
        )
        status = raw.get("status")
        if status not in (None, "pass", "success"):
            metrics.returncode = 1
        exit_code = _int_metric(raw, "exit_code")
        if exit_code not in (None, 0):
            metrics.returncode = exit_code
        return metrics

    def _append_target_notes(
        self,
        lines: list[str],
        failures: list[str],
        configs: list[str],
        current_results: dict[str, FpgaMetrics],
    ) -> None:
        """Append every Target's notes to *lines* and its failure summary to *failures*."""
        for cfg in configs:
            cur = current_results[cfg]
            if cur.has_critical:
                lines.append(self._format_critical_line(cfg, cur))
            if cur.failure_output:
                lines.append(self._format_failure_output(cfg, cur))
            if cur.log_path:
                lines.append(f"  {cfg}: log: {cur.log_path}")
            if not cur.passed:
                failures.append(self._format_failure_summary(cfg, cur))

    def _summary_table_lines(
        self,
        configs: list[str],
        current_results: dict[str, FpgaMetrics],
        baseline_results: dict[str, FpgaMetrics],
        reports: dict[str, ImplementationReport],
    ) -> list[str]:
        """The per-Target table; baseline deltas sit on a dimmed row under their Target."""
        headers = ["Target", "LUT", "FF", "BRAM", "DSP", "WNS", "WHS", "Fmax", "Time", "Status"]
        rows: list[list[str]] = []
        for cfg in configs:
            cur = current_results[cfg]
            rows.append(
                self._summary_row(cfg, cur, comparison_error=reports[cfg].grade == "error")
            )
            delta_row = self._delta_row(cur, baseline_results.get(cfg))
            if delta_row:
                rows.append(delta_row)
        return render_table(headers, rows, max_widths={0: TARGET_WIDTH})

    @staticmethod
    def _summary_row(cfg: str, cur: FpgaMetrics, *, comparison_error: bool) -> list[str]:
        """Cells of one Target's table row (``—`` for a metric the run lacks)."""
        # Timing-worst clock, named only for multi-clock designs.
        worst = worst_clock(cur.per_clock)
        fmax = MISSING
        if worst is not None and worst.fmax_mhz is not None:
            tag = f" [{worst.clock}]" if len(cur.per_clock) > 1 else ""
            fmax = f"{worst.fmax_mhz:.1f} MHz{tag}"
        if cur.infra_error:
            status = "ERROR"
        elif comparison_error:
            # The baseline could not be compared: RESULT is ERROR and names this Target,
            # even when its own run also failed.
            status = "ERROR (baseline)"
        elif not cur.passed:
            status = "FAIL"
        else:
            status = "PASS"
        return [
            cfg,
            f"{cur.lut_count:,}" if cur.lut_count is not None else MISSING,
            f"{cur.ff_count:,}" if cur.ff_count is not None else MISSING,
            str(cur.bram_count) if cur.bram_count is not None else MISSING,
            str(cur.dsp_count) if cur.dsp_count is not None else MISSING,
            f"{cur.wns_ns:.3f} ns" if cur.wns_ns is not None else MISSING,
            f"{cur.whs_ns:.3f} ns" if cur.whs_ns is not None else MISSING,
            fmax,
            "cached" if cur.cached else f"{cur.elapsed_s:.1f}s",
            status_cell(status),
        ]

    @staticmethod
    def _delta_row(cur: FpgaMetrics, base: FpgaMetrics | None) -> list[str]:
        """Dimmed ``Δ +1.0%`` cells under LUT/FF/BRAM/DSP; empty without a baseline delta."""
        if base is None:
            return []
        deltas = [
            _delta_pct(getattr(cur, field), getattr(base, field))
            for field in ("lut_count", "ff_count", "bram_count", "dsp_count")
        ]
        if all(pct is None for pct in deltas):
            return []
        return ["", *(colors.dim(f"Δ {pct:+.1f}%") if pct is not None else "" for pct in deltas)]

    @staticmethod
    def _aggregate_detail(
        configs: list[str],
        current_results: dict[str, FpgaMetrics],
        aggregate: ImplementationAggregate,
    ) -> dict[str, Any]:
        detail: dict[str, Any] = {}
        for cfg in configs:
            cur = current_results[cfg]
            block = {
                **({"log": cur.log_path} if cur.log_path else {}),
                **({"live_dirs": dict(cur.dirs)} if cur.dirs else {}),
            }
            if block:
                detail.setdefault("artifacts", {})[cfg] = block
            detail.setdefault("cache", {})[cfg] = {
                "cached": cur.cached,
                "fingerprint": cur.cache_fingerprint or None,
            }
        detail["implementation"] = aggregate.detail
        return detail

    @staticmethod
    def _format_config_line(
        cfg: str, cur: FpgaMetrics, base: FpgaMetrics | None, *, human: bool = False
    ) -> str:
        parts = [
            f"{cur.lut_count:,} LUTs" if cur.lut_count is not None else "-- LUTs",
            f"{cur.ff_count:,} FFs" if cur.ff_count is not None else "-- FFs",
        ]
        if cur.bram_count is not None:
            parts.append(f"{cur.bram_count} BRAMs")
        if cur.dsp_count is not None:
            parts.append(f"{cur.dsp_count} DSPs")
        if cur.wns_ns is not None:
            parts.append(f"WNS {cur.wns_ns:.3f} ns")
        if cur.whs_ns is not None:
            parts.append(f"WHS {cur.whs_ns:.3f} ns")
        # One representative Fmax on the summary line = the timing-worst clock
        # (lowest Fmax). The authoritative per-clock breakdown is in the report.
        worst = worst_clock(cur.per_clock)
        if worst is not None and worst.fmax_mhz is not None:
            label = f"{worst.fmax_mhz:.1f} MHz"
            if len(cur.per_clock) > 1:
                label += f" (worst: {worst.clock})"
            parts.append(label)
        parts.append(
            "cached"
            if cur.cached
            else (format_duration(cur.elapsed_s) if human else f"{cur.elapsed_s:.1f}s")
        )
        if base:
            deltas = []
            for label, cur_val, base_val in (
                ("LUTs", cur.lut_count, base.lut_count),
                ("FFs", cur.ff_count, base.ff_count),
                ("BRAMs", cur.bram_count, base.bram_count),
                ("DSPs", cur.dsp_count, base.dsp_count),
            ):
                pct = _delta_pct(cur_val, base_val)
                if pct is not None:
                    sign = "+" if pct >= 0 else ""
                    deltas.append(f"{label} {sign}{pct:.1f}%")
            if deltas:
                parts.append("(" + ", ".join(deltas) + ")")
        suffix = ""
        if cur.infra_error:
            suffix = " ERROR"
        elif not cur.passed:
            suffix = " FAIL"
        line = f"[fpga] {cfg:<16}" + "   ".join(parts) + suffix
        return (
            ProgressText(line, record_text=FpgaImplFlow._format_config_line(cfg, cur, base))
            if human
            else line
        )

    @staticmethod
    def _format_critical_line(cfg: str, cur: FpgaMetrics) -> str:
        return note_line(cfg, "CRITICAL", cur.critical_summary())

    @staticmethod
    def _format_failure_output(cfg: str, cur: FpgaMetrics) -> str:
        """Render the captured log/stderr tail under the config line."""
        indented = "\n".join(f"    {ln}" for ln in cur.failure_output.splitlines())
        return f"  {cfg}: subprocess output:\n{indented}"

    @staticmethod
    def _format_failure_summary(cfg: str, cur: FpgaMetrics) -> str:
        if cur.infra_error:
            return f"{cfg}: infrastructure error: {cur.infra_error}"
        if cur.timed_out:
            return f"{cfg}: timeout"
        if not cur.has_primary_metrics:
            return f"{cfg}: missing LUT/FF metrics"
        if not cur.timing_met:
            return f"{cfg}: timing not met"
        if cur.has_critical:
            return f"{cfg}: critical conditions: {cur.critical_summary()}"
        return f"{cfg}: failed"

    def _build_implementation_report(
        self,
        context: ImplementationContext,
        current: FpgaMetrics,
        baseline: FpgaMetrics | None,
    ) -> ImplementationReport:
        """Adapt native FPGA evidence to the shared canonical schema."""
        return build_fpga_implementation_report(context, current, baseline)

    def _target_report_payload(
        self,
        target: str,
        current: FpgaMetrics,
        baseline: FpgaMetrics | None,
        baseline_ref: str | None,
        report_path: Path,
    ) -> dict[str, Any]:
        plan = self._target_plan(target)
        report = {
            "flow": self.name,
            "target": target,
            "eda_tool": "vivado",
            "timestamp": utc_now_rfc3339(),
            "passed": current.passed,
            "returncode": current.returncode,
            "timed_out": current.timed_out,
            "infra_error": current.infra_error,
            "cached": current.cached,
            "cache_fingerprint": current.cache_fingerprint or None,
            "metrics": _metrics_detail(current),
            "recipe_fingerprint": current.recipe_fingerprint or None,
            "recipe_snapshot": current.recipe_snapshot or None,
            "run_evidence": current.run_evidence or None,
            "cache_consumer_run_id": current.cache_consumer_run_id or None,
            "baseline_ref": baseline_ref,
            "baseline_metrics": _metrics_detail(baseline, baseline=True) if baseline else None,
            "baseline_recipe_fingerprint": baseline.recipe_fingerprint if baseline else None,
            "baseline_recipe_snapshot": baseline.recipe_snapshot if baseline else None,
            "baseline_run_evidence": baseline.run_evidence if baseline else None,
            "baseline_target": plan.baseline.selector,
            "candidate_target": plan.candidate.selector,
            "baseline_target_identity": plan.baseline.identity,
            "candidate_target_identity": plan.candidate.identity,
        }
        # Top-level ``artifacts`` mirrors the block inside ``metrics`` and adds
        # this file's own path, matching what the other Booley Flows emit — a
        # consumer holding only the report can find the log and the rest.
        report["artifacts"] = {
            "report": posix_relpath(report_path, self.args.work_dir),
            **({"log": current.log_path} if current.log_path else {}),
            **({"live_dirs": dict(current.dirs)} if current.dirs else {}),
        }
        return report


if __name__ == "__main__":
    FpgaImplFlow().cli()
