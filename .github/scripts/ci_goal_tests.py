"""Own the CI side of keeping Goal Mode unit tests out of the CI test lanes.

Temporary, like the ``collect_ignore`` entry in ``tests/conftest.py``: delete this
module with Goal Mode (#3276). Three CI scripts share it (the change classifier, the
documentation-test derivation and flake-stress), so it imports none of them.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path, PurePosixPath

GOAL_TESTS_PREFIX = "tests/goals/"
_GOAL_PACKAGE = "tests.goals"
_TESTS_PREFIX = "tests/"
_FIXTURES_PREFIX = "tests/fixtures/"


def goal_module_name(path: str) -> str:
    """Return the dotted module name of a ``tests/goals/`` Python path."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_shared_goal_path(path: str, shared_modules: frozenset[str]) -> bool:
    """Return whether a ``tests/goals/`` path is a Python module a test outside imports."""
    return path.endswith(".py") and goal_module_name(path) in shared_modules


def _tracked_python_tests(repo: Path) -> list[str]:
    """Return Git's tracked ``tests/**/*.py`` paths, fixtures excluded."""
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--", "tests"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.decode(errors="replace").strip() or "git ls-files failed")
    paths = (name.decode("utf-8", errors="surrogateescape") for name in result.stdout.split(b"\0"))
    return [
        path
        for path in paths
        if path.endswith(".py")
        and path.startswith(_TESTS_PREFIX)
        and not path.startswith(_FIXTURES_PREFIX)
    ]


def _imported_goal_modules(repo: Path, path: str) -> set[str]:
    """Return every ``tests.goals`` module name (and package prefix) a file imports."""
    try:
        tree = ast.parse((repo / path).read_bytes(), filename=path)
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    package = PurePosixPath(path).parent.parts
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _from_import_base(node, package)
            if base:
                names.add(base)
                names.update(f"{base}.{alias.name}" for alias in node.names)
    goal_names: set[str] = set()
    for name in names:
        if name == _GOAL_PACKAGE or name.startswith(f"{_GOAL_PACKAGE}."):
            parts = name.split(".")
            goal_names.update(".".join(parts[:end]) for end in range(2, len(parts) + 1))
    return goal_names


def _from_import_base(node: ast.ImportFrom, package: tuple[str, ...]) -> str:
    """Return the absolute dotted module a ``from`` import names ('' when unresolvable)."""
    if node.level == 0:
        return node.module or ""
    anchor = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join((*anchor, *((node.module,) if node.module else ())))


def shared_goal_modules(repo: Path) -> frozenset[str]:
    """Return the ``tests.goals`` module names that tests outside ``tests/goals/`` depend on.

    Works on names, not files: a deleted or renamed helper that an outside test still
    imports stays shared. The set is closed over the imports of each shared module that
    still exists, so helpers of helpers count too.
    """
    tracked = _tracked_python_tests(repo)
    goal_files = {
        goal_module_name(path): path for path in tracked if path.startswith(GOAL_TESTS_PREFIX)
    }
    shared: set[str] = set()
    for path in tracked:
        if not path.startswith(GOAL_TESTS_PREFIX):
            shared |= _imported_goal_modules(repo, path)
    pending = [name for name in shared if name in goal_files]
    while pending:  # terminates: `shared` only grows within the finite set of goal modules
        for name in _imported_goal_modules(repo, goal_files[pending.pop()]) - shared:
            shared.add(name)
            if name in goal_files:
                pending.append(name)
    return frozenset(shared)
