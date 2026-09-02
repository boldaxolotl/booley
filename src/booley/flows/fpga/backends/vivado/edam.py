"""fpga_edam.py — Edalize vivado-flow helpers for ``fpga_impl`` (ADR 0019).

``fpga_impl`` renders a Vivado project through Edalize and interprets the
resulting reports in Booley:

  * **Edalize generates the project + tcl** from a flow-API EDAM, in-sandbox
    (``edalize.flows.vivado.Vivado.configure()``); the generated tcl replaces
    the hand-written ``vivado_impl.tcl`` (:func:`build_fpga_edam`).
  * **The resolved command runs inside the Session Runtime**
    (:func:`fpga_run_command` — ``make -C <work_root>``).
  * **Booley parses the reports** through the thin post-processor
    :func:`parse_fpga_reports`, following the same
    "invocation delegated, interpretation stays in Booley" principle the sim
    family uses (:func:`sim_edam.reemit_sim_summary`).

The post-processor is fully verifiable without a Vivado installation: it
runs against captured Vivado log/report fixtures. The EDAM build and the
``configure()`` file generation is unit-tested against Edalize. This is the
only ``fpga_impl`` invocation path.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from booley.flows.clock_timing import CLOCK_TIMING_FIELDS, make_clock_timing

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EDAM construction (the edalize ``vivado`` flow)
# ---------------------------------------------------------------------------


def build_fpga_edam(
    *,
    name: str,
    toplevel: str,
    part: str,
    sv_files: list[Path],
    v_files: list[Path],
    include_dirs: list[Path],
    xdc_files: list[Path],
    defines: list[str],
    vlogparams: dict[str, Any],
    workspace_root: Path,
    work_root: Path,
    extra_eda_tool_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the flow-API EDAM for one target's Vivado implementation run.

    Mirrors ``sim_edam.build_sim_edam``: resolved Booley inputs in, a flow-API
    EDAM out, with file names relative to *work_root* so the materialized
    project directory is relocatable within the Session Runtime workspace.

    ``part`` is the only edalize ``vivado`` flow option Booley forwards by
    default (it is in the :mod:`edam` whitelist and unambiguously safe). The XDC
    constraint files (``xdc_files``, per-target and sourced from the Target's
    ``file_type: xdc`` fileset since ADR 0031) are each carried as a typed
    constraint file (``file_type: xdc``, inferred from suffix), defines become
    ``vlogdefine`` parameters, ``vlogparams`` remain top-level parameter
    overrides, and the top module is the EDAM ``toplevel``.
    ``extra_eda_tool_options`` allows a Target to pass further *whitelisted* vivado
    flow options (``pnr``/``synth``/``jobs``); anything outside the whitelist is
    rejected by :func:`edam.build_edam`.

    Strategy / free-form Vivado knobs deliberately do **not** ride here: the
    edalize 0.6.8 vivado flow exposes no ``strategy`` option, so forwarding it
    would be a silent no-op. It stays a Phase-2 Target concern (plan §fpga_impl).
    """
    from booley.flows import edam as edam_layer

    eda_tool_options: dict[str, Any] = {"part": part}
    if extra_eda_tool_options:
        eda_tool_options.update(extra_eda_tool_options)

    return edam_layer.build_edam(
        name=name,
        flow="vivado",
        eda_tool="vivado",
        files=[*sv_files, *v_files, *xdc_files],
        include_dirs=include_dirs,
        toplevel=toplevel,
        defines=defines,
        vlogparams=vlogparams,
        eda_tool_options=eda_tool_options,
        workspace_root=workspace_root,
        relative_to=work_root,
    )


def validate_vivado_parameter_contract(
    work_root: Path,
    name: str,
    vlogparams: dict[str, Any],
) -> None:
    """Prove Edalize rendered every top parameter into Vivado's project Tcl."""
    if not vlogparams:
        return
    project_tcl = work_root / f"{name}.tcl"
    try:
        text = project_tcl.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"fpga: cannot verify top-level parameter overrides; generated "
            f"project Tcl is unavailable: {project_tcl}"
        ) from exc
    generic_blocks = re.findall(
        r"set_property\s+generic\s+\{(.*?)\}\s+\[get_filesets\s+sources_1\]",
        text,
        re.DOTALL,
    )
    rendered = _vivado_generic_assignments(generic_blocks, set(vlogparams))
    expected = {name: _vivado_parameter_value(value) for name, value in vlogparams.items()}
    mismatches = [
        f"{name}={value} (rendered {rendered.get(name, '<missing>')})"
        for name, value in expected.items()
        if rendered.get(name) != value
    ]
    if mismatches:
        raise RuntimeError(
            "fpga: generated Vivado project changed or dropped top-level parameter "
            "override(s): " + ", ".join(mismatches)
        )


