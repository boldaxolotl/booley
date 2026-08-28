"""Synthesis coordinator — sv2v/Yosys script generation and timing config.

This module builds the sv2v + Yosys synthesis scripts and owns the shared
configuration used by the OpenROAD physical path. The remaining concerns were
split into sibling leaf modules:

* :mod:`booley.yosys.syn_config`     — project-context path constants
* :mod:`booley.yosys.syn_discovery`  — EDA tool + liberty discovery
* :mod:`booley.yosys.syn_parse`      — config-param + area result parsing

Their public names are re-exported below so existing importers of ``syn_core``
keep working unchanged.
"""

from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path
from typing import NamedTuple

from booley.core.boundary import (
    BoundaryError,
    is_str_list,
    require_finite_number,
    require_opt_str,
)
from booley.synthesis.mode import SYNTH_MODE_CHOICES, SynthMode
from booley.targets.parameter_integrity import enabled_define_names

# --- re-exported for backward compatibility (moved to sibling leaf modules) ---
from booley.yosys.syn_config import (
    PROJECT_ROOT,
    RTL_DIR,
    SCRIPT_DIR,
    SYN_DIR,
)
from booley.yosys.syn_discovery import (
    DEFAULT_LIB_DIR,
    DEFAULT_LIBERTY,
    resolve_liberty,
)
from booley.yosys.syn_parse import (
    NAND2_AREA_UM2,
    area_to_kge,
    parse_area_from_stat,
    parse_params,
)

# Public surface of this facade (defined here + re-exported above). Listing the
# re-exports keeps them from tripping the unused-import (F401) linter while
# documenting the module's stable API.
__all__ = [
    "CHFORMAL_REMOVE",
    "DEFAULT_FRONTEND",
    "DEFAULT_LIBERTY",
    "DEFAULT_LIB_DIR",
    "DEFAULT_STA_INPUT_DELAY_PCT",
    "DEFAULT_STA_OUTPUT_DELAY_PCT",
    "DEFAULT_STA_PERIOD_PS",
    "DEFAULT_STA_UTILIZATION_PCT",
    "FORMAL_CELL_TYPES",
    "FRONTEND_CHOICES",
    "NAND2_AREA_UM2",
    "PROJECT_ROOT",
    "RTL_DIR",
    "SCRIPT_DIR",
    "SV2V_OUTPUT_NAME",
    "SYNTH_MODE_CHOICES",
    "SYN_DIR",
    "StaTimingConfig",
    "area_to_kge",
    "detect_clock_port",
    "effective_params_filename",
    "effective_period_ps",
    "emit_timing_markers",
    "enabled_define_names",
    "parse_abc_mapped_delay_ps",
    "parse_area_from_stat",
    "parse_effective_parameters",
    "parse_params",
    "parse_reg2reg_slack",
    "parse_sdc_clock_periods_ps",
    "parse_sta_clock_period_ps",
    "parse_sta_worst_slack",
    "print_overall_fmax",
    "print_reg2reg_fmax",
    "read_user_sdc_text",
    "resolve_frontend",
    "resolve_liberty",
    "resolve_slang_options",
    "scan_synth_logs",
    "sv2v_argv",
    "synth_timing_config",
    "write_sta_sdc",
]


_ABC_MAPPED_DELAY_RE = re.compile(
    r"^ABC:\s+netlist\b.*?\bdelay\s*=\s*([0-9]+(?:\.[0-9]+)?)\b.*?\blev\s*=",
    re.MULTILINE,
)


def parse_abc_mapped_delay_ps(output: str) -> float | None:
    """Return the slowest positive delay from a final liberty-mapped ABC log."""
    delays_ps = [float(value) for value in _ABC_MAPPED_DELAY_RE.findall(output)]
    positive_delays_ps = [delay for delay in delays_ps if delay > 0]
    return max(positive_delays_ps) if positive_delays_ps else None


# ============================================================================
# Timing configuration
# ============================================================================


class StaTimingConfig(NamedTuple):
    """Timing and physical-synthesis setup for the built-in backend."""

    mode: SynthMode
    clock: str | None
    period_ps: float
    input_delay_pct: float
    output_delay_pct: float
    # STA constraint SDC files from the Target's ``file_type: SDC`` fileset
    # (ADR 0029), concatenated in fileset order (last-wins). Empty tuple = no
    # authored SDC, so ``write_sta_sdc`` emits its full generated default block.
    sdc: tuple[Path, ...] = ()
    # Physical-mode knobs: floorplan utilization and whether the setup-only
    # ``repair_timing`` pass runs.
    utilization_pct: float = 40.0
    repair_timing: bool = True
    placement_density: float | None = None
    repair_hold: bool = False
    gate_cloning: bool = False
    setup_margin_ns: float = 0.0
    repair_tns_percent: float | None = None


DEFAULT_STA_PERIOD_PS = 4000.0
DEFAULT_STA_INPUT_DELAY_PCT = 30.0
DEFAULT_STA_OUTPUT_DELAY_PCT = 70.0
DEFAULT_STA_UTILIZATION_PCT = 40.0
_CLOCK_CANDIDATES = ("clk_i", "clk", "clock", "i_clk", "aclk")
_STA_SLACK_RE = re.compile(r"STA_WORST_SLACK_NS:\s*([-+]?\d+(?:\.\d+)?)")
_REG2REG_SLACK_RE = re.compile(r"STA_REG2REG_SLACK_NS:\s*([-+]?\d+(?:\.\d+)?)")
_STA_CLOCK_PERIOD_RE = re.compile(r"STA_CLOCK_PERIOD_NS:\s*([-+]?\d+(?:\.\d+)?)")
# Per-clock timing marker emitted once per clock by ``perclock_timing_tcl``.
# ``wns_ns``/``whs_ns`` are ``NA`` when that clock has no setup/hold path.
_PERCLOCK_RE = re.compile(
    r"STA_PERCLOCK:\s*name=(?P<name>\S+)\s+period_ns=(?P<period>[-+]?\d+(?:\.\d+)?)"
    r"\s+wns_ns=(?P<wns>NA|[-+]?\d+(?:\.\d+)?)"
    r"\s+whs_ns=(?P<whs>NA|[-+]?\d+(?:\.\d+)?)"
)
# create_clock -name <clk> — recover authored clock names so a multi-clock SDC
# (no single detected port) can still drive the flow. Same #-comment guard as
# the period regex above.
_CREATE_CLOCK_NAME_RE = re.compile(r"(?m)^[^\n#]*?\bcreate_clock\b[^\n]*?-name\s+([^\s\]\}]+)")

