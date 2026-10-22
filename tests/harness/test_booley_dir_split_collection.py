"""Harness tests that resolve a Booley Directory select one themselves."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# A root-level argument between Harness arguments makes pytest build a second
# Harness package node without ``tests/harness/conftest.py`` fixtures (#1798).
_SPLIT_PREFIX = (
    "tests/harness/test_goal_cli_surface.py::test_goal_surface_matches_catalogs",
    "tests/test_booley_directory_errors.py",
)
_BOOLEY_DIR_DEPENDENTS = (
    "tests/harness/test_goal_cli_surface.py::test_goal_abandon_refuses_without_occupying_record",
    "tests/harness/test_image_lifecycle.py::test_incremental_adapter_prepare_and_policy_helpers",
    "tests/harness/test_session_runtime.py::"
    "test_enter_preserves_container_credentials_without_literal_placeholder_overrides",
    "tests/harness/setup/test_git_hooks.py::TestProjectCommitMsgHookVendoring::"
    "test_vendors_runtime_helpers_alongside_scripts",
    "tests/harness/setup/test_git_hooks.py::TestProjectCommitMsgHookVendoring::"
    "test_installed_hook_is_lf_not_crlf",
    "tests/harness/test_endpoint_help.py::test_missing_project_error_precedes_host_venue_error",
)
# Leaves a resolved Booley Directory cached; it must run right before the
# test_endpoint_help dependent, which needs an empty cache.
_CACHE_WARMER = (
    "tests/harness/test_image_lifecycle.py::"
    "test_incremental_plan_builds_only_wheel_overlay_for_python_change"
)


def test_booley_dir_dependents_pass_without_harness_conftest(tmp_path: Path) -> None:
    environment = dict(os.environ)
    # The outer Harness fixture exports BOOLEY_DIR; the child must not inherit it.
    for name in (
        "BOOLEY_DIR",
        "BOOLEY_PYTEST_EVIDENCE_DIR",
        "BOOLEY_PYTEST_TIMING_FILE",
        "PYTEST_ADDOPTS",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        environment.pop(name, None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-v",
            "-p",
            "no:xdist",
            "-p",
            "no:cacheprovider",
            "--test-profile",
            "python",
            f"--basetemp={tmp_path / 'child'}",
            *_SPLIT_PREFIX,
            *_BOOLEY_DIR_DEPENDENTS[:-1],
            _CACHE_WARMER,
            _BOOLEY_DIR_DEPENDENTS[-1],
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    passed = [
        line
        for line in result.stdout.splitlines()
        if line.endswith(" PASSED") or " PASSED " in line
    ]
    for node in _BOOLEY_DIR_DEPENDENTS:
        assert any(line.startswith(node) for line in passed), node
