"""Required CLI commands refuse absent Project context before dispatch."""

from __future__ import annotations

import argparse
import sys

import pytest

from booley.harness import booley as cli
from booley.runtime.booley_dir import reset_cache
from booley.runtime.docs_links import SITE_URL


def _command_paths(parser, prefix=()):
    arguments = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        if action.required:
            value = str(next(iter(action.choices))) if action.choices else "example"
            if action.type in (int, float):
                value = "1"
            if action.option_strings:
                arguments.append(action.option_strings[0])
            count = action.nargs if isinstance(action.nargs, int) else 1
            arguments.extend([value] * count)
    prefix = (*prefix, *arguments)
    subcommands = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None
    )
    if subcommands is None:
        return [prefix]
    parents = [] if subcommands.required or not prefix else [prefix]
    return parents + [
        path
        for name, child in subcommands.choices.items()
        for path in _command_paths(child, (*prefix, name))
    ]


REQUIRED_PATHS = [
    path
    for path in _command_paths(cli._build_parser())
    if cli._command_project_binding(path[0], cli._build_parser().parse_args(list(path)))
    is cli.ProjectBinding.REQUIRED
]


@pytest.mark.parametrize(
    ("path", "git_checkout"),
    [(path, git) for path in REQUIRED_PATHS for git in (False, True) if path[0] != "init"],
    ids=lambda value: " ".join(value) if isinstance(value, tuple) else str(value),
)
def test_required_commands_refuse_outside_project(
    path, git_checkout, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    if git_checkout:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    for variable in ("BOOLEY_DIR", "RTL_PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(variable, raising=False)
    reset_cache()
    monkeypatch.setattr(sys, "argv", ["booley", *path])
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)
    assert cli.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "ERROR: Cannot determine owning Project; run from the Project Root.\n"
        if path[0] == "session"
        else "ERROR: not a Booley Project; cd into one or run `booley init` on the host.\n"
    )


@pytest.mark.parametrize("argv", [["targets"], ["upgrade", "status"]])
def test_missing_project_with_real_parser(argv, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    reset_cache()
    monkeypatch.setattr(sys, "argv", ["booley", *argv])
    assert cli.main() == 2
    assert "not a Booley Project" in capsys.readouterr().err


@pytest.mark.parametrize("git_checkout", [False, True])
def test_init_accepts_uninitialized_folder(git_checkout, tmp_path, monkeypatch):
    if git_checkout:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["booley", "init"])
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)
    roots = []
    monkeypatch.setitem(cli._EARLY_COMMANDS, "init", lambda _args, root: roots.append(root) or 0)
    assert cli.main() == 0
    assert roots == [tmp_path]


@pytest.mark.parametrize("selection", ["explicit", "stale-env", "source-env"])
def test_invalid_project_selection_is_a_clean_cli_error(selection, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    target = tmp_path / "selected"
    target.mkdir()
    argv = ["booley", "doctor"]
    if selection == "explicit":
        argv += ["--project", str(target)]
    elif selection == "stale-env":
        monkeypatch.setenv("BOOLEY_DIR", str(target / "missing"))
    else:
        (target / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
        monkeypatch.setenv("BOOLEY_DIR", str(target / ".booley"))
    reset_cache()
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ERROR: ")
    assert len(captured.err.splitlines()) == 1
    assert "Traceback" not in captured.err
    assert "UserWarning" not in captured.err


def test_project_selection_permission_error_is_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["booley", "doctor"])

    def refuse(_root):
        raise PermissionError("Booley Directory is not readable")

    monkeypatch.setattr("booley.runtime.booley_dir.resolve_checkout_booley_dir", refuse)
    assert cli.main() == 2
    assert capsys.readouterr().err == (
        "ERROR: cannot access Booley Directory: Booley Directory is not readable\n"
    )


def _unexpected_runtime_work(*_args, **_kwargs):
    raise AssertionError("help or argument error reached runtime work")


@pytest.mark.parametrize("directory", ["plain", "git", "source", "project"])
@pytest.mark.parametrize("argv", [[], ["--project", "missing"]])
def test_bare_host_help_precedes_project_and_runtime_work(
    directory, argv, tmp_path, monkeypatch, capsys
):
    if directory != "plain":
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    if directory == "source":
        (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    if directory == "project":
        (tmp_path / ".booley").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / "stale"))
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(sys, "argv", ["booley", *argv])
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(cli, "find_project_root", _unexpected_runtime_work)
    monkeypatch.setattr(cli, "_resolve_cli_selection", _unexpected_runtime_work)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", _unexpected_runtime_work)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    output = capsys.readouterr()
    assert output.err == ""
    for text in (
        "usage: booley",
        "chat",
        "board",
        "doctor",
        "booley bootstrap",
        "booley init",
        "devcontainer",
        f"{SITE_URL}docs/setup/",
    ):
        assert text in output.out