def _vivado_parameter_value(value: Any) -> str:
    """Match Edalize's Vivado rendering for supported EDAM scalar types."""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _vivado_generic_assignments(blocks: list[str], names: set[str]) -> dict[str, str]:
    """Parse requested assignments, allowing a string value to contain spaces."""
    assignments: dict[str, str] = {}
    marker = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    if not marker:
        return assignments
    for block in blocks:
        for name in names:
            match = re.search(
                rf"(?:^|\s){re.escape(name)}=(.*?)(?=\s+(?:{marker})=|\s*$)",
                block,
                re.DOTALL,
            )
            if match is not None:
                assignments[name] = match.group(1).strip()
    return assignments


_OOC_TCL_SNIPPET = """
# Booley: out-of-context QoR gate ([flows.fpga] out_of_context = true).
# Synthesize without inferring IO buffers so a bare engine block whose port
# count exceeds the package pin budget can still place+route for Fmax/QoR.
# The downstream write_bitstream step is expected to fail (no pinout); the
# fpga_impl post-processor already keys success on route completion, not make.
set_property -name {STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS} -value {-mode out_of_context} -objects [get_runs synth_1]
"""


def enable_out_of_context(work_root: Path, name: str) -> None:
    """Append the out-of-context synth property to the materialized project tcl.

    Edalize 0.6.8's vivado flow has no OOC knob, so Booley patches the generated
    ``<name>.tcl`` after ``configure()`` — the same file that creates the
    ``.xpr`` (``create_project -force``), so the property lands on ``synth_1``
    every (re)build. Idempotent: configure() rewrites the tcl each run, so the
    snippet is appended at most once per materialization.
    """
    project_tcl = work_root / f"{name}.tcl"
    if not project_tcl.is_file():
        raise FileNotFoundError(
            f"fpga: cannot enable out_of_context — generated project tcl not found: {project_tcl}"
        )
    content = project_tcl.read_text(encoding="utf-8")
    if "-mode out_of_context" not in content:
        project_tcl.write_text(content + _OOC_TCL_SNIPPET, encoding="utf-8")


def fpga_run_command(work_root: Path, work_dir: Path) -> list[str]:
    """Return the command that drives the materialized Vivado project (ADR 0019).

    The edalize ``vivado`` flow emits a ``Makefile`` whose default target runs
    ``vivado -mode batch`` over the generated tcl. The relative ``make -C``
    command runs inside the Session Runtime against its mounted Project tree.
    """
    from booley.flows import edam as edam_layer

    rel = edam_layer.relpath_for_make(work_root, work_dir)
    return edam_layer.make_command(rel)


# ---------------------------------------------------------------------------
# Report post-processor (interpretation stays in Booley)
# ---------------------------------------------------------------------------

# Each pattern is run over the concatenated Vivado run log + report files. The
# regexes target the **colon** form Vivado's ``report_timing_summary`` /
# ``report_utilization`` actually emit (verified against captured fixtures) — the
# table form the legacy host-side ``vivado_impl.yaml`` assumed does not match the
# real post-route summary, which is exactly the metric-fidelity risk the plan
# flagged and the reason extraction moves into Booley where it is tested.
_INT_METRIC_RES: dict[str, re.Pattern[str]] = {
    # "| Slice LUTs*               | 2853 |" (CLB LUTs on UltraScale+).
    "lut_count": re.compile(r"(?:Slice|CLB) LUTs\*?\s*\|\s*(\d+)"),
    # "| Register as Flip Flop    | 1523 |" / "Slice Registers" / "CLB Registers".
    "ff_count": re.compile(
        r"(?:Register as Flip Flop|CLB Registers|Slice Registers)\s*\|\s*(\d+)"
    ),
    "bram_count": re.compile(r"Block RAM Tile\s*\|\s*(\d+(?:\.\d+)?)"),
    "dsp_count": re.compile(r"DSPs\s*\|\s*(\d+)"),
}

