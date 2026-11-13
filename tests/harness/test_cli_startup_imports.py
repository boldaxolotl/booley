"""`booley --help` / `--version` must not import subcommand handler modules (issue #3654).

Every `booley` call used to import Doctor, init, auth, bootstrap and the FuseSoC stack just to
fill the dispatch table, costing about half a second per call. The rule these tests enforce: a
module that only a dispatch handler needs is imported inside that handler; top-level imports of
`booley.harness.booley` serve parser build, CLI normalization and dispatch. No test here asserts
wall-clock time; they assert module sets and delegation only.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType

from booley.harness import booley as cli
from tests.architecture.sandbox_reach import load_source_graph, reachable_harness_functions
from tests.subprocess_budget import run_bounded_child

# Heavy modules the issue names: none may load for `--help` or `--version`.
FORBIDDEN_AT_STARTUP = (
    "booley.harness.doctor",
    "booley.audit.design_size",
    "booley.fusesoc.fusesoc_registry",
    "fusesoc",
    "pyparsing",
    "pydantic",
    "booley.harness.bootstrap_cli",
    "booley.harness.init_cmd",
    "booley.harness.auth_cmd",
    "booley.mcp.registry",
)

# Child program: run the real entry point and dump `[exit code, loaded modules]` to a file.
# Output goes to a file, not stdout: `main()` reads `sys.stdout.encoding`, so stdout must stay
# a real stream.
_PROBE = """
import json, sys
out_file = sys.argv[1]
sys.argv = ["booley", *sys.argv[2:]]
from booley.harness.booley import main
try:
    code = main()
except SystemExit as exc:
    code = exc.code
with open(out_file, "w", encoding="utf-8") as handle:
    json.dump([code, sorted(sys.modules)], handle)