@pytest.mark.parametrize("inside", [False, True])
@pytest.mark.parametrize(
    "flag", ["--board", "-b", "--cheat", "--doctor", "--unknown", "--project"]
)
def test_invalid_top_level_options_fail_before_dispatch(inside, flag, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["booley", flag])
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: inside)
    monkeypatch.setattr(cli, "find_project_root", _unexpected_runtime_work)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", _unexpected_runtime_work)
    monkeypatch.setattr(cli, "_handle_early_exits", _unexpected_runtime_work)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert (
        "expected one argument" if flag == "--project" else "unrecognized arguments"
    ) in output.err


@pytest.mark.parametrize("inside", [False, True])
@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_information_options_need_no_project(inside, flag, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["booley", flag])
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: inside)
    monkeypatch.setattr(cli, "find_project_root", _unexpected_runtime_work)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", _unexpected_runtime_work)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    output = capsys.readouterr()
    assert output.out
    assert output.err == ""


@pytest.fixture
def cli_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (project / ".booley").mkdir()
    monkeypatch.chdir(project)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", lambda: False)
    reset_cache()
    return project


@pytest.mark.parametrize("selector", [False, True])
def test_bare_sandbox_dispatches_chat_with_selected_project(
    selector, cli_project, tmp_path, monkeypatch
):
    argv = ["booley"]
    if selector:
        monkeypatch.chdir(tmp_path)
        argv += ["--project", str(cli_project)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    calls = []
    monkeypatch.setitem(
        cli._EARLY_COMMANDS, "chat", lambda args, root: calls.append((args.command, root)) or 17
    )
    assert cli.main() == 17
    assert calls == [("chat", cli_project)]


def test_bare_and_explicit_chat_keep_launch_argv(cli_project, monkeypatch):
    from booley.harness import chat_cmd

    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(chat_cmd, "resolve_checkout_booley_dir", lambda root: root)
    monkeypatch.setattr(chat_cmd, "load_agent_settings", lambda _root, *, booley_dir: None)
    monkeypatch.setattr(chat_cmd, "get_agent_settings", type("Config", (), {"provider": "claude"}))
    # The chat launcher resolves the CLI first; CI hosts have no claude on PATH.
    monkeypatch.setattr(chat_cmd.shutil, "which", lambda provider, **_kw: f"/bin/{provider}")
    monkeypatch.setattr(chat_cmd, "capture_process_identity", lambda _pid: object())
    monkeypatch.setattr(chat_cmd, "process_session_key", lambda _identity: "key")
    monkeypatch.setattr(chat_cmd, "reserve_session_handle", lambda *_args: "S17")
    executed = []

    def execute(executable, argv):
        executed.append((executable, argv))
        raise SystemExit(0)

    monkeypatch.setattr(chat_cmd.os, "execvp", execute)
    for argv in (["booley"], ["booley", "chat"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            cli.main()
    assert executed == [("claude", ["S17", "-n", "S17"])] * 2


@pytest.mark.parametrize("source", [False, True])
def test_bare_sandbox_preserves_project_guards(source, tmp_path, monkeypatch, capsys):
    if source:
        (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["booley"])
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setitem(cli._EARLY_COMMANDS, "chat", _unexpected_runtime_work)
    assert cli.main() == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "inside"),
    [("goal", True), ("cheat", False), ("cheat", True), ("doctor", False), ("doctor", True)],
)
def test_canonical_replacements_dispatch(command, inside, cli_project, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["booley", command, *(["status"] if command == "goal" else [])]
    )
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: inside)
    calls = []
    monkeypatch.setitem(
        cli._EARLY_COMMANDS, command, lambda args, root: calls.append((args.command, root)) or 19
    )
    assert cli.main() == 19
    expected_root = None if command == "cheat" else cli_project
    assert calls == [(command, expected_root)]


@pytest.mark.parametrize("command", ["chat", "goal", "dashboard"])
def test_explicit_sandbox_commands_still_refuse_host(command, cli_project, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["booley", command, *(["status"] if command == "goal" else [])]
    )
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setitem(cli._EARLY_COMMANDS, command, _unexpected_runtime_work)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "runs inside the Booley Sandbox" in capsys.readouterr().err


@pytest.mark.parametrize("location", ["plain", "orphan-worktree"])
def test_session_without_owner_requires_project_root(tmp_path, monkeypatch, capsys, location):
    monkeypatch.chdir(tmp_path)
    if location == "orphan-worktree":
        (tmp_path / ".git").write_text("gitdir: /missing/worktrees/orphan\n")
    for key in ("BOOLEY_DIR", "RTL_PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    reset_cache()
    effects = []
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)
    monkeypatch.setitem(cli._EARLY_COMMANDS, "session", lambda *_: effects.append("dispatch") or 0)
    monkeypatch.setattr(sys, "argv", ["booley", "session", "status"])
    result = cli.main()
    text = capsys.readouterr().err
    assert "run from the Project Root" in text, (
        "unresolvable session must direct the user to the Project Root"
    )
    assert result == 2 and not effects
    assert "booley init" not in text and "Traceback" not in text