# Timing slack. Real Vivado ``report_timing_summary`` emits a "Design Timing
# Summary" *table* (verified against 2025.2 routed output) — the worst-slack row
# is ``WNS TNS TNS-fail TNS-total WHS …``, NOT the ``WNS(ns) : <v>`` colon form a
# synthetic fixture once assumed. Match the table first; keep the colon form as a
# fallback so older/custom report shapes still parse.
_TIMING_TABLE_RE = re.compile(
    r"Design Timing Summary"  # the global (worst) summary, not per-clock
    r".*?WNS\(ns\).*?WHS\(ns\).*?\n"  # column header carrying both metrics
    r"\s*-[-\s]*\n"  # the dashes underline
    r"\s*(?P<wns>-?[\d.]+)\s+(?P<tns>-?[\d.]+)\s+\d+\s+\d+\s+(?P<whs>-?[\d.]+)",
    re.S,
)
_WNS_COLON_RE = re.compile(r"WNS\(ns\)\s*:\s*(-?[\d.]+)")
_WHS_COLON_RE = re.compile(r"WHS\(ns\)\s*:\s*(-?[\d.]+)")

# Per-clock timing. Fmax and critical path are inherently per-clock, so we join
# two of ``report_timing_summary``'s tables by clock name:
#   * "Clock Summary" — each clock's constrained ``Period(ns)`` (row shape
#     ``<name> {rise fall} <period> <freq>``; the ``{..}`` waveform brace makes
#     the row unmistakable and skips the header/dashes lines).
#   * "Intra Clock Table" — each clock's *own* worst setup/hold slack, in the
#     same ``WNS TNS <int> <int> WHS`` column shape as the aggregate Design
#     Timing Summary but prefixed by the clock name.
# critical_path_ps / fmax_mhz are then derived (period - WNS) by the shared
# clock_timing helper. Inter-clock (cross-domain) rows are deliberately excluded
# — they are path groups, not clock domains.
_CLOCK_SUMMARY_ROW_RE = re.compile(
    r"(?m)^\s*(?P<clk>\S+)\s+\{[^}]*\}\s+(?P<period>[\d.]+)\s+[\d.]+\s*$"
)
_INTRA_CLOCK_ROW_RE = re.compile(
    r"(?m)^\s*(?P<clk>\S+)\s+(?P<wns>-?[\d.]+)\s+-?[\d.]+\s+\d+\s+\d+\s+(?P<whs>-?[\d.]+)\b"
)


def _section(text: str, start: str, *ends: str) -> str:
    """Return the slice of *text* from the *start* header to the first *end*.

    Scopes a table parse to its section so, e.g., the Intra Clock Table row
    regex never strays into the Inter Clock Table (cross-domain path groups) or
    beyond. Empty string when *start* is absent.
    """
    i = text.find(start)
    if i < 0:
        return ""
    rest = text[i + len(start) :]
    cut = len(rest)
    for end in ends:
        j = rest.find(end)
        if 0 <= j < cut:
            cut = j
    return rest[:cut]


def _parse_clock_periods_ns(text: str) -> dict[str, float]:
    """Map ``clock_name -> Period(ns)`` from the Clock Summary table."""
    section = _section(text, "Clock Summary", "Intra Clock Table", "Inter Clock Table")
    periods: dict[str, float] = {}
    for match in _CLOCK_SUMMARY_ROW_RE.finditer(section):
        period = _safe_float(match.group("period"))
        if period is not None:
            periods[match.group("clk")] = period
    return periods


def _parse_intra_clock_slack(text: str) -> dict[str, tuple[float | None, float | None]]:
    """Map ``clock_name -> (wns_ns, whs_ns)`` from the Intra Clock Table."""
    section = _section(text, "Intra Clock Table", "Inter Clock Table", "Other Path Groups")
    rows: dict[str, tuple[float | None, float | None]] = {}
    for match in _INTRA_CLOCK_ROW_RE.finditer(section):
        rows[match.group("clk")] = (
            _safe_float(match.group("wns")),
            _safe_float(match.group("whs")),
        )
    return rows


def _parse_per_clock(text: str) -> dict[str, dict[str, float | None]]:
    """Join Clock Summary periods with Intra Clock Table slack → per_clock JSON.

    Returns the nested ``{clk: {period_ns, wns_ns, whs_ns, critical_path_ps,
    fmax_mhz}}`` dict that ``FpgaMetrics.per_clock`` round-trips. A clock present
    in only one table still yields a row (missing fields stay ``None``); a design
    with no constrained clock yields ``{}``.
    """
    periods = _parse_clock_periods_ns(text)
    slacks = _parse_intra_clock_slack(text)
    names = list(dict.fromkeys([*periods, *slacks]))  # union, stable source order
    per_clock: dict[str, dict[str, float | None]] = {}
    for name in names:
        wns_ns, whs_ns = slacks.get(name, (None, None))
        ct = make_clock_timing(name, periods.get(name), wns_ns, whs_ns)
        per_clock[name] = {field: getattr(ct, field) for field in CLOCK_TIMING_FIELDS}
    return per_clock


