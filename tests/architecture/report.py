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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path("src/booley"))
    parser.add_argument("--top", type=int, default=20)
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


if __name__ == "__main__":
    main()
