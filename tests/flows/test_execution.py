"""Unit tests for Session Runtime-only Flow selection."""

from booley.flows.execution import ExecutionSelection, execution_error, resolve_execution


def _project(tmp_path, toml_text: str):
    state = tmp_path / ".booley_project"
    state.mkdir()
    (state / "booley.toml").write_text(toml_text, encoding="utf-8")
    return tmp_path


def test_defaults_enabled_without_config(tmp_path):
    assert resolve_execution("sim", tmp_path) == ExecutionSelection()


def test_enabled_false_read_from_flow_section(tmp_path):
    root = _project(tmp_path, "[flows.sim]\nenabled = false\n")
    assert resolve_execution("sim", root).enabled is False


def test_surviving_backend_is_carried_for_migration(tmp_path):
    root = _project(tmp_path, '[flows.sim]\nbackend = "project-native"\n')
    assert resolve_execution("sim", root).legacy_backend == "project-native"


def test_backend_migration_names_session_runtime():
    err = execution_error("sim", ExecutionSelection(legacy_backend="project-native"))
    assert err is not None and "Session Runtime" in err and "Delete" in err


def test_none_backend_maps_to_enabled_false():
    err = execution_error("lint", ExecutionSelection(legacy_backend="none"))
    assert err is not None and "enabled = false" in err


def test_clean_disabled_selection_is_valid():
    assert execution_error("lint", ExecutionSelection(enabled=False)) is None
