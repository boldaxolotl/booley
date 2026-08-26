"""Resolve and render the built-in Yosys synthesis specification.

The ASIC synthesis Flow parses this module's option surface in-process, renders
a generated Makefile, and executes that Makefile through the Session Runtime
boundary. This module does not execute EDA tools itself.
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
from pathlib import Path

from booley.core.boundary import BoundaryError

# ============================================================================
# Configuration
# ============================================================================

# Predefined configurations — imported from shared module (in parent dir)
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR.parent.parent))
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
    parse_params,
    resolve_liberty,
    synth_timing_config,
)

# Explicit-source synthesis result dirs land under the transient runtime tree, NOT
# the design repo's ``util/syn/`` namespace (which the project may legitimately
# own) — this mirrors the Edalize build dirs at
# ``.booley_project/.runtime/edalize/...``. Keyed on PROJECT_ROOT (``/work`` in
# the Session Runtime) so it remains stable across the generated boundary
# command, and
# ``.booley_project/.runtime/`` is already git-ignored and skipped by the
# workspace-isolation scanner. (SETUP-27)
SYN_RESULT_ROOT = PROJECT_ROOT / ".booley_project" / ".runtime" / "syn" / "syn_result"

# ============================================================================
# High-level actions
# ============================================================================


def _resolve_extra_rtl(args: argparse.Namespace, root: Path | None = None) -> list[Path]:
    """Resolve and validate extra RTL files from CLI.

    Relative paths resolve against *root* (default ``PROJECT_ROOT`` — ``/work``
    inside the Session Runtime); the in-process configure half (ADR 0037 §8)
    passes the Flow's work_dir explicitly instead of relying on the import-time
    constant.
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
    inside the Session Runtime), so a path the caller relativized against the
    worktree stays valid for the generated boundary command — mirroring
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
    # The pre-Flow CLI historically read utilization/repair from the legacy
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


def resolve_spec(
    args: argparse.Namespace,
    *,
    project_root: Path | None = None,
    require_liberty: bool = True,
) -> syn_make.SynthSpec:
    """Resolve parsed configure options into a :class:`SynthSpec`.

    Sources / include dirs / SDC files are validated here
    (they live in the shared workspace and must exist at configure time), the
    timing config is resolved, and the liberty path is computed.

    *project_root* anchors relative paths and the booley.toml lookup
    (``None`` = the import-time ``PROJECT_ROOT``). *require_liberty* keeps the
    hard "Liberty file not found" error for direct configuration; ``False``
    permits the Flow to render a diagnostic plan.

    Exits with an ``ERROR:`` message on any validation failure; in-process
    callers catch ``SystemExit``.
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

    # The ABC-mode --sdc knob is existence-checked relative to the project root.
    # It has no effect on the rendered scripts; the current Yosys script never
    # consumed it beyond this check.
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


def parse_configure_argv(cmd: list[str]) -> argparse.Namespace:
    """Parse the synth Flow's configure argv back into typed options.

    The built-in Flow builds a full configure argv as the validated,
    golden-snapshotted carrier of every recipe knob, then parses it here
    in-process instead of executing this module as a subprocess.
    Accepts the argv with or without the ``python3 -m <module>`` prefix.
    """
    tokens = list(cmd)
    if tokens[:3] == ["python3", "-m", "booley.yosys.run_yosys_syn"]:
        tokens = tokens[3:]
    return _build_parser().parse_args(tokens)


def do_configure(args: argparse.Namespace) -> Path:
    """CLI ``configure`` action: render the make-driven build dir and stop.

    EDA execution remains the responsibility of the built-in Flow's Session
    Runtime boundary command.
    """
    spec = resolve_spec(args)
    work_dir = _resolve_syn_workdir(args, args.top, spec.params)
    plan = syn_make.configure_synthesis(spec, work_dir)
    for warning in plan.warnings:
        print(f"WARNING: {warning}")
    print(f"Configured: {work_dir}")
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
        description="Render Yosys synthesis inputs in a parallel-safe isolated work directory"
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="configure",
        choices=["configure", "clean", "check-paths"],
        help="Action to perform (default: configure). 'configure' renders the "
        "scripts + Makefile without executing EDA tools.",
    )
    parser.add_argument("-t", "--top", help="Top-level module name for explicit-source synthesis")
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
        help="STA clock period in ps (an explicit design constraint override)",
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


def main() -> None:
    args = _build_parser().parse_args()

    if args.action == "clean":
        do_clean()
    elif args.action == "check-paths":
        do_check_paths()
    elif args.action == "configure":
        do_configure(args)


if __name__ == "__main__":
    main()
