"""Narrow architecture guards for shared low-level runtime mechanisms."""

from __future__ import annotations

import ast
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "booley"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_platform_lock_apis_stay_in_runtime_lock_module_or_fifo_domain() -> None:
    allowed = {
        _SOURCE_ROOT / "runtime" / "file_lock.py",
        # FIFO flag manipulation is not a file-lock policy.
        _SOURCE_ROOT / "sim" / "bwave_fifo.py",
    }
    offenders = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        if path in allowed:
            continue
        if _imports(path) & {"fcntl", "msvcrt"}:
            offenders.append(path.relative_to(_SOURCE_ROOT).as_posix())

    assert offenders == []


def test_owned_child_termination_never_rediscovers_group_from_child_pid() -> None:
    offenders = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "getpgid(proc.pid)" in source or "getpgid(process.pid)" in source:
            offenders.append(path.relative_to(_SOURCE_ROOT).as_posix())

    assert offenders == []
