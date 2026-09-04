"""FpgaImplFlow - run FPGA implementation per config via the Edalize vivado flow.

Invocation is delegated to Edalize (ADR 0019): ``fusesoc run --setup`` resolves
the design-description, ``build_fpga_edam`` + Edalize ``configure()`` materialize
the Vivado project + Tcl in the Session Runtime, and the resolved ``make``
command runs there via ``BooleyFlow._execute_boundary``. Interpretation
stays in Booley: report collection, metric parsing
(:func:`fpga_edam.parse_fpga_reports`), Criteria, and baseline comparison.

There is one builder (the built-in Flow) and no execution-location knob. The
FPGA Flow owns its fixed Vivado backend; the resolved Target's EDA-selection
field controls FuseSoC resolution inputs, not the implementation backend.

``--baseline <ref>`` re-implements the design at a past commit in a throwaway
``git worktree`` (see :mod:`booley.flows.baseline_worktree`) rather than checking the ref
out in place, so it never disturbs the caller's tree and works in Interactive
Mode as well as Ticket Mode (ADR 0012).
"""

from __future__ import annotations

import argparse
import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from booley.core.boundary import (
    as_float,
    as_int,
    as_str,
    require_bool,
)
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_ERROR, EXIT_SUCCESS, McpToolResult
from booley.runtime import job_slots
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.flow_names import config_section
from booley.targets.parameter_integrity import validate_top_parameter_intent, vlogparam_values
from booley.targets.target import TargetHandle, select_targets

from .. import artifacts, run_evidence
from .. import edam as edam_layer
from ..base import BooleyFlow, SubprocessResult
from ..baseline_worktree import (
    BaselineWorktreeError,
    baseline_worktree,
    git_full_sha,
    git_short_sha,
    resolve_ticket_baseline,
)
from ..clock_timing import per_clock_from_json, worst_clock
from ..implementation_comparison import (
    ImplementationComparisonError,
    TargetPairPlan,
    baseline_execution_context,
    candidate_execution_refs,
    resolve_target_execution_ref,
    selected_target_handle,
    target_pair_plans_for_handles,
    target_plan_for_handle,
)
from ..implementation_publication import (
    ImplementationProgress,
    ImplementationPublisher,
    target_report_path,
)
from ..implementation_report import (
    ImplementationAggregate,
    ImplementationReport,
    build_implementation_aggregate,
)
from ..recipe_evidence import (
    BASELINE_RECIPE_FINGERPRINT_DETAIL,
    BASELINE_RECIPE_SNAPSHOT_DETAIL,
    BASELINE_REF_DETAIL,
    BASELINE_TARGET_DETAIL,
    CANDIDATE_TARGET_DETAIL,
    RECIPE_FINGERPRINT_DETAIL,
    RECIPE_SNAPSHOT_DETAIL,
)
from ..run_evidence import (
    BASELINE_RUN_EVIDENCE_DETAIL,
    RUN_EVIDENCE_DETAIL,
)
from .backends.vivado import cache as fpga_cache
from .backends.vivado import edam as fpga_edam
from .backends.vivado.metrics import (
    FpgaMetrics,
    _delta_pct,
    _first_valid_display,
    _metrics_detail,
    _split_resolved_sources,
    _unique_strings,
    _vlogdefine_args,
)
from .implementation_report import (
    build_fpga_implementation_report,
)
from .recipe import fpga_recipe_snapshot, fpga_recipe_snapshot_fingerprint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PreparedFpgaCommand:
    """Materialized command plus the exact inputs governing cache reuse."""

    run_cmd: list[str]
    work_root: Path
    fingerprint: str
    require_bitstream: bool
    recipe_snapshot: dict[str, Any] = field(default_factory=dict)
    recipe_fingerprint: str = ""
    run_evidence: dict[str, Any] = field(default_factory=dict)

    def __iter__(self):
        """Keep the historical ``run_cmd, work_root = ...`` test/API shape."""
        yield self.run_cmd
        yield self.work_root


@dataclass(frozen=True)
class _ResolvedFpgaRecipe:
    """Validated Target recipe shared by dry-run and real materialization."""

    target: str
    resolved: Any
    part: str
    top: str
    xdc_files: tuple[Path, ...]
    sv_files: tuple[Path, ...]
    v_files: tuple[Path, ...]
    include_dirs: tuple[Path, ...]
    defines: tuple[str, ...]
    vlogparams: dict[str, Any]
    out_of_context: bool
    recipe_snapshot: dict[str, Any]
    recipe_fingerprint: str
    source_evidence: run_evidence.FlowSourceEvidence


