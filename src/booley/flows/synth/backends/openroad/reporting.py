"""OpenROAD timing constraints, commands, and report interpretation."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

from booley.flows.synth.timing import (
    StaTimingConfig,
    parse_perclock,
    parse_sdc_clock_periods_ps,
    read_user_sdc_text,
)

_STA_SLACK_RE = re.compile(r"STA_WORST_SLACK_NS:\s*([-+]?\d+(?:\.\d+)?)")
_REG2REG_SLACK_RE = re.compile(r"STA_REG2REG_SLACK_NS:\s*([-+]?\d+(?:\.\d+)?)")
_STA_CLOCK_PERIOD_RE = re.compile(r"STA_CLOCK_PERIOD_NS:\s*([-+]?\d+(?:\.\d+)?)")


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
        "-sort_by_slack -group_path_count 1 -from [all_registers] "
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
    per-clock critical path/Fmax from period and setup slack
    (:func:`booley.evidence.timing.derive_critical_path_and_fmax`), exactly as the
    overall path does. A clock with no setup/hold path emits ``NA`` for that slack.

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
        "-sort_by_slack -group_path_count 1 -to $_clk]}] && [llength $_sp] > 0} {\n"
        '      set _wns [format "%.6f" [get_property [lindex $_sp 0] slack]]\n'
        "    }\n"
        '    set _whs "NA"\n'
        "    if {![catch {set _hp [find_timing_paths -path_delay min "
        "-sort_by_slack -group_path_count 1 -to $_clk]}] && [llength $_hp] > 0} {\n"
        '      set _whs [format "%.6f" [get_property [lindex $_hp 0] slack]]\n'
        "    }\n"
        '    puts [format "STA_PERCLOCK: name=%s period_ns=%.6f wns_ns=%s '
        'whs_ns=%s" $_cn $_per $_wns $_whs]\n'
        "  }\n"
        "}\n"
    )


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
    # Recover the effective period only from the Target SDC or OpenROAD's
    # runtime marker. Physical synthesis has no generated/default period.
    period_ps = effective_period_ps(config, stdout)
    slack_ns = parse_sta_worst_slack(stdout)
    if slack_ns is None:
        slack_ns = parse_sta_worst_slack(report_dir / "overall.csv.rpt")
    if slack_ns is not None and period_ps is not None:
        print_overall_fmax(slack_ns, period_ps)
    reg2reg_emitted = period_ps is not None and print_reg2reg_fmax(stdout, period_ps)
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


def effective_period_ps(config: StaTimingConfig, stdout: str) -> float | None:
    """Resolve the clock period (ps) that is the Fmax denominator.

    ADR 0029 decision 6: with the period now living in the Target SDC, recover
    it in priority order —

    Parse ``create_clock -period`` out of the Target's authored SDC (the
    tightest period when several clocks are declared), then fall back to the
    STA-reported clock period marker for dynamic Tcl. There is deliberately no
    built-in period fallback: authored SDC is the only physical timing authority.
    """
    user_sdc = read_user_sdc_text(config)
    periods = parse_sdc_clock_periods_ps(user_sdc)
    if periods:
        return min(periods)
    return parse_sta_clock_period_ps(stdout)


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
