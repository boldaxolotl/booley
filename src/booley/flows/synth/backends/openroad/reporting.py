"""OpenROAD timing constraints, commands, and report interpretation."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import NamedTuple

from booley.evidence.timing import derive_critical_path_and_fmax
from booley.flows.synth.timing import DEFAULT_TIMING_REPORT_PATHS, parse_perclock

_STA_SLACK_RE = re.compile(r"STA_WORST_SLACK_NS:\s*([-+]?\d+(?:\.\d+)?)")
_REG2REG_SLACK_RE = re.compile(r"STA_REG2REG_SLACK_NS:\s*([-+]?\d+(?:\.\d+)?)")


def slack_cap_tcl(slacks_var: str) -> str:
    """Tcl option capping ``report_checks`` at the slack of the last kept path.

    ``get_property slack`` returns the slack rounded to six decimals, while
    ``-slack_max`` compares against the unrounded value. When the rounding goes
    down, the cap would drop the last kept path (and with one path, every
    path). One printed unit (1e-6 ns) of margin keeps it; a path that near-ties
    with the last kept one may then also be listed.
    """
    return f"-slack_max [expr {{[lindex ${slacks_var} end] + 1e-6}}]"


def top_paths_report_tcl(slacks_var: str, count: int, report_path: str) -> str:
    """Tcl writing a text ``report_checks`` that holds the global worst *count* paths.

    ``report_checks -group_path_count N`` applies N per path group (each clock,
    SDC ``group_path``, async and gating group), so on its own it can list
    N x G paths. Capping it with ``-slack_max`` at the slack of the last kept
    path makes the text report hold the same worst *count* paths as the CSV
    (plus near ties). With no paths the report runs uncapped and says "No paths".

    ``$slacks_var`` must hold the kept paths' slacks as plain numbers, worst
    first: ``report_checks`` frees the path objects ``find_timing_paths``
    returned, so touching them afterwards crashes OpenROAD.
    """
    base = f"report_checks -path_delay max -sort_by_slack -group_path_count {count}"
    return (
        f"if {{[llength ${slacks_var}] > 0}} {{\n"
        f"  {base} {slack_cap_tcl(slacks_var)} > {{{report_path}}}\n"
        "} else {\n"
        f"  {base} > {{{report_path}}}\n"
        "}\n"
    )


def _clock_probe_tcl(path_expr: str, same_var: str, period_var: str) -> str:
    """Tcl statements classifying one path end by its launch and capture clocks.

    Sets ``$same_var`` to 1 when the path's ``startpoint_clock`` and
    ``endpoint_clock`` are the same clock and to 0 otherwise (a different
    clock, or an unclocked end, which STA reports as the string ``NULL``), and
    ``$period_var`` to the capture clock's period in ns (``NA`` when the end is
    unclocked). Clocks are compared by object handle, never by name. Must run
    inside ``catch``.
    """
    return (
        f"set {same_var} 0\n"
        f'set {period_var} "NA"\n'
        f"set _pc {path_expr}\n"
        "set _sc [get_property $_pc startpoint_clock]\n"
        "set _ec [get_property $_pc endpoint_clock]\n"
        'if {$_ec ne "NULL"} {\n'
        f'  set {period_var} [format "%.6f" [get_property $_ec period]]\n'
        f'  if {{$_sc ne "NULL" && $_sc eq $_ec}} {{ set {same_var} 1 }}\n'
        "}\n"
    )


def path_clocks_tcl(marker: str, path_expr: str) -> str:
    """Tcl that prints *marker* with the clocks of the timing path *path_expr*.

    The line reads ``<marker>: same_clock=<1|0> capture_period_ns=<ns|NA>``.
    Python derives the critical path and Fmax from it only for a same-clock
    path (:func:`print_overall_fmax`). Under ``catch``: a failing query yields
    no marker, hence no Fmax, instead of aborting the ``-exit`` run. Like the
    slack queries, it must run before ``report_checks``, which frees the path
    objects.
    """
    probe = _clock_probe_tcl(path_expr, "_same", "_cap")
    puts = f'puts "{marker}: same_clock=$_same capture_period_ns=$_cap"\n'
    return "catch {\n" + probe + puts + "}\n"


def reg2reg_timing_tcl(
    report_path: str | None = None, *, report_paths: int = DEFAULT_TIMING_REPORT_PATHS
) -> str:
    """Tcl that prints the worst register-to-register setup slack.

    Physical mode emits this alongside the overall worst path so the internal
    (reg->reg) critical path is always visible. The overall worst
    path is the single most-negative-slack path in the design; with a non-zero
    ``set_input_delay``/``set_output_delay`` budget an I/O path routinely wins
    it, hiding the true reg->reg Fmax.  Restricting ``find_timing_paths`` to
    ``all_registers`` on both ends reports the internal path directly.

    ``report_path``, when given, also dumps the reg->reg path detail
    (gate-by-gate arrival table) for the worst ``report_paths`` paths there.
    Without it the reg->reg Fmax number was surfaced but the path behind it was
    not: ``overall.rpt`` lists the worst overall paths, which on an I/O-bound
    design are pad-to-pad feed-throughs — useless for deciding what RTL to
    pipeline.  Digging the reg->reg path out then meant re-running timing
    analysis by hand.  ``STA_REG2REG_SLACK_NS`` is always the single worst
    path's slack, whatever ``report_paths`` is. ``STA_REG2REG_CLOCKS`` follows it
    with that path's own clocks (:func:`path_clocks_tcl`), printed before the
    report because ``report_checks`` frees the path objects.

    Wrapped in ``catch``: a purely combinational design (no registers) or an
    timer without ``all_registers`` degrades to *no* marker rather than
    failing the whole timing run — same warn-and-degrade contract as the rest
    of this module.  The report write sits under its own ``catch`` inside the
    same guard, so an engine that can find the paths but chokes on the report
    still yields the marker.
    """
    report_block = ""
    if report_path is not None:
        # The guard below ensures the slack list is non-empty, so the cap is valid.
        report_block = (
            "  catch {report_checks -path_delay max -sort_by_slack "
            f"-group_path_count {report_paths} -from [all_registers] -to [all_registers] "
            f"-format full {slack_cap_tcl('_r2r_slacks')} > {{{report_path}}}}}\n"
        )
    return (
        "if {![catch {set _r2r [find_timing_paths -path_delay max "
        f"-sort_by_slack -group_path_count {report_paths} -from [all_registers] "
        "-to [all_registers]]}] && [llength $_r2r] > 0} {\n"
        "  set _r2r_slacks {}\n"
        f"  foreach _p [lrange $_r2r 0 {report_paths - 1}] "
        "{ lappend _r2r_slacks [get_property $_p slack] }\n"
        '  puts [format "STA_REG2REG_SLACK_NS: %.6f" [lindex $_r2r_slacks 0]]\n'
        f"{path_clocks_tcl('STA_REG2REG_CLOCKS', '[lindex $_r2r 0]')}"
        f"{report_block}"
        "}\n"
    )


def constraint_check_tcl(report_dir: Path) -> str:
    """Tcl that lists what the authored SDC leaves untimed, as a prefixed block.

    Runs OpenSTA's ``check_setup`` for input ports without ``set_input_delay``,
    output ports without ``set_output_delay``, and unconstrained endpoints (clock
    ports are not listed). The output is captured and re-printed between
    ``STA_CONSTRAINT_CHECK: begin`` and ``end``, every line prefixed: raw
    ``Warning:`` lines would be counted as an actionable EDA warning and turn a
    PASS into WARN, and the NOTE built from this block must not move the
    verdict. The begin/end pair tells "checked, nothing found" from "not
    checked". The unprefixed text also goes, uncapped, to ``unconstrained.rpt`` in
    ``report_dir`` because ``run.log`` keeps only the tail of a very large run.

    Wrapped in ``catch``: an engine without the command loses only this block,
    the same warn-and-degrade contract as the rest of this module.
    """
    return (
        "if {![catch {with_output_to_variable _cc {check_setup -verbose "
        "-no_input_delay -no_output_delay -unconstrained_endpoints}}]} {\n"
        f"  set _ccf [open {{{(report_dir / 'unconstrained.rpt').as_posix()}}} w]\n"
        "  puts -nonewline $_ccf $_cc\n"
        "  close $_ccf\n"
        '  puts "STA_CONSTRAINT_CHECK: begin"\n'
        '  foreach _l [split $_cc "\\n"] {\n'
        '    if {[string trim $_l] ne ""} { puts "STA_CONSTRAINT_CHECK: $_l" }\n'
        "  }\n"
        '  puts "STA_CONSTRAINT_CHECK: end"\n'
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


class PathClocks(NamedTuple):
    """The clocks of one timing path, from a :func:`path_clocks_tcl` marker."""

    same_clock: bool
    capture_period_ns: float | None


def parse_path_clocks(text: str, marker: str) -> PathClocks | None:
    """Parse the ``<marker>:`` clocks line from STA stdout, or None when absent."""
    match = re.search(
        rf"{re.escape(marker)}:\s*same_clock=([01])\s+capture_period_ns=(NA|[-+]?\d+(?:\.\d+)?)",
        text,
    )
    if match is None:
        return None
    period = None if match.group(2) == "NA" else float(match.group(2))
    return PathClocks(match.group(1) == "1", period)


def _same_clock_timing(
    slack_ns: float, clocks: PathClocks | None
) -> tuple[float | None, float | None]:
    """Critical path (ps) and Fmax (MHz) of a path, or ``(None, None)``.

    Only a path launched and captured by one clock is timed against that
    clock's period; with another clock, an unclocked end, or no clocks marker
    the path has no critical path or Fmax, only its slack.
    """
    if clocks is None or not clocks.same_clock:
        return None, None
    return derive_critical_path_and_fmax(clocks.capture_period_ns, slack_ns)


def print_reg2reg_fmax(stdout: str) -> bool:
    """Emit derived reg->reg critical-path (ps) + Fmax (MHz) markers.

    The Tcl prints the worst reg->reg path's slack and its clocks
    (``STA_REG2REG_CLOCKS``); the derivation lives here in Python, exactly
    like the overall critical path. The markers are printed only for a
    same-clock path, and Fmax only when the critical path is positive.
    No-op when the slack marker is absent (combinational design / degraded STA).

    Returns True iff the reg->reg slack marker was present — callers gate the
    "no timing at all" degrade on this so a false-pathed overall worst path
    never buries the internal Fmax.
    """
    slack_ns = parse_reg2reg_slack(stdout)
    if slack_ns is None:
        return False
    critical_path_ps, fmax_mhz = _same_clock_timing(
        slack_ns, parse_path_clocks(stdout, "STA_REG2REG_CLOCKS")
    )
    if critical_path_ps is not None:
        print(f"STA_REG2REG_CRITICAL_PATH_PS: {critical_path_ps:.3f}")
    if fmax_mhz is not None:
        print(f"STA_REG2REG_FMAX_MHZ: {fmax_mhz:.3f}")
    return True


def print_overall_fmax(slack_ns: float, clocks: PathClocks | None) -> None:
    """Emit the overall worst-path slack + derived critical-path/Fmax markers."""
    print(f"STA_WORST_SLACK_NS: {slack_ns:.6f}")
    critical_path_ps, fmax_mhz = _same_clock_timing(slack_ns, clocks)
    if critical_path_ps is not None:
        print(f"STA_CRITICAL_PATH_PS: {critical_path_ps:.3f}")
    if fmax_mhz is not None:
        print(f"STA_FMAX_MHZ: {fmax_mhz:.3f}")


def perclock_timing_tcl() -> str:
    """Tcl that prints one ``STA_PERCLOCK`` marker per clock in the design.

    Fmax and critical-path delay are inherently per-clock, so physical mode
    iterates ``[all_clocks]`` and, for each, reports its own worst setup
    (``-path_delay max``) and hold (``-path_delay min``) slack against paths
    *ending* in that clock domain (``-to $clk``). The marker carries the clock's
    name, its constrained period (ns), both slacks (ns), and ``setup_same_clock``:
    1 when the worst setup path is launched by that same clock, else 0 (another
    clock, an unclocked start, or no setup path). Python derives the per-clock
    critical path/Fmax from period and setup slack
    (:func:`booley.evidence.timing.derive_critical_path_and_fmax`) only for a
    same-clock path, exactly as the overall path does. A clock with no
    setup/hold path emits ``NA`` for that slack.

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
        "    set _ssc 0\n"
        "    if {![catch {set _sp [find_timing_paths -path_delay max "
        "-sort_by_slack -group_path_count 1 -to $_clk]}] && [llength $_sp] > 0} {\n"
        '      set _wns [format "%.6f" [get_property [lindex $_sp 0] slack]]\n'
        "      catch {\n"
        f"{_clock_probe_tcl('[lindex $_sp 0]', '_same', '_cap')}"
        "        set _ssc $_same\n"
        "      }\n"
        "    }\n"
        '    set _whs "NA"\n'
        "    if {![catch {set _hp [find_timing_paths -path_delay min "
        "-sort_by_slack -group_path_count 1 -to $_clk]}] && [llength $_hp] > 0} {\n"
        '      set _whs [format "%.6f" [get_property [lindex $_hp 0] slack]]\n'
        "    }\n"
        '    puts [format "STA_PERCLOCK: name=%s period_ns=%.6f wns_ns=%s '
        'whs_ns=%s setup_same_clock=%s" $_cn $_per $_wns $_whs $_ssc]\n'
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
        wns = "NA" if row.wns_ns is None else f"{row.wns_ns:.6f}"
        whs = "NA" if row.whs_ns is None else f"{row.whs_ns:.6f}"
        period = 0.0 if row.period_ns is None else row.period_ns
        print(
            f"STA_PERCLOCK: name={name} period_ns={period:.6f} wns_ns={wns} whs_ns={whs} "
            f"setup_same_clock={int(row.setup_same_clock)}"
        )
    return bool(rows)


