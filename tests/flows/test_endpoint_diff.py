"""Implicit Flow roots reject detached Booley Directory at the use boundary."""

import importlib

import pytest


@pytest.fixture
def _flow_project_root() -> None:
    """Exercise the actual lazy root resolver rather than the synthetic checkout."""


def _detached_data_config(tmp_path, monkeypatch, text):
    from booley.runtime import shared_infra
    from booley.runtime.booley_dir import reset_cache

    data = tmp_path / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text(text)
    monkeypatch.chdir(data)
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    reset_cache()
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    with pytest.raises(ProjectRootDiscoveryError):
        shared_infra.resolve_project_root()
    return data


def test_existing_data_budget_does_not_require_checkout(tmp_path, monkeypatch):
    from booley.runtime import shared_infra

    _detached_data_config(tmp_path, monkeypatch, "[jobs]\nmax_heavy = 3\n")
    assert shared_infra.load_job_budget_config() == {"jobs": {"max_heavy": 3}}


def test_empty_sdc_does_not_require_checkout(tmp_path, monkeypatch):
    from booley.flows.synth.backends.yosys import core

    _detached_data_config(tmp_path, monkeypatch, "[flows.synth.timing]\nutilization_pct = 55\n")
    assert core._resolve_sta_sdc_paths([], None) == []
    timing = core.synth_timing_config()
    assert timing.utilization_pct == 55
    assert timing.sdc == ()


@pytest.mark.parametrize("absolute", [False, True])
def test_nonempty_sdc_requires_checkout_containment(tmp_path, monkeypatch, absolute):
    from booley.flows.synth.backends.yosys import core
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    data = _detached_data_config(tmp_path, monkeypatch, "[flows]\n")
    constraint = data / "timing.sdc"
    constraint.write_text("create_clock -period 10 [get_ports clk]\n")
    selected = str(constraint) if absolute else constraint.name
    with pytest.raises(ProjectRootDiscoveryError):
        core._resolve_sta_sdc_paths([selected], None)


def test_import_is_lazy_but_implicit_operation_refuses(tmp_path, monkeypatch):
    data = tmp_path / ".booley"
    data.mkdir()
    monkeypatch.chdir(data)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    from booley.runtime import shared_infra

    importlib.reload(shared_infra)
    from booley.flows.endpoint_diff import _core_classified_sets
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    with pytest.raises(ProjectRootDiscoveryError):
        shared_infra.load_job_budget_config()
    with pytest.raises(ProjectRootDiscoveryError):
        shared_infra.get_rtl_dir()
    with pytest.raises(ProjectRootDiscoveryError):
        _core_classified_sets(None)


def test_shared_directory_diff_classifies_typed_sim_harness_exactly(tmp_path):
    from booley.flows.endpoint_diff import _classify_files
    from tests.test_target_catalog import _category_sim_core

    _category_sim_core(tmp_path)
    assert _classify_files(["shared/main.cpp"], tmp_path) == {"tb"}
    assert _classify_files(["shared/dut.sv"], tmp_path) == {"rtl"}
    assert _classify_files(["shared/new.sv"], tmp_path) == set()
