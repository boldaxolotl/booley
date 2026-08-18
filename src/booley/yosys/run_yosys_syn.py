#!/usr/bin/env python3
"""
Yosys Synthesis Script for RTL projects.
Runs sv2v + Yosys for area estimation with parallel-safe isolated work directories.

CLI and orchestration layer — execution logic in syn_core.py.

Two execution shapes live behind this CLI (ADR 0037 §8):

* ``run`` — the legacy in-process path: sv2v/yosys/STA are spawned directly
  with the stall-detecting synthesis watchdog attached.
* ``configure`` — renders the scripts plus a generated ``Makefile`` (see
  :mod:`booley.yosys.syn_make`) and stops. Execution is then a plain
  ``make -C <build dir>`` in the Session Runtime; timeout enforcement is the caller's budget and stage
  attribution comes from the BOOLEY_STAGE markers in the captured log.

The builtin ``asic_synthesize`` Flow uses the configure half in-process (it
never spawns this CLI); the ``run`` surface remains for legacy non-FuseSoC
callers.
"""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

from booley.core.boundary import BoundaryError

# ============================================================================
# Configuration
# ============================================================================

# Predefined configurations — imported from shared module (in parent dir)
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR.parent.parent))
from booley.runtime.heartbeat import fmt_elapsed as fmt_time
from booley.runtime.shared_infra import (
    check_paths as _check_paths,
)
from booley.synthesis.profiles import DEFAULT_PPA_PROFILE, PPA_PROFILE_CHOICES
from booley.yosys import ppa as ppa_options
from booley.yosys import syn_make
from booley.yosys.syn_core import (  # Core synthesis functions
    FRONTEND_CHOICES,
    PROJECT_ROOT,
    RTL_DIR,
    SYN_DIR,
    TIMING_ENGINE_CHOICES,
    StaTimingConfig,
    area_to_kge,
    parse_area_from_stat,
    parse_params,
    prepare_work_dir,
    resolve_liberty,
    run_sv2v,
    run_yosys,
    scan_synth_logs,
    synth_timing_config,
)

# Standalone-synthesis result dirs land under the transient runtime tree, NOT
# the design repo's ``util/syn/`` namespace (which the project may legitimately
# own) — this mirrors the Edalize build dirs at
# ``.booley_project/.runtime/edalize/...``. Keyed on PROJECT_ROOT (``/work`` in
# the sandbox) so it crosses the host/container boundary unchanged, and
# ``.booley_project/.runtime/`` is already git-ignored and skipped by the
# workspace-isolation scanner. (SETUP-27)
SYN_RESULT_ROOT = PROJECT_ROOT / ".booley_project" / ".runtime" / "syn" / "syn_result"

# ============================================================================
# High-level actions
# ============================================================================


def _resolve_syn_sources(args: argparse.Namespace) -> list[Path]:
    """Validate standalone-synthesis prerequisites and resolve include dirs.

    RTL is supplied via ``--extra-rtl`` (the FuseSoC synth path forwards the
    resolved filelist) with ``-t/--top`` naming the design; the caller reads
    both straight off ``args``. Returns the resolved include dirs — the only
    value this function actually computes.
    """
    if not args.extra_rtl:
        sys.exit("ERROR: --extra-rtl is required (use with -t/--top to name the design).")
    if not args.top:
        sys.exit("ERROR: -t/--top is required.")

    return _resolve_inc_dirs(args)


def _resolve_extra_rtl(args: argparse.Namespace, root: Path | None = None) -> list[Path]:
    """Resolve and validate extra RTL files from CLI.

    Relative paths resolve against *root* (default ``PROJECT_ROOT`` — ``/work``
    inside the sandbox); the in-process configure half (ADR 0037 §8) passes the
    Flow's work_dir explicitly instead of relying on the import-time constant.
    """
    base = root if root is not None else PROJECT_ROOT
    extra = []
    if args.extra_rtl:
        for f in args.extra_rtl:
            p = Path(f)
            p = (base / p).resolve() if not p.is_absolute() else p.resolve()
            if not p.exists():
                sys.exit(f"ERROR: Extra RTL file not found: {p}")
            extra.append(p)
    return extra