# Content-scan detectors for an *authored* Target SDC (ADR 0029 decision 5):
# when the SDC declares its own clock / I/O delays, ``write_sta_sdc`` suppresses
# the matching generated default. Anchored so a ``#``-commented line never
# counts (``^[^\n#]*?`` cannot cross a ``#`` before the keyword).
_SDC_CREATE_CLOCK_RE = re.compile(r"(?m)^[^\n#]*?\bcreate_clock\b")
_SDC_INPUT_DELAY_RE = re.compile(r"(?m)^[^\n#]*?\bset_input_delay\b")
_SDC_OUTPUT_DELAY_RE = re.compile(r"(?m)^[^\n#]*?\bset_output_delay\b")
# ``create_clock ... -period <value>`` — the value is in the SDC time unit (ns
# by default); ``parse_sdc_clock_periods_ps`` converts to ps. Same comment guard.
_CREATE_CLOCK_PERIOD_RE = re.compile(
    r"(?m)^[^\n#]*?\bcreate_clock\b[^\n]*?-period\s+"
    r"([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)"
)


def reg2reg_timing_tcl(report_path: str | None = None) -> str:
    """Tcl that prints the worst register-to-register setup slack.

    Physical mode emits this alongside the overall worst path so the internal
    (reg->reg) critical path is always visible. The overall worst
    path is the single most-negative-slack path in the design; with a non-zero
    ``set_input_delay``/``set_output_delay`` budget an I/O path routinely wins
    it, hiding the true reg->reg Fmax.  Restricting ``find_timing_paths`` to
    ``all_registers`` on both ends reports the internal path directly.

    ``report_path``, when given, also dumps the *full* reg->reg path detail
    (gate-by-gate arrival table) there.  Without it the reg->reg Fmax number
    was surfaced but the path behind it was not: ``overall.rpt`` holds only the
    single worst overall path, which on an I/O-bound design is a pad-to-pad
    feed-through — useless for deciding what RTL to pipeline.  Digging the
    reg->reg path out then meant re-running timing analysis by hand.

    Wrapped in ``catch``: a purely combinational design (no registers) or an
    timer without ``all_registers`` degrades to *no* marker rather than
    failing the whole timing run — same warn-and-degrade contract as the rest
    of this module.  The report write sits under its own ``catch`` inside the
    same guard, so an engine that can find the paths but chokes on the report
    still yields the marker.
    """
    report_block = ""
    if report_path is not None:
        report_block = (
            "  catch {report_checks -path_delay max -sort_by_slack "
            "-group_count 1 -from [all_registers] -to [all_registers] "
            f"-format full > {{{report_path}}}}}\n"
        )
    return (
        "if {![catch {set _r2r [find_timing_paths -path_delay max "
        "-sort_by_slack -group_count 1 -from [all_registers] "
        "-to [all_registers]]}] && [llength $_r2r] > 0} {\n"
        "  foreach _p $_r2r {\n"
        '    puts [format "STA_REG2REG_SLACK_NS: %.6f" [get_property $_p slack]]\n'
        "    break\n"
        "  }\n"
        f"{report_block}"
        "}\n"
    )


def parse_reg2reg_slack(source: str | Path) -> float | None:
    """Parse the worst reg->reg setup slack (ns) from STA stdout, or None."""
    if isinstance(source, Path):
        if not source.exists():
            return None
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        text = source
    match = _REG2REG_SLACK_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def print_reg2reg_fmax(stdout: str, period_ps: float) -> bool:
    """Emit derived reg->reg critical-path (ps) + Fmax (MHz) markers.

    The Tcl prints only the raw slack (it has no clean handle on the period);
    the period lives here in Python, exactly like the overall critical path.
    No-op when the slack marker is absent (combinational design / degraded STA).

    Returns True iff the reg->reg slack marker was present and the derived
    markers were emitted — callers gate the "no timing at all" degrade on this
    so a false-pathed overall worst path never buries the internal Fmax.
    """
    slack_ns = parse_reg2reg_slack(stdout)
    if slack_ns is None:
        return False
    critical_path_ps = period_ps - (slack_ns * 1000.0)
    print(f"STA_REG2REG_CRITICAL_PATH_PS: {critical_path_ps:.3f}")
    if critical_path_ps > 0:
        print(f"STA_REG2REG_FMAX_MHZ: {1_000_000.0 / critical_path_ps:.3f}")
    return True


def print_overall_fmax(slack_ns: float, period_ps: float) -> None:
    """Emit the overall worst-path slack + derived critical-path/Fmax markers."""
    critical_path_ps = period_ps - (slack_ns * 1000.0)
    print(f"STA_WORST_SLACK_NS: {slack_ns:.6f}")
    print(f"STA_CRITICAL_PATH_PS: {critical_path_ps:.3f}")
    if critical_path_ps > 0:
        print(f"STA_FMAX_MHZ: {1_000_000.0 / critical_path_ps:.3f}")


def perclock_timing_tcl() -> str:
    """Tcl that prints one ``STA_PERCLOCK`` marker per clock in the design.

    Fmax and critical-path delay are inherently per-clock, so physical mode
    iterates ``[all_clocks]`` and, for each, reports its own worst setup
    (``-path_delay max``) and hold (``-path_delay min``) slack against paths
    *ending* in that clock domain (``-to $clk``). The marker carries the clock's
    name, its constrained period (ns), and both slacks (ns) — Python derives the
    per-clock critical path/Fmax from period and setup slack (:func:`booley.dev_support
    .clock_timing.derive_critical_path_and_fmax`), exactly as the overall path
    does. A clock with no setup/hold path emits ``NA`` for that slack.

    Every step is wrapped in ``catch``: a build lacking ``all_clocks`` /
    ``-to <clock>`` support degrades to *no* per-clock markers (the aggregate
    worst-slack and reg->reg markers still stand) rather than failing the run —
    the same warn-and-degrade contract as the rest of this module.

    Portability: the OpenSTA Tcl embedded by this pinned OpenROAD build returns
    plain Tcl lists but does not define ``foreach_in_collection``. Install a
    small ``foreach`` shim when the command is absent so per-clock reporting
    cannot abort an otherwise-complete physical run.
    """
    return (
        "if {[llength [info commands foreach_in_collection]] == 0} {\n"
        "  proc foreach_in_collection {_var _coll _body} {\n"
        "    upvar 1 $_var _v\n"
        "    foreach _v $_coll { uplevel 1 $_body }\n"
        "  }\n"
        "}\n"
        "if {![catch {set _clks [all_clocks]}]} {\n"
        "  foreach_in_collection _clk $_clks {\n"
        "    set _cn [get_property $_clk name]\n"
        "    set _per [get_property $_clk period]\n"
        '    set _wns "NA"\n'
        "    if {![catch {set _sp [find_timing_paths -path_delay max "
        "-sort_by_slack -group_count 1 -to $_clk]}] && [llength $_sp] > 0} {\n"
        '      set _wns [format "%.6f" [get_property [lindex $_sp 0] slack]]\n'
        "    }\n"
        '    set _whs "NA"\n'
        "    if {![catch {set _hp [find_timing_paths -path_delay min "
        "-sort_by_slack -group_count 1 -to $_clk]}] && [llength $_hp] > 0} {\n"
        '      set _whs [format "%.6f" [get_property [lindex $_hp 0] slack]]\n'
        "    }\n"
        '    puts [format "STA_PERCLOCK: name=%s period_ns=%.6f wns_ns=%s '
        'whs_ns=%s" $_cn $_per $_wns $_whs]\n'
        "  }\n"
        "}\n"
    )


