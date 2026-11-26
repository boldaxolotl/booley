"""AsicSynthesizeFlow — run ASIC synthesis per config with optional baseline comparison.

Wraps the Yosys synthesis flow (sv2v + Yosys + ABC) with structured reporting,
critical-condition detection, and optional delta computation against a baseline
git ref. The baseline is synthesized in a throwaway ``git worktree`` (see
:mod:`booley.flows.baseline_worktree`) rather than by checking the ref out in place, so
``--baseline`` never touches the caller's working tree.

The configure half renders scripts and a Makefile into the per-target build dir
in-process (:mod:`booley.flows.synth.backends.pipeline`), execution runs ``make -C <rel>`` in
the Sandbox, and the interpret half reconstructs the report from files
the make run left in the build directory.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from booley.core.booley_dir_links import lexical_booley_path
from booley.core.booley_layout import BOOLEY_DIR_NAME
from booley.core.boundary import (
    BoundaryError,
    as_dict,
    as_float,
    as_int,
    as_str,
    as_str_list,
    require_bool,
    require_positive_int,
)
from booley.core.build_paths import work_root_for
from booley.evidence.timing import (
    ClockTiming,
    make_clock_timing,
    per_clock_from_json,
    per_clock_to_json,
    worst_clock,
)
from booley.flows.cli_help import CliHelp
from booley.flows.eda_failures import (
    EdaFailure,
    classify_eda_failure,
    first_tool_diagnostic,
    strip_failure_markers,
)
from booley.flows.flow_config import expected_count
from booley.flows.job_estimates import JobRequest
from booley.flows.job_resources import (
    SYNTH_PHASES,
    JudgedCommands,
    judged_commands,
    resolved_source_bytes,
    resource_phase,
)
from booley.flows.memory_admission import job_thread_count
from booley.flows.metric_declarations import (
    Better,
    CostTier,
    FlowMetricValues,
    MetricDeclaration,
    MetricKind,
    MetricsUnavailable,
    metric_values,
)
from booley.flows.plan import (
    CommandPlan,
    FlowPlan,
    WorkUnitPlan,
    WorkUnitRole,
    normalize_plan_argv,
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
from booley.flows.synth import openroad_reuse
from booley.flows.synth.backends.pipeline import (
    BoundaryOutcome,
    openroad_thread_assignment,
    tool_phases,
    tool_source_names,
    yosys_tmpdir,
    yosys_tmpdir_assignment,
)
from booley.flows.synth.backends.yosys.core import (
    DEFAULT_FRONTEND,
    NAND2_AREA_UM2,
    echoed_source_lines,
    load_timing_config,
    resolve_frontend,
)
from booley.flows.synth.backends.yosys.parsing import parse_stat
from booley.flows.synth.cli import SynthArguments
from booley.flows.synth.mode import SynthMode, runs_openroad
from booley.flows.synth.request import SynthRequest
from booley.flows.synth.timing import (
    UnconstrainedTiming,
    parse_perclock,
    parse_unconstrained_timing,
)
from booley.flows.synth.warnings import WarningSummary
from booley.flows.terminal_progress import ProgressText, observe_source_names
from booley.flows.tool_runs import setup_runner
from booley.fusesoc import fusesoc_registry
from booley.presentation.terminal_table import render_table
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
)
from booley.runtime.filesystem_utils import make_directories
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import (
    format_clock,
    format_duration,
    rfc3339_from_epoch,
    utc_now_rfc3339,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle
from booley.targets.flow_names import config_section

from .. import artifacts, edam
from ..base import SubprocessResult
from ..baseline_worktree import (
    BaselineWorktreeError,
    baseline_worktree,
    git_full_sha,
)
from ..implementation_comparison import (
    ImplementationComparisonError,
    TargetPairPlan,
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
from ..run_evidence import (
    build_flow_run_evidence,
)
from ..source_fingerprint import compute_source_fingerprint
from ..target_parameters import vlogdefine_args as _vlogdefine_args
from ..target_parameters import vlogparam_specs
from .implementation_report import (
    build_synth_implementation_report,
)
from .recipe import (
    resolve_synth_mode,
    synthesis_recipe_args,
    synthesis_recipe_snapshot,
    synthesis_recipe_snapshot_fingerprint,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PlannedSynthRecipe:
    """Resolved synthesis recipe retained for candidate execution."""

    command: tuple[str, ...]
    resolved: Any
    project_paths: ProjectSourcePaths


@dataclass(frozen=True)
class _SynthRun:
    """One boundary run plus what OpenROAD reuse needs after interpretation."""

    result: SubprocessResult
    #: Set on a hit: the run whose OpenROAD outputs were restored.
    producer: openroad_reuse.Producer | None = None
    #: Set on a fresh OpenROAD run with a provable pre-run key: store under it.
    key: str | None = None
    identity: tuple[tuple[str, str], ...] | None = None


def _merge_stage_results(yosys: SubprocessResult, openroad: SubprocessResult) -> SubprocessResult:
    """One result for the Yosys and OpenROAD stage commands of a physical run."""
    peaks = [rss for rss in (yosys.peak_rss_mb, openroad.peak_rss_mb) if rss is not None]
    return SubprocessResult(
        returncode=openroad.returncode,
        stdout=yosys.stdout + openroad.stdout,
        stderr=yosys.stderr + openroad.stderr,
        timed_out=openroad.timed_out,
        duration_s=yosys.duration_s + openroad.duration_s,
        # The Yosys stage's dispatch time keeps its artifacts fresh for the stale guard.
        dispatched_unix=yosys.dispatched_unix,
        peak_rss_mb=max(peaks) if peaks else None,
        oom_kill_delta=openroad.oom_kill_delta,
        oom_killed=openroad.oom_killed,
    )


def synth_target_report_slug(target: str) -> str:
    """Filesystem-safe, collision-resistant name for one Target selector."""
    return target_report_slug(target)


def _load_flow_config(work_dir: Path) -> dict[str, Any]:
    """Read the ``[flows.synth]`` runtime-policy section."""
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir) or {}
    except Exception:  # noqa: BLE001 — best-effort config read; any failure degrades to empty knobs
        cfg = {}
    flows = cfg.get("flows", {})
    return config_section(flows, "synth") if isinstance(flows, dict) else {}


def _resolve_synth_timeout_ms(
    work_dir: Path | None,
    requested: Any = None,
) -> int:
    """Resolve the per-target synthesis budget for MCP and Flow callers."""
    return resolve_timeout_ms("synth", work_dir, requested)


def _fail_on_timing_violation(work_dir: Path) -> bool:
    """``[flows.synth].fail_on_timing_violation`` — timing gates the exit code.

    Default **false**, preserving the historical policy: negative slack prints
    ``RESULT: WARN -- timing VIOLATED`` and exits 0, because synthesis (a
    structural pass over the RTL) succeeded and the constraints are frequently
    a placeholder the project has not tuned yet. But an rc-only consumer — a
    ticket gate, a CI step, a review agent reading exit codes — then reads a
    -2.633 ns design as success (ravenoc F-37). A project that has real
    constraints flips this to ``true`` and a violated path becomes exit 1.

    Raises ``BoundaryError`` on a non-bool value: silently ignoring a
    ``fail_on_timing_violation = "yes"`` would leave the gate the author asked
    for quietly disarmed.
    """
    return require_bool(
        _load_flow_config(work_dir),
        "fail_on_timing_violation",
        default=False,
        field="[flows.synth] 'fail_on_timing_violation'",
    )


def _timing_report_paths(work_dir: Path) -> int | None:
    """``[flows.synth].timing_report_paths`` — worst setup paths per timing report.

    ``None`` when unset, so the backend default applies and the configure argv
    stays unchanged. The count is a report-size policy, not a recipe input: it
    must not enter the recipe fingerprint, which is why it lives here and not in
    a Target's ``advanced_settings_openroad``.

    Raises ``BoundaryError`` on anything but an integer of at least 1.
    """
    raw = _load_flow_config(work_dir).get("timing_report_paths")
    if raw is None:
        return None
    return require_positive_int(raw, field="[flows.synth] 'timing_report_paths'")


# Standard-cell NAND2_X1 equivalent conversion for Nangate.
# 1 GE is one NAND2_X1 cell; 1 kGE is 1000 NAND2_X1 cells.
KGE_DIVISOR = NAND2_AREA_UM2 * 1000.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


# Metrics the synthesis Flow declares; names are the ones ``metrics.toml`` bounds use.
# area_um2/area_kge/cell_count are the mapped netlist in logical mode and the placed netlist
# after repair in OpenROAD modes.
_SYNTH_COST_TIER = CostTier.HIGH
SYNTH_METRIC_DECLARATIONS: tuple[MetricDeclaration, ...] = (
    MetricDeclaration(
        "area_um2", "μm²", Better.LOWER, _SYNTH_COST_TIER, MetricKind.PLAIN, minimum=0
    ),
    MetricDeclaration(
        "area_kge", "kGE", Better.LOWER, _SYNTH_COST_TIER, MetricKind.PLAIN, minimum=0
    ),
    MetricDeclaration(
        "cell_count", "cells", Better.LOWER, _SYNTH_COST_TIER, MetricKind.PLAIN, minimum=0
    ),
    MetricDeclaration(
        "fmax_mhz", "MHz", Better.HIGHER, _SYNTH_COST_TIER, MetricKind.PER_CLOCK, minimum=0
    ),
    MetricDeclaration(
        "critical_path_ps", "ps", Better.LOWER, _SYNTH_COST_TIER, MetricKind.PER_CLOCK, minimum=0
    ),
    MetricDeclaration("wns_ns", "ns", Better.HIGHER, _SYNTH_COST_TIER, MetricKind.PER_CLOCK),
    MetricDeclaration("whs_ns", "ns", Better.HIGHER, _SYNTH_COST_TIER, MetricKind.PER_CLOCK),
)


@dataclass
class SynthMetrics:
    """Parsed synthesis metrics for a single config run."""

    area_um2: float | None = None
    # Provenance for the canonical area selected by ``synth_mode``.
    area_source: str = ""
    area_kge: float | None = None
    cells: int | None = None
    # Provenance for ``cells``, like ``area_source``.
    cell_count_source: str = ""
    wire_count: int = 0
    process_count: int = 0
    # Worst setup slack in ns from OpenROAD's embedded STA (negative == timing VIOLATED). The
    # honest whole-design aggregate worst — kept as a scalar (unlike Fmax/critical
    # path, which are per-clock below) so the report can flag violations without
    # re-deriving them from the period. Named ``wns_ns`` to match fpga_impl's
    # aggregate-setup-slack field (the two Flows' output schemas are kept
    # consistent); the per-clock breakdown carries its own ``wns_ns`` too.
    wns_ns: float | None = None
    # Per-clock timing keyed by clock name. Fmax and critical-path delay are
    # inherently per-clock, so the old single ``critical_path_ps``/``fmax_mhz``
    # scalars were removed in favour of this map (one entry per ``create_clock``;
    # a single-clock design has exactly one). Empty when STA reported no clock.
    per_clock: dict[str, ClockTiming] = field(default_factory=dict)
    # Public synthesis intent. None only for synthetic/unit-test metrics that
    # did not pass through the configured seam.
    synth_mode: SynthMode | None = None
    # Logical-mode frequency estimate derived from ABC's final liberty-mapped
    # combinational delay. This is not STA: placement, wiring, clock-to-Q, and
    # setup time are absent, so it is deliberately separate from ``per_clock``
    # and must never participate in timing thresholds.
    estimated_fmax_mhz: float | None = None
    # Worst register-to-register (internal) setup slack + Fmax from STA. Reported
    # alongside the overall worst path because a non-zero set_input/output_delay
    # budget lets an I/O path dominate the overall worst path and hide the true
    # reg->reg critical path. None when STA emitted no reg->reg marker (e.g. a
    # purely combinational design).
    reg2reg_slack_ns: float | None = None
    reg2reg_fmax_mhz: float | None = None
    # What the authored SDC leaves untimed. None when no check ran (logical mode,
    # or OpenROAD did not reach it); zero counts mean it ran and found nothing.
    unconstrained: UnconstrainedTiming | None = None
    elapsed_s: float = 0.0
    latches: int = 0
    latch_types: dict[str, int] | None = None
    expected_latches: int = 0
    """Latches the design is declared to contain on purpose
    (``[flows.synth].expected_latches``). Not every latch is an
    accident: a standard-cell library's integrated clock gater is built from
    a deliberate ``always_latch``, and lowRISC's generic
    ``prim_clock_gating`` ships exactly one. Treating every latch as an
    unwaivable error made a correct design unsynthesizable (F-19). Latches
    are always reported; only the count above this is a critical condition."""
    comb_loops: int = 0
    expected_comb_loops: int = 0
    """Combinational loops the design is declared to contain on purpose
    (``[flows.synth].expected_comb_loops``). Loops are always reported; only
    the count above this is a critical condition."""
    multi_driven: int = 0
    warning_summary: WarningSummary = field(default_factory=WarningSummary)
    returncode: int = 0
    timed_out: bool = False
    # Terminal classification is deliberately separate from returncode: make
    # commonly collapses a child rc137 into rc2, and a timeout may leave valid
    # intermediate area/stat artifacts behind.
    termination: str = "completed"
    yosys_complete: bool = True
    timing_complete: bool = True
    structural_checks_complete: bool = True
    ppa_complete: bool = True
    peak_rss_mb: float | None = None
    infra_error: str = ""
    # Error tail of the reconstructed synth output when a builtin run fails
    # without metrics. Surfaced so the real diagnostic (missing EDA tool,
    # unresolved liberty, a yosys/sv2v error out of the stage logs, ...)
    # reaches the report instead of being swallowed behind a generic
    # "no metrics" line.
    failure_output: str = ""
    # Project-relative path of the persisted full synth output (run.log in the
    # per-target Edalize work dir), written on pass AND fail. Empty when the
    # run never produced output to persist (e.g. resolution failure).
    log_path: str = ""
    #: Work-dir-relative directories holding this run's artifacts (``build``,
    #: ``timing``). The report names directories, not a file inventory — see
    #: :mod:`booley.flows.artifacts`.
    dirs: dict[str, str] = field(default_factory=dict)
    #: Normalized Target recipe used for this exact run. Kept with the metrics
    #: so a baseline pass cannot be overwritten by the later current pass.
    recipe_snapshot: dict[str, Any] = field(default_factory=dict)
    recipe_fingerprint: str = ""
    run_evidence: dict[str, Any] = field(default_factory=dict)
    #: Set only when OpenROAD was reused: ``producer_run_id`` and
    #: ``producer_started_at`` (UTC RFC 3339) of the run that produced its outputs.
    openroad_reuse: dict[str, str] | None = None

    def __post_init__(self) -> None:
        """Normalize string-compatible construction at the metrics interface."""
        if isinstance(self.synth_mode, str):
            self.synth_mode = SynthMode(self.synth_mode)

    def flow_metrics(self) -> FlowMetricValues:
        """Return this run's typed values for :data:`SYNTH_METRIC_DECLARATIONS`."""
        return metric_values(
            SYNTH_METRIC_DECLARATIONS,
            plain={
                "area_um2": self.area_um2,
                "area_kge": self.area_kge,
                "cell_count": self.cells,
            },
            per_clock=self.per_clock,
        )

    def qor_detail(self) -> dict[str, Any]:
        """Return the canonical QoR fields shared by every report surface."""
        detail: dict[str, Any] = {
            "area_um2": self.area_um2,
            "area_source": self.area_source,
            "area_kge": self.area_kge,
            "cell_count": self.cells,
            "cell_count_source": self.cell_count_source,
            "wire_count": self.wire_count,
            "synth_mode": self.synth_mode.value if self.synth_mode else "",
            "per_clock": per_clock_to_json(self.per_clock),
            "wns_ns": self.wns_ns,
            "whs_ns": _worst_hold_slack_ns(self),
            "reg2reg_slack_ns": self.reg2reg_slack_ns,
            "reg2reg_fmax_mhz": self.reg2reg_fmax_mhz,
        }
        if self.estimated_fmax_mhz is not None:
            detail["estimated_fmax_mhz"] = self.estimated_fmax_mhz
        return detail

    def status_detail(self) -> dict[str, Any]:
        """Return completion and terminal-state fields shared by reports."""
        return {
            "passed": self.passed,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "termination": self.termination,
            "infra_error": self.infra_error,
            "has_metrics": self.has_metrics,
            "yosys_complete": self.yosys_complete,
            "timing_complete": self.timing_complete,
            "structural_checks_complete": self.structural_checks_complete,
            "ppa_complete": self.ppa_complete,
            "peak_rss_mb": self.peak_rss_mb,
        }

    def structural_detail(self) -> dict[str, Any]:
        """Return canonical structural-check counts and verdict."""
        return {
            "has_critical": self.has_critical,
            "latches": self.latches,
            "expected_latches": self.expected_latches,
            "unexpected_latches": self.unexpected_latches,
            "comb_loops": self.comb_loops,
            "expected_comb_loops": self.expected_comb_loops,
            "unexpected_comb_loops": self.unexpected_comb_loops,
            "multi_driven": self.multi_driven,
            "warning_summary": self.warning_summary.to_detail(),
        }

    def warning_detail(self) -> dict[str, Any]:
        """Return warning evidence shared by legacy report surfaces."""
        return {
            "total_warnings": self.warning_summary.total_warnings,
            "warning_summary": self.warning_summary.to_detail(),
        }

    @property
    def has_timing_evidence(self) -> bool:
        """Whether OpenROAD surfaced at least one numeric timing result."""
        scalar_evidence = (
            self.wns_ns,
            self.reg2reg_slack_ns,
            self.reg2reg_fmax_mhz,
        )
        if any(value is not None for value in scalar_evidence):
            return True
        return any(
            timing.wns_ns is not None or timing.whs_ns is not None
            for timing in self.per_clock.values()
        )

    @property
    def unexpected_latches(self) -> int:
        """Latches beyond the declared intentional count (never negative)."""
        return max(0, self.latches - self.expected_latches)

    @property
    def unexpected_comb_loops(self) -> int:
        """Combinational loops beyond the declared count (never negative)."""
        return max(0, self.comb_loops - self.expected_comb_loops)

    @property
    def has_critical(self) -> bool:
        return self.structural_checks_complete and (
            self.unexpected_latches > 0
            or self.unexpected_comb_loops > 0
            or self.multi_driven > 0
            or self.process_count > 0
        )

    @property
    def has_metrics(self) -> bool:
        """True when synthesis produced enough data to be actionable."""
        return self.cells is not None or self.area_um2 is not None

    @property
    def passed(self) -> bool:
        """True only when the EDA tool exited cleanly and produced usable metrics."""
        return (
            self.returncode == 0
            and not self.timed_out
            and self.ppa_complete
            and self.structural_checks_complete
            and not self.has_critical
        )

    def failure_reason(self) -> str:
        """One-line cause of a failed run, as the implementation report and RESULT line name it."""
        if self.timed_out:
            return "timeout"
        killed = {"oom": "OOM", "resource_killed": "resource-killed"}
        if self.termination in killed:
            return killed[self.termination]
        if self.termination == "design_failure":
            return _design_failure_reason(self)
        if self.returncode != 0:
            return f"rc={self.returncode}" + ("" if self.has_metrics else ", no metrics")
        if not self.has_metrics:
            return "no metrics"
        return "critical conditions" if self.has_critical else "failed"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_per_clock_sta(output: str) -> dict[str, ClockTiming]:
    """Build the per-clock timing map from ``STA_PERCLOCK`` markers.

    Each clock's ``critical_path_ps``/``fmax_mhz`` is derived from its period and
    worst setup slack by the shared :mod:`booley.evidence.timing` helper, so
    the STA and Vivado flows share one ns→ps→MHz derivation, but only when
    that worst path is launched by the same clock. A path launched by another
    clock is not timed against this clock's period, so the row keeps its
    slacks and has no critical path or Fmax. ``critical_path_ps``
    intentionally means STA timing — ABC ``delay =`` mapper estimates are never
    a source here.
    """
    per_clock: dict[str, ClockTiming] = {}
    for name, row in parse_perclock(output).items():
        if row.setup_same_clock:
            per_clock[name] = make_clock_timing(name, row.period_ns, row.wns_ns, row.whs_ns)
        else:
            per_clock[name] = ClockTiming(
                clock=name,
                period_ns=row.period_ns,
                wns_ns=row.wns_ns,
                whs_ns=row.whs_ns,
                critical_path_ps=None,
                fmax_mhz=None,
            )
    return per_clock


