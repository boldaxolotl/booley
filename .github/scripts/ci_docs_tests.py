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
from pathlib import Path, PurePosixPath

from ci_changes import DOCUMENTATION_CATEGORIES, classify

# Content tests the derivation must always find. Losing one means a rename or a
# rule change silently removed a named guard, so the script fails loudly.
REQUIRED_CONTENT_TESTS = (
    "tests/test_readme_contract.py",
    "tests/test_site_contract.py",
    "tests/docs/test_links.py",
    "tests/docs/test_current_version_docs.py",
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
    candidate = candidate.removeprefix("./").strip("/")
    return candidate


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


def _path_candidates(tree: ast.Module) -> set[str]:
    """Return every string literal and every joined `/` chain in a module."""
    bindings: dict[str, ast.expr] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and isinstance(
            statement.value, (ast.BinOp, ast.Constant)
        ):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = statement.value
    candidates: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            candidates.add(_normalise(node.value))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            pieces = _pieces(node, bindings, frozenset())
            if pieces:
                candidates.add(_normalise("/".join(piece.strip("/") for piece in pieces)))
    return candidates


def _module_name(path: str) -> str:
    """Return the dotted import name of a repository Python file."""
    parts = list(PurePosixPath(path.removeprefix(_SOURCE_PREFIX)).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: str, tree: ast.Module, modules: dict[str, str]) -> set[str]:
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

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
                add(f"{sibling_root}.{alias.name}")
        elif isinstance(node, ast.ImportFrom):
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


def derive(repo: Path) -> list[str]:
    """Return the sorted test modules a documentation-only change must run."""
    files = _tracked_files(repo)
    names = narrow_names(files)
    documents = documentation_files(files)
    python_files = [
        path
        for path in files
        if path.endswith(".py") and path.startswith((_SOURCE_PREFIX + "booley/", _TESTS_PREFIX))
    ]
    trees: dict[str, ast.Module] = {}
    for path in python_files:
        try:
            trees[path] = ast.parse((repo / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue  # not importable, so it can neither read nor run tests
    modules = {_module_name(path): path for path in trees}

    direct_readers: set[str] = set()
    for path, tree in trees.items():
        if path in _SYNTHETIC_CONFTESTS:
            continue
        candidates = _path_candidates(tree)
        if path.startswith(_SOURCE_PREFIX):
            # Source modules must name a documentation file and be anchored to
            # the repository through __file__; a bare word such as the "docs"
            # feedback bucket or CLI subcommand is not a read.
            anchored = any(
                isinstance(node, ast.Name) and node.id == "__file__" for node in ast.walk(tree)
            )
            if anchored and candidates & documents:
                direct_readers.add(path)
        elif candidates & names:
            direct_readers.add(path)

    # Readership propagates through tests/ imports only; src -> src imports are
    # not followed because they pull most of the runtime suite into this job.
    readers = set(direct_readers)
    imports = {path: _imports(path, trees[path], modules) for path in trees}
    changed = True
    while changed:
        changed = False
        for path in trees:
            if (
                path.startswith(_TESTS_PREFIX)
                and path not in readers
                and PurePosixPath(path).name != "conftest.py"
                and imports[path] & readers
            ):
                readers.add(path)
                changed = True
    reader_conftests = {
        str(PurePosixPath(path).parent)
        for path in readers
        if path.startswith(_TESTS_PREFIX) and PurePosixPath(path).name == "conftest.py"
    }

    selected = {
        path
        for path in trees
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