def _resolve_inc_dirs(args: argparse.Namespace, root: Path | None = None) -> list[Path]:
    """Resolve include directories from CLI.

    Relative paths resolve against *root* (default ``PROJECT_ROOT``, ``/work``
    inside the sandbox), so a path the caller relativized against the worktree
    crosses the host/sandbox boundary unchanged — mirroring
    :func:`_resolve_extra_rtl`. The FuseSoC synth path (asic_synthesize) passes
    the resolved ``rtl_include_dirs`` here.
    """
    base = root if root is not None else PROJECT_ROOT
    inc_dirs: list[Path] = []
    for d in getattr(args, "inc_dir", []) or []:
        p = Path(d)
        p = (base / p).resolve() if not p.is_absolute() else p.resolve()
        if not p.exists():
            sys.exit(f"ERROR: Include directory not found: {p}")
        inc_dirs.append(p)
    return inc_dirs


def _resolve_syn_workdir(
    args: argparse.Namespace, design_name: str, params: dict[str, str]
) -> Path:
    """Compute work directory for this synthesis run."""
    if args.workdir:
        return SYN_RESULT_ROOT / args.workdir
    suffix = "_".join(f"{k}{v}" for k, v in params.items()) if params else ""
    dir_name = f"standalone.{design_name}" + (f".{suffix}" if suffix else "")
    return SYN_RESULT_ROOT / dir_name


def _print_syn_report(
    work_dir: Path,
    design_name: str,
    t_sv2v: float,
    t_yosys: float,
    t_total: float,
    wd_result: object,
) -> None:
    """Print synthesis results, area, timing, and watchdog summary."""
    stat_file = work_dir / f"stat_{design_name}.txt"
    area = parse_area_from_stat(stat_file)

    print(f"\n{'=' * 60}")
    print("Synthesis complete!")
    print(f"{'=' * 60}")
    print(f"Results: {work_dir}")

    if stat_file.exists():
        print(f"\n--- Area Report ({stat_file.name}) ---")
        print(stat_file.read_text(encoding="utf-8"))
    kge = area_to_kge(area)
    if kge is not None:
        print(f"Gate count: {kge:.1f} kGE (NAND2 equivalent)")

    print("--- Timing ---")
    print(f"sv2v:  {fmt_time(t_sv2v)}")
    print(f"Yosys: {fmt_time(t_yosys)}")
    print(f"Total: {fmt_time(t_total)}")

    if wd_result:
        _print_watchdog_summary(wd_result)


def _print_watchdog_summary(wd_result: object) -> None:
    """Print watchdog metrics if available."""
    print("\n--- Watchdog ---")
    if wd_result.peak_rss_mb:
        print(f"Peak RSS:  {wd_result.peak_rss_mb:.0f} MB")
    if wd_result.max_stall_s > 0:
        print(f"Max stall: {wd_result.max_stall_s:.0f}s (during {wd_result.max_stall_stage})")
    if wd_result.memory_growth_rate_mb_per_min is not None:
        print(f"Mem growth: {wd_result.memory_growth_rate_mb_per_min:.1f} MB/min")
    stages = {k: v for k, v in wd_result.stage_timings.items() if not k.startswith("_")}
    if stages:
        print("\nStage breakdown:")
        for name, st in stages.items():
            dur = st.get("duration_s")
            if dur is not None:
                print(f"  {name:<20} {dur:>8.1f}s")


def _print_syn_config(
    args: argparse.Namespace,
    design_name: str,
    liberty: Path,
    extra_files: list[Path],
    timing_engine: str,
) -> None:
    """Print synthesis configuration banner.

    ``design_name``, ``liberty``, ``extra_files``, and ``timing_engine`` are
    derived/computed values not directly on ``args`` (top defaults to a CLI
    flag but ``design_name`` is validated/resolved beforehand; ``liberty`` is
    resolved via ``resolve_liberty``; ``timing_engine`` is the resolved
    ``timing.engine``, distinct from the raw ``args.timing_engine`` override).
    Everything else is read straight off ``args`` — the single call site
    (standalone synthesis) always used ``"standalone"`` as the config label.
    """
    params = parse_params(args.param)
    print("Yosys Synthesis Script")
    print(f"Project root: {PROJECT_ROOT}")
    print("Config: standalone")
    print(f"Frontend: {args.frontend}")
    print(f"Top module: {design_name}")
    print(f"Defines: {list(args.define)}")
    print(f"Liberty: {liberty}")
    print(f"Flatten: {args.flatten}")
    if params:
        print(f"Parameters: {params}")
    if extra_files:
        print(f"Extra RTL: {[str(f) for f in extra_files]}")
    print(f"SDC: {args.sdc or 'disabled'}")
    if args.sdc and getattr(args, "abc_delay_ps", None) is not None:
        print(f"ABC delay target: {args.abc_delay_ps} ps")
    print(f"Timing engine: {timing_engine}")


