"""Terminal views of the Target surface: ``booley targets`` without ``--json``.

Both views render the JSON payloads of :mod:`booley.targets.target_surface`, so
the text and ``--json`` outputs always come from the same data. They live in
the harness because they use the harness palette and table renderer, which
``booley.targets`` may not import.

In the FLOW column a Target shows the Booley Flows that can drive it
(``drivable_by``), the same meaning as the detail view's ``flow`` row. The
edalize flow the ``.core`` file authors stays visible in the detail view as
the ``.core flow`` row.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from .colors import accent, bold, bold_accent, dim, green
from .terminal_table import render_table

if TYPE_CHECKING:
    from booley.targets.target_surface import SurfacePayload, TargetDetailPayload, TargetRow

_HEADERS = ("TARGET", "FLOW", "EDA TOOL", "TOP", "DOCTOR")
_TOP_COLUMN = 3

# A declared toplevel is normally one module name; upstream cores occasionally
# declare CAPI2 conditional expressions ("tool_verilator? (wrapper)" ...) that
# would stretch the column across the whole terminal. Cap the display: the
# detail view and --json carry the full string.
_TOP_DISPLAY_CHARS = 28

_FOOTER = (
    "✓ = selected by flow_options.booley.doctor · "
    "`booley targets <name>` resolves parameters/files/SDC/XDC"
)
_DETAIL_LABEL_WIDTH = 13


def _doctor_cell(doctor_flows: list[str]) -> str:
    return green("✓ " + ", ".join(doctor_flows)) if doctor_flows else ""


def _listing_row(target: TargetRow) -> list[str]:
    return [
        bold(target["selector"]),
        ", ".join(target["drivable_by"]) or "-",
        target["eda_tool"] or "-",
        target["toplevel"] or "-",
        _doctor_cell(target["doctor_flows"]),
    ]


def render_listing(payload: SurfacePayload) -> str:
    """The grouped-by-core terminal listing: header, rule, Targets, footer."""
    targets = [t for core in payload["cores"] for t in core["targets"]]
    lines: list[str] = []
    if targets:
        table = render_table(
            _HEADERS,
            [_listing_row(t) for t in targets],
            max_widths={_TOP_COLUMN: _TOP_DISPLAY_CHARS},
        )
        lines.extend(table[:2])
        table_lines = iter(table[2:])
        for core in payload["cores"]:
            lines.append(bold_accent(core["vlnv"]) + "  " + dim(f"({core['core_file']})"))
            for target in core["targets"]:
                lines.append(next(table_lines))
                if target["cocotb_module"]:
                    lines.append("    " + dim(f"cocotb {target['cocotb_module']}"))
            lines.append("")
        lines.append(dim(_FOOTER))
    else:
        lines.append("(no Targets match)")
    lines.extend(f"WARNING: {warning}" for warning in payload["warnings"])
    return "\n".join(lines)


def _row(label: str, value: str) -> str:
    """One ``label value`` detail line; padding follows the plain label width."""
    return f"  {dim(label.ljust(_DETAIL_LABEL_WIDTH))} {value}"


def render_detail(payload: TargetDetailPayload) -> str:
    """The single-Target detail view."""
    doctor = payload["doctor_flows"]
    lines = [
        bold_accent(f"Target {payload['name']}")
        + dim(f"  (core {payload['vlnv']}, {payload['core_file']})"),
        _row("selector", payload["selector"]),
        _row("flow", ", ".join(payload["drivable_by"]) or "-"),
        _row(".core flow", payload["flow"] or "-"),
        _row("EDA tool", payload["eda_tool"] or "-"),
    ]
    if payload["cocotb_module"]:
        lines.append(_row("cocotb", payload["cocotb_module"]))
    lines.append(_row("toplevel", (payload["toplevel"] or "-") + "  (declared in .core)"))
    lines.append(_row("Doctor", _doctor_cell(doctor) or "(not selected)"))

    resolved = payload.get("resolved")
    if resolved is not None:
        lines.append("")
        lines.append(accent("Resolved via `fusesoc run --setup`:"))
        lines.append(_row("toplevel", resolved["toplevel"] or "-"))
        lines.append(_row("EDA tool", resolved["eda_tool"] or "-"))
        lines.append(_row("parameters", _render_parameters(resolved["parameters"])))
        lines.append(
            _row(
                "rtl",
                f"{resolved['rtl_hdl_sources']} (System)Verilog sources, "
                f"{resolved['rtl_include_dirs']} include dirs",
            )
        )
        lines.append(_row("tb files", str(resolved["tb_files"])))
        lines.append(_row("SDC", ", ".join(resolved["sdc_files"]) or "(none)"))
        lines.append(_row("XDC", ", ".join(resolved["xdc_files"]) or "(none)"))
        lines.append(_row("build dir", resolved["build_root"]))
    elif resolved_error := payload.get("resolved_error"):
        lines.append("")
        lines.append(f"Resolved view unavailable: {resolved_error}")
        if resolution_command := payload.get("resolution_command"):
            lines.append(f"  Run `{resolution_command}`.")

    lines.extend(f"WARNING: {warning}" for warning in payload.get("warnings", []))
    return "\n".join(lines)


def _render_parameters(params: Mapping[str, object]) -> str:
    """One-line EDAM ``parameters`` summary: ``NAME (datatype) = default``."""
    if not params:
        return "(none)"
    parts: list[str] = []
    for name, spec in params.items():
        if isinstance(spec, Mapping):
            item = cast(Mapping[object, object], spec)
            datatype = item.get("datatype")
            default = item.get("default")
            piece = name + (f" ({datatype})" if datatype else "")
            if default is not None:
                piece += f" = {default}"
            parts.append(piece)
        else:
            parts.append(f"{name} = {spec}")
    return ", ".join(parts)
