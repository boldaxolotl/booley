"""Tests for CI JUnit execution and skip assertions."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / ".github/scripts/assert_junit.py"
_SPEC = importlib.util.spec_from_file_location("assert_junit", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
assert_junit = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = assert_junit
_SPEC.loader.exec_module(assert_junit)


def _write_junit(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_reads_pytest_testsuites_document(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        '<testsuites><testsuite tests="8" failures="1" errors="2" skipped="3"/></testsuites>',
    )

    counts = assert_junit.read_counts(junit)

    assert counts == assert_junit.Counts(tests=8, failures=1, errors=2, skipped=3)


def test_rejects_zero_test_required_suite(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    _write_junit(junit, '<testsuite tests="0"/>')

    with pytest.raises(ValueError, match="at least 1 tests"):
        assert_junit.validate(assert_junit.read_counts(junit), min_tests=1, max_skips=None)


def test_rejects_required_suite_skip(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    _write_junit(junit, '<testsuite tests="2" skipped="1"/>')

    with pytest.raises(ValueError, match="at most 0 skips"):
        assert_junit.validate(assert_junit.read_counts(junit), min_tests=1, max_skips=0)


_GITHUB_DIR = Path(__file__).parents[2] / ".github"

# The one evidence floor allowed to exceed 1: a deliberate order-of-magnitude
# sanity floor against collection collapse of the full suite (~22.3k tests
# collected 2026-10-23), documented in the workflow itself.
_DOCUMENTED_SANITY_FLOORS = {(".github/workflows/full-python-matrix.yml", "10000")}

# Files whose assert_junit.py invocations deliberately set no skip limit
# (sharded legs and the full suite may skip legitimately).
_NO_SKIP_LIMIT_FILES = {
    ".github/actions/publish-pytest-evidence/action.yml",
    ".github/workflows/full-python-matrix.yml",
}


_SCRIPT_SUFFIXES = {".sh", ".py", ".ps1", ".bash"}


def _ci_files_using_assert_junit() -> dict[str, str]:
    """Return repo-relative path -> text for every .github file that calls assert_junit.py."""
    files: dict[str, str] = {}
    candidates = [
        *_GITHUB_DIR.rglob("*.yml"),
        *_GITHUB_DIR.rglob("*.yaml"),
        *(p for p in (_GITHUB_DIR / "scripts").rglob("*") if p.suffix in _SCRIPT_SUFFIXES),
    ]
    for path in candidates:
        if path == _SCRIPT:
            continue
        text = path.read_text(encoding="utf-8")
        if "assert_junit.py" in text:
            files[path.relative_to(_GITHUB_DIR.parent).as_posix()] = text
    return files


def test_ci_junit_floors_do_not_encode_suite_sizes() -> None:
    """Evidence floors are 1 plus --max-skips 0, never a count that drifts from the suite."""
    files = _ci_files_using_assert_junit()
    violations: list[str] = []
    ones = 0
    for rel, text in sorted(files.items()):
        for value in re.findall(r"--min-tests[=\s]+(\S+)", text):
            if value == "1":
                ones += 1
            elif (rel, value) not in _DOCUMENTED_SANITY_FLOORS:
                violations.append(f"{rel}: --min-tests {value}")
        invocations = text.count("assert_junit.py")
        skip_limited = len(re.findall(r"--max-skips[=\s]+0\b", text))
        expected = 0 if rel in _NO_SKIP_LIMIT_FILES else invocations
        if skip_limited != expected:
            violations.append(
                f"{rel}: {invocations} assert_junit.py calls but {skip_limited} "
                f"with --max-skips 0 (expected {expected})"
            )

    assert ones, "found no --min-tests 1 floor; the scan is vacuous"
    assert not violations, (
        "CI JUnit floors must not encode suite sizes; use '--min-tests 1 --max-skips 0' "
        "(or document a sanity floor in _DOCUMENTED_SANITY_FLOORS):\n" + "\n".join(violations)
    )
