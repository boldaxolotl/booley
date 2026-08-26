"""Tests for CI JUnit execution and skip assertions."""

from __future__ import annotations

import importlib.util
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
