"""AsicSynthesizeFlow — run ASIC synthesis per config with optional baseline comparison.

Wraps the Yosys synthesis flow (sv2v + Yosys + ABC) with structured reporting,
critical-condition detection, and optional delta computation against a baseline
git ref. The baseline is synthesized in a throwaway ``git worktree`` (see
:mod:`booley.flows.baseline_worktree`) rather than by checking the ref out in place, so
``--baseline`` never touches the caller's working tree and works in both Ticket
and Interactive Mode.

The configure half renders scripts and a Makefile into the per-target build dir
in-process (:mod:`booley.flows.synth.pipeline`), execution runs ``make -C <rel>`` in
the Session Runtime, and the interpret half reconstructs the report from files
the make run left in the build directory.
"""

from __future__ import annotations

import argparse
import copy
import logging
import os
import re
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from booley.core.boundary import BoundaryError, as_int, require_bool
from booley.criteria.templates import TargetPair
from booley.flows.synth.backends.yosys.core import (
    FRONTEND_CHOICES,
    NAND2_AREA_UM2,
)
from booley.flows.synth.mode import SynthMode
from booley.flows.synth.timing import parse_perclock
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_ERROR, EXIT_SUCCESS, McpToolResult
from booley.runtime import job_slots
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.flow_names import config_section

from .. import artifacts
from ..base import BooleyFlow, SubprocessResult
from ..baseline_worktree import (
    BaselineWorktreeError,
    baseline_worktree,
    git_full_sha,
    git_short_sha,
    resolve_ticket_baseline,
)
from ..clock_timing import (
    ClockTiming,
    make_clock_timing,
    per_clock_to_json,
    worst_clock,
)
from ..implementation_comparison import (
    ImplementationComparisonError,
    target_pair_for_candidate,
    target_pairs_for_candidates,
)
from ..implementation_publication import (
    ImplementationProgress,
    ImplementationPublisher,
    target_report_slug,
)
from ..implementation_report import (
    ImplementationAggregate,
    ImplementationReport,
    build_implementation_aggregate,
)
from ..recipe_evidence import BASELINE_TARGET_DETAIL, CANDIDATE_TARGET_DETAIL
from ..run_evidence import (
    BASELINE_RUN_EVIDENCE_DETAIL,
    RUN_EVIDENCE_DETAIL,
    build_flow_run_evidence,
)
from ..source_fingerprint import compute_source_fingerprint
from ..target_parameters import vlogdefine_args as _vlogdefine_args
from ..target_parameters import vlogparam_args as _vlogparam_args
from .implementation_report import (
    build_synth_implementation_report,
)
from .ppa_config import add_ppa_arguments
from .recipe import (
    BASELINE_RECIPE_FINGERPRINT_DETAIL,
    BASELINE_RECIPE_SNAPSHOT_DETAIL,
    BASELINE_REF_DETAIL,
    RECIPE_FINGERPRINT_DETAIL,
    RECIPE_SNAPSHOT_DETAIL,
    resolve_synth_mode,
    synthesis_recipe_args,
    synthesis_recipe_snapshot,
    synthesis_recipe_snapshot_fingerprint,
)

logger = logging.getLogger(__name__)


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
    if requested is not None:
        try:
            return max(1, int(requested))
        except (TypeError, ValueError):
            return 1800000
    if work_dir is None:
        return 1800000
    return max(1, as_int(_load_flow_config(work_dir).get("timeout_ms"), 1800000))