# SETUP-26 source-provenance helpers moved to syn_make (the interpret half of
# the ADR 0037 boundary split reuses them); re-exported here for the legacy
# print-based surface and its tests.
_converted_lineref = syn_make._converted_lineref
_enclosing_module = syn_make._enclosing_module
_source_file_for_module = syn_make._source_file_for_module


def _report_source_provenance(work_dir: Path, files: list[Path]) -> None:
    """Print a provenance hint when a yosys error references the sv2v output.

    See :func:`booley.yosys.syn_make._source_provenance_text` — no-op when the
    failure isn't about the concatenated sv2v output. (SETUP-26)
    """
    text = syn_make._source_provenance_text(work_dir, files)
    if text:
        print(text)


def _resolve_ppa_settings(
    args: argparse.Namespace,
) -> tuple[str, ppa_options.YosysPpaSettings, ppa_options.OpenRoadPpaSettings]:
    """Resolve a generic profile plus explicit backend overrides."""
    profile = getattr(args, "ppa_profile", None) or DEFAULT_PPA_PROFILE
    try:
        yosys = ppa_options.with_yosys_overrides(
            ppa_options.yosys_profile(profile),
            abc_recipe=getattr(args, "abc_recipe", None),
            abc_script=getattr(args, "abc_script", None),
            generic_abc_before_mapping=getattr(args, "generic_abc_before_mapping", None),
            abc_delay_ps=getattr(args, "abc_delay_ps", None),
        )
    except BoundaryError as exc:
        raise SystemExit(f"ERROR: {exc}") from None
    openroad = ppa_options.with_openroad_overrides(
        ppa_options.openroad_profile(profile),
        **_openroad_cli_overrides(args),
    )
    _validate_resolved_ppa(yosys, openroad)
    return profile, yosys, openroad