def parse_perclock(text: str) -> dict[str, dict[str, float | None]]:
    """Parse ``STA_PERCLOCK`` markers → ``{clk: {period_ns, wns_ns, whs_ns}}``.

    When a clock is reported more than once (e.g. the raw marker plus a
    re-emitted copy, or two STA passes), the most pessimistic slack wins — min
    ``wns_ns``/``whs_ns`` — matching :func:`_parse_worst_slack`'s min-across
    convention. ``NA`` slacks become ``None`` and never displace a real value.
    """
    rows: dict[str, dict[str, float | None]] = {}
    for match in _PERCLOCK_RE.finditer(text):
        name = match.group("name")
        period = _na_float(match.group("period"))
        wns = _na_float(match.group("wns"))
        whs = _na_float(match.group("whs"))
        row = rows.setdefault(name, {"period_ns": period, "wns_ns": wns, "whs_ns": whs})
        if period is not None:
            row["period_ns"] = period
        row["wns_ns"] = _min_opt(row["wns_ns"], wns)
        row["whs_ns"] = _min_opt(row["whs_ns"], whs)
    return rows


def emit_perclock_markers(stdout: str) -> bool:
    """Re-print canonical ``STA_PERCLOCK`` markers parsed from *stdout*.

    The raw markers land in the STA EDA tool's own stdout (captured to its log), not
    on the synthesis process stdout the ASIC Flow reads — so, exactly like the
    overall and reg->reg markers, they are re-emitted here via ``print`` to reach
    the metric parser. Returns True iff at least one clock was surfaced.
    """
    rows = parse_perclock(stdout)
    for name, row in rows.items():
        wns = "NA" if row["wns_ns"] is None else f"{row['wns_ns']:.6f}"
        whs = "NA" if row["whs_ns"] is None else f"{row['whs_ns']:.6f}"
        period = 0.0 if row["period_ns"] is None else row["period_ns"]
        print(f"STA_PERCLOCK: name={name} period_ns={period:.6f} wns_ns={wns} whs_ns={whs}")
    return bool(rows)


def parse_sdc_clock_names(text: str) -> list[str]:
    """Every ``create_clock -name <clk>`` name in *text*, in source order.

    Lets an authored multi-clock SDC drive the flow even when no single clock
    *port* is detected: the first authored name becomes the reference clock for
    any generated I/O-delay block. Commented lines are ignored.
    """
    return _CREATE_CLOCK_NAME_RE.findall(text)


def _first_authored_clock(config: StaTimingConfig) -> str | None:
    """First ``create_clock -name`` in the Target's authored SDC, or None.

    Used as the last-resort reference clock when neither ``config.clock`` nor a
    detected port is available but the SDC declares its own clocks (the
    multi-clock authored-constraints case). ``write_sta_sdc`` then suppresses the
    generated ``create_clock`` (the SDC owns it) and only borrows this name for a
    generated I/O-delay block, if any.
    """
    names = parse_sdc_clock_names(read_user_sdc_text(config))
    return names[0] if names else None


def _na_float(token: str) -> float | None:
    """Coerce a marker token to float; ``NA``/malformed → None."""
    if token == "NA":
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _min_opt(a: float | None, b: float | None) -> float | None:
    """Return the min of two optional floats, ignoring None operands."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def emit_timing_markers(stdout: str, config: StaTimingConfig, report_dir: Path) -> bool:
    """Emit the STA stdout markers from a completed timing run; report if any.

    The overall worst path and the internal reg->reg group are **independent**
    timing data.  A user SDC that false-paths the I/O worst path (or an
    otherwise I/O-bound design) leaves the overall ``find_timing_paths`` query
    empty — no ``STA_WORST_SLACK_NS`` — yet the reg->reg group is still valid
    and its Fmax is the number the RTL author actually needs.  Historically the
    engines bailed (``return False``, degrade to nothing) the instant the
    overall slack was absent, dropping the reg->reg markers with it and
    collapsing the QoR line to area-only even though the data sat in run.log.

    This helper emits whatever is present: overall markers when the overall
    slack parses, reg->reg markers whenever the reg->reg slack marker is
    present, and the ``STA_REPORT``/``STA_CSV_REPORT`` pointers when *either*
    surfaced.  ``STA_REG2REG_REPORT`` points at the reg->reg path detail and is
    emitted only when that report was actually written — the Tcl skips it on a
    register-free design, and a dangling pointer reads worse than none.
    Returns True iff any timing datum was surfaced (so the caller only
    degrades/falls back when there is genuinely nothing).  (SETUP-29)
    """
    # ADR 0029 decision 6: recover the effective period from the SDC (the Target
    # may own its own create_clock), not the config scalar. No-SDC runs fall back
    # to config.period_ps, keeping their numbers bit-identical to pre-0029.
    period_ps = effective_period_ps(config, stdout)
    slack_ns = parse_sta_worst_slack(stdout)
    if slack_ns is None:
        slack_ns = parse_sta_worst_slack(report_dir / "overall.csv.rpt")
    if slack_ns is not None:
        print_overall_fmax(slack_ns, period_ps)
    reg2reg_emitted = print_reg2reg_fmax(stdout, period_ps)
    # Per-clock markers are independent of the overall/reg->reg queries: a
    # design whose overall worst path is false-pathed still reports genuine
    # per-clock Fmax, and vice versa. Re-emit whatever the engine surfaced.
    perclock_emitted = emit_perclock_markers(stdout)
    if slack_ns is None and not reg2reg_emitted and not perclock_emitted:
        return False
    print(f"STA_REPORT: {report_dir / 'overall.rpt'}")
    print(f"STA_CSV_REPORT: {report_dir / 'overall.csv.rpt'}")
    reg2reg_rpt = report_dir / "reg2reg.rpt"
    if reg2reg_rpt.exists():
        print(f"STA_REG2REG_REPORT: {reg2reg_rpt}")
    return True


# ============================================================================
# Effective-period recovery (ADR 0029 decision 6)
# ============================================================================


def read_user_sdc_text(config: StaTimingConfig) -> str:
    """Concatenate the Target's authored SDC files in fileset order.

    Multiple ``file_type: SDC`` files are joined last-wins in EDAM fileset
    order (ADR 0029). Missing files already errored at config-resolution time
    (:func:`synth_timing_config`), so the read here is safe. Returns ``""``
    when the Target carries no SDC — the signal ``write_sta_sdc`` /
    :func:`effective_period_ps` use to keep today's generated behaviour.
    """
    return "\n".join(p.read_text(encoding="utf-8") for p in config.sdc)


def parse_sdc_clock_periods_ps(text: str) -> list[float]:
    """Every ``create_clock -period <value>`` in *text*, converted ns→ps.

    Commented (``#``) lines are ignored. Multiple clocks yield multiple
    periods, in source order. Values are read as ns (the SDC default time
    unit); a design that authored ps would be off by 1000, but SDC convention
    is ns and the generated defaults are ns, so this matches practice.
    """
    periods: list[float] = []
    for match in _CREATE_CLOCK_PERIOD_RE.finditer(text):
        try:
            periods.append(float(match.group(1)) * 1000.0)
        except ValueError:
            continue
    return periods


def parse_sta_clock_period_ps(source: str | Path) -> float | None:
    """Parse the STA-reported clock period (``STA_CLOCK_PERIOD_NS``) ns→ps."""
    if isinstance(source, Path):
        if not source.exists():
            return None
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        text = source
    match = _STA_CLOCK_PERIOD_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1)) * 1000.0
    except ValueError:
        return None


def effective_period_ps(config: StaTimingConfig, stdout: str) -> float:
    """Resolve the clock period (ps) that is the Fmax denominator.

    ADR 0029 decision 6: with the period now living in the Target SDC, recover
    it in priority order —

    1. parse ``create_clock -period`` out of the Target's authored SDC (the
       tightest period when several clocks are declared — a single-clock Fmax
       heuristic; multi-clock Fmax is out of scope);
    2. fall back to the STA-reported clock period marker;
    3. fall back to ``config.period_ps``.

    (3) is taken whenever the Target owns no clock — which keeps the no-SDC and
    constraint-less path bit-identical to pre-0029 behaviour (the generated
    ``create_clock`` is built from ``config.period_ps``, so reading it back is
    unnecessary and would risk sub-ps ``%.6f`` rounding).
    """
    user_sdc = read_user_sdc_text(config)
    if _SDC_CREATE_CLOCK_RE.search(user_sdc):
        periods = parse_sdc_clock_periods_ps(user_sdc)
        if periods:
            return min(periods)
        reported = parse_sta_clock_period_ps(stdout)
        if reported is not None:
            return reported
    return config.period_ps


# ============================================================================
# sv2v / Yosys script generation + execution
# ============================================================================


# The one name every consumer of the transpile agrees on. The make-driven
# synth recipe writes it, the synth Yosys script reads it, and the provenance
# helper greps Yosys errors for it — a second spelling would silently break the
# last of those.
SV2V_OUTPUT_NAME = "sv2v_converted.v"

# Tiny RTLIL module-header artifact written immediately after RTL frontend
# processing. It records the effective top-level parameter values without
# serializing the (potentially very large) frontend netlist.
EFFECTIVE_PARAMS_PREFIX = "effective_params_"


def effective_params_filename(design_name: str) -> str:
    """Artifact name holding *design_name*'s effective parameter header."""
    return f"{EFFECTIVE_PARAMS_PREFIX}{design_name}.il"


