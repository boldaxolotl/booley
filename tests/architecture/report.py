"""Print reproducible diagnostics for Booley's source dependency graph."""

from __future__ import annotations

import argparse
from pathlib import Path

from import_graph import (
    analyze_imports,
    file_fan_out,
    mutual_package_pairs,
    top_level_package_sccs,
)

_NAMED_HOTSPOTS = (
    "booley.harness.doctor",
    "booley.harness.booley",
    "booley.harness.init_cmd",
    "booley.harness.developer",
    "booley.flows.sim.flow",
    "booley.flows.synth.flow",
    "booley.mcp.server",
    "booley.flows.fpga.flow",
    "booley.specialists.mutation_tester",
    "booley.specialists.coverage_analyst",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path("src/booley"))
    parser.add_argument("--top", type=_positive_int, default=20)
    args = parser.parse_args()
    dependencies = analyze_imports(args.source_root)
    unique_edges = {(item.source, item.target) for item in dependencies}
    cyclic_groups = tuple(
        group for group in top_level_package_sccs(dependencies) if len(group) > 1
    )
    fan_out = sorted(file_fan_out(dependencies), key=lambda item: (-item.count, item.source))

    print(f"Parsed Python modules: {len(tuple(args.source_root.rglob('*.py')))}")
    print(f"Normalized dependency facts: {len(dependencies)}")
    print(f"Unique normalized edges: {len(unique_edges)}")
    print("\nCyclic top-level package groups:")
    for group in cyclic_groups:
        print(f"- {', '.join(group)}")
    print("\nMutual top-level package pairs:")
    for source, target in mutual_package_pairs(dependencies):
        print(f"- {source} <-> {target}")
    print(f"\nTop {args.top} file fan-out:")
    for item in fan_out[: args.top]:
        print(f"- {item.source}: {item.count}")
    fan_out_by_source = {item.source: item.count for item in fan_out}
    print("\nNamed composition hotspot fan-out (diagnostic only):")
    for source in _NAMED_HOTSPOTS:
        print(f"- {source}: {fan_out_by_source.get(source, 0)}")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


if __name__ == "__main__":
    main()
