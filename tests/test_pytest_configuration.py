"""Regression tests for the repository's pytest process configuration."""

from __future__ import annotations

import ntpath
from pathlib import Path

import pytest


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
