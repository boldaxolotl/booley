#!/usr/bin/env python3
"""Derive the pytest modules that guard README, website and documentation content.

The narrow `docs-check` CI job runs only these modules for documentation-only
changes, so the list is derived from the repository rather than maintained by
hand: a newly added test that reads documentation is picked up automatically.

A Python file is a *reader* when it names a documentation path or a directory
holding only documentation (string literals and `/` path joins). The selected
modules are the tests that read documentation themselves, the tests under
`tests/docs/`, the tests importing a reader module or support module, and the
tests below a `conftest.py` that itself reads documentation (a conftest that
merely imports a reader is not followed: the root one would select every test). Over-inclusion only costs time; a missed
reader would let a documentation PR merge past a failing content test.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ci_changes import DOCUMENTATION_CATEGORIES, classify

# Content tests the derivation must always find. Losing one means a rename or a
# rule change silently removed a named guard, so the script fails loudly.
REQUIRED_CONTENT_TESTS = (
    "tests/test_readme_contract.py",
    "tests/test_site_contract.py",
    "tests/docs/test_links.py",
    "tests/docs/test_current_version_docs.py",
    # Found only through documentation paths they name; pinned so that a
    # tighter reader rule cannot silently drop the docs/user guards.
    "tests/runtime/test_docs_links.py",
    "tests/test_paths.py",
)
# Conftests that write documentation into temporary projects instead of
# reading this repository's documentation, with the reason each is exempt.
_SYNTHETIC_CONFTESTS = {
    "tests/goals/conftest.py": "writes docs/spec.md into a temporary Goal project",
}
_SOURCE_PREFIX = "src/"
_TESTS_PREFIX = "tests/"


def _tracked_files(repo: Path) -> list[str]:
    """Return Git's tracked paths (NUL-delimited, so unusual names survive)."""
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=False
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.decode(errors="replace").strip() or "git ls-files failed")
    return [
        name.decode("utf-8", errors="surrogateescape")
        for name in result.stdout.split(b"\0")
        if name
    ]


def documentation_files(files: Iterable[str]) -> set[str]:
    """Return the tracked paths that are README, website or documentation content."""
    return {path for path in files if classify([path]) <= DOCUMENTATION_CATEGORIES}


def narrow_names(files: Sequence[str]) -> set[str]:
    """Return documentation files plus every directory holding only documentation."""
    documentation = documentation_files(files)
    names: set[str] = set()
    mixed_directories: set[str] = set()
    for path in files:
        parents = {str(parent) for parent in PurePosixPath(path).parents if str(parent) != "."}
        if path in documentation:
            names.add(path)
            names.update(parents)
        else:
            mixed_directories.update(parents)
    return names - mixed_directories


def _normalise(candidate: str) -> str:
    return candidate.removeprefix("./").strip("/")


