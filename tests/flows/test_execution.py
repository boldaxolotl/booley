"""Unit tests for Sandbox-only Flow selection."""

import pytest

from booley.flows.execution import FlowConfigError, flow_enabled, flow_enabled_from_config


def _project(tmp_path, toml_text: str):
    state = tmp_path / ".booley"
    state.mkdir()
    (state / "booley.toml").write_text(toml_text, encoding="utf-8")
    return tmp_path


def test_defaults_enabled_without_config(tmp_path):
    assert flow_enabled("sim", tmp_path) is True


def test_malformed_config_preserves_legacy_enabled_default(tmp_path):
    root = _project(tmp_path, "[flows.sim\nenabled = false\n")
    assert flow_enabled("sim", root) is True


@pytest.mark.parametrize(
    "config",
    [
        None,
        [],
        {"flows": []},
        *({"flows": {"sim": {"enabled": value}}} for value in (None, 0, "false", {})),
        {
            "flows": {"venue": "x", "sim": {"backend": "none", "host_setup_commands": []}},
            "sandbox": {"passthrough_env": []},
        },
    ],
    ids=[
        "none",
        "list",
        "flows-list",
        "enabled-none",
        "enabled-0",
        "enabled-str",
        "enabled-map",
        "unread-keys",
    ],
)
def test_only_an_explicit_boolean_false_disables_a_flow(config):
    assert flow_enabled_from_config("sim", config) is True


def test_enabled_false_read_from_flow_section(tmp_path):
    root = _project(tmp_path, "[flows.sim]\nenabled = false\n")
    assert flow_enabled("sim", root) is False


def test_linked_worktree_without_local_config_uses_main_project_config(tmp_path):
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    git_dir = main / ".git" / "worktrees" / "feature"
    git_dir.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _project(main, "[flows.fpga]\nenabled = false\n")

    assert flow_enabled("fpga", worktree) is False


@pytest.mark.parametrize("requested", ["sim", "lint"])
@pytest.mark.parametrize(
    ("section", "pattern"),
    [
        ("sim", r"standalone_frontend is retired.*each Target's simulator"),
        ("elab", r"flows\.elab.*sim --mode elab-only.*delete standalone_frontend"),
        ("elaborate", r"flows\.elaborate.*sim --mode elab-only.*delete standalone_frontend"),
    ],
)
def test_every_flow_rejects_retired_standalone_frontend_tables(requested, section, pattern):
    config = {"flows": {section: {"standalone_frontend": "iverilog"}}}

    with pytest.raises(FlowConfigError, match=pattern):
        flow_enabled_from_config(requested, config)


def test_flow_config_error_names_the_document_it_read(tmp_path):
    root = _project(tmp_path, "[flows.elab]\n")
    with pytest.raises(FlowConfigError, match="retired") as raised:
        flow_enabled("sim", root)
    assert raised.value.path == root / ".booley" / "booley.toml"