_EFFECTIVE_PARAM_RE = re.compile(r"(?m)^\s*parameter \\([A-Za-z_$][\w$]*)\s*(.*?)\s*$")


def parse_effective_parameters(text: str) -> dict[str, str]:
    """Parse parameter values from a Yosys ``dump -n`` module header."""
    return dict(_EFFECTIVE_PARAM_RE.findall(text))


def _parameter_guard_commands(
    design_name: str,
    work_dir: Path,
    defines: list[str],
) -> str:
    """Emit the effective-param artifact and fail on enabled-but-zero collisions."""
    artifact = _quote_yosys_path(work_dir / effective_params_filename(design_name))
    commands: list[str] = []
    for name in enabled_define_names(defines):
        escaped = re.escape(name)
        # Anchor the complete RTLIL value. Without the line boundary, a
        # non-zero padded vector such as 4'0001 matched the 4'000 prefix.
        pattern = rf"parameter \\{escaped} (0|[0-9]+'s?0+)[[:space:]]*$"
        commands.extend((f'logger -warn "{pattern}"', f'logger -werror "{pattern}"'))
    commands.append(f"tee -o {artifact} dump -n {design_name}")
    return "; ".join(commands)


def sv2v_argv(
    source_files: list[Path],
    inc_dirs: list[Path],
    defines: list[str],
    output: Path | str,
    *,
    sv2v: Path | str = "sv2v",
) -> list[str]:
    """The sv2v transpile command line: SystemVerilog in, one Verilog file out.

    The single source of truth for the transpile invocation, shared by the
    make-driven synthesis recipe (``syn_make._sv2v_recipe``).

    Parameter overrides are deliberately absent: sv2v preserves parameter
    declarations, and the overrides are applied on the Yosys side
    (``chparam``), so passing them here would double-apply them.
    """

    def boundary_path(value: Path | str) -> str:
        """Render a path for the Linux Session Runtime command boundary."""
        return str(value).replace("\\", "/")

    argv = [boundary_path(sv2v)]
    argv += [f"-I{boundary_path(inc)}" for inc in inc_dirs]
    argv += [f"-D{d}" for d in defines]
    argv += [boundary_path(f) for f in source_files]
    argv += ["-w", boundary_path(output)]
    return argv


def _quote_yosys_path(path) -> str:
    """Quote a path for Yosys script (handles spaces in Windows paths)."""
    s = str(path).replace("\\", "/")
    return f'"{s}"' if " " in s else s


FRONTEND_CHOICES = ("sv2v", "slang")

# What an unset Target ``flow_options.frontend`` resolves to. Named rather
# than repeated as a literal default across the synthesis entry points.
DEFAULT_FRONTEND = "sv2v"

# Yosys formal/verification cell types. These carry no gates and no liberty
# area, so anything downstream of RTL frontend processing chokes on them: ``stat`` prints
# "Area for cell type $check is unknown!" (and scores the design short), ABC has
# nothing to map, and ``write_verilog`` emits a non-structural instance that
# can abort OpenROAD's netlist parse ("syntax error") — leaving a synthesis run that
# exits 0 with silently corrupted area and *no* timing (ravenoc F-30).
FORMAL_CELL_TYPES = frozenset({"$check", "$assert", "$assume", "$cover", "$live", "$fair"})

# The pass that deletes them. slang-only on purpose: sv2v strips SVA during the
# transpile, and plain ``read_verilog`` (no ``-formal``) never creates a formal
# cell, so the sv2v script — and every golden area number taken through it —
# stays byte-identical. yosys-slang instead *lowers* SVA into ``$check`` cells,
# which is right for a formal flow and wrong for gate-level synthesis.
CHFORMAL_REMOVE = "chformal -remove"


def resolve_frontend(
    recipe: dict,
    *,
    override: str | None = None,
    field: str = "Target flow_options.frontend",
) -> str | None:
    """The RTL frontend for the Yosys flows: *override* else the Target recipe.

    *recipe* is the selected Target's ``flow_options`` — user-authored, so a
    wrong value is rejected loudly here rather than becoming an opaque argparse
    crash inside the sandbox. ``None`` means "neither source picked one", which
    callers translate to their own default (``sv2v``); it is deliberately
    distinct from an explicit ``"sv2v"``, so ``asic_synthesize`` can keep
    forwarding no ``--frontend`` flag at all in that case.

    Shared by the synthesis CLI and Target-driven Flow.
    """
    frontend = override or require_opt_str(recipe, "frontend", field=field)
    if frontend is not None and frontend not in FRONTEND_CHOICES:
        raise BoundaryError(
            f"{field} must be one of {', '.join(FRONTEND_CHOICES)}; got {frontend!r}"
        )
    return frontend