_FPGA_METRIC_MAP: dict[str, str] = {
    "lut_count": "lut_count",
    "ff_count": "ff_count",
    "bram_count": "bram_count",
    "dsp_count": "dsp_count",
    # Aggregate-timing + per-clock sub-metrics. Fmax/critical-path are addressed
    # per clock as "<clock>.<metric>_min|_max" (the threshold engine resolves the
    # dotted prefix into detail["per_clock"][clock]); wns_ns/whs_ns are also the
    # honest aggregate worst-case, addressable flat. One spelling table serves
    # both flat and per-clock lookups.
    "wns_ns": "wns_ns",
    "whs_ns": "whs_ns",
    "critical_path_ps": "critical_path_ps",
    "fmax_mhz": "fmax_mhz",
    "period_ns": "period_ns",
}


def _load_rtl_config(work_dir: Path) -> dict[str, Any]:
    try:
        from booley.runtime.shared_infra import _load_rtl_config as load_config

        return load_config(work_dir) or {}
    except Exception:  # noqa: BLE001 — best-effort config read; any failure degrades to empty config
        return {}


def _load_flow_config(work_dir: Path) -> dict[str, Any]:
    cfg = _load_rtl_config(work_dir)
    flows = cfg.get("flows", {}) if cfg else {}
    return config_section(flows, "fpga") if isinstance(flows, dict) else {}


def _resolve_fpga_timeout_ms(work_dir: Path | None, requested: Any = None) -> int:
    """Resolve the per-target FPGA implementation budget for MCP and Flow callers."""
    if requested is not None:
        try:
            return max(1, int(requested))
        except (TypeError, ValueError):
            return 7_200_000
    if work_dir is None:
        return 7_200_000
    configured = as_int(_load_flow_config(work_dir).get("timeout_ms"), 7_200_000)
    return max(1, configured if configured is not None else 7_200_000)


def _float_metric(data: dict[str, Any], key: str) -> float | None:
    """Report metric as float, or None when absent/non-numeric (bool rejected)."""
    return as_float(data.get(key), None)


def _int_metric(data: dict[str, Any], key: str) -> int | None:
    """Report metric as int, or None when absent/non-numeric (bool rejected)."""
    return as_int(data.get(key), None)


