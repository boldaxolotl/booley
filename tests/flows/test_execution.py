"""Unit tests for Session Runtime-only Flow selection."""

import pytest

from booley.flows.execution import FlowConfigError, flow_enabled, flow_enabled_from_config


def _project(tmp_path, toml_text: str):
    state = tmp_path / ".booley_project"
    state.mkdir()
    (state / "booley.toml").write_text(toml_text, encoding="utf-8")
    return tmp_path


def test_defaults_enabled_without_config(tmp_path):
    assert flow_enabled("sim", tmp_path) is True


def test_malformed_config_preserves_legacy_enabled_default(tmp_path):
    root = _project(tmp_path, "[flows.sim\nenabled = false\n")
    assert flow_enabled("sim", root) is True


@pytest.mark.parametrize("config", [None, [], {"flows": []}])
def test_non_mapping_config_preserves_enabled_default(config):
    assert flow_enabled_from_config("sim", config) is True


@pytest.mark.parametrize("value", [None, 0, "false", {}])
def test_only_explicit_boolean_false_disables_flow(value):
    assert flow_enabled_from_config("sim", {"flows": {"sim": {"enabled": value}}}) is True


def test_enabled_false_read_from_flow_section(tmp_path):
    root = _project(tmp_path, "[flows.sim]\nenabled = false\n")
    assert flow_enabled("sim", root) is False


def test_parsed_config_enablement_uses_shared_resolver():
    assert flow_enabled_from_config("sim", {"flows": {"sim": {"enabled": False}}}) is False


@pytest.mark.parametrize("backend", ["docker", "host"])
def test_retired_backend_is_a_hard_migration_error(backend):
    config = {"flows": {"sim": {"backend": backend}}}

    with pytest.raises(FlowConfigError, match=r"all Flows run inside the Session Runtime"):
        flow_enabled_from_config("sim", config)


def test_retired_none_backend_points_to_enabled_false():
    config = {"flows": {"sim": {"backend": "none"}}}

    with pytest.raises(FlowConfigError, match=r"enabled = false"):
        flow_enabled_from_config("sim", config)


def test_loaded_project_backend_is_not_silently_ignored(tmp_path):
    root = _project(tmp_path, '[flows.sim]\nbackend = "docker"\n')

    with pytest.raises(FlowConfigError, match=r"delete the key"):
        flow_enabled("sim", root)


def test_linked_worktree_without_local_config_uses_main_project_config(tmp_path):
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    git_dir = main / ".git" / "worktrees" / "feature"
    git_dir.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _project(main, "[flows.fpga]\nenabled = false\n")

    assert flow_enabled("fpga", worktree) is False


@pytest.mark.parametrize("retired", ["elab", "elaborate"])
@pytest.mark.parametrize("requested", ["sim", "lint"])
def test_every_flow_rejects_retired_elaboration_tables(retired, requested):
    config = {"flows": {retired: {"standalone_frontend": "iverilog"}}}

    with pytest.raises(
        FlowConfigError,
        match=rf"flows\.{retired}.*sim --mode elab-only.*flows\.sim",
    ):
        flow_enabled_from_config(requested, config)