def resolve_slang_options(
    recipe: dict,
    *,
    field: str = "Target flow_options.slang_options",
) -> list[str]:
    """Extra raw ``read_slang`` tokens from a Target recipe.

    Empty when the knob is absent. A present-but-wrong value (a bare string, an
    empty list) is a loud config error: silently ignoring it would drop the one
    option — ``--single-unit`` — a repo may be unbuildable without.
    """
    slang_options = recipe.get("slang_options")
    if slang_options is None:
        return []
    if not is_str_list(slang_options) or not slang_options:
        raise BoundaryError(f"{field} must be a non-empty list of strings, got {slang_options!r}")
    return list(slang_options)


def _slang_read_command(
    source_files: list[Path],
    design_name: str,
    inc_dirs: list[Path],
    defines: list[str],
    params: dict[str, str] | None,
    slang_options: list[str] | None = None,
) -> str:
    """Build the ``read_slang`` command that reads and lowers SystemVerilog.

    Yosys 0.67's native slang frontend (povik/sv-elab + MikePopoloski/slang)
    reads SystemVerilog directly, so the sv2v transpile step is skipped: the
    raw source files are handed to ``read_slang`` along with the include search
    paths (``-I``), preprocessor defines (``-D``), and — replacing the Yosys
    ``chparam`` pass the sv2v path uses — top-level parameter overrides applied
    at read time (``-G NAME=VALUE``). ``--top`` names the synthesis top.

    *slang_options* are extra raw ``read_slang`` tokens appended verbatim after
    the generated options (Target ``flow_options.slang_options``). The
    motivating case is ``--single-unit``: slang compiles each file as its own
    compilation unit by default, which breaks the common "defines header
    included once, macros leak across the filelist" convention that sv2v,
    Verilator, and event-driven simulators all honor.
    """
    q = _quote_yosys_path
    opts = [f"--top {design_name}"]
    opts += [f"-I {q(d)}" for d in inc_dirs]
    opts += [f"-D {d}" for d in defines]
    if params:
        opts += [f"-G {name}={value}" for name, value in params.items()]
    opts += slang_options or []
    files_str = " ".join(q(f) for f in source_files)
    return f"read_slang {' '.join(opts)} {files_str}"


def _rtl_frontend_commands(
    source_files: list[Path],
    design_name: str,
    *,
    frontend: str,
    inc_dirs: list[Path],
    defines: list[str],
    params: dict[str, str] | None,
    slang_options: list[str] | None,
) -> str:
    """Read RTL and prepare the selected top for synthesis tech-mapping.

    This is the synthesis RTL frontend: source ingestion, hierarchy selection,
    process lowering, and formal-cell cleanup. Keeping it as one helper makes
    the sv2v and slang paths converge before the technology-mapping stages.

    ``sv2v`` reads the single transpiled Verilog file with ``read_verilog`` +
    ``chparam``; ``slang`` hands the raw SystemVerilog (plus ``inc_dirs`` /
    ``defines`` / ``slang_options``) to ``read_slang``.
    """
    q = _quote_yosys_path
    if frontend == "slang":
        read_src = _slang_read_command(
            source_files, design_name, inc_dirs, defines, params, slang_options
        )
        # read_slang already applied the parameter overrides; hierarchy
        # just re-roots/-checks the design and proc lowers any remaining always
        # blocks. These are near no-ops on slang's word-level netlist but stay
        # harmless. ``chformal -remove`` runs right after ``proc`` (which lowers
        # the last procedural assertions into cells) and before ``opt``/
        # ``techmap`` — see FORMAL_CELL_TYPES for why it is slang-only.
        hls = f"hierarchy -check -top {design_name}; proc; {CHFORMAL_REMOVE}"
    else:
        read_src = "; ".join(f"read_verilog {q(f)}" for f in source_files)
        chparam_cmd = ""
        if params:
            set_args = " ".join(f"-set {name} {value}" for name, value in params.items())
            chparam_cmd = f"chparam {set_args} {design_name}; "
        hls = f"{chparam_cmd}hierarchy -libdir ./ -check -top {design_name}; proc"
    return f"{read_src}; {hls}"


def _build_yosys_script(
    source_files: list[Path],
    design_name: str,
    liberty: Path,
    work_dir: Path,
    flatten: bool,
    params: dict[str, str] | None,
    abc_recipe: str | None,
    frontend: str = "sv2v",
    inc_dirs: list[Path] | None = None,
    defines: list[str] | None = None,
    slang_options: list[str] | None = None,
    generic_abc_before_mapping: bool = False,
    abc_script: str | None = None,
    abc_delay_ps: int | None = None,
) -> str:
    """Build the Yosys synthesis command script string.

    *frontend* selects how the RTL enters Yosys — see
    :func:`_rtl_frontend_commands`, which builds the shared read-design prefix.
    The tech-mapping tail (dfflibmap → ABC → stat) added here is identical for
    both frontends.

    The middle is Yosys's own ``synth`` pass. It used to be a hand-rolled
    ``opt; check -noinit; memory; fsm; techmap``, which skipped the
    optimisations ``synth`` curates (``wreduce``/``peepopt``/``alumacc``/
    ``share``/``opt -full``/``memory_map``) and cost ~14.5 % area — 273.0 kGE
    against 239.9 kGE on this project's 67k-cell top, measured both ways with
    the same frontend and liberty. ``synth`` also prunes unused and constant
    registers (9500 → 9021 flops on that design): ordinary dead-register
    removal, but not formally proven equivalent. By default it runs with
    ``-noabc`` so there is exactly one, explicitly controlled ABC technology
    mapping pass below. ``generic_abc_before_mapping`` is an expert escape
    hatch that restores the generic pre-mapping ABC pass.
    """
    q = _quote_yosys_path
    lib_path = q(liberty)
    out_dir = str(work_dir).replace("\\", "/")

    # Step 1 + 2: read RTL and prepare the selected top for synthesis.
    hls = _rtl_frontend_commands(
        source_files,
        design_name,
        frontend=frontend,
        inc_dirs=inc_dirs or [],
        defines=defines or [],
        params=params,
        slang_options=slang_options,
    )
    parameter_guard = _parameter_guard_commands(design_name, work_dir, defines or [])

    # Step 3: coarse- and fine-grain synthesis. ``synth`` runs its own
    # ``opt``/``memory``/``fsm``/``techmap`` internally, so nothing is hand-run
    # around it — including the ``opt`` the slang path used to need to stop
    # read_slang's async-reset ``$_DFF_*`` primitives reaching dfflibmap
    # unmapped (which scored ZERO area, the undercount scan_synth_logs
    # hard-fails on).
    #
    # No pre-mapping netlist is written here. Yosys's Verilog backend is not
    # read-only: an intermediate ``write_verilog`` between techmap and
    # dfflibmap perturbed the design enough to add 5.95 kGE, and nothing
    # consumed the file it produced.
    synth = f"synth -top {design_name}"
    if not generic_abc_before_mapping:
        synth += " -noabc"
    if flatten:
        synth += " -flatten"

    # Step 4: DFF library mapping
    dfflibmap = f"dfflibmap -liberty {lib_path}"

    # Step 5: ABC technology mapping
    abc = _build_abc_command(
        lib_path,
        out_dir,
        design_name,
        abc_recipe,
        abc_script=abc_script,
        abc_delay_ps=abc_delay_ps,
    )

    # Step 6: Write final netlists and statistics. The sta_* netlist keeps
    # OpenROAD on a plain structural dialect while the regular netlist preserves
    # the historical artifact name.
    synth_out = q(out_dir + "/synth_" + design_name + ".v")
    sta_out = q(out_dir + "/sta_" + design_name + ".v")
    stat_out = q(out_dir + "/stat_" + design_name + ".txt")
    wout = (
        f"write_verilog {synth_out}; "
        "setundef -zero; splitnets; clean; "
        f"write_verilog -noattr -noexpr -nohex -nodec {sta_out}; "
        f"tee -o {stat_out} stat -liberty {lib_path}"
    )

    return f"{hls}; {parameter_guard}; {synth}; {dfflibmap}; {abc}; {wout}"