def _parse_worst_slack(output: str) -> float | None:
    """Extract physical STA worst setup slack in ns (negative == VIOLATED).

    The synth engines emit ``STA_WORST_SLACK_NS:`` once per STA run; the most
    pessimistic (minimum) value is the design's worst slack.
    """
    matches = re.findall(r"STA_WORST_SLACK_NS:\s*([-+]?\d+(?:\.\d+)?)", output)
    return min(float(s) for s in matches) if matches else None


def _parse_reg2reg_slack(output: str) -> float | None:
    """Extract the worst register-to-register setup slack in ns, or None.

    Both timing engines emit ``STA_REG2REG_SLACK_NS:`` once; the most
    pessimistic (minimum) value is the internal worst slack.
    """
    matches = re.findall(r"STA_REG2REG_SLACK_NS:\s*([-+]?\d+(?:\.\d+)?)", output)
    return min(float(s) for s in matches) if matches else None


def _parse_reg2reg_fmax(output: str) -> float | None:
    """Extract the reg->reg Fmax in MHz, or None when the marker is absent."""
    matches = re.findall(r"STA_REG2REG_FMAX_MHZ:\s*([-+]?\d+(?:\.\d+)?)", output)
    return min(float(f) for f in matches) if matches else None


# Tolerance (ns) for the overall-vs-reg2reg slack comparison below. The two
# numbers come from separate STA queries printed at 6-decimal precision; a gap
# smaller than this is noise, not a distinct I/O path.
_IO_BOUND_SLACK_EPS_NS = 1e-3


def _is_io_bound_critical(metrics: SynthMetrics) -> bool:
    """True when the design's binding (worst) timing path is I/O-bound.

    The *overall* worst path is the single most-negative-slack path in the
    design; the *reg2reg* group is the worst path constrained to register
    endpoints on both ends. When the overall worst slack is strictly worse than
    the reg2reg worst slack, the binding path is NOT a reg->reg path — it starts
    at an input port or ends at an output port (in->reg, reg->out, or a pure
    in->out feedthrough).

    That matters because an I/O path's slack depends on the external delays the
    Target's SDC declares for that port, so the fix is there: a realistic
    ``set_input_delay`` / ``set_output_delay``, or ``set_false_path`` for a port
    that should not be timed. (SETUP-28)
    """
    worst = metrics.wns_ns
    r2r = metrics.reg2reg_slack_ns
    if worst is None or r2r is None:
        return False
    return worst < r2r - _IO_BOUND_SLACK_EPS_NS


def _worst_critical_path_ps(metrics: SynthMetrics) -> float | None:
    """Timing-worst clock's critical path (ps) for one-number display/deltas.

    A representative scalar for the summary line and area/timing delta only —
    never a stored metric (Fmax/critical-path are per-clock; that is the whole
    point of :attr:`SynthMetrics.per_clock`). ``None`` when no clock has timing.
    """
    worst = worst_clock(metrics.per_clock)
    return worst.critical_path_ps if worst else None


def _worst_hold_slack_ns(metrics: SynthMetrics) -> float | None:
    """Most pessimistic per-clock hold slack, or ``None`` when unavailable."""
    slacks = [row.whs_ns for row in metrics.per_clock.values() if row.whs_ns is not None]
    return min(slacks) if slacks else None


def _parse_physical_area(output: str) -> float | None:
    """Extract OpenROAD's post-optimization placed design area in µm²."""
    matches = re.findall(r"OPENROAD_DESIGN_AREA_UM2:\s*([-+]?\d+(?:\.\d+)?)", output)
    return float(matches[-1]) if matches else None


def _parse_physical_cell_count(output: str) -> int | None:
    """Extract OpenROAD's instance count after repair and final placement."""
    matches = re.findall(r"OPENROAD_INSTANCE_COUNT:\s*(\d+)", output)
    return int(matches[-1]) if matches else None


def _cell_count(output: str, mode: SynthMode, yosys_cells: int | None) -> tuple[int | None, str]:
    """Return the canonical cell count and its provenance for ``mode``.

    OpenROAD modes use the placed netlist's instance count; a run that never
    reached the final report, and logical mode, use the mapped Yosys count.
    """
    if mode.runs_openroad:
        placed = _parse_physical_cell_count(output)
        if placed is not None:
            return placed, SynthMode.PHYSICAL.area_source
    if yosys_cells is None:
        return None, ""
    return yosys_cells, SynthMode.LOGICAL.area_source


def _parse_logical_estimated_fmax(output: str) -> float | None:
    """Convert the final mapped-ABC delay marker in ps to an Fmax estimate."""
    matches = re.findall(r"YOSYS_ABC_LOGIC_DELAY_PS:\s*([0-9]+(?:\.[0-9]+)?)", output)
    delays_ps = [float(value) for value in matches if float(value) > 0]
    if not delays_ps:
        return None
    return 1_000_000.0 / max(delays_ps)


def _parse_wire_count(output: str) -> int:
    """Extract wire count from the authoritative final stat."""
    return parse_stat(output).wires


def _parse_process_count(output: str) -> int:
    """Extract residual process count from the authoritative final stat."""
    return parse_stat(output).processes


def _count_latches(output: str) -> int:
    """Count final inferred cells; occurrence fallback only without usable stat."""
    stat = parse_stat(output)
    if stat.section is not None:
        return sum(stat.latch_types.values())
    return len(re.findall(r"\$dlatch", output))


def _latch_diagnostic(metrics: SynthMetrics) -> str:
    """Bounded structural evidence shared by console and diagnostic excerpts."""
    types = (
        ", ".join(f"{cell}={count}" for cell, count in sorted(metrics.latch_types.items()))
        if metrics.latch_types is not None
        else "unavailable"
    )
    return (
        f"{metrics.latches} latches (actual={metrics.latches}, "
        f"expected={metrics.expected_latches}, excess={metrics.unexpected_latches}; "
        f"types: {types or 'none'})"
    )


def _comb_loop_diagnostic(metrics: SynthMetrics) -> str:
    """Actual, expected, and excess combinational loops for console and excerpts."""
    return (
        f"{metrics.comb_loops} comb loops (actual={metrics.comb_loops}, "
        f"expected={metrics.expected_comb_loops}, excess={metrics.unexpected_comb_loops})"
    )


def _critical_diagnostic(metrics: SynthMetrics) -> str:
    """Name each critical structural count that exceeds its declared allowance."""
    parts = []
    if metrics.unexpected_latches:
        parts.append(_latch_diagnostic(metrics))
    if metrics.unexpected_comb_loops:
        parts.append(_comb_loop_diagnostic(metrics))
    return "; ".join(parts) or "critical structural conditions"


# Header prepended to a design diagnostic when the tool exited 0 but its log
# still reported an error; it describes the exit code, not the design problem.
_EXIT_ZERO_ERROR_HEADER = "ERROR: synthesis log reports an error despite exit 0:"


def _design_failure_reason(metrics: SynthMetrics) -> str:
    """Keep the rejected generic master distinct from allowed latch evidence."""
    if "unmapped generic cell:" in metrics.failure_output:
        reason = "unmapped generic cell"
        return f"{reason}; {_latch_diagnostic(metrics)}" if metrics.latches else reason
    if metrics.latches or metrics.unexpected_comb_loops:
        return "; ".join(
            part
            for part in (
                _latch_diagnostic(metrics) if metrics.latches else "",
                _comb_loop_diagnostic(metrics) if metrics.unexpected_comb_loops else "",
            )
            if part
        )
    if "unmapped latch:" in metrics.failure_output:
        return "unmapped latch"
    # The exit-0 header names the exit code, not the design problem: skip it.
    lines = [
        line.strip()
        for line in metrics.failure_output.splitlines()
        if line.strip() and line.strip() != _EXIT_ZERO_ERROR_HEADER
    ]
    # A short Yosys run's excerpt starts with its banner: name the tool's error line,
    # never an echoed source line (an RTL comment may read ``ERROR: ...``).
    echoed = echoed_source_lines(lines)
    candidates = "\n".join(line for index, line in enumerate(lines) if index not in echoed)
    return first_tool_diagnostic(candidates, default=lines[0] if lines else "design failure")


def _detect_critical_conditions(output: str) -> tuple[int, int, int]:
    """Count latches, combinational loops, and multi-driven nets in output.

    Returns (latches, comb_loops, multi_driven).
    """
    latches = _count_latches(output)
    comb_loops = len(re.findall(r"[Cc]ombinational loop", output))
    multi_driven = len(re.findall(r"[Mm]ulti-driven", output))
    return latches, comb_loops, multi_driven


def _parse_synth_output(
    output: str,
    elapsed_s: float,
    synth_mode: SynthMode | str = SynthMode.LOGICAL,
) -> SynthMetrics:
    """Parse all metrics from combined synthesis stdout/stderr."""
    mode = SynthMode(synth_mode)
    stat = parse_stat(output)
    mapped_area_um2 = stat.area_um2
    cells, cell_count_source = _cell_count(output, mode, stat.cells)
    if mode.runs_openroad:
        area_um2 = _parse_physical_area(output)
        estimated_fmax_mhz = None
    else:
        area_um2 = mapped_area_um2
        estimated_fmax_mhz = _parse_logical_estimated_fmax(output)
    area_source = mode.area_source if area_um2 is not None else ""
    area_kge = area_um2 / KGE_DIVISOR if area_um2 is not None else None
    wns_ns = _parse_worst_slack(output)
    reg2reg_slack_ns = _parse_reg2reg_slack(output)
    reg2reg_fmax_mhz = _parse_reg2reg_fmax(output)
    wire_count = _parse_wire_count(output)
    process_count = _parse_process_count(output)
    latches, comb_loops, multi_driven = _detect_critical_conditions(output)
    peak_matches = re.findall(r"Peak RSS:\s*([\d.]+)\s*MB", output, re.IGNORECASE)
    return SynthMetrics(
        area_um2=area_um2,
        area_source=area_source,
        area_kge=area_kge,
        cells=cells,
        cell_count_source=cell_count_source,
        wire_count=wire_count,
        process_count=process_count,
        wns_ns=wns_ns,
        per_clock=_parse_per_clock_sta(output),
        estimated_fmax_mhz=estimated_fmax_mhz,
        reg2reg_slack_ns=reg2reg_slack_ns,
        reg2reg_fmax_mhz=reg2reg_fmax_mhz,
        unconstrained=parse_unconstrained_timing(output),
        synth_mode=mode,
        elapsed_s=elapsed_s,
        latches=latches,
        latch_types=stat.latch_types if stat.section is not None else None,
        comb_loops=comb_loops,
        multi_driven=multi_driven,
        peak_rss_mb=float(peak_matches[-1]) if peak_matches else None,
    )


