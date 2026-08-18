"""OpenROAD timing engine for the builtin Yosys ASIC-synthesis flow.

Unlike the zero-RC OpenSTA path (:func:`syn_core.run_opensta`), this engine runs
a quick floorplan + global placement inside OpenROAD, buffers/sizes the netlist
(``repair_design``/``repair_timing``), estimates wire RC from placement, and then
reports timing through OpenROAD's embedded OpenSTA.  It emits the *same* stdout
markers (``STA_WORST_SLACK_NS:`` etc.) as ``run_opensta`` so ``_parse_synth_output``
and the criteria layer need no structural change — plus two informational area
markers.

Mirrors :func:`syn_core.run_opensta`'s warn-and-degrade shape: any missing
prerequisite (binary, netlist, clock, PDK) or a nonzero/unparseable run returns
``False`` so the caller can fall back to OpenSTA.  Imported lazily from
``run_yosys()``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

from booley.yosys import syn_core
from booley.yosys.syn_core import (
    DEFAULT_LIB_DIR,
    StaTimingConfig,
    detect_clock_port,
    find_eda_tool,
    run_cmd_watched,
    write_sta_sdc,
)

# Nangate45 physical conventions from the OpenROAD reference flow @ a008522d8.
_SITE = "FreePDK45_38x28_10R_NP_162NW_34O"
_SIGNAL_LAYER = "metal3"
_CLOCK_LAYER = "metal6"
_PIN_HOR_LAYER = "metal3"
_PIN_VER_LAYER = "metal2"
_DONT_USE = "CLKBUF_* AOI211_X1 OAI211_X1"
# Per-cell global-placement padding (sites each side).  Kept at 1 (not the
# ORFS-typical 2): padding inflates the placed utilization above the floorplan
# figure, and at 2 sites the inflation pushes a util=40 floorplan past the
# density cap (util/100 + 0.25 = 0.65) → GPL-0302 "use a higher -density".
_GP_PAD_SITES = 1

_AREA_RE = re.compile(
    r"Design\s+area\s+([\d.]+)\s*u\^2\s+([\d.]+)\s*%\s*utilization",
    re.IGNORECASE,
)
_PRE_REPAIR_SLACK_RE = re.compile(r"STA_PRE_REPAIR_WORST_SLACK_NS:\s*([-+]?\d+(?:\.\d+)?)")


class OpenRoadPdk(NamedTuple):
    """Physical files the OpenROAD timing engine needs alongside the liberty."""

    tech_lef: Path
    stdcell_lef: Path
    layer_rc: Path


def openroad_pdk_paths() -> OpenRoadPdk:
    """Expected locations of the setup-managed Nangate45 physical files.

    Root mirrors :func:`syn_core.resolve_liberty`: ``$PRJ_LIB_DIR`` if set, else
    the platform default lib dir (``/opt/pdk``); files live under
    ``<root>/nangate45/``. The generated make recipe probes their presence.
    """
    root = Path(os.environ.get("PRJ_LIB_DIR") or DEFAULT_LIB_DIR)
    pdk_dir = root / "nangate45"
    return OpenRoadPdk(
        tech_lef=pdk_dir / "Nangate45_tech.lef",
        stdcell_lef=pdk_dir / "Nangate45_stdcell.lef",
        layer_rc=pdk_dir / "Nangate45.rc",
    )


def resolve_openroad_pdk() -> OpenRoadPdk | None:
    """Locate the setup-managed Nangate45 files (see :func:`openroad_pdk_paths`).

    Returns ``None`` (with a WARNING) if any is missing, so the caller degrades
    to OpenSTA instead of crashing.
    """
    pdk = openroad_pdk_paths()
    missing = [p for p in pdk if not p.exists()]
    if missing:
        print("WARNING: OpenROAD PDK files not found: " + ", ".join(str(p) for p in missing))
        return None
    return pdk


def _repair_timing_tcl(config: StaTimingConfig) -> str:
    """Tcl for configured setup/hold repair, or ``""`` when disabled.

    OpenROAD prints no per-command banners (unlike Yosys's "Executing ..."
    lines), so the script announces each stage itself. The watchdog matches
    these BOOLEY_STAGE markers for stage timing and stall attribution — keep
    the names in sync with ``synthesis_watchdog.OPENROAD_STAGE_NAMES``.
    """
    if not _timing_repair_enabled(config):
        return ""
    commands = ['puts "BOOLEY_STAGE: repair_timing"']
    if config.repair_timing:
        commands.append(_repair_setup_command(config))
    if config.repair_hold:
        commands.append("repair_timing -hold")
    commands.append("detailed_placement ; estimate_parasitics -placement")
    return "\n".join(commands) + "\n"


def _timing_repair_enabled(config: StaTimingConfig) -> bool:
    """Whether either OpenROAD timing-repair phase is enabled."""
    return config.repair_timing or config.repair_hold


def _repair_setup_command(config: StaTimingConfig) -> str:
    """Build one OpenROAD setup-repair command from expert controls."""
    args = ["repair_timing", "-setup"]
    if config.setup_margin_ns > 0:
        args.extend(["-setup_margin", f"{config.setup_margin_ns:g}"])
    if config.repair_tns_percent is not None:
        args.extend(["-repair_tns", f"{config.repair_tns_percent:g}"])
    if not config.gate_cloning:
        args.append("-skip_gate_cloning")
    return " ".join(args)


def _pre_repair_snapshot_tcl(config: StaTimingConfig, pre_rpt: str, pre_csv: str) -> str:
    """Tcl snapshotting the placed (pre-repair) STA, or ``""`` when repair is off.

    ADR 0029 D2 (pre-repair salvage): when repair_timing runs, snapshot the
    placed (pre-repair) STA first — to its own pre_repair.{rpt,csv} + a
    distinct STA_PRE_REPAIR_WORST_SLACK_NS marker, flushed immediately. If the
    repair stage then stalls/errors/is-killed, ``run_openroad_timing``
    salvages these numbers instead of losing the whole run. Distinct marker
    names keep the post-repair report below canonical (no double-count in the
    min-slack parse). Omitted when repair is off — the single report is final.
    """
    if not _timing_repair_enabled(config):
        return ""
    return (
        'puts "BOOLEY_STAGE: sta_report_pre_repair"\n'
        f"report_checks -path_delay max -sort_by_slack -group_count 1 > {{{pre_rpt}}}\n"
        "set _pre_paths [find_timing_paths -path_delay max -sort_by_slack -group_count 1]\n"
        f"set _pre_csv [open {{{pre_csv}}} w]\n"
        "foreach _pp $_pre_paths {\n"
        "  set _slk [get_property $_pp slack]\n"
        '  puts $_pre_csv [format "%s,%s,%.6f" '
        "[get_property [get_property $_pp startpoint] full_name] "
        "[get_property [get_property $_pp endpoint] full_name] $_slk]\n"
        '  puts [format "STA_PRE_REPAIR_WORST_SLACK_NS: %.6f" $_slk]\n'
        "  break\n"
        "}\n"
        "close $_pre_csv\n"
        "flush stdout\n"
    )


def write_openroad_script(
    design_name: str,
    liberty: Path,
    sta_netlist: Path,
    sdc_path: Path,
    pdk: OpenRoadPdk,
    report_dir: Path,
    work_dir: Path,
    config: StaTimingConfig,
) -> Path:
    """Write the Tcl script driving OpenROAD placement + repair + timing report.

    Density is either the expert override or utilization plus buffering
    headroom (``min(0.80, util/100 + 0.25)``). Setup and hold repair are
    independently controlled by ``config.repair_timing`` and
    ``config.repair_hold``. No trailing ``exit`` — the
    ``-exit`` flag preserves the nonzero-on-error semantics we rely on for the
    OpenSTA fallback.  Reports land at the same ``overall{,.csv}.rpt`` paths as
    OpenSTA so ``STA_REPORT``/``STA_CSV_REPORT`` markers stay byte-compatible.
    """
    script_path = work_dir / "run_openroad.tcl"
    util = config.utilization_pct
    density = (
        config.placement_density
        if config.placement_density is not None
        else min(0.80, util / 100.0 + 0.25)
    )
    # cwd-relative, NOT work_dir-absolute: both callers run OpenROAD with
    # cwd == work_dir (the make-split recipe via `make -C`, the legacy inline
    # path via run_cmd_watched's cwd). Keeping this relative also makes the
    # generated plan relocatable within the Session Runtime workspace.
    out_verilog = f"openroad_{design_name}.v"
    rpt = (report_dir / "overall.rpt").as_posix()
    csv = (report_dir / "overall.csv.rpt").as_posix()
    reg2reg_block = syn_core.reg2reg_timing_tcl((report_dir / "reg2reg.rpt").as_posix())
    perclock_block = syn_core.perclock_timing_tcl()

    repair_timing_line = _repair_timing_tcl(config)
    pre_rpt = (report_dir / "pre_repair.rpt").as_posix()
    pre_csv = (report_dir / "pre_repair.csv.rpt").as_posix()
    pre_repair_block = _pre_repair_snapshot_tcl(config, pre_rpt, pre_csv)

    script = f"""
