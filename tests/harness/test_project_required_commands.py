"""Required CLI commands refuse absent Project context before dispatch."""

from __future__ import annotations

import argparse
import sys

import pytest

from booley.harness import booley as cli
from booley.runtime.project_dir import reset_cache


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
    for variable in ("BOOLEY_PROJECT_DIR", "RTL_PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(variable, raising=False)
    reset_cache()
    monkeypatch.setattr(sys, "argv", ["booley", *path])
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)
    assert cli.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "ERROR: not a Booley Project; cd into one or run `booley init` on the host.\n"
    )


@pytest.mark.parametrize("argv", [["targets"], ["upgrade", "status"]])
def test_missing_project_with_real_parser(argv, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
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


def test_host_refusal_preserves_arguments(monkeypatch, capsys):
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(sys, "argv", ["booley", "run", "--slug", "ticket with spaces"])
    with pytest.raises(SystemExit):
        cli._enforce_runtime_location("run")
    import os

    invocation = (
        'booley run --slug "ticket with spaces"'
        if os.name == "nt"
        else "booley run --slug 'ticket with spaces'"
    )
    assert f"booley session enter -- {invocation}" in capsys.readouterr().err


def test_bare_host_invocation_keeps_venue_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["booley"])
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "runs inside the Booley Sandbox" in captured.err
    assert "not a Booley Project" not in captured.err


@pytest.mark.parametrize("selection", ["explicit", "stale-env", "source-env"])
def test_invalid_project_selection_is_a_clean_cli_error(selection, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    target = tmp_path / "selected"
    target.mkdir()
    argv = ["booley", "doctor"]
    if selection == "explicit":
        argv += ["--project", str(target)]
    elif selection == "stale-env":
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(target / "missing"))
    else:
        (target / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(target / ".booley_project"))
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
        raise PermissionError("Project data is not readable")

    monkeypatch.setattr("booley.runtime.project_dir.resolve_checkout_project_dir", refuse)
    assert cli.main() == 2
    assert capsys.readouterr().err == (
        "ERROR: cannot access Booley Project data: Project data is not readable\n"
    )
