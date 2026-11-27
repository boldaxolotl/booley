"""The registry markers: each selects its modules; Sandbox-only tests skip on Windows."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.subprocess_budget import run_nested_pytest

_ROOT = Path(__file__).parents[1]
# A Sandbox-only module that stays cheap (the nested run must finish well inside the 30 s
# subprocess budget even under the broad suite's coverage) and never reaches a host command.
_MARKED_MODULE = "tests/mcp_tools/test_report_encoding.py"
# A neighbouring module that covers host code and is in neither registry.
_HOST_MODULE = "tests/harness/test_cheatsheet.py"


def _pytest(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a nested pytest in this checkout with the outer run's options removed."""
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")
    }
    return run_nested_pytest(
        ["-p", "no:cacheprovider", "--basetemp", str(tmp_path / "nested"), *arguments],
        cwd=_ROOT,
        env=environment,
    )


@pytest.mark.parametrize(
    ("marker", "marked"),
    [
        pytest.param("sandbox_only", _MARKED_MODULE, id="sandbox_only"),
        pytest.param("os_specific", "tests/runtime/test_pipe.py", id="os_specific"),
    ],
)
def test_registry_marks_only_registered_modules(tmp_path: Path, marker: str, marked: str) -> None:
    result = _pytest(tmp_path, "--collect-only", "-q", "-m", marker, marked, _HOST_MODULE)

    modules = {line.partition("::")[0] for line in result.stdout.splitlines() if "::" in line}
    assert modules == {marked}, f"{marker} selected {sorted(modules)}"


def test_sandbox_only_tests_skip_only_on_windows(tmp_path: Path) -> None:
    from tests.sandbox_only import SKIP_REASON

    result = _pytest(tmp_path, "-q", "-rs", _MARKED_MODULE)

    summary = result.stdout.strip().splitlines()[-1]
    counts = {word: int(number) for number, word in re.findall(r"(\d+) (\w+)", summary)}
    if sys.platform == "win32":
        assert counts.get("skipped", 0) > 0 and "passed" not in counts, result.stdout
        assert SKIP_REASON in result.stdout
    else:
        assert counts.get("passed", 0) > 0 and "skipped" not in counts, result.stdout
