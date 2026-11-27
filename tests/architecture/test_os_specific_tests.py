"""Platform-branching code has an OS-specific test or an exemption; see ``platform_branches``."""

from __future__ import annotations

import ast

import pytest


def test_platform_dependent_code_has_os_specific_tests() -> None:
    from tests.architecture import platform_branches as guard
    from tests.architecture.sandbox_reach import load_source_graph, module_test_coverage
    from tests.os_specific import is_os_specific

    problems = guard.guard_problems(
        guard.test_module_branches(),
        guard.source_module_branches(),
        module_test_coverage(load_source_graph().modules),
        is_os_specific,
        guard.PLATFORM_EXEMPTIONS,
    )

    assert not problems, (
        "Register the test module in tests/os_specific.py, or add an exemption with a reason "
        "to PLATFORM_EXEMPTIONS in tests/architecture/platform_branches.py:\n"
        + "\n".join(problems)
    )


@pytest.mark.parametrize(
    ("source", "evidence"),
    [
        ('import sys\nif sys.platform == "win32":\n    pass\n', "sys.platform (line 2)"),
        ('import os\nWINDOWS = os.name == "nt"\n', "os.name (line 2)"),
        ("import fcntl\n", "import fcntl (line 1)"),
        ("from fcntl import flock\n", "import fcntl (line 1)"),
        ("from booley.runtime.platform_paths import IS_WINDOWS\n", "IS_WINDOWS (line 1)"),
        ('import os\nfrom pathlib import Path\nPath(os.sep, "sys.platform")\n', None),
    ],
    ids=["sys-platform", "os-name", "import", "from-import", "is-windows", "no-branch"],
)
def test_platform_evidence(source: str, evidence: str | None) -> None:
    from tests.architecture.platform_branches import platform_evidence

    assert platform_evidence(ast.parse(source)) == evidence


def test_guard_reports_unregistered_uncovered_and_stale_entries() -> None:
    from tests.architecture.platform_branches import guard_problems

    problems = guard_problems(
        tests={"tests/test_registered.py": "os.name (line 1)", "tests/test_new.py": "E"},
        sources={"pkg.covered": "E", "pkg.uncovered": "E", "pkg.untested": "E", "pkg.exempt": "E"},
        coverage={
            "pkg.covered": frozenset({"tests/test_registered.py"}),
            "pkg.uncovered": frozenset({"tests/test_new.py"}),
        },
        is_os_specific=lambda path: path == "tests/test_registered.py",
        exemptions={"pkg.exempt": " ", "pkg.gone": "Sandbox only"},
    )

    assert problems == [
        "exemption pkg.exempt has no reason",
        "no OS-specific test covers pkg.uncovered: E; covered by tests/test_new.py",
        "no OS-specific test covers pkg.untested: E; covered by no test",
        "stale exemption pkg.gone: it no longer branches on the platform",
        "unregistered test module tests/test_new.py: E",
    ]


def test_registry_is_the_only_os_specific_marker_source() -> None:
    from tests.architecture.platform_branches import marker_writers
    from tests.architecture.sandbox_reach import REPOSITORY_ROOT
    from tests.os_specific import OS_SPECIFIC_TESTS

    missing = [path for path in OS_SPECIFIC_TESTS if not (REPOSITORY_ROOT / path).is_file()]

    assert not missing, f"registry names paths that do not exist: {missing}"
    assert marker_writers("os_specific") == ["tests/os_specific.py"]