def _expected_latches(work_dir: Path) -> int:
    """``[flows.synth].expected_latches`` — declared intentional latches.

    A standard-cell integrated clock gater is built from a deliberate
    ``always_latch``; lowRISC's generic ``prim_clock_gating`` contains exactly
    one. Failing on any latch at all makes such a design unsynthesizable
    through Booley even though the RTL is correct (F-19). Declaring the count
    keeps the check meaningful — one more latch than declared still fails, and
    the raw count is always reported — while letting a correct design pass.

    A negative or non-integer value is ignored (treated as 0) rather than
    silently widening the gate.
    """
    raw = _load_flow_config(work_dir).get("expected_latches", 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(0, raw)


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


# Standard-cell NAND2_X1 equivalent conversion for Nangate.
# 1 GE is one NAND2_X1 cell; 1 kGE is 1000 NAND2_X1 cells.
KGE_DIVISOR = NAND2_AREA_UM2 * 1000.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SynthMetrics:
    """Parsed synthesis metrics for a single config run."""

    area_um2: float | None = None
    # Provenance for the canonical area selected by ``synth_mode``.
    area_source: str = ""
    area_kge: float | None = None
    cells: int | None = None
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
    elapsed_s: float = 0.0
    latches: int = 0
    expected_latches: int = 0
    """Latches the design is declared to contain on purpose
    (``[flows.synth].expected_latches``). Not every latch is an
    accident: a standard-cell library's integrated clock gater is built from
    a deliberate ``always_latch``, and lowRISC's generic
    ``prim_clock_gating`` ships exactly one. Treating every latch as an
    unwaivable error made a correct design unsynthesizable (F-19). Latches
    are always reported; only the count above this is a critical condition."""
    comb_loops: int = 0
    multi_driven: int = 0
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

    def __post_init__(self) -> None:
        """Normalize string-compatible construction at the metrics interface."""
        if isinstance(self.synth_mode, str):
            self.synth_mode = SynthMode(self.synth_mode)

    def qor_detail(self) -> dict[str, Any]:
        """Return the canonical QoR fields shared by every report surface."""
        detail: dict[str, Any] = {
            "area_um2": self.area_um2,
            "area_source": self.area_source,
            "area_kge": self.area_kge,
            "cells": self.cells,
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
            "multi_driven": self.multi_driven,
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
    def has_critical(self) -> bool:
        return self.structural_checks_complete and (
            self.unexpected_latches > 0
            or self.comb_loops > 0
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
            and not self.has_critical
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_area(output: str) -> tuple[float | None, int | None]:
    """Extract area_um2 and cell_count from synthesis output.

    Looks for the Yosys ``stat -liberty`` patterns:
      - "Chip area for top module '...': <number>"
      - "Number of cells: <number>" (Yosys <= 0.5x)
      - "<count> <area> cells" stat-table row (Yosys 0.67+ dropped the
        "Number of cells:" line for a count/area column table)
    Falls back to any "Chip area for" line.
    """
    area: float | None = None
    cells: int | None = None

    # Area — prefer "top module" line (hierarchical total)
    m = re.search(r"Chip area for top module\b.*?:\s*([\d.]+)", output)
    if m:
        area = float(m.group(1))
    else:
        matches = re.findall(r"Chip area for\b.*?:\s*([\d.]+)", output)
        if matches:
            area = float(matches[-1])

    # Cell count — legacy summary line first, then the 0.67+ table's total
    # "cells" row (bare word: per-type rows carry a cell name after it).
    m = re.search(r"Number of cells:\s*(\d+)", output)
    if m:
        cells = int(m.group(1))
    else:
        table = re.findall(r"^\s*(\d+)\s+[0-9.eE+-]+\s+cells\s*$", output, flags=re.MULTILINE)
        if table:
            # Multiple stat blocks (per module): the totals accumulate per
            # module; take the last block's row — it follows the final
            # (post-mapping, flattened-top) stat, matching the area pick.
            cells = int(table[-1])

    return area, cells


def _parse_per_clock_sta(output: str) -> dict[str, ClockTiming]:
    """Build the per-clock timing map from ``STA_PERCLOCK`` markers.

    Each clock's ``critical_path_ps``/``fmax_mhz`` is derived from its period and
    worst setup slack by the shared :mod:`booley.flows.clock_timing` helper, so
    the STA and Vivado flows share one ns→ps→MHz derivation. ``critical_path_ps``
    intentionally means STA timing — ABC ``delay =`` mapper estimates are never
    a source here.
    """
    return {
        name: make_clock_timing(name, row["period_ns"], row["wns_ns"], row["whs_ns"])
        for name, row in parse_perclock(output).items()
    }


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

    That matters because the generated SDC sets I/O delays as a *percentage of
    the clock period*: ``set_input_delay`` and ``set_output_delay`` both scale
    with ``period_ps``, so an I/O path's slack barely moves as the period is
    tuned (a pure in->out feedthrough is exactly period-invariant, since the
    scaled input and output delays cancel). An author chasing an I/O-bound
    violation by shrinking ``period_ps`` chases a tail that never closes; the
    real levers are declaring the actual external I/O delays or false-pathing
    the port via the ``[flows.synth.timing].sdc`` knob. (SETUP-28)
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


def _parse_logical_estimated_fmax(output: str) -> float | None:
    """Convert the final mapped-ABC delay marker in ps to an Fmax estimate."""
    matches = re.findall(r"YOSYS_ABC_LOGIC_DELAY_PS:\s*([0-9]+(?:\.[0-9]+)?)", output)
    delays_ps = [float(value) for value in matches if float(value) > 0]
    if not delays_ps:
        return None
    return 1_000_000.0 / max(delays_ps)


def _parse_wire_count(output: str) -> int:
    """Extract wire count from Yosys stat output."""
    m = re.search(r"Number of wires:\s*(\d+)", output)
    return int(m.group(1)) if m else 0


def _parse_process_count(output: str) -> int:
    """Extract process count from Yosys stat output (non-zero = unsynthesized)."""
    m = re.search(r"Number of processes:\s*(\d+)", output)
    return int(m.group(1)) if m else 0


# A Yosys ``stat`` cell tally line: leading whitespace, the cell type, then
# the instance count. Counting these is exact; counting bare ``$dlatch``
# occurrences over the whole log also catches mentions in banners and
# techmap/ABC traces, inflating the number that decides a FAIL.
_DLATCH_STAT_RE = re.compile(r"^\s+\$_?DLATCH\S*\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE)


def _count_latches(output: str) -> int:
    """Number of latch cells Yosys inferred.

    Prefers the ``stat`` cell tally, which is an exact instance count. A stat
    section that ran but prints **no** ``$_DLATCH*`` row means zero latches —
    ``stat`` omits cell types with no instances, so "no tally" must not be
    conflated with "run died before ``stat``" (ravenoc F-29: yosys-slang emits
    transient ``$driver$…($dlatch)`` helper cells that opt folds away; the
    occurrence fallback counted 1680 log mentions on a netlist with zero
    latches and failed a clean synthesis). Occurrence-matching remains only
    for runs with no stat section at all, where over-counting is the safe
    direction.
    """
    tallies = _DLATCH_STAT_RE.findall(output)
    if tallies:
        return sum(int(n) for n in tallies)
    if "Printing statistics." in output:
        return 0
    return len(re.findall(r"\$dlatch", output))


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
    mapped_area_um2, cells = _parse_area(output)
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
        wire_count=wire_count,
        process_count=process_count,
        wns_ns=wns_ns,
        per_clock=_parse_per_clock_sta(output),
        estimated_fmax_mhz=estimated_fmax_mhz,
        reg2reg_slack_ns=reg2reg_slack_ns,
        reg2reg_fmax_mhz=reg2reg_fmax_mhz,
        synth_mode=mode,
        elapsed_s=elapsed_s,
        latches=latches,
        comb_loops=comb_loops,
        multi_driven=multi_driven,
        peak_rss_mb=float(peak_matches[-1]) if peak_matches else None,
    )


_RESOURCE_KILL_RE = re.compile(
    r"(?:\b(?:exit|code|error)\s*137\b|\breturncode\s*[=:]\s*137\b|\bkilled\b)",
    re.IGNORECASE,
)


def _termination_reason(result: SubprocessResult, output: str) -> str:
    """Classify the boundary's terminal state without overclaiming OOM."""
    if result.timed_out:
        return "timeout"
    if result.returncode == 0:
        return "completed"
    if result.oom_kill_delta > 0:
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
) -> str:
    """The single ``RESULT:`` headline, in strict severity order.

    FAIL outranks every WARN; among the WARNs a meaningless baseline delta
    outranks a timing violation. Each WARN still exits 0 — only *failed*
    (which already folds in an opted-in
    ``fail_on_timing_violation``, F-37) moves the exit code.
    """
    if failed:
        return f"RESULT: FAIL ({'; '.join(failed)})"
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
        return f"RESULT: WARN -- timing VIOLATED ({'; '.join(violated)})"
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
    "cells",
    "wire_count",
    "per_clock",
    "estimated_fmax_mhz",
)
_REPORT_QOR_FIELDS = (
    "area_kge",
    "area_um2",
    "area_source",
    "cells",
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
    "cells",
    "per_clock",
    "estimated_fmax_mhz",
)
_CRITERION_METRIC_MAP = {
    "area": "area_um2",
    "area_um2": "area_um2",
    "area_kge": "area_kge",
    "cell_count": "cells",
    "wire_count": "wire_count",
    "critical_path_ps": "critical_path_ps",
    "fmax_mhz": "fmax_mhz",
    "wns_ns": "wns_ns",
    "whs_ns": "whs_ns",
    "period_ns": "period_ns",
    "reg2reg_fmax_mhz": "reg2reg_fmax_mhz",
    "reg2reg_slack_ns": "reg2reg_slack_ns",
}
_CRITERION_MIN_ALLOWED = ["fmax_mhz", "reg2reg_fmax_mhz", "wns_ns", "whs_ns"]


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


def _add_baseline_criterion_detail(detail: dict[str, Any], baseline: SynthMetrics) -> None:
    """Attach baseline QoR and recipe evidence to criterion detail."""
    detail["baseline_metrics"] = _select_present(
        baseline.qor_detail(),
        _BASELINE_DETAIL_FIELDS,
    )
    detail[BASELINE_RECIPE_FINGERPRINT_DETAIL] = baseline.recipe_fingerprint or None
    detail[BASELINE_RECIPE_SNAPSHOT_DETAIL] = baseline.recipe_snapshot or None
    detail[BASELINE_RUN_EVIDENCE_DETAIL] = baseline.run_evidence or None


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
        "multi_driven": metrics.multi_driven,
        # The aggregate detail is what reaches the agent as MCP
        # structuredContent, so the pointers ride along with the numbers.
        "artifacts": {
            **({"log": metrics.log_path} if metrics.log_path else {}),
            **({"dirs": dict(metrics.dirs)} if metrics.dirs else {}),
        },
    }
    if baseline is not None:
        summary["baseline_metrics"] = {
            "passed": baseline.passed,
            "returncode": baseline.returncode,
            "infra_error": baseline.infra_error,
            "ppa_complete": baseline.ppa_complete,
            **_select_present(baseline.qor_detail(), _BASELINE_DETAIL_FIELDS),
        }
    return summary


def _aggregate_detail(
    targets: list[str],
    current_results: dict[str, SynthMetrics],
    baseline_results: dict[str, SynthMetrics] | None = None,
    baseline_sha: str | None = None,
) -> dict[str, Any]:
    """Detail for the run-level ``asic_synthesize.json`` (SETUP-F-29b).

    Every number used to live ONLY in the per-target ``synthesis_ok_<tgt>``
    criteria, leaving the flat aggregate report with ``"detail": {}`` — so a
    consumer that read the Flow's own report file (the MCP poll path, triage)
    saw a verdict with no QoR behind it and had to go hunting through
    ``state.json``. Mirror the per-target summaries here, keyed by target,
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
    if baseline_metrics and baseline_ref:
        report["baseline"] = _baseline_report_detail(baseline_metrics, baseline_ref)
        _add_report_deltas(report, metrics, baseline_metrics, compute_delta)
    if metrics.failure_output:
        report["failure_output"] = metrics.failure_output
    report["conditions"] = metrics.structural_detail()
    return report


def _baseline_self_compare_warning(project_root: Path, wt: Path) -> str | None:
    """Detect a no-op ``--baseline`` self-comparison and return an actionable warning.

    An unpaired project may mirror live ``.booley_project/cores/`` (ADR 0036)
    into the baseline worktree because the outer Git ref does not contain those
    files. A project whose RTL lives as real files below those cores can therefore
    synthesize byte-identical sources on both sides. Paired project repositories
    instead materialize their ticket fork, but can still compare identical RTL.

    Compare the canonical source fingerprint (:func:`compute_source_fingerprint`,
    the same ``_source_fingerprint`` the criteria records already stamp) of the
    two trees. Only flag when the project HAS stealth cores *and* both sides hash
    identical, so a plain project whose git-tracked RTL genuinely didn't change
    between the ref and HEAD isn't nagged about a legitimately-zero delta.
    Returns ``None`` when there is nothing to warn about (or the fingerprint
    can't be computed — never let the guard itself break a run).

    The caller later suppresses this warning when the two normalized Target
    recipes differ, because that is a meaningful comparison even with identical
    RTL sources.
    """
    from booley.fusesoc.fusesoc_registry import state_cores_dir

    if not state_cores_dir(project_root).is_dir():
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


class AsicSynthesizeFlow(BooleyFlow):
    """Run ASIC synthesis for one or more Targets with optional baseline comparison."""

    def _resolve_job_class(self) -> str:
        """Synthesis is a heavy Session Runtime workload."""
        return job_slots.CLASS_HEAVY

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
        "Target with NO SDC is a hard error, not a silent default: "
        "pass --default-clock <ps> to run against an explicitly-named canned "
        "clock instead. "
        "Persistent ppa_profile (compact|balanced|max_frequency), flatten, "
        "frontend, synth_mode, and advanced_settings_yosys/advanced_settings_openroad "
        "recipe knobs belong in the selected .core Target's flow_options. "
        "Per-call flags can override ppa_profile, flatten, frontend, and expert "
        "backend settings; synth_mode remains Target-owned. [flows.synth] in "
        "booley.toml is only for enablement and verdict "
        "policy such as target, timeout_ms, and expected_latches."
    )
    code_modifying: bool = False
    # Minimum outer MCP kill budget (seconds). mcp_server scales this floor by
    # per-target timeout, matrix width, and baseline/current pass count.
    default_timeout: int = 7200
    satisfies: ClassVar[list[str]] = ["synthesis_ok"]

    # The built-in flow is make-driven: run_yosys_syn renders the build tree,
    # then results are interpreted from files under that same directory.
    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--baseline",
            default=None,
            help="Baseline git ref (SHA/branch/tag) for comparison",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print commands without executing",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=None,
            help="Per-config timeout in ms. Overrides "
            "[flows.synth].timeout_ms; both default to 1800000. "
            "OpenROAD placement + repair on large designs needs the "
            "headroom; the outer MCP budget scales with the matrix.",
        )
        # ADR 0031: explicit opt-in for the canned clock. A Target with no
        # file_type:SDC fileset is a hard error UNLESS this names a period, so
        # the default is chosen per-run instead of fabricated silently.
        parser.add_argument(
            "--default-clock",
            type=float,
            default=None,
            metavar="PS",
            dest="default_clock",
            help="Named canned clock period (ps) for a Target that carries no "
            "SDC fileset. Without it, a constraint-less Target is a hard "
            "error rather than a silent ~250 MHz default.",
        )
        # ADR 0029 decision 7: flatten is an A/B experiment toggle, so it lives
        # on the Flow CLI. Tri-state: unset (None) means "use the selected
        # Target's flow_options.flatten"; the explicit flag
        # wins over it. --no-flatten shares the dest so the two are exclusive.
        parser.add_argument(
            "--flatten",
            dest="flatten",
            action="store_true",
            default=None,
            help="Flatten the hierarchy before tech-mapping (overrides the "
            "selected Target's flow_options.flatten).",
        )
        parser.add_argument(
            "--no-flatten",
            dest="flatten",
            action="store_false",
            default=None,
            help="Preserve hierarchy through synthesis (overrides the selected Target).",
        )
        # RTL frontend A/B toggle (like --flatten): sv2v transpile vs Yosys
        # 0.67 native read_slang. Tri-state — unset (None) uses the selected
        # Target's flow_options.frontend (else sv2v).
        parser.add_argument(
            "--frontend",
            choices=list(FRONTEND_CHOICES),
            default=None,
            help="RTL frontend: 'sv2v' (transpile + read_verilog) or 'slang' "
            "(native Yosys read_slang, requires the Yosys>=0.67 sandbox image). "
            "Overrides the selected Target's flow_options.frontend (default sv2v).",
        )
        add_ppa_arguments(parser)

    # -- BooleyFlow abstract methods (unused — we override _run) ----------

    def _build_command(self) -> list[str]:
        """Not used — _run handles multi-config orchestration directly."""
        return []

    def _interpret_result(self, result: SubprocessResult) -> McpToolResult:
        """Not used — _run handles interpretation directly."""
        return McpToolResult()

    # -- Command builder for a single config ----------------------------------

    def _append_rtl_source_args(
        self,
        cmd: list[str],
        resolved: object,
        work_dir: Path,
    ) -> str:
        """Append ``-t``/``--extra-rtl``/``--inc-dir`` flags; return the top module."""
        # Top comes from the resolved Target (decision 12).
        top = resolved.toplevel
        if top:
            cmd.extend(["-t", top])
        for f in resolved.rtl_hdl_source_files:
            rel = posix_relpath(f.absolute(resolved.build_root), work_dir)
            cmd.extend(["--extra-rtl", rel])
        for inc in resolved.rtl_include_dirs:
            cmd.extend(["--inc-dir", posix_relpath(inc, work_dir)])
        return top

    def _append_sta_constraint_args(
        self,
        cmd: list[str],
        resolved: object,
        target: str,
        work_dir: Path,
        synth_mode: SynthMode,
    ) -> None:
        """Append ``--sta-sdc``/``--default-clock`` flags (ADR 0029/0031)."""
        if not synth_mode.runs_openroad:
            return
        # STA constraints from the Target's file_type:SDC fileset (ADR 0029):
        # forward one --sta-sdc per file, in EDAM fileset order. Same boundary
        # treatment as --extra-rtl — each is a path relative to the worktree,
        # resolved against /work inside the sandbox.
        for sdc_file in resolved.sdc_files:
            rel = posix_relpath(sdc_file.absolute(resolved.build_root), work_dir)
            cmd.extend(["--sta-sdc", rel])
        # ADR 0031 (P1): design constraints are explicit and per-target. A Target
        # with no file_type:SDC fileset must name its clock via --default-clock,
        # or it is a hard error here — refuse to fabricate a 250 MHz clock and
        # report a PPA number against a period the author never chose. Caught by
        # _run_single_config (BoundaryError -> rc=2 infra) so the fix-hint reaches
        # the report instead of crashing the whole run. Fail during in-process
        # configuration, before EDA execution, with the Target name that
        # run_yosys_syn's own guard cannot know.
        default_clock = getattr(self.args, "default_clock", None)
        if not resolved.sdc_files and default_clock is None:
            raise BoundaryError(
                f"synth: Target {target!r} has no timing constraints. "
                "Add a `file_type: SDC` fileset (create_clock / set_input_delay / "
                "set_output_delay / set_false_path) to the Target, or pass "
                "--default-clock <ps> to run against an explicitly-named clock."
            )
        if default_clock is not None:
            cmd.extend(["--default-clock", str(default_clock)])

    def _append_typed_param_args(
        self,
        cmd: list[str],
        resolved: object,
        target: str,
        top: str,
    ) -> None:
        """Append ``-d``/``-p`` typed-param flags and warn on likely sim Targets."""
        # FuseSoC owns build-time defines. ``vlogdefine`` parameters on this
        # Target are the sole source, shared with FPGA.
        defines = _vlogdefine_args(resolved.parameters)
        for define in defines:
            cmd.extend(["-d", define])
        for assignment in _vlogparam_args(resolved.parameters):
            cmd.extend(["-p", assignment])
        # Warn early when the target looks aimed at simulation (testbench top or
        # SIMULATION define) — otherwise it fails deep inside Yosys.
        for warning in _synth_target_warnings(top, defines):
            logger.warning("Synth %s: %s", target, warning)

    def _append_synth_recipe_args(self, cmd: list[str], resolved: Any, target: str) -> None:
        """Append the shared normalized synthesis recipe for this invocation."""
        cmd.extend(synthesis_recipe_args(resolved.flow_options, self.args, target=target))

    def _resolve_synth_target(self, target: str) -> Any:
        """Clean and resolve one FuseSoC Target into its isolated build root."""
        from ..edam import work_root_for

        build_root = work_root_for(self.args.work_dir, self.name, target)
        shutil.rmtree(build_root, ignore_errors=True)
        return fusesoc_registry.resolve_target(
            target,
            project_root=self.args.work_dir,
            build_root=build_root,
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

    def _build_synth_cmd(self, target: str) -> list[str]:
        """Resolve *target* and build its validated run_yosys_syn spec argv.

        FuseSoC owns design description; this command-gen exception forwards
        its staged sources into Booley's make-driven Yosys/OpenROAD module.
        Baselines point ``work_dir`` at a separate worktree, so resolution and
        artifacts remain isolated. The argv also remains runnable for dry-run.
        """
        resolved = self._resolve_synth_target(target)
        self._record_recipe_evidence(target, resolved)
        work_dir = Path(self.args.work_dir)
        cmd = ["python3", "-m", "booley.flows.synth.configure", "configure"]
        top = self._append_rtl_source_args(cmd, resolved, work_dir)
        synth_mode = resolve_synth_mode(resolved.flow_options, target=target)
        self._append_sta_constraint_args(cmd, resolved, target, work_dir, synth_mode)
        self._append_typed_param_args(cmd, resolved, target, top)
        self._append_synth_recipe_args(cmd, resolved, target)
        cmd.extend(["-w", target])
        return cmd

    def _synth_build_dir(self, target: str) -> Path:
        """The make-driven synth build dir for *target* (under its work root).

        A ``synth/`` leaf inside the per-target Edalize work root keeps the
        rendered scripts/Makefile/logs clear of the FuseSoC-staged tree that
        shares the root. Wiped together with the root by ``_build_synth_cmd``'s
        pre-resolution cleanup.
        """
        from ..edam import work_root_for

        return work_root_for(self.args.work_dir, self.name, target) / "synth"

    def _configure_synth(self, target: str, cmd: list[str]) -> Any:
        """Render *target*'s make-driven build dir from the spec argv (ADR 0037 §8).

        The configure half: *cmd* (the run_yosys_syn spec argv built by
        ``_build_synth_cmd``) is parsed back by run_yosys_syn's own parser —
        guaranteeing the two stay shape-compatible — resolved against this
        run's work_dir, and rendered into scripts + a Makefile. Returns the
        :class:`booley.flows.synth.pipeline.SynthPlan` the interpret half consumes.

        The liberty existence check is hard because configuration and execution
        share the Session Runtime filesystem.

        Raises ``SystemExit`` (run_yosys_syn's validation guards) or ``OSError``
        (render failure); ``_run_single_config`` maps both to infra errors.
        """
        from booley.flows.synth import configure as run_yosys_syn
        from booley.flows.synth import pipeline as syn_make

        args = run_yosys_syn.parse_configure_argv(cmd)
        spec = run_yosys_syn.resolve_spec(
            args,
            project_root=Path(self.args.work_dir),
            require_liberty=True,
        )
        return syn_make.configure_synthesis(spec, self._synth_build_dir(target))

    def _synth_boundary_cmd(self, plan: Any) -> list[str]:
        """Return the Session Runtime command for a configured synthesis plan."""
        from .. import edam

        return ["make", "-C", edam.relpath_for_make(plan.build_dir, self.args.work_dir)]

    # -- Single-target run ----------------------------------------------------

    def _run_single_config(
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
        self._eda_tool = "yosys"

        # FuseSoC resolution happens in-process now (decision 4); a setup failure
        # becomes an infra error for this target, not an unhandled crash of the
        # whole _run.
        try:
            cmd = self._build_synth_cmd(target)
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
        except OSError as exc:
            msg = f"failed to render synthesis build dir: {exc}"
            logger.warning("Synth %s: %s", target, msg)
            return self._attach_recipe_evidence(target, _infra_metrics(msg)), msg

        # A bare `make -C <rel>` runs the generated plan with EDA binaries from
        # the Session Runtime PATH.
        make_cmd = self._synth_boundary_cmd(plan)
        logger.info("Synth %s: running %s (timeout=%.0fs)", target, " ".join(make_cmd), timeout_s)

        # F-26: _persist_synth_log only lands at the END of a multi-minute
        # synth, so claim the log now — a tail during the wait must not see
        # the previous run's area/timing tail as this run's progress.
        from ..edam import work_root_for

        self._open_run_log(target, work_root_for(self.args.work_dir, self.name, target))
        start = time.monotonic()
        proc_result = self._execute_boundary(make_cmd, timeout=self._get_timeout())
        elapsed = time.monotonic() - start
        metrics, output = self._interpret_boundary_run(target, plan, proc_result, elapsed)
        return self._attach_recipe_evidence(target, metrics), output

    def _attach_recipe_evidence(self, target: str, metrics: SynthMetrics) -> SynthMetrics:
        """Attach the recipe resolved before this run to its metrics."""
        evidence = getattr(self, "_recipe_evidence", {}).get(target)
        if evidence is not None:
            metrics.recipe_snapshot, metrics.recipe_fingerprint, metrics.run_evidence = evidence
        return metrics

    def _read_boundary_output(self, plan: Any, result: SubprocessResult) -> tuple[Any, str]:
        """Reconstruct fresh synthesis output from boundary artifacts."""
        from booley.flows.synth import pipeline as syn_make

        outcome = syn_make.boundary_output(
            plan,
            result.returncode,
            is_stale=lambda p: self._is_stale_artifact(p, result.dispatched_unix or None),
        )
        output = "\n".join(part for part in (result.stdout, result.stderr, outcome.text) if part)
        return outcome, output

    def _apply_boundary_completion(
        self,
        metrics: SynthMetrics,
        outcome: Any,
        result: SubprocessResult,
        output: str,
    ) -> None:
        """Apply terminal and mode-specific completeness policy to metrics."""
        metrics.expected_latches = _expected_latches(self.args.work_dir)
        metrics.returncode = result.returncode
        metrics.timed_out = result.timed_out
        metrics.termination = _termination_reason(result, output)
        if result.peak_rss_mb is not None:
            metrics.peak_rss_mb = max(metrics.peak_rss_mb or 0.0, result.peak_rss_mb)
        if outcome.forced_failure and metrics.returncode == 0:
            metrics.returncode = 1
            metrics.termination = "eda_tool_failure"
        metrics.yosys_complete = metrics.termination == "completed" or outcome.yosys_complete
        metrics.timing_complete = (
            metrics.termination == "completed"
            and metrics.synth_mode is not None
            and metrics.synth_mode.runs_openroad
            and metrics.has_timing_evidence
        )
        metrics.structural_checks_complete = metrics.yosys_complete
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

    def _record_boundary_diagnostics(
        self,
        target: str,
        metrics: SynthMetrics,
        result: SubprocessResult,
        elapsed: float,
        output: str,
    ) -> None:
        """Persist output and surface terminal diagnostics without duplicating it."""
        metrics.log_path = self._persist_synth_log(target, output)
        metrics.dirs = self._artifact_dirs(target, metrics.synth_mode)
        if metrics.termination == "oom":
            logger.warning("Synth %s was killed by the cgroup OOM killer", target)
        elif metrics.termination == "resource_killed":
            logger.warning("Synth %s was resource-killed", target)
        elif result.timed_out:
            logger.warning("Synth %s timed out after %.1fs", target, elapsed)
        elif result.returncode != 0:
            logger.warning("Synth %s failed with rc=%d", target, result.returncode)
        if metrics.termination != "completed" or (not metrics.has_metrics and result.returncode):
            metrics.failure_output = _error_excerpt(output)
            reason_lines = metrics.failure_output.splitlines()
            logger.error(
                "Synth %s: boundary run failed (rc=%d, timed_out=%s): %s (full output: %s)",
                target,
                result.returncode,
                result.timed_out,
                reason_lines[0] if reason_lines else "(no output)",
                metrics.log_path or "not persisted",
            )

    def _interpret_boundary_run(
        self,
        target: str,
        plan: Any,
        proc_result: SubprocessResult,
        elapsed: float,
    ) -> tuple[SynthMetrics, str]:
        """Interpret a boundary result from its freshness-gated artifacts."""
        outcome, output = self._read_boundary_output(plan, proc_result)
        metrics = _parse_synth_output(output, elapsed, plan.spec.timing.mode)
        self._apply_boundary_completion(metrics, outcome, proc_result, output)
        self._record_boundary_diagnostics(target, metrics, proc_result, elapsed, output)

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
        dirs = {"build": build_dir}
        if synth_mode is not None and synth_mode.runs_openroad:
            dirs["timing"] = build_dir / "reports" / "timing"
        return artifacts.artifacts_block(
            self.args.work_dir,
            dirs=dirs,
        ).get("dirs", {})  # type: ignore[return-value]

    def _persist_synth_log(self, target: str, output: str) -> str:
        """Write *target*'s full synth output to its Edalize work dir.

        Lands as ``run.log`` in ``.booley_project/.runtime/edalize/
        asic_synthesize/<target>/`` — the per-target dir the FuseSoC resolution
        already uses. Reuses the sim layer's :func:`write_run_log` for its
        tail-cap + atomic-write semantics (flows/ already depends on sim/ — see
        simulate.py).

        Returns the project-relative path, or ``""`` when the write failed —
        a log-write problem must never fail an otherwise-finished synth run.
        """
        from booley.flows.run_log import write_run_log

        from ..edam import work_root_for

        log_dir = work_root_for(self.args.work_dir, self.name, target)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
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

        from ..edam import work_root_for

        log_dir = work_root_for(project_root, self.name, target, variant="baseline")
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
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

    def _timeout_ms(self) -> int:
        """Resolve the per-config timeout in ms.

        Precedence: explicit ``--timeout`` CLI/MCP arg (trusted, argparse-typed)
        > ``[flows.synth].timeout_ms`` in booley.toml (validated) >
        1800000 default. Making it a project knob lets a design whose OpenROAD
        repair_timing needs more than 30 min raise the inner cap without a code
        change; the MCP layer derives the aggregate matrix budget from it.
        """
        return _resolve_synth_timeout_ms(self.args.work_dir, self.args.timeout)

    def _get_timeout(self) -> int:
        """Per-config timeout in whole seconds (see :meth:`_timeout_ms`)."""
        return max(1, self._timeout_ms() // 1000)

    # -- Formatting helpers ---------------------------------------------------

    @staticmethod
    def _fmt_area(kge: float | None) -> str:
        return f"{kge:>6.1f} KGe" if kge is not None else "   -- KGe"

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

    def _implementation_report(
        self,
        target: str,
        metrics: SynthMetrics,
        baseline_metrics: SynthMetrics | None,
        baseline_ref: str | None,
    ) -> ImplementationReport:
        """Adapt native synthesis evidence to the shared canonical schema."""
        pair = target_pair_for_candidate(getattr(self, "_target_pairs", ()), target)
        return build_synth_implementation_report(
            target=target,
            pair=pair,
            current=metrics,
            baseline=baseline_metrics,
            baseline_ref=self.args.baseline if baseline_metrics is not None else None,
            resolved_baseline_ref=(
                getattr(self, "_baseline_full_sha", None) or baseline_ref
                if baseline_metrics is not None
                else None
            ),
            eda_tool=self._eda_tool,
            fatal_timing=getattr(self, "_timing_violation_is_fatal", False),
        )

    def _publisher(self) -> ImplementationPublisher:
        return ImplementationPublisher(
            work_dir=Path(self.args.work_dir),
            report_dir=self.args.report_dir,
            invocation_dir=self.reserve_invocation_dir(),
        )

    def _write_target_report(
        self,
        target: str,
        metrics: SynthMetrics,
        baseline_metrics: SynthMetrics | None,
        baseline_ref: str | None,
        implementation: ImplementationReport | None = None,
    ) -> ImplementationReport:
        """Write per-target JSON report to report_dir."""
        implementation = implementation or self._implementation_report(
            target, metrics, baseline_metrics, baseline_ref
        )
        report_dir = self.args.report_dir
        if report_dir is None:
            return implementation
        report_dir.mkdir(parents=True, exist_ok=True)

        report = _build_report_dict(
            self.name,
            target,
            metrics,
            baseline_metrics,
            baseline_ref,
            self._compute_delta_pct,
            eda_tool=self._eda_tool,
        )
        report["passed"] = implementation.passed
        pair = target_pair_for_candidate(getattr(self, "_target_pairs", ()), target)
        report["baseline_target"] = pair.baseline
        report["candidate_target"] = pair.candidate
        report["run_evidence"] = metrics.run_evidence or None
        report["baseline_run_evidence"] = (
            baseline_metrics.run_evidence if baseline_metrics else None
        )
        run_id = os.environ.get("BOOLEY_RUN_ID", "")
        if run_id:
            report["run_id"] = run_id
        safe_target = synth_target_report_slug(target)
        report_path = report_dir / f"synth_{safe_target}.json"
        # The run.log pointer lived only in the stdout ``log:`` line, which the
        # MCP layer tail-truncates; ``reports`` was already here but not the log
        # itself. Both now travel together in the shared ``artifacts`` shape,
        # alongside this file's own path.
        preserved = self._snapshot_report_artifacts(report_dir, safe_target, metrics)
        report["artifacts"] = {
            "report": posix_relpath(report_path, self.args.work_dir),
            **preserved,
        }
        published = self._publisher().publish_report(implementation, report)
        return ImplementationReport(published.payload["implementation"])

    def _snapshot_report_artifacts(
        self, report_dir: Path, safe_target: str, metrics: SynthMetrics
    ) -> dict[str, Any]:
        """Copy mutable log/timing evidence beside this invocation's report."""
        root = Path(self.args.work_dir)
        destination = report_dir / "artifacts" / f"synth_{safe_target}"
        result: dict[str, Any] = {
            **({"log": metrics.log_path} if metrics.log_path else {}),
            **({"dirs": dict(metrics.dirs)} if metrics.dirs else {}),
        }
        if metrics.log_path:
            source = root / metrics.log_path
            if source.is_file():
                destination.mkdir(parents=True, exist_ok=True)
                copied = destination / "run.log"
                shutil.copy2(source, copied)
                result["log"] = posix_relpath(copied, root)
        timing = metrics.dirs.get("timing") if metrics.dirs else None
        if timing:
            source_dir = root / timing
            if source_dir.is_dir():
                copied_dir = destination / "timing"
                shutil.copytree(source_dir, copied_dir, dirs_exist_ok=True)
                result.setdefault("dirs", {})["timing"] = posix_relpath(copied_dir, root)
        return result

    def _write_progress_report(
        self,
        targets: list[str],
        current_results: dict[str, SynthMetrics],
        baseline_results: dict[str, SynthMetrics],
        *,
        phase: str,
        baseline_ref: str | None = None,
        complete: bool = False,
    ) -> None:
        """Checkpoint a long matrix after every target and phase transition."""
        reports = getattr(self, "_implementation_reports", {})
        progress = ImplementationProgress(
            flow=self.name,
            run_id=os.environ.get("BOOLEY_RUN_ID", ""),
            targets=tuple(targets),
            completed_targets=tuple(current_results),
            baseline_completed_targets=tuple(baseline_results),
            phase=phase,
            complete=complete,
            baseline_ref=baseline_ref,
            reports=reports,
        )
        self._publisher().publish_progress(progress)

    def _persist_target_outcome(
        self,
        target: str,
        metrics: SynthMetrics,
        baseline_metrics: SynthMetrics | None,
        baseline_ref: str | None,
    ) -> None:
        """Durably record one terminal target before the next one starts."""
        implementation = self._implementation_report(
            target, metrics, baseline_metrics, baseline_ref
        )
        implementation = self._write_target_report(
            target, metrics, baseline_metrics, baseline_ref, implementation
        )
        self._implementation_reports[target] = implementation
        if implementation.grade != "error":
            self._set_config_criterion(
                target,
                metrics,
                baseline_metrics,
                baseline_ref,
                implementation=implementation,
            )
            if self.state._file_path is not None:
                self.state.save()

    # -- Main run logic -------------------------------------------------------

    def _resolve_run_policy(self, enabled: bool) -> str | None:
        """Latch the per-run policy state, or return the message that blocks the run.

        The ``fail_on_timing_violation`` gate (F-37) must be settled before a
        half-hour synthesis starts: a mistyped knob has to fail now, not while
        formatting the report at the end.
        """
        if not enabled:
            return "synth is disabled ([flows.synth].enabled = false)."
        try:
            self._timing_violation_is_fatal = _fail_on_timing_violation(self.args.work_dir)
        except BoundaryError as exc:
            return f"synth: {exc}"
        return None

    def _apply_ticket_baseline(self, targets: list[str]) -> str | None:
        """Default relative ticket criteria to their immutable baseline SHA."""
        baseline, full_sha, error = resolve_ticket_baseline(
            self.state.criteria,
            "synthesis_ok_",
            targets,
            self.args.baseline,
            Path(self.args.work_dir),
            "synth",
        )
        self.args.baseline = baseline
        self._baseline_full_sha = full_sha
        return error

    def _run(self) -> McpToolResult:
        """Execute synthesis for all targets, optionally comparing to baseline."""
        from booley.fusesoc import fusesoc_registry

        # Populated by _run_baseline_configs when a stealth-cores self-compare is
        # detected; read by _aggregate_results. Reset per run so a stale value
        # from a reused instance never leaks into a fresh invocation.
        self._baseline_selfcompare_msg: str | None = None
        self._baseline_full_sha: str | None = None
        self._implementation_reports: dict[str, ImplementationReport] = {}

        targets = fusesoc_registry.resolve_target_selection(
            self.args.target,
            self.args.work_dir,
        )
        if not targets:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "synth: no Target selected. Pass --target <name> "
                    "(bare name if unambiguous, else vlnv#name)."
                ),
            )
        preparation_error = self._prepare_target_pairs(targets)
        if preparation_error is not None:
            return preparation_error
        if self.args.dry_run:
            return self._dry_run(targets)
        self.reserve_invocation_dir()
        self._write_progress_report(targets, {}, {}, phase="starting")
        baseline_results, short_sha = self._run_baseline_configs(self._target_pairs)
        if isinstance(baseline_results, McpToolResult):
            return baseline_results
        current_results = self._run_current_targets(targets, baseline_results, short_sha)
        self._discard_stale_selfcompare(targets, current_results, baseline_results)
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

    def _prepare_target_pairs(self, targets: list[str]) -> McpToolResult | None:
        baseline_error = self._apply_ticket_baseline(targets)
        comparison_error: str | None = None
        try:
            self._target_pairs = target_pairs_for_candidates(
                self.state.criteria,
                "synthesis_ok_",
                targets,
                contract=getattr(self, "_target_contract", None),
                project_root=self.args.work_dir,
                flow="synth",
            )
        except ImplementationComparisonError as exc:
            comparison_error = f"synth: {exc}"
        if baseline_error is not None or comparison_error is not None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=baseline_error or comparison_error or "synth comparison error",
            )
        # Resolve enablement once per run, then reuse it in each config and the
        # dry-run report.
        config_error = self._resolve_run_policy(self._flow_enabled())
        if config_error is not None:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=config_error)
        return None

    def _run_current_targets(
        self,
        targets: list[str],
        baseline_results: dict[str, SynthMetrics],
        short_sha: str | None,
    ) -> dict[str, SynthMetrics]:
        current_results: dict[str, SynthMetrics] = {}
        for tgt in targets:
            metrics, _output = self._run_single_config(tgt)
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
                    self._format_config_line(
                        tgt,
                        metrics,
                        baseline_results.get(tgt),
                    )
                )
        return current_results

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

    def _dry_run(self, targets: list[str]) -> McpToolResult:
        """Print the boundary command + the spec it is rendered from.

        The executed command is a bare ``make -C <rel>`` (ADR 0037 §8); the
        recipe knobs live in the scripts the configure half renders into that
        dir, so the preview also shows the resolved run_yosys_syn spec argv —
        the validated carrier of every flag. Building it requires the resolved
        RTL, so this resolves every target through FuseSoC (one ``fusesoc run
        --setup`` each) — an honest preview, not a cheap ``.core`` read. The
        per-target label keeps the target name visible.

        When resolution itself is unavailable (no ``fusesoc`` outside the
        sandbox), dry-run must still succeed — the contract says dry-run needs
        no EDA tools — so it degrades to the cheap ``setup_command`` preview
        the make-driven built-ins use.
        """
        from .. import edam
        from ..edam import work_root_for

        lines = []
        for tgt in targets:
            try:
                cmd = self._build_synth_cmd(tgt)
                rel = edam.relpath_for_make(self._synth_build_dir(tgt), self.args.work_dir)
                lines.append(
                    f"[synth] dry-run ({tgt}): make -C {rel}"
                    f"  # rendered at configure time from: {' '.join(cmd)}",
                )
            except BoundaryError as exc:
                # A wrong-typed config knob fails the preview loudly — dry-run
                # exists to vet the command, so hiding a config error here would
                # defeat its purpose.
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=f"[synth] dry-run ({tgt}): config error: {exc}",
                )
            except fusesoc_registry.TargetResolutionError as exc:
                setup_cmd = fusesoc_registry.setup_command(
                    tgt,
                    project_root=self.args.work_dir,
                    build_root=work_root_for(self.args.work_dir, self.name, tgt),
                )
                lines.append(
                    f"[synth] dry-run ({tgt}): {' '.join(setup_cmd)}"
                    " && make -C <configured synth build dir>"
                    f"  # full preview unavailable here: {exc}",
                )
        return McpToolResult(exit_code=EXIT_SUCCESS, report_text="\n".join(lines))

    def _run_baseline_configs(
        self,
        pairs: Sequence[TargetPair | str],
    ) -> tuple[dict[str, SynthMetrics] | McpToolResult, str | None]:
        """Synthesize paired baseline Targets in an ephemeral worktree."""
        baseline_ref = self.args.baseline
        if not baseline_ref:
            return {}, None
        pairs = self._normalize_target_pairs(pairs)
        targets = [pair.candidate for pair in pairs]
        project_root = Path(self.args.work_dir)
        short_sha = git_short_sha(baseline_ref, project_root)
        full_sha = git_full_sha(str(baseline_ref), project_root)
        if full_sha is not None:
            self._baseline_full_sha = full_sha
        try:
            with baseline_worktree(project_root, baseline_ref) as wt:
                self.args.work_dir = wt
                self._project_root = project_root
                if all(pair.baseline == pair.candidate for pair in pairs):
                    self._baseline_selfcompare_msg = _baseline_self_compare_warning(
                        project_root, wt
                    )
                try:
                    result = self._execute_baseline_pairs(pairs, targets, project_root, short_sha)
                finally:
                    self.args.work_dir = project_root
                    self._project_root = None
        except BaselineWorktreeError as exc:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"synth: {exc}",
            ), None
        return result, short_sha

    @staticmethod
    def _normalize_target_pairs(pairs: Sequence[TargetPair | str]) -> tuple[TargetPair, ...]:
        return tuple(
            pair if isinstance(pair, TargetPair) else TargetPair(pair, pair) for pair in pairs
        )

    def _execute_baseline_pairs(
        self,
        pairs: Sequence[TargetPair],
        targets: list[str],
        project_root: Path,
        short_sha: str,
    ) -> dict[str, SynthMetrics]:
        baseline_results: dict[str, SynthMetrics] = {}
        executed: dict[str, SynthMetrics] = {}
        for pair in pairs:
            if pair.baseline not in executed:
                metrics, output = self._run_single_config(pair.baseline)
                metrics.log_path = self._persist_baseline_log(pair.baseline, output, project_root)
                executed[pair.baseline] = metrics
            metrics = copy.deepcopy(executed[pair.baseline])
            baseline_results[pair.candidate] = metrics
            self._write_progress_report(
                targets, {}, baseline_results, phase="baseline", baseline_ref=short_sha
            )
        return baseline_results

    def _target_report_lines(
        self,
        tgt: str,
        cur: SynthMetrics,
        base: SynthMetrics | None,
    ) -> list[str]:
        """Render the human-facing report lines for one target."""
        lines = [self._format_config_line(tgt, cur, base)]
        if cur.passed:
            lines.append(self._format_qor_line(tgt, cur))
            if cur.estimated_fmax_mhz is not None:
                lines.append(
                    f"[synth] {tgt}: WARNING -- estimated Fmax is probably inaccurate; "
                    "logical mode excludes placement and wire delays"
                )
        elif cur.has_metrics and not cur.ppa_complete:
            lines.append(
                f"[synth] {tgt}: PARTIAL -- Yosys emitted intermediate "
                "area/cell data, but the synthesis/PPA run did not complete; "
                "do not use these numbers as final QoR"
            )
        if not cur.structural_checks_complete and (
            cur.latches or cur.comb_loops or cur.multi_driven or cur.process_count
        ):
            lines.append(
                f"[synth] {tgt}: INCONCLUSIVE -- structural counts were emitted "
                "before the final synthesis checks completed"
            )
        if _is_io_bound_critical(cur):
            lines.append(self._format_io_bound_line(tgt, cur))
        if cur.has_critical:
            lines.append(self._format_critical_line(tgt, cur))
        if cur.failure_output:
            lines.append(self._format_failure_output(tgt, cur))
        if cur.log_path:
            lines.append(f"[synth] {tgt}: log: {cur.log_path}")
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
        base: SynthMetrics | None,
    ) -> tuple[list[str], str | None, str | None]:
        """Return report lines, timing violation, and failure summary for one target."""
        failure = None if cur.passed else self._format_failure_summary(tgt, cur)
        return self._target_report_lines(tgt, cur, base), self._timing_violation(tgt, cur), failure

    @staticmethod
    def _comparison_error_lines(
        reports: dict[str, ImplementationReport],
    ) -> tuple[list[str], list[str]]:
        lines: list[str] = []
        failures: list[str] = []
        for target, report in reports.items():
            comparison = report.canonical.get("comparison")
            errors = comparison.get("basis_errors", []) if isinstance(comparison, dict) else []
            for message in errors:
                if str(message).startswith("baseline infrastructure error:"):
                    continue
                lines.append(f"[synth] comparison {target}: ERROR -- {message}")
                failures.append(f"comparison {target}: invalid evidence")
        return lines, failures

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

    def _aggregate_results(
        self,
        targets: list[str],
        current_results: dict[str, SynthMetrics],
        baseline_results: dict[str, SynthMetrics],
        short_sha: str | None,
    ) -> McpToolResult:
        """Build the final synthesis report from policy-resolved target evidence."""
        implementation_reports = {
            target: getattr(self, "_implementation_reports", {}).get(target)
            or self._implementation_report(
                target,
                current_results[target],
                baseline_results.get(target),
                short_sha,
            )
            for target in targets
        }
        implementation_aggregate = build_implementation_aggregate(
            implementation_reports,
            baseline_ref=getattr(self, "_baseline_full_sha", None) or short_sha,
        )
        stdout_lines, failed_targets, selfcompare_msg = self._aggregate_prefix(
            short_sha, baseline_results, implementation_reports
        )
        violated = self._append_target_blocks(
            stdout_lines, failed_targets, targets, current_results, baseline_results
        )
        self._append_fatal_timing_failure(failed_targets, violated)
        stdout_lines.extend(["", _result_line(failed_targets, selfcompare_msg, violated)])
        report_text = "\n".join(stdout_lines)
        print(report_text)
        detail = self._implementation_aggregate_detail(
            targets,
            current_results,
            baseline_results,
            short_sha,
            implementation_reports,
            implementation_aggregate,
        )
        return McpToolResult(
            exit_code=implementation_aggregate.exit_code,
            report_text=report_text,
            display_lines=_first_valid_display(targets, current_results),
            detail=detail,
        )

    def _aggregate_prefix(
        self,
        short_sha: str | None,
        baseline_results: dict[str, SynthMetrics],
        reports: dict[str, ImplementationReport],
    ) -> tuple[list[str], list[str], str | None]:
        lines: list[str] = []
        failures: list[str] = []
        if self.args.baseline and short_sha:
            lines.append(f"[synth] baseline: {short_sha}")
        for target, metrics in baseline_results.items():
            if metrics.infra_error:
                lines.append(f"[synth] baseline {target}: ERROR -- {metrics.infra_error}")
                failures.append(f"baseline {target}: infrastructure error")
        comparison_lines, comparison_failures = self._comparison_error_lines(reports)
        lines.extend(comparison_lines)
        failures.extend(comparison_failures)
        selfcompare_msg = getattr(self, "_baseline_selfcompare_msg", None)
        if selfcompare_msg:
            lines.append(f"[synth] WARNING -- {selfcompare_msg}")
        return lines, failures, selfcompare_msg

    def _append_target_blocks(
        self,
        lines: list[str],
        failures: list[str],
        targets: list[str],
        current_results: dict[str, SynthMetrics],
        baseline_results: dict[str, SynthMetrics],
    ) -> list[str]:
        violated: list[str] = []
        for target in targets:
            current = current_results[target]
            target_lines, violation, failure = self._emit_target_block(
                target, current, baseline_results.get(target)
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
    ) -> str:
        """Format one target's area/timing/elapsed line."""
        area_str = self._fmt_area(cur.area_kge)
        timing_str = (
            "logical estimate"
            if cur.synth_mode is SynthMode.LOGICAL
            else self._fmt_timing(_worst_critical_path_ps(cur))
        )
        elapsed_str = f"{cur.elapsed_s:.1f}s"
        if base:
            area_delta = self._compute_delta_pct(cur.area_kge, base.area_kge)
            timing_delta = self._compute_delta_pct(
                _worst_critical_path_ps(cur),
                _worst_critical_path_ps(base),
            )
            return (
                f"[synth] {tgt:<10}"
                f"{area_str} {self._fmt_delta(area_delta):>20}   "
                f"{timing_str} {self._fmt_delta(timing_delta):>20}   "
                f" {elapsed_str}{self._format_status_suffix(cur)}"
            )
        return (
            f"[synth] {tgt:<10}"
            f"{area_str}   {timing_str}    {elapsed_str}"
            f"{self._format_status_suffix(cur)}"
        )

    @staticmethod
    def _format_failure_output(tgt: str, cur: SynthMetrics) -> str:
        """Render the captured subprocess error tail under the config line."""
        indented = "\n".join(f"    {ln}" for ln in cur.failure_output.splitlines())
        return f"[synth] {tgt}: subprocess output:\n{indented}"

    @staticmethod
    def _format_qor_line(tgt: str, cur: SynthMetrics) -> str:
        """One-line QoR summary (cells / area / timing) for a passing config.

        These numbers otherwise land only in ``util/syn/`` report files; a
        concise summary here keeps the happy-path output actionable.
        """
        parts: list[str] = []
        if cur.cells is not None:
            parts.append(f"{cur.cells:,} cells")
        if cur.area_kge is not None:
            parts.append(f"{cur.area_kge:.1f} kGE")
        if cur.estimated_fmax_mhz is not None:
            parts.append(f"estimated Fmax {cur.estimated_fmax_mhz:.0f} MHz")
        # Fmax/critical path are per-clock; show the timing-worst clock here,
        # tagged with its name only when the design has more than one clock (so
        # single-clock output is unchanged). Full breakdown lives in the report.
        worst = worst_clock(cur.per_clock)
        tag = f" [{worst.clock}]" if worst and len(cur.per_clock) > 1 else ""
        if worst is not None and worst.critical_path_ps is not None:
            parts.append(f"crit path {worst.critical_path_ps:,.0f} ps{tag}")
        if worst is not None and worst.fmax_mhz is not None:
            parts.append(f"Fmax {worst.fmax_mhz:.0f} MHz{tag}")
        if cur.wns_ns is not None:
            parts.append(f"setup slack {cur.wns_ns:+.3f} ns")
        worst_hold = _worst_hold_slack_ns(cur)
        if worst_hold is not None:
            parts.append(f"hold slack {worst_hold:+.3f} ns")
        if cur.reg2reg_fmax_mhz is not None:
            parts.append(f"reg2reg Fmax {cur.reg2reg_fmax_mhz:.0f} MHz")
        elif cur.reg2reg_slack_ns is not None:
            parts.append(f"reg2reg slack {cur.reg2reg_slack_ns:+.3f} ns")
        return f"[synth] {tgt}: QoR -- {', '.join(parts) or 'no metrics'}"

    @staticmethod
    def _format_io_bound_line(tgt: str, cur: SynthMetrics) -> str:
        """Advisory: the worst path is I/O-bound, so period_ps is the wrong lever."""
        worst = cur.wns_ns
        r2r = cur.reg2reg_slack_ns
        return (
            f"[synth] {tgt}: NOTE -- critical path is I/O-bound "
            f"(worst slack {worst:+.3f} ns vs reg2reg {r2r:+.3f} ns); "
            "period_ps won't move it — declare I/O delays / false-path the port "
            "via the [flows.synth.timing].sdc knob"
        )

    @staticmethod
    def _format_critical_line(tgt: str, cur: SynthMetrics) -> str:
        """Format a CRITICAL conditions warning line."""
        parts = []
        if cur.unexpected_latches:
            parts.append(f"{cur.unexpected_latches} latches")
        if cur.comb_loops:
            parts.append(f"{cur.comb_loops} comb loop")
        if cur.multi_driven:
            parts.append(f"{cur.multi_driven} multi-driven")
        return f"[synth] {tgt}: CRITICAL -- {', '.join(parts)}"

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
        reason = "failed"
        if cur.timed_out:
            reason = "timeout"
        elif cur.termination == "oom":
            reason = "OOM"
        elif cur.termination == "resource_killed":
            reason = "resource-killed"
        elif cur.infra_error:
            reason = "infrastructure error"
        elif cur.returncode != 0 and not cur.has_metrics:
            reason = f"rc={cur.returncode}, no metrics"
        elif cur.returncode != 0:
            reason = f"rc={cur.returncode}"
        elif not cur.has_metrics:
            reason = "no metrics"
        elif cur.has_critical:
            reason = "critical conditions"
        return f"{tgt}: {reason}"

    def _set_config_criterion(
        self,
        tgt: str,
        cur: SynthMetrics,
        base: SynthMetrics | None,
        baseline_ref: str | None,
        *,
        implementation: ImplementationReport | None = None,
    ) -> None:
        """Set the synthesis_ok criterion for one target."""
        pair = target_pair_for_candidate(getattr(self, "_target_pairs", ()), tgt)
        detail: dict[str, Any] = {
            **cur.qor_detail(),
            **cur.structural_detail(),
            "process_count": cur.process_count,
            **cur.status_detail(),
            RECIPE_FINGERPRINT_DETAIL: cur.recipe_fingerprint or None,
            RECIPE_SNAPSHOT_DETAIL: cur.recipe_snapshot or None,
            RUN_EVIDENCE_DETAIL: cur.run_evidence or None,
            BASELINE_TARGET_DETAIL: pair.baseline,
            CANDIDATE_TARGET_DETAIL: pair.candidate,
            "_metric_map": dict(_CRITERION_METRIC_MAP),
            "_min_allowed": list(_CRITERION_MIN_ALLOWED),
        }
        if base:
            _add_baseline_criterion_detail(detail, base)
        if baseline_ref:
            detail[BASELINE_REF_DETAIL] = getattr(self, "_baseline_full_sha", None) or baseline_ref
        implementation = implementation or self._implementation_report(
            tgt, cur, base, baseline_ref
        )
        self.set_criterion(
            f"synthesis_ok_{tgt}",
            implementation.passed,
            detail=implementation.envelope(detail),
            source_target=tgt,
        )


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


_ERROR_LINE_RE = re.compile(
    r"\b(error|fatal|not found|no such|permission denied)\b", re.IGNORECASE
)


def _error_excerpt(output: str, max_lines: int = 12) -> str:
    """Pull the actionable error out of swallowed synth subprocess output.

    Prefers lines that look like diagnostics (the ``run_yosys_syn`` /
    ``syn_core`` ``sys.exit("ERROR: ...")`` guards, yosys/sv2v errors); falls
    back to the last non-empty lines so even a bare crash surfaces *something*.
    Bounded to ``max_lines`` so a giant log tail can't swamp the report.
    """
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return ""
    err_lines = [ln for ln in lines if _ERROR_LINE_RE.search(ln)]
    chosen = err_lines or lines[-max_lines:]
    return "\n".join(chosen[-max_lines:])


if __name__ == "__main__":
    AsicSynthesizeFlow().cli()
