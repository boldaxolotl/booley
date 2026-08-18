#!/usr/bin/env python3
"""Compare VCD traces from two simulators.

Parses two VCD files, filters to DUT-internal signals, and compares
signal transitions at matching timestamps.  Reports matches, mismatches,
and signals unique to one simulator.

Usage:
    python compare_vcd.py <sim_a.vcd> <sim_b.vcd> [--sim-a-name NAME]
           [--sim-b-name NAME] [--dut-scope-a SCOPE] [--dut-scope-b SCOPE]
           [--start-time T] [--end-time T] [--ignore PATTERN ...] [--summary]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# VCD parser — lightweight, supports only what we need
# ---------------------------------------------------------------------------

_BIT_RANGE_RE = re.compile(r"\[[\d:]+\]$")

# Default DUT instance leaf name used by the literal-name heuristic when
# no explicit dut_scope is provided.  Historically hardcoded as "dut";
# Phase 8.4 derives the leaf from dut_info.dut_hier_path when the caller
# plumbs it through (CLI: --dut-instance).  Single-DUT invariant: there
# is exactly one DUT path per TB.
_DEFAULT_DUT_INSTANCE = "dut"


def _strip_bit_range(name: str) -> str:
    """Strip trailing bit range like [7:0] from a signal name."""
    return _BIT_RANGE_RE.sub("", name)


def _leaf_from_hier_path(hier_path: str) -> str:
    """Extract the DUT instance leaf name from a hierarchy path.

    ``tb.dut`` -> ``"dut"``; ``tb.gen[0].uut`` -> ``"uut"``.  Bracket
    suffixes are stripped so generate-array entries match by base name.
    """
    if not hier_path:
        return _DEFAULT_DUT_INSTANCE
    leaf = hier_path.rsplit(".", 1)[-1]
    return _BIT_RANGE_RE.sub("", leaf) or _DEFAULT_DUT_INSTANCE


@dataclass
class VCDSignal:
    code: str  # 1-char or multi-char identifier code
    size: int  # bit width
    name: str  # local signal name (bit range stripped)
    scope: str  # full hierarchical scope (dot-separated)
    full_name: str  # scope.name (bit range stripped)


@dataclass
class VCDData:
    """Parsed VCD contents."""

    timescale: str = ""
    signals: dict[str, VCDSignal] = field(default_factory=dict)
    transitions: dict[str, list[tuple[int, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    end_time: int = 0


def parse_vcd(path: Path) -> VCDData:
    """Parse a VCD file into structured data."""
    data = VCDData()
    scope_stack: list[str] = []
    current_time = 0

    with Path(path).open(encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            # Header keywords
            result = _parse_vcd_header_line(line, data, scope_stack, f)
            if result is not None:
                continue

            # Timestamp
            if line.startswith("#"):
                current_time = _parse_vcd_timestamp(line, current_time, data)
                continue

            # Value changes
            _parse_vcd_value_change(line, current_time, data)

    return data


def _parse_vcd_header_line(
    line: str,
    data: VCDData,
    scope_stack: list[str],
    f,
) -> bool | None:
    """Handle VCD header/keyword lines. Returns True if consumed, None if not."""
    if line.startswith("$timescale"):
        if "$end" in line:
            data.timescale = line.replace("$timescale", "").replace("$end", "").strip()
        else:
            data.timescale = next(f, "").strip()
        return True
    if line.startswith("$scope"):
        parts = line.split()
        if len(parts) >= 3:
            scope_stack.append(parts[2])
        return True
    if line.startswith("$upscope"):
        if scope_stack:
            scope_stack.pop()
        return True
    if line.startswith("$var"):
        _parse_vcd_var(line, data, scope_stack)
        return True
    if line.startswith(
        (
            "$enddefinitions",
            "$dumpvars",
            "$dumpoff",
            "$dumpon",
            "$dumpall",
            "$end",
            "$comment",
            "$date",
            "$version",
        )
    ):
        return True
    return None


def _parse_vcd_var(line: str, data: VCDData, scope_stack: list[str]) -> None:
    """Parse a $var line and register the signal."""
    parts = line.split()
    if len(parts) < 5:
        return
    try:
        size = int(parts[2])
    except ValueError:
        # Malformed $var width field — skip this signal rather than crash the
        # whole parse (consistent with the guarded timestamp parse below).
        return
    code = parts[3]
    raw_name = parts[4]
    if len(parts) >= 6 and parts[5].startswith("[") and not parts[5].startswith("$"):
        raw_name += parts[5]
    name = _strip_bit_range(raw_name)
    scope = ".".join(scope_stack)
    full_name = f"{scope}.{name}" if scope else name
    data.signals[code] = VCDSignal(
        code=code,
        size=size,
        name=name,
        scope=scope,
        full_name=full_name,
    )


def _parse_vcd_timestamp(line: str, current_time: int, data: VCDData) -> int:
    """Parse a timestamp line, update end_time, return new current_time."""
    try:
        t = int(line[1:])
        data.end_time = t
        return t
    except ValueError:
        return current_time


def _parse_vcd_value_change(line: str, current_time: int, data: VCDData) -> None:
    """Parse a value change line (scalar or vector)."""
    if line[0] in "01xXzZ":
        code = line[1:]
        if code in data.signals:
            data.transitions[code].append((current_time, line[0]))
    elif line[0] in "bB":
        parts = line.split()
        if len(parts) == 2 and parts[1] in data.signals:
            data.transitions[parts[1]].append((current_time, parts[0]))
    elif "\t" in line and line[0] in "bB":
        parts = line.split("\t")
        if len(parts) == 2:
            code = parts[1].strip()
            if code in data.signals:
                data.transitions[code].append((current_time, parts[0]))


# ---------------------------------------------------------------------------
# Signal matching between two VCD files
# ---------------------------------------------------------------------------


def build_dut_signal_map(
    vcd: VCDData,
    dut_scope: str | None = None,
    dut_instance: str = _DEFAULT_DUT_INSTANCE,
) -> dict[str, tuple[str, VCDSignal]]:
    """Build a map of DUT-relative signal name -> (code, signal).

    Filters to signals under the DUT hierarchy.  If ``dut_scope`` is given,
    matches signals whose scope starts with it; otherwise auto-detects by
    looking for a component matching ``dut_instance`` in the hierarchy.
    ``dut_instance`` defaults to ``"dut"`` — callers with a populated
    ``dut_info`` should pass the leaf of ``dut_info.dut_hier_path``.
    """
    result: dict[str, tuple[str, VCDSignal]] = {}

    for code, sig in vcd.signals.items():
        full = sig.full_name
        matched_rel = None

        if dut_scope:
            prefix = dut_scope.rstrip(".")
            if full.startswith(prefix + "."):
                matched_rel = full[len(prefix) + 1 :]
            elif full == prefix:
                matched_rel = sig.name
        else:
            parts = full.split(".")
            for i, p in enumerate(parts):
                if p == dut_instance and i + 1 < len(parts):
                    matched_rel = ".".join(parts[i + 1 :])
                    break

        if matched_rel is not None:
            # Normalize: strip bit ranges from the key for cross-sim matching
            matched_rel = _strip_bit_range(matched_rel)
            result[matched_rel] = (code, sig)

    return result


def normalize_value(val: str, width: int) -> str:
    """Normalize a VCD value for comparison.

    Strips 'b'/'B' prefix, zero-extends to width, lowercases x/z.
    """
    val = val.lower()
    if val.startswith("b"):
        val = val[1:]

    # A bare "b" (or otherwise empty) value has no MSB to extend from; treat it
    # as all-zeros for the target width instead of indexing an empty string.
    if not val:
        return "0" * width

    if len(val) < width:
        extend_char = "0" if val[0] in "01" else val[0]
        val = extend_char * (width - len(val)) + val

    return val


# ---------------------------------------------------------------------------
# Comparison engine
# ---------------------------------------------------------------------------


@dataclass
class ComparisonResult:
    total_signals: int = 0
    matched_signals: int = 0
    mismatched_signals: int = 0
    sim_a_only: list[str] = field(default_factory=list)
    sim_b_only: list[str] = field(default_factory=list)
    mismatches: list[dict] = field(default_factory=list)
    signal_details: dict[str, dict] = field(default_factory=dict)


@dataclass
class _CompareWindow:
    """Comparison-time-window + per-signal cap, bound once and reused per signal."""

    start_time: int
    end_time: int | None
    max_mismatches: int


def compare_signals(
    sim_a: VCDData,
    sim_b: VCDData,
    dut_scope_a: str | None = None,
    dut_scope_b: str | None = None,
    start_time: int = 0,
    end_time: int | None = None,
    ignore_patterns: list[str] | None = None,
    max_mismatches_per_signal: int = 5,
    dut_instance: str = _DEFAULT_DUT_INSTANCE,
) -> ComparisonResult:
    """Compare DUT signals between two simulator VCD files.

    ``dut_instance`` is the leaf instance name used by the literal-name
    heuristic when no ``dut_scope_*`` is provided.  Derive it from
    ``dut_info.dut_hier_path`` via ``_leaf_from_hier_path`` for
    runs with a seeded dut_info; defaults to ``"dut"`` for legacy callers.
    """
    result = ComparisonResult()

    map_a = build_dut_signal_map(sim_a, dut_scope_a, dut_instance=dut_instance)
    map_b = build_dut_signal_map(sim_b, dut_scope_b, dut_instance=dut_instance)

    ignore_re = [re.compile(p) for p in (ignore_patterns or [])]

    def should_ignore(name: str) -> bool:
        return any(r.search(name) for r in ignore_re)

    all_names = sorted(set(map_a.keys()) | set(map_b.keys()))
    result.total_signals = len(all_names)

    window = _CompareWindow(start_time, end_time, max_mismatches_per_signal)

    for name in all_names:
        if should_ignore(name):
            continue
        if name not in map_a:
            result.sim_b_only.append(name)
            continue
        if name not in map_b:
            result.sim_a_only.append(name)
            continue

        sig_mismatches, detail = _compare_single_signal(
            name,
            map_a[name],
            map_b[name],
            sim_a,
            sim_b,
            window,
        )
        result.signal_details[name] = detail
        if sig_mismatches:
            result.mismatched_signals += 1
            result.mismatches.extend(sig_mismatches)
        else:
            result.matched_signals += 1

    return result


def _collect_mismatches(
    name: str,
    vals_a: dict[int, str],
    vals_b: dict[int, str],
    initial_a: str | None,
    initial_b: str | None,
    max_mismatches: int,
) -> list[dict]:
    """Walk merged timestamps, return mismatch dicts."""
    all_times = sorted(set(vals_a.keys()) | set(vals_b.keys()))
    current_a, current_b = initial_a, initial_b
    sig_mismatches: list[dict] = []

    for t in all_times:
        if t in vals_a:
            current_a = vals_a[t]
        if t in vals_b:
            current_b = vals_b[t]
        if (
            current_a is not None
            and current_b is not None
            and current_a != current_b
            and len(sig_mismatches) < max_mismatches
        ):
            sig_mismatches.append(
                {
                    "signal": name,
                    "time": t,
                    "sim_a": current_a,
                    "sim_b": current_b,
                }
            )
    return sig_mismatches


def _compare_single_signal(
    name: str,
    a_entry: tuple[str, VCDSignal],
    b_entry: tuple[str, VCDSignal],
    sim_a: VCDData,
    sim_b: VCDData,
    window: _CompareWindow,
) -> tuple[list[dict], dict]:
    """Compare transitions for a single signal.

    Returns (mismatches, signal_detail) for the caller to fold into
    ``ComparisonResult`` — this function has no side effects on shared state.
    """
    a_code, a_sig = a_entry
    b_code, b_sig = b_entry

    vals_a = _build_value_at_time(
        sim_a.transitions.get(a_code, []),
        a_sig.size,
        window.start_time,
        window.end_time,
    )
    vals_b = _build_value_at_time(
        sim_b.transitions.get(b_code, []),
        b_sig.size,
        window.start_time,
        window.end_time,
    )

    sig_mismatches = _collect_mismatches(
        name,
        vals_a,
        vals_b,
        _initial_value(sim_a.transitions.get(a_code, []), a_sig.size, window.start_time),
        _initial_value(sim_b.transitions.get(b_code, []), b_sig.size, window.start_time),
        window.max_mismatches,
    )

    if sig_mismatches:
        detail = {
            "status": "MISMATCH",
            "count": len(sig_mismatches),
            "first_time": sig_mismatches[0]["time"],
        }
    else:
        detail = {"status": "MATCH"}
    return sig_mismatches, detail


def _build_value_at_time(
    transitions: list[tuple[int, str]],
    width: int,
    start_time: int,
    end_time: int | None,
) -> dict[int, str]:
    """Build a {timestamp: normalized_value} dict within the time window."""
    vals: dict[int, str] = {}
    for t, v in transitions:
        if end_time is not None and t > end_time:
            break
        if t >= start_time:
            vals[t] = normalize_value(v, width)
    return vals


def _initial_value(
    transitions: list[tuple[int, str]],
    width: int,
    start_time: int,
) -> str | None:
    """Find the last value before start_time (initial state)."""
    current = None
    for t, v in transitions:
        if t < start_time:
            current = normalize_value(v, width)
        else:
            break
    return current


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(
    res: ComparisonResult,
    summary_only: bool = False,
    sim_a_name: str = "Sim-A",
    sim_b_name: str = "Sim-B",
) -> None:
    """Print a human-readable comparison report."""
    print("=" * 70)
    print(f"VCD Comparison Report: {sim_a_name} vs {sim_b_name}")
    print("=" * 70)

    print(f"\nTotal DUT signals compared: {res.total_signals}")
    print(f"  Matching:       {res.matched_signals}")
    print(f"  Mismatched:     {res.mismatched_signals}")
    print(f"  {sim_a_name}-only:    {len(res.sim_a_only)}")
    print(f"  {sim_b_name}-only: {len(res.sim_b_only)}")

    if res.sim_a_only and not summary_only:
        print(f"\n--- Signals only in {sim_a_name} ({len(res.sim_a_only)}) ---")
        for s in res.sim_a_only[:20]:
            print(f"  {s}")
        if len(res.sim_a_only) > 20:
            print(f"  ... and {len(res.sim_a_only) - 20} more")

    if res.sim_b_only and not summary_only:
        print(f"\n--- Signals only in {sim_b_name} ({len(res.sim_b_only)}) ---")
        for s in res.sim_b_only[:20]:
            print(f"  {s}")
        if len(res.sim_b_only) > 20:
            print(f"  ... and {len(res.sim_b_only) - 20} more")

    if res.mismatches and not summary_only:
        print(f"\n--- Mismatches ({len(res.mismatches)} transitions) ---")
        for m in res.mismatches[:50]:
            print(f"  t={m['time']:>10}  {m['signal']}")
            print(f"    {sim_a_name}: {m['sim_a']}")
            print(f"    {sim_b_name}: {m['sim_b']}")
        if len(res.mismatches) > 50:
            print(f"  ... and {len(res.mismatches) - 50} more")

    print("\n" + "=" * 70)
    if res.mismatched_signals == 0 and not res.sim_a_only and not res.sim_b_only:
        print("VERDICT: PASS — all DUT signals match exactly")
    elif res.mismatched_signals == 0:
        print(
            "VERDICT: PASS (with caveats) — matching signals agree, "
            f"but {len(res.sim_a_only)} {sim_a_name}-only / "
            f"{len(res.sim_b_only)} {sim_b_name}-only signals"
        )
    else:
        print(f"VERDICT: FAIL — {res.mismatched_signals} signal(s) have mismatches")
    print("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for VCD comparison."""
    parser = argparse.ArgumentParser(
        description="Compare VCD traces from two simulators",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("vcd_a", type=Path, help="First simulator VCD file")
    parser.add_argument("vcd_b", type=Path, help="Second simulator VCD file")
    parser.add_argument(
        "--sim-a-name", default="Sim-A", help="Label for first simulator (default: Sim-A)"
    )
    parser.add_argument(
        "--sim-b-name", default="Sim-B", help="Label for second simulator (default: Sim-B)"
    )
    parser.add_argument(
        "--dut-scope-a",
        default=None,
        help="DUT scope prefix for first VCD (auto-detected if omitted)",
    )
    parser.add_argument(
        "--dut-scope-b",
        default=None,
        help="DUT scope prefix for second VCD (auto-detected if omitted)",
    )
    parser.add_argument(
        "--dut-instance",
        default=_DEFAULT_DUT_INSTANCE,
        help="DUT instance leaf name used by the auto-detect "
        "heuristic (default: 'dut'). Pass the leaf of "
        "dut_info.dut_hier_path when known.",
    )
    parser.add_argument(
        "--dut-hier-path",
        default=None,
        help="Full DUT hierarchy path (e.g. tb.gen[0].uut); "
        "the leaf is used as --dut-instance. Convenience "
        "for passing dut_info.dut_hier_path directly.",
    )
    parser.add_argument(
        "--start-time", type=int, default=0, help="Start comparison at this timestamp"
    )
    parser.add_argument(
        "--end-time", type=int, default=None, help="End comparison at this timestamp"
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="Regex pattern for signals to ignore (repeatable)",
    )
    parser.add_argument(
        "--summary", action="store_true", help="Print only summary, no individual mismatches"
    )
    parser.add_argument(
        "--max-per-signal",
        type=int,
        default=5,
        help="Max mismatches to report per signal (default 5)",
    )
    return parser


def _parse_and_compare(args) -> ComparisonResult:
    """Parse both VCD files and run comparison."""
    print(f"Parsing {args.sim_a_name} VCD: {args.vcd_a}")
    sim_a = parse_vcd(args.vcd_a)
    print(f"  -> {len(sim_a.signals)} signals, end time {sim_a.end_time}")

    print(f"Parsing {args.sim_b_name} VCD: {args.vcd_b}")
    sim_b = parse_vcd(args.vcd_b)
    print(f"  -> {len(sim_b.signals)} signals, end time {sim_b.end_time}")

    dut_instance = args.dut_instance
    if getattr(args, "dut_hier_path", None):
        dut_instance = _leaf_from_hier_path(args.dut_hier_path)

    return compare_signals(
        sim_a,
        sim_b,
        dut_scope_a=args.dut_scope_a,
        dut_scope_b=args.dut_scope_b,
        start_time=args.start_time,
        end_time=args.end_time,
        ignore_patterns=args.ignore,
        max_mismatches_per_signal=args.max_per_signal,
        dut_instance=dut_instance,
    )


def main() -> None:
    args = _build_arg_parser().parse_args()

    for p in [args.vcd_a, args.vcd_b]:
        if not p.exists():
            sys.exit(f"ERROR: File not found: {p}")

    result = _parse_and_compare(args)

    print_report(
        result, summary_only=args.summary, sim_a_name=args.sim_a_name, sim_b_name=args.sim_b_name
    )
    sys.exit(0 if result.mismatched_signals == 0 else 1)


if __name__ == "__main__":
    main()
