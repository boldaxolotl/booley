"""Hardware-domain metric formatters for console criteria rows.

Extracted from ``widgets.py`` (principle 8 — Single Responsibility) so the
generic Textual UI widgets stay free of hardware-domain knowledge (coverage,
FPGA implementation, synthesis, mutation, lint, review metrics). These render
the short metric string shown after each criterion name in the MainPane.
"""

from __future__ import annotations

from booley.core.boundary import as_dict, as_float, as_int
from booley.flows.clock_timing import worst_fmax_from_json

_COVERAGE_KEYS = {
    "coverage_toggle": "toggle",
    "coverage_fsm": "fsm",
    "coverage_value": "value",
    "coverage_branch": "branch",
    "coverage_expression": "expression",
    "coverage_mean": "mean",
}


def _format_coverage_metric(key: str, d: dict, p: dict, stale: bool) -> str | None:
    """Coverage-family metric: "<pct>% (>=<thr>%)". None if not a coverage key/no pct."""
    sub_key = next(
        (
            detail_key
            for base_key, detail_key in _COVERAGE_KEYS.items()
            if key == base_key or key.startswith(f"{base_key}_")
        ),
        None,
    )
    if sub_key is None:
        return None
    sub = as_dict(d.get(sub_key), default={})
    pct = as_float(sub.get("pct"))
    if pct is None:
        return None
    thr = as_float(p.get("min_pct"))
    val = "?%" if stale else f"{pct:.0f}%"
    threshold = f" (>={thr:g}%)" if thr is not None else ""
    return f"{val}{threshold}"


def _format_fpga_impl_metric(d: dict, stale: bool) -> str:
    """fpga_impl_ok metric: "<luts> LUTs | <ffs> FFs | WNS <ns>ns"."""
    if stale:
        return "?"
    luts = as_int(d.get("lut_count"))
    ffs = as_int(d.get("ff_count"))
    wns = as_float(d.get("wns_ns"))
    parts = []
    if luts is not None:
        parts.append(f"{luts / 1000:.1f}k LUTs" if luts >= 1000 else f"{luts} LUTs")
    if ffs is not None:
        parts.append(f"{ffs / 1000:.1f}k FFs" if ffs >= 1000 else f"{ffs} FFs")
    if wns is not None:
        parts.append(f"WNS {wns:.2f}ns")
    return " | ".join(parts) if parts else ""


def _format_synthesis_metric(d: dict, stale: bool) -> str:
    """synthesis_ok metric: "<cells> cells · <fmax>MHz"."""
    if stale:
        return "?"
    cells = as_int(d.get("cells"))
    # Fmax is per-clock now; show the timing-worst clock's value as the one
    # representative number on this compact line.
    fmax = worst_fmax_from_json(as_dict(d.get("per_clock"), default={}))
    parts = []
    if cells is not None:
        parts.append(f"{cells / 1000:.1f}k cells" if cells >= 1000 else f"{cells} cells")
    if fmax is not None:
        parts.append(f"{fmax:.0f}MHz")
    return " · ".join(parts) if parts else ""


def _format_cycle_metric(d: dict, stale: bool) -> str:
    """cycle_count metric: current count with optional baseline delta."""
    if stale:
        return "?"
    current = as_int(d.get("cycles"))
    baseline = as_int(d.get("baseline_cycles"))
    if current is None:
        return ""
    if baseline is None:
        return f"{current:,} cycles"
    return f"{baseline:,} → {current:,} cycles ({current - baseline:+,})"


def _format_metric(key: str, entry: object) -> str:  # noqa: PLR0911 — metric-type dispatch; each criterion kind is its own return branch
    """Short metric string from criterion detail/params."""
    entry = as_dict(entry, default={})
    d = as_dict(entry.get("detail"), default={})
    p = as_dict(entry.get("params"), default={})
    stale = entry.get("stale") is True

    coverage = _format_coverage_metric(key, d, p, stale)
    if coverage is not None:
        return coverage

    if key.startswith("fpga_impl_ok"):
        return _format_fpga_impl_metric(d, stale)

    if key.startswith("synthesis_ok"):
        return _format_synthesis_metric(d, stale)

    if key.startswith("cycle_count_"):
        return _format_cycle_metric(d, stale)

    if key.startswith("mutation_score"):
        detected = as_int(d.get("detected"))
        total = as_int(d.get("total_valid"))
        if detected is not None and total:
            pct = detected / total * 100
            return f"{detected}/{total} ({pct:.0f}%)"

    if key.startswith("lint_clean"):
        if stale:
            return "?"
        wc = as_int(d.get("warnings"))
        if wc is not None:
            return f"{wc} warnings" if wc else "clean"

    if key.startswith("review_"):
        issues = as_int(d.get("issues"))
        if issues is not None:
            return f"{issues} issues" if issues else "clean"

    return ""
