"""Normalize static imports from an in-repository Python package."""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

NAMED_HOTSPOTS = (
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


@dataclass(frozen=True)
class Dependency:
    """One normalized source-module dependency at its source location."""

    source: str
    target: str
    path: Path
    line: int
    column: int


@dataclass(frozen=True)
class ModuleFanOut:
    """The unique in-repository modules known by one source module."""

    source: str
    targets: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.targets)


@dataclass(frozen=True)
class DependencyGraphSnapshot:
    """Immutable diagnostics for one exact package source tree."""

    modules: tuple[str, ...]
    dependency_fact_count: int
    edges: tuple[tuple[str, str], ...]
    mutual_pairs: tuple[tuple[str, str], ...]
    scc_membership: tuple[tuple[str, tuple[str, ...]], ...]
    hotspot_fan_out: tuple[tuple[str, int], ...]

    @property
    def parsed_module_count(self) -> int:
        return len(self.modules)


@dataclass(frozen=True)
class SccMembershipTransition:
    """One package's before/after strongly connected component membership."""

    package: str
    before: tuple[str, ...] | None
    after: tuple[str, ...] | None


@dataclass(frozen=True)
class FanOutTransition:
    """One named importer's changed unique-module fan-out."""

    source: str
    before: int
    after: int

    @property
    def delta(self) -> int:
        return self.after - self.before


@dataclass(frozen=True)
class DependencyGraphDiff:
    """Deterministic informational differences between two graph snapshots."""

    added_edges: tuple[tuple[str, str], ...]
    removed_edges: tuple[tuple[str, str], ...]
    added_mutual_pairs: tuple[tuple[str, str], ...]
    removed_mutual_pairs: tuple[tuple[str, str], ...]
    scc_transitions: tuple[SccMembershipTransition, ...]
    fan_out_transitions: tuple[FanOutTransition, ...]


@dataclass(frozen=True)
class _ModuleSource:
    name: str
    path: Path
    is_package: bool


def analyze_imports(source_root: Path) -> tuple[Dependency, ...]:
    """Return every static import that resolves within ``source_root``."""
    modules = _discover_modules(source_root)
    known_modules = frozenset(modules)
    dependencies: set[Dependency] = set()
    for source in modules.values():
        tree = ast.parse(source.path.read_text(encoding="utf-8"), filename=str(source.path))
        for node in ast.walk(tree):
            targets = _node_targets(node, source, known_modules)
            dependencies.update(
                Dependency(source.name, target, source.path, node.lineno, node.col_offset)
                for target in targets
            )
    return tuple(sorted(dependencies, key=_dependency_key))


def dependency_graph_snapshot(
    source_root: Path,
    *,
    named_hotspots: tuple[str, ...] = NAMED_HOTSPOTS,
) -> DependencyGraphSnapshot:
    """Analyze one package tree into immutable comparison diagnostics."""
    modules = tuple(sorted(_discover_modules(source_root)))
    dependencies = analyze_imports(source_root)
    edges = tuple(sorted({(item.source, item.target) for item in dependencies}))
    memberships = _scc_memberships(modules, dependencies)
    fan_out = {item.source: item.count for item in file_fan_out(dependencies)}
    return DependencyGraphSnapshot(
        modules=modules,
        dependency_fact_count=len(dependencies),
        edges=edges,
        mutual_pairs=mutual_package_pairs(dependencies),
        scc_membership=tuple(sorted(memberships.items())),
        hotspot_fan_out=tuple((source, fan_out.get(source, 0)) for source in named_hotspots),
    )


def compare_dependency_graphs(
    before: DependencyGraphSnapshot,
    after: DependencyGraphSnapshot,
) -> DependencyGraphDiff:
    """Return a pure, deterministic comparison of two graph snapshots."""
    before_membership = dict(before.scc_membership)
    after_membership = dict(after.scc_membership)
    transitions = tuple(
        SccMembershipTransition(
            package,
            before_membership.get(package),
            after_membership.get(package),
        )
        for package in sorted(before_membership.keys() | after_membership.keys())
        if before_membership.get(package) != after_membership.get(package)
    )
    before_fan_out = dict(before.hotspot_fan_out)
    after_fan_out = dict(after.hotspot_fan_out)
    fan_out_transitions = tuple(
        FanOutTransition(source, before_fan_out.get(source, 0), after_fan_out.get(source, 0))
        for source in dict.fromkeys((*before_fan_out, *after_fan_out))
        if before_fan_out.get(source, 0) != after_fan_out.get(source, 0)
    )
    return DependencyGraphDiff(
        added_edges=tuple(sorted(set(after.edges) - set(before.edges))),
        removed_edges=tuple(sorted(set(before.edges) - set(after.edges))),
        added_mutual_pairs=tuple(sorted(set(after.mutual_pairs) - set(before.mutual_pairs))),
        removed_mutual_pairs=tuple(sorted(set(before.mutual_pairs) - set(after.mutual_pairs))),
        scc_transitions=transitions,
        fan_out_transitions=fan_out_transitions,
    )


def select_dependencies(
    dependencies: tuple[Dependency, ...],
    *,
    source_prefixes: tuple[str, ...] = (),
    target_prefixes: tuple[str, ...] = (),
) -> tuple[Dependency, ...]:
    """Select dependencies whose ends match the supplied module prefixes."""
    return tuple(
        dependency
        for dependency in dependencies
        if _matches_any(dependency.source, source_prefixes)
        and _matches_any(dependency.target, target_prefixes)
    )


