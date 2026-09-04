"""Synthesis coordinator — sv2v/Yosys script generation and timing config.

This module builds the sv2v + Yosys synthesis scripts and owns the shared
configuration used by the OpenROAD physical path. The remaining concerns were
split into sibling leaf modules:

* :mod:`booley.flows.synth.backends.yosys.paths` — project-context path constants
* :mod:`booley.flows.synth.backends.yosys.discovery`  — EDA tool + liberty discovery
* :mod:`booley.flows.synth.backends.yosys.parsing`      — config-param + area result parsing

Their public names are re-exported below so existing importers of ``syn_core``
keep working unchanged.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from booley.core.boundary import (
    BoundaryError,
    is_str_list,
    require_finite_number,
    require_opt_str,
)
from booley.flows.synth.backends.yosys.discovery import (
    DEFAULT_LIB_DIR,
    DEFAULT_LIBERTY,
    resolve_liberty,
)
from booley.flows.synth.backends.yosys.parsing import (
    NAND2_AREA_UM2,
    area_to_kge,
    parse_area_from_stat,
    parse_params,
)
from booley.flows.synth.mode import SYNTH_MODE_CHOICES, SynthMode
from booley.flows.synth.timing import (
    DEFAULT_STA_INPUT_DELAY_PCT,
    DEFAULT_STA_OUTPUT_DELAY_PCT,
    DEFAULT_STA_PERIOD_PS,
    DEFAULT_STA_UTILIZATION_PCT,
    StaTimingConfig,
    detect_clock_port,
)
from booley.runtime.shared_infra import resolve_project_root
from booley.targets.parameter_integrity import enabled_define_names

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
    "SV2V_OUTPUT_NAME",
    "SYNTH_MODE_CHOICES",
    "StaTimingConfig",
    "area_to_kge",
    "detect_clock_port",
    "effective_params_filename",
    "enabled_define_names",
    "parse_abc_mapped_delay_ps",
    "parse_area_from_stat",
    "parse_effective_parameters",
    "parse_params",
    "resolve_frontend",
    "resolve_liberty",
    "resolve_slang_options",
    "scan_synth_logs",
    "sv2v_argv",
    "synth_timing_config",
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

    # Step 6: run one authoritative structural check on the final mapped
    # netlist. ``synth`` runs CHECK internally more than once, so its log can
    # contain repeated or transient loop/driver warnings.  Keeping this pass
    # quiet and in its own artifact gives result interpretation one exact
    # source of truth without duplicating it in yosys.log.
    check_out = q(out_dir + "/check_" + design_name + ".txt")
    final_check = f"tee -q -o {check_out} check"

    # Step 7: Write final netlists and statistics. The sta_* netlist keeps
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

    return f"{hls}; {parameter_guard}; {synth}; {dfflibmap}; {abc}; {final_check}; {wout}"


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
    """Resolve ``--sta-sdc`` paths against *root* or the active checkout.

    STA constraint SDC files (ADR 0029): one per ``--sta-sdc``, sourced from
    the Target's ``file_type: SDC`` fileset (the Flow forwards them) or passed
    directly to the configure surface. A relative path resolves against
    the project root (``/work`` in the Session Runtime), not cwd — same
    convention as ``--inc-dir`` / ``--extra-rtl`` — so a path the caller
    relativized against the worktree stays valid for the generated boundary
    command. There is no TOML fallback: ``[flows.synth.timing].sdc`` is a
    hard-removed key.
    """
    base = root.resolve() if root is not None else resolve_project_root()
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
    an explicit worktree (in-process configure, ADR 0037 §8); ``None`` resolves
    the active checkout lazily.

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