_RESOURCE_KILL_RE = re.compile(
    r"(?:\b(?:exit|code|error)\s*137\b|\breturncode\s*[=:]\s*137\b|^\s*(?:/bin/(?:ba)?sh:[^\n]*Killed(?:\s|$)|[0-9]+\s+Killed(?:\s|$)|Killed\s*$))",
    re.IGNORECASE | re.MULTILINE,
)
_INPUT_ERROR_RE = re.compile(r"BOOLEY_INPUT_ERROR:\s*([^\r\n]+)")


def _termination_reason(result: SubprocessResult, output: str) -> str:
    """Classify the boundary's terminal state without overclaiming OOM."""
    if result.timed_out:
        return "timeout"
    if result.returncode == 0:
        return "completed"
    if result.oom_blamed:
        return "oom"
    if result.returncode in {-9, 137} or _RESOURCE_KILL_RE.search(output):
        return "resource_killed"
    if result.returncode == -1:
        return "infrastructure_error"
    return "eda_tool_failure"


def _infra_metrics(message: str) -> SynthMetrics:
    """Build an explicitly incomplete result for pre-boundary failures."""
    return SynthMetrics(
        returncode=2,
        infra_error=message,
        termination="infrastructure_error",
        yosys_complete=False,
        timing_complete=False,
        structural_checks_complete=False,
        ppa_complete=False,
    )


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------


def _result_line(
    failed: list[str],
    selfcompare_msg: str | None,
    violated: list[str],
    eda_warnings: list[str] | None = None,
    exit_code: int | None = None,
) -> str:
    """The single ``RESULT:`` headline, in strict severity order.

    The canonical aggregate determines ERROR versus FAIL, which outrank WARN.
    Among WARNs a meaningless baseline delta outranks a timing violation.
    Warnings exit 0 unless timing violations are explicitly configured fatal.
    """
    if failed or exit_code in {EXIT_FAILURE, EXIT_ERROR}:
        reasons = failed or [
            "aggregate infrastructure error"
            if exit_code == EXIT_ERROR
            else "aggregate design failure"
        ]
        return (
            "RESULT: ERROR — " + "; ".join(reasons)
            if exit_code == EXIT_ERROR
            else f"RESULT: FAIL ({'; '.join(reasons)})"
        )
    if selfcompare_msg:
        # Structurally clean, but the baseline delta measured identical
        # sources -- the one thing --baseline exists to catch.
        return (
            "RESULT: WARN -- baseline delta not meaningful "
            "(baseline and current synthesized identical sources)"
        )
    if violated:
        # Structurally clean but timing is VIOLATED. Exit stays 0 by default
        # (timing does not gate synthesis), yet the user must clearly see it.
        timing = f"timing VIOLATED ({'; '.join(violated)})"
        if eda_warnings:
            return f"RESULT: WARN -- {timing}; {'; '.join(eda_warnings)}"
        return f"RESULT: WARN -- {timing}"
    if eda_warnings:
        return f"RESULT: WARN -- {'; '.join(eda_warnings)}"
    return "RESULT: PASS"


def _first_valid_display(
    targets: list[str],
    current_results: dict[str, SynthMetrics],
) -> list[str]:
    """Build concise display line from the first target with valid metrics."""
    for tgt in targets:
        cur = current_results[tgt]
        critical_path_ps = _worst_critical_path_ps(cur)
        if cur.cells is not None and cur.synth_mode is SynthMode.LOGICAL:
            return [f"{cur.cells:,} cells, logical area only"]
        if cur.cells is not None and critical_path_ps is not None:
            return [f"{cur.cells:,} cells, {critical_path_ps:.0f}ps"]
    return []


_BASELINE_DETAIL_FIELDS = (
    "area_um2",
    "area_source",
    "area_kge",
    "cell_count",
    "cell_count_source",
    "wire_count",
    "per_clock",
    "estimated_fmax_mhz",
)
_REPORT_QOR_FIELDS = (
    "area_kge",
    "area_um2",
    "area_source",
    "cell_count",
    "cell_count_source",
    "synth_mode",
    "per_clock",
    "estimated_fmax_mhz",
    "wns_ns",
    "whs_ns",
    "reg2reg_slack_ns",
    "reg2reg_fmax_mhz",
)
_REPORT_BASELINE_FIELDS = (
    "area_kge",
    "area_um2",
    "area_source",
    "synth_mode",
    "cell_count",
    "cell_count_source",
    "per_clock",
    "estimated_fmax_mhz",
)


