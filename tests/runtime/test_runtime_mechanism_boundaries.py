"""Narrow architecture guards for shared low-level runtime mechanisms."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "booley"


def _sources() -> Iterator[tuple[Path, ast.Module]]:
    for path in _SOURCE_ROOT.rglob("*.py"):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _relative(path: Path, lineno: int) -> str:
    return f"{path.relative_to(_SOURCE_ROOT).as_posix()}:{lineno}"


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return None


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
        _SOURCE_ROOT / "flows" / "sim" / "bwave_fifo.py",
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


def test_pid_liveness_primitives_stay_in_runtime_pid_module() -> None:
    allowed = _SOURCE_ROOT / "runtime" / "pid.py"
    offenders = []
    for path, tree in _sources():
        if path == allowed:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _qualified_name(node.func)
            is_pid_probe = (
                name == "os.kill"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == 0
            )
            if is_pid_probe or (name and name.endswith(".OpenProcess")):
                offenders.append(_relative(path, node.lineno))

    assert offenders == []


def test_pid_liveness_consumers_import_the_runtime_module() -> None:
    compatibility_module = _SOURCE_ROOT / "ticket_board" / "helpers.py"
    offenders = []
    for path, tree in _sources():
        if path == compatibility_module:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if any(alias.name == "is_pid_alive" for alias in node.names) and (
                node.module == "booley.ticket_board.helpers"
                or (node.level == 1 and node.module == "helpers")
            ):
                offenders.append(_relative(path, node.lineno))

    assert offenders == []


def test_owned_process_group_primitives_stay_in_runtime_module() -> None:
    allowed_calls = {
        _SOURCE_ROOT / "runtime" / "process_group.py",
        # A Specialist terminates its own inherited group, not an owned child.
        _SOURCE_ROOT / "specialists" / "specialist.py",
    }
    allowed_detached_spawns = allowed_calls | {
        _SOURCE_ROOT / "harness" / "auto_doctor.py",
        _SOURCE_ROOT / "harness" / "console" / "links.py",
        _SOURCE_ROOT / "runtime" / "incontainer_register.py",
    }
    offenders = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _qualified_name(node.func)
                if path not in allowed_calls and name in {"os.getpgid", "os.killpg"}:
                    offenders.append(_relative(path, node.lineno))
                if path not in allowed_detached_spawns and any(
                    keyword.arg == "start_new_session" for keyword in node.keywords
                ):
                    offenders.append(_relative(path, node.lineno))
            elif isinstance(node, ast.Attribute):
                if (
                    path not in allowed_detached_spawns
                    and _qualified_name(node) == "subprocess.CREATE_NEW_PROCESS_GROUP"
                ):
                    offenders.append(_relative(path, node.lineno))
            elif isinstance(node, ast.Constant):
                if path not in allowed_calls and node.value == "taskkill":
                    offenders.append(_relative(path, node.lineno))

    assert offenders == []


def test_ticket_workspace_consumers_do_not_bypass_the_runtime_boundary() -> None:
    consumers = (
        _SOURCE_ROOT / "harness" / "developer.py",
        _SOURCE_ROOT / "ticket_board" / "operations.py",
        _SOURCE_ROOT / "ticket_board" / "archive.py",
    )
    bypass_names = {
        "ticket_repositories",
        "merge_project_ticket_branch",
        "cleanup_project_ticket_branch",
    }
    offenders = []
    for path in consumers:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported = bypass_names.intersection(alias.name for alias in node.names)
            if imported:
                offenders.append(f"{_relative(path, node.lineno)}: {', '.join(sorted(imported))}")

    assert offenders == []
