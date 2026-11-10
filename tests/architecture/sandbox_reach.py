"""Host reachability of Booley source modules, and which tests cover them.

Backs ``test_sandbox_only_tests.py``. A module is *host reachable* when a command that runs
on the host (``HOST`` or ``EITHER`` in ``COMMAND_LOCATIONS``) can import it. Host code runs on
Windows, so a module that only Sandbox-only tests cover would lose all Windows coverage.
Graph semantics are those of ``import_graph``; this module adds the cut from the Sandbox
command handlers and a few declared Sandbox-only lazy imports.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from tests.architecture.import_graph import (
    SourceModule,
    SourceTreeAnalysis,
    analyze_source_tree,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "booley"
TESTS_ROOT = REPOSITORY_ROOT / "tests"
HARNESS_MODULE = "booley.harness.booley"

# Console scripts that only run inside the Sandbox. Any other script is host code, so a new
# script fails the guard until it is classified here.
SANDBOX_ENTRY_POINTS = frozenset({"booley-mcp"})

# Test directories whose tests inspect source text or the import graph rather than exercise
# the modules they name, so they do not count as coverage.
NON_COVERING_TEST_DIRECTORIES = ("tests/architecture/",)

_IMPORT_SITES = "import-sites"
_MODULE_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class LazyImportCut:
    """Function-level imports that run only on the Sandbox MCP call path.

    ``callers`` is the exact set of host-reachable modules (other than ``source``) that
    reference any function in ``functions`` by name. A new caller changes the set and fails
    the guard until the cut is re-justified or dropped. ``internal_callers`` is the exact set
    of top-level names inside ``source`` (``"<module>"`` for module-level statements) that
    reference those functions from outside them; each was read to confirm it runs only on the
    Sandbox MCP call path. Direct references only: an indirect host caller through one of
    these modules is a reviewed residual risk.
    """

    source: str
    targets: tuple[str, ...]
    functions: tuple[str, ...]
    callers: frozenset[str]
    reason: str
    internal_callers: frozenset[str] = frozenset()


# Each cut was confirmed by reading every caller of its enclosing functions.
SANDBOX_ONLY_LAZY_IMPORTS: tuple[LazyImportCut, ...] = (
    LazyImportCut(
        source="booley.mcp.catalog",
        targets=("booley.goals.rules", "booley.mcp.flow_adapter", "booley.mcp.goal_tools"),
        functions=(
            "_default_interactive_definitions",
            "_find_mcp_tool_class_in_module",
            "_import_and_extract",
            "visible_definitions",
            "work_dir_guidance",
        ),
        callers=frozenset({"booley.mcp.call_context"}),
        internal_callers=frozenset({"discover_builtin_definitions"}),
        reason=(
            "MCP server catalog build (mcp/server.py, mcp/catalog_worker.py) and the Goal "
            "work-dir refusal raised inside an MCP call (mcp/call_context.py)"
        ),
    ),
    LazyImportCut(
        source="booley.mcp.flow_execution_selection",
        targets=(
            "booley.flows.sim.unpublished_results",
            "booley.goals.flow_execution",
            "booley.mcp.goal_freshness",
        ),
        functions=("_goal_acceptance_recorder",),
        callers=frozenset(),
        internal_callers=frozenset({"select_flow_execution"}),
        reason=(
            "the Goal recorder builder runs only for a Goal-bound MCP call; the unbound "
            "recorder is not cut because every McpTool constructor builds it on the host"
        ),
    ),
    LazyImportCut(
        source="booley.mcp.call_context",
        targets=("booley.mcp.sim_baseline_jobs",),
        functions=("_goal_call_context",),
        callers=frozenset(),
        internal_callers=frozenset({"resolve_call_context"}),
        reason="builds the call context of an MCP tool call bound to a Goal",
    ),
    LazyImportCut(
        source="booley.flows.endpoint_acceptance",
        targets=("booley.criteria.actions",),
        functions=("_pending_criterion_invocations",),
        callers=frozenset(),
        internal_callers=frozenset({"_criterion_binding_gate"}),
        reason=(
            "endpoint acceptance during execution, which Doctor runs only through a Sandbox "
            "subprocess"
        ),
    ),
)


@dataclass(frozen=True)
class ImportEdge:
    """One executed import: ``function`` is the outermost enclosing def, or ``""``."""

    source: str
    target: str
    function: str


@dataclass(frozen=True)
class SourceGraph:
    """Executed import edges of the production tree and its module names."""

    modules: frozenset[str]
    edges: tuple[ImportEdge, ...]


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_sites_scan(source: SourceModule, tree: ast.Module) -> list[str]:
    """Return ``module|line|column|function|type_checking`` for every import statement."""
    sites: list[str] = []

    def visit(nodes: Iterable[ast.AST], function: str, type_checking: bool) -> None:
        for node in nodes:
            if isinstance(node, ast.Import | ast.ImportFrom):
                sites.append(
                    f"{source.name}|{node.lineno}|{node.col_offset}|{function}|{int(type_checking)}"
                )
            elif isinstance(node, ast.If) and _is_type_checking(node.test):
                visit(node.body, function, True)
                visit(node.orelse, function, type_checking)
            else:
                inner = function or (node.name if isinstance(node, _FUNCTIONS) else "")
                for _, value in ast.iter_fields(node):
                    children = value if isinstance(value, list) else [value]
                    visit((c for c in children if isinstance(c, ast.AST)), inner, type_checking)

    visit(tree.body, "", False)
    return sites


@cache
def load_source_graph() -> SourceGraph:
    """Parse the production tree once; drop ``TYPE_CHECKING`` imports, which never execute."""
    analysis: SourceTreeAnalysis = analyze_source_tree(
        SOURCE_ROOT, {_IMPORT_SITES: _import_sites_scan}
    )
    sites: dict[tuple[str, int, int], tuple[str, bool]] = {}
    for site in analysis.findings(_IMPORT_SITES):
        module, line, column, function, type_checking = site.split("|")
        sites[(module, int(line), int(column))] = (function, type_checking == "1")
    edges: set[ImportEdge] = set()
    for dependency in analysis.dependencies:
        function, type_checking = sites[(dependency.source, dependency.line, dependency.column)]
        if not type_checking:
            edges.add(ImportEdge(dependency.source, dependency.target, function))
    return SourceGraph(frozenset(_module_names()), tuple(sorted(edges, key=repr)))


def _module_names() -> set[str]:
    names: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        parts = path.relative_to(SOURCE_ROOT.parent).with_suffix("").parts
        names.add(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def _parents(module: str) -> list[str]:
    parts = module.split(".")
    return [".".join(parts[:end]) for end in range(len(parts) - 1, 0, -1)]


def reachable_paths(
    adjacency: Mapping[str, Iterable[str]], roots: Iterable[str], modules: frozenset[str]
) -> dict[str, tuple[str, ...]]:
    """Return each module reachable from ``roots`` with its shortest import path.

    Importing a module also imports its parent packages, so those are neighbors too.
    """
    ordered_roots = sorted(set(roots) & modules)
    paths: dict[str, tuple[str, ...]] = {root: (root,) for root in ordered_roots}
    queue = deque(ordered_roots)
    while queue:
        module = queue.popleft()
        neighbors = sorted(set(adjacency.get(module, ())) | set(_parents(module)))
        for neighbor in neighbors:
            if neighbor in modules and neighbor not in paths:
                paths[neighbor] = (*paths[module], neighbor)
                queue.append(neighbor)
    return paths


# --- Host roots ------------------------------------------------------------------------


def _top_level_functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {node.name: node for node in tree.body if isinstance(node, _FUNCTIONS)}


def _referenced_names(node: ast.AST) -> set[str]:
    names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
    return names | {child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)}


def _literal_assignment(tree: ast.Module, name: str) -> ast.expr:
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return node.value
    raise AssertionError(f"{HARNESS_MODULE} no longer annotates and assigns {name}")


def pre_venue_commands(tree: ast.Module) -> frozenset[str]:
    """Commands whose endpoint help runs on the host: the set literal in the help check."""
    function = _top_level_functions(tree)["_endpoint_help_candidate"]
    literals = [node for node in ast.walk(function) if isinstance(node, ast.Set)]
    assert len(literals) == 1, "_endpoint_help_candidate must name its commands in one set"
    return frozenset(
        element.value
        for element in literals[0].elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    )


def sandbox_handlers(tree: ast.Module, session_runtime_commands: Iterable[str]) -> frozenset[str]:
    """Names of the handlers that dispatch ``SESSION_RUNTIME`` commands and run no help early."""
    dispatch = _literal_assignment(tree, "_EARLY_COMMANDS")
    assert isinstance(dispatch, ast.Dict)
    handlers: dict[str, str] = {}
    for key, value in zip(dispatch.keys, dispatch.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(value, ast.Name):
            handlers[str(key.value)] = value.id
    wanted = set(session_runtime_commands) - pre_venue_commands(tree)
    missing = wanted - handlers.keys()
    assert not missing, f"Sandbox commands without an _EARLY_COMMANDS handler: {sorted(missing)}"
    return frozenset(handlers[command] for command in wanted)


def reachable_harness_functions(tree: ast.Module, stop_at: frozenset[str]) -> frozenset[str]:
    """Top-level functions a host run reaches from ``main`` and module-level statements."""
    functions = _top_level_functions(tree)
    reached: set[str] = set()
    pending = ["main"]
    for node in tree.body:
        if not isinstance(node, _FUNCTIONS):
            pending.extend(_referenced_names(node))
    while pending:
        name = pending.pop()
        if name in reached or name in stop_at or name not in functions:
            continue
        reached.add(name)
        pending.extend(_referenced_names(functions[name]))
    return frozenset(reached)


def endpoint_modules() -> frozenset[str]:
    """Built-in Flow and Specialist modules that endpoint help imports on the host."""
    from booley.mcp.registry import discover_mcp_inventory

    modules: set[str] = set()
    for info in discover_mcp_inventory():
        if info.kind in {"flow", "specialist"}:
            path = Path(info.path)
            assert not path.is_absolute(), f"built-in endpoint {info.name} has an absolute path"
            modules.add(".".join(("booley", *path.parent.parts, path.stem)))
    return frozenset(modules)


def entry_point_modules() -> frozenset[str]:
    """Modules behind every console script that is not Sandbox-only."""
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts: Mapping[str, str] = pyproject["project"]["scripts"]
    return frozenset(
        target.partition(":")[0]
        for name, target in scripts.items()
        if name not in SANDBOX_ENTRY_POINTS
    )


@dataclass(frozen=True)
class HostReach:
    """Host-reachable modules with their shortest import path from a host root."""

    paths: Mapping[str, tuple[str, ...]]
    roots: frozenset[str]
    harness_functions: frozenset[str]


def host_reach(cuts: Iterable[LazyImportCut] | None = None) -> HostReach:
    """Compute host reachability from the harness, endpoint help, and host entry points."""
    from booley.harness.booley import COMMAND_LOCATIONS, CommandLocation

    graph = load_source_graph()
    tree = ast.parse((SOURCE_ROOT / "harness" / "booley.py").read_text(encoding="utf-8"))
    session_runtime = [
        command
        for command, location in COMMAND_LOCATIONS.items()
        if location is CommandLocation.SESSION_RUNTIME
    ]
    functions = reachable_harness_functions(tree, sandbox_handlers(tree, session_runtime))
    removed = {
        (cut.source, target, function)
        for cut in (SANDBOX_ONLY_LAZY_IMPORTS if cuts is None else cuts)
        for target in cut.targets
        for function in cut.functions
    }
    adjacency: dict[str, set[str]] = {}
    for edge in graph.edges:
        if (edge.source, edge.target, edge.function) in removed:
            continue
        if edge.source == HARNESS_MODULE and edge.function and edge.function not in functions:
            continue
        adjacency.setdefault(edge.source, set()).add(edge.target)
    roots = frozenset({*entry_point_modules(), *endpoint_modules(), HARNESS_MODULE})
    paths = reachable_paths(adjacency, roots, graph.modules)
    return HostReach(paths, roots, functions)


# --- Coverage --------------------------------------------------------------------------


def _booley_targets(node: ast.AST, known: frozenset[str]) -> set[str]:
    """Known source modules one absolute import statement names."""
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names if alias.name in known}
    if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
        return set()
    targets: set[str] = set()
    for alias in node.names:
        candidate = f"{node.module}.{alias.name}"
        if candidate in known:
            targets.add(candidate)
        elif node.module in known:
            targets.add(node.module)
    return targets


def _named_module(text: str, known: frozenset[str]) -> str | None:
    """The longest known module a string constant such as a patch target begins with."""
    match = _MODULE_NAME.match(text)
    if match is None:
        return None
    parts = match.group().split(".")
    for end in range(len(parts), 0, -1):
        if ".".join(parts[:end]) in known:
            return ".".join(parts[:end])
    return None


def _tests_module_path(dotted: str) -> str | None:
    base = REPOSITORY_ROOT / Path(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate.relative_to(REPOSITORY_ROOT).as_posix()
    return None


def _helper_imports(node: ast.AST, path: Path) -> set[str]:
    """Repository test modules one import statement names, as repository-relative paths."""
    names: list[str] = []
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            package = path.parent.relative_to(REPOSITORY_ROOT).parts
            package = package[: len(package) - (node.level - 1)]
            base = ".".join((*package, *([node.module] if node.module else [])))
        names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
    found = (_tests_module_path(name) for name in names if name.split(".")[0] == "tests")
    return {path_text for path_text in found if path_text is not None}


def _is_test_module(relative: str) -> bool:
    name = relative.rpartition("/")[2]
    return name.startswith("test_") and name.endswith(".py")


def module_test_coverage(known: frozenset[str]) -> dict[str, frozenset[str]]:
    """Map each source module to the test modules that cover it.

    A test module covers a source module it imports at any location, or names in a string
    constant (``mock.patch`` targets, ``import_module``). Non-test helpers under ``tests/``
    that a test imports count as part of that test, transitively; ``conftest.py`` does not.
    """
    direct: dict[str, set[str]] = {}
    helpers: dict[str, set[str]] = {}
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        if path.name == "conftest.py" or relative.startswith(NON_COVERING_TEST_DIRECTORIES):
            continue
        sources: set[str] = set()
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            sources |= _booley_targets(node, known)
            imported |= _helper_imports(node, path)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                named = _named_module(node.value, known)
                if named is not None:
                    sources.add(named)
        direct[relative] = sources
        helpers[relative] = {name for name in imported if not _is_test_module(name)}
    covering: dict[str, set[str]] = {}
    for relative in direct:
        if not _is_test_module(relative):
            continue
        seen: set[str] = set()
        pending = [relative]
        while pending:
            current = pending.pop()
            if current in seen or current not in direct:
                continue
            seen.add(current)
            pending.extend(helpers[current])
        for current in seen:
            for module in direct[current]:
                covering.setdefault(module, set()).add(relative)
    return {module: frozenset(tests) for module, tests in covering.items()}


# --- Guard core ------------------------------------------------------------------------


def uncovered_host_modules(
    reachable: Mapping[str, tuple[str, ...]],
    coverage: Mapping[str, frozenset[str]],
    is_sandbox_only: Callable[[str], bool],
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """Host-reachable modules whose covering tests are all Sandbox-only.

    Maps each failing module to ``(covering tests, import path from a host root)``. A module
    with no covering test is not reported: there is no Windows coverage to lose.
    """
    return {
        module: (tuple(sorted(tests)), reachable[module])
        for module, tests in sorted(coverage.items())
        if module in reachable and all(is_sandbox_only(test) for test in tests)
    }


# --- Cut checks ------------------------------------------------------------------------


@cache
def _names_by_module() -> dict[str, frozenset[str]]:
    """Every name each source module references: names, attributes, and imported aliases."""
    names: dict[str, frozenset[str]] = {}
    for module in _module_names():
        parts = module.split(".")[1:]
        path = SOURCE_ROOT.joinpath(*parts)
        file = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
        tree = ast.parse(file.read_text(encoding="utf-8"))
        aliases = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        names[module] = frozenset(_referenced_names(tree) | aliases)
    return names


def referencing_modules(
    reachable: Iterable[str], source: str, functions: Iterable[str]
) -> frozenset[str]:
    """Host-reachable modules other than ``source`` that name any of ``functions``."""
    wanted = set(functions)
    names = _names_by_module()
    return frozenset(
        module
        for module in reachable
        if module != source and wanted & names.get(module, frozenset())
    )


def internal_callers(source: str, functions: Iterable[str]) -> frozenset[str]:
    """Top-level names of ``source`` outside ``functions`` that reference any of them."""
    wanted = set(functions)
    path = SOURCE_ROOT.joinpath(*source.split(".")[1:])
    file = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    callers: set[str] = set()
    for node in ast.parse(file.read_text(encoding="utf-8")).body:
        if isinstance(node, _FUNCTIONS):
            if node.name not in wanted and wanted & _referenced_names(node):
                callers.add(node.name)
        elif wanted & _referenced_names(node):
            callers.add("<module>")
    return frozenset(callers)