def emit_timing_markers(stdout: str, report_dir: Path) -> bool:
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
    present (each path's critical path and Fmax come from its own clocks, and
    only when it launches and captures on one clock), and the
    ``STA_REPORT``/``STA_CSV_REPORT`` pointers when *either* surfaced.  ``STA_REG2REG_REPORT`` points at the reg->reg path detail and is
    emitted only when that report was actually written — the Tcl skips it on a
    register-free design, and a dangling pointer reads worse than none.
    Returns True iff any timing datum was surfaced (so the caller only
    degrades/falls back when there is genuinely nothing).  (SETUP-29)
    """
    slack_ns = parse_sta_worst_slack(stdout)
    if slack_ns is None:
        # The CSV carries no clocks, so this slack yields no critical path or Fmax.
        slack_ns = parse_overall_csv_worst_slack(report_dir / "overall.csv.rpt")
    if slack_ns is not None:
        print_overall_fmax(slack_ns, parse_path_clocks(stdout, "STA_WORST_CLOCKS"))
    reg2reg_emitted = print_reg2reg_fmax(stdout)
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


def parse_sta_worst_slack(text: str) -> float | None:
    """Parse worst setup slack in ns from Booley's ``STA_WORST_SLACK_NS`` marker.

    Only the marker counts. OpenROAD's own log lines (for example GPL-0028's
    ``Bin count (X, Y): 4 , 4``) can look like numeric CSV rows, so scanning
    the log for them would invent a slack for a design with no timing paths.
    """
    marker = _STA_SLACK_RE.search(text)
    if marker is None:
        return None
    return float(marker.group(1))


def parse_overall_csv_worst_slack(path: Path) -> float | None:
    """Return the minimum slack in Booley's own ``overall.csv.rpt``, if any.

    The file is written by Booley's STA Tcl as ``startpoint,endpoint,slack``
    rows, so the third field of every row with at least three fields is a
    slack. A missing or empty file (STA found no paths) yields ``None``.
    """
    if not path.exists():
        return None
    values: list[float] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 3:
            with contextlib.suppress(ValueError):
                values.append(float(parts[2]))
    return min(values) if values else None
