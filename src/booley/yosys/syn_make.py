"""Generated-Makefile implementation of the Yosys synthesis flow.

The flow uses a ``make`` argv runnable with only the EDA binaries on ``PATH``
and leaves results as files under the Session Runtime workspace. This module
implements the configure and interpret halves:

* **configure** (:func:`configure_synthesis`) — renders everything up front:
  the sv2v command line, the Yosys script (frontend read, ABC recipe, netlist
  + ``stat`` emission), and, in physical mode, the OpenROAD Tcl and merged SDC,
  plus a generated ``Makefile`` whose default target chains the stages
  (sv2v → Yosys → OpenROAD) with each stage's output captured to a log file
  and ``BOOLEY_STAGE`` markers echoed between stages for post-mortem
  attribution. Recipes invoke only EDA binaries + POSIX shell.
* **interpret** (:func:`boundary_output`) — reconstructs the report text the
  stdout-based flow used to produce, from the stage logs / ``stat`` file /
  timing reports written by the make run. Every artifact is freshness-gated
  by the caller (``SubprocessResult.dispatched_unix``) so a leftover file
  from an earlier run is never parsed as a fresh result.

Script-internal paths are rendered relative to the build directory (make runs
with ``-C <build dir>``). Liberty/PDK data live at the Session Runtime's issued
paths (``/opt/pdk`` / ``$PRJ_LIB_DIR``).

The legacy ``run_yosys_syn run`` CLI keeps its in-process execution path
(with the stall watchdog); this module never spawns a process itself.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shlex
import shutil
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.platform_paths import posix_relpath
from booley.yosys import openroad_timing, syn_core
from booley.yosys.syn_core import StaTimingConfig

# Bounded log tail echoed by a failing make recipe so the actionable
# diagnostic reaches the boundary command's own stdout (mirrors the legacy
# _print_log_tail, sized up because a recipe can't pick "error lines only").
_FAIL_TAIL_LINES = 40


# ---------------------------------------------------------------------------
# Spec / plan / outcome types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthSpec:
    """Fully-resolved inputs of one synthesis run (the configure half's input).

    Everything here is resolved *before* any EDA tool runs: absolute source /
    include / SDC paths (they exist in the shared workspace), the recipe knobs,
    and the resolved :class:`StaTimingConfig`.
    """

    design_name: str
    sources: tuple[Path, ...]
    inc_dirs: tuple[Path, ...]
    defines: tuple[str, ...]
    params: dict[str, str]
    liberty: Path
    liberty_found: bool
    flatten: bool
    abc_recipe: str | None
    frontend: str
    timing: StaTimingConfig
    # Extra raw read_slang tokens (slang frontend only); default keeps every
    # existing constructor call and golden snapshot unchanged.
    slang_options: tuple[str, ...] = ()
    ppa_profile: str = "balanced"
    generic_abc_before_mapping: bool = False
    abc_script: str | None = None
    abc_delay_ps: int | None = None


@dataclass(frozen=True)
class SynthPlan:
    """A configured build dir, ready for its ``make -C`` boundary command."""

    build_dir: Path
    spec: SynthSpec
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundaryOutcome:
    """Interpretation of a finished boundary run's file artifacts.

    ``forced_failure`` carries the first error line found by the
    false-pass log scan (yosys/ABC can emit ``ERROR:`` yet exit 0) when the
    make run itself exited 0 — the caller downgrades the run to a failure.
    """

    text: str
    forced_failure: str | None = None
    # A fresh final ``stat_<design>.txt`` is the durable proof that the Yosys
    # stage reached its contractual statistics/check boundary.
    yosys_complete: bool = False


# ---------------------------------------------------------------------------
# Configure half — render scripts + Makefile into the build dir
# ---------------------------------------------------------------------------


def configure_synthesis(spec: SynthSpec, build_dir: Path) -> SynthPlan:
    """Render the synthesis scripts and Makefile for *spec* into *build_dir*.

    Writes ``synth.ys`` (+ the sv2v recipe inside the Makefile) and, in
    physical mode, ``sta_constraints.sdc`` and ``run_openroad.tcl``. Previous
    stage artifacts are cleared so the interpret half never reads a leftover
    (freshness-gating remains the second line of defense).
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    _clear_stage_artifacts(build_dir, spec.design_name)

    warnings: list[str] = []
    if not spec.liberty_found:
        warnings.append(
            f"liberty file not found at configure time: {spec.liberty} — the "
            "yosys stage will fail unless the Session Runtime provides it "
            "(set PRJ_LIB_DIR or --liberty; see booley doctor)."
        )

    _write_yosys_script(spec, build_dir)
    _write_recipe_artifact(spec, build_dir)

    if spec.timing.mode == "physical":
        report_dir = build_dir / "reports" / "timing"
        report_dir.mkdir(parents=True, exist_ok=True)
        _write_boundary_sdc(spec.timing, build_dir)
        # Relative script-internal paths: the recipes run with cwd=build_dir.
        openroad_timing.write_openroad_script(
            spec.design_name,
            spec.liberty,
            Path(f"sta_{spec.design_name}.v"),
            Path("sta_constraints.sdc"),
            openroad_timing.openroad_pdk_paths(),
            Path("reports/timing"),
            build_dir,
            spec.timing,
        )

    (build_dir / "Makefile").write_text(_render_makefile(spec, build_dir), encoding="utf-8")
    return SynthPlan(build_dir=build_dir, spec=spec, warnings=tuple(warnings))


def _clear_stage_artifacts(build_dir: Path, design: str) -> None:
    """Remove the artifacts a previous run may have left in *build_dir*.

    Best-effort (a survivor is caught by the freshness gate at interpret
    time), but deliberate: the false-pass log scan reads files by name and
    must never see last run's logs.
    """
    stale = [
        "sv2v.log",
        "yosys.log",
        "sta.log",
        "openroad.log",
        syn_core.SV2V_OUTPUT_NAME,
        syn_core.effective_params_filename(design),
        f"stat_{design}.txt",
        f"log_abc_{design}.txt",
        # No longer emitted (the pre-mapping write_verilog is gone), but still
        # swept so a leftover from an older build dir can't be mistaken for
        # this run's output.
        f"asic_synth_{design}.v",
        f"synth_{design}.v",
        f"sta_{design}.v",
        f"openroad_{design}.v",
    ]
    for name in stale:
        with contextlib.suppress(OSError):
            (build_dir / name).unlink(missing_ok=True)
    shutil.rmtree(build_dir / "reports", ignore_errors=True)


def _rel(path: Path, build_dir: Path) -> str:
    """POSIX path of *path* relative to the build dir (recipes run with -C)."""
    return posix_relpath(path, build_dir)


def _write_yosys_script(spec: SynthSpec, build_dir: Path) -> None:
    """Render ``synth.ys`` with build-dir-relative source paths.

    The sv2v frontend reads the transpiled ``sv2v_converted.v`` the sv2v stage
    writes into the build dir; the slang frontend reads the raw sources. The
    script builder's ``work_dir`` is ``.`` so every emitted netlist/stat path
    is build-dir-relative too.
    """
    if spec.frontend == "sv2v":
        yosys_sources = [Path(syn_core.SV2V_OUTPUT_NAME)]
        inc_dirs: list[Path] = []
        # sv2v has already consumed these defines, so read_verilog ignores
        # them; the post-elaboration parameter guard still needs the original
        # list to detect an enabled macro that left a same-named top parameter
        # at zero.
        defines = list(spec.defines)
    else:
        yosys_sources = [Path(_rel(f, build_dir)) for f in spec.sources]
        inc_dirs = [Path(_rel(d, build_dir)) for d in spec.inc_dirs]
        defines = list(spec.defines)
    script = syn_core._build_yosys_script(
        yosys_sources,
        spec.design_name,
        spec.liberty,
        Path(),  # work_dir "." — netlist/stat paths come out build-dir-relative
        spec.flatten,
        spec.params,
        spec.abc_recipe,
        frontend=spec.frontend,
        inc_dirs=inc_dirs,
        defines=defines,
        slang_options=list(spec.slang_options),
        generic_abc_before_mapping=spec.generic_abc_before_mapping,
        abc_script=spec.abc_script,
        abc_delay_ps=spec.abc_delay_ps,
    )
    # One command per line: the "; " separators join top-level commands only
    # (ABC's "+strash;balance;..." recipe token has no space after ';', so it
    # survives the split intact).
    lines = ["# Generated by Booley synth (ADR 0037) -- do not edit."]
    lines.extend(script.split("; "))
    (build_dir / "synth.ys").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_recipe_artifact(spec: SynthSpec, build_dir: Path) -> None:
    """Persist the fully resolved generic, Yosys, and OpenROAD controls."""
    timing = spec.timing
    density = timing.placement_density
    if density is None:
        density = min(0.80, timing.utilization_pct / 100.0 + 0.25)
    payload = {
        "ppa_profile": spec.ppa_profile,
        "flatten": spec.flatten,
        "yosys": {
            "abc_recipe": spec.abc_recipe or "default",
            "abc_script": spec.abc_script,
            "generic_abc_before_mapping": spec.generic_abc_before_mapping,
            "abc_delay_ps": spec.abc_delay_ps,
        },
        "openroad": {
            "utilization_pct": timing.utilization_pct,
            "placement_density": density,
            "repair_setup": timing.repair_timing,
            "repair_hold": timing.repair_hold,
            "gate_cloning": timing.gate_cloning,
            "setup_margin_ns": timing.setup_margin_ns,
            "repair_tns_percent": timing.repair_tns_percent,
        },
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    (build_dir / "synthesis_recipe.json").write_text(text, encoding="utf-8")


# Clock-port candidates, mirrored from syn_core._CLOCK_CANDIDATES: the probe
# below runs in Tcl during execution because the netlist to detect a clock
# from does not exist at configure time.
_TCL_CLOCK_CANDIDATES = "clk_i clk clock i_clk aclk"


def _write_boundary_sdc(config: StaTimingConfig, build_dir: Path) -> None:
    """Write ``sta_constraints.sdc`` for the boundary (make) pipeline.

    When the clock is statically known — an explicit ``--clock`` or a
    ``create_clock -name`` in the Target's authored SDC — this is byte-for-byte
    :func:`syn_core.write_sta_sdc`. Otherwise the legacy flow detected the
    clock *port* from the synthesized netlist, which does not exist at
    configure time; the generated block then resolves the port at read time
    with a Tcl candidate probe (SDC is a Tcl dialect, and the generated file
    already uses ``if``/``catch``).
    """
    clock = config.clock or syn_core._first_authored_clock(config)
    if clock:
        syn_core.write_sta_sdc(config, clock, build_dir)
        return

    period_ns = config.period_ps / 1000.0
    input_delay_ns = period_ns * (config.input_delay_pct / 100.0)
    output_delay_ns = period_ns * (config.output_delay_pct / 100.0)
    user_sdc = syn_core.read_user_sdc_text(config)
    owns_clock = bool(syn_core._SDC_CREATE_CLOCK_RE.search(user_sdc))
    owns_input = bool(syn_core._SDC_INPUT_DELAY_RE.search(user_sdc))
    owns_output = bool(syn_core._SDC_OUTPUT_DELAY_RE.search(user_sdc))

    lines: list[str] = []
    if user_sdc:
        lines.append(user_sdc)
    lines.extend(
        [
            "",
            "# Auto-generated by Booley physical synthesis backend.",
            "# The clock port is probed at read time: this file is rendered",
            "# before synthesis runs (ADR 0037 configure half), so there is no",
            "# netlist to detect a clock port from yet.",
            'set _booley_clk ""',
            f"foreach _c {{{_TCL_CLOCK_CANDIDATES}}} {{",
            '  if {$_booley_clk eq "" && ![catch {get_ports $_c} _p] && [llength $_p] > 0} {',
            "    set _booley_clk $_c",
            "  }",
            "}",
            'if {$_booley_clk eq ""} {',
            '  puts "WARNING: no clock port was configured or detected"',
            "} else {",
        ]
    )
    if not owns_clock:
        lines.append(
            f"  create_clock -name $_booley_clk -period {period_ns:.6f} [get_ports $_booley_clk]"
        )
    if not owns_input:
        lines.append(
            "  if { [catch { set input_ports [remove_from_collection [all_inputs]"
            " [get_ports $_booley_clk]] }] } { set input_ports [all_inputs] }"
        )
        lines.append(f"  set_input_delay -clock $_booley_clk {input_delay_ns:.6f} $input_ports")
    if not owns_output:
        lines.append(f"  set_output_delay -clock $_booley_clk {output_delay_ns:.6f} [all_outputs]")
    if not owns_input:
        lines.append("  catch { set_driving_cell -lib_cell BUF_X1 $input_ports }")
    if not owns_output:
        lines.append("  catch { set_load 10.0 [all_outputs] }")
    lines.append("}")
    (build_dir / "sta_constraints.sdc").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sv2v_recipe(spec: SynthSpec, build_dir: Path) -> str:
    """The sv2v stage's shell command (build-dir-relative paths, quoted).

    The argv itself comes from :func:`syn_core.sv2v_argv` so the make recipe,
    the legacy in-process runner, and ``elaborate``'s ASIC path cannot drift.
    """
    argv = syn_core.sv2v_argv(
        [Path(_rel(f, build_dir)) for f in spec.sources],
        [Path(_rel(d, build_dir)) for d in spec.inc_dirs],
        list(spec.defines),
        syn_core.SV2V_OUTPUT_NAME,
    )
    return " ".join(shlex.quote(a) for a in argv)


def _fail_tail(log_name: str) -> str:
    """Recipe suffix: on failure, echo the log tail and preserve the rc."""
    return f"|| {{ rc=$$?; tail -n {_FAIL_TAIL_LINES} {log_name}; exit $$rc; }}"


def _sta_recipe_lines(spec: SynthSpec) -> list[str]:
    """The physical synthesis + timing stage's recipe body."""
    lines = ["\t@echo 'BOOLEY_STAGE: sta'"]
    tech_lef = shlex.quote(str(openroad_timing.openroad_pdk_paths().tech_lef))
    body = [
        "\t@if ! command -v openroad >/dev/null 2>&1; then "
        "echo 'ERROR: physical synthesis requires OpenROAD in the Session Runtime'; "
        "exit 127; fi",
        f"\t@test -f {tech_lef} || {{ echo 'ERROR: physical synthesis PDK is missing'; exit 1; }}",
        "\topenroad -no_init -exit run_openroad.tcl > openroad.log 2>&1 "
        + _fail_tail("openroad.log"),
    ]
    lines.extend(body)
    return lines


def _render_makefile(spec: SynthSpec, build_dir: Path) -> str:
    """Render the stage-chaining Makefile (the boundary command's far side).

    Contract (ADR 0037 §5): recipes use only EDA binaries + POSIX shell — no
    python, no Booley. Stage stdout/stderr goes to per-stage log files (the
    interpret half's inputs); ``BOOLEY_STAGE`` markers echo between stages so
    a post-mortem log still says which stage died. Stage-level timeout
    enforcement lives with the caller (the Flow executor's budget).
    """
    mode = spec.timing.mode
    default_target = "sta" if mode == "physical" else "yosys"
    phony = ["all", "yosys"] + (["sv2v"] if spec.frontend == "sv2v" else [])
    if mode == "physical":
        phony.append("sta")

    lines = [
        "# Generated by Booley synth (ADR 0037) -- do not edit.",
        "# Boundary Command Contract: runnable with only the EDA binaries on",
        "# PATH (sv2v/yosys plus OpenROAD in physical mode); results land as",
        "# files in this directory. BOOLEY_STAGE markers attribute a",
        "# post-mortem log tail to the stage that died.",
        ".POSIX:",
        "",
        f"all: {default_target}",
        f".PHONY: {' '.join(phony)}",
        "",
    ]
    yosys_prereq = ""
    if spec.frontend == "sv2v":
        lines += [
            "sv2v:",
            "\t@echo 'BOOLEY_STAGE: sv2v'",
            f"\t{_sv2v_recipe(spec, build_dir)} > sv2v.log 2>&1 {_fail_tail('sv2v.log')}",
            "",
        ]
        yosys_prereq = " sv2v"
    lines += [
        f"yosys:{yosys_prereq}",
        "\t@echo 'BOOLEY_STAGE: yosys'",
        f"\tyosys -s synth.ys > yosys.log 2>&1 {_fail_tail('yosys.log')}",
        "",
    ]
    if mode == "physical":
        lines += ["sta: yosys", *_sta_recipe_lines(spec), ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interpret half — reconstruct the report text from file artifacts
# ---------------------------------------------------------------------------


def boundary_output(
    plan: SynthPlan,
    returncode: int,
    *,
    is_stale: Callable[[Path], bool],
) -> BoundaryOutcome:
    """Rebuild the synthesis report text from the make run's file artifacts.

    Concatenates the fresh stage logs + ``stat`` file (the raw material the
    stdout parsers in asic_synthesize already understand), then re-derives the
    Python-computed markers the legacy flow printed in-process: overall /
    reg->reg / per-clock Fmax (``emit_timing_markers``), the OpenROAD area
    markers, the ``STA_REPORT`` pointers, the false-pass log scan, and the sv2v
    source-provenance hint on failure.

    *is_stale* gates every artifact against the boundary command's dispatch
    time (``SubprocessResult.dispatched_unix``) — ADR 0037 contract clause (d).
    """
    build_dir = plan.build_dir
    spec = plan.spec
    parts: list[str] = [f"WARNING: {w}" for w in plan.warnings]
    parts.append(_recipe_summary(spec))

    def fresh_text(name: str) -> str | None:
        path = build_dir / name
        if not path.is_file() or is_stale(path):
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def append_section(name: str, text: str) -> None:
        parts.append(f"--- {name} ---\n{text}")

    log_names = ["yosys.log"]
    if spec.frontend == "sv2v":
        log_names.insert(0, "sv2v.log")
    have_yosys_log = False
    for name in log_names:
        text = fresh_text(name)
        if text is not None:
            have_yosys_log = have_yosys_log or name == "yosys.log"
            append_section(name, text)
    parameter_parts, parameter_failure = _effective_parameter_sections(spec, fresh_text)
    parts.extend(parameter_parts)

    stat_text = fresh_text(f"stat_{spec.design_name}.txt")
    if stat_text is not None:
        append_section(f"stat_{spec.design_name}.txt", stat_text)

    abc_marker = _abc_delay_marker(spec.design_name, fresh_text)
    if abc_marker is not None:
        parts.append(abc_marker)

    if spec.timing.mode == "physical":
        parts.extend(_timing_sections(plan, fresh_text))

    # False-pass guard (legacy do_run parity): yosys/ABC can emit ERROR: lines
    # yet exit 0. Only meaningful when this run actually produced fresh logs.
    forced_failure: str | None = parameter_failure
    if returncode == 0 and have_yosys_log:
        log_failure = syn_core.scan_synth_logs(build_dir)
        if log_failure:
            forced_failure = forced_failure or log_failure
            parts.append(
                "ERROR: synthesis log reports an error despite exit 0:\n  " + forced_failure
            )

    if returncode != 0 or forced_failure:
        provenance = _source_provenance_text(build_dir, list(spec.sources))
        if provenance:
            parts.append(provenance)

    return BoundaryOutcome(
        text="\n".join(p for p in parts if p),
        forced_failure=forced_failure,
        yosys_complete=stat_text is not None,
    )


def _abc_delay_marker(
    design: str,
    fresh_text: Callable[[str], str | None],
) -> str | None:
    """Build a marker from the dedicated final liberty-mapped ABC log."""
    abc_text = fresh_text(f"log_abc_{design}.txt")
    if abc_text is None:
        return None
    delay_ps = syn_core.parse_abc_mapped_delay_ps(abc_text)
    return f"YOSYS_ABC_LOGIC_DELAY_PS: {delay_ps:.3f}" if delay_ps is not None else None


_RTLIL_ZERO_RE = re.compile(r"(?:0|[0-9]+'s?0+)")


def _effective_parameter_sections(
    spec: SynthSpec,
    fresh_text: Callable[[str], str | None],
) -> tuple[list[str], str | None]:
    """Effective-parameter evidence plus any enabled-but-zero diagnostic."""
    name = syn_core.effective_params_filename(spec.design_name)
    text = fresh_text(name)
    failure = _effective_parameter_failure(spec, text)
    parts = [f"--- {name} ---\n{text}"] if text is not None else []
    if failure is not None:
        parts.append(failure)
    return parts, failure


def _effective_parameter_failure(spec: SynthSpec, text: str | None) -> str | None:
    """Explain an enabled define that left its same-named top parameter off."""
    if text is None:
        return None
    values = syn_core.parse_effective_parameters(text)
    mismatches = [
        name
        for name in syn_core.enabled_define_names(spec.defines)
        if name in values and _RTLIL_ZERO_RE.fullmatch(values[name])
    ]
    if not mismatches:
        return None
    rendered = ", ".join(f"{name}=0" for name in mismatches)
    return (
        "ERROR: effective top-level parameter mismatch: synthesis requested "
        f"{', '.join(mismatches)} enabled as a preprocessor define, but elaborated "
        f"top {spec.design_name!r} retained {rendered}. Declare the setting as "
        "`paramtype: vlogparam` so Booley applies a top-level parameter override, "
        "or change the RTL so the macro drives the parameter default."
    )


def _recipe_summary(spec: SynthSpec) -> str:
    """Human-readable resolved-recipe block for reports and run logs."""
    timing = spec.timing
    density = timing.placement_density
    if density is None:
        density = min(0.80, timing.utilization_pct / 100.0 + 0.25)
    return "\n".join(
        [
            "--- synthesis recipe ---",
            f"PPA_PROFILE: {spec.ppa_profile}",
            f"FLATTEN: {str(spec.flatten).lower()}",
            f"YOSYS_ABC_RECIPE: {spec.abc_recipe or 'default'}",
            f"YOSYS_ABC_SCRIPT: {spec.abc_script or 'none'}",
            f"YOSYS_ABC_DELAY_PS: {spec.abc_delay_ps if spec.abc_delay_ps is not None else 'none'}",
            f"YOSYS_GENERIC_ABC_BEFORE_MAPPING: {str(spec.generic_abc_before_mapping).lower()}",
            f"OPENROAD_UTILIZATION_PCT: {timing.utilization_pct:g}",
            f"OPENROAD_PLACEMENT_DENSITY: {density:g}",
            f"OPENROAD_REPAIR_SETUP: {str(timing.repair_timing).lower()}",
            f"OPENROAD_REPAIR_HOLD: {str(timing.repair_hold).lower()}",
            f"OPENROAD_GATE_CLONING: {str(timing.gate_cloning).lower()}",
            f"OPENROAD_SETUP_MARGIN_NS: {timing.setup_margin_ns:g}",
            "OPENROAD_REPAIR_TNS_PERCENT: "
            f"{timing.repair_tns_percent if timing.repair_tns_percent is not None else 'none'}",
        ]
    )


def _timing_sections(
    plan: SynthPlan,
    fresh_text: Callable[[str], str | None],
) -> list[str]:
    """OpenROAD log sections plus re-derived physical STA markers."""
    spec = plan.spec
    report_dir = plan.build_dir / "reports" / "timing"
    parts: list[str] = []
    openroad_text = fresh_text("openroad.log")

    buf = io.StringIO()
    with redirect_stdout(buf):
        surfaced = False
        if openroad_text is not None:
            parts.append(f"--- openroad.log ---\n{openroad_text}")
            surfaced = syn_core.emit_timing_markers(openroad_text, spec.timing, report_dir)
            openroad_timing._print_openroad_area_markers(openroad_text)
        if not surfaced and openroad_text is not None:
            print("WARNING: STA completed but no timing path slack was reported")
    derived = buf.getvalue()
    if derived:
        parts.append(derived.rstrip("\n"))
    return parts


# ---------------------------------------------------------------------------
# SETUP-26 source provenance (moved from run_yosys_syn so the interpret half
# can use it; run_yosys_syn re-exports the print-based wrapper)
# ---------------------------------------------------------------------------

# A yosys frontend diagnostic on the sv2v output points at
# ``sv2v_converted.v:<line>`` — a line number in the *concatenated* file, which
# has lost all per-source provenance (sv2v emits no `line directives). Match the
# reference so we can map it back to a real source core/file. (SETUP-26)
_CONVERTED_LINEREF_RE = re.compile(r"sv2v_converted\.v[:\s]+(?:line\s+)?(\d+)")
_MODULE_DECL_RE = re.compile(r"^\s*(?:\(\*.*\*\)\s*)?module\s+\\?(\w+)")


def _converted_lineref(work_dir: Path) -> int | None:
    """First ``sv2v_converted.v:<line>`` reference in the sv2v/yosys logs, or None."""
    for log_name in ("yosys.log", "sv2v.log"):
        log_path = work_dir / log_name
        if not log_path.exists():
            continue
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _CONVERTED_LINEREF_RE.search(text)
        if match:
            return int(match.group(1))
    return None


def _enclosing_module(converted: Path, line_no: int) -> str | None:
    """Name of the module enclosing *line_no* in the concatenated Verilog, or None.

    Scans upward from the offending line for the nearest ``module <name>``
    declaration — sv2v preserves module names, so this attributes a flat-file
    line back to a design unit even though the line number itself is meaningless
    to the author.
    """
    try:
        lines = converted.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start = min(line_no, len(lines)) - 1
    for i in range(start, -1, -1):
        match = _MODULE_DECL_RE.match(lines[i])
        if match:
            return match.group(1)
    return None


def _source_file_for_module(name: str, files: list[Path]) -> Path | None:
    """The input source file that declares module *name*, or None if not found."""
    decl = re.compile(r"\bmodule\s+\\?" + re.escape(name) + r"\b")
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if decl.search(text):
            return f
    return None


def _source_provenance_text(work_dir: Path, files: list[Path]) -> str:
    """Provenance hint when a yosys error references the sv2v output, or ``""``.

    The concatenated ``sv2v_converted.v:<line>`` reference is useless to an
    author (the flat file has no per-core provenance). Map it back to the
    enclosing module and the source file that declares it; failing that, at
    least name the candidate source cores fed to sv2v. Empty when the failure
    isn't about the converted file (e.g. a missing liberty). (SETUP-26)
    """
    line_no = _converted_lineref(work_dir)
    if line_no is None:
        return ""  # failure is unrelated to the concatenated frontend input

    lines = [
        "\n--- Source provenance (SETUP-26) ---",
        "The failing line is in the concatenated sv2v_converted.v, which loses "
        "per-source provenance (sv2v emits no `line directives).",
    ]
    module = _enclosing_module(work_dir / syn_core.SV2V_OUTPUT_NAME, line_no)
    source = _source_file_for_module(module, files) if module else None
    if module and source:
        lines.append(f"Offending module '{module}' is declared in: {source}")
        return "\n".join(lines)
    if module:
        lines.append(f"Offending module '{module}' (source file not resolved).")
    lines.append("Source files fed to sv2v (candidate cores):")
    lines.extend(f"  - {f}" for f in files)
    return "\n".join(lines)