def top_level_package_sccs(
    dependencies: tuple[Dependency, ...],
) -> tuple[tuple[str, ...], ...]:
    """Return deterministic SCCs after projection to ``booley.<package>`` owners."""
    adjacency = _package_adjacency(dependencies)
    finishing_order = _finishing_order(adjacency)
    reverse = _reverse_adjacency(adjacency)
    assigned: set[str] = set()
    groups: list[tuple[str, ...]] = []
    for package in reversed(finishing_order):
        if package in assigned:
            continue
        members = _reachable(package, reverse, assigned)
        groups.append(tuple(sorted(members)))
    return tuple(sorted(groups))


def mutual_package_pairs(
    dependencies: tuple[Dependency, ...],
) -> tuple[tuple[str, str], ...]:
    """Return top-level package pairs with direct edges in both directions."""
    adjacency = _package_adjacency(dependencies)
    return tuple(
        (source, target)
        for source in sorted(adjacency)
        for target in sorted(adjacency[source])
        if source < target and source in adjacency[target]
    )


def file_fan_out(dependencies: tuple[Dependency, ...]) -> tuple[ModuleFanOut, ...]:
    """Return deterministic unique target modules for each importing module."""
    targets_by_source: dict[str, set[str]] = defaultdict(set)
    for dependency in dependencies:
        targets_by_source[dependency.source].add(dependency.target)
    return tuple(
        ModuleFanOut(source, tuple(sorted(targets)))
        for source, targets in sorted(targets_by_source.items())
    )


def _discover_modules(source_root: Path) -> dict[str, _ModuleSource]:
    root = source_root.resolve()
    modules: dict[str, _ModuleSource] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        is_package = path.name == "__init__.py"
        suffix = relative.parent.parts if is_package else (*relative.parent.parts, path.stem)
        name = ".".join((root.name, *suffix))
        if name in modules:
            raise ValueError(f"multiple source files resolve to module {name}")
        modules[name] = _ModuleSource(name, path, is_package)
    if root.name not in modules:
        raise ValueError(f"source root is not a Python package: {root}")
    return modules


def _node_targets(
    node: ast.AST,
    source: _ModuleSource,
    known_modules: frozenset[str],
) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names if alias.name in known_modules)
    if not isinstance(node, ast.ImportFrom):
        return ()
    base = _from_base(node, source)
    if not base:
        return ()
    targets = []
    for alias in node.names:
        candidate = f"{base}.{alias.name}"
        if alias.name != "*" and candidate in known_modules:
            targets.append(candidate)
        elif base in known_modules:
            targets.append(base)
    return tuple(targets)


def _from_base(node: ast.ImportFrom, source: _ModuleSource) -> str | None:
    if node.level == 0:
        return node.module
    package = source.name if source.is_package else source.name.rpartition(".")[0]
    parts = package.split(".")
    parents = node.level - 1
    if parents >= len(parts):
        return None
    base = parts[: len(parts) - parents]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _matches_any(module: str, prefixes: tuple[str, ...]) -> bool:
    return not prefixes or any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes
    )


def _dependency_key(dependency: Dependency) -> tuple[str, str, str, int, int]:
    return (
        dependency.source,
        dependency.target,
        dependency.path.as_posix(),
        dependency.line,
        dependency.column,
    )


def top_level_package(module: str) -> str | None:
    """Return the immediate ``booley.<package>`` owner of a module."""
    parts = module.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else None


def _package_adjacency(dependencies: tuple[Dependency, ...]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for dependency in dependencies:
        source = top_level_package(dependency.source)
        target = top_level_package(dependency.target)
        if source is None or target is None:
            continue
        adjacency[source]
        adjacency[target]
        if source != target:
            adjacency[source].add(target)
    return dict(adjacency)


def _scc_memberships(
    modules: tuple[str, ...], dependencies: tuple[Dependency, ...]
) -> dict[str, tuple[str, ...]]:
    packages = {
        package for module in modules if (package := top_level_package(module)) is not None
    }
    memberships = {package: (package,) for package in packages}
    for group in top_level_package_sccs(dependencies):
        for package in group:
            memberships[package] = group
    return memberships


def _finishing_order(adjacency: dict[str, set[str]]) -> list[str]:
    visited: set[str] = set()
    order: list[str] = []

    def visit(package: str) -> None:
        visited.add(package)
        for target in sorted(adjacency[package]):
            if target not in visited:
                visit(target)
        order.append(package)

    for package in sorted(adjacency):
        if package not in visited:
            visit(package)
    return order


def _reverse_adjacency(adjacency: dict[str, set[str]]) -> dict[str, set[str]]:
    reverse = {package: set() for package in adjacency}
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].add(source)
    return reverse


def _reachable(start: str, adjacency: dict[str, set[str]], assigned: set[str]) -> set[str]:
    pending = [start]
    members: set[str] = set()
    while pending:
        package = pending.pop()
        if package in assigned:
            continue
        assigned.add(package)
        members.add(package)
        pending.extend(sorted(adjacency[package] - assigned, reverse=True))
    return members