def _openroad_cli_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Collect modern OpenROAD flags and the legacy repair alias."""
    repair_setup = getattr(args, "repair_setup", None)
    if repair_setup is None:
        repair_setup = getattr(args, "repair_timing", None)
    return {
        "utilization_pct": getattr(args, "utilization_pct", None),
        "placement_density": getattr(args, "placement_density", None),
        "repair_setup": repair_setup,
        "repair_hold": getattr(args, "repair_hold", None),
        "gate_cloning": getattr(args, "gate_cloning", None),
        "setup_margin_ns": getattr(args, "setup_margin_ns", None),
        "repair_tns_percent": getattr(args, "repair_tns_percent", None),
    }


def _validate_resolved_ppa(
    yosys: ppa_options.YosysPpaSettings,
    openroad: ppa_options.OpenRoadPpaSettings,
) -> None:
    """Range-check resolved PPA values before rendering backend commands."""
    if yosys.abc_delay_ps is not None and yosys.abc_delay_ps <= 0:
        raise SystemExit("ERROR: abc_delay_ps must be greater than zero")
    numeric = {
        "utilization_pct": openroad.utilization_pct,
        "placement_density": openroad.placement_density,
        "setup_margin_ns": openroad.setup_margin_ns,
        "repair_tns_percent": openroad.repair_tns_percent,
    }
    for name, value in numeric.items():
        if value is not None and not math.isfinite(value):
            raise SystemExit(f"ERROR: {name} must be a finite number")
    if not 0 < openroad.utilization_pct < 100:
        raise SystemExit("ERROR: utilization_pct must be between 0 and 100")
    if not 0 < openroad.placement_density <= 1:
        raise SystemExit("ERROR: placement_density must be in the range (0, 1]")
    if openroad.setup_margin_ns < 0:
        raise SystemExit("ERROR: setup_margin_ns must be non-negative")
    if openroad.repair_tns_percent is not None and not 0 <= openroad.repair_tns_percent <= 100:
        raise SystemExit("ERROR: repair_tns_percent must be between 0 and 100")


def _resolve_syn_timing(
    args: argparse.Namespace,
    openroad: ppa_options.OpenRoadPpaSettings,
    project_root: Path | None = None,
) -> StaTimingConfig:
    """Resolve the STA timing configuration for this run (ADR 0031 P1).

    A synthesis run with no timing constraints must name its clock explicitly
    - either a file_type:SDC fileset (forwarded as one or more --sta-sdc), an
    explicit --period-ps, or the named --default-clock opt-in. The old silent
    DEFAULT_STA_PERIOD_PS (~250 MHz) fallback re-buried the exact input the
    whole measurement hangs on: a Target that dropped its SDC would report a
    green PPA number against a period no one chose, indistinguishable from a
    deliberately-constrained run.
    """
    default_clock_ps = getattr(args, "default_clock", None)
    if not args.sta_sdc and args.period_ps is None:
        if default_clock_ps is None:
            sys.exit(
                "ERROR: no timing constraints for this synthesis run. Provide a "
                "file_type:SDC fileset (create_clock / set_input_delay / "
                "set_output_delay / set_false_path, forwarded as --sta-sdc), an "
                "explicit --period-ps, or the named --default-clock <ps> opt-in. "
                "Refusing to fabricate a default clock silently — a reported Fmax "
                "would be measured against a period no one chose."
            )
        # Explicit opt-in: the canned clock is now chosen and named, never implicit.
        resolved_period_ps = default_clock_ps
    else:
        resolved_period_ps = args.period_ps
    # The standalone CLI historically read utilization/repair from the legacy
    # [flows.synth.timing] table. Ask synth_timing_config to honor those keys
    # only when no generic profile was explicit. The Booley Flow always forwards its
    # selected profile, so its deterministic profile semantics are unaffected.
    profile_explicit = getattr(args, "ppa_profile", None) is not None
    legacy_utilization_fallback = (
        not profile_explicit and getattr(args, "utilization_pct", None) is None
    )
    legacy_repair_fallback = (
        not profile_explicit
        and getattr(args, "repair_setup", None) is None
        and getattr(args, "repair_timing", None) is None
    )
    return synth_timing_config(
        engine=args.timing_engine,
        clock=args.clock,
        period_ps=resolved_period_ps,
        input_delay_pct=args.input_delay_pct,
        output_delay_pct=args.output_delay_pct,
        sdc=args.sta_sdc,
        utilization_pct=openroad.utilization_pct,
        repair_timing=openroad.repair_setup,
        placement_density=openroad.placement_density,
        repair_hold=openroad.repair_hold,
        gate_cloning=openroad.gate_cloning,
        setup_margin_ns=openroad.setup_margin_ns,
        repair_tns_percent=openroad.repair_tns_percent,
        legacy_utilization_fallback=legacy_utilization_fallback,
        legacy_repair_fallback=legacy_repair_fallback,
        project_root=project_root,
    )


def _run_sv2v_and_yosys(
    args: argparse.Namespace,
    design_name: str,
    liberty: Path,
    work_dir: Path,
    files: list[Path],
    inc_dirs: list[Path],
    defines: list[str],
    params: dict[str, str],
    timing: StaTimingConfig,
) -> tuple[object, float, float]:
    """Run the sv2v -> yosys pipeline; exits the process on a yosys failure.

    Returns ``(wd_result, t_sv2v, t_yosys)`` for the caller's final report.
    With ``--frontend slang`` the sv2v transpile is skipped entirely (Yosys
    0.67 reads the raw SystemVerilog natively via ``read_slang``), so ``t_sv2v``
    is 0 and the raw sources / include dirs / defines are fed straight to Yosys.
    """
    frontend = args.frontend
    # sv2v path: transpile to a single Verilog file, then read_verilog it.
    # slang path: no transpile; read_slang consumes the raw sources directly.
    t_sv2v = 0.0
    if frontend == "sv2v":
        t_sv2v_start = time.monotonic()
        yosys_sources = [run_sv2v(files, inc_dirs, defines, work_dir)]
        t_sv2v = time.monotonic() - t_sv2v_start
        # sv2v already inlined includes and applied defines, so Yosys reads a
        # self-contained file — no include dirs / defines forwarded onward.
        yosys_inc_dirs: list[Path] = []
        yosys_defines: list[str] = []
    else:
        print("FRONTEND: SLANG (read_slang; sv2v skipped)")
        yosys_sources = files
        yosys_inc_dirs = inc_dirs
        yosys_defines = defines

    profile, yosys_ppa, _openroad_ppa = _resolve_ppa_settings(args)
    abc_recipe = yosys_ppa.abc_recipe
    if abc_recipe == "default":
        abc_recipe = None
        print("ABC RECIPE: DEFAULT (YOSYS BUILT-IN)")
    elif abc_recipe:
        print(f"ABC RECIPE: {abc_recipe.upper()}")
    print(f"PPA PROFILE: {profile}")

    t_yosys_start = time.monotonic()
    try:
        wd_result = run_yosys(
            yosys_sources,
            design_name,
            liberty,
            work_dir,
            flatten=args.flatten,
            sdc=args.sdc,
            tdelay=yosys_ppa.abc_delay_ps,
            params=params,
            abc_recipe=abc_recipe,
            timing_config=timing,
            frontend=frontend,
            inc_dirs=yosys_inc_dirs,
            defines=yosys_defines,
            slang_options=list(getattr(args, "slang_option", []) or []),
            generic_abc_before_mapping=yosys_ppa.generic_abc_before_mapping,
            abc_script=yosys_ppa.abc_script,
        )
    except subprocess.CalledProcessError as e:
        _report_source_provenance(work_dir, files)
        sys.exit(e.returncode)
    t_yosys = time.monotonic() - t_yosys_start
    return wd_result, t_sv2v, t_yosys


def do_run(args: argparse.Namespace) -> None:
    """Full synthesis flow: sv2v -> yosys."""
    inc_dirs = _resolve_syn_sources(args)
    design_name = args.top
    defines = list(args.define)
    liberty = resolve_liberty(args.liberty)

    extra_files = _resolve_extra_rtl(args)
    files = list(extra_files)

    params = parse_params(args.param)
    _profile, _yosys_ppa, openroad_ppa = _resolve_ppa_settings(args)
    timing = _resolve_syn_timing(args, openroad_ppa)
    _print_syn_config(args, design_name, liberty, extra_files, timing.engine)

    work_dir = _resolve_syn_workdir(args, design_name, params)
    prepare_work_dir(work_dir)

    t_total_start = time.monotonic()
    wd_result, t_sv2v, t_yosys = _run_sv2v_and_yosys(
        args,
        design_name,
        liberty,
        work_dir,
        files,
        inc_dirs,
        defines,
        params,
        timing,
    )
    t_total = time.monotonic() - t_total_start

    # False-pass guard: yosys/ABC can emit ERROR: lines yet exit 0. Catch those
    # so a partial-fail doesn't masquerade as a successful synthesis.
    err_line = scan_synth_logs(work_dir)
    if err_line:
        print(f"\nERROR: synthesis log reports an error despite exit 0:\n  {err_line}")
        _report_source_provenance(work_dir, files)
        sys.exit(1)

    _print_syn_report(work_dir, design_name, t_sv2v, t_yosys, t_total, wd_result)


def resolve_spec(
    args: argparse.Namespace,
    *,
    project_root: Path | None = None,
    require_liberty: bool = True,
) -> syn_make.SynthSpec:
    """Resolve the parsed ``run``/``configure`` options into a :class:`SynthSpec`.

    The pure-resolution front of :func:`do_run`, shared with the boundary
    split (ADR 0037 §8): sources / include dirs / SDC files are validated here
    (they live in the shared workspace and must exist at configure time), the
    timing config is resolved, and the liberty path is computed.

    *project_root* anchors relative paths and the booley.toml lookup
    (``None`` = the legacy import-time ``PROJECT_ROOT``). *require_liberty*
    keeps the legacy hard "Liberty file not found" error for runs that execute
    immediately; ``False`` permits callers to render a diagnostic plan.

    Exits with an ``ERROR:`` message on any validation failure, exactly like
    the legacy CLI path; in-process callers catch ``SystemExit``.
    """
    from booley.yosys.syn_discovery import resolve_liberty_lenient

    if not args.extra_rtl:
        sys.exit("ERROR: --extra-rtl is required (use with -t/--top to name the design).")
    if not args.top:
        sys.exit("ERROR: -t/--top is required.")
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    sources = tuple(_resolve_extra_rtl(args, root))
    inc_dirs = tuple(_resolve_inc_dirs(args, root))
    profile, yosys_ppa, openroad_ppa = _resolve_ppa_settings(args)
    timing = _resolve_syn_timing(args, openroad_ppa, project_root=project_root)

    if require_liberty:
        liberty, liberty_found = resolve_liberty(args.liberty), True
    else:
        liberty, liberty_found = resolve_liberty_lenient(args.liberty)

    # The legacy ABC-mode --sdc knob: existence-checked as config validation
    # (relative to the project root, mirroring the subprocess cwd it used to
    # resolve against). It has no effect on the rendered scripts — the current
    # yosys script never consumed it beyond this check.
    if args.sdc:
        sdc_path = Path(args.sdc)
        sdc_resolved = sdc_path if sdc_path.is_absolute() else root / sdc_path
        if not sdc_resolved.resolve().exists():
            sys.exit(f"ERROR: SDC constraints file not found: {sdc_resolved.resolve()}")

    abc_recipe = None if yosys_ppa.abc_recipe == "default" else yosys_ppa.abc_recipe
    return syn_make.SynthSpec(
        design_name=args.top,
        sources=sources,
        inc_dirs=inc_dirs,
        defines=tuple(args.define),
        params=parse_params(args.param),
        liberty=liberty,
        liberty_found=liberty_found,
        flatten=args.flatten,
        abc_recipe=abc_recipe,
        frontend=args.frontend,
        timing=timing,
        slang_options=tuple(getattr(args, "slang_option", []) or []),
        ppa_profile=profile,
        generic_abc_before_mapping=yosys_ppa.generic_abc_before_mapping,
        abc_script=yosys_ppa.abc_script,
        abc_delay_ps=yosys_ppa.abc_delay_ps,
    )


def parse_run_argv(cmd: list[str]) -> argparse.Namespace:
    """Parse a run_yosys_syn argv (as built by asic_synthesize) back into options.

    The builtin asic_synthesize path keeps building the full spec argv
    (``python3 -m booley.yosys.run_yosys_syn run …``) — it is the validated,
    golden-snapshotted carrier of every recipe knob — but under ADR 0037 §8 it
    is parsed back here in-process instead of being executed as a subprocess.
    Accepts the argv with or without the ``python3 -m <module>`` prefix.
    """
    tokens = list(cmd)
    if tokens[:3] == ["python3", "-m", "booley.yosys.run_yosys_syn"]:
        tokens = tokens[3:]
    return _build_parser().parse_args(tokens)


def do_configure(args: argparse.Namespace) -> Path:
    """CLI ``configure`` action: render the make-driven build dir and stop.

    The configure half of the ADR 0037 §8 split, exposed standalone: after it
    returns, ``make -C <printed dir>`` runs the whole flow with only the EDA
    binaries on PATH (no Booley, no in-process watchdog — the BOOLEY_STAGE
    markers in the captured log carry stage attribution instead).
    """
    spec = resolve_spec(args)
    work_dir = _resolve_syn_workdir(args, args.top, spec.params)
    plan = syn_make.configure_synthesis(spec, work_dir)
    for warning in plan.warnings:
        print(f"WARNING: {warning}")
    print(f"Configured: {work_dir}")
    print(f"Run: make -C {work_dir}")
    return work_dir


def do_check_paths() -> None:
    """Verify PROJECT_ROOT and key directories resolve correctly. JSON report."""
    _check_paths(PROJECT_ROOT, {"rtl": RTL_DIR, "syn": SYN_DIR})


def do_clean() -> None:
    """Remove all synthesis result directories."""
    print("Cleaning synthesis results...")
    result_root = SYN_RESULT_ROOT
    if result_root.exists():
        shutil.rmtree(result_root)
        print(f"  Removed: {result_root}")
    else:
        print("  Nothing to clean.")
    print("Clean complete.")


# ============================================================================
# Main
# ============================================================================


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for Yosys synthesis."""
    parser = argparse.ArgumentParser(
        description="Yosys synthesis runner with parallel-safe isolated work directories"
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="run",
        choices=["run", "configure", "clean", "check-paths"],
        help="Action to perform (default: run). 'configure' renders the "
        "scripts + Makefile for a make-driven run (ADR 0037) without "
        "executing anything.",
    )
    parser.add_argument("-t", "--top", help="Top-level module name for the standalone design")
    parser.add_argument(
        "-d",
        "--define",
        action="append",
        default=[],
        help="Add preprocessor define (can use multiple times)",
    )
    parser.add_argument(
        "--liberty",
        help="Path to liberty library file (default: auto-resolved from PRJ_LIB_DIR or C:/tools)",
    )
    parser.add_argument(
        "-w",
        "--workdir",
        help="Override work directory name (default: auto-derived from config/top/defines)",
    )
    # ``action="extend"`` (not the argparse default) so that *repeated* flags
    # accumulate: the asic_synthesize wrapper emits one ``--extra-rtl <file>``
    # per resolved source. With a plain ``nargs="+"`` each occurrence would
    # OVERWRITE the previous, silently dropping every source but the last and
    # synthesizing an all-but-empty design. ``extend`` also accepts the batched
    # ``--extra-rtl a b c`` form, so both invocation styles are correct.
    parser.add_argument(
        "--extra-rtl",
        action="extend",
        nargs="+",
        default=[],
        metavar="FILE",
        help="Extra RTL files to include (not in file lists; "
        "repeatable — repeated flags accumulate)",
    )
    parser.add_argument(
        "--inc-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Include directory for sv2v/yosys (repeatable; "
        "the FuseSoC synth path forwards resolved include "
        "dirs here)",
    )
    parser.add_argument(
        "--ppa-profile",
        choices=list(PPA_PROFILE_CHOICES),
        default=None,
        help="EDA-tool-independent PPA intent (default: balanced). Backend-specific "
        "flags may override individual profile settings.",
    )
    parser.add_argument(
        "--flatten",
        action="store_true",
        default=True,
        help="Flatten design hierarchy before technology mapping (default: True)",
    )
    parser.add_argument(
        "--no-flatten", action="store_false", dest="flatten", help="Disable hierarchy flattening"
    )
    parser.add_argument(
        "-p",
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Set top-level parameter (e.g. -p OP_W=32 -p DEPTH=4)",
    )
    default_sdc = str(Path(__file__).resolve().parent / "sdc" / "abc_simple.sdc")
    parser.add_argument(
        "--sdc",
        default=default_sdc,
        help="Path to SDC timing constraints file. Default: abc_simple.sdc",
    )
    parser.add_argument(
        "--no-sdc",
        action="store_const",
        const=None,
        dest="sdc",
        help="Disable SDC timing constraints",
    )
    parser.add_argument(
        "--abc-delay-ps",
        "--tdelay",
        dest="abc_delay_ps",
        type=int,
        default=None,
        help="Expert Yosys override: ABC delay target in ps (--tdelay is a legacy alias)",
    )
    parser.add_argument(
        "--abc-recipe",
        choices=["fast", "balanced", "default"],
        default=None,
        help="Expert Yosys override for the profile's named ABC recipe",
    )
    parser.add_argument(
        "--abc-script",
        default=None,
        help="Expert Yosys override: raw ABC +script (mutually exclusive with --abc-recipe)",
    )
    parser.add_argument(
        "--generic-abc-before-mapping",
        action="store_true",
        dest="generic_abc_before_mapping",
        default=None,
        help="Expert compatibility override: let synth run generic ABC before liberty mapping",
    )
    parser.add_argument(
        "--no-generic-abc-before-mapping",
        action="store_false",
        dest="generic_abc_before_mapping",
        help="Use one liberty-aware ABC mapping pass (profile default)",
    )
    parser.add_argument(
        "--frontend",
        choices=list(FRONTEND_CHOICES),
        default="sv2v",
        help="RTL frontend: 'sv2v' transpiles SystemVerilog then read_verilog "
        "(default); 'slang' reads SystemVerilog natively via Yosys read_slang "
        "(requires Yosys >=0.67, no sv2v step).",
    )
    parser.add_argument(
        "--slang-option",
        action="append",
        default=[],
        metavar="OPT",
        help="Extra read_slang option token, appended verbatim (repeatable; "
        "slang frontend only). E.g. --slang-option --single-unit for repos "
        "whose macros leak across the filelist from a once-included header.",
    )
    _add_timing_args(parser)
    return parser


