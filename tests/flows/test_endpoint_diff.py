"""Implicit Flow roots reject detached Project data at the use boundary."""

import importlib

import pytest


@pytest.fixture
def _flow_project_root() -> None:
    """Exercise the actual lazy root resolver rather than the synthetic checkout."""


def test_import_is_lazy_but_implicit_operation_refuses(tmp_path, monkeypatch):
    data = tmp_path / ".booley_project"
    data.mkdir()
    monkeypatch.chdir(data)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    from booley.runtime import shared_infra

    importlib.reload(shared_infra)
    from booley.flows.endpoint_diff import _core_classified_sets
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    with pytest.raises(ProjectRootDiscoveryError):
        shared_infra.get_rtl_dir()
    with pytest.raises(ProjectRootDiscoveryError):
        _core_classified_sets(None)