def _pieces(node: ast.expr, bindings: dict[str, ast.expr], expanding: frozenset[str]) -> list[str]:
    """Return the string parts of a `/` chain, expanding module-level names once."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [
            *_pieces(node.left, bindings, expanding),
            *_pieces(node.right, bindings, expanding),
        ]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in bindings and node.id not in expanding:
        return _pieces(bindings[node.id], bindings, expanding | {node.id})
    return []


@dataclass(frozen=True)
class _ModuleFacts:
    """What the derivation needs from one parsed module, gathered in one AST walk.

    Walking each tree once matters: the scan covers every tracked Python file,
    and CI runs it under coverage tracing.
    """

    candidates: frozenset[str]  # every string literal and joined `/` chain
    anchored: bool  # names `__file__`, so it can locate repository files
    imports: tuple[ast.Import | ast.ImportFrom, ...]


def _scan_module(tree: ast.Module) -> _ModuleFacts:
    """Collect path candidates, the `__file__` anchor and imports in one walk."""
    bindings: dict[str, ast.expr] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and isinstance(
            statement.value, (ast.BinOp, ast.Constant)
        ):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = statement.value
    candidates: set[str] = set()
    anchored = False
    imports: list[ast.Import | ast.ImportFrom] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            candidates.add(_normalise(node.value))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            pieces = _pieces(node, bindings, frozenset())
            if pieces:
                candidates.add(_normalise("/".join(piece.strip("/") for piece in pieces)))
        elif isinstance(node, ast.Name):
            anchored = anchored or node.id == "__file__"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
    return _ModuleFacts(frozenset(candidates), anchored, tuple(imports))


def _module_name(path: str) -> str:
    """Return the dotted import name of a repository Python file."""
    parts = list(PurePosixPath(path.removeprefix(_SOURCE_PREFIX)).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(
    path: str, nodes: Iterable[ast.Import | ast.ImportFrom], modules: dict[str, str]
) -> set[str]:
    """Return the repository files a module imports (by dotted name or sibling)."""
    package = _module_name(path).split(".")
    if PurePosixPath(path).name != "__init__.py":
        package = package[:-1]
    sibling_root = ".".join(PurePosixPath(path).parent.parts)
    found: set[str] = set()

    def add(name: str) -> None:
        parts = name.split(".")
        for end in range(1, len(parts) + 1):
            target = modules.get(".".join(parts[:end]))
            if target:
                found.add(target)

    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
                add(f"{sibling_root}.{alias.name}")
        else:
            if node.level:
                base = ".".join(
                    [
                        *package[: len(package) - node.level + 1],
                        *([node.module] if node.module else []),
                    ]
                )
                bases = [base]
            else:
                bases = [node.module or "", f"{sibling_root}.{node.module or ''}"]
            for base in bases:
                add(base)
                for alias in node.names:
                    add(f"{base}.{alias.name}")
    return found


def _is_test_module(path: str) -> bool:
    name = PurePosixPath(path).name
    return path.startswith(_TESTS_PREFIX) and (
        (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py")
    )


def _parse_python(repo: Path, files: Iterable[str]) -> dict[str, _ModuleFacts]:
    """Parse and scan the repository's Python files; report each unparsable one."""
    modules: dict[str, _ModuleFacts] = {}
    for path in files:
        if not path.endswith(".py") or not path.startswith(
            (_SOURCE_PREFIX + "booley/", _TESTS_PREFIX)
        ):
            continue
        try:
            tree = ast.parse((repo / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as error:
            # Skipped, but visibly: a file that cannot be parsed cannot be derived.
            print(f"warning: not parsed, skipped: {path}: {error}", file=sys.stderr)
            continue
        modules[path] = _scan_module(tree)
    return modules


def _direct_readers(
    modules: dict[str, _ModuleFacts], names: set[str], documents: set[str]
) -> set[str]:
    """Return the Python files that themselves name documentation."""
    readers: set[str] = set()
    for path, facts in modules.items():
        if path in _SYNTHETIC_CONFTESTS:
            continue
        candidates = facts.candidates
        if path.startswith(_SOURCE_PREFIX):
            # Source modules must name a documentation file and be anchored to
            # the repository through __file__; a bare word such as the "docs"
            # feedback bucket or CLI subcommand is not a read.
            if facts.anchored and candidates & documents:
                readers.add(path)
        elif candidates & names:
            readers.add(path)
    return readers


def _propagate_through_tests(
    modules: dict[str, _ModuleFacts], direct_readers: set[str]
) -> set[str]:
    """Add the tests that import a reader, until no more are found.

    Readership propagates through tests/ imports only; src -> src imports are
    not followed because they pull most of the runtime suite into this job.
    """
    by_name = {_module_name(path): path for path in modules}
    imports = {path: _imports(path, facts.imports, by_name) for path, facts in modules.items()}
    readers = set(direct_readers)
    changed = True
    while changed:  # terminates: `readers` only grows within a finite set
        changed = False
        for path in modules:
            if (
                path.startswith(_TESTS_PREFIX)
                and path not in readers
                and PurePosixPath(path).name != "conftest.py"
                and imports[path] & readers
            ):
                readers.add(path)
                changed = True
    return readers


def derive(repo: Path) -> list[str]:
    """Return the sorted test modules a documentation-only change must run."""
    files = _tracked_files(repo)
    modules = _parse_python(repo, files)
    readers = _propagate_through_tests(
        modules, _direct_readers(modules, narrow_names(files), documentation_files(files))
    )
    reader_conftests = {
        str(PurePosixPath(path).parent)
        for path in readers
        if path.startswith(_TESTS_PREFIX) and PurePosixPath(path).name == "conftest.py"
    }
    selected = {
        path
        for path in modules
        if _is_test_module(path)
        and (
            path.startswith("tests/docs/")
            or path in readers
            or any(str(parent) in reader_conftests for parent in PurePosixPath(path).parents)
        )
    }
    return sorted(selected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        modules = derive(args.repo)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    missing = [module for module in REQUIRED_CONTENT_TESTS if module not in modules]
    if missing:
        print(
            f"error: required content test(s) not derived: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2
    print("\n".join(modules))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