def _build_abc_command(
    lib_path: str,
    out_dir: str,
    design_name: str,
    abc_recipe: str | None,
    *,
    abc_script: str | None = None,
    abc_delay_ps: int | None = None,
) -> str:
    """Build the ABC technology mapping command string."""
    q = _quote_yosys_path
    abc_log = q(out_dir + "/log_abc_" + design_name + ".txt")
    abc_base = f"tee -o {abc_log} abc -liberty {lib_path}"
    if abc_delay_ps is not None:
        abc_base += f" -D {abc_delay_ps}"
    pstats = ";print_stats"
    if abc_script is not None:
        abc = f"{abc_base} -script {abc_script}"
    elif abc_recipe == "fast":
        abc = f"{abc_base} -script +strash;balance;rewrite;rewrite,-z;refactor;map{pstats}"
    elif abc_recipe == "balanced":
        abc = f"{abc_base} -script +strash;ifraig;balance;rewrite;rewrite,-z;balance;refactor;balance;rewrite;map{pstats}"
    elif abc_recipe and abc_recipe.startswith("+"):
        abc = f"{abc_base} -script {abc_recipe}"
    else:
        abc = abc_base
    return abc + "; opt"


# Yosys/ABC/sv2v error markers that can appear in a log even when the EDA tool
# exits 0 (e.g. ABC fallback recipes that silently partial-fail). Keep this list
# conservative — a false positive fails otherwise-valid synthesis.
_SYNTH_ERROR_MARKERS = (
    "ERROR:",  # standard yosys error
    "ABC: Error",  # ABC-side errors surfaced in the yosys log
    "Error: ABC",
    "Unsupported",  # sv2v / yosys frontend unsupported construct
    "Syntax error",
)

# Yosys `stat` emits "   Area for cell type <type> is unknown!" (a plain log(),
# so it reaches yosys.log via the `tee -o`) for any cell with no liberty area.
# For an unmapped LOGIC primitive (e.g. an async-reset $_DFF_PN0_ the dfflibmap
# couldn't place) or a liberty gap this is a real corruption: `stat` scores the
# cell as ZERO area and still exits 0, so the flow would report a PASS with a
# silently-undercounted (often ~2x too small) area. Treat it as a hard failure.
_UNKNOWN_AREA_RE = re.compile(r"Area for cell type (\S+) is unknown!")

# ...EXCEPT for Yosys-internal metadata cells that carry no gates by design and
# legitimately have no area — a stat "unknown area" for these is noise, not a
# corrupted total. ``$scopeinfo`` (source-scope annotation kept for formal/debug,
# present since ~Yosys 0.40) survives a ``flatten`` that doesn't ``opt_clean
# -purge``, so failing on it would wrongly reject valid flatten runs.
_BENIGN_UNKNOWN_AREA_CELLS = frozenset({"$scopeinfo"})


def _unknown_area_hint(cell_type: str) -> str:
    """Name the likely cause behind a ``stat`` "unknown area" for *cell_type*.

    The bare Yosys line names the symptom only, and the two recurring causes
    have opposite fixes (ravenoc F-30): a surviving *formal* cell means SVA
    reached the gate-level flow, while any other unknown cell means a mapping
    or liberty gap. Returns "" when nothing specific can be said.
    """
    if cell_type in FORMAL_CELL_TYPES:
        return (
            f"\n  cause: {cell_type} is an assertion/formal cell, not logic — SVA reached "
            "tech-mapping. sv2v strips assertions but the slang frontend lowers them, so "
            f"Booley's slang script runs `{CHFORMAL_REMOVE}`; seeing this means the cells "
            "survived it (custom yosys template, or a Yosys build whose chformal predates "
            "$check). Guard the assertions out of the synthesis view (e.g. an "
            "`ifndef SYNTHESIS` / a NO_ASSERTIONS vlogdefine on the synth Target) and "
            "re-run. Left in place they can also abort OpenROAD's netlist parse, so timing "
            "comes back empty."
        )
    return (
        f"\n  cause: {cell_type} reached `stat` unmapped, so it was scored as ZERO area "
        "and the reported total is an undercount. Usually a cell the liberty has no entry "
        "for, or a primitive dfflibmap/ABC could not map (e.g. an async-reset flop with no "
        "matching library cell)."
    )


def scan_synth_logs(work_dir: Path) -> str | None:
    """Scan sv2v/yosys logs for error markers that appear despite a 0 exit code.

    Yosys (and ABC) sometimes emit ``ERROR:`` lines yet exit 0, which would
    otherwise masquerade as a PASS. Also fails on a ``stat`` "Area for cell type
    ... is unknown!" for a real (non-metadata) cell — an unmapped primitive whose
    zero area silently undercounts the design. Returns the first offending log
    line (stripped, truncated), or None if the logs are clean/absent. An
    unknown-area line is returned with a ``cause:`` hint appended
    (:func:`_unknown_area_hint`) so the report names the diagnosis, not just the
    Yosys symptom. The OpenROAD log is intentionally excluded — timing
    violations and RSZ buffering warnings are warnings, not failures.
    """
    for log_name in ("sv2v.log", "yosys.log"):
        log_path = work_dir / log_name
        if not log_path.exists():
            continue
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if any(marker in line for marker in _SYNTH_ERROR_MARKERS):
                return line.strip()[:500]
            unknown = _UNKNOWN_AREA_RE.search(line)
            if unknown and unknown.group(1) not in _BENIGN_UNKNOWN_AREA_CELLS:
                return line.strip()[:500] + _unknown_area_hint(unknown.group(1))
    return None