def _select_present(detail: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Select named fields, retaining explicit None values when present."""
    return {name: detail[name] for name in fields if name in detail}


def _report_qor_detail(metrics: SynthMetrics) -> dict[str, Any]:
    """Return canonical QoR fields with absent optional timing values omitted."""
    detail = _select_present(metrics.qor_detail(), _REPORT_QOR_FIELDS)
    optional = {
        "estimated_fmax_mhz",
        "wns_ns",
        "whs_ns",
        "reg2reg_slack_ns",
        "reg2reg_fmax_mhz",
    }
    return {
        name: value for name, value in detail.items() if name not in optional or value is not None
    }


def _baseline_report_detail(metrics: SynthMetrics, baseline_ref: str) -> dict[str, Any]:
    """Return the baseline subset used by per-target JSON reports."""
    return {
        "ref": baseline_ref,
        **_select_present(metrics.qor_detail(), _REPORT_BASELINE_FIELDS),
        **metrics.warning_detail(),
    }


def _add_report_deltas(
    report: dict[str, Any],
    current: SynthMetrics,
    baseline: SynthMetrics,
    compute_delta: Any,
) -> None:
    """Add meaningful area and timing deltas to a per-target report."""
    area_delta = compute_delta(current.area_kge, baseline.area_kge)
    timing_delta = compute_delta(
        _worst_critical_path_ps(current),
        _worst_critical_path_ps(baseline),
    )
    if area_delta is not None:
        report["delta_pct"] = round(area_delta, 1)
    if timing_delta is not None:
        report["timing_delta_pct"] = round(timing_delta, 1)


def _target_summary(
    metrics: SynthMetrics,
    baseline: SynthMetrics | None = None,
) -> dict[str, Any]:
    """QoR + verdict for one target, as carried in the aggregate detail."""
    summary: dict[str, Any] = {
        **metrics.status_detail(),
        **metrics.qor_detail(),
        "has_critical": metrics.has_critical,
        "unexpected_latches": metrics.unexpected_latches,
        "comb_loops": metrics.comb_loops,
        "unexpected_comb_loops": metrics.unexpected_comb_loops,
        "multi_driven": metrics.multi_driven,
        **metrics.warning_detail(),
        # The aggregate detail is what reaches the agent as MCP
        # structuredContent, so the pointers ride along with the numbers.
        "artifacts": {
            **({"log": metrics.log_path} if metrics.log_path else {}),
            **({"live_dirs": dict(metrics.dirs)} if metrics.dirs else {}),
        },
    }
    if metrics.unconstrained is not None:
        summary["unconstrained"] = metrics.unconstrained.to_detail()
    if metrics.openroad_reuse:
        summary["openroad_reuse"] = dict(metrics.openroad_reuse)
    if baseline is not None:
        summary["baseline_metrics"] = {
            "passed": baseline.passed,
            "returncode": baseline.returncode,
            "infra_error": baseline.infra_error,
            "ppa_complete": baseline.ppa_complete,
            **_select_present(baseline.qor_detail(), _BASELINE_DETAIL_FIELDS),
            **baseline.warning_detail(),
        }
    return summary


def _aggregate_detail(
    targets: list[str],
    current_results: dict[str, SynthMetrics],
    baseline_results: dict[str, SynthMetrics] | None = None,
    baseline_sha: str | None = None,
) -> dict[str, Any]:
    """Detail for the run-level ``asic_synthesize.json`` (SETUP-F-29b).

    A consumer that reads the Flow's own report file (the MCP poll path, triage)
    needs the QoR behind a verdict, not only the verdict. Mirror the per-target
    summaries here, keyed by target,
    with an explicit ``targets`` list so the run's scope is readable even when
    a target produced no metrics at all.
    """
    baseline_results = baseline_results or {}
    current_passed = all(current_results[t].passed for t in targets) if targets else False
    baseline_infra = any(metrics.infra_error for metrics in baseline_results.values())
    detail: dict[str, Any] = {
        "targets": list(targets),
        "passed": current_passed and not baseline_infra,
    }
    for tgt in targets:
        detail[tgt] = _target_summary(current_results[tgt], baseline_results.get(tgt))
    if baseline_sha:
        detail["baseline_ref"] = baseline_sha
    return detail


def _incomplete_reason(summary: dict[str, Any]) -> str | None:
    """Why a Target summary's run did not complete, or ``None`` when it did.

    A summary without ``ppa_complete`` is not a completed run: the reader fails closed.
    """
    if infra_error := as_str(summary.get("infra_error")):
        return infra_error
    if summary.get("ppa_complete") is not True:
        termination = as_str(summary.get("termination"))
        return (
            f"the synthesis run did not complete{f' ({termination})' if termination else ''}; "
            "its partial numbers are not final QoR"
        )
    return None


def report_metric_values(
    detail: dict[str, Any],
) -> dict[str, FlowMetricValues | MetricsUnavailable]:
    """Read each Target's :data:`SYNTH_METRIC_DECLARATIONS` values from an aggregate *detail*.

    The inverse of :func:`_aggregate_detail`. A Target whose summary is missing or
    malformed is skipped: judging an absent measurement belongs to the caller. A Target
    whose run did not complete (timeout, out of memory, infrastructure error, partial
    PPA) is :class:`MetricsUnavailable` with the reason, never its partial numbers.
    """
    values: dict[str, FlowMetricValues | MetricsUnavailable] = {}
    for target in as_str_list(detail.get("targets")):
        summary = as_dict(detail.get(target))
        if summary is None:
            continue
        reason = _incomplete_reason(summary)
        if reason is not None:
            values[target] = MetricsUnavailable(reason)
            continue
        values[target] = metric_values(
            SYNTH_METRIC_DECLARATIONS,
            plain={
                "area_um2": as_float(summary.get("area_um2")),
                "area_kge": as_float(summary.get("area_kge")),
                "cell_count": as_int(summary.get("cell_count")),
            },
            per_clock=per_clock_from_json(summary.get("per_clock")),
        )
    return values


def _publish_timing_dir(
    invocation_dir: Path,
    source_dir: Path,
    components: tuple[str, ...],
    root: Path,
    label: str,
) -> str | None:
    """Copy every file under *source_dir* to ``invocation_dir/<components>``.

    Returns the project-relative destination directory, ``""`` when there was
    nothing to copy (no directory, or an empty one), and ``None`` when the copy
    failed: the partial copy is removed and a warning naming *label* is logged.
    Publication never changes a run's verdict, so failures are not raised.
    """
    if not source_dir.is_dir():
        return ""
    destination = invocation_dir.joinpath(*components)
    try:
        copied_any = False
        for source in sorted(path for path in source_dir.rglob("*") if path.is_file()):
            artifacts.publish_file(
                invocation_dir,
                (*components, *source.relative_to(source_dir).parts),
                source,
                work_dir=root,
            )
            copied_any = True
    except (OSError, ValueError):
        shutil.rmtree(destination, ignore_errors=True)
        logger.warning("could not publish %s", label, exc_info=True)
        return None
    return posix_relpath(destination, root) if copied_any else ""


def _build_report_dict(
    flow_name: str,
    target: str,
    metrics: SynthMetrics,
    baseline_metrics: SynthMetrics | None,
    baseline_ref: str | None,
    compute_delta: Any,
    eda_tool: str | None = None,
) -> dict[str, Any]:
    """Build the per-target report dict for JSON serialization.

    *eda_tool* is the underlying EDA binary ("yosys" on the builtin flow).
    """
    report: dict[str, Any] = {
        "flow": flow_name,
        "target": target,
        "eda_tool": eda_tool,
        "timestamp": utc_now_rfc3339(),
        "elapsed_s": round(metrics.elapsed_s, 1),
        **metrics.status_detail(),
        **_report_qor_detail(metrics),
    }
    if _is_io_bound_critical(metrics):
        report["io_bound_critical"] = True
    if metrics.unconstrained is not None:
        report["unconstrained"] = metrics.unconstrained.to_detail()
    if metrics.openroad_reuse:
        report["openroad_reuse"] = dict(metrics.openroad_reuse)
    if baseline_metrics and baseline_ref:
        report["baseline"] = _baseline_report_detail(baseline_metrics, baseline_ref)
        _add_report_deltas(report, metrics, baseline_metrics, compute_delta)
    if metrics.failure_output:
        report["failure_output"] = metrics.failure_output
    report["conditions"] = metrics.structural_detail()
    report.update(metrics.warning_detail())
    return report


def _baseline_self_compare_warning(project_root: Path, wt: Path) -> str | None:
    """Detect a no-op ``--baseline`` self-comparison and return an actionable warning.

    A Project without a Paired Checkout may mirror live ``.booley/cores/`` (ADR 0036)
    into the baseline worktree because the outer Git ref does not contain those
    files. A project whose RTL lives as real files below those cores can therefore
    synthesize byte-identical sources on both sides. Paired Checkouts
    instead materialize their pinned or fork-point revision, but can still compare identical RTL.

    Compare the canonical source fingerprint (:func:`compute_source_fingerprint`) of the
    two trees. Only flag when the project HAS stealth cores *and* both sides hash
    identical, so a plain project whose git-tracked RTL genuinely didn't change
    between the ref and HEAD isn't nagged about a legitimately-zero delta.
    Returns ``None`` when there is nothing to warn about (or the fingerprint
    can't be computed — never let the guard itself break a run).

    The caller later suppresses this warning when the two normalized Target
    recipes differ, because that is a meaningful comparison even with identical
    RTL sources.
    """
    from booley.fusesoc.core_projection import stealth_core_root

    if not stealth_core_root(project_root).is_dir():
        return None
    try:
        cur = compute_source_fingerprint(project_root)["rtl"]["digest"]
        base = compute_source_fingerprint(wt)["rtl"]["digest"]
    except Exception:  # a guard must never fail the synthesis run
        logger.debug("baseline self-compare fingerprint check failed", exc_info=True)
        return None
    if cur != base:
        return None
    return (
        "baseline and current synthesized byte-identical RTL with an unchanged "
        "Target recipe -- the reported delta is a no-op self-comparison, not a "
        "real regression check. Compare against a ref whose RTL or Target recipe "
        "differs before trusting a +0.0% delta."
    )


# ---------------------------------------------------------------------------
# Flow implementation
# ---------------------------------------------------------------------------


def _work_path(path: Path, work_dir: Path) -> Path:
    """Spell a resolved Target file the way the tools see it from *work_dir*.

    Target files come back resolved (physical), and the tools run in the
    physical *work_dir*. Both sides must match, or a checkout reached through
    a link (a Codespace worktree under the symlinked ``.booley`` entry)
    yields a relative path that climbs out of the checkout. A file behind the
    checkout's own linked ``.booley`` entry stays named through the entry,
    which is what ``/work`` shows in the Sandbox.
    """
    return lexical_booley_path(work_dir.resolve() / BOOLEY_DIR_NAME, path.resolve())


def _work_relpath(path: Path, work_dir: Path) -> str:
    """The POSIX path of *path* relative to the physical *work_dir* (see :func:`_work_path`)."""
    return posix_relpath(_work_path(path, work_dir), work_dir.resolve())


def _plan_source_name(project_root: Path, item: fusesoc_registry.ResolvedFile) -> str:
    """The POSIX name a plan records for a resolved source file.

    Isolated Projects resolve through a private source mirror that exports
    ``src/<core>/<Project-relative path>``; like simulation reports, the plan
    hides the mirror and names the file by its Project-relative path, so the
    identity is the same in every checkout. Other Projects keep FuseSoC's name.
    """
    return fusesoc_registry.mirrored_source_path(project_root, item) or Path(item.name).as_posix()


def _tool_source_paths(planned: _PlannedSynthRecipe, build_dir: Path) -> ProjectSourcePaths:
    """The Project path of each Target source, by every name synthesis output gives it."""
    export_root = Path(planned.resolved.build_root)

    def spellings(name: str) -> tuple[str, str]:
        return tool_source_names((export_root / name).resolve(), build_dir)

    return planned.project_paths.with_spellings(spellings)


def _boundary_failure(
    termination: str, outcome: Any, result: SubprocessResult, output: str
) -> EdaFailure | None:
    """The failure a stage's wrapper marker reports, unless the run was stopped."""
    if termination in {"timeout", "oom", "resource_killed"}:
        return None
    boundary_result = replace(result, stdout=output, stderr="")
    token = getattr(outcome, "attempt_token", None)
    failure = classify_eda_failure(boundary_result, expected_token=token)
    stage = getattr(outcome, "stage", None)
    if (
        failure is None
        and re.fullmatch(r"[0-9a-f]{32}", token or "")
        and stage in {"sv2v", "yosys", "openroad"}
    ):
        failure = classify_eda_failure(
            boundary_result, expected_stage=stage, expected_executable=stage
        )
    return failure


# Positions in the records a physical run holds: Yosys is recorded before OpenROAD.
_YOSYS_COMMAND = 0
_OPENROAD_COMMAND = 1


def _judge_stage_commands(
    commands: JudgedCommands,
    plan: Any,
    metrics: SynthMetrics,
    outcome: BoundaryOutcome,
    failure: EdaFailure | None,
) -> None:
    """Judge the run's commands: only the one whose own output failed the run is failed."""
    if metrics.returncode == 0:
        commands.judge(True)
    elif not runs_openroad(plan.spec.timing.mode):
        commands.judge(False)
    else:
        yosys_owned = (
            outcome.forced_failure is not None
            or not outcome.yosys_complete
            or (failure is not None and failure.stage in {"sv2v", "yosys"})
            or (metrics.termination == "design_failure" and outcome.openroad_diagnostic is None)
        )
        commands.judge(False, _YOSYS_COMMAND if yosys_owned else _OPENROAD_COMMAND)


class AsicSynthesizeFlow(ImplementationFlow[SynthRequest, SynthMetrics]):
    """Run ASIC synthesis for one or more Targets with optional baseline comparison."""

    request_type = SynthRequest
    argument_adapter = SynthArguments

    name: str = "synth"
    description: str = (
        "Run ASIC synthesis for one or more Targets with optional baseline comparison. "
        "This is a QUICK PPA (power/performance/area) ESTIMATE to optimize RTL "
        "against — not tape-out sign-off (which is out of scope for Booley), "
        "whatever engine backs it. "
        "Design timing constraints (clock period, I/O delays, false/multicycle "
        "paths) come from the Target's `file_type: SDC` fileset in the .core, "
        "NOT booley.toml: add an SDC file with your create_clock / "
        "set_input_delay / set_output_delay / set_false_path to the Target. A "
        "physical Target with NO SDC is a hard configuration error; logical "
        "synthesis remains valid without timing constraints. "
        "Persistent ppa_profile (compact|balanced|max_frequency), flatten, "
        "frontend, synth_mode, and advanced_settings_yosys/advanced_settings_openroad "
        "recipe knobs belong in the selected .core Target's flow_options. "
        "Per-call flags can override ppa_profile, flatten, frontend, and expert "
        "backend settings; synth_mode remains Target-owned. [flows.synth] in "
        "booley.toml is only for enablement, verdict, and report "
        "policy such as enabled, timeout_ms, expected_latches, "
        "expected_comb_loops, fail_on_timing_violation, and timing_report_paths."
    )
    cli_help: ClassVar[CliHelp | None] = CliHelp(
        summary=(
            "Run ASIC synthesis for one or more Targets. Use it for a quick PPA (power, "
            "performance, area) estimate while optimizing RTL; it is not tape-out "
            "sign-off. Timing constraints come from the SDC file in the Target's .core "
            "fileset; a physical Target needs one. The flags below override the "
            "Target's flow_options for one run."
        ),
        example="booley flow synth --target synth_soc --baseline main",
    )
    code_modifying: bool = False
    # ``[flows.synth].timing_report_paths``, latched by ``_run_policy_error``.
    _report_path_count: int | None = None
    # Set when a stealth-cores self-compare is detected in the baseline checkout;
    # reset per run by ``_begin_run``.
    _baseline_selfcompare_msg: str | None = None
    # Candidate selector -> published baseline timing dir (None: publishing failed).
    _baseline_timing: dict[str, str | None]
    # Minimum outer MCP kill budget (seconds). mcp_server scales this floor by
    # per-target timeout, matrix width, and baseline/current pass count.
    default_timeout: ClassVar[int] = 7200

    # The built-in flow is make-driven: run_yosys_syn renders the build tree,
    # then results are interpreted from files under that same directory.

    # -- Command builder for a single config ----------------------------------

    def _append_rtl_source_args(
        self,
        cmd: list[str],
        resolved: fusesoc_registry.ResolvedTarget,
        work_dir: Path,
    ) -> str:
        """Append ``-t``/``--extra-rtl``/``--inc-dir`` flags; return the top module."""
        # Top comes from the resolved Target (decision 12).
        top = resolved.toplevel
        if top:
            cmd.extend(["-t", top])
        for f in resolved.rtl_hdl_source_files:
            cmd.extend(["--extra-rtl", _work_relpath(f.absolute(resolved.build_root), work_dir)])
        for inc in resolved.rtl_include_dirs:
            cmd.extend(["--inc-dir", _work_relpath(inc, work_dir)])
        return top

    def _append_sta_constraint_args(
        self,
        cmd: list[str],
        resolved: fusesoc_registry.ResolvedTarget,
        target: str,
        work_dir: Path,
        synth_mode: SynthMode,
    ) -> None:
        """Append Target-owned ``--sta-sdc`` flags for physical synthesis."""
        if not synth_mode.runs_openroad:
            return
        # STA constraints from the Target's file_type:SDC fileset (ADR 0029):
        # forward one --sta-sdc per file, in EDAM fileset order. Same boundary
        # treatment as --extra-rtl — each is a path relative to the worktree,
        # resolved against /work inside the sandbox.
        for sdc_file in resolved.sdc_files:
            path = _work_path(sdc_file.absolute(resolved.build_root), work_dir)
            try:
                path.relative_to(work_dir.resolve())
            except ValueError as exc:
                raise BoundaryError(
                    f"synth: Target {target!r} SDC escapes the selected checkout "
                    f"{work_dir.resolve()}: {path}"
                ) from exc
            if not path.is_file():
                raise BoundaryError(f"synth: Target {target!r} SDC file not found: {path}")
            try:
                path.read_bytes()
            except OSError as exc:
                raise BoundaryError(
                    f"synth: Target {target!r} SDC file is not readable: {path}: {exc}"
                ) from exc
            cmd.extend(["--sta-sdc", _work_relpath(path, work_dir)])
        if not resolved.sdc_files:
            raise BoundaryError(
                f"synth: physical Target {target!r} has no timing constraints. "
                "Add a `file_type: SDC` fileset containing a clock to the Target. "
                "Logical synthesis may run without SDC."
            )

    def _append_typed_param_args(
        self,
        cmd: list[str],
        resolved: fusesoc_registry.ResolvedTarget,
        target: str,
        top: str,
    ) -> None:
        """Append ``-d``/``-p`` typed-param flags and warn on likely sim Targets."""
        # FuseSoC owns build-time defines. ``vlogdefine`` parameters on this
        # Target are the sole source, shared with FPGA.
        defines = _vlogdefine_args(resolved.parameters)
        for define in defines:
            cmd.extend(["-d", define])
        for name, parameter in vlogparam_specs(resolved.parameters).items():
            cmd.extend(["-p", f"{name}={parameter.value}"])
            if parameter.is_string:
                cmd.extend(["--param-datatype", f"{name}={parameter.datatype}"])
        # Warn early when the target looks aimed at simulation (testbench top or
        # SIMULATION define) — otherwise it fails deep inside Yosys.
        for warning in _synth_target_warnings(top, defines):
            logger.warning("Synth %s: %s", target, warning)

    def _append_synth_recipe_args(
        self, cmd: list[str], resolved: fusesoc_registry.ResolvedTarget, target: str
    ) -> None:
        """Append the shared normalized synthesis recipe for this invocation."""
        cmd.extend(synthesis_recipe_args(resolved.flow_options, self.args, target=target))

    def _resolve_synth_target(
        self,
        target: str,
        *,
        build_root: Path | None = None,
    ) -> fusesoc_registry.ResolvedTarget:
        """Clean and resolve one FuseSoC Target into its isolated build root."""
        selected_root = build_root or self._synth_work_root(target)
        shutil.rmtree(selected_root, ignore_errors=True)
        handle = self._target_handle(target)
        runner = setup_runner(self.name, target, "synth_prepare")
        ref = getattr(self, "_target_execution_refs", {}).get(target)
        if ref is not None:
            return resolve_target_execution_ref(
                handle, ref, build_root=selected_root, runner=runner
            )
        return fusesoc_registry.resolve_target_handle(
            handle,
            build_root=selected_root,
            library_plan=fusesoc_registry.build_library_plan(handle.project_root, selected_root),
            runner=runner,
        )

    def _record_recipe_evidence(self, target: str, resolved: Any) -> None:
        """Freeze the normalized recipe and source evidence for one Target."""
        snapshot = synthesis_recipe_snapshot(resolved, self.args, target=target)
        recipe_fingerprint = synthesis_recipe_snapshot_fingerprint(snapshot)
        run_evidence = build_flow_run_evidence(
            flow=self.name,
            target=target,
            recipe_sha256=recipe_fingerprint,
            work_dir=Path(self.args.work_dir),
        )
        evidence = getattr(self, "_recipe_evidence", None)
        if evidence is None:
            evidence = self._recipe_evidence = {}
        evidence[target] = (snapshot, recipe_fingerprint, run_evidence.as_dict())

    def _resolve_synth_recipe(
        self,
        target: str,
        *,
        build_root: Path | None = None,
    ) -> _PlannedSynthRecipe:
        """Resolve *target* and build its validated run_yosys_syn spec argv.

        FuseSoC owns design description; this command-gen exception forwards
        its staged sources into Booley's make-driven Yosys/OpenROAD module.
        Baselines point ``work_dir`` at a separate worktree, so resolution and
        artifacts remain isolated. The argv also remains runnable for dry-run.
        """
        resolved = self._resolve_synth_target(target, build_root=build_root)
        self._record_recipe_evidence(target, resolved)
        work_dir = Path(self.args.work_dir)
        cmd = ["python3", "-m", "booley.flows.synth.backends.configure", "configure"]
        top = self._append_rtl_source_args(cmd, resolved, work_dir)
        synth_mode = resolve_synth_mode(resolved.flow_options, target=target)
        self._append_sta_constraint_args(cmd, resolved, target, work_dir, synth_mode)
        self._append_typed_param_args(cmd, resolved, target, top)
        self._append_synth_recipe_args(cmd, resolved, target)
        if self._report_path_count is not None:
            cmd.extend(["--timing-report-paths", str(self._report_path_count)])
        cmd.extend(["-w", target])
        try:
            load_timing_config(work_dir)
        except SystemExit as exc:
            raise BoundaryError(str(exc.code)) from None
        paths = ProjectSourcePaths.for_target(self._target_handle(target), resolved)
        return _PlannedSynthRecipe(tuple(cmd), resolved, paths)

    def _build_synth_cmd(self, target: str) -> list[str]:
        """Return the mutable compatibility argv for one resolved recipe."""
        return list(self._resolve_synth_recipe(target).command)

    def _synth_work_root(self, target: str) -> Path:
        """Return the shared mutable work root for one synthesis Target."""
        return work_root_for(self.args.work_dir, self.name, target)

    def _synth_build_dir(self, target: str) -> Path:
        """The make-driven synth build dir for *target* (under its work root).

        A ``synth/`` leaf inside the per-target Edalize work root keeps the
        rendered scripts/Makefile/logs clear of the FuseSoC-staged tree that
        shares the root. Wiped together with the root by ``_build_synth_cmd``'s
        pre-resolution cleanup.
        """
        return self._synth_work_root(target) / "synth"

    def _report_workspace_wait(self, target: str) -> None:
        """Surface same-Target serialization in logs, stderr, and live progress."""
        line = f"waiting for synth target workspace [{target}]"
        logger.info("Synth %s: waiting for target workspace", target)
        print(f"[synth] {line}", file=sys.stderr, flush=True)
        self.emit_progress(line)

    def _configure_synth(self, target: str, cmd: list[str]) -> Any:
        """Render *target*'s make-driven build dir from the spec argv (ADR 0037 §8).

        The configure half: *cmd* (the run_yosys_syn spec argv built by
        ``_build_synth_cmd``) is parsed back by run_yosys_syn's own parser —
        guaranteeing the two stay shape-compatible — resolved against this
        run's work_dir, and rendered into scripts + a Makefile. Returns the
        :class:`booley.flows.synth.backends.pipeline.SynthPlan` the interpret half consumes.

        The liberty existence check is hard because configuration and execution
        share the Sandbox filesystem.

        Raises ``SystemExit`` (run_yosys_syn's validation guards) or ``OSError``
        (render failure); ``_run_single_config`` maps both to infra errors.
        """
        from booley.flows.synth.backends import configure as run_yosys_syn
        from booley.flows.synth.backends import pipeline as syn_make

        args = run_yosys_syn.parse_configure_argv(cmd)
        spec = run_yosys_syn.resolve_spec(
            args,
            project_root=Path(self.args.work_dir),
            require_liberty=True,
        )
        return syn_make.configure_synthesis(spec, self._synth_build_dir(target))

    def _synth_boundary_cmd(self, plan: Any, stage: str | None = None) -> list[str]:
        """Return the Sandbox command for a configured plan, or for one Makefile *stage*.

        The ``openroad`` stage also carries the Job's thread share, when it has one.
        """
        cmd = ["make", "-C", edam.relpath_for_make(plan.build_dir, self.args.work_dir)]
        if not stage:
            return cmd
        cmd.append(stage)
        threads = job_thread_count()
        if stage == "openroad" and threads is not None:
            cmd.append(openroad_thread_assignment(threads))
        return cmd

    # -- Single-target run ----------------------------------------------------

    def _run_single_config(
        self,
        target: str,
    ) -> tuple[SynthMetrics, str]:
        """Run synthesis for one target."""
        return self._synthesize_target(target)

    def _end_phases(self, target: str, *phases: str) -> None:
        """Report *phases* of *target* ended, unless this is a baseline run.

        A baseline's Target may be the candidate's, whose phases it must not
        count out of the Job's Memory Reservation. A phase the Job did not plan
        or already ended changes nothing.
        """
        if getattr(self, "_execution_role", "candidate") != "baseline":
            for phase in phases:
                report_phase_end(target, phase)

    def memory_job_requests(
        self, handles: tuple[TargetHandle, ...]
    ) -> tuple[JobRequest, ...] | None:
        """One request per Target, planning the tools its mode and frontend run.

        With ``--baseline`` every Target plans all three tools: the baseline
        revision's mode and frontend are known only once its worktree exists,
        and the baselines run first, under the largest tool estimate.
        """
        catalog = TargetCatalog.build(self.args.work_dir)
        requests: list[JobRequest] = []
        for handle in dict.fromkeys(handles):
            phases = self._planned_phases(catalog, handle)
            self._planned_tool_phases[handle.selector] = phases
            requests.append(JobRequest(self.name, handle.selector, split_phases=phases))
        return tuple(requests)

    def _planned_phases(self, catalog: TargetCatalog, handle: TargetHandle) -> tuple[str, ...]:
        """The tool phases to plan for *handle*; ``()`` plans the whole command."""
        if self.args.baseline:
            return tool_phases(SynthMode.PHYSICAL, "sv2v")
        try:
            options = catalog.inspect(handle).flow_options
            mode = resolve_synth_mode(options, target=handle.selector)
            frontend = (
                resolve_frontend(dict(options), override=self.args.frontend) or DEFAULT_FRONTEND
            )
        except (BoundaryError, fusesoc_registry.FuseSocError, OSError, ValueError):
            return ()  # the run reports the same error
        return tool_phases(mode, frontend)

    def begin_invocation(self) -> None:
        """Forget the tool phases an earlier invocation planned."""
        self._planned_tool_phases: dict[str, tuple[str, ...]] = {}

    def _synthesize_target(
        self,
        target: str,
    ) -> tuple[SynthMetrics, str]:
        """Run synthesis for one target. Returns (metrics, raw_output).

        Baseline runs are isolated by their separate worktree.
        """
        # Resolved once per run in _run (defaults to builtin/sandbox when a
        # test drives this method directly without going through _run).
        # The builtin flow is fixed: Yosys drives synthesis (plus the configured
        # STA timing engine). Recorded for run/report observability.
        if getattr(self, "_execution_role", "candidate") == "candidate":
            self.record_eda_tool(target, "yosys")

        # FuseSoC resolution happens in-process now (decision 4); a setup failure
        # becomes an infra error for this target, not an unhandled crash of the
        # whole _run.
        try:
            planned = None
            if getattr(self, "_execution_role", "candidate") == "candidate":
                planned = getattr(self, "_planned_candidate_synth_recipes", {}).get(target)
            if planned is None:
                planned = self._resolve_synth_recipe(target)
            else:
                # The recipe was resolved before baseline execution. Refresh
                # only invocation-local evidence, retaining those planned inputs.
                self._record_recipe_evidence(target, planned.resolved)
            cmd = list(planned.command)
        except fusesoc_registry.TargetResolutionError as exc:
            logger.warning("Synth %s: FuseSoC resolution failed: %s", target, exc)
            return _infra_metrics(str(exc)), str(exc)
        except BoundaryError as exc:
            # Wrong-typed [flows.synth] knob in booley.toml — a config
            # error, not a synthesis failure. Surfaced like a resolution failure
            # (returncode-2 infra error) so the fix-hint reaches the report
            # instead of crashing the whole Flow run.
            logger.warning("Synth %s: invalid Flow config: %s", target, exc)
            return _infra_metrics(str(exc)), str(exc)

        from booley.flows.terminal_progress import announce_unit, observe_logs

        role = getattr(self, "_execution_role", "candidate")
        announce_unit(f"{role}: configure", target=target)
        timeout_s = self._timeout_ms() / 1000.0
        # DEBUG, not INFO: the joined argv sprays one ``--extra-rtl <file>``
        # pair per source file into captured stderr, eating the MCP layer's
        # 12KB stdout/stderr budget on every run. The full spec remains
        # reachable via --log-level debug (and --dry-run prints it).
        logger.debug("Synth %s: %s (timeout=%.0fs)", target, " ".join(cmd), timeout_s)

        # ADR 0037 §8 configure half: parse the spec argv back in-process and
        # render the sv2v/yosys/STA scripts + Makefile into the build dir. The
        # run_yosys_syn resolution guards report via sys.exit("ERROR: ...");
        # in-process that is a SystemExit to map onto the infra-error path.
        try:
            plan = self._configure_synth(target, cmd)
        except SystemExit as exc:
            msg = str(exc.code) if exc.code is not None else "synthesis configure failed"
            logger.warning("Synth %s: configure failed: %s", target, msg)
            return self._attach_recipe_evidence(target, _infra_metrics(msg)), msg
        except (BoundaryError, OSError) as exc:
            msg = f"failed to render synthesis build dir: {exc}"
            logger.warning("Synth %s: %s", target, msg)
            return self._attach_recipe_evidence(target, _infra_metrics(msg)), msg

        # A bare `make -C <rel>` runs the generated plan with EDA binaries from
        # the Sandbox PATH.
        make_cmd = self._synth_boundary_cmd(plan)
        logger.info("Synth %s: running %s (timeout=%.0fs)", target, " ".join(make_cmd), timeout_s)

        # F-26: _persist_synth_log only lands at the END of a multi-minute
        # synth, so claim the log now — a tail during the wait must not see
        # the previous run's area/timing tail as this run's progress.
        self._open_run_log(target, self._synth_work_root(target))
        announce_unit(f"{role}: synthesis", target=target)
        observe_logs(
            [plan.build_dir / name for name in ("sv2v.log", "yosys.log", "openroad.log")],
            target=target,
            temporary=role == "baseline",
        )
        return self._run_configured(target, plan, planned)

    def _run_configured(
        self, target: str, plan: Any, planned: _PlannedSynthRecipe
    ) -> tuple[SynthMetrics, str]:
        """Run and interpret one configured plan, then refresh its OpenROAD reuse slot."""
        paths = _tool_source_paths(planned, plan.build_dir)
        observe_source_names(paths)
        with judged_commands() as commands:
            start = time.monotonic()
            run = self._run_synth_make(target, plan, resolved_source_bytes(planned.resolved))
            elapsed = time.monotonic() - start
            metrics, output = self._interpret_boundary_run(
                target,
                plan,
                run.result,
                elapsed,
                producer=run.producer,
                commands=commands,
                project_paths=paths,
            )
        metrics = self._attach_recipe_evidence(target, metrics)
        self._store_openroad_result(target, plan, metrics, run)
        return metrics, output

    def _run_synth_make(self, target: str, plan: Any, source_bytes: int | None) -> _SynthRun:
        """Run the synthesis Makefile; sv2v, Yosys and OpenROAD are measured as phases."""
        with (
            resource_phase(target, "synth", split=SYNTH_PHASES, source_bytes=source_bytes),
            yosys_tmpdir() as yosys_tmp,
        ):
            if not runs_openroad(plan.spec.timing.mode):
                cmd = [*self._synth_boundary_cmd(plan), yosys_tmpdir_assignment(yosys_tmp)]
                return _SynthRun(self._execute_boundary(cmd, timeout=self._get_timeout()))
            return self._run_physical_stages(target, plan, yosys_tmp)

    def _openroad_slot(self, target: str) -> openroad_reuse.ReuseSlot:
        """The Target's reuse slot, beside its work root (which every run wipes)."""
        root = work_root_for(self.args.work_dir, self.name, target, variant="openroad_reuse")
        return openroad_reuse.ReuseSlot(root)

    def _run_physical_stages(self, target: str, plan: Any, yosys_tmp: Path) -> _SynthRun:
        """Run Yosys, then OpenROAD unless the Target's reuse slot matches every input.

        A baseline runs in a throwaway worktree, so it neither reads nor
        writes a slot. Before OpenROAD runs, the slot is invalidated: a run
        that fails, times out or is interrupted leaves no slot that can hit.
        """
        timeout = self._get_timeout()
        start = time.monotonic()
        yosys_cmd = [*self._synth_boundary_cmd(plan, "yosys"), yosys_tmpdir_assignment(yosys_tmp)]
        yosys = self._execute_boundary(yosys_cmd, timeout=timeout)
        if yosys.returncode != 0 or yosys.timed_out:
            return _SynthRun(yosys)
        # Only a plan that holds OpenROAD may shrink here: a Target admitted as
        # logical but run as physical must keep its reservation through OpenROAD.
        if "openroad" in self._planned_tool_phases.get(target, ()):
            self._end_phases(target, "sv2v", "yosys")
        if getattr(self, "_execution_role", "candidate") != "candidate":
            return _SynthRun(
                _merge_stage_results(yosys, self._run_openroad_stage(plan, timeout, start))
            )
        identity = openroad_reuse.sandbox_identity()
        key = openroad_reuse.openroad_reuse_key(
            openroad_reuse.collect_openroad_inputs(plan, identity)
        )
        slot = self._openroad_slot(target)
        entry = slot.lookup(key) if key is not None and not self.args.no_cache else None
        if entry is not None:
            from booley.flows.terminal_progress import announce_unit

            # Before the copy: the log observer replays the restored openroad.log.
            announce_unit("candidate: OpenROAD reused", target=target)
            if slot.restore(entry, plan.build_dir):
                self._end_phases(target, "openroad")
                # The stage marker attributes the run to OpenROAD exactly as a fresh run.
                stdout = yosys.stdout.rstrip("\n") + "\nBOOLEY_STAGE: sta\n"
                return _SynthRun(replace(yosys, stdout=stdout), producer=entry.producer)
        slot.invalidate()
        openroad = self._run_openroad_stage(plan, timeout, start)
        self._end_phases(target, "openroad")
        return _SynthRun(_merge_stage_results(yosys, openroad), key=key, identity=identity)

    def _run_openroad_stage(self, plan: Any, timeout: int, start: float) -> SubprocessResult:
        """Run the Makefile's OpenROAD stage within what is left of the run's budget."""
        remaining = max(1, int(timeout - (time.monotonic() - start)))
        return self._execute_boundary(
            self._synth_boundary_cmd(plan, "openroad"), timeout=remaining
        )

    def _store_openroad_result(
        self, target: str, plan: Any, metrics: SynthMetrics, run: _SynthRun
    ) -> None:
        """Refresh the reuse slot after a fresh OpenROAD run that completed cleanly.

        Stores only when the key recomputed now equals the one computed
        before OpenROAD ran, so an input changed during the run stores
        nothing. A store failure never fails the run.
        """
        if run.key is None or not (
            metrics.termination == "completed"
            and metrics.returncode == 0
            and metrics.timing_complete
            and metrics.ppa_complete
        ):
            return
        post = openroad_reuse.openroad_reuse_key(
            openroad_reuse.collect_openroad_inputs(plan, run.identity)
        )
        if post != run.key:
            logger.info("Synth %s: OpenROAD inputs changed during the run; not stored", target)
            return
        producer = openroad_reuse.Producer(
            run_id=str(metrics.run_evidence.get("run_id", "")),
            started_at=rfc3339_from_epoch(run.result.dispatched_unix),
        )
        try:
            self._openroad_slot(target).store(
                run.key, plan.build_dir, plan.spec.design_name, producer
            )
        except OSError:
            logger.warning("Synth %s: could not store the OpenROAD result", target, exc_info=True)

    def _attach_recipe_evidence(self, target: str, metrics: SynthMetrics) -> SynthMetrics:
        """Attach the recipe resolved before this run to its metrics."""
        evidence = getattr(self, "_recipe_evidence", {}).get(target)
        if evidence is not None:
            metrics.recipe_snapshot, metrics.recipe_fingerprint, metrics.run_evidence = evidence
        return metrics

    def _read_boundary_output(
        self, plan: Any, result: SubprocessResult, project_paths: ProjectSourcePaths
    ) -> tuple[Any, str]:
        """Reconstruct fresh synthesis output from boundary artifacts."""
        from booley.flows.synth.backends import pipeline as syn_make

        outcome = syn_make.boundary_output(
            plan,
            result.returncode,
            stdout=result.stdout,
            is_stale=lambda p: self._is_stale_artifact(p, result.dispatched_unix or None),
            expected_comb_loops=expected_count("synth", self.args.work_dir, "expected_comb_loops"),
            project_paths=project_paths,
        )
        output = "\n".join(part for part in (result.stdout, result.stderr, outcome.text) if part)
        return outcome, output

    def _apply_boundary_completion(
        self,
        metrics: SynthMetrics,
        outcome: Any,
        result: SubprocessResult,
        output: str,
    ) -> EdaFailure | None:
        """Apply terminal and mode-specific completeness policy; return the failure it read."""
        metrics.expected_latches = expected_count("synth", self.args.work_dir, "expected_latches")
        metrics.expected_comb_loops = expected_count(
            "synth", self.args.work_dir, "expected_comb_loops"
        )
        metrics.warning_summary = outcome.diagnostics.warnings
        metrics.returncode = result.returncode
        metrics.timed_out = result.timed_out
        metrics.termination = _termination_reason(result, output)
        # The producer is a Tcl ``error`` that makes OpenROAD exit non-zero, so an
        # exit-0 log mentioning the token (echoed RTL comment) is not an input error.
        input_error = _INPUT_ERROR_RE.search(output) if result.returncode != 0 else None
        if input_error is not None:
            metrics.returncode = 2
            metrics.infra_error = input_error.group(1).strip()
            metrics.termination = "infrastructure_error"
        failure = _boundary_failure(metrics.termination, outcome, result, output)
        if failure is not None and failure.kind == "infrastructure":
            metrics.returncode = 2
            metrics.infra_error = failure.reason
            metrics.termination = "infrastructure_error"
        if result.peak_rss_mb is not None:
            metrics.peak_rss_mb = max(metrics.peak_rss_mb or 0.0, result.peak_rss_mb)
        if outcome.forced_failure and metrics.returncode == 0:
            metrics.returncode = 1
            metrics.termination = "eda_tool_failure"
            metrics.failure_output = outcome.forced_failure
        legacy_inline = (
            not getattr(outcome, "attempt_token", "")
            and not outcome.yosys_complete
            and metrics.has_metrics
            and bool(result.stdout.strip())
            and "BOOLEY_STAGE:" not in result.stdout
        )
        metrics.yosys_complete = outcome.yosys_complete or legacy_inline
        metrics.timing_complete = (
            metrics.termination == "completed"
            and metrics.synth_mode is not None
            and metrics.synth_mode.runs_openroad
            and metrics.has_timing_evidence
        )
        self._apply_structural_completion(metrics, outcome, result.stdout)
        if metrics.synth_mode is not None and metrics.synth_mode.runs_openroad:
            metrics.ppa_complete = (
                metrics.termination == "completed"
                and metrics.area_source == "openroad_post_optimization"
                and metrics.timing_complete
            )
        else:
            metrics.ppa_complete = (
                metrics.termination == "completed" and metrics.area_source == "yosys_mapped"
            )
        self._apply_design_failure(metrics, outcome, result, output)
        self._apply_unknown_failure(metrics, outcome, result)
        return failure

    @staticmethod
    def _apply_unknown_failure(
        metrics: SynthMetrics, outcome: Any, result: SubprocessResult
    ) -> None:
        """Unknown failure is execution evidence, never an implicit RTL verdict."""
        if metrics.termination in {"timeout", "oom", "resource_killed"}:
            metrics.infra_error = f"synthesis execution {metrics.termination}"
            metrics.returncode = 2
        elif metrics.termination == "infrastructure_error" and not metrics.infra_error:
            metrics.infra_error = "synthesis boundary could not start"
            metrics.returncode = 2
        elif metrics.returncode and metrics.termination not in {
            "design_failure",
            "infrastructure_error",
        }:
            stage = (
                getattr(outcome, "stage_marker", None)
                or getattr(outcome, "stage", None)
                or "synthesis"
            )
            log = getattr(outcome, "stage_log", "") or "\n".join((result.stdout, result.stderr))
            metrics.infra_error = _prefix_tool(stage, first_tool_diagnostic(log))
            metrics.returncode = 2
            metrics.termination = "infrastructure_error"
        if metrics.termination != "completed":
            log = getattr(outcome, "stage_log", "")
            if log:
                tail = _error_excerpt(log)
                if metrics.failure_output and metrics.failure_output not in tail:
                    tail = metrics.failure_output + "\n" + tail
                metrics.failure_output = tail

    @staticmethod
    def _apply_design_failure(
        metrics: SynthMetrics, outcome: Any, result: SubprocessResult, output: str
    ) -> None:
        """Normalize recognized design failures after stronger execution gates."""
        if metrics.termination in {"timeout", "oom", "resource_killed", "infrastructure_error"}:
            return
        diagnostic = getattr(outcome, "openroad_diagnostic", None)
        if diagnostic:
            kind = (
                "latch" if re.search(r"\$_DLATCH", diagnostic, re.IGNORECASE) else "generic cell"
            )
            diagnostic = f"unmapped {kind}: {diagnostic}"
        elif getattr(outcome, "design_diagnostic", None):
            diagnostic = outcome.design_diagnostic
        elif result.returncode == 0 and metrics.has_critical:
            diagnostic = _critical_diagnostic(metrics)
        elif (
            metrics.has_critical
            and result.returncode != 0
            and not outcome.yosys_complete
            and getattr(outcome, "stage", None) in {None, "yosys"}
        ):
            diagnostic = "critical structural conditions"
        if not diagnostic:
            return
        failure = classify_eda_failure(
            replace(result, stdout=output, stderr=""),
            expected_token=getattr(outcome, "attempt_token", None),
            expected_stage=getattr(outcome, "stage", None) or "yosys",
            design_diagnostic=diagnostic,
        )
        if failure is not None and failure.kind == "design":
            metrics.returncode = 1
            metrics.termination = "design_failure"
            metrics.failure_output = (
                f"{_EXIT_ZERO_ERROR_HEADER}\n  "
                if result.returncode == 0 and outcome.forced_failure
                else ""
            ) + failure.diagnostic

    @staticmethod
    def _apply_structural_completion(
        metrics: SynthMetrics,
        outcome: Any,
        stdout: str,
    ) -> None:
        """Apply authoritative pre-mapping and final check evidence or legacy inline evidence."""
        structural = outcome.diagnostics.structural
        legacy_inline = (
            not getattr(outcome, "attempt_token", "")
            and not outcome.yosys_complete
            and not structural.complete
            and metrics.has_metrics
            and bool(stdout.strip())
            and "BOOLEY_STAGE:" not in stdout
        )
        metrics.structural_checks_complete = structural.complete or legacy_inline
        if structural.complete:
            metrics.comb_loops = structural.comb_loops
            metrics.multi_driven = structural.multi_driven
        elif not legacy_inline:
            metrics.comb_loops = 0
            metrics.multi_driven = 0

    def _record_boundary_diagnostics(
        self,
        target: str,
        metrics: SynthMetrics,
        result: SubprocessResult,
        elapsed: float,
        output: str,
        project_paths: ProjectSourcePaths,
    ) -> None:
        """Persist output and surface terminal diagnostics without duplicating it."""
        self._persist_synth_log(target, output)
        metrics.log_path = ""
        if getattr(self, "_execution_role", "candidate") == "candidate":
            invocation_dir = self.reserve_invocation_dir()
            if invocation_dir is not None:
                try:
                    metrics.log_path = artifacts.publish_bytes(
                        invocation_dir,
                        ("artifacts", f"synth_{target_report_slug(target)}", "run.log"),
                        output.encode(),
                        work_dir=Path(self.args.work_dir),
                    )
                except OSError:
                    logger.warning(
                        "could not publish numbered synth log for %s", target, exc_info=True
                    )
        metrics.dirs = self._artifact_dirs(target, metrics.synth_mode)
        if metrics.termination == "oom":
            logger.warning("Synth %s was killed by the OOM killer", target)
        elif metrics.termination == "resource_killed":
            logger.warning("Synth %s was resource-killed", target)
        elif result.timed_out:
            logger.warning("Synth %s timed out after %.1fs", target, elapsed)
        elif result.returncode != 0:
            logger.warning("Synth %s failed with rc=%d", target, result.returncode)
        if metrics.termination != "completed" or (not metrics.has_metrics and result.returncode):
            self._log_boundary_failure(target, metrics, result, project_paths)

    def _log_boundary_failure(
        self,
        target: str,
        metrics: SynthMetrics,
        result: SubprocessResult,
        project_paths: ProjectSourcePaths,
    ) -> None:
        """Settle the failure text, naming sources by Project path, and log its first line.

        The line is a design check, or the tool's exit state.
        """
        metrics.failure_output = metrics.failure_output or _error_excerpt(
            "\n".join((result.stdout, result.stderr))
        )
        if metrics.latches and not metrics.failure_output.startswith(_latch_diagnostic(metrics)):
            metrics.failure_output = _latch_diagnostic(metrics) + "\n" + metrics.failure_output
        metrics.failure_output = project_paths.project_text(metrics.failure_output)
        metrics.infra_error = project_paths.project_text(metrics.infra_error)
        if metrics.termination == "design_failure":
            # The tool ran to completion; the design failed a check. The
            # process exit code and timeout state say nothing about that.
            logger.error(
                "Synth %s: design check failed: %s (full output: %s)",
                target,
                _design_failure_reason(metrics),
                metrics.log_path or "not persisted",
            )
            return
        reason_lines = metrics.failure_output.splitlines()
        logger.error(
            "Synth %s: boundary run failed (rc=%d, timed_out=%s): %s (full output: %s)",
            target,
            result.returncode,
            result.timed_out,
            metrics.infra_error or (reason_lines[0] if reason_lines else "(no output)"),
            metrics.log_path or "not persisted",
        )

    def _interpret_boundary_run(
        self,
        target: str,
        plan: Any,
        proc_result: SubprocessResult,
        elapsed: float,
        producer: openroad_reuse.Producer | None = None,
        *,
        commands: JudgedCommands,
        project_paths: ProjectSourcePaths,
    ) -> tuple[SynthMetrics, str]:
        """Interpret a boundary result from its freshness-gated artifacts.

        The run's recorded commands are judged from that interpretation.

        A *producer* means the OpenROAD outputs were restored from the reuse
        slot; they are interpreted exactly as a fresh run's, and labelled.
        """
        outcome, output = self._read_boundary_output(plan, proc_result, project_paths)
        metrics = _parse_synth_output(output, elapsed, plan.spec.timing.mode)
        if producer is not None:
            # At the end: run.log keeps only the tail of very long output.
            output += (
                f"\nBOOLEY_OPENROAD_REUSED: run_id={producer.run_id} "
                f"started_at={producer.started_at}"
            )
            metrics.openroad_reuse = {
                "producer_run_id": producer.run_id,
                "producer_started_at": producer.started_at,
            }
        failure = self._apply_boundary_completion(metrics, outcome, proc_result, output)
        _judge_stage_commands(commands, plan, metrics, outcome, failure)
        self._record_boundary_diagnostics(
            target, metrics, proc_result, elapsed, output, project_paths
        )

        return metrics, output

    def _artifact_dirs(self, target: str, synth_mode: SynthMode | None) -> dict[str, str]:
        """The directories holding *target*'s synth artifacts, by role.

        Two of them in physical mode: the build dir (Yosys/OpenROAD logs, the ``stat_*.txt``
        area report, both netlists, the rendered ``synth.ys``, the SDC fed to
        STA, the sv2v output) and the STA report dir under it.

        Directories rather than a file inventory on purpose. An earlier version
        enumerated all sixteen files: it was 80% of the report's bytes, and
        every key hardcoded a filename that would silently vanish the day the
        flow renamed its output — the "drop missing pointers" rule turns a
        rename into an absent key nobody notices. A directory cannot go stale,
        and the agent has ``ls``.
        """
        build_dir = self._synth_build_dir(target)
        dirs: dict[str, Path | str | None] = {"build": build_dir}
        if synth_mode is not None and synth_mode.runs_openroad:
            dirs["timing"] = build_dir / "reports" / "timing"
        return artifacts.artifacts_block(
            self.args.work_dir,
            dirs=dirs,
        ).get("dirs", {})  # type: ignore[return-value]

    def _persist_synth_log(self, target: str, output: str) -> str:
        """Write *target*'s full synth output to its Edalize work dir.

        Lands as ``run.log`` in ``.booley/.runtime/edalize/
        asic_synthesize/<target>/`` — the per-target dir the FuseSoC resolution
        already uses. Reuses the Flow-neutral :func:`write_run_log` for its
        tail-cap + atomic-write semantics (flows/ already depends on sim/ — see
        simulate.py).

        Returns the project-relative path, or ``""`` when the write failed —
        a log-write problem must never fail an otherwise-finished synth run.
        """
        from booley.flows.run_log import write_run_log

        log_dir = self._synth_work_root(target)
        try:
            make_directories(log_dir)
            log_path = write_run_log(log_dir, output)
        except OSError:
            logger.debug(
                "Synth %s: failed to persist run.log in %s", target, log_dir, exc_info=True
            )
            return ""
        return posix_relpath(log_path, self.args.work_dir)

    def _persist_baseline_log(
        self,
        target: str,
        output: str,
        project_root: Path,
    ) -> str:
        """Persist a baseline run's output to the *real* project runtime.

        The baseline run executes inside a throwaway worktree that is destroyed
        on context exit, so its ``run.log`` (written under that worktree by
        :meth:`_persist_synth_log`) would vanish. Re-write the captured output to
        the real project's runtime under a ``-baseline`` variant dir — distinct
        from the current run's ``run.log`` — and return that durable, project-
        relative path. Best-effort: a write failure yields ``""``.
        """
        from booley.flows.run_log import write_run_log

        log_dir = work_root_for(project_root, self.name, target, variant="baseline")
        try:
            make_directories(log_dir)
            log_path = write_run_log(log_dir, output)
        except OSError:
            logger.debug(
                "Synth %s: failed to persist baseline run.log in %s",
                target,
                log_dir,
                exc_info=True,
            )
            return ""
        # posix_relpath, not os.path.relpath: every other pointer Booley emits
        # is POSIX-separated because it is read inside the Linux Session
        # Runtime, where a Windows host's backslashes are meaningless.
        return posix_relpath(log_path, project_root)

    # -- Formatting helpers ---------------------------------------------------

    @staticmethod
    def _fmt_area(kge: float | None) -> str:
        return f"{kge:>6.1f} kGE" if kge is not None else "   -- kGE"

    @staticmethod
    def _fmt_timing(ps: float | None) -> str:
        return f"{ps:,.0f} ps" if ps is not None else "-- ps"

    @staticmethod
    def _fmt_delta(pct: float | None) -> str:
        if pct is None:
            return ""
        sign = "+" if pct >= 0 else ""
        return f"(delta {sign}{pct:.1f}%)"

    @staticmethod
    def _compute_delta_pct(
        current: float | None,
        baseline: float | None,
    ) -> float | None:
        if current is None or baseline is None or baseline == 0.0:
            return None
        return ((current - baseline) / baseline) * 100.0

    # -- Report writing -------------------------------------------------------

    def _build_implementation_report(
        self,
        context: ImplementationContext,
        current: SynthMetrics,
        baseline: SynthMetrics | None,
    ) -> ImplementationReport:
        """Adapt native synthesis evidence to the shared canonical schema."""
        return build_synth_implementation_report(
            context,
            current,
            baseline,
            fatal_timing=getattr(self, "_timing_violation_is_fatal", False),
        )

    def _target_report_payload(
        self,
        target: str,
        current: SynthMetrics,
        baseline: SynthMetrics | None,
        baseline_ref: str | None,
        report_path: Path,
    ) -> dict[str, Any]:
        """Return the per-Target JSON report before the canonical verdict is set."""
        report = _build_report_dict(
            self.name,
            target,
            current,
            baseline,
            baseline_ref,
            self._compute_delta_pct,
            eda_tool=self._eda_tool,
        )
        plan = self._target_plan(target)
        report["baseline_target"] = plan.baseline.selector
        report["candidate_target"] = plan.candidate.selector
        report["baseline_target_identity"] = plan.baseline.identity
        report["candidate_target_identity"] = plan.candidate.identity
        report["run_evidence"] = current.run_evidence or None
        report["baseline_run_evidence"] = baseline.run_evidence if baseline else None
        run_id = os.environ.get("BOOLEY_RUN_ID", "")
        if run_id:
            report["run_id"] = run_id
        self._add_report_artifacts(report, report_path.parent, report_path, target, current)
        return report

    def _add_report_artifacts(
        self,
        report: dict[str, Any],
        report_dir: Path,
        report_path: Path,
        target: str,
        metrics: SynthMetrics,
    ) -> None:
        """Set ``report["artifacts"]``: this file, the run log, and preserved evidence."""
        # The run.log pointer lived only in the stdout ``log:`` line, which the
        # MCP layer tail-truncates; ``reports`` was already here but not the log
        # itself. Both now travel together in the shared ``artifacts`` shape,
        # alongside this file's own path.
        preserved = self._snapshot_report_artifacts(
            report_dir, synth_target_report_slug(target), metrics
        )
        report["artifacts"] = {
            "report": posix_relpath(report_path, self.args.work_dir),
            **preserved,
        }
        if "baseline" in report:
            self._add_baseline_timing_pointer(report, target)

    def _add_baseline_timing_pointer(self, report: dict[str, Any], target: str) -> None:
        """Name the published baseline timing reports, or say why there are none."""
        # A missing entry means the baseline wrote no timing reports.
        published = getattr(self, "_baseline_timing", {}).get(target, "")
        if published:
            report["artifacts"]["baseline_timing"] = published
        elif published is None:
            report["baseline"]["timing_unavailable"] = (
                "baseline timing reports could not be published"
            )
        else:
            report["baseline"]["timing_unavailable"] = "baseline run produced no timing reports"

    def _snapshot_report_artifacts(
        self, report_dir: Path, safe_target: str, metrics: SynthMetrics
    ) -> dict[str, Any]:
        """Copy mutable log/timing evidence beside this invocation's report."""
        root = Path(self.args.work_dir)
        invocation_dir = self.reserve_invocation_dir()
        assert invocation_dir is not None
        result: dict[str, Any] = {
            **({"log": metrics.log_path} if metrics.log_path else {}),
            **({"live_dirs": dict(metrics.dirs)} if metrics.dirs else {}),
        }
        timing = metrics.dirs.get("timing") if metrics.dirs else None
        if timing:
            published = _publish_timing_dir(
                invocation_dir,
                root / timing,
                ("artifacts", f"synth_{safe_target}", "timing"),
                root,
                f"numbered synth timing evidence for {safe_target}",
            )
            if published:
                result["timing"] = published
        return result

    # -- Main run logic -------------------------------------------------------

    def _run_policy_error(self) -> str | None:
        """Latch the per-run policy state, or return the message that blocks the run.

        The ``fail_on_timing_violation`` gate (F-37) must be settled before a
        half-hour synthesis starts: a mistyped knob has to fail now, not while
        formatting the report at the end.
        """
        try:
            self._timing_violation_is_fatal = _fail_on_timing_violation(self.args.work_dir)
            self._report_path_count = _timing_report_paths(self.args.work_dir)
        except BoundaryError as exc:
            return f"synth: {exc}"
        return None

    def _begin_run(self, targets: list[str], /) -> None:
        """Reset the per-run baseline state so a reused instance never leaks a stale value."""
        self._baseline_selfcompare_msg = None
        self._baseline_timing = {}

    def _enter_baseline_worktree(
        self, pairs: Sequence[TargetPairPlan], project_root: Path, worktree: Path, /
    ) -> None:
        """Warn when every pair compares a Target against its own identity."""
        if all(plan.baseline.identity == plan.candidate.identity for plan in pairs):
            self._baseline_selfcompare_msg = _baseline_self_compare_warning(project_root, worktree)

    def _run_target(self, target: str, publish: Callable[[SynthMetrics], None], /) -> SynthMetrics:
        """Synthesize one Target under its work-root lease, publishing inside the lease."""
        baseline = getattr(self, "_execution_role", "candidate") == "baseline"
        output = ""
        try:
            with edam.work_root_lease(
                self._synth_work_root(target),
                timeout_s=self._get_timeout(),
                on_wait=lambda: self._report_workspace_wait(target),
            ):
                metrics, output = self._run_single_config(target)
                if baseline:
                    self._publish_baseline_timing(
                        metrics, target, self._target_pairs, self._project_root
                    )
                publish(metrics)
        except edam.WorkRootLeaseError as exc:
            logger.error("Synth %s%s: %s", "baseline " if baseline else "", target, exc)
            output = str(exc)
            metrics = _infra_metrics(output)
            publish(metrics)
        finally:
            # Whatever ran, a phase the Target never reached must not hold its estimate.
            self._end_phases(target, "sv2v", "yosys", "openroad", "synth")
        if baseline:
            metrics.log_path = self._persist_baseline_log(target, output, self._project_root)
        return metrics

    def _result_text(
        self,
        targets: list[str],
        current: dict[str, SynthMetrics],
        baseline: dict[str, SynthMetrics],
        reports: dict[str, ImplementationReport],
        aggregate: ImplementationAggregate,
        /,
    ) -> tuple[list[str], list[str], str]:
        """Build the synthesis table, notes and ``RESULT:`` headline from policy-resolved evidence."""
        self._discard_stale_selfcompare(targets, current, baseline)
        notes, failed_targets, selfcompare_msg = self._aggregate_prefix(baseline, reports)
        violated = self._append_target_blocks(notes, failed_targets, targets, current)
        self._append_fatal_timing_failure(failed_targets, violated)
        headline = _result_line(
            failed_targets,
            selfcompare_msg,
            violated,
            self._eda_warning_headlines(targets, current),
            aggregate.exit_code,
        )
        table = self._summary_table_lines(targets, current, baseline, selfcompare_msg, reports)
        return table, notes, headline

    def _result_detail(
        self,
        targets: list[str],
        current: dict[str, SynthMetrics],
        baseline: dict[str, SynthMetrics],
        short_sha: str | None,
        reports: dict[str, ImplementationReport],
        aggregate: ImplementationAggregate,
        /,
    ) -> dict[str, Any]:
        return self._implementation_aggregate_detail(
            targets, current, baseline, short_sha, reports, aggregate
        )

    def _display_lines(self, targets: list[str], current: dict[str, SynthMetrics], /) -> list[str]:
        return _first_valid_display(targets, current)

    def _synth_work_unit(
        self,
        target: str,
        planned: _PlannedSynthRecipe,
        *,
        role: WorkUnitRole,
        revision: str | None,
    ) -> WorkUnitPlan:
        """Project one resolved ASIC recipe into the shared plan contract."""
        resolved = planned.resolved
        root = Path(self.args.work_dir)
        sources = tuple(
            _plan_source_name(root, item)
            for item in resolved.files
            if item.file_type.lower() != "sdc"
        )
        constraints = tuple(_plan_source_name(root, item) for item in resolved.sdc_files)
        snapshot = synthesis_recipe_snapshot(resolved, self.args, target=target)
        plan_recipe = dict(snapshot)
        recipe_args = snapshot.get("recipe_args", [])
        physical = False
        if "--synth-mode" in recipe_args:
            index = recipe_args.index("--synth-mode")
            physical = recipe_args[index + 1] == "physical"
            if physical:
                plan_recipe["clock_validation"] = (
                    "Target SDC clock creation is validated by OpenROAD at runtime"
                )
        build_dir = self._synth_build_dir(target)
        make = ("make", "-C", normalize_plan_path(build_dir, root))
        # Physical runs Yosys and OpenROAD as separate stages (OpenROAD may be reused).
        make_commands = [(*make, "yosys"), (*make, "openroad")] if physical else [make]
        handle = self._target_handle(target)
        return WorkUnitPlan(
            unit_id=stable_unit_id(
                "synth",
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
            eda_tool="yosys",
            timeout_ms=self._timeout_ms(),
            sources=sources,
            constraints=constraints,
            parameters=resolved.parameters,
            recipe=plan_recipe,
            commands=(
                CommandPlan(
                    self._normalized_synth_plan_command(planned, root),
                    cwd=".",
                    template=True,
                ),
                *(CommandPlan(command, cwd=".", template=True) for command in make_commands),
            ),
            expected_artifacts=(
                normalize_plan_path(self._synth_work_root(target) / "run.log", root),
            ),
        )

    @staticmethod
    def _normalized_synth_plan_command(
        planned: _PlannedSynthRecipe,
        root: Path,
    ) -> tuple[str, ...]:
        """Replace disposable staged paths with their Target-owned names."""
        replacements = {
            _work_relpath(item.absolute(planned.resolved.build_root), root): _plan_source_name(
                root, item
            )
            for item in planned.resolved.files
        }
        replacements.update(
            {
                _work_relpath(item.absolute(planned.resolved.build_root).parent, root): (
                    PurePosixPath(_plan_source_name(root, item)).parent.as_posix()
                )
                for item in planned.resolved.files
                if item.is_include
            }
        )
        return tuple(
            replacements.get(argument, argument)
            for argument in normalize_plan_argv(planned.command, root)
        )

    def _plan_candidate_unit(
        self,
        target: str,
        revision: str | None,
        recipes: dict[str, _PlannedSynthRecipe],
    ) -> WorkUnitPlan:
        """Plan one candidate Target: in scratch for a dry run, else under its lease."""
        if self.args.dry_run:
            with self._dry_run_build_root(target) as build_root:
                planned = self._resolve_synth_recipe(target, build_root=build_root)
                return self._synth_work_unit(target, planned, role="candidate", revision=revision)
        lease = edam.work_root_lease(
            self._synth_work_root(target),
            timeout_s=self._get_timeout(),
            on_wait=lambda selected=target: self._report_workspace_wait(selected),
        )
        with lease:
            planned = self._resolve_synth_recipe(target)
        recipes[target] = planned
        return self._synth_work_unit(target, planned, role="candidate", revision=revision)

    def _plan_implementation(self, targets: list[str]) -> FlowPlan:
        """Resolve candidate and baseline recipes before either revision runs."""
        candidate_units: list[WorkUnitPlan] = []
        baseline_units: list[WorkUnitPlan] = []
        errors: list[str] = []
        candidate_revision = git_full_sha("HEAD", Path(self.args.work_dir))
        candidate_recipes: dict[str, _PlannedSynthRecipe] = {}
        for target in targets:
            try:
                candidate_units.append(
                    self._plan_candidate_unit(target, candidate_revision, candidate_recipes)
                )
            except (
                BoundaryError,
                edam.WorkRootLeaseError,
                fusesoc_registry.FuseSocError,
                OSError,
                ValueError,
            ) as exc:
                errors.append(f"candidate {target}: {exc}")
        self._planned_candidate_synth_recipes = candidate_recipes

        baseline_ref = self.args.baseline
        if baseline_ref:
            baseline_units, baseline_errors = self._plan_synth_baselines(baseline_ref)
            errors.extend(baseline_errors)
        return FlowPlan(
            flow="synth",
            mode="implementation",
            work_units=(*baseline_units, *candidate_units),
            aggregate_errors=tuple(errors),
            planning_disclosures=(
                "FuseSoC setup may run in invocation-owned disposable scratch; "
                "EDA commands are not executed.",
            ),
        )

    def _plan_synth_baselines(
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
        candidate_evidence = dict(getattr(self, "_recipe_evidence", {}))
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
                seen: set[str] = set()
                for pair in self._target_pairs:
                    target = pair.baseline.selector
                    if target in seen:
                        continue
                    seen.add(target)
                    try:
                        planned = self._resolve_synth_recipe(target)
                        units.append(
                            self._synth_work_unit(
                                target,
                                planned,
                                role="baseline",
                                revision=revision,
                            )
                        )
                    except (
                        BoundaryError,
                        fusesoc_registry.FuseSocError,
                        OSError,
                        ValueError,
                    ) as exc:
                        errors.append(f"baseline {target}: {exc}")
        except (BaselineWorktreeError, ImplementationComparisonError) as exc:
            errors.append(f"baseline: {exc}")
        finally:
            self.args.work_dir = project_root
            self._target_handles = candidate_handles
            self._target_execution_refs = candidate_refs
            self._recipe_evidence = candidate_evidence
        return units, errors

    def _discard_stale_selfcompare(
        self,
        targets: list[str],
        current_results: dict[str, SynthMetrics],
        baseline_results: dict[str, SynthMetrics],
    ) -> None:
        if self._baseline_selfcompare_msg and any(
            baseline_results.get(target) is not None
            and baseline_results[target].recipe_fingerprint
            != current_results[target].recipe_fingerprint
            for target in targets
        ):
            self._baseline_selfcompare_msg = None

    def _publish_baseline_timing(
        self,
        metrics: SynthMetrics,
        baseline: str,
        pairs: Sequence[TargetPairPlan],
        project_root: Path,
    ) -> None:
        """Copy the baseline's timing reports out of the ephemeral worktree.

        Runs while the worktree and the work-root lease still exist. The copy
        lands beside each paired candidate's own ``timing/`` directory.
        """
        invocation_dir = self.reserve_invocation_dir()
        timing = metrics.dirs.get("timing") if metrics.dirs else None
        if invocation_dir is None or not timing:
            return
        for plan in pairs:
            if plan.baseline.selector != baseline:
                continue
            candidate = plan.candidate.selector
            published = _publish_timing_dir(
                invocation_dir,
                Path(self.args.work_dir) / timing,
                ("artifacts", f"synth_{synth_target_report_slug(candidate)}", "baseline_timing"),
                project_root,
                f"baseline synth timing evidence for {candidate}",
            )
            if published != "":
                self._baseline_timing[candidate] = published

    def _target_note_lines(self, tgt: str, cur: SynthMetrics) -> list[str]:
        """Render the notes (everything but the table row) for one target."""
        lines: list[str] = []
        if cur.passed:
            detail = self._format_detail_note(tgt, cur)
            if detail:
                lines.append(detail)
            if cur.estimated_fmax_mhz is not None:
                lines.append(
                    note_line(
                        tgt,
                        "WARNING",
                        "estimated Fmax is probably inaccurate; "
                        "logical mode excludes placement and wire delays",
                    )
                )
        elif cur.has_metrics and not cur.ppa_complete:
            lines.append(
                note_line(
                    tgt,
                    "PARTIAL",
                    "Yosys emitted intermediate area/cell data, but the synthesis/PPA "
                    "run did not complete; do not use these numbers as final QoR",
                )
            )
        if not cur.structural_checks_complete and (
            cur.latches or cur.comb_loops or cur.multi_driven or cur.process_count
        ):
            lines.append(
                note_line(
                    tgt,
                    "INCONCLUSIVE",
                    "structural counts were emitted before the final synthesis checks completed",
                )
            )
        lines.extend(self._constraint_note_lines(tgt, cur))
        if cur.has_critical:
            lines.append(self._format_critical_line(tgt, cur))
        if cur.warning_summary.total_warnings:
            lines.append(self._format_warning_line(tgt, cur))
            lines.extend(self._removed_warning_lines(tgt, cur))
        if cur.openroad_reuse:
            lines.append(self._format_reuse_line(tgt, cur.openroad_reuse))
        if cur.failure_output:
            lines.append(self._format_failure_output(tgt, cur))
        if cur.log_path:
            lines.append(f"  {tgt}: log: {cur.log_path}")
        return lines

    @staticmethod
    def _timing_violation(tgt: str, cur: SynthMetrics) -> str | None:
        """Return one aggregate timing-violation message for *tgt*."""
        timing_violations: list[str] = []
        if cur.wns_ns is not None and cur.wns_ns < 0:
            timing_violations.append(f"setup slack {cur.wns_ns:.3f} ns")
        worst_hold = _worst_hold_slack_ns(cur)
        if worst_hold is not None and worst_hold < 0:
            timing_violations.append(f"hold slack {worst_hold:.3f} ns")
        return f"{tgt}: {', '.join(timing_violations)}" if timing_violations else None

    def _emit_target_block(
        self,
        tgt: str,
        cur: SynthMetrics,
    ) -> tuple[list[str], str | None, str | None]:
        """Return note lines, timing violation, and failure summary for one target."""
        failure = None if cur.passed else self._format_failure_summary(tgt, cur)
        return self._target_note_lines(tgt, cur), self._timing_violation(tgt, cur), failure

    @staticmethod
    def _implementation_aggregate_detail(
        targets: list[str],
        current: dict[str, SynthMetrics],
        baseline: dict[str, SynthMetrics],
        baseline_ref: str | None,
        reports: dict[str, ImplementationReport],
        aggregate: ImplementationAggregate,
    ) -> dict[str, Any]:
        detail = _aggregate_detail(targets, current, baseline, baseline_ref)
        detail["passed"] = aggregate.exit_code == EXIT_SUCCESS
        for target, report in reports.items():
            detail[target]["passed"] = report.passed
        detail["implementation"] = aggregate.detail
        return detail

    @staticmethod
    def _eda_warning_headlines(
        targets: list[str],
        current_results: dict[str, SynthMetrics],
    ) -> list[str]:
        """Return concise actionable-warning summaries for the result headline."""
        return [
            f"{target}: {current_results[target].warning_summary.actionable_warnings} "
            "actionable EDA warning(s)"
            for target in targets
            if current_results[target].warning_summary.actionable_warnings
        ]

    def _aggregate_prefix(
        self,
        baseline_results: dict[str, SynthMetrics],
        reports: dict[str, ImplementationReport],
    ) -> tuple[list[str], list[str], str | None]:
        """Return the run-level notes, failures and self-compare message."""
        lines, failures = self._baseline_error_notes(baseline_results, reports)
        selfcompare_msg = getattr(self, "_baseline_selfcompare_msg", None)
        if selfcompare_msg:
            lines.append(note_line("", "WARNING", selfcompare_msg))
        return lines, failures, selfcompare_msg

    def _append_target_blocks(
        self,
        lines: list[str],
        failures: list[str],
        targets: list[str],
        current_results: dict[str, SynthMetrics],
    ) -> list[str]:
        """Append every target's notes to *lines*; return the timing violations."""
        violated: list[str] = []
        for target in targets:
            target_lines, violation, failure = self._emit_target_block(
                target, current_results[target]
            )
            lines.extend(target_lines)
            if violation is not None:
                violated.append(violation)
            if failure is not None:
                failures.append(failure)
        return violated

    def _append_fatal_timing_failure(
        self,
        failures: list[str],
        violated: list[str],
    ) -> None:
        if violated and getattr(self, "_timing_violation_is_fatal", False):
            failures.append(
                f"timing VIOLATED ({'; '.join(violated)}) "
                "-- [flows.synth] fail_on_timing_violation = true"
            )

    def _format_config_line(
        self,
        tgt: str,
        cur: SynthMetrics,
        base: SynthMetrics | None,
        *,
        human: bool = False,
    ) -> str:
        """Format one target's area/timing/elapsed line."""
        area_str = self._fmt_area(cur.area_kge)
        timing_str = (
            "logical estimate"
            if cur.synth_mode is SynthMode.LOGICAL
            else self._fmt_timing(_worst_critical_path_ps(cur))
        )
        elapsed_str = format_duration(cur.elapsed_s) if human else f"{cur.elapsed_s:.1f}s"
        if base:
            area_delta = self._compute_delta_pct(cur.area_kge, base.area_kge)
            timing_delta = self._compute_delta_pct(
                _worst_critical_path_ps(cur),
                _worst_critical_path_ps(base),
            )
            line = (
                f"[synth] {tgt:<10}"
                f"{area_str} {self._fmt_delta(area_delta):>20}   "
                f"{timing_str} {self._fmt_delta(timing_delta):>20}   "
                f" {elapsed_str}{self._format_status_suffix(cur)}"
            )
        else:
            line = (
                f"[synth] {tgt:<10}"
                f"{area_str}   {timing_str}    {elapsed_str}"
                f"{self._format_status_suffix(cur)}"
            )

        return (
            ProgressText(line, record_text=self._format_config_line(tgt, cur, base))
            if human
            else line
        )

    @staticmethod
    def _format_failure_output(tgt: str, cur: SynthMetrics) -> str:
        """Render the captured subprocess error tail under the config line."""
        indented = "\n".join(f"    {ln}" for ln in cur.failure_output.splitlines())
        return f"  {tgt}: subprocess output:\n{indented}"

    @staticmethod
    def _format_detail_note(tgt: str, cur: SynthMetrics) -> str | None:
        """Metrics the table has no column for (cells, estimated Fmax, hold, reg2reg).

        These numbers otherwise land only in ``util/syn/`` report files; a
        concise note keeps the happy-path output actionable. ``None`` when the
        run produced none of them.
        """
        parts: list[str] = []
        if cur.cells is not None:
            parts.append(f"{cur.cells:,} cells")
        if cur.estimated_fmax_mhz is not None:
            parts.append(f"estimated Fmax {cur.estimated_fmax_mhz:.0f} MHz")
        worst_hold = _worst_hold_slack_ns(cur)
        if worst_hold is not None:
            parts.append(f"hold slack {worst_hold:+.3f} ns")
        if cur.reg2reg_fmax_mhz is not None:
            parts.append(f"reg2reg Fmax {cur.reg2reg_fmax_mhz:.0f} MHz")
        elif cur.reg2reg_slack_ns is not None:
            parts.append(f"reg2reg slack {cur.reg2reg_slack_ns:+.3f} ns")
        return f"  {tgt}: {', '.join(parts)}" if parts else None

    def _summary_table_lines(
        self,
        targets: list[str],
        current: dict[str, SynthMetrics],
        baseline: dict[str, SynthMetrics],
        selfcompare_msg: str | None,
        reports: dict[str, ImplementationReport],
    ) -> list[str]:
        """The per-Target table: one row per Target; ``Δ`` columns only against a baseline."""
        with_delta = any(target in baseline for target in targets)
        delta = ["Δ"] if with_delta else []
        headers = ["Target", "Area", *delta, "Crit path", *delta, "Fmax", "Setup slack", "Time"]
        headers.append("Status")
        rows = [
            self._summary_row(
                target,
                current[target],
                baseline.get(target),
                with_delta,
                # The self-compare WARN is run-level: it applies to every compared Target.
                baseline_not_meaningful=bool(selfcompare_msg) and target in baseline,
                comparison_error=reports[target].grade == "error",
            )
            for target in targets
        ]
        return render_table(headers, rows, max_widths={0: TARGET_WIDTH})

    def _summary_row(
        self,
        target: str,
        cur: SynthMetrics,
        base: SynthMetrics | None,
        with_delta: bool,
        *,
        baseline_not_meaningful: bool,
        comparison_error: bool,
    ) -> list[str]:
        """Cells of one Target's table row (``—`` for a metric the run lacks)."""
        worst = worst_clock(cur.per_clock)
        # Per-clock metrics: the timing-worst clock, named only for multi-clock designs.
        tag = f" [{worst.clock}]" if worst and len(cur.per_clock) > 1 else ""
        logical = cur.synth_mode is SynthMode.LOGICAL
        crit_ps = _worst_critical_path_ps(cur)
        if logical:
            crit = "logical estimate"
        else:
            crit = f"{crit_ps:,.0f} ps" if crit_ps is not None else MISSING
        fmax = (
            f"{worst.fmax_mhz:.0f} MHz{tag}"
            if worst is not None and worst.fmax_mhz is not None and not logical
            else MISSING
        )
        area = f"{cur.area_kge:.1f} kGE" if cur.area_kge is not None else MISSING
        slack = f"{cur.wns_ns:+.3f} ns" if cur.wns_ns is not None else MISSING
        status = self._format_status_suffix(cur).strip() or self._run_status(
            target, cur, baseline_not_meaningful=baseline_not_meaningful
        )
        if comparison_error and not status.startswith("ERROR"):
            # RESULT is ERROR and names this Target, whatever else its own run did.
            status = "ERROR (baseline)"
        cells = [target, area]
        if with_delta:
            cells.append(self._delta_cell(cur.area_kge, base.area_kge if base else None))
        cells.append(crit)
        if with_delta:
            cells.append(
                self._delta_cell(crit_ps, _worst_critical_path_ps(base) if base else None)
            )
        return [*cells, fmax, slack, f"{cur.elapsed_s:.1f}s", status_cell(status)]

    def _run_status(
        self,
        target: str,
        cur: SynthMetrics,
        *,
        baseline_not_meaningful: bool,
    ) -> str:
        """Status of a Target whose run passed, agreeing with the ``RESULT:`` line.

        ``FAIL (timing)`` for a fatal timing violation, ``WARN (<reasons>)``
        for every WARN reason this Target adds, else ``PASS``. The inputs are the
        ones ``_result_line`` is fed: ``_timing_violation``, the actionable EDA
        warnings, and the run's self-compare message.
        """
        timing = self._timing_violation(target, cur)
        if timing and getattr(self, "_timing_violation_is_fatal", False):
            return "FAIL (timing)"
        reasons = [
            reason
            for reason, applies in (
                ("baseline", baseline_not_meaningful),
                ("timing", bool(timing)),
                ("EDA warnings", bool(cur.warning_summary.actionable_warnings)),
            )
            if applies
        ]
        return f"WARN ({', '.join(reasons)})" if reasons else "PASS"

    @classmethod
    def _delta_cell(cls, current: float | None, baseline: float | None) -> str:
        """``+1.2%`` against the baseline, ``—`` when it cannot be computed."""
        pct = cls._compute_delta_pct(current, baseline)
        return f"{pct:+.1f}%" if pct is not None else MISSING

    @staticmethod
    def _format_reuse_line(tgt: str, producer: dict[str, str]) -> str:
        """Name the run whose OpenROAD results this run reused."""
        when = format_clock(producer.get("producer_started_at"))
        run = producer.get("producer_run_id")
        return note_line(tgt, "REUSED", f"OpenROAD results reused from run {run} ({when})")

    def _constraint_note_lines(self, tgt: str, cur: SynthMetrics) -> list[str]:
        """The I/O-bound and unconstrained-timing NOTEs, when they apply."""
        lines: list[str] = []
        if _is_io_bound_critical(cur):
            lines.append(self._format_io_bound_line(tgt, cur))
        if cur.unconstrained is not None and cur.unconstrained.found:
            lines.append(self._format_unconstrained_line(tgt, cur, cur.unconstrained))
        return lines

    @staticmethod
    def _format_unconstrained_line(
        tgt: str, cur: SynthMetrics, unconstrained: UnconstrainedTiming
    ) -> str:
        """Advisory: timing numbers cover only the paths the SDC constrains."""

        def ports(names: tuple[str, ...], noun: str, command: str) -> str:
            shown = ", ".join(names[:3])
            more = len(names) - 3
            if more > 0:
                shown += f", +{more} more"
            plural = noun if len(names) == 1 else f"{noun}s"
            return f"{len(names)} {plural} without {command} ({shown})"

        parts: list[str] = []
        if unconstrained.endpoints:
            count = len(unconstrained.endpoints)
            parts.append(f"{count} unconstrained endpoint{'' if count == 1 else 's'}")
        if unconstrained.inputs_without_delay:
            parts.append(ports(unconstrained.inputs_without_delay, "input", "set_input_delay"))
        if unconstrained.outputs_without_delay:
            parts.append(ports(unconstrained.outputs_without_delay, "output", "set_output_delay"))
        return note_line(
            tgt,
            "NOTE",
            f"timing covers only constrained paths: {'; '.join(parts)}; "
            f"full lists: {cur.log_path or 'run.log'}",
        )

    @staticmethod
    def _format_io_bound_line(tgt: str, cur: SynthMetrics) -> str:
        """Advisory: the worst path is I/O-bound; its remedy is in the Target's SDC."""
        worst = cur.wns_ns
        r2r = cur.reg2reg_slack_ns
        return note_line(
            tgt,
            "NOTE",
            f"critical path is I/O-bound "
            f"(worst slack {worst:+.3f} ns vs reg2reg {r2r:+.3f} ns); "
            "set the port's real external delay with set_input_delay / "
            "set_output_delay, or exclude it with set_false_path, in the Target's "
            "file_type: SDC fileset",
        )

    @staticmethod
    def _format_critical_line(tgt: str, cur: SynthMetrics) -> str:
        """Format a CRITICAL conditions warning line."""
        parts = []
        if cur.unexpected_latches:
            parts.append(_latch_diagnostic(cur))
        if cur.unexpected_comb_loops:
            parts.append(_comb_loop_diagnostic(cur))
        if cur.multi_driven:
            parts.append(f"{cur.multi_driven} multi-driven")
        return note_line(tgt, "CRITICAL", ", ".join(parts))

    @staticmethod
    def _removed_warning_lines(tgt: str, cur: SynthMetrics) -> list[str]:
        """Show each earlier-pass loop or driver warning that optimization removed, with its reason."""
        return [
            note_line(
                tgt,
                "NOTE" if group.disposition == "informational" else "WARNING",
                f"{group.message} {group.rationale}",
            )
            for group in cur.warning_summary.representatives
            if group.removed
        ]

    @staticmethod
    def _format_warning_line(tgt: str, cur: SynthMetrics) -> str:
        """Format the bounded warning inventory without replaying the log."""
        summary = cur.warning_summary
        label = "WARNING" if summary.actionable_warnings else "NOTE"
        noun = "warning" if summary.total_warnings == 1 else "warnings"
        return note_line(
            tgt,
            label,
            f"{summary.total_warnings} EDA {noun}, "
            f"{summary.unique_warnings} unique; full diagnostics: "
            f"{cur.log_path or 'run.log'}",
        )

    @staticmethod
    def _format_status_suffix(cur: SynthMetrics) -> str:
        """Append a concise failure reason to the per-config summary line."""
        if cur.passed:
            return ""
        reason = ""
        if cur.timed_out:
            reason = "timeout"
        elif cur.termination == "oom":
            reason = "OOM"
        elif cur.termination == "resource_killed":
            reason = "resource-killed"
        elif cur.infra_error:
            return "   ERROR"
        elif cur.termination == "design_failure":
            reason = _design_failure_reason(cur)
        elif cur.returncode != 0 and not cur.has_metrics:
            reason = f"rc={cur.returncode}, no metrics"
        elif cur.returncode != 0:
            reason = f"rc={cur.returncode}"
        elif not cur.has_metrics:
            reason = "no metrics"
        return f"   FAIL ({reason})" if reason else "   FAIL"

    @staticmethod
    def _format_failure_summary(tgt: str, cur: SynthMetrics) -> str:
        """Format one target failure for the final RESULT line."""
        return f"{tgt}: {cur.infra_error or cur.failure_reason()}"


def _synth_target_warnings(top: str, defines: list[str]) -> list[str]:
    """Flag a synth target that looks aimed at simulation rather than the DUT.

    Two common misconfigurations make synthesis fail deep inside Yosys with
    opaque errors instead of up front:

    * the resolved toplevel is a testbench (``*_tb``) — testbenches instantiate
      stimulus/clock generators that don't synthesize;
    * a ``SIMULATION`` define is set — it gates simulation-only constructs such
      as ``$fatal`` assertions that Yosys can't synthesize.

    Both point at the same fix: synthesize a dedicated synth target whose
    toplevel is the DUT and that leaves ``SIMULATION`` undefined (see the
    ``booley-setup`` skill, Step 6, ASIC synthesis). Returns human-readable
    warnings.
    """
    warnings: list[str] = []
    if top and top.lower().endswith("_tb"):
        warnings.append(
            f"synth toplevel {top!r} looks like a testbench (ends in '_tb'); "
            "synthesis expects the DUT as top. Point synth at a "
            "dedicated synth target whose toplevel is the DUT."
        )
    sim = [d for d in defines if d == "SIMULATION" or d.startswith("SIMULATION=")]
    if sim:
        warnings.append(
            "synth defines SIMULATION; it enables simulation-only constructs "
            "(e.g. $fatal assertions) that Yosys cannot synthesize. Use a synth "
            "target that leaves SIMULATION undefined."
        )
    return warnings


def _prefix_tool(tool: str, diagnostic: str) -> str:
    """Name the failing tool once: keep a diagnostic that already starts with it."""
    if re.match(rf"\s*{re.escape(tool)}\s*:", diagnostic, re.IGNORECASE):
        return diagnostic
    return f"{tool}: {diagnostic}"


def _error_excerpt(output: str, max_lines: int = 12) -> str:
    """Retain the bounded nonempty tail of the failing tool's output, minus markers."""
    lines = [line.rstrip() for line in strip_failure_markers(output).splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:])


if __name__ == "__main__":
    AsicSynthesizeFlow().cli()
