"""Regression tests for the repository's pytest process configuration."""

from __future__ import annotations

import ntpath
from pathlib import Path

import pytest
import yaml


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
    workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "test.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    test_steps = workflow["jobs"]["test"]["steps"]
    parallel_step = next(step for step in test_steps if step.get("name") == "Run tests (parallel)")
    assert parallel_step["env"]["RUNNER_TEMP"] == "${{ runner.temp }}"
    assert parallel_step["env"]["PYTEST_ADDOPTS"] == "--basetemp=${{ runner.temp }}/pytest"
