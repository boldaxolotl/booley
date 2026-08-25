"""Unit tests for Session Runtime-only Flow selection."""

from booley.flows.execution import flow_enabled


def _project(tmp_path, toml_text: str):
    state = tmp_path / ".booley_project"
    state.mkdir()
    (state / "booley.toml").write_text(toml_text, encoding="utf-8")
    return tmp_path


def test_defaults_enabled_without_config(tmp_path):
    assert flow_enabled("sim", tmp_path) is True


def test_enabled_false_read_from_flow_section(tmp_path):
    root = _project(tmp_path, "[flows.sim]\nenabled = false\n")
    assert flow_enabled("sim", root) is False
