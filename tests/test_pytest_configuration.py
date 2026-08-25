"""Regression tests for the repository's pytest process configuration."""

from __future__ import annotations

import ntpath
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).parents[1]


def _test_workflow() -> dict:
    workflow_path = REPOSITORY_ROOT / ".github" / "workflows" / "test.yml"
    return yaml.safe_load(workflow_path.read_text(encoding="utf-8"))


def _suite_config(pytestconfig: pytest.Config):
    config_path = Path(__file__).with_name("conftest.py")
    return next(
        plugin
        for plugin in pytestconfig.pluginmanager.get_plugins()
        if Path(getattr(plugin, "__file__", "")) == config_path
    )


def test_windows_worker_temp_shares_workspace_drive(monkeypatch, pytestconfig) -> None:
    """FuseSoC cannot relativize a temp core across Windows drive letters."""
    suite_config = _suite_config(pytestconfig)
    workspace = Path("D:/workspace")
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    monkeypatch.setattr(suite_config.sys, "platform", "win32")
    monkeypatch.setattr(suite_config.tempfile, "tempdir", "C:/system-temp")
    monkeypatch.setattr(suite_config.Path, "cwd", lambda: workspace)

    worker_temp = suite_config._xdist_worker_temp_base()

    ntpath.relpath(str(worker_temp / "project.core"), str(workspace / "build"))


def test_ci_pytest_temp_uses_runner_volume() -> None:
    """pytest's controller temp must share the Windows checkout volume."""
    workflow = _test_workflow()

    test_steps = workflow["jobs"]["test"]["steps"]
    parallel_step = next(step for step in test_steps if step.get("name") == "Run tests (parallel)")
    assert parallel_step["env"]["RUNNER_TEMP"] == "${{ runner.temp }}"
    assert "PYTEST_ADDOPTS" not in parallel_step["env"]
    assert '--basetemp "${{ runner.temp }}/pytest"' in parallel_step["run"]


def test_native_bwave_marker_selects_only_real_binary_tests() -> None:
    """The native integration job owns every test that executes B-Wave."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "native_bwave",
            "tests/bwave",
        ],
        cwd=REPOSITORY_ROOT,
        env={
            name: value
            for name, value in os.environ.items()
            if not name.startswith("PYTEST_XDIST_")
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    selected = {line for line in result.stdout.splitlines() if line.startswith("tests/")}
    assert selected == {
        "tests/bwave/test_contract.py::test_total_miss_is_exit_usage_plus_marker",
        "tests/bwave/test_contract.py::test_list_tree_stderr_carries_the_scope_line",
        "tests/bwave/test_contract.py::test_build_refuses_zero_signal_vcd",
        "tests/bwave/test_contract.py::test_empty_store_marker_survives_in_binary",
        "tests/bwave/test_contract.py::test_env_errors_stay_exit_env",
        "tests/bwave/test_sessions.py::test_query_uses_default_session",
        "tests/bwave/test_sessions.py::test_query_uses_named_alias",
        "tests/bwave/test_sessions.py::test_query_explicit_overrides_session",
        "tests/bwave/test_sessions.py::test_stale_session_warning",
        "tests/bwave/test_sessions.py::test_fresh_trace_with_old_registration_does_not_warn",
        "tests/bwave/test_sessions.py::test_register_reports_trace_identity_and_age",
    }


def test_generic_python_matrix_excludes_native_bwave() -> None:
    """Compatibility legs never install Rust or execute native B-Wave."""
    workflow = _test_workflow()
    test_steps = workflow["jobs"]["test"]["steps"]
    rendered_steps = "\n".join(str(step) for step in test_steps)

    assert "Swatinem/rust-cache" not in rendered_steps
    assert "cargo build" not in rendered_steps
    pytest_steps = [
        step for step in test_steps if str(step.get("name", "")).startswith("Run tests")
    ]
    assert pytest_steps
    assert all('-m "not native_bwave"' in step["run"] for step in pytest_steps)


def test_bwave_integration_prebuilds_and_runs_native_tests_without_skips() -> None:
    """The dedicated Linux job owns the binary and fails on a skipped test."""
    workflow = _test_workflow()
    job = workflow["jobs"]["bwave-integration"]
    rendered_steps = "\n".join(str(step) for step in job["steps"])

    assert job["runs-on"] == "ubuntu-latest"
    assert "cargo build --locked" in rendered_steps
    assert "test -x crates/bwave/target/debug/bwave" in rendered_steps
    assert "pytest crates/bwave/tests/test_*.py" in rendered_steps
    assert "pytest tests/ -m native_bwave" in rendered_steps
    assert "skipped == 0" in rendered_steps


def test_primary_pytest_commands_emit_timing_and_junit_data() -> None:
    """CI retains test-level evidence for later scheduling decisions."""
    workflow = _test_workflow()
    primary_jobs = ("test", "bwave-integration")

    for job_name in primary_jobs:
        pytest_commands = [
            step["run"]
            for step in workflow["jobs"][job_name]["steps"]
            if "pytest " in str(step.get("run", ""))
        ]
        assert pytest_commands, job_name
        assert all("--durations=30" in command for command in pytest_commands), job_name
        assert all("--junitxml=" in command for command in pytest_commands), job_name


def test_coverage_leg_combines_xdist_and_subprocess_coverage() -> None:
    """The coverage leg is parallel without dropping child-process data."""
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = project["project"]["optional-dependencies"]["dev"]
    coverage_config = project["tool"]["coverage"]["run"]
    workflow = _test_workflow()
    coverage_step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name") == "Run tests with coverage"
    )
    command = coverage_step["run"]

    assert "pytest-cov==7.1.0" in dev_dependencies
    assert coverage_config["patch"] == ["subprocess"]
    assert "coverage run" not in command
    assert "pytest tests/" in command
    assert "-n 4 --dist=loadscope" in command
    assert '-m "not native_bwave"' in command
    assert "--cov=booley" in command
    assert "--cov-report=" in command
    assert "--cov-fail-under=60" in command
    assert "coverage report --fail-under=60" in command
    assert "coverage xml -o coverage.xml" in command