read_lef {{{pdk.tech_lef.as_posix()}}}
read_lef {{{pdk.stdcell_lef.as_posix()}}}
read_liberty {{{liberty.as_posix()}}}
read_verilog {{{sta_netlist.as_posix()}}}
link_design {design_name}
read_sdc {{{sdc_path.as_posix()}}}
catch {{ foreach_in_collection _clk [all_clocks] {{ puts [format "STA_CLOCK_PERIOD_NS: %.6f" [get_property $_clk period]] ; break }} }}
puts "BOOLEY_STAGE: floorplan"
initialize_floorplan -utilization {util:.3f} -aspect_ratio 1.0 -core_space 2.0 \\
  -site {_SITE}
make_tracks
remove_buffers
source {{{pdk.layer_rc.as_posix()}}}
set_wire_rc -signal -layer {_SIGNAL_LAYER}
set_wire_rc -clock -layer {_CLOCK_LAYER}
set_dont_use {{{_DONT_USE}}}
puts "BOOLEY_STAGE: global_placement"
global_placement -density {density:.3f} -pad_left {_GP_PAD_SITES} -pad_right {_GP_PAD_SITES} -skip_io
puts "BOOLEY_STAGE: place_pins"
place_pins -hor_layers {_PIN_HOR_LAYER} -ver_layers {_PIN_VER_LAYER}
puts "BOOLEY_STAGE: global_placement"
global_placement -density {density:.3f} -pad_left {_GP_PAD_SITES} -pad_right {_GP_PAD_SITES}
estimate_parasitics -placement
puts "BOOLEY_STAGE: repair_design"
repair_design
repair_tie_fanout -separation 5 LOGIC0_X1/Z
repair_tie_fanout -separation 5 LOGIC1_X1/Z
set_placement_padding -global -left 1 -right 1
puts "BOOLEY_STAGE: detailed_placement"
detailed_placement
estimate_parasitics -placement
{pre_repair_block}{repair_timing_line}puts "BOOLEY_STAGE: sta_report"
report_design_area
write_verilog {{{out_verilog}}}
report_checks -path_delay max -sort_by_slack -group_count 1 > {{{rpt}}}
set paths [find_timing_paths -path_delay max -sort_by_slack -group_count 1]
set csv_out [open {{{csv}}} w]
foreach path $paths {{
  set startpoint_name [get_property [get_property $path startpoint] full_name]
  set endpoint_name [get_property [get_property $path endpoint] full_name]
  set slack [get_property $path slack]
  puts $csv_out [format "%s,%s,%.6f" $startpoint_name $endpoint_name $slack]
  puts [format "STA_WORST_SLACK_NS: %.6f" $slack]
  break
}}
close $csv_out
{perclock_block}{reg2reg_block}""".lstrip()
    script_path.write_text(script, encoding="utf-8")
    return script_path


def parse_openroad_area(text: str) -> tuple[float | None, float | None]:
    """Parse ``report_design_area`` output → (area_um2, utilization_pct).

    Matches e.g. ``Design area 235 u^2 33% utilization.``  Returns ``(None,
    None)`` when the line is absent or unparseable — area is informational and
    must never fail the timing run.
    """
    m = _AREA_RE.search(text)
    if not m:
        return None, None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None, None


def _salvage_pre_repair(
    config: StaTimingConfig,
    report_dir: Path,
    stdout: str,
) -> bool:
    """Emit the pre-repair placed STA as a salvaged result (ADR 0029 D2).

    Reads the pre-repair worst slack from the stdout marker (or, failing that,
    the ``pre_repair.csv.rpt`` OpenROAD wrote before the repair stage) and
    re-emits the canonical ``STA_*`` markers so a killed/failed repair still
    yields actionable numbers — fronted by an ``STA_REPAIR_INCOMPLETE`` line the
    report layer surfaces as a warning. Returns True when numbers were salvaged.
    """
    match = _PRE_REPAIR_SLACK_RE.search(stdout)
    slack_ns = float(match.group(1)) if match else None
    if slack_ns is None:
        slack_ns = syn_core.parse_sta_worst_slack(report_dir / "pre_repair.csv.rpt")
    if slack_ns is None:
        return False
    period_ps = syn_core.effective_period_ps(config, stdout)
    critical_path_ps = period_ps - (slack_ns * 1000.0)
    fmax_mhz = 1_000_000.0 / critical_path_ps if critical_path_ps > 0 else None
    print("STA_REPAIR_INCOMPLETE: repair_timing did not complete; reporting pre-repair placed STA")
    print(f"STA_WORST_SLACK_NS: {slack_ns:.6f}")
    print(f"STA_CRITICAL_PATH_PS: {critical_path_ps:.3f}")
    if fmax_mhz is not None:
        print(f"STA_FMAX_MHZ: {fmax_mhz:.3f}")
    print(f"STA_REPORT: {report_dir / 'pre_repair.rpt'}")
    print(f"STA_CSV_REPORT: {report_dir / 'pre_repair.csv.rpt'}")
    return True


class _OpenRoadRunInputs(NamedTuple):
    openroad: str
    sta_netlist: Path
    clock: str
    pdk: object


def _resolve_openroad_run_inputs(
    design_name: str,
    work_dir: Path,
    config: StaTimingConfig,
) -> _OpenRoadRunInputs | None:
    """Resolve the binary/netlist/clock/PDK prerequisites for an OpenROAD run.

    Prints the same WARNING and returns ``None`` for any missing prerequisite,
    mirroring :func:`syn_core.run_opensta`'s warn-and-degrade shape so the
    caller can fall back to OpenSTA.
    """
    openroad = find_eda_tool("openroad")
    if not openroad:
        print("WARNING: OpenROAD requested but 'openroad' is not on PATH")
        return None

    sta_netlist = work_dir / f"sta_{design_name}.v"
    if not sta_netlist.exists():
        print(f"WARNING: OpenROAD netlist missing: {sta_netlist}")
        return None

    clock = (
        config.clock or detect_clock_port(sta_netlist) or syn_core._first_authored_clock(config)
    )
    if not clock:
        print("WARNING: OpenROAD requested but no clock port was configured or detected")
        return None

    pdk = resolve_openroad_pdk()
    if pdk is None:
        return None

    return _OpenRoadRunInputs(openroad, sta_netlist, clock, pdk)


def _print_openroad_area_markers(stdout: str) -> None:
    """Print the informational OPENROAD_DESIGN_AREA_UM2/UTILIZATION_PCT markers.

    Placed area is independent of the timing path group, so it is printed
    even when only reg->reg (or nothing) timing surfaced.
    """
    area_um2, utilization_pct = parse_openroad_area(stdout)
    if area_um2 is not None:
        print(f"OPENROAD_DESIGN_AREA_UM2: {area_um2:.3f}")
    if utilization_pct is not None:
        print(f"OPENROAD_UTILIZATION_PCT: {utilization_pct:.3f}")


def run_openroad_timing(
    design_name: str,
    liberty: Path,
    work_dir: Path,
    config: StaTimingConfig,
) -> bool:
    """Run OpenROAD placement + repair + timing; print stable markers.

    Returns ``True`` on success (markers emitted), ``False`` on any degrade
    (missing binary/netlist/clock/PDK, nonzero rc, unparseable slack) so the
    caller can fall back to OpenSTA.  Reuses the OpenSTA SDC verbatim so the two
    engines constrain identically.
    """
    inputs = _resolve_openroad_run_inputs(design_name, work_dir, config)
    if inputs is None:
        return False
    openroad, sta_netlist, clock, pdk = inputs

    report_dir = work_dir / "reports" / "timing"
    report_dir.mkdir(parents=True, exist_ok=True)
    sdc_path = write_sta_sdc(config, clock, work_dir)
    script_path = write_openroad_script(
        design_name,
        liberty,
        sta_netlist,
        sdc_path,
        pdk,
        report_dir,
        work_dir,
        config,
    )
    try:
        result = run_cmd_watched(
            [str(openroad), "-no_init", "-exit", str(script_path)],
            "OpenROAD: Placement + repair + static timing",
            work_dir,
            log_file="openroad.log",
            heartbeat_interval=60,
            poll_interval=10,
            # Own file — the default name would clobber Yosys's stage timings.
            timings_filename="stage_timings_openroad.json",
        )
    except subprocess.CalledProcessError as exc:
        print(f"WARNING: OpenROAD failed with code {exc.returncode}; timing unavailable")
        # ADR 0029 D2: a nonzero exit during/after repair may still have emitted
        # the pre-repair placed STA — salvage it rather than lose the whole run.
        # Only meaningful when repair was on (that is the only path that writes a
        # pre-repair snapshot); a repair-off failure is a genuine dead end.
        return bool(
            _timing_repair_enabled(config)
            and _salvage_pre_repair(config, report_dir, exc.output or "")
        )

    # Surface overall AND reg->reg markers independently: an I/O-bound /
    # false-pathed overall worst path leaves the overall query empty, but the
    # internal reg->reg Fmax is still valid and must not be dropped — nor should
    # its absence force a needless OpenSTA fallback (SETUP-29). emit_timing_markers
    # recovers the effective period from the Target SDC (ADR 0029 decision 6).
    surfaced = syn_core.emit_timing_markers(result.stdout, config, report_dir)

    _print_openroad_area_markers(result.stdout)

    if not surfaced:
        # No overall AND no reg->reg timing at all — a genuine degrade.
        print("WARNING: OpenROAD completed but no timing path slack was reported")
        # ADR 0029 D2: repair may have wedged silently and left a pre-repair
        # placed-STA snapshot — salvage it; else return False so run_yosys falls
        # back to the OpenSTA path (which retries timing on the same netlist).
        return bool(
            _timing_repair_enabled(config)
            and _salvage_pre_repair(config, report_dir, result.stdout)
        )
    return True