def _toml_float(cfg: dict, key: str, default: float) -> float:
    """Coerce a TOML timing value to float, validating at the boundary.

    ``cfg`` comes from user-authored booley.toml — a trust boundary.  An
    absent key yields ``default``; a present-but-non-numeric value is a
    config error and is reported loudly rather than crashing with an opaque
    ValueError/TypeError. ``require_finite_number`` also rejects the bool
    trap (``float(True)`` would otherwise silently become ``1.0``) and
    NaN/inf.
    """
    if key not in cfg:
        return default
    raw = cfg[key]
    try:
        return require_finite_number(raw, field=f"[flows.synth.timing] {key!r}")
    except BoundaryError as exc:
        sys.exit(f"ERROR: {exc}")


def _toml_bool(cfg: dict, key: str, default: bool) -> bool:
    """Coerce a TOML timing value to bool, validating at the boundary.

    Same trust-boundary contract as :func:`_toml_float`: an absent key yields
    ``default``; a present-but-non-bool value is a loud config error rather than
    a silent truthiness surprise (e.g. the string ``"false"`` is truthy).
    """
    if key not in cfg:
        return default
    raw = cfg[key]
    if not isinstance(raw, bool):
        sys.exit(
            f"ERROR: [flows.synth.timing] {key!r} must be a boolean (true/false), got {raw!r}"
        )
    return raw


# Every key ``synth_timing_config`` still consumes from
# ``[flows.synth.timing]``. These are the genuine flow/backend knobs
# (ADR 0029 decision 2). Anything else is a typo or stale knob that would be
# *silently ignored*, so we warn on unknown keys.
TIMING_CONFIG_KEYS = frozenset(
    {
        "utilization_pct",
        "repair_timing",
    }
)

# ADR 0029 decision 3 (hard cutoff): the portable *design constraints* moved
# out of ``booley.toml`` and into a ``file_type: SDC`` fileset on the FuseSoC
# Target. These keys are no longer read; a project that still sets one gets a
# setup-time error naming the migration (no additive fallback — a dual source
# of truth would resurrect the design-list drift ADR 0022 decision 3 deleted).
_MIGRATED_CONSTRAINT_KEYS = frozenset(
    {
        "sdc",
        "period_ps",
        "input_delay_pct",
        "output_delay_pct",
        "clock",
    }
)


def _load_and_validate_timing_config(project_root: Path | None = None) -> dict:
    """Load ``[flows.synth.timing]`` from booley.toml and validate its keys.

    Hard cutoff (ADR 0029 decision 3): a migrated design-constraint key still
    present in booley.toml is a setup-time error naming the migration, not a
    silent second source of truth. Unknown keys still warn (typo guard).

    *project_root* pins the booley.toml lookup to an explicit worktree (the
    in-process asic_synthesize configure half, ADR 0037 §8); ``None`` keeps the
    default project-root resolution path for direct configuration.
    """
    from booley.runtime.shared_infra import _load_rtl_config

    cfg = _load_rtl_config(project_root) or {}
    from booley.targets.flow_names import config_section

    timing = config_section(cfg.get("flows", {}), "synth").get("timing", {})
    if not isinstance(timing, dict):
        timing = {}

    for key in timing:
        if key == "engine":
            sys.exit(
                "ERROR: [flows.synth.timing] 'engine' is retired. Select "
                "flow_options.synth_mode = physical or logical on the .core Target."
            )
        if key in _MIGRATED_CONSTRAINT_KEYS:
            sys.exit(
                f"ERROR: [flows.synth.timing] {key!r} is no longer "
                "read (ADR 0029). ASIC design constraints moved into a "
                "`file_type: SDC` fileset on the FuseSoC Target. Put "
                "create_clock / set_input_delay / set_output_delay / "
                "set_false_path into an SDC file, add it to the Target's "
                f"fileset, and delete the {key!r} line from "
                "[flows.synth.timing] in booley.toml."
            )
        if key not in TIMING_CONFIG_KEYS:
            print(
                f"WARNING: [flows.synth.timing] unknown key {key!r} "
                "ignored; valid keys: " + ", ".join(sorted(TIMING_CONFIG_KEYS))
            )
    return timing


def _resolve_synth_mode(mode: str | SynthMode | None) -> SynthMode:
    """Resolve and validate physical versus logical synthesis intent."""
    resolved_mode = str(mode or "physical").lower()
    if resolved_mode not in SYNTH_MODE_CHOICES:
        sys.exit("ERROR: synth mode must be one of: " + ", ".join(SYNTH_MODE_CHOICES))
    return SynthMode(resolved_mode)


def _resolve_sta_sdc_paths(sdc: list[str] | None, root: Path | None = None) -> list[Path]:
    """Resolve ``--sta-sdc`` file paths against *root* (default ``PROJECT_ROOT``).

    STA constraint SDC files (ADR 0029): one per ``--sta-sdc``, sourced from
    the Target's ``file_type: SDC`` fileset (the Flow forwards them) or passed
    directly to the configure surface. A relative path resolves against
    the project root (``/work`` in the Session Runtime), not cwd — same
    convention as ``--inc-dir`` / ``--extra-rtl`` — so a path the caller
    relativized against the worktree stays valid for the generated boundary
    command. There is no TOML fallback: ``[flows.synth.timing].sdc`` is a
    hard-removed key.
    """
    base = root if root is not None else PROJECT_ROOT
    resolved_sdc: list[Path] = []
    for raw in sdc or []:
        sdc_path = Path(raw)
        resolved = sdc_path.resolve() if sdc_path.is_absolute() else (base / sdc_path).resolve()
        if not resolved.exists():
            sys.exit(f"ERROR: STA SDC constraints file not found: {resolved}")
        resolved_sdc.append(resolved)
    return resolved_sdc