class FpgaImplFlow(BooleyFlow):
    """Run FPGA implementation for one or more Targets with optional baseline comparison.

    The description deliberately does not name Vivado: which EDA Flow backs a
    Booley Flow is the Target's and booley.toml's business, not part of the Booley Flow's
    identity, and the supported set moves (SUPPORTED-EDA-TOOLS.md owns that list).
    ``asic_synthesize`` reads the same way.
    """

    name: str = "fpga"
    description: str = (
        "Run FPGA implementation for one or more Targets with optional baseline comparison"
    )
    code_modifying: bool = False
    default_timeout: int = 7200
    # F-14: a passing route otherwise prints nothing on the CLI (its verdict
    # lives in display_lines, which a bare CLI run drops). Surface the RESULT:
    # summary on stdout so PASS is never indistinguishable from a no-op.
    announce_success_report: bool = True
    satisfies: ClassVar[list[str]] = ["fpga_impl_ok"]

    # FPGA implementation is always admitted as a heavy Session Runtime job.
    def _resolve_job_class(self) -> str:
        """FPGA implementation is a heavy Session Runtime workload."""
        return job_slots.CLASS_HEAVY

    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--baseline", default=None, help="Baseline git ref for comparison")
        parser.add_argument("--dry-run", action="store_true", default=False)
        parser.add_argument(
            "--no-cache",
            action="store_true",
            help="Bypass reusable implementation results and run the recipe again",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=None,
            help=(
                "Per-config timeout in milliseconds. Falls back to "
                "[flows.fpga].timeout_ms (both default to 7200000)."
            ),
        )

    def _build_command(self) -> list[str]:
        return []

    def _interpret_result(self, result: SubprocessResult) -> McpToolResult:
        return McpToolResult()

    def _timeout_ms(self) -> int:
        """Resolve the per-config timeout in ms.

        Precedence: explicit ``--timeout`` CLI/MCP arg (trusted, argparse-typed)
        > ``[flows.fpga].timeout_ms`` in booley.toml (validated) > a
        7200000 (2h) default. FPGA impl runs are legitimately long, so the
        default value is larger than asic synth's; only the resolution
        *mechanism* is unified with asic (mirrors ``_timeout_ms`` there).
        """
        return _resolve_fpga_timeout_ms(self.args.work_dir, self.args.timeout)

    def _get_timeout(self) -> int:
        """Per-config timeout in whole seconds (see :meth:`_timeout_ms`)."""
        return max(1, self._timeout_ms() // 1000)

    def _run(self) -> McpToolResult:
        # The initially selected worktree, captured before any baseline run
        # swaps ``self.args.work_dir`` to a throwaway worktree.  It distinguishes
        # primary-run artifacts from temporary baseline artifacts.
        self._project_root = Path(self.args.work_dir)
        self._baseline_full_sha: str | None = None
        handles = select_targets(self.args.work_dir, self.args.target, for_flow="fpga")
        self._target_handles = {handle.selector: handle for handle in handles}
        targets = [handle.selector for handle in handles]
        if not targets:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "fpga: no Target selected. Pass --target <name> "
                    "(bare name if unambiguous, else vlnv#name)."
                ),
            )

        comparison_error = self._prepare_target_pairs(handles)
        if comparison_error is not None:
            return comparison_error
        if not self._flow_enabled():
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="fpga is disabled ([flows.fpga].enabled = false).",
            )
        if self.args.dry_run:
            return self._dry_run(targets)
        self._eda_tool = "vivado"
        self._implementation_reports: dict[str, ImplementationReport] = {}
        self.reserve_invocation_dir()
        self._write_progress_report(targets, {}, {}, phase="starting")
        baseline_results, short_sha = self._run_baseline_configs(self._target_pairs)
        if isinstance(baseline_results, McpToolResult):
            return baseline_results
        current_results = self._run_current_targets(targets, baseline_results, short_sha)
        result = self._aggregate_results(targets, current_results, baseline_results, short_sha)
        self._write_progress_report(
            targets,
            current_results,
            baseline_results,
            phase="complete",
            baseline_ref=short_sha,
            complete=True,
        )
        return result

    def _run_current_targets(
        self,
        targets: list[str],
        baseline_results: dict[str, FpgaMetrics],
        short_sha: str | None,
    ) -> dict[str, FpgaMetrics]:
        current_results: dict[str, FpgaMetrics] = {}
        for tgt in targets:
            metrics = self._run_single_target(tgt)
            current_results[tgt] = metrics
            self._persist_target_outcome(
                tgt,
                metrics,
                baseline_results.get(tgt),
                short_sha,
            )
            self._write_progress_report(
                targets,
                current_results,
                baseline_results,
                phase="current",
                baseline_ref=short_sha,
            )
            if len(targets) > 1:
                self.emit_completion(
                    self._format_config_line(tgt, metrics, baseline_results.get(tgt))
                )
        return current_results

    def _prepare_target_pairs(self, handles: tuple[TargetHandle, ...]) -> McpToolResult | None:
        baseline_error = self._apply_ticket_baseline(handles)
        comparison_error: str | None = None
        try:
            self._target_pairs = target_pair_plans_for_handles(
                self.state.criteria,
                "fpga_impl_ok_",
                handles,
                contract=getattr(self, "_acceptance_basis", None),
                flow="fpga",
            )
            self._target_execution_refs = candidate_execution_refs(handles, self._target_pairs)
        except ImplementationComparisonError as exc:
            comparison_error = f"fpga: {exc}"
        if baseline_error is not None or comparison_error is not None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=baseline_error or comparison_error or "fpga comparison error",
            )
        return None

    def _apply_ticket_baseline(self, targets: tuple[TargetHandle, ...]) -> str | None:
        """Default relative ticket criteria to their immutable baseline SHA."""
        baseline, full_sha, error = resolve_ticket_baseline(
            self.state.criteria,
            "fpga_impl_ok_",
            targets,
            self.args.baseline,
            Path(self.args.work_dir),
            "fpga",
        )
        self.args.baseline = baseline
        self._baseline_full_sha = full_sha
        return error

    def _dry_run(self, targets: list[str]) -> McpToolResult:
        """Resolve every Target recipe before reporting an all-or-nothing preview."""
        recipes: list[_ResolvedFpgaRecipe] = []
        for target in targets:
            try:
                recipes.append(self._resolve_fpga_recipe(target))
            except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
                logger.debug("fpga dry-run setup failed for %s", target, exc_info=True)
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=f"fpga dry-run setup failed for {target}: {exc}",
                )
        lines = ["[fpga] dry-run mode (session-runtime)"]
        for recipe in recipes:
            lines.append(
                f"  target={recipe.target} part={recipe.part} top={recipe.top} "
                f"xdc={','.join(str(path) for path in recipe.xdc_files)}"
            )
            lines.append(f"  sv_files={len(recipe.sv_files)} v_files={len(recipe.v_files)}")
        return McpToolResult(exit_code=EXIT_SUCCESS, report_text="\n".join(lines))

    def _resolve_fpga_recipe(self, target: str) -> _ResolvedFpgaRecipe:
        """Resolve and validate every Target input shared with a real dispatch."""
        work_dir = Path(self.args.work_dir)
        build_root = edam_layer.work_root_for(work_dir, self.name, target, variant="fusesoc")
        handle = self._target_handle(target)
        ref = getattr(self, "_target_execution_refs", {}).get(target)
        if ref is not None:
            resolved = resolve_target_execution_ref(handle, ref, build_root=build_root)
        else:
            resolved = fusesoc_registry.resolve_target_handle(handle, build_root=build_root)
        validate_top_parameter_intent(resolved, flow="fpga")
        part = self._resolve_part(resolved.flow_options)
        xdc_files = tuple(self._resolve_xdc_files(resolved, target))
        top = str(resolved.toplevel or "")
        if not top:
            raise ValueError(
                f"fpga: top module not found for target {target!r} (set the Target toplevel)"
            )
        sv_files, v_files, include_dirs = _split_resolved_sources(resolved)
        out_of_context = require_bool(
            resolved.flow_options,
            "out_of_context",
            field=f"Target {target!r} flow_options.out_of_context",
        )
        snapshot = fpga_recipe_snapshot(resolved, target=target)
        fingerprint = fpga_recipe_snapshot_fingerprint(snapshot)
        source = run_evidence.capture_flow_source_evidence(work_dir, target)
        return _ResolvedFpgaRecipe(
            target=target,
            resolved=resolved,
            part=part,
            top=top,
            xdc_files=xdc_files,
            sv_files=tuple(sv_files),
            v_files=tuple(v_files),
            include_dirs=tuple(include_dirs),
            defines=tuple(_unique_strings(_vlogdefine_args(resolved.parameters))),
            vlogparams=vlogparam_values(resolved.parameters),
            out_of_context=out_of_context,
            recipe_snapshot=snapshot,
            recipe_fingerprint=fingerprint,
            source_evidence=source,
        )

    def _target_handle(self, target: str) -> TargetHandle:
        """Return the selected handle, reselecting only in a baseline checkout."""
        return selected_target_handle(
            getattr(self, "_target_handles", {}),
            self.args.work_dir,
            target,
            flow="fpga",
        )

    def _target_plan(self, target: str) -> TargetPairPlan:
        """Look up the total pair plan by canonical candidate identity."""
        return target_plan_for_handle(
            getattr(self, "_target_pairs", ()),
            self._target_handle(target),
            flow="fpga",
            sealed=getattr(self, "_acceptance_basis", None) is not None,
        )

    def _prepare_fpga_command(
        self,
        target: str,
    ) -> _PreparedFpgaCommand:
        """Materialize the Edalize vivado project and return (run_cmd, work_root).

        The single seam between EDAM generation / in-process ``configure()`` and
        execution (mirrors ``simulate._prepare_sim_command``). Per ADR 0022
        (decision 4) FuseSoC owns *design-description*: ``resolve_target`` runs
        ``fusesoc run --setup`` and leaves a resolved ``.eda.yml`` listing the RTL
        sources, top module, and typed parameters. Only this **resolution half**
        is swapped — the vivado boundary crossing (ADR 0019/0037) and the
        EDAM-builder command-gen exception are preserved: the resolved
        sources/top/defines are
        fed *into* ``build_fpga_edam`` (whose ``configure()`` materializes the
        vivado project) instead of the legacy-registry-derived ones.

        The Target owns Vivado's part, constraints, defines, and out-of-context
        setting. A ``--baseline`` re-resolve runs against a throwaway worktree
        (``self.args.work_dir``
        points at it), so its build dir is physically separate from the current
        run's and cannot clobber it.

        Raises ``ValueError`` / ``EdamSecurityError`` on bad inputs and
        ``TargetResolutionError`` on FuseSoC setup failure (the caller records
        all three as infra errors).
        """
        work_dir = Path(self.args.work_dir)
        recipe = self._resolve_fpga_recipe(target)
        work_root = edam_layer.work_root_for(work_dir, "fpga", target)
        edam = fpga_edam.build_fpga_edam(
            name=f"fpga_{target}",
            toplevel=recipe.top,
            part=recipe.part,
            sv_files=list(recipe.sv_files),
            v_files=list(recipe.v_files),
            include_dirs=list(recipe.include_dirs),
            xdc_files=list(recipe.xdc_files),
            defines=list(recipe.defines),
            vlogparams=recipe.vlogparams,
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
        # QoR-gate targets whose bare toplevel out-ports the package (e.g. an
        # engine block never meant for pin mapping) opt into OOC synthesis so
        # placement does not fail on IO-buffer overutilization. Strictly typed:
        # a string ``"false"`` is truthy and would silently enable OOC, so a
        # non-bool raises (BoundaryError is a ValueError → infra error upstream).
        if recipe.out_of_context:
            fpga_edam.enable_out_of_context(work_root, project_name)
        run_cmd = fpga_edam.fpga_run_command(work_root, Path(self.args.work_dir))
        fingerprint = fpga_cache.input_fingerprint(
            recipe.resolved,
            edam,
            out_of_context=recipe.out_of_context,
        )
        dispatch_evidence = run_evidence.build_flow_run_evidence(
            flow=self.name,
            target=target,
            recipe_sha256=recipe.recipe_fingerprint,
            work_dir=work_dir,
            source_evidence=recipe.source_evidence,
        )
        return _PreparedFpgaCommand(
            run_cmd=run_cmd,
            work_root=work_root,
            fingerprint=fingerprint,
            require_bitstream=not recipe.out_of_context,
            recipe_snapshot=recipe.recipe_snapshot,
            recipe_fingerprint=recipe.recipe_fingerprint,
            run_evidence=dispatch_evidence.as_dict(),
        )

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
            "lives with the design, not on [flows.fpga] (ADR 0031)."
        )

    def _run_single_target(self, target: str) -> FpgaMetrics:
        """Configure, run, and interpret Vivado inside the Session Runtime."""
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
        self._open_run_log(target, work_root)
        # The command is not path-remapped: ``make -C <rel>`` resolves from the
        # shared Session Runtime workspace.
        result = self._execute_boundary(run_cmd, timeout=self._get_timeout())
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
        report_text = self._collect_route_reports(work_root, min_mtime=result.dispatched_unix)
        metric_dict = fpga_edam.parse_fpga_reports(
            log_text + "\n" + report_text if report_text else log_text
        )

        # QoR flow stops at route_design: a boardless soft IP cannot write a
        # bitstream (write_bitstream's NSTD-1/UCIO-1 DRC precondition fails with
        # no pinout), so ``make`` exits non-zero even when synth+place+route fully
        # succeed. fpga_impl is a QoR Flow — route completion (the report files +
        # the route-done marker parse_fpga_reports keys ``status`` on) defines
        # success, NOT the bitstream/make exit code. Only when route did *not*
        # complete do we surface the boundary command's exit code as the failure.
        route_completed = metric_dict.get("status") in ("pass", "success")
        metric_dict["exit_code"] = 0 if route_completed else result.returncode
        metrics = self._metrics_from_parsed_reports(metric_dict, result.duration_s)
        metrics.cache_fingerprint = fingerprint
        if result.timed_out:
            metrics.timed_out = True
        if metrics.returncode != 0 and not metrics.infra_error:
            # infra_error stays the concise reason; the bulky log/stderr tail
            # goes into failure_output (mirrors asic), so the report can surface
            # it separately from the one-line criterion reason.
            metrics.infra_error = (
                f"Vivado (edalize) did not reach route_design (exit {metrics.returncode})."
            )
            metrics.failure_output = self._failure_tail(log_text, stderr_text).strip()
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
            metrics.log_path = self._persist_fpga_log(target, combined)
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
        parsed = fpga_edam.parse_fpga_reports(hit.report_text)
        parsed["exit_code"] = 0
        metrics = self._metrics_from_parsed_reports(parsed, 0.0)
        if not metrics.passed:
            return None
        metrics.cached = True
        metrics.cache_fingerprint = hit.fingerprint
        metrics.run_evidence = hit.producer_evidence
        metrics.dirs = self._artifact_dirs(work_root)
        self._attach_existing_log(target, metrics)
        return metrics

    def _attach_existing_log(self, target: str, metrics: FpgaMetrics) -> None:
        """Point a cache hit at the previous successful run log when it exists."""
        if Path(self.args.work_dir) != getattr(self, "_project_root", None):
            return
        path = edam_layer.work_root_for(self.args.work_dir, self.name, target) / "run.log"
        if path.is_file():
            metrics.log_path = posix_relpath(path, self.args.work_dir)

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

        log_dir = edam_layer.work_root_for(self.args.work_dir, self.name, target)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = write_run_log(log_dir, text)
        except OSError:
            logger.debug(
                "fpga_impl %s: failed to persist run.log in %s", target, log_dir, exc_info=True
            )
            return ""
        return posix_relpath(log_path, self.args.work_dir)

    @staticmethod
    def _failure_tail(log_text: str, stderr_text: str) -> str:
        """Build the log/stderr tail appended to an fpga_impl infra error.

        make's real error lands on stderr (the executor separates the streams),
        so surface both — swallowing stderr is exactly
        what made the original "Entering/Leaving directory"-only failures
        undiagnosable.
        """
        sections = []
        stdout_tail = "\n".join(log_text.splitlines()[-40:])
        if stdout_tail.strip():
            sections.append(f" Log tail:\n{stdout_tail}")
        stderr_tail = "\n".join(stderr_text.splitlines()[-40:])
        if stderr_tail.strip():
            sections.append(f" stderr tail:\n{stderr_tail}")
        return "".join(sections)

    def _collect_route_reports(self, work_root: Path, *, min_mtime: float | None = None) -> str:
        """Concatenate the Vivado-generated route reports for the post-processor.

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
        return "\n".join(parts)

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

    def _run_baseline_configs(
        self,
        pairs: tuple[TargetPairPlan, ...],
    ) -> tuple[dict[str, FpgaMetrics] | McpToolResult, str | None]:
        """Implement *configs* at ``--baseline`` in a throwaway worktree.

        Returns ``(results_dict, short_sha)``, or ``(McpToolResult, None)`` when the
        worktree could not be created. The baseline ref is materialized in an
        ephemeral ``git worktree`` under the project (inside the Session
        Runtime workspace) rather than checked out in
        place, so the caller's tree is untouched and this works in Interactive
        Mode too (ADR 0012). ``self.args.work_dir`` is pointed at the worktree
        for the duration; ``self._project_root`` (set in :meth:`_run`) preserves
        the initially selected checkout. Route reports and any failure tail are parsed
        into the returned in-memory metrics before the worktree dies.
        """
        baseline_ref = self.args.baseline
        if not baseline_ref:
            return {}, None

        project_root = Path(self.args.work_dir)
        short_sha = git_short_sha(baseline_ref, project_root)
        full_sha = git_full_sha(str(baseline_ref), project_root)
        if full_sha is not None:
            self._baseline_full_sha = full_sha
        try:
            with baseline_worktree(project_root, baseline_ref) as wt:
                self.args.work_dir = wt
                current_handles = self._target_handles
                current_refs = getattr(self, "_target_execution_refs", {})
                self._target_handles, self._target_execution_refs = baseline_execution_context(
                    pairs, wt
                )
                try:
                    baseline_results = self._execute_baseline_pairs(pairs, short_sha)
                finally:
                    self._target_handles = current_handles
                    self._target_execution_refs = current_refs
                    self.args.work_dir = project_root
        except (BaselineWorktreeError, ImplementationComparisonError) as exc:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"fpga: {exc}",
            ), None

        return baseline_results, short_sha

    def _execute_baseline_pairs(
        self,
        pairs: tuple[TargetPairPlan, ...],
        short_sha: str,
    ) -> dict[str, FpgaMetrics]:
        results: dict[str, FpgaMetrics] = {}
        executed: dict[str, FpgaMetrics] = {}
        targets = [plan.candidate.selector for plan in pairs]
        for plan in pairs:
            baseline = plan.baseline.selector
            candidate = plan.candidate.selector
            if baseline not in executed:
                executed[baseline] = self._run_single_target(baseline)
            results[candidate] = copy.deepcopy(executed[baseline])
            self._write_progress_report(
                targets, {}, results, phase="baseline", baseline_ref=short_sha
            )
        return results

    def _implementation_report(
        self,
        target: str,
        current: FpgaMetrics,
        baseline: FpgaMetrics | None,
        baseline_ref: str | None,
    ) -> ImplementationReport:
        """Adapt native FPGA evidence to the shared canonical schema."""
        plan = self._target_plan(target)
        return build_fpga_implementation_report(
            target=target,
            pair=plan.evidence_pair(),
            current=current,
            baseline=baseline,
            baseline_ref=self.args.baseline if baseline is not None else None,
            resolved_baseline_ref=(
                getattr(self, "_baseline_full_sha", None) or baseline_ref
                if baseline is not None
                else None
            ),
            eda_tool=self._eda_tool,
            baseline_target_identity=plan.baseline.identity,
            candidate_target_identity=plan.candidate.identity,
        )

    def _publisher(self) -> ImplementationPublisher:
        return ImplementationPublisher(
            work_dir=Path(self.args.work_dir),
            report_dir=self.args.report_dir,
            invocation_dir=self.reserve_invocation_dir(),
        )

    def _write_progress_report(
        self,
        targets: list[str],
        current_results: dict[str, FpgaMetrics],
        baseline_results: dict[str, FpgaMetrics],
        *,
        phase: str,
        baseline_ref: str | None = None,
        complete: bool = False,
    ) -> None:
        progress = ImplementationProgress(
            flow=self.name,
            run_id=os.environ.get("BOOLEY_RUN_ID", ""),
            targets=tuple(targets),
            completed_targets=tuple(current_results),
            baseline_completed_targets=tuple(baseline_results),
            phase=phase,
            complete=complete,
            baseline_ref=baseline_ref,
            reports=getattr(self, "_implementation_reports", {}),
        )
        self._publisher().publish_progress(progress)

    def _persist_target_outcome(
        self,
        target: str,
        current: FpgaMetrics,
        baseline: FpgaMetrics | None,
        baseline_ref: str | None,
    ) -> None:
        implementation = self._implementation_report(target, current, baseline, baseline_ref)
        implementation = self._write_target_report(
            target, current, baseline, baseline_ref, implementation=implementation
        )
        self._implementation_reports[target] = implementation
        if implementation.grade == "error":
            return
        self._set_config_criterion(
            target,
            current,
            baseline,
            baseline_ref,
            implementation=implementation,
        )
        if self.state._file_path is not None:
            self.state.save()

    @staticmethod
    def _baseline_error_lines(
        baseline_results: dict[str, FpgaMetrics],
        reports: dict[str, ImplementationReport],
    ) -> tuple[list[str], list[str]]:
        lines: list[str] = []
        failures: list[str] = []
        for target, metrics in baseline_results.items():
            if metrics.infra_error:
                lines.append(f"[fpga] baseline {target}: ERROR -- {metrics.infra_error}")
                failures.append(f"baseline {target}: infrastructure error")
        for target, report in reports.items():
            comparison = report.canonical.get("comparison")
            errors = comparison.get("basis_errors", []) if isinstance(comparison, dict) else []
            for message in errors:
                if str(message).startswith("baseline infrastructure error:"):
                    continue
                lines.append(f"[fpga] comparison {target}: ERROR -- {message}")
                failures.append(f"comparison {target}: invalid evidence")
        return lines, failures

    def _aggregate_results(
        self,
        configs: list[str],
        current_results: dict[str, FpgaMetrics],
        baseline_results: dict[str, FpgaMetrics],
        short_sha: str | None,
    ) -> McpToolResult:
        implementation_reports = {
            target: getattr(self, "_implementation_reports", {}).get(target)
            or self._implementation_report(
                target,
                current_results[target],
                baseline_results.get(target),
                short_sha,
            )
            for target in configs
        }
        implementation_aggregate = build_implementation_aggregate(
            implementation_reports,
            baseline_ref=getattr(self, "_baseline_full_sha", None) or short_sha,
        )
        lines, failures = self._aggregate_head(short_sha, baseline_results, implementation_reports)
        self._append_target_results(lines, failures, configs, current_results, baseline_results)
        lines.append("")
        lines.append("RESULT: PASS" if not failures else f"RESULT: FAIL ({'; '.join(failures)})")
        return McpToolResult(
            exit_code=implementation_aggregate.exit_code,
            report_text="\n".join(lines),
            display_lines=_first_valid_display(configs, current_results),
            detail=self._aggregate_detail(configs, current_results, implementation_aggregate),
        )

    def _aggregate_head(
        self,
        short_sha: str | None,
        baseline_results: dict[str, FpgaMetrics],
        reports: dict[str, ImplementationReport],
    ) -> tuple[list[str], list[str]]:
        lines: list[str] = []
        if self.args.baseline and short_sha:
            lines.append(f"[fpga] baseline: {short_sha}")
        baseline_lines, baseline_failures = self._baseline_error_lines(baseline_results, reports)
        lines.extend(baseline_lines)
        return lines, baseline_failures

    def _append_target_results(
        self,
        lines: list[str],
        failures: list[str],
        configs: list[str],
        current_results: dict[str, FpgaMetrics],
        baseline_results: dict[str, FpgaMetrics],
    ) -> None:
        for cfg in configs:
            cur = current_results[cfg]
            base = baseline_results.get(cfg)
            lines.append(self._format_config_line(cfg, cur, base))
            if cur.has_critical:
                lines.append(self._format_critical_line(cfg, cur))
            if cur.failure_output:
                lines.append(self._format_failure_output(cfg, cur))
            if cur.log_path:
                lines.append(f"[fpga] {cfg}: log: {cur.log_path}")
            if not cur.passed:
                failures.append(self._format_failure_summary(cfg, cur))

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
                **({"dirs": dict(cur.dirs)} if cur.dirs else {}),
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
    def _format_config_line(cfg: str, cur: FpgaMetrics, base: FpgaMetrics | None) -> str:
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
        parts.append("cached" if cur.cached else f"{cur.elapsed_s:.1f}s")
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
        return f"[fpga] {cfg:<16}" + "   ".join(parts) + suffix

    @staticmethod
    def _format_critical_line(cfg: str, cur: FpgaMetrics) -> str:
        parts = []
        if cur.latches:
            parts.append(f"{cur.latches} latches")
        if cur.comb_loops:
            parts.append(f"{cur.comb_loops} comb loops")
        if cur.multi_driven:
            parts.append(f"{cur.multi_driven} multi-driven")
        return f"[fpga] {cfg}: CRITICAL -- {', '.join(parts)}"

    @staticmethod
    def _format_failure_output(cfg: str, cur: FpgaMetrics) -> str:
        """Render the captured log/stderr tail under the config line."""
        indented = "\n".join(f"    {ln}" for ln in cur.failure_output.splitlines())
        return f"[fpga] {cfg}: subprocess output:\n{indented}"

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
            return f"{cfg}: critical conditions"
        return f"{cfg}: failed"

    def _write_target_report(
        self,
        cfg: str,
        cur: FpgaMetrics,
        base: FpgaMetrics | None,
        baseline_ref: str | None,
        *,
        implementation: ImplementationReport,
    ) -> ImplementationReport:
        report_dir = self.args.report_dir
        if report_dir is None:
            return implementation
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = target_report_path(self.name, cfg, report_dir)
        report = self._target_report_payload(cfg, cur, base, baseline_ref, report_path)
        report["passed"] = implementation.passed
        published = self._publisher().publish_report(implementation, report)
        return ImplementationReport(published.payload["implementation"])

    def _target_report_payload(
        self,
        cfg: str,
        cur: FpgaMetrics,
        base: FpgaMetrics | None,
        baseline_ref: str | None,
        report_path: Path,
    ) -> dict[str, Any]:
        plan = self._target_plan(cfg)
        report = {
            "flow": self.name,
            "target": cfg,
            "eda_tool": "vivado",
            "timestamp": utc_now_rfc3339(),
            "passed": cur.passed,
            "returncode": cur.returncode,
            "timed_out": cur.timed_out,
            "infra_error": cur.infra_error,
            "cached": cur.cached,
            "cache_fingerprint": cur.cache_fingerprint or None,
            "metrics": _metrics_detail(cur),
            "recipe_fingerprint": cur.recipe_fingerprint or None,
            "recipe_snapshot": cur.recipe_snapshot or None,
            "run_evidence": cur.run_evidence or None,
            "cache_consumer_run_id": cur.cache_consumer_run_id or None,
            "baseline_ref": baseline_ref,
            "baseline_metrics": _metrics_detail(base, baseline=True) if base else None,
            "baseline_recipe_fingerprint": base.recipe_fingerprint if base else None,
            "baseline_recipe_snapshot": base.recipe_snapshot if base else None,
            "baseline_run_evidence": base.run_evidence if base else None,
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
            **({"log": cur.log_path} if cur.log_path else {}),
            **({"dirs": dict(cur.dirs)} if cur.dirs else {}),
        }
        return report

    def _set_config_criterion(
        self,
        cfg: str,
        cur: FpgaMetrics,
        base: FpgaMetrics | None,
        baseline_ref: str | None,
        *,
        implementation: ImplementationReport | None = None,
    ) -> None:
        plan = self._target_plan(cfg)
        detail = {
            **(_metrics_detail(cur) or {}),
            "has_primary_metrics": cur.has_primary_metrics,
            "timing_met": cur.timing_met,
            "has_critical": cur.has_critical,
            "returncode": cur.returncode,
            "timed_out": cur.timed_out,
            "passed": cur.passed,
            RECIPE_FINGERPRINT_DETAIL: cur.recipe_fingerprint or None,
            RECIPE_SNAPSHOT_DETAIL: cur.recipe_snapshot or None,
            RUN_EVIDENCE_DETAIL: cur.run_evidence or None,
            BASELINE_TARGET_DETAIL: plan.baseline.selector,
            CANDIDATE_TARGET_DETAIL: plan.candidate.selector,
            "_baseline_target_identity": plan.baseline.identity,
            "_candidate_target_identity": plan.candidate.identity,
            "_cache_consumer_run_id": cur.cache_consumer_run_id or None,
            "_metric_map": dict(_FPGA_METRIC_MAP),
            "_min_allowed": ["fmax_mhz", "wns_ns", "whs_ns"],
        }
        if base:
            detail["baseline_metrics"] = _metrics_detail(base, baseline=True)
            detail[BASELINE_RECIPE_FINGERPRINT_DETAIL] = base.recipe_fingerprint or None
            detail[BASELINE_RECIPE_SNAPSHOT_DETAIL] = base.recipe_snapshot or None
            detail[BASELINE_RUN_EVIDENCE_DETAIL] = base.run_evidence or None
        if baseline_ref:
            detail[BASELINE_REF_DETAIL] = getattr(self, "_baseline_full_sha", None) or baseline_ref
        implementation = implementation or self._implementation_report(
            cfg, cur, base, baseline_ref
        )
        self.set_criterion(
            f"fpga_impl_ok_{cfg}",
            implementation.passed,
            detail=implementation.envelope(detail),
            source_target=cfg,
        )


if __name__ == "__main__":
    FpgaImplFlow().cli()
