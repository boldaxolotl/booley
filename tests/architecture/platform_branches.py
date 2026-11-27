"""Find modules that branch on the platform and check their OS-specific tests.

A module branches on the platform when it reads an attribute in ``PLATFORM_ATTRIBUTES``,
imports a package in ``PLATFORM_MODULES``, or imports ``IS_WINDOWS`` from
``booley.runtime.platform_paths``. The guard requires every such ``test_*.py`` module under
``tests/`` to be registered in ``tests/os_specific.py``, and every such module under
``src/booley/`` to be covered by a registered test or listed in ``PLATFORM_EXEMPTIONS`` with a
reason. Coverage is mapped as
for Sandbox-only tests (``sandbox_reach.module_test_coverage``).

Blind spots:

* Detection is syntactic and names a fixed list. Feature probes (``hasattr(os, "getuid")``,
  ``getattr(signal, "SIGKILL", None)``), an aliased or member-imported ``sys``, ``os``, or
  ``ctypes`` name (``from sys import platform``), any other platform flag, and
  ``IS_WINDOWS`` imported any other way pass unnoticed.
* A test that fakes the platform (``monkeypatch.setattr(sys, "platform", ...)``) is not
  detected; register it by its subject.
* Coverage is per module: one registered test of a module satisfies every branch in it, and
  a registered test counts even when it is Sandbox-only and Windows skips it.
* A test module's own code is all that is checked; a branch or skip marker it takes from a
  support module, ``conftest.py``, or another test module is not attributed to it.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping

from tests.architecture.import_graph import SourceModule, analyze_source_tree
from tests.architecture.sandbox_reach import REPOSITORY_ROOT, SOURCE_ROOT, TESTS_ROOT

# Packages that exist on one OS family only, and ``platform``, whose job is inspecting the OS.
PLATFORM_MODULES = frozenset(
    {
        "_winapi",
        "fcntl",
        "grp",
        "msvcrt",
        "platform",
        "pty",
        "pwd",
        "resource",
        "termios",
        "tty",
        "winreg",
    }
)
# Attributes code reads to tell which OS it runs on, or to call a Windows-only API.
PLATFORM_ATTRIBUTES = frozenset(
    {"sys.platform", "os.name", "sys.getwindowsversion", "ctypes.windll", "ctypes.WinDLL"}
)
# Booley's shared platform flag: importing it is how a module branches through it.
_FLAG_MODULE = "booley.runtime.platform_paths"
_FLAG = "IS_WINDOWS"

# Production modules that branch on the platform and need no OS-specific test, with why.
PLATFORM_EXEMPTIONS: Mapping[str, str] = {
    "booley.data.docker.base_package_inventory": (
        "Runs only in Linux images (the Dockerfile.base build and the image contract probe); "
        "platform reads the Python version, not the OS."
    ),
    "booley.flows.synth.pdk": (
        "Picks the default PDK directory, which only synthesis reads; synthesis runs in the "
        "Sandbox (Linux); the host imports this only for endpoint help and cheat --metrics, "
        "which do not read it."
    ),
    "booley.runtime.loopback_http": (
        "Not host reachable: only the MCP server and the Dashboard web server, which run in "
        "the Sandbox (Linux), use it."
    ),
}


def _node_check(node: ast.AST) -> str | None:
    """Name the platform check one AST node performs, if any."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        attribute = f"{node.value.id}.{node.attr}"
        return attribute if attribute in PLATFORM_ATTRIBUTES else None
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        if node.module == _FLAG_MODULE and any(alias.name == _FLAG for alias in node.names):
            return _FLAG
        names = [node.module]
    else:
        return None
    packages = (name.partition(".")[0] for name in names)
    return next((f"import {name}" for name in packages if name in PLATFORM_MODULES), None)


def platform_evidence(tree: ast.Module) -> str | None:
    """Return ``"<check> (line N)"`` for a module that branches on the platform."""
    for node in ast.walk(tree):
        check = _node_check(node)
        if check is not None and isinstance(node, ast.expr | ast.stmt):
            return f"{check} (line {node.lineno})"
    return None


def test_module_branches() -> dict[str, str]:
    """Map each test module that branches on the platform to its evidence."""
    branching: dict[str, str] = {}
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        evidence = platform_evidence(ast.parse(path.read_text(encoding="utf-8")))
        if evidence is not None:
            branching[path.relative_to(REPOSITORY_ROOT).as_posix()] = evidence
    return branching


def _platform_scan(source: SourceModule, tree: ast.Module) -> list[str]:
    evidence = platform_evidence(tree)
    return [] if evidence is None else [f"{source.name}|{evidence}"]


def source_module_branches() -> dict[str, str]:
    """Map each production module that branches on the platform to its evidence."""
    analysis = analyze_source_tree(SOURCE_ROOT, {"platform": _platform_scan})
    found = (finding.partition("|") for finding in analysis.findings("platform"))
    return {module: evidence for module, _, evidence in found}


def guard_problems(
    tests: Mapping[str, str],
    sources: Mapping[str, str],
    coverage: Mapping[str, frozenset[str]],
    is_os_specific: Callable[[str], bool],
    exemptions: Mapping[str, str],
) -> list[str]:
    """Return one message per unregistered, uncovered, stale, or unreasoned entry.

    ``tests`` and ``sources`` map each branching test path and production module to its
    evidence; ``coverage`` maps a production module to the test modules that cover it.
    """
    problems = [
        f"unregistered test module {path}: {evidence}"
        for path, evidence in tests.items()
        if not is_os_specific(path)
    ]
    for module, evidence in sources.items():
        covering = sorted(coverage.get(module, ()))
        if module in exemptions or any(is_os_specific(test) for test in covering):
            continue
        covered_by = ", ".join(covering) or "no test"
        problems.append(
            f"no OS-specific test covers {module}: {evidence}; covered by {covered_by}"
        )
    for module, reason in exemptions.items():
        if module not in sources:
            problems.append(f"stale exemption {module}: it no longer branches on the platform")
        if not reason.strip():
            problems.append(f"exemption {module} has no reason")
    return sorted(problems)


def marker_writers(marker: str) -> list[str]:
    """Return the test files that write ``pytest.mark.<marker>`` or ``mark.<marker>``."""
    return sorted(
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in TESTS_ROOT.rglob("*.py")
        if any(
            isinstance(node, ast.Attribute)
            and node.attr == marker
            and isinstance(node.value, ast.Attribute | ast.Name)
            and getattr(node.value, "attr", getattr(node.value, "id", "")) == "mark"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )
