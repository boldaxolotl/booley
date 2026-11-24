"""Architecture guard: Simulation Record names say "record", not "campaign".

The Simulation Record package and every other source, test and QA module name the concept
with Record names. A name that still says campaign names another concept and is listed
below with its reason; it is allowed only in the file that is listed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.source_checkout_support import source_checkout_files

_ROOT = Path(__file__).resolve().parents[2]
_RECORD_PACKAGE = _ROOT / "src" / "booley" / "flows" / "sim" / "record"
_FORBIDDEN = "campaign"


class _DefinedNames(ast.NodeVisitor):
    """Collect (line, scope, name) for every identifier a module defines.

    *scope* is the dotted name of the enclosing classes and functions (the function or
    class itself for its own name and parameters), or <module> at the top level.
    """

    def __init__(self) -> None:
        self.found: list[tuple[int, str, str]] = []
        self._stack: list[str] = []

    def add(self, line: int, name: str) -> None:
        self.found.append((line, ".".join(self._stack) or "<module>", name))

    def add_arguments(self, arguments: ast.arguments) -> None:
        every_argument = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        if arguments.vararg is not None:
            every_argument.append(arguments.vararg)
        if arguments.kwarg is not None:
            every_argument.append(arguments.kwarg)
        for argument in every_argument:
            self.add(argument.lineno, argument.arg)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(node.name)
        self.add(node.lineno, node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self.add(node.lineno, node.name)
        self.add_arguments(node.args)
        self.generic_visit(node)
        self._stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self.add(node.lineno, node.name)
        self.add_arguments(node.args)
        self.generic_visit(node)
        self._stack.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.add_arguments(node.args)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.add(node.lineno, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Store):
            self.add(node.lineno, node.attr)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.add(node.lineno, node.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.add(node.lineno, alias.asname)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.asname:
                self.add(node.lineno, alias.asname)

    def add_exports(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        if any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in targets
        ) and isinstance(value, (ast.List, ast.Tuple)):
            for element in value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    self.add(element.lineno, element.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.add_exports(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.add_exports([node.target], node.value)
        self.generic_visit(node)


def _defined_names(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Return (line, scope, name) for every identifier a module defines."""
    collector = _DefinedNames()
    collector.visit(tree)
    return collector.found


# Files whose "campaign" names another concept than the Simulation Record, with the reason.
# A campaign name may appear in a listed file unless it also says record or simulation: that
# is the Simulation Record concept under its old name.
_RECORD_WORDS = re.compile(r"record|simulation", re.IGNORECASE)
_OTHER_CONCEPT_FILES: dict[str, str] = {
    "src/booley/agent_workspace/isolation.py": "Workspace isolation roots of a Mutation campaign",
    "src/booley/flows/mutation/flow.py": "Mutation Flow campaign",
    "src/booley/flows/mutation/memory_claims.py": "memory claims of a Mutation campaign",
    "src/booley/flows/target_campaign.py": "Target campaign",
    "src/booley/flows/source_fingerprint.py": "source-fingerprint campaign category (#3045)",
    "tests/flows/mutation/test_flow.py": "tests of the Mutation Flow campaign",
    "tests/flows/mutation/test_memory_claims.py": "tests of Mutation campaign memory claims",
    "tests/flows/test_target_campaign.py": "tests of the Target campaign",
    "tests/flows/test_source_fingerprint_campaign.py": "tests of the source-fingerprint campaign",
    "tests/runtime/test_job_observation.py": "a CLI Target campaign waiting for an item claim",
}


def test_simulation_record_package_defines_only_record_names() -> None:
    sources = sorted(_RECORD_PACKAGE.glob("*.py"))
    assert sources, f"no modules found under {_RECORD_PACKAGE}"
    offenders = [
        f"{source.name}:{line} {name}"
        for source in sources
        for line, _scope, name in _defined_names(ast.parse(source.read_bytes()))
        if _FORBIDDEN in name.lower()
    ]
    assert not offenders, "Simulation Record package defines campaign names:\n" + "\n".join(
        offenders
    )


def test_simulation_record_names_outside_the_package_use_record_names() -> None:
    used: set[str] = set()
    offenders: list[str] = []
    for path in source_checkout_files(_ROOT, "src", "tests", "qa"):
        if path.suffix != ".py":
            continue
        relative = path.relative_to(_ROOT).as_posix()
        for line, scope, name in _defined_names(ast.parse(path.read_bytes())):
            if _FORBIDDEN not in name.lower():
                continue
            if relative in _OTHER_CONCEPT_FILES and not _RECORD_WORDS.search(name):
                used.add(relative)
            else:
                offenders.append(f"{relative}:{line} {scope}:{name}")
    assert not offenders, (
        "names that say campaign for the Simulation Record; rename them to Record names, or "
        "list the file of another concept with its reason:\n" + "\n".join(offenders)
    )
    stale = sorted(set(_OTHER_CONCEPT_FILES) - used)
    assert not stale, "listed files that define no campaign name any more:\n" + "\n".join(stale)