def _add_timing_args(parser: argparse.ArgumentParser) -> None:
    """Register OpenSTA/OpenROAD timing-engine arguments."""
    parser.add_argument(
        "--timing-engine",
        choices=list(TIMING_ENGINE_CHOICES),
        default=None,
        help="Timing source (default: openroad, or booley.toml timing.engine)",
    )
    parser.add_argument(
        "--clock", default=None, help="Clock port for STA (default: booley.toml or auto-detect)"
    )
    parser.add_argument(
        "--period-ps",
        type=float,
        default=None,
        help="STA clock period in ps (an explicit design constraint override for standalone runs)",
    )
    parser.add_argument(
        "--default-clock",
        type=float,
        default=None,
        metavar="PS",
        dest="default_clock",
        help="Named opt-in canned clock period (ps) for a Target "
        "that carries no SDC. Without it (and no --sta-sdc / "
        "--period-ps) a constraint-less run is a hard error "
        "(ADR 0031): the default clock must be chosen and "
        "named, never applied silently.",
    )
    parser.add_argument(
        "--input-delay-pct",
        type=float,
        default=None,
        help="Default input delay as percent of period (default: 30)",
    )
    parser.add_argument(
        "--output-delay-pct",
        type=float,
        default=None,
        help="Default output delay as percent of period (default: 70)",
    )
    parser.add_argument(
        "--sta-sdc",
        action="append",
        default=[],
        metavar="FILE",
        help="STA constraint SDC file (repeatable; the FuseSoC "
        "synth path forwards one per file in the Target's "
        "file_type:SDC fileset, concatenated last-wins)",
    )
    parser.add_argument(
        "--utilization-pct",
        type=float,
        default=None,
        help="Expert OpenROAD floorplan utilization override (profile-derived by default)",
    )
    parser.add_argument(
        "--placement-density",
        type=float,
        default=None,
        help="Expert OpenROAD override for global-placement density",
    )
    parser.add_argument(
        "--repair-setup",
        action="store_true",
        dest="repair_setup",
        default=None,
        help="Enable OpenROAD setup timing repair",
    )
    parser.add_argument(
        "--no-repair-setup",
        action="store_false",
        dest="repair_setup",
        help="Disable OpenROAD setup timing repair",
    )
    parser.add_argument(
        "--repair-hold",
        action="store_true",
        dest="repair_hold",
        default=None,
        help="Enable OpenROAD hold timing repair after setup repair",
    )
    parser.add_argument(
        "--no-repair-hold",
        action="store_false",
        dest="repair_hold",
        help="Disable OpenROAD hold timing repair",
    )
    parser.add_argument(
        "--gate-cloning",
        action="store_true",
        dest="gate_cloning",
        default=None,
        help="Allow OpenROAD gate cloning during setup repair",
    )
    parser.add_argument(
        "--no-gate-cloning",
        action="store_false",
        dest="gate_cloning",
        help="Disable OpenROAD gate cloning",
    )
    parser.add_argument(
        "--setup-margin-ns",
        type=float,
        default=None,
        help="Expert OpenROAD setup-repair margin in ns",
    )
    parser.add_argument(
        "--repair-tns-percent",
        type=float,
        default=None,
        help="Expert OpenROAD percentage of violating endpoints to repair",
    )
    # Tri-state: default None so an absent flag lets booley.toml decide; the
    # flag only forces repair_timing OFF when explicitly passed.
    parser.add_argument(
        "--no-repair-timing",
        action="store_false",
        dest="repair_timing",
        default=None,
        help="Disable the OpenROAD setup-only repair_timing pass",
    )


def _do_run_locked(args: argparse.Namespace) -> None:
    """Acquire the Yosys EDA-tool lock and run synthesis once.

    Legacy CLI execution path only (ADR 0037 §8): the in-process synthesis
    watchdog and this EDA-tool lock apply here and nowhere else — the make-driven
    boundary path (``configure`` + ``make -C``) relies on the caller's
    timeout budget and the BOOLEY_STAGE log markers instead. A wall-clock
    timeout + tree-kill is enforced by whoever spawns this runner; the CLI
    itself runs the flow exactly once with no retries.
    """
    from booley.runtime.eda_tool_lock import eda_tool_lock

    with eda_tool_lock("yosys"):
        do_run(args)


def main() -> None:
    args = _build_parser().parse_args()

    if args.action == "clean":
        do_clean()
    elif args.action == "check-paths":
        do_check_paths()
    elif args.action == "configure":
        do_configure(args)
    elif args.action == "run":
        _do_run_locked(args)


if __name__ == "__main__":
    main()
