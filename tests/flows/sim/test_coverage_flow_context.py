from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim import coverage_flow_context
from booley.runtime.project_dir import reset_cache, resolve_project_dir


def test_context_uses_checkout_local_legacy_config_over_cached_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    local = root / ".booley_project"
    override = tmp_path / "override"
    local.mkdir(parents=True)
    override.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (local / "pipeline.toml").write_text(
        '[coverage.waivers]\nanchor = "rtl_repository"\ndirectory = "approved-waivers"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(override))
    reset_cache()
    assert resolve_project_dir() == override
    monkeypatch.setattr(coverage_flow_context, "_coverage_policies", lambda *_args: {})
    monkeypatch.setattr(
        coverage_flow_context,
        "load_test_configuration_field",
        lambda *_args: {},
    )

    context = coverage_flow_context.coverage_project_context(root, SimpleNamespace())

    assert context.project_data_repository == local
    assert context.waiver_config is not None
    assert context.waiver_config.anchor == "rtl_repository"
    assert context.waiver_config.directory == "approved-waivers"