# Critical structural conditions — counted, not valued.
_LATCH_RE = re.compile(r"Register as Latch\s*\|\s*(\d+)")
# Combinational loops: the routed timing summary states the count explicitly
# ("There are N combinational loops in the design.") — authoritative, and it must
# NOT be mistaken for a violation when N==0 (matching the bare phrase made every
# clean run look like it had a loop). Prefer that count; fall back to counting the
# LUTLP-1 DRC violation code when only a DRC report (no summary) is present.
_COMB_LOOP_SUMMARY_RE = re.compile(
    r"There are (\d+) combinational loops in the design", re.IGNORECASE
)
_COMB_LOOP_DRC_RE = re.compile(r"LUTLP-1")
# Multi-driven nets surface as the MDRV-1 DRC violation code. Match the code, not
# loose "multi-driven" prose, so a benign "0 multi-driven" summary can't trip it.
_MULTI_DRIVEN_RE = re.compile(r"MDRV-1")
# Success marker. The legacy non-project flow printed "Implementation completed
# successfully"; the edalize project-mode flow (launch_runs/wait_on_run) does
# not — its authoritative marker is "route_design completed successfully" (the
# QoR flow stops at route — a boardless soft IP cannot write a bitstream, so
# bitstream success is deliberately *not* required; see fpga_impl).
_IMPL_OK_RE = re.compile(
    r"Implementation completed successfully|route_design completed successfully",
    re.IGNORECASE,
)


def _safe_float(text: str | None) -> float | None:
    """Coerce a regex-captured numeric string to float; None on format drift.

    The timing patterns capture ``-?[\\d.]+``, which can still match malformed
    tokens ("1.2.3", ".", "-") when Vivado's log format drifts. Degrade to
    ``None`` — matching how the integer metrics leave an unparsed field unset —
    rather than raising on EDA tool output.
    """
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _extract_timing(text: str) -> tuple[float | None, float | None]:
    """Return ``(wns_ns, whs_ns)`` from Vivado timing output, table or colon form."""
    table = _TIMING_TABLE_RE.search(text)
    if table:
        return _safe_float(table.group("wns")), _safe_float(table.group("whs"))
    wns = _WNS_COLON_RE.search(text)
    whs = _WHS_COLON_RE.search(text)
    return (
        _safe_float(wns.group(1)) if wns else None,
        _safe_float(whs.group(1)) if whs else None,
    )


def parse_fpga_reports(text: str) -> dict[str, Any]:
    """Extract FPGA implementation metrics from raw Vivado log/report text.

    The thin Booley post-processor that replaces the host-side
    ``result_extraction`` block — it observes the *raw* Vivado output (run log
    plus any ``report_utilization`` / ``report_timing_summary`` report files,
    concatenated by the caller) and returns the metric dict
    ``fpga_impl._metrics_from_parsed_reports`` already consumes, so the metric
    normalization / criterion mapping downstream is unchanged.

    Returns a dict with ``lut_count``/``ff_count``/``bram_count``/``dsp_count``
    (numeric|None; BRAM tiles may be fractional), aggregate
    ``wns_ns``/``whs_ns`` (float|None), a ``per_clock``
    map (``{clk: {period_ns, wns_ns, whs_ns, critical_path_ps, fmax_mhz}}`` —
    Fmax/critical-path are per-clock, so there is no top-level scalar for them),
    ``latch_count``/``comb_loop_count``/``multi_driven_count`` (int), and a derived ``status``
    (``"pass"`` only when Vivado reported a successful implementation). Missing
    metrics are left ``None``/``0`` rather than guessed.
    """
    result: dict[str, Any] = {}

    for key, pattern in _INT_METRIC_RES.items():
        match = pattern.search(text)
        if not match:
            result[key] = None
        elif key == "bram_count":
            result[key] = float(match.group(1))
        else:
            result[key] = int(match.group(1))

    result["wns_ns"], result["whs_ns"] = _extract_timing(text)
    result["per_clock"] = _parse_per_clock(text)

    latch = _LATCH_RE.search(text)
    result["latch_count"] = int(latch.group(1)) if latch else 0
    comb_summary = _COMB_LOOP_SUMMARY_RE.search(text)
    result["comb_loop_count"] = (
        int(comb_summary.group(1)) if comb_summary else len(_COMB_LOOP_DRC_RE.findall(text))
    )
    result["multi_driven_count"] = len(_MULTI_DRIVEN_RE.findall(text))

    # The run log is authoritative for success; absent the explicit marker we
    # leave status unset so the caller's exit-code handling decides (a crashed
    # Vivado never prints it).
    result["status"] = "pass" if _IMPL_OK_RE.search(text) else None
    return result
