"""The ``sandbox_only`` marker: Sandbox-only tests skip on Windows and run everywhere else."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[1]
# A Sandbox-only module that stays cheap (six tests) and never reaches a host command.
_MARKED_MODULE = "tests/goals/test_waiver_policy.py"
# A neighbouring module that covers host code and must stay unmarked.
_HOST_MODULE = "tests/goals/test_store.py"
# Sized from the suite's 60 s per-test timeout, which Windows CI limits to 80%.
_PYTEST_TIMEOUT_S = 40


def _pytest(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a nested pytest in this checkout with the outer run's options removed."""
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "nested"),
            *arguments,
        ],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_PYTEST_TIMEOUT_S,
        check=False,
    )


def test_registry_marks_only_sandbox_only_modules(tmp_path: Path) -> None:
    result = _pytest(
        tmp_path,
        "--collect-only",
        "-q",
        "-m",
        "sandbox_only",
        _MARKED_MODULE,
        _HOST_MODULE,
    )

    modules = {line.partition("::")[0] for line in result.stdout.splitlines() if "::" in line}
    assert modules == {_MARKED_MODULE}, f"sandbox_only selected {sorted(modules)}"


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
