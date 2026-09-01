"""Init validation for host-dependent EDA provisioning."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.eda import config as eda_config
from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime.project_dir import reset_cache


def test_init_rejects_host_provisioning_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project_dir = project / ".booley_project"
    project_dir.mkdir(parents=True)
    (project_dir / "booley.toml").write_text(
        '[eda.vivado]\nprovisioning = "host"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.setattr(eda_config.sys, "platform", "win32")
    monkeypatch.setattr(init_cmd, "_devcontainer_is_tracked", lambda _project: False)
    monkeypatch.setattr(init_cmd, "_select_interactive_app", lambda _project: "none")
    reset_cache()
    try:
        context = InitContext(project_root=project)
        init_cmd._step_interactive(context, nangate_pdk_root=tmp_path / "pdk")
    finally:
        reset_cache()

    assert context.results[-1].name == "interactive"
    assert context.results[-1].status == "err"
    assert "host provisioning is unsupported on Windows" in context.results[-1].detail