"""


def _probe(argv: list[str], tmp_path: Path) -> tuple[int, str, set[str]]:
    """Run `booley <argv>` in a fresh interpreter; return exit code, stdout, loaded modules."""
    out_file = tmp_path / "probe.json"
    result = run_bounded_child(
        [sys.executable, "-c", _PROBE, str(out_file), *argv],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), "HOME": str(tmp_path)},
        cwd=tmp_path,
    )
    code, modules = json.loads(out_file.read_text(encoding="utf-8"))
    return code, result.stdout, set(modules)


def test_help_and_version_skip_handler_modules(tmp_path):
    # Bug caught: the entry module importing every handler (Doctor, FuseSoC, pydantic, ...)
    # at load time, so informational calls pay for commands they never run.
    for argv in (["--help"], ["--version"], ["doctor", "--help"]):
        code, stdout, modules = _probe(argv, tmp_path)

        assert code == 0
        assert stdout.strip()
        loaded = [name for name in FORBIDDEN_AT_STARTUP if name in modules]
        assert not loaded, f"`booley {' '.join(argv)}` imported handler-only modules: {loaded}"


# --- AC2: generic, fail-closed check over the dispatch table ---------------------------


def _booley_tree() -> ast.Module:
    return ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))


def _is_table_assignment(node: ast.stmt) -> bool:
    """True for `_EARLY_COMMANDS: ... = {...}` and `_EARLY_COMMANDS["name"] = handler`."""
    if isinstance(node, ast.AnnAssign):
        targets: list[ast.expr] = [node.target]
    elif isinstance(node, ast.Assign):
        targets = node.targets
    else:
        return False
    for target in targets:
        root = target.value if isinstance(target, ast.Subscript) else target
        if isinstance(root, ast.Name) and root.id == "_EARLY_COMMANDS":
            return True
    return False


def _table_values(tree: ast.Module) -> dict[str, ast.expr]:
    """Map each dispatch-table command to its handler expression."""
    values: dict[str, ast.expr] = {}
    for node in tree.body:
        if not _is_table_assignment(node):
            continue
        if isinstance(node, ast.AnnAssign):
            assert isinstance(node.value, ast.Dict), "_EARLY_COMMANDS must stay a dict literal"
            for key, value in zip(node.value.keys, node.value.values, strict=True):
                assert isinstance(key, ast.Constant), "_EARLY_COMMANDS keys must be literals"
                values[str(key.value)] = value
        else:
            assert isinstance(node, ast.Assign)
            (target,) = node.targets
            assert isinstance(target, ast.Subscript)
            assert isinstance(target.slice, ast.Constant), "_EARLY_COMMANDS keys must be literals"
            values[str(target.slice.value)] = node.value
    assert values, "found no _EARLY_COMMANDS entries"
    return values


def _names(nodes: list[ast.AST]) -> set[str]:
    """Plain names (not attribute names) that *nodes* read."""
    return {child.id for node in nodes for child in ast.walk(node) if isinstance(child, ast.Name)}


def _resolve_import_from(node: ast.ImportFrom, modules: frozenset[str]) -> dict[str, str]:
    """Map each name an `import from` binds to the Booley module it loads."""
    assert node.module is not None
    bound: dict[str, str] = {}
    for alias in node.names:
        submodule = f"{node.module}.{alias.name}"
        bound[alias.asname or alias.name] = submodule if submodule in modules else node.module
    return bound


def _top_level_booley_imports(tree: ast.Module, modules: frozenset[str]) -> dict[str, set[str]]:
    """Map each Booley module imported at top level (outside TYPE_CHECKING) to its bound names."""
    imports: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("booley"):
            for name, module in _resolve_import_from(node, modules).items():
                imports.setdefault(module, set()).add(name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("booley"):
                    imports.setdefault(alias.name, set()).add(alias.asname or alias.name)
    return imports


def _handler_only_top_level_imports(tree: ast.Module, modules: frozenset[str]) -> list[str]:
    """Rule 1: top-level Booley imports that only dispatch handlers (or the table) use."""
    handlers = {value.id for value in _table_values(tree).values() if isinstance(value, ast.Name)}
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    handler_functions = frozenset(handlers & functions)
    startup_functions = reachable_harness_functions(tree, handler_functions)
    startup_code: list[ast.AST] = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in startup_functions
    ]
    startup_code += [
        node
        for node in tree.body
        if not isinstance(node, ast.FunctionDef | ast.Import | ast.ImportFrom)
        and not _is_table_assignment(node)
    ]
    startup_names = _names(startup_code)
    # A startup function may import the same module locally (the parser build does for
    # `booley.eda.cli`); that module loads at startup whatever the top-level binding says.
    startup_local_modules = {
        module
        for node in startup_code
        for child in ast.walk(node)
        if isinstance(child, ast.ImportFrom) and (child.module or "").startswith("booley")
        for module in _resolve_import_from(child, modules).values()
    }

    return [
        f"{module} (bound as {sorted(names)})"
        for module, names in sorted(_top_level_booley_imports(tree, modules).items())
        if not names & startup_names and module not in startup_local_modules
    ]


def _delegate_modules(tree: ast.Module, modules: frozenset[str]) -> dict[str, set[str]]:
    """Map each table handler defined in `booley.py` to the modules it delegates to.

    A delegate module is one the handler imports locally and whose imported name the handler
    returns a call of: the module that implements the command. Fails on a table value it
    cannot classify, so a new shape of entry is never silently skipped.
    """
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    top_level_bound = {
        name for names in _top_level_booley_imports(tree, modules).values() for name in names
    }
    delegates: dict[str, set[str]] = {}
    for command, value in _table_values(tree).items():
        if isinstance(value, ast.Name) and value.id in functions:
            function = functions[value.id]
            local: dict[str, str] = {}
            for node in ast.walk(function):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("booley"):
                    local.update(_resolve_import_from(node, modules))
            called = {
                call.func.id
                for node in ast.walk(function)
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Call)
                for call in [node.value]
                if isinstance(call.func, ast.Name)
            }
            delegates[command] = {local[name] for name in called if name in local}
            continue
        root = value
        while isinstance(root, ast.Attribute):
            root = root.value
        assert isinstance(root, ast.Name) and root.id in top_level_bound, (
            f"cannot classify the handler of `{command}`: use a function in booley.harness.booley "
            "or a name bound by a top-level Booley import"
        )
    return delegates


def test_help_loads_no_dispatch_handler_module(tmp_path):
    # Bug caught: (rule 1) a handler-only module imported at the top of the entry module, e.g.
    # an eager `run_new` table entry, a `_cmd_new` wrapper calling a top-level `_run_new`, or
    # a handler's import moved to the top; (rule 2) a handler module reaching `--help` through
    # a parser-time module. Covers any command added to the table without editing this test.
    tree = _booley_tree()
    modules = load_source_graph().modules
    handler_only = _handler_only_top_level_imports(tree, modules)
    assert not handler_only, (
        "top-level imports in booley.harness.booley used only by dispatch handlers; import "
        f"each inside the handler that uses it: {handler_only}"
    )

    delegates = _delegate_modules(tree, modules)
    handler_modules = {module for modules in delegates.values() for module in modules}
    assert handler_modules, "found no lazily imported handler module; the classifier is blind"

    _, _, loaded = _probe(["--help"], tmp_path)

    leaked = sorted(handler_modules & loaded)
    assert not leaked, f"`booley --help` imported dispatch handler modules: {leaked}"


# --- AC3: lazy entries still reach the real handler ------------------------------------


LAZY_HANDLERS = (
    ("chat", "booley.harness.chat_cmd", "run"),
    ("doctor", "booley.harness.doctor", "run_doctor"),
    ("init", "booley.harness.init_cmd", "run_init"),
    ("auth", "booley.harness.auth_cmd", "run_auth"),
)


def test_lazy_dispatch_reaches_real_handler(monkeypatch, tmp_path):
    # Bug caught: a lazy wrapper that drops an argument, returns the wrong exit code, or
    # resolves a different function than the command's real handler.
    for command, module_name, attribute in LAZY_HANDLERS:
        module: ModuleType = importlib.import_module(module_name)
        seen: list[tuple[object, object]] = []

        def recorder(args, project_root, seen=seen):
            seen.append((args, project_root))
            return 37

        monkeypatch.setattr(module, attribute, recorder)
        args = cli.argparse.Namespace(command=command, agent_args=[], session_name=None, cli=None)

        code = cli._EARLY_COMMANDS[command](args, tmp_path)

        assert code == 37, command
        assert len(seen) == 1, command
        assert seen[0][0] is args, command
        assert seen[0][1] is tmp_path, command