def synth_timing_config(
    *,
    mode: str | SynthMode | None = None,
    clock: str | None = None,
    period_ps: float | None = None,
    input_delay_pct: float | None = None,
    output_delay_pct: float | None = None,
    sdc: list[str] | None = None,
    utilization_pct: float | None = None,
    repair_timing: bool | None = None,
    placement_density: float | None = None,
    repair_hold: bool = False,
    gate_cloning: bool = False,
    setup_margin_ns: float = 0.0,
    repair_tns_percent: float | None = None,
    legacy_utilization_fallback: bool | None = None,
    legacy_repair_fallback: bool | None = None,
    project_root: Path | None = None,
) -> StaTimingConfig:
    """Resolve simple-backend timing config from CLI overrides and booley.toml.

    Physical mode is the default: Yosys mapping followed by OpenROAD placement,
    repair, parasitic estimation, and STA. Logical mode stops after Yosys and
    reports mapped area without timing.

    Design constraints (``period``/``clock``/I-O delays/extra SDC) are **not**
    read from ``booley.toml`` anymore (ADR 0029): they arrive as ``--sta-sdc``
    files sourced from the Target's ``file_type: SDC`` fileset, plus the CLI
    overrides for direct configure use. ``sdc`` here is the list of
    those SDC paths (repeatable ``--sta-sdc``).

    *project_root* pins the booley.toml lookup and relative-SDC resolution to
    an explicit worktree (in-process configure, ADR 0037 §8); ``None`` keeps
    the legacy module-level ``PROJECT_ROOT``/CWD behaviour.

    The two ``legacy_*_fallback`` controls preserve standalone callers that
    historically sourced utilization and setup repair from
    ``[flows.synth.timing]``. ``None`` keeps the old direct-call rule: use TOML
    only when the corresponding argument is absent. The profile-aware Booley Flow
    passes ``False`` by forwarding an explicit profile.
    """
    timing = _load_and_validate_timing_config(project_root)
    resolved_mode = _resolve_synth_mode(mode)
    resolved_sdc = _resolve_sta_sdc_paths(sdc, project_root)
    # Design-constraint scalars (period/clock/I-O delays) come only from the
    # trusted argparse-typed CLI overrides now; there is no booley.toml fallback
    # for them (ADR 0029). Absent → the DEFAULT_STA_* constants. The remaining
    # recipe knobs (utilization/repair_timing) keep their validated TOML read.
    if legacy_utilization_fallback is None:
        legacy_utilization_fallback = utilization_pct is None
    if legacy_repair_fallback is None:
        legacy_repair_fallback = repair_timing is None
    legacy_utilization = legacy_utilization_fallback and "utilization_pct" in timing
    legacy_repair = legacy_repair_fallback and "repair_timing" in timing
    if legacy_utilization:
        resolved_utilization = _toml_float(timing, "utilization_pct", DEFAULT_STA_UTILIZATION_PCT)
    elif utilization_pct is not None:
        resolved_utilization = float(utilization_pct)
    else:
        resolved_utilization = DEFAULT_STA_UTILIZATION_PCT
    if legacy_repair:
        resolved_repair = _toml_bool(timing, "repair_timing", True)
    elif repair_timing is not None:
        resolved_repair = repair_timing
    else:
        resolved_repair = True
    return StaTimingConfig(
        mode=resolved_mode,
        clock=clock,
        period_ps=float(period_ps) if period_ps else DEFAULT_STA_PERIOD_PS,
        input_delay_pct=(
            float(input_delay_pct) if input_delay_pct is not None else DEFAULT_STA_INPUT_DELAY_PCT
        ),
        output_delay_pct=(
            float(output_delay_pct)
            if output_delay_pct is not None
            else DEFAULT_STA_OUTPUT_DELAY_PCT
        ),
        sdc=tuple(resolved_sdc),
        utilization_pct=resolved_utilization,
        repair_timing=resolved_repair,
        placement_density=None if legacy_utilization else placement_density,
        repair_hold=repair_hold,
        gate_cloning=gate_cloning,
        setup_margin_ns=setup_margin_ns,
        repair_tns_percent=repair_tns_percent,
    )


def detect_clock_port(netlist: Path) -> str | None:
    """Best-effort clock-port detection for simple single-clock projects."""
    try:
        text = netlist.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    inputs = set()
    for match in re.finditer(
        r"\binput\b\s*(?:wire\s+|reg\s+|logic\s+)?(?:\[[^\]]+\]\s*)?([^;]+);",
        text,
    ):
        for raw_name in match.group(1).split(","):
            name = raw_name.strip().split()[-1].lstrip("\\")
            if name:
                inputs.add(name)
    for candidate in _CLOCK_CANDIDATES:
        if candidate in inputs:
            return candidate
    return None


def write_sta_sdc(config: StaTimingConfig, clock: str, work_dir: Path) -> Path:
    """Create the physical-mode SDC from generated defaults plus the Target's SDC.

    ADR 0029 decision 5: when the Target's authored SDC declares its own
    ``create_clock`` / ``set_input_delay`` / ``set_output_delay``, the matching
    generated default is suppressed (detected by scanning the authored SDC text,
    not a config flag) so a Target can fully own its timing intent. A Target
    with no SDC — or one that constrains only false/multicycle paths — keeps the
    historical generated block byte-for-byte.
    """
    sdc_path = work_dir / "sta_constraints.sdc"
    period_ns = config.period_ps / 1000.0
    input_delay_ns = period_ns * (config.input_delay_pct / 100.0)
    output_delay_ns = period_ns * (config.output_delay_pct / 100.0)

    user_sdc = read_user_sdc_text(config)
    owns_clock = bool(_SDC_CREATE_CLOCK_RE.search(user_sdc))
    owns_input = bool(_SDC_INPUT_DELAY_RE.search(user_sdc))
    owns_output = bool(_SDC_OUTPUT_DELAY_RE.search(user_sdc))

    lines: list[str] = []
    if user_sdc:
        lines.append(user_sdc)
    lines.extend(["", "# Auto-generated by Booley Yosys/OpenROAD backend."])
    if not owns_clock:
        lines.append(f"create_clock -name {clock} -period {period_ns:.6f} [get_ports {{{clock}}}]")
    if not owns_input:
        # Constrain every input except the clock. ``remove_from_collection`` is a
        # Guard the collection operation and fall back to constraining all
        # inputs so an SDC command mismatch cannot silently erase I/O timing.
        lines.append(
            "if { [catch { set input_ports [remove_from_collection [all_inputs]"
            + " [get_ports {"
            + clock
            + "}]] }] } { set input_ports [all_inputs] }"
        )
        lines.append(f"set_input_delay -clock {clock} {input_delay_ns:.6f} $input_ports")
    if not owns_output:
        lines.append(f"set_output_delay -clock {clock} {output_delay_ns:.6f} [all_outputs]")
    # Electrical-environment defaults. ``set_driving_cell`` references the
    # ``$input_ports`` helper, so it is only emitted alongside the generated
    # input-delay block that defines it.
    if not owns_input:
        lines.append("catch { set_driving_cell -lib_cell BUF_X1 $input_ports }")
    if not owns_output:
        lines.append("catch { set_load 10.0 [all_outputs] }")
    sdc_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sdc_path


def parse_sta_worst_slack(source: str | Path) -> float | None:
    """Parse worst setup slack in ns from STA output or Booley CSV."""
    if isinstance(source, Path):
        if not source.exists():
            return None
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        text = source
    marker = _STA_SLACK_RE.search(text)
    if marker:
        # EDA-tool-output parsing: the regex constrains the group to a numeric
        # form, but if the marker format ever drifts, degrade to the
        # CSV scan below instead of crashing the whole synthesis flow.
        try:
            return float(marker.group(1))
        except ValueError:
            print(
                "WARNING: STA slack marker present but not a valid float: "
                f"{marker.group(1)!r}; falling back to CSV scan"
            )
    values: list[float] = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 3:
            with contextlib.suppress(ValueError):
                values.append(float(parts[2]))
    return min(values) if values else None
